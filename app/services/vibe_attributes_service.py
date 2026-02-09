"""Service for fetching and managing venue vibe attributes."""
import asyncio
import logging
from typing import Optional

from app.api.google_places_client import GooglePlacesAPIClient, search_for_lgbtq_indicators
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models.vibe_attributes import VibeAttributes
from app.metrics import VIBE_ATTRIBUTES_FETCH_RESULTS, VENUES_WITH_VIBE_ATTRIBUTES

logger = logging.getLogger(__name__)

# Rate limiting: Google Places API has quotas
# Default: 10 requests per second for most projects
REQUESTS_PER_SECOND = 5
REQUEST_DELAY = 1.0 / REQUESTS_PER_SECOND


class VibeAttributesService:
    """Service for managing venue vibe attributes.

    Coordinates fetching vibe attributes from Google Places API
    and caching them in Redis.
    """

    def __init__(
        self,
        google_places_client: GooglePlacesAPIClient,
        venue_dao: RedisVenueDAO,
    ):
        """Initialize VibeAttributesService.

        Args:
            google_places_client: Google Places API client
            venue_dao: Redis venue DAO for caching
        """
        self.google_places_client = google_places_client
        self.venue_dao = venue_dao

    async def fetch_and_cache_vibe_attributes(
        self,
        venue_id: str,
        google_place_id: str,
        force_refresh: bool = False,
    ) -> Optional[VibeAttributes]:
        """Fetch vibe attributes for a single venue and cache them.

        Args:
            venue_id: Our internal venue ID
            google_place_id: Google Place ID for the venue
            force_refresh: If True, fetch even if cached entry exists

        Returns:
            VibeAttributes if successful, None on error
        """
        if not google_place_id:
            logger.warning(f"[VibeAttributesService] No Google Place ID for venue {venue_id}")
            VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_no_place_id").inc()
            return None

        # Check if already cached (skip fetch if exists and not forcing refresh)
        if not force_refresh:
            existing = self.venue_dao.get_vibe_attributes(venue_id)
            if existing is not None:
                logger.debug(f"[VibeAttributesService] Vibe attributes already cached for {venue_id}, skipping fetch")
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_cached").inc()
                return existing

        try:
            # Fetch place details from Google
            details = await self.google_places_client.get_place_details(google_place_id)

            if details is None:
                logger.warning(f"[VibeAttributesService] Failed to fetch details for {google_place_id}")
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="error").inc()
                return None

            # Convert to our vibe attributes model
            vibe_attrs = self.google_places_client.details_to_vibe_attributes(venue_id, details)

            # Check for LGBTQ+ indicators in the summary
            if details.generative_summary or details.editorial_summary:
                summary = details.generative_summary or details.editorial_summary
                vibe_attrs.lgbtq_friendly = await search_for_lgbtq_indicators(summary)

            # Cache the results
            self.venue_dao.set_vibe_attributes(vibe_attrs)
            VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="cached").inc()

            logger.info(
                f"[VibeAttributesService] Cached vibe attributes for {venue_id}: "
                f"labels={vibe_attrs.get_vibe_labels()}"
            )

            return vibe_attrs

        except Exception as e:
            logger.error(f"[VibeAttributesService] Error fetching vibe attributes for {venue_id}: {e}")
            VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="error").inc()
            return None

    async def refresh_vibe_attributes_for_venues(
        self,
        venue_id_to_place_id: dict[str, str],
        batch_size: int = 50,
    ) -> int:
        """Refresh vibe attributes for multiple venues.

        Args:
            venue_id_to_place_id: Mapping of our venue IDs to Google Place IDs
            batch_size: Number of venues to process in parallel

        Returns:
            Number of venues successfully updated
        """
        logger.info(
            f"[VibeAttributesService] Starting vibe attributes refresh for "
            f"{len(venue_id_to_place_id)} venues"
        )

        successful = 0
        venue_items = list(venue_id_to_place_id.items())

        # Process in batches to avoid overwhelming the API
        for i in range(0, len(venue_items), batch_size):
            batch = venue_items[i:i + batch_size]

            for venue_id, place_id in batch:
                result = await self.fetch_and_cache_vibe_attributes(venue_id, place_id)
                if result:
                    successful += 1

                # Rate limiting
                await asyncio.sleep(REQUEST_DELAY)

            logger.info(
                f"[VibeAttributesService] Processed batch {i // batch_size + 1}, "
                f"successful so far: {successful}"
            )

        # Update metrics
        count = self.venue_dao.count_venues_with_vibe_attributes()
        VENUES_WITH_VIBE_ATTRIBUTES.set(count)

        logger.info(
            f"[VibeAttributesService] Vibe attributes refresh complete: "
            f"{successful}/{len(venue_id_to_place_id)} venues updated"
        )

        return successful

    async def refresh_vibe_attributes_for_all_venues(self) -> int:
        """Refresh vibe attributes for all known venues.

        This method fetches all venues from Redis and searches Google Places
        by name/address to get the Google Place ID, then fetches vibe attributes.

        Returns:
            Number of venues successfully updated
        """
        # Get all venue IDs
        all_venue_ids = self.venue_dao.list_all_venue_ids()
        logger.info(f"[VibeAttributesService] Found {len(all_venue_ids)} venues to process")

        if not all_venue_ids:
            logger.warning("[VibeAttributesService] No venues found in database")
            return 0

        successful = 0

        for venue_id in all_venue_ids:
            # Check if already cached
            existing = self.venue_dao.get_vibe_attributes(venue_id)
            if existing is not None:
                logger.debug(f"[VibeAttributesService] Vibe attributes already cached for {venue_id}, skipping")
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_cached").inc()
                continue

            # Get venue data to search Google Places
            venue = self.venue_dao.get_venue(venue_id)
            if venue is None:
                logger.warning(f"[VibeAttributesService] Venue not found: {venue_id}")
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_no_venue").inc()
                continue

            # Search Google Places by venue name/address to get Place ID
            google_place_id = await self.google_places_client.search_place_id(
                venue_name=venue.venue_name,
                venue_address=venue.venue_address,
                lat=venue.venue_lat,
                lng=venue.venue_lng,
            )

            if not google_place_id:
                logger.warning(f"[VibeAttributesService] Could not find Google Place ID for {venue.venue_name}")
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_no_place_id").inc()
                await asyncio.sleep(REQUEST_DELAY)
                continue

            # Fetch vibe attributes using the Google Place ID
            result = await self.fetch_and_cache_vibe_attributes(
                venue_id=venue_id,
                google_place_id=google_place_id,
                force_refresh=True,  # We already checked cache above
            )

            if result:
                successful += 1

            # Rate limiting (extra delay since we make 2 API calls per venue)
            await asyncio.sleep(REQUEST_DELAY * 2)

        # Update metrics
        count = self.venue_dao.count_venues_with_vibe_attributes()
        VENUES_WITH_VIBE_ATTRIBUTES.set(count)

        logger.info(
            f"[VibeAttributesService] Vibe attributes refresh complete: "
            f"{successful}/{len(all_venue_ids)} venues updated"
        )

        return successful

    def get_vibe_attributes(self, venue_id: str) -> Optional[VibeAttributes]:
        """Get cached vibe attributes for a venue.

        Args:
            venue_id: Venue identifier

        Returns:
            VibeAttributes or None if not cached
        """
        return self.venue_dao.get_vibe_attributes(venue_id)

    def get_vibe_labels(self, venue_id: str) -> list[str]:
        """Get human-readable vibe labels for a venue.

        Args:
            venue_id: Venue identifier

        Returns:
            List of vibe label strings (e.g., ["LGBTQ+ Friendly", "Pet Friendly"])
        """
        attrs = self.get_vibe_attributes(venue_id)
        if attrs:
            return attrs.get_vibe_labels()
        return []
