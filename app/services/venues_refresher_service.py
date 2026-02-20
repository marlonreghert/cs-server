"""Venues refresher service with background job orchestration."""
import logging
from typing import Optional
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
)

logger = logging.getLogger(__name__)


@dataclass
class Location:
    """Location configuration for venue discovery."""
    lat: float
    lng: float
    radius: int  # Meters
    limit: int   # Max venues to fetch


# CRITICAL: Default locations - exact values from Go implementation
# Lines 39-41 in service/venues_refresher_service.go
DEFAULT_LOCATIONS = [
    Location(lat=-8.07834, lng=-34.90938, radius=6000, limit=500),  # ZS/ZN - C1
    Location(lat=-7.99081, lng=-34.85141, radius=6000, limit=200),  # Olinda
    Location(lat=-8.18160, lng=-34.92980, radius=6000, limit=200),  # Jaboatao/Candeias
]

# CRITICAL: Nightlife venue types - exact list from Go implementation
# Lines 60-96 in service/venues_refresher_service.go
NIGHTLIFE_VENUE_TYPES = [
    "BAR",
    "BREWERY",
    "CASINO",
    "CONCERT_HALL",
    "ADULT",
    "CLUBS",
    "EVENT_VENUE",
    "FOOD_AND_DRINK",
    "PERFORMING_ARTS",
    "ARTS",
    "WINERY",
]


class VenuesRefresherService:
    """Service for refreshing venue data from BestTime API."""

    def __init__(
        self,
        venue_dao: RedisVenueDAO,
        besttime_api: BestTimeAPIClient,
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
            fetch_venue_limit_override: If > 0, overrides the limit for each location when fetching from BestTime API
            fetch_venue_total_limit: Global cap on total venues fetched across all locations (-1 = disabled, 0 = fetch none)
            dev_mode: If True, use single dev location instead of DEFAULT_LOCATIONS
            dev_lat: Dev mode latitude
            dev_lng: Dev mode longitude
            dev_radius: Dev mode radius in meters
        """
        self.venue_dao = venue_dao
        self.besttime_api = besttime_api
        self.fetch_venue_limit_override = fetch_venue_limit_override
        self.fetch_venue_total_limit = fetch_venue_total_limit
        self.dev_mode = dev_mode
        self.dev_lat = dev_lat
        self.dev_lng = dev_lng
        self.dev_radius = dev_radius

    def update_data_quality_metrics(self) -> None:
        """Compute and update all data quality metrics from cached venues.

        This method reads all venues from the cache and updates Prometheus
        gauges with counts and statistics about data quality.
        """
        try:
            venues = self.venue_dao.list_all_venues()
        except Exception as e:
            logger.error(f"[VenuesRefresherService] Failed to list venues for metrics: {e}")
            return

        total = len(venues)
        VENUES_TOTAL.set(total)

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
            f"with_live={live_count}, with_weekly={weekly_count}"
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
            price_level=vf.price_level,
            venue_foot_traffic_forecast=foot_traffic,
        )

        return venue

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

            # Map and upsert
            venue = self._map_venue_filter_venue_to_venue(vf)

            logger.info(
                f"[VenuesRefresherService] Upserting venue id={venue.venue_id}, "
                f"name={venue.venue_name!r}, lat={venue.venue_lat:.6f}, lng={venue.venue_lng:.6f}"
            )

            try:
                self.venue_dao.upsert_venue(venue)
            except Exception as e:
                logger.error(
                    f"[VenuesRefresherService] Upsert failed for {venue.venue_id}: {e}"
                )
                continue

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

    async def refresh_venues_by_filter_for_default_locations(
        self, fetch_and_cache_live: bool = False
    ) -> None:
        """Refresh venues for all default locations using VenueFilter.

        Implements exact logic from Go (lines 486-536).

        Args:
            fetch_and_cache_live: Whether to fetch and cache live forecasts
        """
        logger.info(
            f"[VenuesRefresherService] Starting VenueFilter refresh for "
            f"{len(DEFAULT_LOCATIONS)} default locations"
        )

        total_inserted = 0
        min_busy = 1
        own_venues_only = False

        # Global total limit: -1 = disabled, 0 = fetch none
        if self.fetch_venue_total_limit == 0:
            logger.info(
                "[VenuesRefresherService] fetch_venue_total_limit=0, skipping venue fetch"
            )
            return

        remaining_budget = self.fetch_venue_total_limit  # -1 means unlimited

        # In dev mode, use single dev location instead of all defaults
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
        else:
            locations = DEFAULT_LOCATIONS

        for loc in locations:
            # Use override limit if set, otherwise use location's default limit
            effective_limit = (
                self.fetch_venue_limit_override if self.fetch_venue_limit_override > 0 else loc.limit
            )

            # Cap per-location limit by remaining global budget
            if remaining_budget >= 0:
                effective_limit = min(effective_limit, remaining_budget)
                if effective_limit <= 0:
                    logger.info(
                        f"[VenuesRefresherService] Global fetch_venue_total_limit "
                        f"({self.fetch_venue_total_limit}) reached, skipping remaining locations"
                    )
                    break

            logger.info(
                f"[VenuesRefresherService] VenueFilter refresh at "
                f"lat={loc.lat:.6f}, lng={loc.lng:.6f} "
                f"(Radius={loc.radius}, Limit={effective_limit})"
            )

            params = VenueFilterParams(
                busy_min=min_busy,
                lat=loc.lat,
                lng=loc.lng,
                radius=loc.radius,
                foot_traffic="both",
                limit=effective_limit,
                own_venues_only=own_venues_only,
                types=NIGHTLIFE_VENUE_TYPES,
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

        logger.info(
            f"[VenuesRefresherService] Finished VenueFilter refresh for all locations; "
            f"total venues upserted={total_inserted}"
        )

        # Update data quality metrics after catalog refresh
        self.update_data_quality_metrics()

    async def refresh_live_forecasts_for_all_venues(self) -> None:
        """Refresh live forecasts for all known venues.

        Implements logic from Go (lines 305-315).
        """
        try:
            ids = self.venue_dao.list_all_venue_ids()
        except Exception as e:
            logger.error(f"[VenuesRefresherService] ListAllVenueIDs failed: {e}")
            raise

        logger.info(
            f"[VenuesRefresherService] Found {len(ids)} venues in geo cache; "
            "refreshing live forecasts."
        )
        await self._fetch_and_cache_live_forecasts(ids)

        # Update data quality metrics after live refresh
        self.update_data_quality_metrics()

    async def refresh_weekly_forecasts_for_all_venues(self) -> None:
        """Refresh weekly forecasts for all known venues.

        Implements exact logic from Go (lines 538-581).
        """
        try:
            ids = self.venue_dao.list_all_venue_ids()
        except Exception as e:
            logger.error(
                f"[VenuesRefresherService] ListAllVenueIDs failed for weekly refresh: {e}"
            )
            raise

        logger.info(
            f"[VenuesRefresherService] Found {len(ids)} venues; refreshing weekly forecasts"
        )

        total_cached = 0
        for vid in ids:
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

        # Update data quality metrics after weekly refresh
        self.update_data_quality_metrics()
