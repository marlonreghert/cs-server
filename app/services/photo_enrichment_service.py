"""Service for fetching and caching venue photos from Google Places API."""
import asyncio
import logging
from typing import Optional

from app.api.google_places_client import GooglePlacesAPIClient
from app.dao.redis_venue_dao import RedisVenueDAO
from app.config import settings

logger = logging.getLogger(__name__)

# Rate limiting: Google Places API has quotas
REQUESTS_PER_SECOND = 5
REQUEST_DELAY = 1.0 / REQUESTS_PER_SECOND


class PhotoEnrichmentService:
    """Service for managing venue photos.

    Coordinates fetching photos from Google Places API
    and caching them in Redis.
    """

    def __init__(
        self,
        google_places_client: GooglePlacesAPIClient,
        venue_dao: RedisVenueDAO,
        enrichment_limit: Optional[int] = None,
    ):
        """Initialize PhotoEnrichmentService.

        Args:
            google_places_client: Google Places API client
            venue_dao: Redis venue DAO for caching
            enrichment_limit: Max venues per run (None = use settings.photo_enrichment_limit)
        """
        self.google_places_client = google_places_client
        self.venue_dao = venue_dao
        self.enrichment_limit = enrichment_limit

    async def fetch_and_cache_photos(
        self,
        venue_id: str,
        google_place_id: str,
        max_photos: int = 5,
        force_refresh: bool = False,
    ) -> Optional[list[dict]]:
        """Fetch photos for a single venue and cache them.

        Args:
            venue_id: Our internal venue ID
            google_place_id: Google Place ID for the venue
            max_photos: Maximum number of photos to fetch
            force_refresh: If True, fetch even if cached entry exists

        Returns:
            List of photo dicts [{url, author_name}] if successful, None on error
        """
        if not google_place_id:
            logger.warning(f"[PhotoEnrichmentService] No Google Place ID for venue {venue_id}")
            return None

        # Check if already cached (skip fetch if exists and not forcing refresh)
        if not force_refresh:
            existing = self.venue_dao.get_venue_photos(venue_id)
            if existing is not None:
                logger.debug(f"[PhotoEnrichmentService] Photos already cached for {venue_id}, skipping fetch")
                return existing

        try:
            # Fetch photos from Google Places API
            photos = await self.google_places_client.get_place_photos(
                place_id=google_place_id,
                max_photos=max_photos,
            )

            if not photos:
                logger.debug(f"[PhotoEnrichmentService] No photos found for {venue_id}")
                # Cache empty list to avoid re-fetching
                self.venue_dao.set_venue_photos(venue_id, [])
                return []

            # Cache the results
            self.venue_dao.set_venue_photos(venue_id, photos)

            logger.info(
                f"[PhotoEnrichmentService] Cached {len(photos)} photos for {venue_id}"
            )

            return photos

        except Exception as e:
            logger.error(f"[PhotoEnrichmentService] Error fetching photos for {venue_id}: {e}")
            return None

    async def refresh_photos_for_venues(
        self,
        limit: Optional[int] = None,
        max_photos_per_venue: Optional[int] = None,
    ) -> int:
        """Refresh photos for venues that don't have them cached.

        This method processes all cached venues that don't have photos yet.

        Args:
            limit: Maximum number of venues to process (uses config default if None)
            max_photos_per_venue: Maximum photos per venue (uses config default if None)

        Returns:
            Number of venues successfully updated
        """
        # Use config values if not specified
        if limit is None:
            limit = self.enrichment_limit if self.enrichment_limit is not None else settings.photo_enrichment_limit
        if max_photos_per_venue is None:
            max_photos_per_venue = settings.photos_per_venue

        # Get all cached venues
        all_venue_ids = self.venue_dao.list_all_venue_ids()
        venues_with_photos = set(self.venue_dao.list_cached_venue_photos_ids())

        # Filter to only venues without photos
        venues_to_process = [
            vid for vid in all_venue_ids
            if vid not in venues_with_photos
        ]

        # Apply limit
        venues_to_process = venues_to_process[:limit]

        logger.info(
            f"[PhotoEnrichmentService] Starting photo enrichment for "
            f"{len(venues_to_process)} venues (limit={limit})"
        )

        if not venues_to_process:
            logger.info("[PhotoEnrichmentService] No venues need photo enrichment")
            return 0

        successful = 0

        for venue_id in venues_to_process:
            # Get venue data to search Google Places
            venue = self.venue_dao.get_venue(venue_id)
            if venue is None:
                logger.warning(f"[PhotoEnrichmentService] Venue not found: {venue_id}")
                continue

            # Search Google Places by venue name/address to get Place ID
            google_place_id = await self.google_places_client.search_place_id(
                venue_name=venue.venue_name,
                venue_address=venue.venue_address,
                lat=venue.venue_lat,
                lng=venue.venue_lng,
            )

            if not google_place_id:
                logger.warning(f"[PhotoEnrichmentService] Could not find Google Place ID for {venue.venue_name}")
                await asyncio.sleep(REQUEST_DELAY)
                continue

            # Fetch and cache photos
            result = await self.fetch_and_cache_photos(
                venue_id=venue_id,
                google_place_id=google_place_id,
                max_photos=max_photos_per_venue,
                force_refresh=True,
            )

            if result is not None:  # Empty list is still a successful result
                successful += 1

            # Rate limiting (extra delay since we make 2 API calls per venue)
            await asyncio.sleep(REQUEST_DELAY * 2)

        logger.info(
            f"[PhotoEnrichmentService] Photo enrichment complete: "
            f"{successful}/{len(venues_to_process)} venues updated"
        )

        return successful

    def get_venue_photos(self, venue_id: str) -> Optional[list[dict]]:
        """Get cached photos for a venue.

        Args:
            venue_id: Venue identifier

        Returns:
            List of photo dicts [{url, author_name}] or None if not cached
        """
        return self.venue_dao.get_venue_photos(venue_id)
