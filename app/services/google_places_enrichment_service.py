"""Service for enriching venues with data from Google Places API.

This service handles:
- Vibe attributes (pet friendly, outdoor seating, etc.)
- Business status checks (operational, temporarily/permanently closed)
- Soft-deprecation of permanently closed venues
- Active retention of temporarily closed venues
- Instagram handle extraction from venue website URLs
"""
import asyncio
import logging
import re
from typing import Optional

from app.api.google_places_client import GooglePlacesAPIClient, search_for_lgbtq_indicators
from app.config import settings
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models.vibe_attributes import VibeAttributes
from app.models.opening_hours import OpeningHours
from app.models.instagram import VenueInstagram
from app.models.venue_review import VenueReview, VenueReviews
from app.metrics import (
    VIBE_ATTRIBUTES_FETCH_RESULTS,
    VENUES_WITH_VIBE_ATTRIBUTES,
    VENUES_BY_BUSINESS_STATUS,
    VENUES_PERMANENTLY_CLOSED_DETECTED,
    VENUES_TEMPORARILY_CLOSED_DETECTED,
    VENUES_DEPRECATED_TOTAL,
    VENUES_SOFT_DELETED_TOTAL,
    INSTAGRAM_ENRICHMENT_RESULTS,
)

logger = logging.getLogger(__name__)

# Rate limiting: Google Places API has quotas
# Default: 10 requests per second for most projects
REQUESTS_PER_SECOND = 5
REQUEST_DELAY = 1.0 / REQUESTS_PER_SECOND

# Google Places API v1 returns priceLevel as an enum string. We persist it as
# the 1-4 int the rest of the system (mobile PriceIndicator, scoring, etc.)
# expects. PRICE_LEVEL_FREE / _UNSPECIFIED resolve to None — neither maps to
# the 1-4 scale, and we'd rather leave the field empty than misrepresent.
_PRICE_LEVEL_ENUM_TO_INT = {
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}


def _price_level_to_int(value: Optional[str]) -> Optional[int]:
    """Map Google's priceLevel enum string to our 1-4 int scale."""
    if not value:
        return None
    return _PRICE_LEVEL_ENUM_TO_INT.get(value)


class GooglePlacesEnrichmentService:
    """Service for enriching venues with Google Places API data.

    Coordinates fetching venue data from Google Places API
    and caching it in Redis. This includes:
    - Vibe attributes (pet friendly, outdoor seating, etc.)
    - Business status (operational, temporarily closed, permanently closed)
    - Permanently closed venue detection and soft-deprecation
    - Temporarily closed venue status tracking without deprecation
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

    def _backfill_venue_review_signal(self, venue_id: str, details) -> None:
        """Write Google's rating/userRatingCount/priceLevel onto the Venue.

        The Venue model's `rating`, `reviews`, and `price_level` fields are
        what the venue card UI reads. The BestTime venue_filter discovery
        path populates them at ingestion; the inventory-sync path (added in
        #18) does not. Without this backfill, ~720 inventory-synced venues
        in prod (Praça Laura Nigro, Jockey Club, Barchef, …) stay null
        forever even though Google has the data.

        Reads only — no Google call here. Skips persistence when every
        Google value is None (no-op upsert would still rewrite the venue).
        Preserves any pre-existing non-null Venue value when Google returns
        None for that specific field (don't blank out BestTime-sourced
        data).
        """
        google_rating = details.rating
        google_review_count = details.user_rating_count
        google_price_int = _price_level_to_int(details.price_level)

        if google_rating is None and google_review_count is None and google_price_int is None:
            return

        venue = self.venue_dao.get_venue(venue_id)
        if venue is None:
            logger.warning(
                f"[GooglePlacesEnrichment] Cannot backfill review signal: "
                f"venue {venue_id} not found"
            )
            return

        changed = False
        if google_rating is not None and venue.rating != google_rating:
            venue.rating = google_rating
            changed = True
        if google_review_count is not None and venue.reviews != google_review_count:
            venue.reviews = google_review_count
            changed = True
        if google_price_int is not None and venue.price_level != google_price_int:
            venue.price_level = google_price_int
            changed = True

        if changed:
            self.venue_dao.upsert_venue(venue)
            logger.info(
                f"[GooglePlacesEnrichment] Backfilled review signal for {venue_id}: "
                f"rating={google_rating} reviews={google_review_count} "
                f"price_level={google_price_int}"
            )

    async def enrich_venue(
        self,
        venue_id: str,
        google_place_id: str,
        force_refresh: bool = False,
    ) -> Optional[VibeAttributes]:
        """Enrich a single venue with Google Places data.

        Fetches vibe attributes and checks business status.
        Soft-deprecates venue if permanently closed.

        Args:
            venue_id: Our internal venue ID
            google_place_id: Google Place ID for the venue
            force_refresh: If True, fetch even if cached entry exists

        Returns:
            VibeAttributes if successful, None on error or if venue was deprecated
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

            self.venue_dao.set_google_business_status(venue_id, details.business_status)

            # Check if permanently closed - soft-deprecate venue if enabled
            if details.is_permanently_closed():
                if settings.remove_permanently_closed_venues:
                    logger.warning(
                        f"[GooglePlacesEnrichment] Venue {venue_id} is PERMANENTLY CLOSED, "
                        "marking as deprecated"
                    )
                    soft_deleted = self.venue_dao.soft_delete_venue(
                        venue_id=venue_id,
                        reason="google_places_closed_permanently",
                        source="google_places",
                        google_business_status=details.business_status,
                    )
                    self._permanently_closed_in_run += 1
                    if soft_deleted:
                        VENUES_SOFT_DELETED_TOTAL.labels(
                            reason="google_places_closed_permanently",
                            source="google_places",
                        ).inc()
                        try:
                            VENUES_DEPRECATED_TOTAL.set(self.venue_dao.count_deprecated_venues())
                        except Exception:
                            pass
                    VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="soft_deleted_permanently_closed").inc()
                    return None
                else:
                    logger.warning(
                        f"[GooglePlacesEnrichment] Venue {venue_id} is PERMANENTLY CLOSED, "
                        f"but removal is disabled by config"
                    )
                    VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_permanently_closed").inc()

            # Temporarily closed venues remain active so live busyness can keep
            # refreshing and public clients can show them when data is available.
            if details.is_temporarily_closed():
                logger.info(
                    f"[GooglePlacesEnrichment] Venue {venue_id} is temporarily closed; "
                    "keeping active for live busyness"
                )
                self._temporarily_closed_in_run += 1

            # Convert to our vibe attributes model
            vibe_attrs = self.google_places_client.details_to_vibe_attributes(venue_id, details)
            vibe_attrs.google_place_id = google_place_id
            vibe_attrs.google_primary_type = details.primary_type

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

            # Store reviews if available
            if details.reviews:
                venue_reviews = VenueReviews(
                    venue_id=venue_id,
                    reviews=[VenueReview(**r) for r in details.reviews],
                )
                self.venue_dao.set_venue_reviews(venue_reviews)
                logger.debug(
                    f"[GooglePlacesEnrichment] Stored {len(venue_reviews.reviews)} reviews for {venue_id}"
                )

            # Backfill Venue.rating / Venue.reviews / Venue.price_level from
            # Google. The inventory-sync ingestion path (added in #18) creates
            # venues with these fields null; without this step they stay null
            # forever and the mobile card has no stars or price indicator
            # even though Google has the data.
            self._backfill_venue_review_signal(venue_id, details)

            # Extract Instagram handle from website URL if it's an Instagram link
            # This provides a free, high-confidence source before Apify fallback
            await self._try_extract_instagram_from_website(venue_id, details.website_uri)

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

        Also checks business status from Google Places API, soft-deprecates
        permanently closed venues, and leaves temporarily closed venues active.

        Args:
            force_refresh: If True, re-check all venues even if already enriched.
                          Use this to detect venues that have become permanently closed
                          since the last enrichment run.

        Returns:
            Number of venues successfully enriched
        """
        # Get active venue IDs. Deprecated venues are retained only for admin
        # troubleshooting and must not be reprocessed by enrichment.
        all_venue_ids = self.venue_dao.list_active_venue_ids()
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
                # Cache empty attributes so we don't retry this venue every run
                self.venue_dao.set_vibe_attributes(VibeAttributes(
                    venue_id=venue_id,
                    google_place_id="",
                ))
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

    async def _try_extract_instagram_from_website(
        self, venue_id: str, website_uri: Optional[str]
    ) -> None:
        """Extract Instagram handle from a venue's website URL if it's an Instagram link.

        Many small venues set their Instagram page as their website in Google.
        This gives us the handle for free (no Apify cost, high confidence).

        Args:
            venue_id: Our internal venue ID
            website_uri: Website URL from Google Places API
        """
        if not website_uri:
            return

        # Already have Instagram cached for this venue? Skip.
        existing = self.venue_dao.get_venue_instagram(venue_id)
        if existing is not None:
            return

        handle = self._parse_instagram_handle(website_uri)
        if not handle:
            return

        # Validate the profile exists before caching
        if not await self._instagram_profile_exists(handle):
            logger.warning(
                f"[GooglePlacesEnrichment] Instagram @{handle} does not exist, "
                f"skipping for {venue_id}"
            )
            INSTAGRAM_ENRICHMENT_RESULTS.labels(result="invalid_handle").inc()
            return

        ig_data = VenueInstagram(
            venue_id=venue_id,
            instagram_handle=handle,
            instagram_url=f"https://instagram.com/{handle}",
            confidence_score=1.0,
            status="found",
        )
        self.venue_dao.set_venue_instagram(
            ig_data,
            cache_ttl_days=settings.instagram_cache_ttl_days,
            not_found_ttl_days=settings.instagram_not_found_cache_ttl_days,
        )
        INSTAGRAM_ENRICHMENT_RESULTS.labels(result="found_via_google_places").inc()
        logger.info(
            f"[GooglePlacesEnrichment] Extracted Instagram @{handle} "
            f"from website for {venue_id}"
        )

    @staticmethod
    async def _instagram_profile_exists(handle: str) -> bool:
        """Check if an Instagram profile exists by requesting the page.

        Returns True if the profile page returns 200, False for 404 or errors.
        """
        import httpx
        url = f"https://www.instagram.com/{handle}/"
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as client:
                resp = await client.head(url)
                exists = resp.status_code == 200
                if not exists:
                    logger.debug(
                        f"[GooglePlacesEnrichment] Instagram @{handle} "
                        f"returned status {resp.status_code}"
                    )
                return exists
        except Exception as e:
            logger.debug(f"[GooglePlacesEnrichment] Instagram check failed for @{handle}: {e}")
            return True  # On error, assume exists (don't block enrichment)

    async def validate_cached_instagram_handles(self) -> int:
        """Check all cached Instagram handles and remove invalid ones.

        Returns number of handles removed.
        """
        all_venue_ids = self.venue_dao.list_active_venue_ids()
        removed = 0

        for venue_id in all_venue_ids:
            ig_data = self.venue_dao.get_venue_instagram(venue_id)
            if ig_data is None or not ig_data.has_instagram():
                continue

            handle = ig_data.instagram_handle
            if not await self._instagram_profile_exists(handle):
                self.venue_dao.delete_venue_instagram(venue_id)
                removed += 1
                logger.info(
                    f"[GooglePlacesEnrichment] Removed invalid Instagram @{handle} "
                    f"for {venue_id}"
                )
            await asyncio.sleep(1)  # Rate limit

        logger.info(f"[GooglePlacesEnrichment] Instagram validation: removed {removed} invalid handles")
        return removed

    @staticmethod
    def _parse_instagram_handle(url: str) -> Optional[str]:
        """Extract Instagram username from a URL.

        Handles formats like:
        - https://www.instagram.com/barconchittas/
        - https://instagram.com/barconchittas
        - http://instagram.com/barconchittas?hl=pt

        Returns:
            Username string or None if not an Instagram URL
        """
        match = re.match(
            r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)",
            url.strip(),
        )
        if match:
            handle = match.group(1)
            # Ignore non-profile paths
            if handle.lower() in ("p", "explore", "reel", "stories", "accounts", "about"):
                return None
            return handle
        return None
