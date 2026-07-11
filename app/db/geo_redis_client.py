"""Redis client with geospatial operations."""
import json
import logging
from typing import Any, Optional
import redis
from redis.commands.search.field import GeoField

logger = logging.getLogger(__name__)


class GeoRedisClient:
    """Redis client with geospatial indexing support."""

    def __init__(self, client):
        """Initialize Redis client.
        
        Args:
            client: Redis client
        """
        logging.info("Passing redis client")
        self.client = client

        # Test connection
        try:
            self.ping()
            logger.info("Connected to Redis")
        except redis.ConnectionError as e:
            logger.error(f"Could not connect to Redis: {e}")
            raise

    def set(self, key: str, value: str) -> None:
        """Set a key-value pair in Redis.

        Args:
            key: Redis key
            value: String value to store
        """
        self.client.set(key, value)

    def get(self, key: str) -> Optional[str]:
        """Get value for a given key from Redis.

        Args:
            key: Redis key

        Returns:
            String value or None if key doesn't exist
        """
        return self.client.get(key)

    def mget(self, keys: list[str]) -> list[Optional[str]]:
        """Get values for multiple keys in one round-trip (P2/P5).

        Args:
            keys: Redis keys, in the order the caller wants results back

        Returns:
            Values in the same order as `keys`; a missing key yields None at
            that position (same per-key absence semantics as `get`). Empty
            input returns an empty list without a round-trip.
        """
        if not keys:
            return []
        return self.client.mget(keys)

    def keys(self, pattern: str) -> list[str]:
        """Return all keys matching the given pattern.

        Uses SCAN (via `scan_iter`) rather than the blocking O(N) KEYS command
        (P5) — same return contract (a list of matching key strings), but
        iterated in small non-blocking batches. SCAN's cursor protocol can
        revisit a key under concurrent keyspace mutation, so results are
        de-duplicated to match KEYS' unique-list contract exactly.

        Args:
            pattern: Redis key pattern (e.g., "prefix:*")

        Returns:
            List of matching keys (unique, order not guaranteed to match KEYS)
        """
        return list(dict.fromkeys(self.client.scan_iter(match=pattern)))

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        """Set a key-value pair with expiration.

        Args:
            key: Redis key
            ttl_seconds: Time-to-live in seconds
            value: String value to store
        """
        self.client.setex(key, ttl_seconds, value)

    def del_(self, key: str) -> None:
        """Delete a key from Redis.

        Args:
            key: Redis key to delete
        """
        self.client.delete(key)

    def zrem(self, name: str, *values: str) -> int:
        """Remove members from a sorted set (including geo sets).

        Args:
            name: Redis sorted set key
            *values: Members to remove

        Returns:
            Number of members removed
        """
        return self.client.zrem(name, *values)

    def add_location_with_json(
        self,
        geo_key: str,
        member_key: str,
        lat: float,
        lon: float,
        data: Any,
    ) -> None:
        """Store geolocation with associated JSON data.

        This method:
        1. Adds the location to a geospatial index using GEOADD
        2. Stores the JSON data separately using SET

        Args:
            geo_key: Redis geo set key (e.g., "venues_geo_v1")
            member_key: Member identifier in the geo set (e.g., "venues_geo_place_v1:venue_123")
            lat: Latitude
            lon: Longitude
            data: Python object to serialize as JSON
        """
        # Serialize data to JSON
        if hasattr(data, "model_dump"):
            # Pydantic model
            json_data = data.model_dump_json(by_alias=True)
        else:
            json_data = json.dumps(data)

        # Store geolocation using GEOADD
        # Note: Redis GEOADD expects (longitude, latitude) order
        self.client.geoadd(geo_key, (lon, lat, member_key))

        # Store JSON data associated with the member
        self.client.set(member_key, json_data)

        logger.debug(f"Added geolocation and JSON for member: {member_key}")

    def get_locations_within_radius(
        self,
        key: str,
        lat: float,
        lon: float,
        radius: float,
    ) -> list[str]:
        """Find all locations within the given radius and return their JSON data.

        Args:
            key: Redis geo set key
            lat: Center latitude
            lon: Center longitude
            radius: Radius in kilometers

        Returns:
            List of JSON strings for matching locations
        """
        logger.debug(f"Reading from radius with key: {key}")

        # GEORADIUS expects (longitude, latitude) order
        # radius is in kilometers
        results = self.client.georadius(
            key,
            longitude=lon,
            latitude=lat,
            radius=radius,
            unit="km",
            withcoord=False,
            withdist=False,
            withhash=False,
        )

        if not results:
            return []

        # P2: one MGET for every member's JSON instead of a GET-per-member loop.
        # A total MGET failure degrades to "no data" for this radius (never a
        # 500), the aggregate of what a connection error would have done to
        # every per-member GET in the old loop.
        try:
            values = self.client.mget(results)
        except redis.RedisError as e:
            logger.warning(f"Bulk get for {len(results)} members failed: {e}")
            return []

        objects = []
        for member_name, data in zip(results, values):
            if data:
                logger.debug(f"Read: {data}")
                objects.append(data)

        return objects

    def ping(self) -> bool:
        """Check connectivity to Redis.

        Returns:
            True if connected

        Raises:
            redis.ConnectionError if connection fails
        """
        return self.client.ping()
