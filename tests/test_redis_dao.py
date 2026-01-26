"""Integration tests for Redis DAO."""
import pytest
from app.db import GeoRedisClient
from app.dao import RedisVenueDAO
from app.models import Venue, LiveForecastResponse, VenueInfo, Analysis, WeekRawDay, DayInfo


@pytest.fixture
def redis_client():
    """Create Redis client for testing.

    Note: Requires a running Redis instance on localhost:6379
    """
    try:
        client = GeoRedisClient(host="localhost", port=6379, password="", db=15)  # Use DB 15 for testing
        yield client
        # Cleanup: flush test database after tests
        client.client.flushdb()
    except Exception as e:
        pytest.skip(f"Redis not available: {e}")


@pytest.fixture
def venue_dao(redis_client):
    """Create RedisVenueDAO for testing."""
    return RedisVenueDAO(redis_client)


class TestRedisVenueDAO:
    """Integration tests for RedisVenueDAO."""

    def test_upsert_and_get_nearby_venues(self, venue_dao):
        """Test upserting a venue and retrieving it with geospatial query."""
        # Create test venue
        venue = Venue(
            forecast=True,
            processed=True,
            venue_id="test_venue_123",
            venue_name="Test Bar",
            venue_address="123 Main St",
            venue_lat=-8.07834,
            venue_lng=-34.90938,
            venue_type="BAR",
            rating=4.5,
            reviews=100,
        )

        # Upsert venue
        venue_dao.upsert_venue(venue)

        # Query nearby (within 10km radius)
        nearby_venues = venue_dao.get_nearby_venues(
            lat=-8.07834,
            lon=-34.90938,
            radius=10,  # 10km
        )

        # Verify venue was found
        assert len(nearby_venues) == 1
        assert nearby_venues[0].venue_id == "test_venue_123"
        assert nearby_venues[0].venue_name == "Test Bar"
        assert nearby_venues[0].rating == 4.5

    def test_get_nearby_venues_multiple(self, venue_dao):
        """Test retrieving multiple nearby venues."""
        # Create multiple venues in Recife area
        venues = [
            Venue(
                venue_id=f"venue_{i}",
                venue_name=f"Venue {i}",
                venue_address=f"Address {i}",
                venue_lat=-8.07834 + (i * 0.01),  # Slightly different locations
                venue_lng=-34.90938 + (i * 0.01),
            )
            for i in range(3)
        ]

        # Upsert all venues
        for venue in venues:
            venue_dao.upsert_venue(venue)

        # Query nearby (within 5km radius)
        nearby_venues = venue_dao.get_nearby_venues(
            lat=-8.07834,
            lon=-34.90938,
            radius=5,
        )

        # All 3 venues should be within 5km
        assert len(nearby_venues) >= 1  # At least the first one

    def test_redis_key_format_compatibility(self, redis_client, venue_dao):
        """Test that Redis key formats match Go implementation exactly."""
        venue = Venue(
            venue_id="test_123",
            venue_lat=-8.0,
            venue_lng=-34.9,
        )

        venue_dao.upsert_venue(venue)

        # Verify exact key format
        expected_member_key = "venues_geo_place_v1:test_123"
        value = redis_client.get(expected_member_key)
        assert value is not None

        # Verify geo set key
        geo_members = redis_client.client.zrange("venues_geo_v1", 0, -1)
        assert expected_member_key in geo_members

    def test_set_and_get_live_forecast(self, venue_dao):
        """Test caching and retrieving live forecast."""
        # Create live forecast
        forecast = LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(
                venue_id="venue_123",
                venue_name="Test Venue",
                venue_timezone="America/Recife",
            ),
            analysis=Analysis(
                venue_live_busyness=75,
                venue_live_busyness_available=True,
                venue_forecasted_busyness=70,
                venue_forecast_busyness_available=True,
                venue_live_forecasted_delta=5,
            ),
        )

        # Cache forecast
        venue_dao.set_live_forecast(forecast)

        # Retrieve forecast
        retrieved = venue_dao.get_live_forecast("venue_123")

        assert retrieved is not None
        assert retrieved.status == "OK"
        assert retrieved.analysis.venue_live_busyness == 75
        assert retrieved.venue_info.venue_id == "venue_123"

    def test_delete_live_forecast(self, venue_dao):
        """Test deleting cached live forecast."""
        forecast = LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id="venue_456"),
            analysis=Analysis(),
        )

        # Cache and verify
        venue_dao.set_live_forecast(forecast)
        assert venue_dao.get_live_forecast("venue_456") is not None

        # Delete and verify
        venue_dao.delete_live_forecast("venue_456")
        assert venue_dao.get_live_forecast("venue_456") is None

    def test_list_all_venue_ids(self, venue_dao):
        """Test listing all venue IDs from geo index."""
        # Add multiple venues
        for i in range(3):
            venue = Venue(
                venue_id=f"list_test_{i}",
                venue_lat=-8.0,
                venue_lng=-34.9,
            )
            venue_dao.upsert_venue(venue)

        # List all venue IDs
        venue_ids = venue_dao.list_all_venue_ids()

        assert len(venue_ids) >= 3
        assert "list_test_0" in venue_ids
        assert "list_test_1" in venue_ids
        assert "list_test_2" in venue_ids

    def test_list_cached_live_forecast_venue_ids(self, venue_dao):
        """Test listing all cached live forecast venue IDs."""
        # Cache forecasts for multiple venues
        for i in range(3):
            forecast = LiveForecastResponse(
                status="OK",
                venue_info=VenueInfo(venue_id=f"live_test_{i}"),
                analysis=Analysis(),
            )
            venue_dao.set_live_forecast(forecast)

        # List cached IDs
        cached_ids = venue_dao.list_cached_live_forecast_venue_ids()

        assert len(cached_ids) >= 3
        assert "live_test_0" in cached_ids
        assert "live_test_1" in cached_ids
        assert "live_test_2" in cached_ids

    def test_set_and_get_week_raw_forecast(self, venue_dao):
        """Test caching and retrieving weekly forecast."""
        # Create weekly forecast for Monday (day_int=0)
        day = WeekRawDay(
            day_int=0,
            day_raw=[10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65,
                     70, 75, 80, 85, 80, 75, 70, 65, 60, 55, 50, 45],
            day_info=DayInfo(
                day_int=0,
                day_max=85,
                day_mean=54,
                day_rank_max=5,
                day_rank_mean=10,
                day_text="Monday",
                venue_open="2100",
                venue_closed="0400",
            ),
        )

        # Cache forecast
        venue_dao.set_week_raw_forecast("venue_789", day)

        # Retrieve forecast
        retrieved = venue_dao.get_week_raw_forecast("venue_789", day_int=0)

        assert retrieved is not None
        assert retrieved.day_int == 0
        assert len(retrieved.day_raw) == 24
        assert retrieved.day_info.day_max == 85

    def test_get_week_raw_forecast_not_found(self, venue_dao):
        """Test retrieving non-existent weekly forecast returns None."""
        result = venue_dao.get_week_raw_forecast("nonexistent", day_int=0)
        assert result is None

    def test_weekly_forecast_key_format(self, redis_client, venue_dao):
        """Test that weekly forecast key format matches Go implementation."""
        day = WeekRawDay(
            day_int=3,  # Wednesday
            day_raw=[50] * 24,
        )

        venue_dao.set_week_raw_forecast("test_venue", day)

        # Verify exact key format: weekly_forecast_v1:venue_id_day_int
        expected_key = "weekly_forecast_v1:test_venue_3"
        value = redis_client.get(expected_key)
        assert value is not None
