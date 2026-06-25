"""Venues refresher service with background job orchestration."""
import json
import logging
from dataclasses import dataclass
from collections import defaultdict

from app.api import BestTimeAPIClient
from app.dao import RedisVenueDAO
from app.models import (
    Venue,
    FootTrafficForecast,
    VenueFilterParams,
    VenueFilterVenue,
)
from app.services.price_signal import GOOGLE_SOURCES, derive_price_signal
from app.metrics import (
    VENUES_TOTAL,
    VENUES_WITH_ATTRIBUTE,
    VENUES_BY_TYPE,
    VENUES_WITH_LIVE_FORECAST,
    VENUES_WITH_WEEKLY_FORECAST,
    VENUES_LIVE_FORECAST_AVAILABILITY_RATIO,
    REFRESH_VENUES_DISCOVERED,
    REFRESH_VENUES_UPSERTED,
    REFRESH_DUPLICATES_SKIPPED,
    LIVE_FORECAST_FETCH_RESULTS,
    WEEKLY_FORECAST_FETCH_RESULTS,
    VENUES_AVERAGE_RATING,
    VENUES_AVERAGE_REVIEWS,
    VENUES_BY_PRICE_LEVEL,
    VENUES_BY_PRICE_LEVEL_SOURCE,
    INVENTORY_SYNC_VENUES_TOTAL,
    INVENTORY_SYNC_RUNS_TOTAL,
    DISCOVERY_SKIPPED_DUE_TO_MONTHLY_CAP_TOTAL,
    VENUE_MONTHLY_NEW_COUNT,
    VENUES_ACTIVE_TOTAL,
    VENUES_DEPRECATED_TOTAL,
    VENUES_DEPRECATED_BY_REASON,
    REFRESH_SELECTED_TOTAL,
    BESTTIME_READ_SKIPPED_TOTAL,
    BESTTIME_UNIQUE_VENUES_TOUCHED,
)

logger = logging.getLogger(__name__)


@dataclass
class Location:
    """Location configuration for venue discovery."""
    lat: float
    lng: float
    radius: int  # Meters
    limit: int   # Max venues to fetch


# Default locations for venue discovery (radius in meters)
DEFAULT_LOCATIONS = [
    Location(lat=-8.07834, lng=-34.90938, radius=15000, limit=500),  # ZS/ZN - C1
    Location(lat=-7.99081, lng=-34.85141, radius=15000, limit=500),  # Olinda
    Location(lat=-8.18160, lng=-34.92980, radius=15000, limit=500),  # Jaboatao/Candeias
]

# Venue types for BestTime /venues/filter API
# We fetch broadly (including OTHER which contains ~60% of BestTime venues)
# and rely on BLOCKED_VENUE_TYPES to filter out junk at query time.
VENUE_TYPES = [
    # Nightlife & entertainment
    "BAR",
    "BREWERY",
    "CLUBS",
    "CONCERT_HALL",
    "EVENT_VENUE",
    "PERFORMING_ARTS",
    "ARTS",
    "WINERY",
    "CASINO",
    "FOOD_AND_DRINK",
    "BEER",
    "BISTRO",
    # Catch-all — many bars/restaurants are misclassified as OTHER by BestTime
    "OTHER",
]

# Eligibility block-lists now live in app/services/venue_eligibility.py, which
# owns the single decision used by serving, sync, discovery, and the sweep.
# Re-exported here for backward compatibility with existing importers.
from app.services.venue_eligibility import (  # noqa: E402,F401
    DEFAULT_BLOCKED_VENUE_TYPES,  # re-exported for backward-compat importers
    DEFAULT_BLOCKED_GOOGLE_TYPES as BLOCKED_GOOGLE_TYPES,
    BLOCKED_NAME_KEYWORDS,
    ALL_REASONS,
)


class VenuesRefresherService:
    """Service for refreshing venue data from BestTime API."""

    ADMIN_CONFIG_DISCOVERY_POINTS_KEY = "admin_config:discovery_points"

    def __init__(
        self,
        venue_dao: RedisVenueDAO,
        besttime_api: BestTimeAPIClient,
        redis_client=None,
        fetch_venue_limit_override: int = 0,
        fetch_venue_total_limit: int = -1,
        dev_mode: bool = False,
        dev_lat: float = 0.0,
        dev_lng: float = 0.0,
        dev_radius: int = 6000,
    ):
        """Initialize refresher service.

        Args:
            venue_dao: Redis DAO for venue persistence
            besttime_api: BestTime API client
            redis_client: Raw Redis client for reading admin config
            fetch_venue_limit_override: If > 0, overrides the limit for each location when fetching from BestTime API
            fetch_venue_total_limit: Global cap on total venues fetched across all locations (-1 = disabled, 0 = fetch none)
            dev_mode: If True, use single dev location instead of DEFAULT_LOCATIONS
            dev_lat: Dev mode latitude
            dev_lng: Dev mode longitude
            dev_radius: Dev mode radius in meters
        """
        self.venue_dao = venue_dao
        self.besttime_api = besttime_api
        self.redis_client = redis_client
        self.fetch_venue_limit_override = fetch_venue_limit_override
        self.fetch_venue_total_limit = fetch_venue_total_limit
        self.dev_mode = dev_mode
        self.dev_lat = dev_lat
        self.dev_lng = dev_lng
        self.dev_radius = dev_radius
        # Optional: set later via set_budget_service so the container can wire
        # this up after construction (avoids a circular import).
        self.budget_service = None

    def set_budget_service(self, budget_service) -> None:
        """Wire the VenueBudgetService used to enforce the monthly cap."""
        self.budget_service = budget_service

    # ── priority-bounded refresh selection + monthly ledger gate ─────────────
    def _select_refresh_venue_ids(self, job: str) -> list[str]:
        """The top-X served venues by priority for bounded refresh — the
        eligibility serving view (serving.eligible_venue), not all active venues,
        so the scarce budget targets venues users actually see. Live and weekly
        both call this so they touch the identical unique-venue set, where
        X = monthly_quota − manual_reserve. A serving-view read failure propagates
        and aborts the cycle (fail-safe) — it never falls back to an active-scoped
        refresh. Falls back to the full servable set only when no budget service
        is wired (keeps standalone use working)."""
        if self.budget_service is not None:
            limit = self.budget_service.get_refresh_budget()
            ids = self.venue_dao.list_servable_venue_ids_by_priority(limit)
            logger.info(
                f"[VenuesRefresherService] {job}: selected {len(ids)} venues "
                f"servable by priority (refresh_budget={limit})"
            )
        else:
            ids = self.venue_dao.list_servable_venue_ids()
            logger.warning(
                f"[VenuesRefresherService] {job}: no budget service wired; "
                f"refreshing all {len(ids)} servable venues (unbounded)"
            )
        REFRESH_SELECTED_TOTAL.labels(job=job).inc(len(ids))
        return ids

    def _ledger_allows_read(self, venue_id: str, job: str) -> bool:
        """Hard ceiling on BestTime reads. Refuses a not-yet-touched venue once
        the calendar month hits monthly_quota distinct venues; already-touched
        venues pass freely. Fails open on a ledger error so a Redis blip never
        silently halts refresh (BestTime's own cap rejection is the backstop)."""
        if self.budget_service is None:
            return True
        try:
            allowed = self.budget_service.try_register_touch(venue_id)
        except Exception as e:
            logger.error(
                f"[VenuesRefresherService] {job}: ledger gate error for "
                f"{venue_id}: {e}; proceeding (fail-open)"
            )
            return True
        if not allowed:
            BESTTIME_READ_SKIPPED_TOTAL.labels(reason="monthly_cap").inc()
            logger.warning(
                f"[VenuesRefresherService] {job}: monthly unique-venue cap "
                f"reached; skipping BestTime read for {venue_id}"
            )
        return allowed

    def _update_touched_gauge(self) -> None:
        if self.budget_service is None:
            return
        try:
            ym = self.budget_service.current_year_month()
            BESTTIME_UNIQUE_VENUES_TOUCHED.labels(year_month=ym).set(
                self.budget_service.unique_touched_count()
            )
        except Exception as e:
            logger.warning(
                f"[VenuesRefresherService] failed to update touched gauge: {e}"
            )

    # Eligibility is no longer applied destructively here. It is a non-destructive
    # serving view (serving.eligible_venue) that the projector reconciles Redis to;
    # block-list edits change serving in both directions with no lifecycle change.
    # The retired eligibility sweep + born-deprecate path lived here.
    def update_data_quality_metrics(self) -> None:
        """Compute and update all data quality metrics from cached venues.

        This method reads all venues from the cache and updates Prometheus
        gauges with counts and statistics about data quality.
        """
        try:
            all_venues = self.venue_dao.list_all_venues()
        except Exception as e:
            logger.error(f"[VenuesRefresherService] Failed to list venues for metrics: {e}")
            return

        venues = [venue for venue in all_venues if venue.is_active()]
        deprecated_count = len(all_venues) - len(venues)
        total = len(venues)
        VENUES_TOTAL.set(total)
        VENUES_ACTIVE_TOTAL.set(total)
        VENUES_DEPRECATED_TOTAL.set(deprecated_count)

        # Breakdown of *why* venues were vetoed (for Grafana / admin inspection).
        reason_counts: dict[str, int] = defaultdict(int)
        for venue in all_venues:
            if venue.is_deprecated():
                reason_counts[venue.deprecated_reason or "unknown"] += 1
        known_reasons = (
            set(ALL_REASONS)
            | {"google_places_closed_permanently"}
            | set(reason_counts)
        )
        for reason in known_reasons:
            VENUES_DEPRECATED_BY_REASON.labels(reason=reason).set(
                reason_counts.get(reason, 0)
            )

        if total == 0:
            # Reset all gauges to 0 when no venues
            for attr in ["address", "lat_lng", "rating", "reviews", "price_level", "type", "dwell_time", "forecast"]:
                VENUES_WITH_ATTRIBUTE.labels(attribute=attr).set(0)
            VENUES_WITH_LIVE_FORECAST.set(0)
            VENUES_WITH_WEEKLY_FORECAST.set(0)
            VENUES_LIVE_FORECAST_AVAILABILITY_RATIO.set(0)
            VENUES_AVERAGE_RATING.set(0)
            VENUES_AVERAGE_REVIEWS.set(0)
            return

        # Count venues with various attributes
        with_address = 0
        with_lat_lng = 0
        with_rating = 0
        with_reviews = 0
        with_price_level = 0
        with_type = 0
        with_dwell_time = 0
        with_forecast = 0

        # For aggregations
        ratings = []
        reviews_list = []
        type_counts = defaultdict(int)
        price_level_counts = defaultdict(int)
        price_source_counts = defaultdict(int)

        for venue in venues:
            # Address
            if venue.venue_address and venue.venue_address.strip():
                with_address += 1

            # Lat/Lng (always present but check for valid values)
            if venue.venue_lat != 0 and venue.venue_lng != 0:
                with_lat_lng += 1

            # Rating
            if venue.rating is not None and venue.rating > 0:
                with_rating += 1
                ratings.append(venue.rating)

            # Reviews
            if venue.reviews is not None and venue.reviews > 0:
                with_reviews += 1
                reviews_list.append(venue.reviews)

            # Price level
            if venue.price_level is not None:
                with_price_level += 1
                price_level_counts[str(venue.price_level)] += 1
            else:
                price_level_counts["unknown"] += 1

            # Which rule produced the served tier (audit-only field).
            price_source_counts[venue.price_level_source or "none"] += 1

            # Venue type
            if venue.venue_type:
                with_type += 1
                type_counts[venue.venue_type] += 1

            # Dwell time
            if venue.venue_dwell_time_min is not None or venue.venue_dwell_time_max is not None:
                with_dwell_time += 1

            # Forecast data
            if venue.venue_foot_traffic_forecast:
                with_forecast += 1

        # Update attribute presence gauges
        VENUES_WITH_ATTRIBUTE.labels(attribute="address").set(with_address)
        VENUES_WITH_ATTRIBUTE.labels(attribute="lat_lng").set(with_lat_lng)
        VENUES_WITH_ATTRIBUTE.labels(attribute="rating").set(with_rating)
        VENUES_WITH_ATTRIBUTE.labels(attribute="reviews").set(with_reviews)
        VENUES_WITH_ATTRIBUTE.labels(attribute="price_level").set(with_price_level)
        VENUES_WITH_ATTRIBUTE.labels(attribute="type").set(with_type)
        VENUES_WITH_ATTRIBUTE.labels(attribute="dwell_time").set(with_dwell_time)
        VENUES_WITH_ATTRIBUTE.labels(attribute="forecast").set(with_forecast)

        # Update type breakdown
        for venue_type, count in type_counts.items():
            VENUES_BY_TYPE.labels(venue_type=venue_type).set(count)

        # Update price level breakdown
        for price_level, count in price_level_counts.items():
            VENUES_BY_PRICE_LEVEL.labels(price_level=price_level).set(count)

        # Tier-source breakdown (enum / range fallback / besttime / none).
        for source in ("google_enum", "google_range", "besttime", "none"):
            VENUES_BY_PRICE_LEVEL_SOURCE.labels(source=source).set(
                price_source_counts.get(source, 0)
            )
        logger.info(
            f"[VenuesRefresherService] Price tier sources: "
            f"{dict(price_source_counts)}"
        )

        # Update averages
        if ratings:
            VENUES_AVERAGE_RATING.set(sum(ratings) / len(ratings))
        if reviews_list:
            VENUES_AVERAGE_REVIEWS.set(sum(reviews_list) / len(reviews_list))

        # Count live and weekly forecasts
        live_count = 0
        weekly_count = 0

        for venue in venues:
            venue_id = venue.venue_id
            if not venue_id:
                continue

            # Check live forecast
            try:
                live = self.venue_dao.get_live_forecast(venue_id)
                if live is not None:
                    live_count += 1
            except Exception:
                pass

            # Check weekly forecast (check if at least one day exists)
            try:
                weekly = self.venue_dao.get_week_raw_forecast(venue_id, 0)  # Check Monday
                if weekly is not None:
                    weekly_count += 1
            except Exception:
                pass

        VENUES_WITH_LIVE_FORECAST.set(live_count)
        VENUES_WITH_WEEKLY_FORECAST.set(weekly_count)

        # Calculate availability ratio
        if total > 0:
            VENUES_LIVE_FORECAST_AVAILABILITY_RATIO.set(live_count / total)
        else:
            VENUES_LIVE_FORECAST_AVAILABILITY_RATIO.set(0)

        logger.info(
            f"[VenuesRefresherService] Updated data quality metrics: "
            f"total={total}, with_address={with_address}, with_rating={with_rating}, "
            f"with_live={live_count}, with_weekly={weekly_count}, "
            f"deprecated={deprecated_count}"
        )

    def _map_venue_filter_venue_to_venue(self, vf: VenueFilterVenue) -> Venue:
        """Convert VenueFilterVenue to Venue model.

        Preserves exact mapping from Go implementation (lines 432-469).

        Args:
            vf: VenueFilterVenue from API response

        Returns:
            Venue object ready for persistence
        """
        # Build one-day FootTrafficForecast from filter result
        foot_traffic = [
            FootTrafficForecast(
                day_info=vf.day_info,
                day_int=vf.day_int,
                day_raw=vf.day_raw,
            )
        ]

        venue = Venue(
            forecast=True,
            processed=True,
            venue_address=vf.venue_address,
            venue_lat=vf.venue_lat,
            venue_lng=vf.venue_lng,
            venue_name=vf.venue_name,
            venue_id=vf.venue_id,
            venue_type=vf.venue_type,
            venue_dwell_time_min=vf.venue_dwell_time_min,
            venue_dwell_time_max=vf.venue_dwell_time_max,
            rating=vf.rating,
            reviews=vf.reviews,
            # BestTime's raw price is kept in its own column and fed to the shared
            # derivation as step 3; the served `price_level` is derived (never the
            # raw int directly), preserving any Google-derived tier (see
            # _apply_besttime_refresh_price).
            besttime_price_level=vf.price_level,
            venue_foot_traffic_forecast=foot_traffic,
        )

        return venue

    def _apply_besttime_refresh_price(self, venue: Venue, existing: "Venue | None") -> None:
        """Set the served price tier on a refreshed venue from its BestTime price,
        WITHOUT clobbering a Google-derived tier.

        BestTime refresh rebuilds the Venue from scratch each cycle. If the venue
        already carries a Google-derived tier (`price_level_source` in
        google_enum/google_range with a real 1..4 value), preserve it and its raw
        signals. Otherwise derive 1..4/NULL from the raw BestTime price (never 0).
        """
        if (
            existing is not None
            and existing.price_level_source in GOOGLE_SOURCES
            and existing.price_level not in (None, 0)
        ):
            venue.price_level = existing.price_level
            venue.price_level_source = existing.price_level_source
            venue.price_range = existing.price_range
            venue.google_price_level = existing.google_price_level
            return
        derived = derive_price_signal(None, None, venue.besttime_price_level)
        venue.price_level = derived.price_level
        venue.price_level_source = derived.source

    async def refresh_venues_data_by_venues_filter(
        self,
        params: VenueFilterParams,
        fetch_and_cache_live: bool = False,
    ) -> list[str]:
        """Refresh venues using /venues/filter endpoint.

        CRITICAL: Implements exact deduplication logic from Go (lines 356-429).

        Args:
            params: Filter parameters
            fetch_and_cache_live: Whether to fetch and cache live forecasts

        Returns:
            List of unique venue IDs processed
        """
        logger.info(f"[VenuesRefresherService] VenueFilter start: params={params}")

        response = await self.besttime_api.venue_filter(params)
        logger.info(
            f"[VenuesRefresherService] VenueFilter status={response.status}, "
            f"venues_n={response.venues_n}"
        )

        if response.status != "OK":
            logger.warning(
                f"[VenuesRefresherService] VenueFilter returned non-OK status={response.status}; "
                "aborting upsert."
            )
            return []

        # CRITICAL: Deduplication algorithm (lines 374-417)
        seen_ids = set()
        seen_names = set()
        unique_ids = []

        for vf in response.venues:
            # Skip venues with no ID and no name
            if not vf.venue_id and not vf.venue_name:
                logger.debug(
                    f"[VenuesRefresherService] Skipping venue with no id and no name: {vf}"
                )
                REFRESH_DUPLICATES_SKIPPED.labels(reason="no_id_or_name").inc()
                continue

            # De-dupe by ID first
            if vf.venue_id:
                if vf.venue_id in seen_ids:
                    logger.debug(
                        f"[VenuesRefresherService] Skipping duplicate venue ID={vf.venue_id}"
                    )
                    REFRESH_DUPLICATES_SKIPPED.labels(reason="duplicate_id").inc()
                    continue

            # De-dupe by name second
            if vf.venue_name:
                if vf.venue_name in seen_names:
                    logger.debug(
                        f"[VenuesRefresherService] Skipping duplicate venue Name={vf.venue_name!r}"
                    )
                    REFRESH_DUPLICATES_SKIPPED.labels(reason="duplicate_name").inc()
                    continue

            # Map and upsert. Ineligible venues are upserted active and simply
            # excluded by the serving view (no born-deprecate / soft-delete).
            venue = self._map_venue_filter_venue_to_venue(vf)

            logger.info(
                f"[VenuesRefresherService] Upserting venue id={venue.venue_id}, "
                f"name={venue.venue_name!r}, lat={venue.venue_lat:.6f}, lng={venue.venue_lng:.6f}"
            )

            # Detect "new to our Redis state" before upsert so we can count
            # it toward the monthly budget exactly once. Reuse the existing-venue
            # read to preserve any Google-derived price tier across the refresh.
            existing_venue = None
            was_new_to_redis = False
            if venue.venue_id:
                try:
                    existing_venue = self.venue_dao.get_venue(venue.venue_id)
                except Exception:
                    # Be defensive: if we can't tell, assume new (it's only
                    # counter drift, BestTime is the source of truth).
                    existing_venue = None
                was_new_to_redis = existing_venue is None
            self._apply_besttime_refresh_price(venue, existing_venue)

            try:
                self.venue_dao.upsert_venue(venue)
            except Exception as e:
                logger.error(
                    f"[VenuesRefresherService] Upsert failed for {venue.venue_id}: {e}"
                )
                continue

            if was_new_to_redis and self.budget_service is not None:
                try:
                    new_count = self.budget_service.record_new_venue_from_discovery()
                    VENUE_MONTHLY_NEW_COUNT.set(new_count)
                except Exception as e:
                    logger.warning(
                        f"[VenuesRefresherService] failed to record new venue "
                        f"{venue.venue_id} against monthly counter: {e}"
                    )

            # Track as seen
            if venue.venue_id:
                seen_ids.add(venue.venue_id)
                unique_ids.append(venue.venue_id)
            if venue.venue_name:
                seen_names.add(venue.venue_name)

        logger.info(
            f"[VenuesRefresherService] Upserted {len(unique_ids)} unique venues via VenueFilter"
        )

        # Update metrics
        REFRESH_VENUES_UPSERTED.labels(operation="venue_filter").set(len(unique_ids))

        # Optionally fetch and cache live forecasts
        if fetch_and_cache_live and unique_ids:
            logger.info(
                "[VenuesRefresherService] Fetching and caching venues live forecasts."
            )
            await self._fetch_and_cache_live_forecasts(unique_ids)
        else:
            logger.info(
                "[VenuesRefresherService] Skipping live forecast fetch "
                "(flag or empty IDs)."
            )

        return unique_ids

    async def _fetch_and_cache_live_forecasts(self, venue_ids: list[str]) -> None:
        """Fetch and cache live forecasts for given venue IDs.

        CRITICAL: Implements exact filtering logic from Go (lines 243-274).

        Args:
            venue_ids: List of venue IDs to fetch forecasts for
        """
        logger.info(
            f"[VenuesRefresherService] Fetching live forecasts for {len(venue_ids)} venues"
        )

        for vid in venue_ids:
            if not self._ledger_allows_read(vid, "live_forecast"):
                continue
            logger.debug(
                f"[VenuesRefresherService] Fetching live forecast for venue_id={vid}"
            )

            try:
                lf = await self.besttime_api.get_live_forecast(venue_id=vid)
            except Exception as e:
                logger.error(
                    f"[VenuesRefresherService] GetLiveForecast failed for {vid}: {e}"
                )
                LIVE_FORECAST_FETCH_RESULTS.labels(result="error").inc()
                continue

            # CRITICAL: Live forecast filtering logic (lines 254-265)
            # Only cache if status OK AND live data available
            # If status not OK or live data not available (perhaps venue is closed),
            # delete stale cache entry
            if lf.status != "OK" or not lf.analysis.venue_live_busyness_available:
                if lf.status != "OK":
                    logger.warning(
                        f"[VenuesRefresherService] Error LiveForecast status={lf.status!r} "
                        f"for {vid}, removing cache"
                    )
                    LIVE_FORECAST_FETCH_RESULTS.labels(result="deleted_not_ok").inc()
                else:
                    logger.info(
                        f"[VenuesRefresherService] No error but LiveForecast not available, "
                        f"maybe venue is closed, for {vid}, removing cache"
                    )
                    LIVE_FORECAST_FETCH_RESULTS.labels(result="deleted_not_available").inc()

                try:
                    self.venue_dao.delete_live_forecast(vid)
                except Exception as e:
                    logger.error(
                        f"[VenuesRefresherService] Failed to delete stale live forecast "
                        f"for {vid}: {e}"
                    )
                continue

            # Cache the live forecast
            logger.debug(
                f"[VenuesRefresherService] Caching live forecast for venue_id={vid}"
            )
            try:
                self.venue_dao.set_live_forecast(lf)
                LIVE_FORECAST_FETCH_RESULTS.labels(result="cached").inc()
                logger.debug(
                    f"[VenuesRefresherService] Live forecast cached for venue_id={vid}"
                )
            except Exception as e:
                logger.error(
                    f"[VenuesRefresherService] SetLiveForecast failed for {vid}: {e}"
                )
                LIVE_FORECAST_FETCH_RESULTS.labels(result="error").inc()

    # ---- Discovery Points (admin-configurable locations) ----

    def _get_discovery_points(self) -> list[dict]:
        """Read discovery points from admin config in Redis.

        Returns list of point dicts or empty list if not configured.
        """
        if self.redis_client is None:
            return []
        try:
            raw = self.redis_client.get(self.ADMIN_CONFIG_DISCOVERY_POINTS_KEY)
            if raw is None:
                return []
            config = json.loads(raw)
            return config.get("points", [])
        except Exception as e:
            logger.error(f"[VenuesRefresherService] Failed to read discovery points: {e}")
            return []

    def _save_discovery_points(self, points: list[dict]) -> None:
        """Write updated discovery points back to admin config in Redis."""
        if self.redis_client is None:
            return
        try:
            self.redis_client.set(
                self.ADMIN_CONFIG_DISCOVERY_POINTS_KEY,
                json.dumps({"points": points}, ensure_ascii=False),
            )
        except Exception as e:
            logger.error(f"[VenuesRefresherService] Failed to save discovery points: {e}")

    def recount_discovery_points(self) -> list[dict]:
        """Recount venues for each discovery point using GEORADIUS.

        Returns updated list of discovery points with recounted current values.
        """
        points = self._get_discovery_points()
        if not points:
            logger.warning("[VenuesRefresherService] No discovery points to recount")
            return []

        for point in points:
            lat = point.get("lat", 0)
            lng = point.get("lng", 0)
            radius = point.get("radius", 15000)
            point_id = point.get("id", "unknown")

            count = self.venue_dao.count_venues_in_radius(lat, lng, float(radius))
            old_current = point.get("current", 0)
            point["current"] = count

            logger.info(
                f"[VenuesRefresherService] Recount '{point_id}': "
                f"old={old_current}, new={count} (radius={radius}m)"
            )

        self._save_discovery_points(points)
        logger.info(f"[VenuesRefresherService] Recount complete for {len(points)} points")
        return points

    async def _refresh_with_discovery_points(
        self,
        points: list[dict],
        remaining_budget: int,
        fetch_and_cache_live: bool,
    ) -> int:
        """Refresh using admin-configured discovery points with per-point counters."""
        total_inserted = 0
        points_updated = False

        for point in points:
            point_id = point.get("id", "unknown")
            current = point.get("current", 0)
            limit = point.get("limit", 500)
            lat = point.get("lat", 0)
            lng = point.get("lng", 0)
            radius = point.get("radius", 15000)

            headroom = limit - current
            if headroom <= 0:
                logger.info(
                    f"[VenuesRefresherService] Skipping '{point_id}' "
                    f"(current={current} >= limit={limit})"
                )
                continue

            effective_limit = headroom
            if self.fetch_venue_limit_override > 0:
                effective_limit = min(effective_limit, self.fetch_venue_limit_override)
            if remaining_budget >= 0:
                effective_limit = min(effective_limit, remaining_budget)
                if effective_limit <= 0:
                    logger.info("[VenuesRefresherService] Global budget reached, skipping remaining")
                    break

            logger.info(
                f"[VenuesRefresherService] Discovery point '{point_id}': "
                f"lat={lat:.6f}, lng={lng:.6f}, radius={radius}, "
                f"current={current}/{limit}, fetching up to {effective_limit}"
            )

            params = VenueFilterParams(
                busy_min=0,
                lat=lat,
                lng=lng,
                radius=radius,
                foot_traffic="both",
                limit=effective_limit,
                own_venues_only=False,
                types=VENUE_TYPES,
            )

            location_label = f"{lat:.4f},{lng:.4f}"
            try:
                ids = await self.refresh_venues_data_by_venues_filter(
                    params, fetch_and_cache_live
                )
                fetched_count = len(ids)
                logger.info(
                    f"[VenuesRefresherService] Discovery point '{point_id}': "
                    f"upserted {fetched_count} venues"
                )
                REFRESH_VENUES_DISCOVERED.labels(location=location_label).set(fetched_count)

                point["current"] = current + fetched_count
                points_updated = True
                total_inserted += fetched_count
                if remaining_budget >= 0:
                    remaining_budget -= fetched_count
            except Exception as e:
                logger.error(
                    f"[VenuesRefresherService] Discovery point '{point_id}' failed: {e}"
                )
                REFRESH_VENUES_DISCOVERED.labels(location=location_label).set(0)
                continue

        if points_updated:
            self._save_discovery_points(points)
            logger.info("[VenuesRefresherService] Updated discovery point counters in Redis")

        return total_inserted

    async def _refresh_with_locations(
        self,
        locations: list[Location],
        remaining_budget: int,
        fetch_and_cache_live: bool,
    ) -> int:
        """Refresh using Location objects (legacy/dev mode path)."""
        total_inserted = 0

        for loc in locations:
            effective_limit = (
                self.fetch_venue_limit_override if self.fetch_venue_limit_override > 0 else loc.limit
            )
            if remaining_budget >= 0:
                effective_limit = min(effective_limit, remaining_budget)
                if effective_limit <= 0:
                    logger.info(
                        f"[VenuesRefresherService] Global fetch_venue_total_limit "
                        f"({self.fetch_venue_total_limit}) reached, skipping remaining"
                    )
                    break

            logger.info(
                f"[VenuesRefresherService] VenueFilter refresh at "
                f"lat={loc.lat:.6f}, lng={loc.lng:.6f} "
                f"(Radius={loc.radius}, Limit={effective_limit})"
            )

            params = VenueFilterParams(
                busy_min=0,
                lat=loc.lat,
                lng=loc.lng,
                radius=loc.radius,
                foot_traffic="both",
                limit=effective_limit,
                own_venues_only=False,
                types=VENUE_TYPES,
            )

            location_label = f"{loc.lat:.4f},{loc.lng:.4f}"
            try:
                ids = await self.refresh_venues_data_by_venues_filter(
                    params, fetch_and_cache_live
                )
                logger.info(
                    f"[VenuesRefresherService] Successfully upserted {len(ids)} venues "
                    f"for lat={loc.lat:.6f}, lng={loc.lng:.6f}"
                )
                REFRESH_VENUES_DISCOVERED.labels(location=location_label).set(len(ids))
                total_inserted += len(ids)
                if remaining_budget >= 0:
                    remaining_budget -= len(ids)
            except Exception as e:
                logger.error(
                    f"[VenuesRefresherService] VenueFilter refresh failed for "
                    f"lat={loc.lat:.6f}, lng={loc.lng:.6f}: {e}"
                )
                REFRESH_VENUES_DISCOVERED.labels(location=location_label).set(0)
                continue

        return total_inserted

    async def sync_account_inventory_to_redis(self) -> dict:
        """Pull every venue from BestTime /api/v1/venues into Redis.

        For each inventory venue not already in our geo index, upsert it.
        Never increments the monthly new-venue counter — these venues are
        already in the BestTime account inventory and cost no credits.

        Returns a summary dict with seen/upserted/skipped/errors counts.
        """
        summary = {"seen": 0, "upserted": 0, "skipped": 0, "errors": 0, "deprecated": 0}
        try:
            iterator = self.besttime_api.list_account_inventory()
        except Exception as e:
            logger.error(
                f"[VenuesRefresherService] inventory list failed to start: {e}"
            )
            INVENTORY_SYNC_RUNS_TOTAL.labels(outcome="failed").inc()
            return summary

        try:
            async for inv in iterator:
                summary["seen"] += 1
                INVENTORY_SYNC_VENUES_TOTAL.labels(result="seen").inc()
                try:
                    if not inv.venue_id:
                        summary["errors"] += 1
                        INVENTORY_SYNC_VENUES_TOTAL.labels(result="error").inc()
                        continue
                    existing = self.venue_dao.get_venue(inv.venue_id)
                    if existing is not None:
                        summary["skipped"] += 1
                        INVENTORY_SYNC_VENUES_TOTAL.labels(result="skipped").inc()
                        continue
                    venue = Venue(
                        processed=True,
                        forecast=bool(inv.venue_forecasted),
                        venue_id=inv.venue_id,
                        venue_name=inv.venue_name or "",
                        venue_address=inv.venue_address or "",
                        venue_lat=float(inv.venue_lat or 0.0),
                        venue_lng=float(inv.venue_lng or 0.0),
                    )
                    # Upserted active; ineligible venues are excluded by the
                    # serving view, not soft-deleted at write time.
                    self.venue_dao.upsert_venue(venue)
                    summary["upserted"] += 1
                    INVENTORY_SYNC_VENUES_TOTAL.labels(result="upserted").inc()
                except Exception as e:
                    summary["errors"] += 1
                    INVENTORY_SYNC_VENUES_TOTAL.labels(result="error").inc()
                    logger.warning(
                        f"[VenuesRefresherService] inventory upsert failed for "
                        f"{getattr(inv, 'venue_id', '?')}: {e}"
                    )
        except Exception as e:
            logger.error(
                f"[VenuesRefresherService] inventory sync iteration failed: {e}"
            )
            INVENTORY_SYNC_RUNS_TOTAL.labels(outcome="partial").inc()
            return summary

        outcome = "ok" if summary["errors"] == 0 else "partial"
        INVENTORY_SYNC_RUNS_TOTAL.labels(outcome=outcome).inc()
        logger.info(
            f"[VenuesRefresherService] inventory sync: seen={summary['seen']} "
            f"upserted={summary['upserted']} skipped={summary['skipped']} "
            f"deprecated={summary['deprecated']} errors={summary['errors']}"
        )
        return summary

    async def refresh_venues_by_filter_for_default_locations(
        self, fetch_and_cache_live: bool = False
    ) -> None:
        """Refresh venues for configured discovery points or default locations.

        Step 1: sync the full BestTime account inventory into Redis (no
                credit cost; failure is logged but does not abort step 2).
        Step 2: discovery refresh via /venues/filter, respecting the
                monthly new-venue cap and manual-add reserve.
        """
        # Step 1: inventory sync (skip in dev_mode to keep per-iteration
        # latency low for local development).
        if not self.dev_mode:
            try:
                await self.sync_account_inventory_to_redis()
            except Exception as e:
                logger.error(
                    f"[VenuesRefresherService] inventory sync raised; "
                    f"continuing with discovery: {e}"
                )

        # Global total limit: -1 = disabled, 0 = fetch none
        if self.fetch_venue_total_limit == 0:
            logger.info(
                "[VenuesRefresherService] fetch_venue_total_limit=0, skipping venue fetch"
            )
            return

        # Step 2: apply monthly cap on top of fetch_venue_total_limit.
        remaining_budget = self.fetch_venue_total_limit  # -1 means unlimited
        if self.budget_service is not None:
            monthly_remaining = self.budget_service.discovery_effective_cap_remaining()
            VENUE_MONTHLY_NEW_COUNT.set(
                self.budget_service.get_snapshot().month_counter
            )
            if monthly_remaining <= 0:
                logger.warning(
                    f"[VenuesRefresherService] monthly new-venue cap reached "
                    f"(discovery_effective_cap_remaining=0); skipping discovery"
                )
                DISCOVERY_SKIPPED_DUE_TO_MONTHLY_CAP_TOTAL.inc()
                return
            if remaining_budget < 0:
                remaining_budget = monthly_remaining
            else:
                remaining_budget = min(remaining_budget, monthly_remaining)

        # Dev mode: single location, no discovery points
        if self.dev_mode:
            locations = [
                Location(
                    lat=self.dev_lat,
                    lng=self.dev_lng,
                    radius=self.dev_radius,
                    limit=self.fetch_venue_total_limit if self.fetch_venue_total_limit > 0 else 500,
                )
            ]
            logger.info(
                f"[VenuesRefresherService] DEV MODE: using single location "
                f"lat={self.dev_lat:.5f}, lng={self.dev_lng:.5f}, radius={self.dev_radius}"
            )
            total = await self._refresh_with_locations(locations, remaining_budget, fetch_and_cache_live)
            logger.info(f"[VenuesRefresherService] DEV MODE refresh done; total={total}")
            self.update_data_quality_metrics()
            return

        # Production: try discovery points from Redis, fall back to DEFAULT_LOCATIONS
        discovery_points = self._get_discovery_points()

        if discovery_points:
            logger.info(
                f"[VenuesRefresherService] Using {len(discovery_points)} discovery points from admin config"
            )
            total = await self._refresh_with_discovery_points(
                discovery_points, remaining_budget, fetch_and_cache_live
            )
        else:
            logger.info(
                f"[VenuesRefresherService] No discovery points in admin config, "
                f"falling back to {len(DEFAULT_LOCATIONS)} hardcoded locations"
            )
            total = await self._refresh_with_locations(
                DEFAULT_LOCATIONS, remaining_budget, fetch_and_cache_live
            )

        logger.info(
            f"[VenuesRefresherService] Finished VenueFilter refresh; "
            f"total venues upserted={total}"
        )
        self.update_data_quality_metrics()

    async def refresh_live_forecasts_for_all_venues(self) -> None:
        """Refresh live forecasts for all known venues.

        Implements logic from Go (lines 305-315).
        """
        try:
            ids = self._select_refresh_venue_ids("live_forecast")
        except Exception as e:
            logger.error(f"[VenuesRefresherService] live refresh selection failed: {e}")
            raise

        logger.info(
            f"[VenuesRefresherService] Selected {len(ids)} venues; "
            "refreshing live forecasts."
        )
        await self._fetch_and_cache_live_forecasts(ids)

        self._update_touched_gauge()

        # Update data quality metrics after live refresh
        self.update_data_quality_metrics()

    async def refresh_weekly_forecasts_for_all_venues(self) -> None:
        """Refresh weekly forecasts for all known venues.

        Implements exact logic from Go (lines 538-581).
        """
        try:
            ids = self._select_refresh_venue_ids("weekly_forecast")
        except Exception as e:
            logger.error(
                f"[VenuesRefresherService] weekly refresh selection failed: {e}"
            )
            raise

        logger.info(
            f"[VenuesRefresherService] Selected {len(ids)} venues; refreshing weekly forecasts"
        )

        total_cached = 0
        for vid in ids:
            if not self._ledger_allows_read(vid, "weekly_forecast"):
                continue
            logger.debug(
                f"[VenuesRefresherService] Fetching weekly raw forecast for venue_id={vid}"
            )

            try:
                resp = await self.besttime_api.get_week_raw_forecast(vid)
            except Exception as e:
                logger.error(
                    f"[VenuesRefresherService] GetWeekRawForecast failed for {vid}: {e}"
                )
                WEEKLY_FORECAST_FETCH_RESULTS.labels(result="error").inc()
                continue

            if resp.status != "OK":
                logger.warning(
                    f"[VenuesRefresherService] Weekly raw forecast status non-OK "
                    f"({resp.status}) for {vid}. Skipping cache."
                )
                WEEKLY_FORECAST_FETCH_RESULTS.labels(result="skipped_not_ok").inc()
                continue

            # Cache each day's raw forecast
            cached_count = 0
            for day in resp.analysis.week_raw:
                try:
                    self.venue_dao.set_week_raw_forecast(vid, day)
                    cached_count += 1
                except Exception as e:
                    logger.error(
                        f"[VenuesRefresherService] Failed to cache weekly raw forecast "
                        f"for {vid} day {day.day_int}: {e}"
                    )

            if cached_count > 0:
                WEEKLY_FORECAST_FETCH_RESULTS.labels(result="cached").inc()
                total_cached += 1

            logger.info(
                f"[VenuesRefresherService] Successfully cached {cached_count} of "
                f"{len(resp.analysis.week_raw)} raw days for {vid}"
            )

        REFRESH_VENUES_UPSERTED.labels(operation="weekly_forecast").set(total_cached)
        logger.info("[VenuesRefresherService] Finished weekly raw forecast refresh.")

        self._update_touched_gauge()

        # Update data quality metrics after weekly refresh
        self.update_data_quality_metrics()
