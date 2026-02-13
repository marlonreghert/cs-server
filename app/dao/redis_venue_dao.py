"""Redis-based Data Access Object for venue operations."""
import json
import logging
from typing import Optional
import redis

from app.db.geo_redis_client import GeoRedisClient
from app.models import Venue, LiveForecastResponse, WeekRawDay
from app.models.vibe_attributes import VibeAttributes
from app.models.opening_hours import OpeningHours
from app.models.instagram import VenueInstagram

logger = logging.getLogger(__name__)

# CRITICAL: These key formats must match exactly with Go implementation for backward compatibility
VENUES_GEO_KEY_V1 = "venues_geo_v1"
VENUES_GEO_PLACE_MEMBER_FORMAT_V1 = "venues_geo_place_v1:{}"
LIVE_FORECAST_KEY_FORMAT = "live_forecast_v1:{}"
WEEKLY_FORECAST_KEY_FORMAT = "weekly_forecast_v1:{}_{}"
VIBE_ATTRIBUTES_KEY_FORMAT = "vibe_attributes_v1:{}"
VENUE_PHOTOS_KEY_FORMAT = "venue_photos_v1:{}"
OPENING_HOURS_KEY_FORMAT = "opening_hours_v1:{}"
VENUE_INSTAGRAM_KEY_FORMAT = "venue_instagram_v1:{}"


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

    def get_venue(self, venue_id: str) -> Optional[Venue]:
        """Retrieve a venue by its ID.

        Args:
            venue_id: Venue identifier

        Returns:
            Venue object or None if not found
        """
        venue_key = VENUES_GEO_PLACE_MEMBER_FORMAT_V1.format(venue_id)
        try:
            json_str = self.client.get(venue_key)
            if json_str is None:
                return None
            return Venue.model_validate_json(json_str)
        except Exception as e:
            logger.error(f"Failed to get venue {venue_id}: {e}")
            return None

    def delete_venue(self, venue_id: str) -> bool:
        """Delete a venue and all its associated data from Redis.

        This removes:
        - The venue from the geo index
        - The venue JSON data
        - Any cached live forecast
        - Any cached weekly forecasts (all 7 days)
        - Any cached vibe attributes
        - Any cached photos

        Args:
            venue_id: Venue identifier

        Returns:
            True if venue was deleted, False if not found
        """
        venue_key = VENUES_GEO_PLACE_MEMBER_FORMAT_V1.format(venue_id)

        # Check if venue exists first
        if self.client.get(venue_key) is None:
            logger.warning(f"[RedisVenueDAO] Venue {venue_id} not found, nothing to delete")
            return False

        try:
            # Remove from geo index
            self.client.zrem(VENUES_GEO_KEY_V1, venue_key)

            # Remove venue JSON data
            self.client.del_(venue_key)

            # Remove associated data
            self.delete_live_forecast(venue_id)
            self.delete_vibe_attributes(venue_id)

            # Remove weekly forecasts for all 7 days
            for day_int in range(7):
                weekly_key = WEEKLY_FORECAST_KEY_FORMAT.format(venue_id, day_int)
                self.client.del_(weekly_key)

            # Remove photos
            photos_key = VENUE_PHOTOS_KEY_FORMAT.format(venue_id)
            self.client.del_(photos_key)

            # Remove opening hours
            self.delete_opening_hours(venue_id)

            # Remove Instagram cache
            ig_key = VENUE_INSTAGRAM_KEY_FORMAT.format(venue_id)
            self.client.del_(ig_key)

            logger.info(f"[RedisVenueDAO] Deleted venue {venue_id} and all associated data")
            return True

        except Exception as e:
            logger.error(f"[RedisVenueDAO] Failed to delete venue {venue_id}: {e}")
            return False

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

    # =========================================================================
    # VIBE ATTRIBUTES METHODS
    # =========================================================================

    def set_vibe_attributes(self, vibe_attrs: VibeAttributes) -> None:
        """Cache vibe attributes for a venue.

        Args:
            vibe_attrs: VibeAttributes object
        """
        key = VIBE_ATTRIBUTES_KEY_FORMAT.format(vibe_attrs.venue_id)
        json_data = vibe_attrs.model_dump_json(by_alias=True)
        self.client.set(key, json_data)
        logger.debug(f"[RedisVenueDAO] Cached vibe attributes for {vibe_attrs.venue_id}")

    def get_vibe_attributes(self, venue_id: str) -> Optional[VibeAttributes]:
        """Retrieve cached vibe attributes for a venue.

        Args:
            venue_id: Venue identifier

        Returns:
            VibeAttributes or None if not found
        """
        key = VIBE_ATTRIBUTES_KEY_FORMAT.format(venue_id)
        try:
            json_str = self.client.get(key)
            if json_str is None:
                return None
            return VibeAttributes.model_validate_json(json_str)
        except redis.RedisError as e:
            logger.error(f"Failed to get vibe attributes from Redis: {e}")
            return None

    def delete_vibe_attributes(self, venue_id: str) -> None:
        """Delete cached vibe attributes for a venue.

        Args:
            venue_id: Venue identifier
        """
        key = VIBE_ATTRIBUTES_KEY_FORMAT.format(venue_id)
        self.client.del_(key)
        logger.info(f"[RedisVenueDAO] Deleted vibe attributes cache for {venue_id}")

    def list_cached_vibe_attributes_venue_ids(self) -> list[str]:
        """Return venue IDs for all cached vibe attributes.

        Returns:
            List of venue IDs
        """
        pattern = "vibe_attributes_v1:*"
        keys = self.client.keys(pattern)

        # Strip prefix to get raw venue IDs
        prefix = "vibe_attributes_v1:"
        venue_ids = [key.replace(prefix, "", 1) for key in keys]
        return venue_ids

    def count_venues_with_vibe_attributes(self) -> int:
        """Count venues with cached vibe attributes.

        Returns:
            Number of venues with vibe attributes
        """
        pattern = "vibe_attributes_v1:*"
        keys = self.client.keys(pattern)
        return len(keys)

    # =========================================================================
    # VENUE PHOTOS METHODS
    # =========================================================================

    def set_venue_photos(self, venue_id: str, photo_urls: list[str]) -> None:
        """Cache photo URLs for a venue.

        Args:
            venue_id: Venue identifier
            photo_urls: List of photo URLs
        """
        key = VENUE_PHOTOS_KEY_FORMAT.format(venue_id)
        json_data = json.dumps(photo_urls)
        self.client.set(key, json_data)
        logger.debug(f"[RedisVenueDAO] Cached {len(photo_urls)} photos for {venue_id}")

    def get_venue_photos(self, venue_id: str) -> Optional[list[str]]:
        """Retrieve cached photo URLs for a venue.

        Args:
            venue_id: Venue identifier

        Returns:
            List of photo URLs or None if not found
        """
        key = VENUE_PHOTOS_KEY_FORMAT.format(venue_id)
        try:
            json_str = self.client.get(key)
            if json_str is None:
                return None
            return json.loads(json_str)
        except redis.RedisError as e:
            logger.error(f"Failed to get venue photos from Redis: {e}")
            return None

    def list_cached_venue_photos_ids(self) -> list[str]:
        """Return venue IDs for all cached venue photos.

        Returns:
            List of venue IDs
        """
        pattern = "venue_photos_v1:*"
        keys = self.client.keys(pattern)

        # Strip prefix to get raw venue IDs
        prefix = "venue_photos_v1:"
        venue_ids = [key.replace(prefix, "", 1) for key in keys]
        return venue_ids

    def count_venues_with_photos(self) -> int:
        """Count venues with cached photos.

        Returns:
            Number of venues with photos
        """
        pattern = "venue_photos_v1:*"
        keys = self.client.keys(pattern)
        return len(keys)

    # =========================================================================
    # OPENING HOURS METHODS
    # =========================================================================

    def set_opening_hours(self, opening_hours: OpeningHours) -> None:
        """Cache opening hours for a venue.

        Args:
            opening_hours: OpeningHours object
        """
        key = OPENING_HOURS_KEY_FORMAT.format(opening_hours.venue_id)
        json_data = opening_hours.model_dump_json(by_alias=True)
        self.client.set(key, json_data)
        logger.debug(f"[RedisVenueDAO] Cached opening hours for {opening_hours.venue_id}")

    def get_opening_hours(self, venue_id: str) -> Optional[OpeningHours]:
        """Retrieve cached opening hours for a venue.

        Args:
            venue_id: Venue identifier

        Returns:
            OpeningHours or None if not found
        """
        key = OPENING_HOURS_KEY_FORMAT.format(venue_id)
        try:
            json_str = self.client.get(key)
            if json_str is None:
                return None
            return OpeningHours.model_validate_json(json_str)
        except redis.RedisError as e:
            logger.error(f"Failed to get opening hours from Redis: {e}")
            return None

    def delete_opening_hours(self, venue_id: str) -> None:
        """Delete cached opening hours for a venue.

        Args:
            venue_id: Venue identifier
        """
        key = OPENING_HOURS_KEY_FORMAT.format(venue_id)
        self.client.del_(key)
        logger.info(f"[RedisVenueDAO] Deleted opening hours cache for {venue_id}")

    # =========================================================================
    # VENUE INSTAGRAM METHODS
    # =========================================================================

    def set_venue_instagram(
        self, instagram: VenueInstagram, cache_ttl_days: int = 30, not_found_ttl_days: int = 7
    ) -> None:
        """Cache Instagram discovery result for a venue with TTL.

        Args:
            instagram: VenueInstagram result to cache
            cache_ttl_days: TTL in days for found results
            not_found_ttl_days: TTL in days for not_found results
        """
        key = VENUE_INSTAGRAM_KEY_FORMAT.format(instagram.venue_id)
        json_data = instagram.model_dump_json(by_alias=True)

        if instagram.status == "not_found":
            ttl_seconds = not_found_ttl_days * 86400
        else:
            ttl_seconds = cache_ttl_days * 86400

        self.client.setex(key, ttl_seconds, json_data)
        logger.debug(
            f"[RedisVenueDAO] Cached Instagram for {instagram.venue_id}: "
            f"status={instagram.status}, ttl={ttl_seconds}s"
        )

    def get_venue_instagram(self, venue_id: str) -> Optional[VenueInstagram]:
        """Retrieve cached Instagram data for a venue.

        Args:
            venue_id: Venue identifier

        Returns:
            VenueInstagram or None if not cached / expired
        """
        key = VENUE_INSTAGRAM_KEY_FORMAT.format(venue_id)
        try:
            json_str = self.client.get(key)
            if json_str is None:
                return None
            return VenueInstagram.model_validate_json(json_str)
        except redis.RedisError as e:
            logger.error(f"Failed to get venue Instagram from Redis: {e}")
            return None

    def delete_venue_instagram(self, venue_id: str) -> None:
        """Delete cached Instagram data for a venue.

        Args:
            venue_id: Venue identifier
        """
        key = VENUE_INSTAGRAM_KEY_FORMAT.format(venue_id)
        self.client.del_(key)
        logger.info(f"[RedisVenueDAO] Deleted Instagram cache for {venue_id}")

    def count_venues_with_instagram(self) -> int:
        """Count venues with cached Instagram results (found or low_confidence).

        Returns:
            Number of venues with Instagram handles
        """
        pattern = "venue_instagram_v1:*"
        keys = self.client.keys(pattern)
        count = 0
        for key in keys:
            try:
                json_str = self.client.get(key)
                if json_str:
                    data = VenueInstagram.model_validate_json(json_str)
                    if data.has_instagram():
                        count += 1
            except Exception:
                continue
        return count
