"""Service for fetching and caching venue photos from Google Places API."""
import asyncio
import logging
import time
from typing import Optional

from app.api.google_places_client import GooglePlacesAPIClient
from app.dao.redis_venue_dao import RedisVenueDAO
from app.config import settings
from app.metrics import (
    VENUE_PHOTO_RESOLVE_TOTAL,
    VENUE_PHOTO_RESOLVE_DURATION_SECONDS,
)

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
        serving_dao: Optional[RedisVenueDAO] = None,
    ):
        """Initialize PhotoEnrichmentService.

        Args:
            google_places_client: Google Places API client
            venue_dao: System-of-record venue DAO (RDS-backed in production). Used
                to read the stored google_place_id and to WRITE the fresh keyless
                cache to Redis (the fresh key is Redis-only, so the RDS repository's
                inherited base method writes it to Redis).
            enrichment_limit: Max venues per run (None = use settings.photo_enrichment_limit)
            serving_dao: Optional Redis-backed DAO used as a FALLBACK to read the
                google_place_id from the Redis venue record when the system of
                record is unavailable or has no value.
        """
        self.google_places_client = google_places_client
        self.venue_dao = venue_dao
        self.enrichment_limit = enrichment_limit
        self.serving_dao = serving_dao

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

    # =========================================================================
    # ON-DEMAND resolution (fresh keyless URLs, short-TTL Redis cache)
    # =========================================================================

    def _lookup_google_place_id(self, venue_id: str) -> Optional[str]:
        """Resolve the venue's stored google_place_id.

        Primary: the system of record (venue_dao vibe attributes — RDS in
        production). Fallback: the Redis venue record (serving_dao) when the
        system of record is unavailable or carries no value. Best-effort — never
        raises; returns None when neither source has a place id.
        """
        for dao in (self.venue_dao, self.serving_dao):
            if dao is None:
                continue
            try:
                vibe = dao.get_vibe_attributes(venue_id)
            except Exception as e:
                logger.warning(
                    f"[PhotoEnrichmentService] vibe-attrs read failed for {venue_id} "
                    f"via {type(dao).__name__}: {e}"
                )
                continue
            if vibe is not None and vibe.google_place_id:
                return vibe.google_place_id
        return None

    def _cache_fresh(self, venue_id: str, photos: list[dict]) -> None:
        """Write the fresh keyless-URL list to Redis (cs-server sole writer).
        A cache-write failure degrades to serving-this-request-only (logged, not
        raised) so a transient Redis hiccup never turns a good resolve into a 5xx."""
        try:
            self.venue_dao.set_venue_photos_fresh(venue_id, photos)
        except Exception as e:
            logger.error(
                f"[PhotoEnrichmentService] Failed to cache fresh photos for {venue_id}: {e}"
            )

    async def resolve_and_cache_fresh_photos(self, venue_id: str) -> list[dict]:
        """Resolve a single venue's Google photos ON DEMAND to FRESH, KEYLESS CDN
        URLs, cache them under venue_photos_fresh_v1:{venue_id}, and return them.

        Contract:
          - No stored google_place_id -> cache an empty list (deterministic within
            the short TTL) and return [].
          - Google returns zero photos -> cache an empty list and return [].
          - Any Google/resolution exception -> return [] WITHOUT writing the fresh
            key, so a later open can retry and a dead URL is never served.

        Returns:
            List of [{url: <keyless>, author_name: str | None}], capped at
            settings.photos_per_venue.
        """
        start = time.perf_counter()
        place_id = self._lookup_google_place_id(venue_id)
        if not place_id:
            logger.info(
                f"[PhotoEnrichmentService] No google_place_id for {venue_id}; "
                f"caching empty fresh-photo list"
            )
            self._cache_fresh(venue_id, [])
            VENUE_PHOTO_RESOLVE_TOTAL.labels(result="empty").inc()
            VENUE_PHOTO_RESOLVE_DURATION_SECONDS.observe(time.perf_counter() - start)
            return []

        try:
            photos = await self.google_places_client.get_place_photos(
                place_id=place_id,
                max_photos=settings.photos_per_venue,
                max_width=800,
            )
        except Exception as e:
            # Never cache on exception — retry-friendly, never serve a dead URL.
            logger.error(
                f"[PhotoEnrichmentService] Fresh photo resolution failed for "
                f"{venue_id} (place {place_id}): {type(e).__name__}: {e}"
            )
            VENUE_PHOTO_RESOLVE_TOTAL.labels(result="error").inc()
            VENUE_PHOTO_RESOLVE_DURATION_SECONDS.observe(time.perf_counter() - start)
            return []

        photos = photos or []
        self._cache_fresh(venue_id, photos)
        VENUE_PHOTO_RESOLVE_TOTAL.labels(result="resolved" if photos else "empty").inc()
        VENUE_PHOTO_RESOLVE_DURATION_SECONDS.observe(time.perf_counter() - start)
        logger.info(
            f"[PhotoEnrichmentService] Resolved {len(photos)} fresh photos for {venue_id}"
        )
        return photos

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

        # Gate on the serving view (active AND eligible) so ineligible venues
        # never burn photo budget; unlabeled venues stay in scope.
        all_venue_ids = self.venue_dao.list_servable_venue_ids()
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
                # Cache empty list so we don't retry this venue every run
                self.venue_dao.set_venue_photos(venue_id, [])
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
