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

from app.api.google_places_client import GooglePlacesAPIClient, GooglePlacesSearchError
from app.config import settings
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models.vibe_attributes import VibeAttributes
from app.services.price_signal import (
    derive_price_signal,
    price_level_from_enum,
)
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

# Google's priceLevel enum -> 1..4 tier mapping now lives in
# app/services/price_signal.py (the single derivation source). Re-exported here as
# `_price_level_to_int` for backward-compatible importers/tests.
_price_level_to_int = price_level_from_enum

# Keywords that indicate LGBTQ+ friendliness in a venue summary.
_LGBTQ_KEYWORDS = [
    "lgbtq",
    "lgbt",
    "gay",
    "lesbian",
    "queer",
    "pride",
    "drag",
    "inclusive",
    "welcoming to all",
    "diverse crowd",
    "rainbow",
]


def contains_lgbtq_keywords(summary: Optional[str]) -> bool:
    """Return True when the summary text contains an LGBTQ+ friendliness keyword.

    A local keyword scan over already-fetched summary text — no awaits, no I/O
    (it is business logic, not an API-client concern, so it lives here in
    app/services/). Renamed from the misleading async ``search_for_lgbtq_indicators``.
    """
    if not summary:
        return False
    summary_lower = summary.lower()
    return any(keyword in summary_lower for keyword in _LGBTQ_KEYWORDS)


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

    def _backfill_rating_reviews_and_price(
        self, venue_id: str, details, google_only_price: bool = False
    ) -> None:
        """Write Google's rating/userRatingCount and the derived price signal onto
        the Venue.

        The Venue card UI reads `rating`, `reviews`, and `price_level`. The BestTime
        venue_filter discovery path populates them at ingestion; the inventory-sync
        path (added in #18) does not. Without this backfill, inventory-synced venues
        (Praça Laura Nigro, Jockey Club, …) stay null even though Google has the data.

        Price tier is derived via the shared helper (enum > range > besttime > null,
        never 0). Google's `priceLevel` enum is PRIMARY; the objective `priceRange`
        is the FALLBACK that fills enum-less venues (e.g. Vasto). Raw signals
        (`google_price_level`, `price_range`) are persisted for audit. Preservation:
        when Google returns NO price signal, an existing real tier (1..4) is kept
        as-is — never blanked and never clobbered by BestTime; only a stale `0`/NULL
        falls through to the BestTime/NULL derivation. Reads only — no Google call.

        ``google_only_price`` (backfill path): drop the stored BestTime fallback from
        the derivation so a venue with no Google price ends NULL (never a
        BestTime-derived tier). Default False keeps cron + add price behavior
        byte-identical. Preservation of an existing real tier still applies.
        """
        google_rating = details.rating
        google_review_count = details.user_rating_count
        google_enum = details.price_level
        google_range = details.price_range
        google_price_signal = google_enum is not None or google_range is not None

        venue = self.venue_dao.get_venue(venue_id)
        if venue is None:
            logger.warning(
                f"[GooglePlacesEnrichment] Cannot backfill review signal: "
                f"venue {venue_id} not found"
            )
            return

        # BestTime tier feeds the derivation only when Google-only is off.
        besttime_fallback = None if google_only_price else venue.besttime_price_level

        changed = False
        if google_rating is not None and venue.rating != google_rating:
            venue.rating = google_rating
            changed = True
        if google_review_count is not None and venue.reviews != google_review_count:
            venue.reviews = google_review_count
            changed = True

        # ── derive the served price tier (single never-0 rule) ──
        # A stale legacy `0` is treated as "no tier" so it is never preserved/served.
        existing_tier = venue.price_level if venue.price_level not in (None, 0) else None
        if google_price_signal:
            derived = derive_price_signal(
                google_enum, google_range, besttime_fallback
            )
            new_tier, new_source = derived.price_level, derived.source
        elif existing_tier is not None:
            # Google silent this run: keep the existing real tier + its source.
            new_tier, new_source = existing_tier, venue.price_level_source
        else:
            derived = derive_price_signal(None, None, besttime_fallback)
            new_tier, new_source = derived.price_level, derived.source

        if google_enum is not None and venue.google_price_level != google_enum:
            venue.google_price_level = google_enum
            changed = True
        if google_range is not None and venue.price_range != google_range:
            venue.price_range = google_range
            changed = True
        if venue.price_level != new_tier:
            venue.price_level = new_tier
            changed = True
        if venue.price_level_source != new_source:
            venue.price_level_source = new_source
            changed = True

        if changed:
            self.venue_dao.upsert_venue(venue)
            logger.info(
                f"[GooglePlacesEnrichment] Backfilled review signal for {venue_id}: "
                f"rating={google_rating} reviews={google_review_count} "
                f"price_level={new_tier} source={new_source}"
            )

    def _apply_business_status(self, venue_id: str, details) -> bool:
        """Persist Google's business status and, on closure, run the same
        deprecation path enrich_venue's full path always applied — soft-delete
        + counters + metrics on permanent closure (when enabled), or count-only
        on temporary closure. Shared by full enrichment (enrich_venue) and the
        cheap status-only recheck (_recheck_business_status) so a closed venue
        is handled identically whichever path detected it.

        Returns True when the venue was soft-deleted this call (permanently
        closed AND removal enabled) — the caller should stop further
        per-venue work for it.
        """
        self.venue_dao.set_google_business_status(venue_id, details.business_status)

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
                return True
            logger.warning(
                f"[GooglePlacesEnrichment] Venue {venue_id} is PERMANENTLY CLOSED, "
                f"but removal is disabled by config"
            )
            return False

        # Temporarily closed venues remain active so live busyness can keep
        # refreshing and public clients can show them when data is available.
        if details.is_temporarily_closed():
            logger.info(
                f"[GooglePlacesEnrichment] Venue {venue_id} is temporarily closed; "
                "keeping active for live busyness"
            )
            self._temporarily_closed_in_run += 1

        return False

    async def _recheck_business_status(self, venue_id: str, google_place_id: str) -> str:
        """Cheap, status-only Google Details call (fields mask: businessStatus
        alone — no vibe/opening-hours/reviews refetch) for an ALREADY-enriched
        venue, so closures are detected without re-running full enrichment.
        Gated by settings.business_status_recheck_enabled (LOCKED default
        False — see app/config.py); callers must not invoke this unless that
        flag is on and google_place_id is non-empty (the no-match poison
        marker's venues carry google_place_id="" and have nothing to recheck).

        Returns:
            "recheck_closed": permanently closed and removed this call.
            "recheck_ok": still operational, or temporarily closed (kept active).
            "recheck_error": the Details call itself failed — the venue is left
                untouched, to be retried on the next run.
        """
        try:
            details = await self.google_places_client.get_place_details(
                google_place_id, fields_mask="businessStatus"
            )
        except Exception as e:
            logger.warning(
                f"[GooglePlacesEnrichment] business-status recheck failed for "
                f"{venue_id}: {type(e).__name__}: {e}"
            )
            return "recheck_error"
        if details is None:
            logger.warning(
                f"[GooglePlacesEnrichment] business-status recheck: no details "
                f"for {venue_id} ({google_place_id})"
            )
            return "recheck_error"

        removed = self._apply_business_status(venue_id, details)
        return "recheck_closed" if removed else "recheck_ok"

    async def enrich_venue(
        self,
        venue_id: str,
        google_place_id: str,
        force_refresh: bool = False,
        google_only_price: bool = False,
    ) -> Optional[VibeAttributes]:
        """Enrich a single venue with Google Places data.

        Fetches vibe attributes and checks business status.
        Soft-deprecates venue if permanently closed.

        Args:
            venue_id: Our internal venue ID
            google_place_id: Google Place ID for the venue
            force_refresh: If True, fetch even if cached entry exists
            google_only_price: If True, derive price from Google signals only (no
                BestTime fallback) — used by the pending backfill. Default False
                keeps cron + add price behavior byte-identical.

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

            # Persist status + apply closure handling (soft-delete on permanent
            # closure when enabled; count temporary closure) — shared with the
            # status-only recheck path (_recheck_business_status) so a closed
            # venue is handled identically whichever path detected it.
            removed = self._apply_business_status(venue_id, details)
            if removed:
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="soft_deleted_permanently_closed").inc()
                return None
            if details.is_permanently_closed():
                # Permanently closed but removal is disabled by config — already
                # logged inside _apply_business_status.
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_permanently_closed").inc()

            # Convert to our vibe attributes model
            vibe_attrs = self.google_places_client.details_to_vibe_attributes(venue_id, details)
            vibe_attrs.google_place_id = google_place_id
            vibe_attrs.google_primary_type = details.primary_type

            # Check for LGBTQ+ indicators in the summary
            if details.generative_summary or details.editorial_summary:
                summary = details.generative_summary or details.editorial_summary
                vibe_attrs.lgbtq_friendly = contains_lgbtq_keywords(summary)

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
            self._backfill_rating_reviews_and_price(
                venue_id, details, google_only_price=google_only_price
            )

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

    async def _search_and_enrich_servable(self, venue, google_only_price: bool) -> str:
        """Resolve a servable venue's Google place_id and enrich it, or write the
        empty no-match marker. Paces identically to both callers (REQUEST_DELAY on
        a no-match or search error, REQUEST_DELAY*2 after an enrich). Returns
        ``"no_google_match"``, ``"enriched"``, ``"error"``, or ``"search_error"``;
        the caller maps that to its own metric label + log + bookkeeping.

        Shared per-venue body of enrich_all_venues and enrich_pending_venues so
        their marker + pacing policy cannot drift. The caller has already decided
        the venue is not cache-skipped (presence check / force_refresh).

        The empty no-match marker is written ONLY on a genuine "Google answered:
        no match" (search_place_id returns None). A transport/quota failure
        (search_place_id raises GooglePlacesSearchError, opted into via
        raise_on_error=True) must never poison the venue with that marker — a
        mid-run Places outage would otherwise permanently mark every remaining
        venue in the loop as a dead end with no retry path. ``"search_error"``
        writes nothing and leaves the venue to be retried on the next run.
        """
        try:
            google_place_id = await self.google_places_client.search_place_id(
                venue_name=venue.venue_name,
                venue_address=venue.venue_address,
                lat=venue.venue_lat,
                lng=venue.venue_lng,
                raise_on_error=True,
            )
        except GooglePlacesSearchError as e:
            logger.warning(
                f"[GooglePlacesEnrichment] place search failed for {venue.venue_id} "
                f"({venue.venue_name!r}); skipping this run, will retry next run: {e}"
            )
            await asyncio.sleep(REQUEST_DELAY)
            return "search_error"

        if not google_place_id:
            # No Google match: write the empty marker so re-runs skip this venue.
            self.venue_dao.set_vibe_attributes(
                VibeAttributes(venue_id=venue.venue_id, google_place_id="")
            )
            await asyncio.sleep(REQUEST_DELAY)
            return "no_google_match"

        result = await self.enrich_venue(
            venue_id=venue.venue_id,
            google_place_id=google_place_id,
            force_refresh=True,  # the caller already checked the cache above
            google_only_price=google_only_price,
        )
        # Two Google calls per venue (search + details): pace accordingly.
        await asyncio.sleep(REQUEST_DELAY * 2)
        return "enriched" if result is not None else "error"

    async def enrich_all_venues(
        self, force_refresh: bool = False, google_only_price: bool = False
    ) -> int:
        """Enrich all known venues with Google Places data.

        This method fetches all venues from Redis and searches Google Places
        by name/address to get the Google Place ID, then fetches enrichment data.

        Also checks business status from Google Places API, soft-deprecates
        permanently closed venues, and leaves temporarily closed venues active.

        Args:
            force_refresh: If True, re-check all venues even if already enriched.
            google_only_price: If True, derive price from Google only (no BestTime
                fallback). Default False keeps the cron/admin behavior unchanged;
                the pending backfill passes True.
                          Use this to detect venues that have become permanently closed
                          since the last enrichment run.

        Returns:
            Number of venues successfully enriched
        """
        # Gate enrichment on the serving view (active AND eligible). Ineligible
        # venues are excluded so known junk never burns Google budget; unlabeled
        # venues stay in the view, so first-time enrichment still learns their type.
        all_venue_ids = self.venue_dao.list_servable_venue_ids()
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
        # 0 (default) = unbounded; a positive value caps how many already-
        # enriched venues get a status-only recheck THIS run (config, not a
        # per-call arg, so the scheduled job and any admin trigger share it).
        recheck_limit = settings.business_status_recheck_limit
        recheck_budget = recheck_limit if recheck_limit > 0 else None

        for venue_id in all_venue_ids:
            # Check if already cached (skip if not forcing refresh)
            existing = self.venue_dao.get_vibe_attributes(venue_id)
            if existing is not None and not force_refresh:
                # Status-only recheck: cheap Details call (businessStatus only)
                # for an already-enriched venue, so a closure since first
                # enrichment is detected without a full re-enrichment sweep.
                # Never attempted for the no-match poison marker
                # (google_place_id="") — there is nothing to recheck.
                recheck_eligible = (
                    settings.business_status_recheck_enabled
                    and bool(existing.google_place_id)
                    and (recheck_budget is None or recheck_budget > 0)
                )
                if recheck_eligible:
                    if recheck_budget is not None:
                        recheck_budget -= 1
                    outcome = await self._recheck_business_status(
                        venue_id, existing.google_place_id
                    )
                    if outcome == "recheck_error":
                        VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_error").inc()
                    else:
                        VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result=outcome).inc()
                else:
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

            # Search Google Places + enrich (or mark no-match) via the shared body.
            outcome = await self._search_and_enrich_servable(venue, google_only_price)
            if outcome == "no_google_match":
                logger.warning(f"[GooglePlacesEnrichment] Could not find Google Place ID for {venue.venue_name}")
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_no_place_id").inc()
            elif outcome == "enriched":
                successful += 1
            elif outcome == "search_error":
                # Transport/quota failure, not a genuine no-match: no marker was
                # written, so the next run retries this venue. Distinct label
                # (error != no-match) keeps this visible from a real zero-result.
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_error").inc()
            # "error": tracked via enrich_venue's own metrics.
            # Note: Closure tracking is done via instance counters in enrich_venue()

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

    async def enrich_pending_venues(self, limit: Optional[int] = None) -> dict:
        """One-time, idempotent, Google-only backfill of PENDING venues.

        Pending = servable (active AND eligible) with no `vibe_attributes` row yet.
        Reuses the same selection + no-match marker as ``enrich_all_venues``:
        - already-enriched venues (a vibe_attributes row exists) are skipped
          (presence-based), so this never reprocesses them;
        - a venue with no Google match gets an empty ``VibeAttributes`` marker
          (google_place_id="") so a re-run skips it — no BestTime column needed;
        - price is Google-only (no BestTime fallback).

        Bounded by ``limit`` (None = all pending). Makes NO BestTime call. Returns a
        summary: seen/enriched/skipped_cached/no_google_match/error.
        """
        summary = {
            "seen": 0, "enriched": 0, "skipped_cached": 0,
            "no_google_match": 0, "error": 0,
        }
        servable_ids = self.venue_dao.list_servable_venue_ids()
        logger.info(
            f"[GooglePlacesEnrichment] Backfill: scanning {len(servable_ids)} "
            f"servable venues for pending (limit={limit})"
        )

        for venue_id in servable_ids:
            if limit is not None and summary["enriched"] + summary["no_google_match"] >= limit:
                break

            # Presence-based skip = the idempotency marker (enriched OR no-match).
            existing = self.venue_dao.get_vibe_attributes(venue_id)
            if existing is not None:
                summary["skipped_cached"] += 1
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_cached").inc()
                continue

            venue = self.venue_dao.get_venue(venue_id)
            if venue is None:
                summary["error"] += 1
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_no_venue").inc()
                continue

            summary["seen"] += 1
            # Search Google Places + enrich (or mark no-match) via the shared body
            # (google-only price: no BestTime fallback for the backfill).
            outcome = await self._search_and_enrich_servable(venue, google_only_price=True)
            if outcome == "no_google_match":
                logger.info(
                    f"[GooglePlacesEnrichment] Backfill: no Google match for "
                    f"{venue.venue_name} ({venue_id}); marking attempted"
                )
                summary["no_google_match"] += 1
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="no_google_match").inc()
            elif outcome == "enriched":
                summary["enriched"] += 1
            elif outcome == "search_error":
                # Transport/quota failure, not a genuine no-match: no marker
                # written, so a later run retries this venue.
                summary["error"] += 1
                VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_error").inc()
            else:  # "error"
                summary["error"] += 1

        count = self.venue_dao.count_venues_with_vibe_attributes()
        VENUES_WITH_VIBE_ATTRIBUTES.set(count)
        logger.info(f"[GooglePlacesEnrichment] Backfill complete: {summary}")
        return summary

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
    async def _check_instagram_status(handle: str) -> str:
        """Check an Instagram profile's existence, returning a tri-state result
        so callers can distinguish a definitive absence from an ambiguous
        check:

        - ``"found"``: the profile page returned 200.
        - ``"not_found"``: the profile page returned 404 (Instagram's
          definitive "no such user").
        - ``"unknown"``: any other status (429 rate-limit, 403, a redirect to
          a login wall, etc.) or a network/timeout error. An ambiguous
          outcome must never be treated as confirmed absence — a mid-sweep
          rate-limit must not read as "doesn't exist".
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
                if resp.status_code == 200:
                    return "found"
                if resp.status_code == 404:
                    return "not_found"
                logger.debug(
                    f"[GooglePlacesEnrichment] Instagram @{handle} returned status "
                    f"{resp.status_code} (ambiguous, treating as unknown)"
                )
                return "unknown"
        except Exception as e:
            logger.debug(f"[GooglePlacesEnrichment] Instagram check failed for @{handle}: {e}")
            return "unknown"

    @classmethod
    async def _instagram_profile_exists(cls, handle: str) -> bool:
        """Boolean gate for the website-extraction path (a newly-discovered
        handle is only cached when we are not confident it is absent) —
        "unknown" outcomes count as existing here (fail open toward caching;
        the low-stakes side, since the validation sweep below never mass-
        deletes on an unknown outcome either)."""
        return await cls._check_instagram_status(handle) != "not_found"

    async def validate_cached_instagram_handles(self) -> int:
        """Check all cached Instagram handles and remove only those confirmed
        definitively absent (a 404).

        A 429/403/redirect/timeout/other ambiguous outcome KEEPS the handle —
        a mid-sweep rate-limit must never mass-delete valid handles (each
        re-discovery costs a paid Apify run).

        Returns number of handles removed.
        """
        all_venue_ids = self.venue_dao.list_active_venue_ids()
        removed = 0

        for venue_id in all_venue_ids:
            ig_data = self.venue_dao.get_venue_instagram(venue_id)
            if ig_data is None or not ig_data.has_instagram():
                continue

            handle = ig_data.instagram_handle
            status = await self._check_instagram_status(handle)
            if status == "not_found":
                self.venue_dao.delete_venue_instagram(venue_id)
                removed += 1
                logger.info(
                    f"[GooglePlacesEnrichment] Removed invalid Instagram @{handle} "
                    f"for {venue_id} (definitive 404)"
                )
            elif status == "unknown":
                logger.info(
                    f"[GooglePlacesEnrichment] Instagram @{handle} for {venue_id} "
                    "check inconclusive; keeping handle"
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
