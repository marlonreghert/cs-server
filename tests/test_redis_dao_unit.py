"""Unit tests for Redis DAO (mocked, no real Redis needed)."""
import pytest
from unittest.mock import Mock, MagicMock
from app.dao import RedisVenueDAO
from app.models import Venue, LiveForecastResponse, VenueInfo, Analysis, WeekRawDay


class TestRedisVenueDAOUnit:
    """Unit tests for RedisVenueDAO with mocked Redis client."""

    @pytest.fixture
    def mock_redis_client(self):
        """Create a mock Redis client."""
        return Mock()

    @pytest.fixture
    def venue_dao(self, mock_redis_client):
        """Create RedisVenueDAO with mocked client."""
        return RedisVenueDAO(mock_redis_client)

    def test_upsert_venue_calls_redis_correctly(self, venue_dao, mock_redis_client):
        """Test that upsert_venue calls Redis with correct parameters."""
        venue = Venue(
            venue_id="test_123",
            venue_lat=-8.07834,
            venue_lng=-34.90938,
            venue_name="Test Venue",
        )

        venue_dao.upsert_venue(venue)

        # Verify add_location_with_json was called with correct parameters
        mock_redis_client.add_location_with_json.assert_called_once()
        call_args = mock_redis_client.add_location_with_json.call_args

        assert call_args.kwargs["geo_key"] == "venues_geo_v1"
        assert call_args.kwargs["member_key"] == "venues_geo_place_v1:test_123"
        assert call_args.kwargs["lat"] == -8.07834
        assert call_args.kwargs["lon"] == -34.90938
        assert call_args.kwargs["data"] == venue

    def test_get_nearby_venues(self, venue_dao, mock_redis_client):
        """Test get_nearby_venues deserializes venues correctly."""
        # Mock Redis response
        venue_json = """{"forecast": true, "processed": true, "venue_id": "v1",
                         "venue_lat": -8.0, "venue_lng": -34.9, "venue_name": "Test"}"""
        mock_redis_client.get_locations_within_radius.return_value = [venue_json]

        venues = venue_dao.get_nearby_venues(lat=-8.0, lon=-34.9, radius=5.0)

        assert len(venues) == 1
        assert venues[0].venue_id == "v1"
        assert venues[0].venue_name == "Test"

        # Verify correct call to Redis
        mock_redis_client.get_locations_within_radius.assert_called_once_with(
            "venues_geo_v1", -8.0, -34.9, 5.0
        )

    def test_set_live_forecast_uses_correct_key_format(self, venue_dao, mock_redis_client):
        """Test that live forecast uses correct Redis key format."""
        forecast = LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id="venue_abc"),
            analysis=Analysis(),
        )

        venue_dao.set_live_forecast(forecast)

        # Verify correct key format: live_forecast_v1:{venue_id}
        mock_redis_client.set.assert_called_once()
        call_args = mock_redis_client.set.call_args
        assert call_args[0][0] == "live_forecast_v1:venue_abc"

    def test_delete_live_forecast_uses_correct_key(self, venue_dao, mock_redis_client):
        """Test delete uses correct key format."""
        venue_dao.delete_live_forecast("venue_123")

        mock_redis_client.del_.assert_called_once_with("live_forecast_v1:venue_123")

    def test_list_all_venue_ids_strips_prefix(self, venue_dao, mock_redis_client):
        """Test that list_all_venue_ids strips the key prefix correctly."""
        mock_redis_client.keys.return_value = [
            "venues_geo_place_v1:id1",
            "venues_geo_place_v1:id2",
            "venues_geo_place_v1:id3",
        ]

        venue_ids = venue_dao.list_all_venue_ids()

        assert venue_ids == ["id1", "id2", "id3"]
        mock_redis_client.keys.assert_called_once_with("venues_geo_place_v1:*")

    def test_list_cached_live_forecast_venue_ids(self, venue_dao, mock_redis_client):
        """Test listing cached live forecast IDs."""
        mock_redis_client.keys.return_value = [
            "live_forecast_v1:v1",
            "live_forecast_v1:v2",
        ]

        ids = venue_dao.list_cached_live_forecast_venue_ids()

        assert ids == ["v1", "v2"]
        mock_redis_client.keys.assert_called_once_with("live_forecast_v1:*")

    def test_set_week_raw_forecast_key_format(self, venue_dao, mock_redis_client):
        """Test weekly forecast uses correct key format with day_int."""
        day = WeekRawDay(day_int=3, day_raw=[50] * 24)

        venue_dao.set_week_raw_forecast("venue_xyz", day)

        # Verify key format: weekly_forecast_v1:{venue_id}_{day_int}
        mock_redis_client.set.assert_called_once()
        call_args = mock_redis_client.set.call_args
        assert call_args[0][0] == "weekly_forecast_v1:venue_xyz_3"

    def test_get_week_raw_forecast_returns_none_when_not_found(
        self, venue_dao, mock_redis_client
    ):
        """Test that get_week_raw_forecast returns None for cache miss."""
        mock_redis_client.get.return_value = None

        result = venue_dao.get_week_raw_forecast("nonexistent", day_int=0)

        assert result is None
