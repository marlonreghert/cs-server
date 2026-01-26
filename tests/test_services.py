"""Unit tests for service layer."""
import pytest
from unittest.mock import Mock, AsyncMock, patch

from app.services import VenueService, VenuesRefresherService
from app.models import (
    Venue,
    VenueFilterResponse,
    VenueFilterVenue,
    VenueFilterParams,
    DayInfo,
    LiveForecastResponse,
    VenueInfo,
    Analysis,
    WeekRawResponse,
    WeekRawAnalysis,
    WeekRawDay,
    RawWindow,
)


@pytest.fixture
def mock_venue_dao():
    """Create mock venue DAO."""
    return Mock()


@pytest.fixture
def mock_besttime_api():
    """Create mock BestTime API client."""
    mock = Mock()
    # Make async methods return AsyncMock
    mock.venue_filter = AsyncMock()
    mock.get_live_forecast = AsyncMock()
    mock.get_week_raw_forecast = AsyncMock()
    return mock


@pytest.fixture
def refresher_service(mock_venue_dao, mock_besttime_api):
    """Create VenuesRefresherService with mocked dependencies."""
    return VenuesRefresherService(mock_venue_dao, mock_besttime_api)


@pytest.fixture
def venue_service(mock_venue_dao, mock_besttime_api):
    """Create VenueService with mocked dependencies."""
    return VenueService(mock_venue_dao, mock_besttime_api)


class TestVenueService:
    """Test VenueService (simple wrapper)."""

    def test_get_venues_nearby(self, venue_service, mock_venue_dao):
        """Test get_venues_nearby delegates to DAO."""
        mock_venue_dao.get_nearby_venues.return_value = [
            Venue(venue_id="v1", venue_lat=-8.0, venue_lng=-34.9)
        ]

        result = venue_service.get_venues_nearby(lat=-8.0, lon=-34.9, radius=5.0)

        assert len(result) == 1
        assert result[0].venue_id == "v1"
        mock_venue_dao.get_nearby_venues.assert_called_once_with(-8.0, -34.9, 5.0)


class TestVenuesRefresherService:
    """Test VenuesRefresherService critical business logic."""

    def test_map_venue_filter_venue_to_venue(self, refresher_service):
        """Test VenueFilterVenue to Venue mapping."""
        vf = VenueFilterVenue(
            venue_id="v123",
            venue_name="Test Bar",
            venue_address="123 Main St",
            venue_lat=-8.07834,
            venue_lng=-34.90938,
            day_int=0,
            day_raw=[50] * 24,
            venue_type="BAR",
            rating=4.5,
            reviews=100,
            price_level=2,
            day_info=DayInfo(
                day_int=0,
                day_max=85,
                day_mean=50,
                day_rank_max=5,
                day_rank_mean=10,
                day_text="Monday",
                venue_open="2100",
                venue_closed="0400",
            ),
        )

        venue = refresher_service._map_venue_filter_venue_to_venue(vf)

        assert venue.venue_id == "v123"
        assert venue.venue_name == "Test Bar"
        assert venue.forecast is True
        assert venue.processed is True
        assert venue.rating == 4.5
        assert venue.price_level == 2
        assert len(venue.venue_foot_traffic_forecast) == 1
        assert venue.venue_foot_traffic_forecast[0].day_int == 0

    @pytest.mark.asyncio
    async def test_deduplication_by_venue_id(self, refresher_service, mock_besttime_api, mock_venue_dao):
        """Test CRITICAL deduplication logic - by venue_id."""
        # Mock API response with duplicate venue IDs
        mock_response = VenueFilterResponse(
            status="OK",
            venues_n=3,
            venues=[
                VenueFilterVenue(
                    venue_id="v1",
                    venue_name="Bar A",
                    venue_address="Addr 1",
                    venue_lat=-8.0,
                    venue_lng=-34.9,
                    day_int=0,
                    day_raw=[50] * 24,
                ),
                VenueFilterVenue(
                    venue_id="v1",  # DUPLICATE ID
                    venue_name="Bar A Different Name",
                    venue_address="Addr 2",
                    venue_lat=-8.01,
                    venue_lng=-34.91,
                    day_int=0,
                    day_raw=[60] * 24,
                ),
                VenueFilterVenue(
                    venue_id="v2",
                    venue_name="Bar B",
                    venue_address="Addr 3",
                    venue_lat=-8.02,
                    venue_lng=-34.92,
                    day_int=0,
                    day_raw=[70] * 24,
                ),
            ],
        )
        mock_besttime_api.venue_filter.return_value = mock_response

        params = VenueFilterParams(lat=-8.0, lng=-34.9, radius=5000)
        unique_ids = await refresher_service.refresh_venues_data_by_venues_filter(params)

        # Should only process 2 unique venue IDs (v1 once, v2 once)
        assert len(unique_ids) == 2
        assert unique_ids == ["v1", "v2"]

        # Should only upsert 2 venues (duplicate v1 skipped)
        assert mock_venue_dao.upsert_venue.call_count == 2

    @pytest.mark.asyncio
    async def test_deduplication_by_venue_name(self, refresher_service, mock_besttime_api, mock_venue_dao):
        """Test CRITICAL deduplication logic - by venue_name."""
        # Mock API response with duplicate venue names (different IDs)
        mock_response = VenueFilterResponse(
            status="OK",
            venues_n=3,
            venues=[
                VenueFilterVenue(
                    venue_id="v1",
                    venue_name="Bar A",
                    venue_address="Addr 1",
                    venue_lat=-8.0,
                    venue_lng=-34.9,
                    day_int=0,
                    day_raw=[50] * 24,
                ),
                VenueFilterVenue(
                    venue_id="v2",
                    venue_name="Bar A",  # DUPLICATE NAME
                    venue_address="Addr 2",
                    venue_lat=-8.01,
                    venue_lng=-34.91,
                    day_int=0,
                    day_raw=[60] * 24,
                ),
                VenueFilterVenue(
                    venue_id="v3",
                    venue_name="Bar B",
                    venue_address="Addr 3",
                    venue_lat=-8.02,
                    venue_lng=-34.92,
                    day_int=0,
                    day_raw=[70] * 24,
                ),
            ],
        )
        mock_besttime_api.venue_filter.return_value = mock_response

        params = VenueFilterParams(lat=-8.0, lng=-34.9, radius=5000)
        unique_ids = await refresher_service.refresh_venues_data_by_venues_filter(params)

        # Should only process 2 unique venues (first "Bar A" kept, duplicate skipped)
        assert len(unique_ids) == 2
        assert unique_ids == ["v1", "v3"]
        assert mock_venue_dao.upsert_venue.call_count == 2

    @pytest.mark.asyncio
    async def test_deduplication_skips_empty_id_and_name(
        self, refresher_service, mock_besttime_api, mock_venue_dao
    ):
        """Test that venues with no ID and no name are skipped."""
        mock_response = VenueFilterResponse(
            status="OK",
            venues_n=2,
            venues=[
                VenueFilterVenue(
                    venue_id="",  # Empty ID
                    venue_name="",  # Empty name
                    venue_address="Addr 1",
                    venue_lat=-8.0,
                    venue_lng=-34.9,
                    day_int=0,
                    day_raw=[50] * 24,
                ),
                VenueFilterVenue(
                    venue_id="v1",
                    venue_name="Valid Bar",
                    venue_address="Addr 2",
                    venue_lat=-8.01,
                    venue_lng=-34.91,
                    day_int=0,
                    day_raw=[60] * 24,
                ),
            ],
        )
        mock_besttime_api.venue_filter.return_value = mock_response

        params = VenueFilterParams(lat=-8.0, lng=-34.9, radius=5000)
        unique_ids = await refresher_service.refresh_venues_data_by_venues_filter(params)

        # Should only process 1 venue (empty ID/name skipped)
        assert len(unique_ids) == 1
        assert unique_ids == ["v1"]
        assert mock_venue_dao.upsert_venue.call_count == 1

    @pytest.mark.asyncio
    async def test_live_forecast_caching_success(
        self, refresher_service, mock_besttime_api, mock_venue_dao
    ):
        """Test CRITICAL live forecast filtering - cache when status OK and available."""
        mock_besttime_api.get_live_forecast.return_value = LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(
                venue_live_busyness=75,
                venue_live_busyness_available=True,  # Available
            ),
        )

        await refresher_service._fetch_and_cache_live_forecasts(["v1"])

        # Should cache because status OK and available
        mock_venue_dao.set_live_forecast.assert_called_once()
        mock_venue_dao.delete_live_forecast.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_forecast_delete_when_status_not_ok(
        self, refresher_service, mock_besttime_api, mock_venue_dao
    ):
        """Test CRITICAL live forecast filtering - delete cache when status not OK."""
        mock_besttime_api.get_live_forecast.return_value = LiveForecastResponse(
            status="ERROR",  # Not OK
            venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(
                venue_live_busyness=0,
                venue_live_busyness_available=False,
            ),
        )

        await refresher_service._fetch_and_cache_live_forecasts(["v1"])

        # Should delete cache, not set
        mock_venue_dao.delete_live_forecast.assert_called_once_with("v1")
        mock_venue_dao.set_live_forecast.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_forecast_delete_when_not_available(
        self, refresher_service, mock_besttime_api, mock_venue_dao
    ):
        """Test CRITICAL live forecast filtering - delete when not available (venue closed)."""
        mock_besttime_api.get_live_forecast.return_value = LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(
                venue_live_busyness=0,
                venue_live_busyness_available=False,  # Not available (venue closed)
            ),
        )

        await refresher_service._fetch_and_cache_live_forecasts(["v1"])

        # Should delete cache because not available
        mock_venue_dao.delete_live_forecast.assert_called_once_with("v1")
        mock_venue_dao.set_live_forecast.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_venues_by_filter_with_live_fetch(
        self, refresher_service, mock_besttime_api, mock_venue_dao
    ):
        """Test refresh with live forecast fetching enabled."""
        mock_response = VenueFilterResponse(
            status="OK",
            venues_n=1,
            venues=[
                VenueFilterVenue(
                    venue_id="v1",
                    venue_name="Test",
                    venue_address="Addr",
                    venue_lat=-8.0,
                    venue_lng=-34.9,
                    day_int=0,
                    day_raw=[50] * 24,
                )
            ],
        )
        mock_besttime_api.venue_filter.return_value = mock_response
        mock_besttime_api.get_live_forecast.return_value = LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(venue_live_busyness_available=True),
        )

        params = VenueFilterParams(lat=-8.0, lng=-34.9, radius=5000)
        await refresher_service.refresh_venues_data_by_venues_filter(
            params, fetch_and_cache_live=True
        )

        # Should fetch live forecast for v1
        mock_besttime_api.get_live_forecast.assert_called_once_with(venue_id="v1")
        mock_venue_dao.set_live_forecast.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_weekly_forecasts(
        self, refresher_service, mock_besttime_api, mock_venue_dao
    ):
        """Test weekly forecast refresh for all venues."""
        mock_venue_dao.list_all_venue_ids.return_value = ["v1", "v2"]

        mock_besttime_api.get_week_raw_forecast.return_value = WeekRawResponse(
            status="OK",
            venue_id="v1",
            venue_name="Test",
            venue_address="Addr",
            window=RawWindow(),
            analysis=WeekRawAnalysis(
                week_raw=[
                    WeekRawDay(day_int=i, day_raw=[50] * 24) for i in range(7)
                ]
            ),
        )

        await refresher_service.refresh_weekly_forecasts_for_all_venues()

        # Should fetch weekly forecast for both venues
        assert mock_besttime_api.get_week_raw_forecast.call_count == 2

        # Should cache 7 days × 2 venues = 14 times
        assert mock_venue_dao.set_week_raw_forecast.call_count == 14

    @pytest.mark.asyncio
    async def test_refresh_weekly_forecasts_skips_non_ok_status(
        self, refresher_service, mock_besttime_api, mock_venue_dao
    ):
        """Test that weekly forecast with non-OK status is not cached."""
        mock_venue_dao.list_all_venue_ids.return_value = ["v1"]

        mock_besttime_api.get_week_raw_forecast.return_value = WeekRawResponse(
            status="ERROR",  # Not OK
            venue_id="v1",
            venue_name="Test",
            venue_address="Addr",
            window=RawWindow(),
            analysis=WeekRawAnalysis(week_raw=[]),
        )

        await refresher_service.refresh_weekly_forecasts_for_all_venues()

        # Should not cache anything
        mock_venue_dao.set_week_raw_forecast.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_locations_values(self):
        """Test that default locations match Go implementation exactly."""
        from app.services.venues_refresher_service import DEFAULT_LOCATIONS

        assert len(DEFAULT_LOCATIONS) == 3

        # ZS/ZN - C1
        assert DEFAULT_LOCATIONS[0].lat == -8.07834
        assert DEFAULT_LOCATIONS[0].lng == -34.90938
        assert DEFAULT_LOCATIONS[0].radius == 6000
        assert DEFAULT_LOCATIONS[0].limit == 500

        # Olinda
        assert DEFAULT_LOCATIONS[1].lat == -7.99081
        assert DEFAULT_LOCATIONS[1].lng == -34.85141
        assert DEFAULT_LOCATIONS[1].radius == 6000
        assert DEFAULT_LOCATIONS[1].limit == 200

        # Jaboatao/Candeias
        assert DEFAULT_LOCATIONS[2].lat == -8.18160
        assert DEFAULT_LOCATIONS[2].lng == -34.92980
        assert DEFAULT_LOCATIONS[2].radius == 6000
        assert DEFAULT_LOCATIONS[2].limit == 200

    @pytest.mark.asyncio
    async def test_nightlife_venue_types_values(self):
        """Test that nightlife venue types match Go implementation exactly."""
        from app.services.venues_refresher_service import NIGHTLIFE_VENUE_TYPES

        expected_types = [
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

        assert NIGHTLIFE_VENUE_TYPES == expected_types
