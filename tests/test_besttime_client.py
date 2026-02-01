"""Unit tests for BestTime API client."""
import pytest
from unittest.mock import AsyncMock, Mock, patch
import httpx

from app.api import BestTimeAPIClient
from app.models import (
    VenueFilterParams,
    VenueFilterResponse,
    LiveForecastResponse,
    WeekRawResponse,
)


@pytest.fixture
def api_client():
    """Create BestTime API client for testing."""
    client = BestTimeAPIClient(
        base_url="https://besttime.app/api/v1",
        api_key_public="test_public_key",
        api_key_private="test_private_key",
        timeout=10.0,
    )
    yield client


class TestBestTimeAPIClient:
    """Unit tests for BestTimeAPIClient."""

    @pytest.mark.asyncio
    async def test_venue_filter_success(self, api_client):
        """Test successful venue_filter call."""
        # Mock response
        mock_response_data = {
            "status": "OK",
            "venues_n": 2,
            "venues": [
                {
                    "venue_id": "ven-123",
                    "venue_name": "Test Bar",
                    "venue_address": "123 Main St",
                    "venue_lat": -8.07834,
                    "venue_lng": -34.90938,
                    "day_int": 0,
                    "day_raw": [50] * 24,
                },
                {
                    "venue_id": "ven-456",
                    "venue_name": "Test Club",
                    "venue_address": "456 Club Ave",
                    "venue_lat": -8.08,
                    "venue_lng": -34.91,
                    "day_int": 0,
                    "day_raw": [60] * 24,
                },
            ],
        }

        # Mock the httpx client
        with patch.object(api_client.client, "request", new_callable=AsyncMock) as mock_request:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_response_data
            mock_request.return_value = mock_response

            # Call venue_filter
            params = VenueFilterParams(
                lat=-8.07834,
                lng=-34.90938,
                radius=5000,
                live=True,
            )

            response = await api_client.venue_filter(params)

            # Verify response
            assert isinstance(response, VenueFilterResponse)
            assert response.status == "OK"
            assert response.venues_n == 2
            assert len(response.venues) == 2
            assert response.venues[0].venue_name == "Test Bar"

            # Verify API key was added to query params
            call_args = mock_request.call_args
            assert call_args.kwargs["params"]["api_key_private"] == "test_private_key"

    @pytest.mark.asyncio
    async def test_get_live_forecast_with_venue_id(self, api_client):
        """Test get_live_forecast using venue_id."""
        mock_response_data = {
            "status": "OK",
            "venue_info": {
                "venue_id": "ven-123",
                "venue_name": "Test Venue",
                "venue_timezone": "America/Recife",
            },
            "analysis": {
                "venue_live_busyness": 75,
                "venue_live_busyness_available": True,
                "venue_forecasted_busyness": 70,
                "venue_forecast_busyness_available": True,
                "venue_live_forecasted_delta": 5,
            },
        }

        with patch.object(api_client.client, "request", new_callable=AsyncMock) as mock_request:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_response_data
            mock_request.return_value = mock_response

            response = await api_client.get_live_forecast(venue_id="ven-123")

            assert isinstance(response, LiveForecastResponse)
            assert response.status == "OK"
            assert response.analysis.venue_live_busyness == 75
            assert response.venue_info.venue_id == "ven-123"

            # Verify request parameters
            call_args = mock_request.call_args
            assert call_args.kwargs["params"]["venue_id"] == "ven-123"
            assert call_args.kwargs["params"]["api_key_private"] == "test_private_key"

    @pytest.mark.asyncio
    async def test_get_live_forecast_with_name_and_address(self, api_client):
        """Test get_live_forecast using venue_name and venue_address."""
        mock_response_data = {
            "status": "OK",
            "venue_info": {
                "venue_id": "ven-789",
                "venue_name": "Test Bar",
                "venue_timezone": "America/Recife",
            },
            "analysis": {
                "venue_live_busyness": 60,
                "venue_live_busyness_available": True,
                "venue_forecasted_busyness": 55,
                "venue_forecast_busyness_available": True,
                "venue_live_forecasted_delta": 5,
            },
        }

        with patch.object(api_client.client, "request", new_callable=AsyncMock) as mock_request:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_response_data
            mock_request.return_value = mock_response

            response = await api_client.get_live_forecast(
                venue_name="Test Bar",
                venue_address="123 Main St",
            )

            assert response.status == "OK"

            # Verify name and address used instead of venue_id
            call_args = mock_request.call_args
            assert "venue_id" not in call_args.kwargs["params"]
            assert call_args.kwargs["params"]["venue_name"] == "Test Bar"
            assert call_args.kwargs["params"]["venue_address"] == "123 Main St"

    @pytest.mark.asyncio
    async def test_get_live_forecast_missing_parameters(self, api_client):
        """Test that get_live_forecast raises ValueError with missing params."""
        with pytest.raises(ValueError, match="Either venue_id or both venue_name"):
            await api_client.get_live_forecast(venue_name="Test Bar")

    @pytest.mark.asyncio
    async def test_get_week_raw_forecast(self, api_client):
        """Test get_week_raw_forecast."""
        mock_response_data = {
            "status": "OK",
            "venue_id": "ven-123",
            "venue_name": "Test Venue",
            "venue_address": "123 Main St",
            "window": {
                "time_window_start": 0,
                "time_window_end": 23,
                "day_window_start_int": 0,
                "day_window_end_int": 6,
                "week_window": "This week",
            },
            "analysis": {
                "week_raw": [
                    {
                        "day_int": i,
                        "day_raw": [50] * 24,
                    }
                    for i in range(7)
                ]
            },
        }

        with patch.object(api_client.client, "request", new_callable=AsyncMock) as mock_request:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_response_data
            mock_request.return_value = mock_response

            response = await api_client.get_week_raw_forecast("ven-123")

            assert isinstance(response, WeekRawResponse)
            assert response.status == "OK"
            assert len(response.analysis.week_raw) == 7

            # Verify request uses public key
            call_args = mock_request.call_args
            assert call_args.kwargs["params"]["api_key_public"] == "test_public_key"
            assert call_args.kwargs["params"]["venue_id"] == "ven-123"

    @pytest.mark.asyncio
    async def test_get_week_raw_forecast_empty_venue_id(self, api_client):
        """Test that get_week_raw_forecast raises ValueError with empty venue_id."""
        with pytest.raises(ValueError, match="venue_id must be provided"):
            await api_client.get_week_raw_forecast("")

    @pytest.mark.asyncio
    async def test_http_error_handling(self, api_client):
        """Test that HTTP errors are properly raised."""
        with patch.object(api_client.client, "request", new_callable=AsyncMock) as mock_request:
            # Simulate 404 error
            mock_response = Mock()
            mock_response.status_code = 404
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "Not Found", request=Mock(), response=mock_response
            )
            mock_request.return_value = mock_response

            params = VenueFilterParams(lat=-8.0, lng=-34.9, radius=5000)

            with pytest.raises(httpx.HTTPStatusError):
                await api_client.venue_filter(params)

    @pytest.mark.asyncio
    async def test_request_error_handling(self, api_client):
        """Test that network errors are properly raised."""
        with patch.object(api_client.client, "request", new_callable=AsyncMock) as mock_request:
            # Simulate connection error
            mock_request.side_effect = httpx.RequestError("Connection failed")

            params = VenueFilterParams(lat=-8.0, lng=-34.9, radius=5000)

            with pytest.raises(httpx.RequestError):
                await api_client.venue_filter(params)

    @pytest.mark.asyncio
    async def test_venue_filter_params_conversion(self):
        """Test VenueFilterParams to_query_params conversion."""
        params = VenueFilterParams(
            lat=-8.07834,
            lng=-34.90938,
            radius=5000,
            live=True,
            types=["BAR", "CLUB", "RESTAURANT"],
            busy_min=10,
            busy_max=90,
            limit=100,
        )

        query_params = params.to_query_params()

        assert query_params["lat"] == "-8.07834"
        assert query_params["lng"] == "-34.90938"
        assert query_params["radius"] == "5000"
        assert query_params["live"] == "true"
        assert query_params["types"] == "BAR,CLUB,RESTAURANT"
        assert query_params["busy_min"] == "10"
        assert query_params["busy_max"] == "90"
        assert query_params["limit"] == "100"

    @pytest.mark.asyncio
    async def test_venue_filter_params_omits_none_values(self):
        """Test that None values are omitted from query params."""
        params = VenueFilterParams(
            lat=-8.0,
            lng=-34.9,
            radius=5000,
            live=None,  # Should be omitted
            types=None,  # Should be omitted
        )

        query_params = params.to_query_params()

        assert "lat" in query_params
        assert "lng" in query_params
        assert "radius" in query_params
        assert "live" not in query_params
        assert "types" not in query_params

    @pytest.mark.asyncio
    async def test_close(self, api_client):
        """Test that close() properly closes the HTTP client."""
        with patch.object(api_client.client, "aclose", new_callable=AsyncMock) as mock_close:
            await api_client.close()
            mock_close.assert_called_once()
