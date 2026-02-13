"""Venue handler for HTTP requests."""
import logging
from datetime import datetime
from typing import Optional

import pytz

from app.dao import RedisVenueDAO
from app.models import (
    Venue,
    VenueWithLive,
    MinifiedVenue,
    LiveForecastResponse,
    WeekRawDay,
)

logger = logging.getLogger(__name__)


class VenueHandler:
    """Handler for venue-related HTTP requests."""

    def __init__(self, venue_dao: RedisVenueDAO):
        """Initialize venue handler.

        Args:
            venue_dao: Redis DAO for venue data access
        """
        self.venue_dao = venue_dao

    def get_venues_nearby(
        self, lat: float, lon: float, radius: float, verbose: bool = False
    ) -> list[VenueWithLive] | list[MinifiedVenue]:
        """Get venues near a location with live and weekly forecasts.

        CRITICAL: Implements exact logic from Go handler (server/handlers/venue_handler.go).

        Flow:
        1. Load nearby venues from geo index
        2. Merge with live forecasts and weekly forecasts for current day
        3. Sort: venues with live data first (desc by busyness), then without
        4. Transform based on verbose flag

        Args:
            lat: Latitude
            lon: Longitude
            radius: Radius in kilometers
            verbose: If True, return full VenueWithLive; if False, return MinifiedVenue

        Returns:
            List of VenueWithLive (verbose=True) or MinifiedVenue (verbose=False)
        """
        logger.info(
            f"[VenueHandler] GetVenuesNearby: lat={lat:.6f}, lon={lon:.6f}, "
            f"radius={radius:.2f}km, verbose={verbose}"
        )

        # 1. Load nearby venues
        venues = self._load_nearby(lat, lon, radius)
        logger.info(f"[VenueHandler] Found {len(venues)} nearby venues")

        # 2. Merge with live and weekly forecasts
        merged = self._merge(venues)

        # 3. Transform based on verbose flag
        result = self._transform(merged, verbose)

        logger.info(f"[VenueHandler] Returning {len(result)} venues")
        return result

    def ping(self) -> dict[str, str]:
        """Health check endpoint.

        Returns:
            {"status": "pong"}
        """
        logger.debug("[VenueHandler] Ping")
        return {"status": "pong"}

    def _load_nearby(self, lat: float, lon: float, radius: float) -> list[Venue]:
        """Load nearby venues from geo index.

        Args:
            lat: Latitude
            lon: Longitude
            radius: Radius in kilometers

        Returns:
            List of nearby venues
        """
        return self.venue_dao.get_nearby_venues(lat, lon, radius)

    def _merge(self, venues: list[Venue]) -> list[VenueWithLive]:
        """Merge venues with live and weekly forecasts.

        CRITICAL: Implements exact logic from Go (lines 130-196).

        - Fetches live forecast for each venue
        - Fetches weekly raw forecast for current day in Recife timezone
        - Always includes venue (sets Live/WeeklyForecast to None if not found)
        - Sorts: venues with live data first (desc by busyness), then without

        Args:
            venues: List of venues to merge

        Returns:
            List of VenueWithLive with live and weekly data
        """
        out: list[VenueWithLive] = []

        # CRITICAL: Day index conversion (lines 143-148 from Go)
        # Get current day in Recife timezone
        try:
            recife_tz = pytz.timezone("America/Recife")
        except Exception as e:
            logger.error(
                f"[VenueHandler] Failed to load America/Recife timezone: {e}. "
                "Falling back to UTC."
            )
            recife_tz = pytz.UTC

        recife_time = datetime.now(recife_tz)
        python_weekday = recife_time.weekday()  # 0=Mon, 6=Sun

        # CRITICAL: Python weekday() already matches BestTime day_int!
        # No conversion needed (unlike Go which has 0=Sun)
        besttime_day_int = python_weekday

        logger.info(
            f"[VenueHandler] Current Recife time: {recife_time.strftime('%Y-%m-%d %H:%M:%S %Z')}, "
            f"Python weekday: {python_weekday}, BestTime day_int: {besttime_day_int}"
        )

        for v in venues:
            # 1. Fetch live forecast
            lf: Optional[LiveForecastResponse] = None
            try:
                lf = self.venue_dao.get_live_forecast(v.venue_id)
            except Exception as e:
                logger.debug(
                    f"[VenueHandler] No live forecast for venue_id={v.venue_id}: {e}"
                )

            # 2. Fetch weekly raw forecast for current day
            raw_day: Optional[WeekRawDay] = None
            try:
                raw_day = self.venue_dao.get_week_raw_forecast(
                    v.venue_id, besttime_day_int
                )
            except Exception as e:
                logger.debug(
                    f"[VenueHandler] No weekly forecast for venue_id={v.venue_id} "
                    f"day {besttime_day_int}: {e}"
                )

            out.append(
                VenueWithLive(
                    venue=v,
                    live_forecast=lf,
                    weekly_forecast=raw_day,
                )
            )

        # CRITICAL: Sort by live busyness (lines 175-193 from Go)
        # Venues with live data first (desc by busyness), then without live
        def sort_key(venue_with_live: VenueWithLive) -> tuple[int, int]:
            if venue_with_live.live_forecast is None:
                return (1, 0)  # No live: group 1, busyness 0
            # Has live: group 0, negative busyness for descending sort
            return (0, -venue_with_live.live_forecast.analysis.venue_live_busyness)

        out.sort(key=sort_key)

        return out

    def _transform(
        self, merged: list[VenueWithLive], verbose: bool
    ) -> list[VenueWithLive] | list[MinifiedVenue]:
        """Transform merged venues based on verbose flag.

        CRITICAL: Implements exact logic from Go (lines 199-232).

        Args:
            merged: List of merged venues with live/weekly data
            verbose: If True, return full; if False, return minified

        Returns:
            Full VenueWithLive list (verbose=True) or MinifiedVenue list (verbose=False)
        """
        if verbose:
            # Verbose mode: return full structure
            return merged

        # Minified mode: extract essential fields
        minified: list[MinifiedVenue] = []
        for m in merged:
            # Extract live busyness (only if available)
            live_busyness: Optional[int] = None
            if (
                m.live_forecast is not None
                and m.live_forecast.analysis.venue_live_busyness_available
            ):
                live_busyness = m.live_forecast.analysis.venue_live_busyness

            # Get vibe labels if available
            vibe_labels: Optional[list[str]] = None
            try:
                vibe_attrs = self.venue_dao.get_vibe_attributes(m.venue.venue_id)
                if vibe_attrs:
                    vibe_labels = vibe_attrs.get_vibe_labels()
            except Exception as e:
                logger.debug(f"[VenueHandler] No vibe attributes for {m.venue.venue_id}: {e}")

            # Get venue photos if available
            venue_photos_urls: Optional[list[str]] = None
            try:
                venue_photos_urls = self.venue_dao.get_venue_photos(m.venue.venue_id)
            except Exception as e:
                logger.debug(f"[VenueHandler] No photos for {m.venue.venue_id}: {e}")

            # Get opening hours if available
            opening_hours: Optional[list[str]] = None
            special_days: Optional[list[str]] = None
            is_open_now: Optional[bool] = None
            try:
                hours = self.venue_dao.get_opening_hours(m.venue.venue_id)
                if hours:
                    opening_hours = hours.weekday_descriptions if hours.has_hours() else None
                    special_days = hours.special_days
                    is_open_now = hours.open_now
            except Exception as e:
                logger.debug(f"[VenueHandler] No opening hours for {m.venue.venue_id}: {e}")

            # Get Instagram handle if available
            instagram_handle: Optional[str] = None
            instagram_url: Optional[str] = None
            try:
                ig_data = self.venue_dao.get_venue_instagram(m.venue.venue_id)
                if ig_data and ig_data.has_instagram():
                    instagram_handle = ig_data.instagram_handle
                    instagram_url = ig_data.instagram_url
            except Exception as e:
                logger.debug(f"[VenueHandler] No Instagram for {m.venue.venue_id}: {e}")

            minified.append(
                MinifiedVenue(
                    forecast=m.venue.forecast,
                    processed=m.venue.processed,
                    venue_address=m.venue.venue_address,
                    venue_foot_traffic_forecast=m.venue.venue_foot_traffic_forecast,
                    venue_live_busyness=live_busyness,
                    venue_lat=m.venue.venue_lat,
                    venue_lng=m.venue.venue_lng,
                    venue_name=m.venue.venue_name,
                    venue_id=m.venue.venue_id,
                    venue_type=m.venue.venue_type,
                    price_level=m.venue.price_level,
                    rating=m.venue.rating,
                    reviews=m.venue.reviews,
                    weekly_forecast=m.weekly_forecast,
                    vibe_labels=vibe_labels,
                    venue_photos_urls=venue_photos_urls,
                    opening_hours=opening_hours,
                    special_days=special_days,
                    is_open_now=is_open_now,
                    instagram_handle=instagram_handle,
                    instagram_url=instagram_url,
                )
            )

        return minified
