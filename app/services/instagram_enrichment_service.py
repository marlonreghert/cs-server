"""Service for discovering and caching Instagram handles for venues.

Two-phase approach:
1. Google Places (FREE): During Google Places enrichment, Instagram handles are
   extracted from venue websiteUri (many small venues set their IG as website).
   These are cached with confidence=1.0.
2. Apify search (PAID): For venues without a handle from Google Places,
   search Instagram by venue name and validate the best match.

When Apify limit/credits are exhausted, the loop continues — remaining venues
still benefit from Google Places cache hits.

Follows the same pattern as GooglePlacesEnrichmentService.
"""
import asyncio
import logging
from typing import Optional

from app.api.apify_instagram_client import ApifyInstagramClient, ApifyCreditExhaustedError
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models.instagram import VenueInstagram
from app.services.instagram_validator import InstagramValidator
from app.metrics import (
    INSTAGRAM_ENRICHMENT_RESULTS,
    INSTAGRAM_VENUES_WITH_HANDLE,
    INSTAGRAM_VALIDATION_SCORES,
    INSTAGRAM_APIFY_COST_ESTIMATE,
)

logger = logging.getLogger(__name__)

# Rate limiting for Apify calls
REQUESTS_PER_SECOND = 2
REQUEST_DELAY = 1.0 / REQUESTS_PER_SECOND

# Search query template
SEARCH_QUERY_TEMPLATE = "{venue_name} {city}"

DEFAULT_CITY = "Recife"

# Known cities in the Recife metro area
KNOWN_CITIES = ["recife", "olinda", "jaboatao", "paulista", "camaragibe"]


class InstagramEnrichmentService:
    """Discovers and caches Instagram handles for venues."""

    def __init__(
        self,
        apify_client: ApifyInstagramClient,
        venue_dao: RedisVenueDAO,
        validator: Optional[InstagramValidator] = None,
        search_candidates: int = 3,
        enrichment_limit: int = 0,
        cache_ttl_days: int = 30,
        not_found_ttl_days: int = 7,
    ):
        self.apify_client = apify_client
        self.venue_dao = venue_dao
        self.validator = validator or InstagramValidator()
        self.search_candidates = search_candidates
        self.enrichment_limit = enrichment_limit  # 0 = unlimited
        self.cache_ttl_days = cache_ttl_days
        self.not_found_ttl_days = not_found_ttl_days

    async def discover_instagram_for_venue(
        self, venue_id: str, force_refresh: bool = False
    ) -> Optional[VenueInstagram]:
        """Discover Instagram handle for a single venue via Apify search.

        Args:
            venue_id: Internal venue ID
            force_refresh: If True, re-discover even if cached

        Returns:
            VenueInstagram result
        """
        # Check cache first (includes handles found via Google Places)
        if not force_refresh:
            existing = self.venue_dao.get_venue_instagram(venue_id)
            if existing is not None:
                logger.debug(f"[InstagramEnrichment] Cache hit for {venue_id}")
                INSTAGRAM_ENRICHMENT_RESULTS.labels(result="cache_hit").inc()
                return existing

        # Get venue data
        venue = self.venue_dao.get_venue(venue_id)
        if venue is None:
            logger.warning(f"[InstagramEnrichment] Venue not found: {venue_id}")
            INSTAGRAM_ENRICHMENT_RESULTS.labels(result="venue_not_found").inc()
            return None

        # Build search query
        city = self._extract_city(venue.venue_address)
        query = SEARCH_QUERY_TEMPLATE.format(
            venue_name=venue.venue_name, city=city
        )
        logger.info(f"[InstagramEnrichment] Searching IG for: {query}")

        # Search returns full InstagramProfile objects (no separate profile call needed)
        candidates = await self.apify_client.search_users(
            query=query, results_limit=self.search_candidates
        )

        if not candidates:
            logger.info(
                f"[InstagramEnrichment] No IG candidates for {venue.venue_name}"
            )
            result = VenueInstagram(
                venue_id=venue_id,
                status="not_found",
                confidence_score=0.0,
            )
            self.venue_dao.set_venue_instagram(
                result,
                cache_ttl_days=self.cache_ttl_days,
                not_found_ttl_days=self.not_found_ttl_days,
            )
            INSTAGRAM_ENRICHMENT_RESULTS.labels(result="not_found").inc()
            return result

        # Validate each candidate (search already provides full profile data)
        best_result: Optional[VenueInstagram] = None
        best_score = 0.0

        for profile in candidates:
            validation = self.validator.validate(venue, profile)

            logger.info(
                f"[InstagramEnrichment] Validated @{profile.username} for "
                f"{venue.venue_name}: score={validation.confidence_score:.3f} "
                f"signals={validation.signals}"
            )

            INSTAGRAM_VALIDATION_SCORES.observe(validation.confidence_score)

            if validation.confidence_score > best_score:
                best_score = validation.confidence_score

                if validation.confidence_score >= self.validator.auto_accept_threshold:
                    status = "found"
                elif validation.confidence_score >= self.validator.low_confidence_threshold:
                    status = "low_confidence"
                else:
                    status = "not_found"

                best_result = VenueInstagram(
                    venue_id=venue_id,
                    instagram_handle=profile.username,
                    instagram_url=f"https://instagram.com/{profile.username}",
                    confidence_score=validation.confidence_score,
                    status=status,
                    bio=profile.biography,
                    followers_count=profile.followers_count,
                    is_business_account=profile.is_business_account,
                    business_category=profile.business_category_name,
                )

            # Early exit if we find a high-confidence match
            if best_score >= self.validator.auto_accept_threshold:
                break

        # If best candidate is below minimum threshold, mark as not found
        if best_result is None or best_result.status == "not_found":
            result = VenueInstagram(
                venue_id=venue_id,
                status="not_found",
                confidence_score=best_score,
            )
            self.venue_dao.set_venue_instagram(
                result,
                cache_ttl_days=self.cache_ttl_days,
                not_found_ttl_days=self.not_found_ttl_days,
            )
            INSTAGRAM_ENRICHMENT_RESULTS.labels(result="not_found").inc()
            return result

        # Cache the best result
        self.venue_dao.set_venue_instagram(
            best_result,
            cache_ttl_days=self.cache_ttl_days,
            not_found_ttl_days=self.not_found_ttl_days,
        )
        INSTAGRAM_ENRICHMENT_RESULTS.labels(result=best_result.status).inc()

        logger.info(
            f"[InstagramEnrichment] Found @{best_result.instagram_handle} for "
            f"{venue.venue_name} (score={best_result.confidence_score:.3f}, "
            f"status={best_result.status})"
        )

        return best_result

    async def enrich_all_venues(self, force_refresh: bool = False) -> int:
        """Discover Instagram handles for all venues.

        Google Places enrichment runs first and extracts handles from websiteUri
        for free. This method handles the Apify fallback for remaining venues.

        When the Apify limit/credits are exhausted, the loop continues so that
        remaining venues still get cache checks (which catch Google Places results).

        Args:
            force_refresh: If True, re-check all venues even if cached

        Returns:
            Number of venues with Instagram handles found
        """
        all_venue_ids = self.venue_dao.list_all_venue_ids()
        logger.info(
            f"[InstagramEnrichment] Starting enrichment for "
            f"{len(all_venue_ids)} venues (force_refresh={force_refresh})"
        )

        if not all_venue_ids:
            logger.warning("[InstagramEnrichment] No venues found")
            return 0

        found_count = 0
        apify_processed = 0
        skipped = 0
        errors = 0
        apify_exhausted = False

        for venue_id in all_venue_ids:
            # Check if already cached (catches Google Places results + previous runs)
            if not force_refresh:
                existing = self.venue_dao.get_venue_instagram(venue_id)
                if existing is not None:
                    if existing.has_instagram():
                        found_count += 1
                    skipped += 1
                    continue

            # If Apify budget is exhausted, skip remaining uncached venues
            if apify_exhausted:
                continue

            # Enforce Apify enrichment limit (0 = unlimited)
            if self.enrichment_limit > 0 and apify_processed >= self.enrichment_limit:
                logger.info(
                    f"[InstagramEnrichment] Reached Apify enrichment limit "
                    f"({self.enrichment_limit}). Skipping remaining venues."
                )
                apify_exhausted = True
                continue

            try:
                result = await self.discover_instagram_for_venue(
                    venue_id, force_refresh=True
                )
                if result and result.has_instagram():
                    found_count += 1
                apify_processed += 1
            except ApifyCreditExhaustedError:
                logger.error(
                    "[InstagramEnrichment] Apify credits exhausted! "
                    "Skipping remaining Apify calls."
                )
                apify_exhausted = True
                continue
            except Exception as e:
                logger.error(
                    f"[InstagramEnrichment] Error processing {venue_id}: {e}"
                )
                INSTAGRAM_ENRICHMENT_RESULTS.labels(result="error").inc()
                errors += 1

            # Rate limiting between venues
            await asyncio.sleep(REQUEST_DELAY * 3)

        # Update metrics
        total_with_ig = self.venue_dao.count_venues_with_instagram()
        INSTAGRAM_VENUES_WITH_HANDLE.set(total_with_ig)

        # Estimate cost: ~$0.006 per search call (search only, no profile call)
        estimated_cost = apify_processed * 0.006
        INSTAGRAM_APIFY_COST_ESTIMATE.inc(estimated_cost)

        logger.info(
            f"[InstagramEnrichment] Enrichment complete: "
            f"{found_count} found, {apify_processed} Apify calls, "
            f"{skipped} skipped (cached), {errors} errors. "
            f"Estimated Apify cost: ${estimated_cost:.2f}"
        )

        return found_count

    @staticmethod
    def _extract_city(address: str) -> str:
        """Extract city name from venue address."""
        address_lower = address.lower()
        for city in KNOWN_CITIES:
            if city in address_lower:
                return city.title()
        return DEFAULT_CITY
