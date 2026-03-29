"""Tests for Discovery Points feature — admin-configurable venue rotation."""
import json
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock

from app.services.venues_refresher_service import VenuesRefresherService, DEFAULT_LOCATIONS
from app.models import VenueFilterResponse, VenueFilterVenue, DayInfo


# ---- Helpers ----

def _make_points(overrides: list[dict] | None = None) -> list[dict]:
    """Create sample discovery points for tests."""
    defaults = [
        {"id": "recife-zs-zn", "label": "Recife ZS/ZN", "lat": -8.07834, "lng": -34.90938,
         "radius": 15000, "limit": 500, "current": 350},
        {"id": "recife-olinda", "label": "Recife Olinda", "lat": -7.99081, "lng": -34.85141,
         "radius": 15000, "limit": 500, "current": 500},  # Saturated
        {"id": "brasilia-asa-sul", "label": "Brasilia Asa Sul", "lat": -15.8267, "lng": -47.9218,
         "radius": 8000, "limit": 300, "current": 100},
    ]
    if overrides:
        for i, ov in enumerate(overrides):
            if i < len(defaults):
                defaults[i].update(ov)
    return defaults


def _make_filter_response(count: int = 5) -> VenueFilterResponse:
    """Create a mock VenueFilterResponse with `count` venues."""
    venues = []
    for i in range(count):
        venues.append(VenueFilterVenue(
            venue_id=f"v{i}",
            venue_name=f"Venue {i}",
            venue_address=f"Addr {i}",
            venue_lat=-8.0 + i * 0.01,
            venue_lng=-34.9 + i * 0.01,
            day_int=0,
            day_raw=[50] * 24,
        ))
    return VenueFilterResponse(status="OK", venues_n=count, venues=venues)


# ---- Fixtures ----

@pytest.fixture
def mock_venue_dao():
    dao = Mock()
    dao.list_all_venues.return_value = []
    dao.count_venues_in_radius.return_value = 0
    return dao


@pytest.fixture
def mock_besttime_api():
    mock = Mock()
    mock.venue_filter = AsyncMock()
    mock.get_live_forecast = AsyncMock()
    mock.get_week_raw_forecast = AsyncMock()
    return mock


@pytest.fixture
def mock_redis():
    """Mock raw Redis client for admin config access."""
    return Mock()


@pytest.fixture
def service(mock_venue_dao, mock_besttime_api, mock_redis):
    """Create VenuesRefresherService with all mocks."""
    return VenuesRefresherService(
        mock_venue_dao,
        mock_besttime_api,
        redis_client=mock_redis,
    )


@pytest.fixture
def service_no_redis(mock_venue_dao, mock_besttime_api):
    """Service without Redis client (fallback scenario)."""
    return VenuesRefresherService(mock_venue_dao, mock_besttime_api)


# ===========================================================================
# _get_discovery_points()
# ===========================================================================

class TestGetDiscoveryPoints:

    def test_returns_points_from_redis(self, service, mock_redis):
        points = _make_points()
        mock_redis.get.return_value = json.dumps({"points": points})

        result = service._get_discovery_points()

        assert len(result) == 3
        assert result[0]["id"] == "recife-zs-zn"
        mock_redis.get.assert_called_once_with(
            VenuesRefresherService.ADMIN_CONFIG_DISCOVERY_POINTS_KEY
        )

    def test_returns_empty_when_key_missing(self, service, mock_redis):
        mock_redis.get.return_value = None

        result = service._get_discovery_points()

        assert result == []

    def test_returns_empty_when_no_redis_client(self, service_no_redis):
        result = service_no_redis._get_discovery_points()

        assert result == []

    def test_returns_empty_on_redis_error(self, service, mock_redis):
        mock_redis.get.side_effect = Exception("Connection refused")

        result = service._get_discovery_points()

        assert result == []

    def test_returns_empty_on_invalid_json(self, service, mock_redis):
        mock_redis.get.return_value = "not-valid-json{{"

        result = service._get_discovery_points()

        assert result == []

    def test_returns_empty_when_no_points_key(self, service, mock_redis):
        mock_redis.get.return_value = json.dumps({"something_else": True})

        result = service._get_discovery_points()

        assert result == []


# ===========================================================================
# _save_discovery_points()
# ===========================================================================

class TestSaveDiscoveryPoints:

    def test_saves_to_redis(self, service, mock_redis):
        points = _make_points()
        service._save_discovery_points(points)

        mock_redis.set.assert_called_once()
        key, value = mock_redis.set.call_args[0]
        assert key == VenuesRefresherService.ADMIN_CONFIG_DISCOVERY_POINTS_KEY
        parsed = json.loads(value)
        assert len(parsed["points"]) == 3
        assert parsed["points"][0]["id"] == "recife-zs-zn"

    def test_noop_when_no_redis_client(self, service_no_redis):
        # Should not raise
        service_no_redis._save_discovery_points(_make_points())

    def test_handles_redis_error(self, service, mock_redis):
        mock_redis.set.side_effect = Exception("Write error")
        # Should not raise
        service._save_discovery_points(_make_points())


# ===========================================================================
# recount_discovery_points()
# ===========================================================================

class TestRecountDiscoveryPoints:

    def test_recount_updates_current_values(self, service, mock_redis, mock_venue_dao):
        points = _make_points()
        mock_redis.get.return_value = json.dumps({"points": points})

        # Simulate GEORADIUS counts: 360, 500, 120
        mock_venue_dao.count_venues_in_radius.side_effect = [360, 500, 120]

        result = service.recount_discovery_points()

        assert len(result) == 3
        assert result[0]["current"] == 360
        assert result[1]["current"] == 500
        assert result[2]["current"] == 120

        # Verify correct calls to count_venues_in_radius
        calls = mock_venue_dao.count_venues_in_radius.call_args_list
        assert calls[0].args == (-8.07834, -34.90938, 15000.0)
        assert calls[1].args == (-7.99081, -34.85141, 15000.0)
        assert calls[2].args == (-15.8267, -47.9218, 8000.0)

    def test_recount_saves_back_to_redis(self, service, mock_redis, mock_venue_dao):
        points = [{"id": "p1", "lat": -8.0, "lng": -34.0, "radius": 5000, "limit": 100, "current": 0}]
        mock_redis.get.return_value = json.dumps({"points": points})
        mock_venue_dao.count_venues_in_radius.return_value = 42

        service.recount_discovery_points()

        # Should have saved updated points
        mock_redis.set.assert_called_once()
        saved = json.loads(mock_redis.set.call_args[0][1])
        assert saved["points"][0]["current"] == 42

    def test_recount_returns_empty_when_no_points(self, service, mock_redis):
        mock_redis.get.return_value = None

        result = service.recount_discovery_points()

        assert result == []


# ===========================================================================
# _refresh_with_discovery_points()
# ===========================================================================

class TestRefreshWithDiscoveryPoints:

    @pytest.mark.asyncio
    async def test_skips_saturated_points(self, service, mock_besttime_api, mock_redis):
        """Points at limit should be skipped entirely."""
        points = [
            {"id": "full", "lat": -8.0, "lng": -34.0, "radius": 5000, "limit": 100, "current": 100},
            {"id": "over", "lat": -8.1, "lng": -34.1, "radius": 5000, "limit": 100, "current": 120},
        ]

        total = await service._refresh_with_discovery_points(points, -1, False)

        assert total == 0
        mock_besttime_api.venue_filter.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetches_headroom_amount(self, service, mock_besttime_api, mock_redis):
        """Should fetch up to headroom = limit - current."""
        points = [
            {"id": "p1", "lat": -8.0, "lng": -34.0, "radius": 5000, "limit": 500, "current": 350},
        ]
        mock_besttime_api.venue_filter.return_value = _make_filter_response(count=5)

        total = await service._refresh_with_discovery_points(points, -1, False)

        assert total == 5
        # Check that the limit passed to venue_filter respects headroom (150)
        call_args = mock_besttime_api.venue_filter.call_args[0][0]
        assert call_args.limit == 150  # headroom = 500 - 350

    @pytest.mark.asyncio
    async def test_updates_counter_after_fetch(self, service, mock_besttime_api, mock_redis):
        """Counter should be incremented by fetched count."""
        points = [
            {"id": "p1", "lat": -8.0, "lng": -34.0, "radius": 5000, "limit": 500, "current": 350},
        ]
        mock_besttime_api.venue_filter.return_value = _make_filter_response(count=3)

        await service._refresh_with_discovery_points(points, -1, False)

        assert points[0]["current"] == 353  # 350 + 3

    @pytest.mark.asyncio
    async def test_saves_points_after_updates(self, service, mock_besttime_api, mock_redis):
        """Updated counters should be saved back to Redis."""
        points = [
            {"id": "p1", "lat": -8.0, "lng": -34.0, "radius": 5000, "limit": 500, "current": 0},
        ]
        mock_besttime_api.venue_filter.return_value = _make_filter_response(count=2)

        await service._refresh_with_discovery_points(points, -1, False)

        mock_redis.set.assert_called_once()
        saved = json.loads(mock_redis.set.call_args[0][1])
        assert saved["points"][0]["current"] == 2

    @pytest.mark.asyncio
    async def test_respects_global_budget(self, service, mock_besttime_api, mock_redis):
        """Global budget should cap how many venues are fetched."""
        points = [
            {"id": "p1", "lat": -8.0, "lng": -34.0, "radius": 5000, "limit": 500, "current": 0},
            {"id": "p2", "lat": -8.1, "lng": -34.1, "radius": 5000, "limit": 500, "current": 0},
        ]
        # First call returns 3 venues; budget starts at 5
        mock_besttime_api.venue_filter.side_effect = [
            _make_filter_response(count=3),
            _make_filter_response(count=2),
        ]

        total = await service._refresh_with_discovery_points(points, remaining_budget=5, fetch_and_cache_live=False)

        assert total == 5
        # First point should get limit=5 (min of headroom=500, budget=5)
        first_call = mock_besttime_api.venue_filter.call_args_list[0][0][0]
        assert first_call.limit == 5
        # Second point should get limit=2 (budget=5-3=2)
        second_call = mock_besttime_api.venue_filter.call_args_list[1][0][0]
        assert second_call.limit == 2

    @pytest.mark.asyncio
    async def test_stops_when_budget_exhausted(self, service, mock_besttime_api, mock_redis):
        """Should stop processing points when global budget is 0."""
        points = [
            {"id": "p1", "lat": -8.0, "lng": -34.0, "radius": 5000, "limit": 500, "current": 0},
            {"id": "p2", "lat": -8.1, "lng": -34.1, "radius": 5000, "limit": 500, "current": 0},
        ]
        mock_besttime_api.venue_filter.return_value = _make_filter_response(count=5)

        total = await service._refresh_with_discovery_points(points, remaining_budget=5, fetch_and_cache_live=False)

        # Only first point should be called, budget exhausted after
        assert mock_besttime_api.venue_filter.call_count == 1
        assert total == 5

    @pytest.mark.asyncio
    async def test_respects_limit_override(self, service, mock_besttime_api, mock_redis):
        """fetch_venue_limit_override should cap per-point fetch."""
        service.fetch_venue_limit_override = 10
        points = [
            {"id": "p1", "lat": -8.0, "lng": -34.0, "radius": 5000, "limit": 500, "current": 0},
        ]
        mock_besttime_api.venue_filter.return_value = _make_filter_response(count=5)

        await service._refresh_with_discovery_points(points, -1, False)

        call_args = mock_besttime_api.venue_filter.call_args[0][0]
        assert call_args.limit == 10  # min(headroom=500, override=10)

    @pytest.mark.asyncio
    async def test_handles_api_error_gracefully(self, service, mock_besttime_api, mock_redis):
        """API errors for one point should not stop processing others."""
        points = [
            {"id": "p1", "lat": -8.0, "lng": -34.0, "radius": 5000, "limit": 500, "current": 0},
            {"id": "p2", "lat": -8.1, "lng": -34.1, "radius": 5000, "limit": 500, "current": 0},
        ]
        mock_besttime_api.venue_filter.side_effect = [
            Exception("API error"),
            _make_filter_response(count=3),
        ]

        total = await service._refresh_with_discovery_points(points, -1, False)

        assert total == 3
        assert points[0]["current"] == 0  # Failed point not updated
        assert points[1]["current"] == 3

    @pytest.mark.asyncio
    async def test_mixed_saturated_and_available(self, service, mock_besttime_api, mock_redis):
        """Mix of full and available points processes only available ones."""
        points = [
            {"id": "full1", "lat": -8.0, "lng": -34.0, "radius": 5000, "limit": 100, "current": 100},
            {"id": "avail", "lat": -8.1, "lng": -34.1, "radius": 5000, "limit": 300, "current": 200},
            {"id": "full2", "lat": -8.2, "lng": -34.2, "radius": 5000, "limit": 50, "current": 80},
        ]
        mock_besttime_api.venue_filter.return_value = _make_filter_response(count=4)

        total = await service._refresh_with_discovery_points(points, -1, False)

        assert total == 4
        assert mock_besttime_api.venue_filter.call_count == 1
        call_args = mock_besttime_api.venue_filter.call_args[0][0]
        assert call_args.limit == 100  # headroom for "avail" = 300 - 200


# ===========================================================================
# refresh_venues_by_filter_for_default_locations() — routing logic
# ===========================================================================

class TestRefreshRoutingLogic:

    @pytest.mark.asyncio
    async def test_uses_discovery_points_when_available(self, service, mock_redis, mock_besttime_api):
        """When admin config has discovery points, use them instead of defaults."""
        points = [
            {"id": "custom", "lat": -15.0, "lng": -47.0, "radius": 10000, "limit": 200, "current": 0},
        ]
        mock_redis.get.return_value = json.dumps({"points": points})
        mock_besttime_api.venue_filter.return_value = _make_filter_response(count=2)

        await service.refresh_venues_by_filter_for_default_locations()

        # Should call API with the custom point's lat/lng
        call_args = mock_besttime_api.venue_filter.call_args[0][0]
        assert call_args.lat == -15.0
        assert call_args.lng == -47.0

    @pytest.mark.asyncio
    async def test_falls_back_to_default_locations(self, service, mock_redis, mock_besttime_api):
        """When no discovery points, fall back to DEFAULT_LOCATIONS."""
        mock_redis.get.return_value = None
        mock_besttime_api.venue_filter.return_value = _make_filter_response(count=1)

        await service.refresh_venues_by_filter_for_default_locations()

        # Should have called venue_filter for each DEFAULT_LOCATION (3 calls)
        assert mock_besttime_api.venue_filter.call_count == len(DEFAULT_LOCATIONS)

    @pytest.mark.asyncio
    async def test_dev_mode_ignores_discovery_points(self, mock_venue_dao, mock_besttime_api, mock_redis):
        """Dev mode should use single location, not discovery points."""
        svc = VenuesRefresherService(
            mock_venue_dao, mock_besttime_api,
            redis_client=mock_redis,
            dev_mode=True, dev_lat=-15.0, dev_lng=-47.0, dev_radius=3000,
        )
        # Even with discovery points configured, dev mode should not use them
        mock_redis.get.return_value = json.dumps({"points": _make_points()})
        mock_besttime_api.venue_filter.return_value = _make_filter_response(count=1)

        await svc.refresh_venues_by_filter_for_default_locations()

        # Should call only once with dev coordinates
        assert mock_besttime_api.venue_filter.call_count == 1
        call_args = mock_besttime_api.venue_filter.call_args[0][0]
        assert call_args.lat == -15.0
        assert call_args.lng == -47.0
        assert call_args.radius == 3000

    @pytest.mark.asyncio
    async def test_total_limit_zero_skips_everything(self, mock_venue_dao, mock_besttime_api, mock_redis):
        """fetch_venue_total_limit=0 should skip all fetching."""
        svc = VenuesRefresherService(
            mock_venue_dao, mock_besttime_api,
            redis_client=mock_redis,
            fetch_venue_total_limit=0,
        )

        await svc.refresh_venues_by_filter_for_default_locations()

        mock_besttime_api.venue_filter.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_when_no_redis_client(self, mock_venue_dao, mock_besttime_api):
        """Service without redis_client should fall back to DEFAULT_LOCATIONS."""
        svc = VenuesRefresherService(mock_venue_dao, mock_besttime_api, redis_client=None)
        mock_besttime_api.venue_filter.return_value = _make_filter_response(count=1)

        await svc.refresh_venues_by_filter_for_default_locations()

        assert mock_besttime_api.venue_filter.call_count == len(DEFAULT_LOCATIONS)


# ===========================================================================
# Admin trigger router — recount endpoint
# ===========================================================================

class TestAdminTriggerRecountEndpoint:

    def test_recount_endpoint_calls_service(self):
        """Test that recount endpoint delegates to service.recount_discovery_points()."""
        from app.routers.admin_trigger_router import recount_discovery_points, set_container

        mock_container = Mock()
        mock_container.venues_refresher_service.recount_discovery_points.return_value = [
            {"id": "p1", "current": 42}
        ]
        set_container(mock_container)

        import asyncio
        result = asyncio.get_event_loop().run_until_complete(recount_discovery_points())

        assert result["status"] == "ok"
        assert len(result["points"]) == 1
        assert result["points"][0]["current"] == 42
        mock_container.venues_refresher_service.recount_discovery_points.assert_called_once()

    def test_recount_endpoint_503_without_container(self):
        """Test that recount returns 503 when container not initialized."""
        import sys
        trigger_mod = sys.modules["app.routers.admin_trigger_router"]
        from fastapi import HTTPException

        # Reset container
        trigger_mod._container = None

        import asyncio
        with pytest.raises(HTTPException) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                trigger_mod.recount_discovery_points()
            )
        assert exc_info.value.status_code == 503
