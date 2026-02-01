"""Unit tests for handlers."""
import pytest
from unittest.mock import Mock, patch
from datetime import datetime

import pytz

from app.handlers import VenueHandler
from app.models import (
    Venue,
    VenueWithLive,
    MinifiedVenue,
    LiveForecastResponse,
    VenueInfo,
    Analysis,
    WeekRawDay,
)


@pytest.fixture
def mock_venue_dao():
    """Create mock venue DAO."""
    return Mock()


@pytest.fixture
def venue_handler(mock_venue_dao):
    """Create VenueHandler with mocked DAO."""
    return VenueHandler(mock_venue_dao)


class TestVenueHandler:
    """Test VenueHandler critical business logic."""

    def test_ping(self, venue_handler):
        """Test ping health check."""
        result = venue_handler.ping()
        assert result == {"status": "pong"}

    def test_get_venues_nearby_delegates_to_dao(self, venue_handler, mock_venue_dao):
        """Test that get_venues_nearby delegates to DAO."""
        mock_venue_dao.get_nearby_venues.return_value = [
            Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        ]
        mock_venue_dao.get_live_forecast.return_value = None
        mock_venue_dao.get_week_raw_forecast.return_value = None

        result = venue_handler.get_venues_nearby(lat=-8.0, lon=-34.9, radius=5.0)

        assert len(result) == 1
        mock_venue_dao.get_nearby_venues.assert_called_once_with(-8.0, -34.9, 5.0)

    def test_sorting_venues_with_live_first(self, venue_handler, mock_venue_dao):
        """Test CRITICAL sorting logic - venues with live data come first."""
        # Create venues: one with live, one without
        v1 = Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9, venue_name="Bar A")
        v2 = Venue(venue_id="v2", venue_lat=-8.01, venue_lng=-34.91, venue_name="Bar B")

        mock_venue_dao.get_nearby_venues.return_value = [v1, v2]

        # v2 has live forecast, v1 doesn't
        def get_live_side_effect(venue_id):
            if venue_id == "v2":
                return LiveForecastResponse(
                    status="OK",
                    venue_info=VenueInfo(venue_id="v2"),
                    analysis=Analysis(
                        venue_live_busyness=75, venue_live_busyness_available=True
                    ),
                )
            return None

        mock_venue_dao.get_live_forecast.side_effect = get_live_side_effect
        mock_venue_dao.get_week_raw_forecast.return_value = None

        result = venue_handler.get_venues_nearby(
            lat=-8.0, lon=-34.9, radius=5.0, verbose=True
        )

        # v2 should come first (has live data)
        assert len(result) == 2
        assert result[0].venue.venue_id == "v2"
        assert result[1].venue.venue_id == "v1"

    def test_sorting_by_live_busyness_descending(self, venue_handler, mock_venue_dao):
        """Test CRITICAL sorting logic - venues sorted by busyness descending."""
        v1 = Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        v2 = Venue(venue_id="v2", venue_lat=-8.01, venue_lng=-34.91)
        v3 = Venue(venue_id="v3", venue_lat=-8.02, venue_lng=-34.92)

        mock_venue_dao.get_nearby_venues.return_value = [v1, v2, v3]

        # All have live, with different busyness: v1=50, v2=100, v3=75
        def get_live_side_effect(venue_id):
            busyness = {"v1": 50, "v2": 100, "v3": 75}[venue_id]
            return LiveForecastResponse(
                status="OK",
                venue_info=VenueInfo(venue_id=venue_id),
                analysis=Analysis(
                    venue_live_busyness=busyness, venue_live_busyness_available=True
                ),
            )

        mock_venue_dao.get_live_forecast.side_effect = get_live_side_effect
        mock_venue_dao.get_week_raw_forecast.return_value = None

        result = venue_handler.get_venues_nearby(
            lat=-8.0, lon=-34.9, radius=5.0, verbose=True
        )

        # Should be sorted by busyness descending: v2(100), v3(75), v1(50)
        assert len(result) == 3
        assert result[0].venue.venue_id == "v2"
        assert result[1].venue.venue_id == "v3"
        assert result[2].venue.venue_id == "v1"

    def test_sorting_mixed_live_and_no_live(self, venue_handler, mock_venue_dao):
        """Test CRITICAL sorting - live venues first, then no-live venues."""
        v1 = Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        v2 = Venue(venue_id="v2", venue_lat=-8.01, venue_lng=-34.91)
        v3 = Venue(venue_id="v3", venue_lat=-8.02, venue_lng=-34.92)
        v4 = Venue(venue_id="v4", venue_lat=-8.03, venue_lng=-34.93)

        mock_venue_dao.get_nearby_venues.return_value = [v1, v2, v3, v4]

        # v2 and v4 have live (100, 75), v1 and v3 don't
        def get_live_side_effect(venue_id):
            if venue_id in ["v2", "v4"]:
                busyness = {"v2": 100, "v4": 75}[venue_id]
                return LiveForecastResponse(
                    status="OK",
                    venue_info=VenueInfo(venue_id=venue_id),
                    analysis=Analysis(
                        venue_live_busyness=busyness, venue_live_busyness_available=True
                    ),
                )
            return None

        mock_venue_dao.get_live_forecast.side_effect = get_live_side_effect
        mock_venue_dao.get_week_raw_forecast.return_value = None

        result = venue_handler.get_venues_nearby(
            lat=-8.0, lon=-34.9, radius=5.0, verbose=True
        )

        # Should be: v2(100), v4(75), v1(no live), v3(no live)
        assert len(result) == 4
        assert result[0].venue.venue_id == "v2"
        assert result[1].venue.venue_id == "v4"
        assert result[2].venue.venue_id == "v1"
        assert result[3].venue.venue_id == "v3"

    @patch("app.handlers.venue_handler.datetime")
    def test_day_conversion_monday(
        self, mock_datetime, venue_handler, mock_venue_dao
    ):
        """Test CRITICAL day conversion - Monday (Python weekday=0 -> BestTime day_int=0)."""
        v1 = Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        mock_venue_dao.get_nearby_venues.return_value = [v1]
        mock_venue_dao.get_live_forecast.return_value = None

        # Mock Monday in Recife timezone (January 26, 2026 is a Monday)
        recife_tz = pytz.timezone("America/Recife")
        mock_recife_time = datetime(2026, 1, 26, 12, 0, 0, tzinfo=recife_tz)  # Monday
        mock_datetime.now.return_value = mock_recife_time

        # Track calls to get_week_raw_forecast
        mock_venue_dao.get_week_raw_forecast.return_value = WeekRawDay(
            day_int=0, day_raw=[50] * 24
        )

        venue_handler.get_venues_nearby(lat=-8.0, lon=-34.9, radius=5.0)

        # Should request day_int=0 (Monday)
        mock_venue_dao.get_week_raw_forecast.assert_called_once_with("v1", 0)

    @patch("app.handlers.venue_handler.datetime")
    def test_day_conversion_sunday(
        self, mock_datetime, venue_handler, mock_venue_dao
    ):
        """Test CRITICAL day conversion - Sunday (Python weekday=6 -> BestTime day_int=6)."""
        v1 = Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        mock_venue_dao.get_nearby_venues.return_value = [v1]
        mock_venue_dao.get_live_forecast.return_value = None

        # Mock Sunday in Recife timezone
        recife_tz = pytz.timezone("America/Recife")
        mock_recife_time = datetime(2026, 2, 1, 12, 0, 0, tzinfo=recife_tz)  # Sunday
        mock_datetime.now.return_value = mock_recife_time

        mock_venue_dao.get_week_raw_forecast.return_value = WeekRawDay(
            day_int=6, day_raw=[50] * 24
        )

        venue_handler.get_venues_nearby(lat=-8.0, lon=-34.9, radius=5.0)

        # Should request day_int=6 (Sunday)
        mock_venue_dao.get_week_raw_forecast.assert_called_once_with("v1", 6)

    def test_verbose_mode_returns_full_structure(self, venue_handler, mock_venue_dao):
        """Test verbose=True returns full VenueWithLive."""
        v1 = Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        mock_venue_dao.get_nearby_venues.return_value = [v1]
        mock_venue_dao.get_live_forecast.return_value = None
        mock_venue_dao.get_week_raw_forecast.return_value = None

        result = venue_handler.get_venues_nearby(
            lat=-8.0, lon=-34.9, radius=5.0, verbose=True
        )

        assert len(result) == 1
        assert isinstance(result[0], VenueWithLive)
        assert result[0].venue.venue_id == "v1"

    def test_minified_mode_returns_essential_fields(
        self, venue_handler, mock_venue_dao
    ):
        """Test verbose=False returns MinifiedVenue with essential fields."""
        v1 = Venue(
            venue_id="v1",
            venue_lat=-8.0,
            venue_lng=-34.9,
            venue_name="Test Bar",
            venue_address="123 Main St",
            rating=4.5,
            price_level=2,
        )
        mock_venue_dao.get_nearby_venues.return_value = [v1]
        mock_venue_dao.get_live_forecast.return_value = LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(
                venue_live_busyness=75, venue_live_busyness_available=True
            ),
        )
        mock_venue_dao.get_week_raw_forecast.return_value = None

        result = venue_handler.get_venues_nearby(
            lat=-8.0, lon=-34.9, radius=5.0, verbose=False
        )

        assert len(result) == 1
        assert isinstance(result[0], MinifiedVenue)
        assert result[0].venue_name == "Test Bar"
        assert result[0].venue_live_busyness == 75
        assert result[0].rating == 4.5

    def test_minified_mode_omits_unavailable_live_busyness(
        self, venue_handler, mock_venue_dao
    ):
        """Test minified mode omits live_busyness when not available."""
        v1 = Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        mock_venue_dao.get_nearby_venues.return_value = [v1]

        # Live forecast exists but not available
        mock_venue_dao.get_live_forecast.return_value = LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(
                venue_live_busyness=0, venue_live_busyness_available=False
            ),
        )
        mock_venue_dao.get_week_raw_forecast.return_value = None

        result = venue_handler.get_venues_nearby(
            lat=-8.0, lon=-34.9, radius=5.0, verbose=False
        )

        assert len(result) == 1
        assert result[0].venue_live_busyness is None

    def test_weekly_forecast_included_in_response(
        self, venue_handler, mock_venue_dao
    ):
        """Test weekly forecast is included when available."""
        v1 = Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        mock_venue_dao.get_nearby_venues.return_value = [v1]
        mock_venue_dao.get_live_forecast.return_value = None

        weekly = WeekRawDay(day_int=3, day_raw=[50] * 24)
        mock_venue_dao.get_week_raw_forecast.return_value = weekly

        result = venue_handler.get_venues_nearby(
            lat=-8.0, lon=-34.9, radius=5.0, verbose=True
        )

        assert len(result) == 1
        assert result[0].weekly_forecast is not None
        assert result[0].weekly_forecast.day_int == 3

    def test_missing_live_forecast_does_not_crash(
        self, venue_handler, mock_venue_dao
    ):
        """Test that missing live forecast doesn't cause errors."""
        v1 = Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        mock_venue_dao.get_nearby_venues.return_value = [v1]
        mock_venue_dao.get_live_forecast.side_effect = Exception("Not found")
        mock_venue_dao.get_week_raw_forecast.return_value = None

        result = venue_handler.get_venues_nearby(
            lat=-8.0, lon=-34.9, radius=5.0, verbose=True
        )

        # Should still return venue with None live_forecast
        assert len(result) == 1
        assert result[0].live_forecast is None

    def test_missing_weekly_forecast_does_not_crash(
        self, venue_handler, mock_venue_dao
    ):
        """Test that missing weekly forecast doesn't cause errors."""
        v1 = Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        mock_venue_dao.get_nearby_venues.return_value = [v1]
        mock_venue_dao.get_live_forecast.return_value = None
        mock_venue_dao.get_week_raw_forecast.side_effect = Exception("Not found")

        result = venue_handler.get_venues_nearby(
            lat=-8.0, lon=-34.9, radius=5.0, verbose=True
        )

        # Should still return venue with None weekly_forecast
        assert len(result) == 1
        assert result[0].weekly_forecast is None
