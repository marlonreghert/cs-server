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


class TestAddVenueTimeout:
    """The slow POST /forecasts create call gets its own longer timeout,
    independent of the tight client-wide default used by read calls."""

    def test_default_add_venue_timeout_is_60_and_base_timeout_unchanged(self):
        client = BestTimeAPIClient(
            base_url="https://besttime.app/api/v1",
            api_key_public="pub",
            api_key_private="priv",
        )
        assert client.add_venue_timeout == 60.0
        # The client-wide default (used by live/read calls) must stay tight.
        assert client.timeout == 10.0

    def test_explicit_add_venue_timeout_overrides_default(self):
        client = BestTimeAPIClient(
            base_url="https://besttime.app/api/v1",
            api_key_public="pub",
            api_key_private="priv",
            timeout=10.0,
            add_venue_timeout=45.0,
        )
        assert client.add_venue_timeout == 45.0

    @pytest.mark.asyncio
    async def test_add_venue_request_uses_add_venue_timeout(self):
        """add_venue_to_account must issue POST /forecasts with the configured
        add-venue timeout, not the client-wide default."""
        client = BestTimeAPIClient(
            base_url="https://besttime.app/api/v1",
            api_key_public="pub",
            api_key_private="priv",
            timeout=10.0,
            add_venue_timeout=30.0,
        )
        with patch.object(client.client, "request", new_callable=AsyncMock) as mock_request:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "status": "OK",
                "venue_info": {
                    "venue_id": "ven_fresh_001",
                    "venue_name": "Bar do Joao",
                    "venue_address": "Rua das Flores 123",
                    "venue_lat": -8.05,
                    "venue_lon": -34.88,
                },
                "analysis": [],
            }
            mock_request.return_value = mock_response

            result = await client.add_venue_to_account("Bar do Joao", "Rua das Flores 123")

            assert result.is_ok()
            assert mock_request.call_args.kwargs["timeout"] == 30.0

    @pytest.mark.asyncio
    async def test_read_calls_do_not_override_timeout(self, api_client):
        """Read/list calls must not pass a per-request timeout, so they inherit
        the tight client-wide default (10s)."""
        with patch.object(api_client.client, "request", new_callable=AsyncMock) as mock_request:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "status": "OK",
                "venues_n": 0,
                "venues": [],
            }
            mock_request.return_value = mock_response

            params = VenueFilterParams(lat=-8.07834, lng=-34.90938, radius=5000, live=True)
            await api_client.venue_filter(params)

            assert "timeout" not in mock_request.call_args.kwargs

    def test_settings_default_add_venue_timeout(self):
        from app.config import Settings, settings

        # Field default is 60s (env/JSON overrides still win at runtime).
        assert (
            Settings.model_fields["besttime_add_venue_timeout_seconds"].default
            == 60.0
        )
        assert settings.besttime_add_venue_timeout_seconds == 60.0


class TestAddVenueResponseParsing:
    """add_venue_to_account must parse the real create response and classify
    an unparseable envelope as its own error type, not a transport failure."""

    def _mock_response(self, body: dict):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = body
        return mock_response

    @staticmethod
    def _errors_metric() -> float:
        from prometheus_client import REGISTRY

        return (
            REGISTRY.get_sample_value(
                "besttime_api_errors_total",
                {"endpoint": "/forecasts", "error_type": "invalid_response_schema"},
            )
            or 0.0
        )

    @pytest.mark.asyncio
    async def test_real_create_shape_returns_ok_response(self, api_client):
        body = {
            "status": "OK",
            "venue_info": {
                "venue_id": "ven_real_001",
                "venue_name": "Laca Burguer",
                "venue_address": "Av. Conselheiro Aguiar 123",
                "venue_lat": -8.119,
                "venue_lon": -34.904,
            },
            "analysis": [
                {
                    "day_info": {"day_int": day, "day_text": "Monday"},
                    "day_raw": [day] * 24,
                }
                for day in range(7)
            ],
        }
        with patch.object(
            api_client.client, "request", new_callable=AsyncMock
        ) as mock_request:
            mock_request.return_value = self._mock_response(body)

            result = await api_client.add_venue_to_account("Laca Burguer", "Av. 123")

        assert result.is_ok()
        assert [d.day_int for d in result.analysis] == list(range(7))

    @pytest.mark.asyncio
    async def test_4xx_rejection_with_idless_venue_info_returns_parsed_not_ok(
        self, api_client
    ):
        """Prod 2026-07-02: a 404 rejection whose venue_info has no venue_id
        must return a parsed non-OK response (so the handler takes the
        rejection path), not raise the typed invalid-response error."""
        before = self._errors_metric()
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.json.return_value = {
            "status": "Error",
            "message": "The venue could not be found.",
            "venue_info": {"venue_name": "Mansao da Matuta"},
        }
        with patch.object(
            api_client.client, "request", new_callable=AsyncMock
        ) as mock_request:
            mock_request.return_value = mock_response

            result = await api_client.add_venue_to_account("Mansao", "R. Bonfim 82")

        assert not result.is_ok()
        assert result.message == "The venue could not be found."
        assert result.venue_info.venue_id is None
        assert self._errors_metric() - before == 0

    @pytest.mark.asyncio
    async def test_unparseable_envelope_raises_typed_error_not_validation_error(
        self, api_client
    ):
        from pydantic import ValidationError

        from app.api.besttime_client import BestTimeInvalidResponseError

        before = self._errors_metric()
        with patch.object(
            api_client.client, "request", new_callable=AsyncMock
        ) as mock_request:
            mock_request.return_value = self._mock_response(
                {"forecast": "maybe", "venue_info": "not-an-object"}
            )

            with pytest.raises(BestTimeInvalidResponseError) as exc_info:
                await api_client.add_venue_to_account("Bar", "Rua 1")

        assert not isinstance(exc_info.value, ValidationError)
        # The typed error names the failed envelope fields for the ERROR log.
        assert "status" in str(exc_info.value)
        assert self._errors_metric() - before == 1

    @pytest.mark.asyncio
    async def test_transport_error_does_not_use_schema_error_type(self, api_client):
        before = self._errors_metric()
        with patch.object(
            api_client.client, "request", new_callable=AsyncMock
        ) as mock_request:
            mock_request.side_effect = httpx.ConnectError("refused")

            with pytest.raises(httpx.ConnectError):
                await api_client.add_venue_to_account("Bar", "Rua 1")

        assert self._errors_metric() - before == 0


class TestSearchRateLimiter:
    """Window math for the venue-search pacing limiter (injected fake clock —
    no real sleeping)."""

    def _limiter(self, per_minute=2, per_hour=3, max_wait=75.0):
        from app.api.besttime_client import _SearchRateLimiter

        clock = {"now": 1000.0}
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["now"] += seconds

        limiter = _SearchRateLimiter(
            per_minute=per_minute,
            per_hour=per_hour,
            max_wait_seconds=max_wait,
            time_func=lambda: clock["now"],
            sleep_func=fake_sleep,
        )
        return limiter, clock, sleeps

    @pytest.mark.asyncio
    async def test_under_the_window_never_waits(self):
        limiter, _, sleeps = self._limiter()
        await limiter.acquire("/venues/filter")
        await limiter.acquire("/venues/filter")
        assert sleeps == []

    @pytest.mark.asyncio
    async def test_minute_window_full_waits_until_slot_frees(self):
        limiter, _, sleeps = self._limiter(per_minute=2, per_hour=100)
        await limiter.acquire("/forecasts")
        await limiter.acquire("/forecasts")
        await limiter.acquire("/forecasts")  # third call must wait ~60s
        assert len(sleeps) == 1
        assert sleeps[0] == pytest.approx(60.0)

    @pytest.mark.asyncio
    async def test_wait_beyond_budget_raises_before_sending(self):
        from app.api.besttime_client import BestTimeRateLimitedError

        # Hour window full → needed wait (~3600s) far exceeds max_wait.
        limiter, _, sleeps = self._limiter(per_minute=100, per_hour=2, max_wait=30.0)
        await limiter.acquire("/forecasts")
        await limiter.acquire("/forecasts")
        with pytest.raises(BestTimeRateLimitedError):
            await limiter.acquire("/forecasts")
        assert sleeps == []  # rejected without sleeping

    @pytest.mark.asyncio
    async def test_window_frees_after_time_passes(self):
        limiter, clock, sleeps = self._limiter(per_minute=2, per_hour=100)
        await limiter.acquire("/forecasts")
        await limiter.acquire("/forecasts")
        clock["now"] += 61.0  # minute window expired
        await limiter.acquire("/forecasts")
        assert sleeps == []


class TestCreate429Retry:
    """HTTP 429 handling on the POST /forecasts create."""

    def _response(self, status, body, headers=None):
        return httpx.Response(
            status, json=body, headers=headers or {},
            request=httpx.Request("POST", "https://besttime.app/api/v1/forecasts"),
        )

    @pytest.mark.asyncio
    async def test_429_then_success_is_retried(self, api_client):
        ok_body = {
            "status": "OK",
            "venue_info": {
                "venue_id": "ven_ok",
                "venue_name": "Bar",
                "venue_address": "Rua 1",
            },
        }
        responses = [
            self._response(429, {"status": "Error", "message": "Too many requests."},
                           {"Retry-After": "0"}),
            self._response(200, ok_body),
        ]
        with patch.object(api_client.client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.side_effect = responses
            parsed = await api_client.add_venue_to_account("Bar", "Rua 1")
        assert mock_request.await_count == 2
        assert parsed.is_ok()

    @pytest.mark.asyncio
    async def test_persistent_429_raises_rate_limited(self, api_client):
        from app.api.besttime_client import BestTimeRateLimitedError

        limit = self._response(
            429, {"status": "Error", "message": "Too many requests."},
            {"Retry-After": "0"},
        )
        with patch.object(api_client.client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = limit
            with pytest.raises(BestTimeRateLimitedError):
                await api_client.add_venue_to_account("Bar", "Rua 1")
        assert mock_request.await_count == 3  # initial + 2 bounded retries

    @pytest.mark.asyncio
    async def test_429_with_monthly_cap_message_is_not_retried(self, api_client):
        cap_body = {
            "status": "Error",
            "message": "Max amount of monthly venues (500) reached. "
                       "Venue counter will reset next month.",
        }
        with patch.object(api_client.client, "request", new_callable=AsyncMock) as mock_request:
            mock_request.return_value = self._response(429, cap_body)
            parsed = await api_client.add_venue_to_account("Bar", "Rua 1")
        # Terminal quota state: single call, flows to the normal parse path so
        # the handler can surface the cap legibly.
        assert mock_request.await_count == 1
        assert not parsed.is_ok()
        assert "monthly venues" in (parsed.message or "").lower()
