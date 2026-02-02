"""Redis-based Data Access Object for venue operations."""
import json
import logging
from typing import Optional
import redis

from app.db.geo_redis_client import GeoRedisClient
from app.models import Venue, LiveForecastResponse, WeekRawDay

logger = logging.getLogger(__name__)

# CRITICAL: These key formats must match exactly with Go implementation for backward compatibility
VENUES_GEO_KEY_V1 = "venues_geo_v1"
VENUES_GEO_PLACE_MEMBER_FORMAT_V1 = "venues_geo_place_v1:{}"
LIVE_FORECAST_KEY_FORMAT = "live_forecast_v1:{}"
WEEKLY_FORECAST_KEY_FORMAT = "weekly_forecast_v1:{}_{}"


class RedisVenueDAO:
    """Data Access Object for venue operations using Redis."""

    def __init__(self, client: GeoRedisClient):
        """Initialize RedisVenueDAO.

        Args:
            client: GeoRedisClient instance
        """
        self.client = client

    def upsert_venue(self, venue: Venue) -> None:
        """Store venue as a geolocation with JSON data.

        Args:
            venue: Venue object to store
        """
        venue_key = VENUES_GEO_PLACE_MEMBER_FORMAT_V1.format(venue.venue_id)
        self.client.add_location_with_json(
            geo_key=VENUES_GEO_KEY_V1,
            member_key=venue_key,
            lat=venue.venue_lat,
            lon=venue.venue_lng,
            data=venue,
        )

    def get_nearby_venues(self, lat: float, lon: float, radius: float) -> list[Venue]:
        """Retrieve nearby venues within a given radius.

        Args:
            lat: Center latitude
            lon: Center longitude
            radius: Radius in kilometers

        Returns:
            List of Venue objects
        """
        logger.info("Getting nearby venues")

        venues_json = self.client.get_locations_within_radius(
            VENUES_GEO_KEY_V1, lat, lon, radius
        )

        venues = []
        for venue_json in venues_json:
            try:
                venue = Venue.model_validate_json(venue_json)
                venues.append(venue)
            except Exception as e:
                logger.error(f"Failed to unmarshal venue JSON: {e}")
                continue

        logger.info(f"Finished getting nearby venues: found {len(venues)}")
        return venues

    def set_live_forecast(self, forecast: LiveForecastResponse) -> None:
        """Cache live forecast for a venue by its ID.

        Args:
            forecast: LiveForecastResponse object
        """
        key = LIVE_FORECAST_KEY_FORMAT.format(forecast.venue_info.venue_id)
        json_data = forecast.model_dump_json(by_alias=True)
        self.client.set(key, json_data)

    def get_live_forecast(self, venue_id: str) -> Optional[LiveForecastResponse]:
        """Retrieve cached live forecast for a venue by its ID.

        Args:
            venue_id: Venue identifier

        Returns:
            LiveForecastResponse or None if not found
        """
        key = LIVE_FORECAST_KEY_FORMAT.format(venue_id)
        try:
            json_str = self.client.get(key)
            if json_str is None:
                return None
            return LiveForecastResponse.model_validate_json(json_str)
        except redis.RedisError as e:
            logger.error(f"Failed to get live forecast from Redis: {e}")
            return None

    def delete_live_forecast(self, venue_id: str) -> None:
        """Delete cached live forecast for a venue.

        Args:
            venue_id: Venue identifier
        """
        key = LIVE_FORECAST_KEY_FORMAT.format(venue_id)
        self.client.del_(key)
        logger.info(f"[RedisVenueDAO] Deleted live forecast cache for {venue_id}")

    def list_cached_live_forecast_venue_ids(self) -> list[str]:
        """Return venue IDs for all cached live forecasts.

        Returns:
            List of venue IDs
        """
        pattern = "live_forecast_v1:*"
        keys = self.client.keys(pattern)

        # Strip prefix to get raw venue IDs
        prefix = "live_forecast_v1:"
        venue_ids = [key.replace(prefix, "", 1) for key in keys]
        return venue_ids

    def list_all_venue_ids(self) -> list[str]:
        """Return all venue IDs present in the geo index.

        Returns:
            List of venue IDs
        """
        pattern = VENUES_GEO_PLACE_MEMBER_FORMAT_V1.format("*")
        keys = self.client.keys(pattern)

        # Strip prefix to get raw venue IDs
        prefix = "venues_geo_place_v1:"
        venue_ids = [key.replace(prefix, "", 1) for key in keys]
        return venue_ids

    def list_all_venues(self) -> list[Venue]:
        """Return all venues from the geo index.

        Returns:
            List of Venue objects
        """
        pattern = VENUES_GEO_PLACE_MEMBER_FORMAT_V1.format("*")
        keys = self.client.keys(pattern)

        venues = []
        for key in keys:
            try:
                json_str = self.client.get(key)
                if json_str:
                    venue = Venue.model_validate_json(json_str)
                    venues.append(venue)
            except Exception as e:
                logger.error(f"Failed to parse venue from key {key}: {e}")
                continue

        return venues

    def set_week_raw_forecast(self, venue_id: str, day: WeekRawDay) -> None:
        """Cache a single day's raw weekly forecast for a venue.

        Args:
            venue_id: Venue identifier
            day: WeekRawDay object containing forecast for one day
        """
        key = WEEKLY_FORECAST_KEY_FORMAT.format(venue_id, day.day_int)
        json_data = day.model_dump_json(by_alias=True)
        self.client.set(key, json_data)

    def get_week_raw_forecast(self, venue_id: str, day_int: int) -> Optional[WeekRawDay]:
        """Retrieve cached raw weekly forecast for a venue and day.

        Args:
            venue_id: Venue identifier
            day_int: Day of week (0=Monday to 6=Sunday)

        Returns:
            WeekRawDay or None if not found
        """
        key = WEEKLY_FORECAST_KEY_FORMAT.format(venue_id, day_int)
        try:
            json_str = self.client.get(key)
            if json_str is None:
                return None  # Cache miss
            return WeekRawDay.model_validate_json(json_str)
        except redis.RedisError as e:
            # Check if it's a "key not found" error
            if "nil" in str(e).lower():
                return None
            logger.error(f"Failed to get weekly raw forecast from Redis: {e}")
            return None
