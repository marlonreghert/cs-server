"""Service for enriching venues with data from Google Places API.

This service handles:
- Vibe attributes (pet friendly, outdoor seating, etc.)
- Business status checks (operational, temporarily/permanently closed)
- Removal of permanently closed venues
- Removal of temporarily closed venues (configurable via remove_temporarily_closed_venues)
"""
import asyncio
import logging
from typing import Optional

from app.api.google_places_client import GooglePlacesAPIClient, search_for_lgbtq_indicators
from app.config import settings
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models.vibe_attributes import VibeAttributes
from app.models.opening_hours import OpeningHours
from app.metrics import (
    VIBE_ATTRIBUTES_FETCH_RESULTS,
    VENUES_WITH_VIBE_ATTRIBUTES,
    VENUES_BY_BUSINESS_STATUS,
    VENUES_PERMANENTLY_CLOSED_REMOVED,
    VENUES_PERMANENTLY_CLOSED_DETECTED,
    VENUES_TEMPORARILY_CLOSED_REMOVED,
    VENUES_TEMPORARILY_CLOSED_DETECTED,
)

logger = logging.getLogger(__name__)

# Rate limiting: Google Places API has quotas
# Default: 10 requests per second for most projects
REQUESTS_PER_SECOND = 5
REQUEST_DELAY = 1.0 / REQUESTS_PER_SECOND


class GooglePlacesEnrichmentService:
    """Service for enriching venues with Google Places API data.

    Coordinates fetching venue data from Google Places API
    and caching it in Redis. This includes:
    - Vibe attributes (pet friendly, outdoor seating, etc.)
    - Business status (operational, temporarily closed, permanently closed)
    - Permanently closed venue detection and removal
    - Temporarily closed venue detection and removal (configurable)
    """

    def __init__(
        self,
        google_places_client: GooglePlacesAPIClient,
        venue_dao: RedisVenueDAO,
    ):
        """Initialize GooglePlacesEnrichmentService.

        Args:
            google_places_client: Google Places API client
            venue_dao: Redis venue DAO for caching
        """
        self.google_places_client = google_places_client
        self.venue_dao = venue_dao
        # Counters for tracking closures during enrichment runs
        self._permanently_closed_in_run = 0
        self._temporarily_closed_in_run = 0

    async def enrich_venue(
        self,
        venue_id: str,
        google_place_id: str,
        force_refresh: bool = False,
    ) -> Optional[VibeAttributes]:
        """Enrich a single venue with Google Places data.

        Fetches vibe attributes and checks business status.
        Removes venue from database if permanently closed.

        Args:
            venue_id: Our internal venue ID
            google_place_id: Google Place ID for the venue
            force_refresh: If True, fetch even if cached entry exists

        Returns:
            VibeAttributes if successful, None on error or if venue was removed
        """
        if not google_place_id:
            logger.warning(f"[GooglePlacesEnrichment] No Google Place ID for venue {venue_id}")
            VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_no_place_id").inc()
            return None

        # Check if already cached (skip fetch if exists and not forcing refresh)
        if not force_refresh:
            existing = self.venue_dao.get_vibe_attributes(venue_id)
            if existing is not None:
                logger.debug(f"[GooglePlacesEnrichment] Already enriched {venue_id}, skipping")
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_cached").inc()
                return existing

        try:
            # Fetch place details from Google
            details = await self.google_places_client.get_place_details(google_place_id)

            if details is None:
                logger.warning(f"[GooglePlacesEnrichment] Failed to fetch details for {google_place_id}")
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="error").inc()
                return None

            # Track business status metric
            status_label = (details.business_status or "unknown").lower()
            VENUES_BY_BUSINESS_STATUS.labels(status=status_label).inc()

            # Check if permanently closed - remove venue from database (if enabled)
            if details.is_permanently_closed():
                if settings.remove_permanently_closed_venues:
                    logger.warning(
                        f"[GooglePlacesEnrichment] Venue {venue_id} is PERMANENTLY CLOSED, "
                        f"removing from database"
                    )
                    self.venue_dao.delete_venue(venue_id)
                    self._permanently_closed_in_run += 1
                    VENUES_PERMANENTLY_CLOSED_REMOVED.inc()
                    VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="removed_permanently_closed").inc()
                    return None
                else:
                    logger.warning(
                        f"[GooglePlacesEnrichment] Venue {venue_id} is PERMANENTLY CLOSED, "
                        f"but removal is disabled by config"
                    )
                    VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_permanently_closed").inc()

            # Check if temporarily closed - remove venue from database (if enabled)
            if details.is_temporarily_closed():
                if settings.remove_temporarily_closed_venues:
                    logger.warning(
                        f"[GooglePlacesEnrichment] Venue {venue_id} is TEMPORARILY CLOSED, "
                        f"removing from database"
                    )
                    self.venue_dao.delete_venue(venue_id)
                    self._temporarily_closed_in_run += 1
                    VENUES_TEMPORARILY_CLOSED_REMOVED.inc()
                    VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="removed_temporarily_closed").inc()
                    return None
                else:
                    logger.info(
                        f"[GooglePlacesEnrichment] Venue {venue_id} is temporarily closed, "
                        f"but removal is disabled by config"
                    )

            # Convert to our vibe attributes model
            vibe_attrs = self.google_places_client.details_to_vibe_attributes(venue_id, details)

            # Check for LGBTQ+ indicators in the summary
            if details.generative_summary or details.editorial_summary:
                summary = details.generative_summary or details.editorial_summary
                vibe_attrs.lgbtq_friendly = await search_for_lgbtq_indicators(summary)

            # Cache the results
            self.venue_dao.set_vibe_attributes(vibe_attrs)
            VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="cached").inc()

            # Store opening hours if available
            if details.weekday_descriptions:
                opening_hours = OpeningHours(
                    venue_id=venue_id,
                    weekday_descriptions=details.weekday_descriptions,
                    open_now=details.open_now,
                    special_days=details.special_days,
                )
                self.venue_dao.set_opening_hours(opening_hours)
                logger.debug(
                    f"[GooglePlacesEnrichment] Stored opening hours for {venue_id}: "
                    f"{len(details.weekday_descriptions)} days"
                )

            logger.info(
                f"[GooglePlacesEnrichment] Enriched {venue_id}: "
                f"labels={vibe_attrs.get_vibe_labels()}"
            )

            return vibe_attrs

        except Exception as e:
            logger.error(f"[GooglePlacesEnrichment] Error enriching venue {venue_id}: {e}")
            VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="error").inc()
            return None

    async def enrich_venues(
        self,
        venue_id_to_place_id: dict[str, str],
        batch_size: int = 50,
    ) -> int:
        """Enrich multiple venues with Google Places data.

        Args:
            venue_id_to_place_id: Mapping of our venue IDs to Google Place IDs
            batch_size: Number of venues to process in parallel

        Returns:
            Number of venues successfully enriched
        """
        logger.info(
            f"[GooglePlacesEnrichment] Starting enrichment for "
            f"{len(venue_id_to_place_id)} venues"
        )

        successful = 0
        venue_items = list(venue_id_to_place_id.items())

        # Process in batches to avoid overwhelming the API
        for i in range(0, len(venue_items), batch_size):
            batch = venue_items[i:i + batch_size]

            for venue_id, place_id in batch:
                result = await self.enrich_venue(venue_id, place_id)
                if result:
                    successful += 1

                # Rate limiting
                await asyncio.sleep(REQUEST_DELAY)

            logger.info(
                f"[GooglePlacesEnrichment] Processed batch {i // batch_size + 1}, "
                f"successful so far: {successful}"
            )

        # Update metrics
        count = self.venue_dao.count_venues_with_vibe_attributes()
        VENUES_WITH_VIBE_ATTRIBUTES.set(count)

        logger.info(
            f"[GooglePlacesEnrichment] Enrichment complete: "
            f"{successful}/{len(venue_id_to_place_id)} venues enriched"
        )

        return successful

    async def enrich_all_venues(self, force_refresh: bool = False) -> int:
        """Enrich all known venues with Google Places data.

        This method fetches all venues from Redis and searches Google Places
        by name/address to get the Google Place ID, then fetches enrichment data.

        Also checks business status from Google Places API and removes
        permanently closed venues from the database.

        Args:
            force_refresh: If True, re-check all venues even if already enriched.
                          Use this to detect venues that have become permanently closed
                          since the last enrichment run.

        Returns:
            Number of venues successfully enriched
        """
        # Get all venue IDs
        all_venue_ids = self.venue_dao.list_all_venue_ids()
        logger.info(
            f"[GooglePlacesEnrichment] Found {len(all_venue_ids)} venues to process "
            f"(force_refresh={force_refresh})"
        )

        if not all_venue_ids:
            logger.warning("[GooglePlacesEnrichment] No venues found in database")
            return 0

        successful = 0
        # Reset closure counters for this run
        self._permanently_closed_in_run = 0
        self._temporarily_closed_in_run = 0

        for venue_id in all_venue_ids:
            # Check if already cached (skip if not forcing refresh)
            existing = self.venue_dao.get_vibe_attributes(venue_id)
            if existing is not None and not force_refresh:
                logger.debug(f"[GooglePlacesEnrichment] Already enriched {venue_id}, skipping")
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_cached").inc()
                continue

            # Log when re-checking already enriched venues
            if existing is not None and force_refresh:
                logger.debug(
                    f"[GooglePlacesEnrichment] Re-checking {venue_id} for permanently closed status"
                )

            # Get venue data to search Google Places
            venue = self.venue_dao.get_venue(venue_id)
            if venue is None:
                logger.warning(f"[GooglePlacesEnrichment] Venue not found: {venue_id}")
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
                logger.warning(f"[GooglePlacesEnrichment] Could not find Google Place ID for {venue.venue_name}")
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_no_place_id").inc()
                await asyncio.sleep(REQUEST_DELAY)
                continue

            # Enrich venue using the Google Place ID
            result = await self.enrich_venue(
                venue_id=venue_id,
                google_place_id=google_place_id,
                force_refresh=True,  # We already checked cache above
            )

            if result:
                successful += 1
            # Note: Closure tracking is done via instance counters in enrich_venue()

            # Rate limiting (extra delay since we make 2 API calls per venue)
            await asyncio.sleep(REQUEST_DELAY * 2)

        # Update metrics
        count = self.venue_dao.count_venues_with_vibe_attributes()
        VENUES_WITH_VIBE_ATTRIBUTES.set(count)
        VENUES_PERMANENTLY_CLOSED_DETECTED.set(self._permanently_closed_in_run)
        VENUES_TEMPORARILY_CLOSED_DETECTED.set(self._temporarily_closed_in_run)

        total_closed = self._permanently_closed_in_run + self._temporarily_closed_in_run
        logger.info(
            f"[GooglePlacesEnrichment] Enrichment complete: "
            f"{successful}/{len(all_venue_ids)} venues enriched, "
            f"{total_closed} closed venues removed "
            f"({self._permanently_closed_in_run} permanent, {self._temporarily_closed_in_run} temporary)"
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
