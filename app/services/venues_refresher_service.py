"""Venues refresher service with background job orchestration."""
import logging
from typing import Optional
from dataclasses import dataclass

from app.api import BestTimeAPIClient
from app.dao import RedisVenueDAO
from app.models import (
    Venue,
    FootTrafficForecast,
    VenueFilterParams,
    VenueFilterVenue,
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

    def __init__(self, venue_dao: RedisVenueDAO, besttime_api: BestTimeAPIClient):
        """Initialize refresher service.

        Args:
            venue_dao: Redis DAO for venue persistence
            besttime_api: BestTime API client
        """
        self.venue_dao = venue_dao
        self.besttime_api = besttime_api

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
                continue

            # De-dupe by ID first
            if vf.venue_id:
                if vf.venue_id in seen_ids:
                    logger.debug(
                        f"[VenuesRefresherService] Skipping duplicate venue ID={vf.venue_id}"
                    )
                    continue

            # De-dupe by name second
            if vf.venue_name:
                if vf.venue_name in seen_names:
                    logger.debug(
                        f"[VenuesRefresherService] Skipping duplicate venue Name={vf.venue_name!r}"
                    )
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
                else:
                    logger.info(
                        f"[VenuesRefresherService] No error but LiveForecast not available, "
                        f"maybe venue is closed, for {vid}, removing cache"
                    )

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
                logger.debug(
                    f"[VenuesRefresherService] Live forecast cached for venue_id={vid}"
                )
            except Exception as e:
                logger.error(
                    f"[VenuesRefresherService] SetLiveForecast failed for {vid}: {e}"
                )

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

        for loc in DEFAULT_LOCATIONS:
            logger.info(
                f"[VenuesRefresherService] VenueFilter refresh at "
                f"lat={loc.lat:.6f}, lng={loc.lng:.6f} "
                f"(Radius={loc.radius}, Limit={loc.limit})"
            )

            params = VenueFilterParams(
                busy_min=min_busy,
                lat=loc.lat,
                lng=loc.lng,
                radius=loc.radius,
                foot_traffic="both",
                limit=loc.limit,
                own_venues_only=own_venues_only,
                types=NIGHTLIFE_VENUE_TYPES,
            )

            try:
                ids = await self.refresh_venues_data_by_venues_filter(
                    params, fetch_and_cache_live
                )
                logger.info(
                    f"[VenuesRefresherService] Successfully upserted {len(ids)} venues "
                    f"for lat={loc.lat:.6f}, lng={loc.lng:.6f}"
                )
                total_inserted += len(ids)
            except Exception as e:
                logger.error(
                    f"[VenuesRefresherService] VenueFilter refresh failed for "
                    f"lat={loc.lat:.6f}, lng={loc.lng:.6f}: {e}"
                )
                continue

        logger.info(
            f"[VenuesRefresherService] Finished VenueFilter refresh for all locations; "
            f"total venues upserted={total_inserted}"
        )

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
                continue

            if resp.status != "OK":
                logger.warning(
                    f"[VenuesRefresherService] Weekly raw forecast status non-OK "
                    f"({resp.status}) for {vid}. Skipping cache."
                )
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

            logger.info(
                f"[VenuesRefresherService] Successfully cached {cached_count} of "
                f"{len(resp.analysis.week_raw)} raw days for {vid}"
            )

        logger.info("[VenuesRefresherService] Finished weekly raw forecast refresh.")
