"""Unit tests for Pydantic data models."""
import json
import pytest
from app.models import (
    Venue,
    FootTrafficForecast,
    DayInfo,
    DayInfoV2,
    OpenCloseDetail,
    LiveForecastResponse,
    VenueInfo,
    Analysis,
    WeekRawResponse,
    WeekRawDay,
)


class TestVenueModels:
    """Test venue-related models."""

    def test_venue_basic_serialization(self):
        """Test basic Venue model creation and serialization."""
        venue = Venue(
            forecast=True,
            processed=True,
            venue_address="123 Main St",
            venue_lat=-8.07834,
            venue_lng=-34.90938,
            venue_name="Test Venue",
            venue_id="venue_123",
        )

        assert venue.venue_name == "Test Venue"
        assert venue.venue_lat == -8.07834
        assert venue.venue_lng == -34.90938

        # Test serialization
        json_data = venue.model_dump(by_alias=True)
        assert json_data["venue_lng"] == -34.90938  # Uses alias

    def test_venue_optional_fields(self):
        """Test venue with optional fields."""
        venue = Venue(
            venue_lat=-8.0,
            venue_lng=-34.9,
            venue_type="BAR",
            price_level=2,
            rating=4.5,
            reviews=150,
        )

        assert venue.venue_type == "BAR"
        assert venue.price_level == 2
        assert venue.rating == 4.5

    def test_venue_to_string(self):
        """Test __str__ method."""
        venue = Venue(
            venue_name="Test Bar",
            venue_address="Rua A",
            venue_lat=-8.0,
            venue_lng=-34.9,
        )

        str_repr = str(venue)
        assert "Test Bar" in str_repr
        assert "Rua A" in str_repr

    def test_day_info_venue_open_closed_string_conversion(self):
        """Test DayInfo converts int to string for venue_open/venue_closed."""
        # Test with integer input
        day_info_json = {
            "day_int": 0,
            "day_max": 85,
            "day_mean": 42,
            "day_rank_max": 5,
            "day_rank_mean": 10,
            "day_text": "Monday",
            "venue_open": 2100,  # Integer
            "venue_closed": 400,  # Integer
        }

        day_info = DayInfo(**day_info_json)
        assert day_info.venue_open == "2100"
        assert day_info.venue_closed == "400"

    def test_day_info_venue_open_closed_keeps_string(self):
        """Test DayInfo keeps string values for venue_open/venue_closed."""
        day_info_json = {
            "day_int": 0,
            "day_max": 85,
            "day_mean": 42,
            "day_rank_max": 5,
            "day_rank_mean": 10,
            "day_text": "Monday",
            "venue_open": "21:00",  # String
            "venue_closed": "04:00",  # String
        }

        day_info = DayInfo(**day_info_json)
        assert day_info.venue_open == "21:00"
        assert day_info.venue_closed == "04:00"

    def test_day_info_v2_with_24h_field(self):
        """Test DayInfoV2 with 24h field alias."""
        day_info_v2_json = {
            "open_24h": False,
            "crosses_midnight": True,
            "day_text": "Monday",
            "special_day": None,
            "24h": [
                {
                    "opens": 21,
                    "closes": 4,
                    "opens_minutes": 0,
                    "closes_minutes": 0,
                }
            ],
            "12h": ["9:00 PM - 4:00 AM"],
        }

        day_info_v2 = DayInfoV2(**day_info_v2_json)
        assert len(day_info_v2.h24) == 1
        assert day_info_v2.h24[0].opens == 21
        assert day_info_v2.crosses_midnight is True

    def test_foot_traffic_forecast(self):
        """Test FootTrafficForecast model."""
        forecast = FootTrafficForecast(
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

        assert len(forecast.day_raw) == 24
        assert forecast.day_info.day_max == 85


class TestLiveForecastModels:
    """Test live forecast-related models."""

    def test_live_forecast_response(self):
        """Test LiveForecastResponse model."""
        response_json = {
            "status": "OK",
            "venue_info": {
                "venue_id": "ven_123",
                "venue_name": "Test Bar",
                "venue_timezone": "America/Recife",
                "venue_current_gmttime": "2025-01-25T12:00:00Z",
                "venue_current_localtime": "2025-01-25T09:00:00-03:00",
                "venue_dwell_time_min": 30,
                "venue_dwell_time_max": 120,
                "venue_dwell_time_avg": 75,
            },
            "analysis": {
                "venue_live_busyness": 65,
                "venue_live_busyness_available": True,
                "venue_forecasted_busyness": 70,
                "venue_forecast_busyness_available": True,
                "venue_live_forecasted_delta": -5,
            },
        }

        response = LiveForecastResponse(**response_json)
        assert response.status == "OK"
        assert response.analysis.venue_live_busyness == 65
        assert response.analysis.venue_live_busyness_available is True
        assert response.venue_info.venue_name == "Test Bar"


class TestWeekRawModels:
    """Test weekly forecast-related models."""

    def test_week_raw_day(self):
        """Test WeekRawDay model."""
        day_data = {
            "day_int": 0,
            "day_raw": [10] * 24,
            "day_info": {
                "day_int": 0,
                "day_max": 50,
                "day_mean": 25,
                "day_rank_max": 3,
                "day_rank_mean": 5,
                "day_text": "Monday",
                "venue_open": "2100",
                "venue_closed": "0400",
            },
        }

        week_day = WeekRawDay(**day_data)
        assert week_day.day_int == 0
        assert len(week_day.day_raw) == 24
        assert week_day.day_info.day_text == "Monday"

    def test_week_raw_response(self):
        """Test WeekRawResponse model."""
        response_json = {
            "status": "OK",
            "venue_id": "ven_123",
            "venue_name": "Test Venue",
            "venue_address": "123 Main St",
            "window": {
                "time_window_start": 0,
                "time_window_start_12h": "12:00 AM",
                "day_window_start_int": 0,
                "day_window_start_txt": "Monday",
                "day_window_end_int": 6,
                "day_window_end_txt": "Sunday",
                "time_window_end": 23,
                "time_window_end_12h": "11:00 PM",
                "week_window": "This week",
            },
            "analysis": {
                "week_raw": [
                    {
                        "day_int": i,
                        "day_raw": [10] * 24,
                        "day_info": None,
                    }
                    for i in range(7)
                ],
            },
        }

        response = WeekRawResponse(**response_json)
        assert response.status == "OK"
        assert len(response.analysis.week_raw) == 7
        assert response.window.week_window == "This week"


def test_json_round_trip():
    """Test JSON serialization round trip."""
    venue = Venue(
        forecast=True,
        processed=True,
        venue_lat=-8.0,
        venue_lng=-34.9,
        venue_name="Test",
        venue_id="test_123",
    )

    # Serialize to JSON
    json_str = venue.model_dump_json(by_alias=True)
    json_dict = json.loads(json_str)

    # Deserialize back
    venue_restored = Venue(**json_dict)

    assert venue_restored.venue_name == venue.venue_name
    assert venue_restored.venue_lat == venue.venue_lat
