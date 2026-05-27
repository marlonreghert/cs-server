"""Unit tests for the BestTime account inventory sync + new-venue helpers."""
import json

import fakeredis
import httpx
import pytest
import respx

from app.api import BestTimeAPIClient
from app.dao import RedisVenueDAO, VenueBudgetDao
from app.db.geo_redis_client import GeoRedisClient
from app.models import (
    AccountInventoryVenue,
    NewVenueResponse,
    Venue,
)
from app.services.venue_budget_service import VenueBudgetService
from app.services.venues_refresher_service import VenuesRefresherService


# ---------------------------------------------------------------------------
# BestTime client model parsing — covers the live-probe schema findings
# ---------------------------------------------------------------------------


class TestNewVenueResponseModel:
    def test_parses_venue_lon_into_venue_lng_alias(self):
        body = {
            "status": "OK",
            "venue_info": {
                "venue_id": "v_abc",
                "venue_lat": -8.0,
                "venue_lon": -34.9,  # BestTime spelling on /forecasts
            },
        }
        parsed = NewVenueResponse.model_validate(body)
        assert parsed.is_ok()
        assert parsed.venue_info.venue_lng == -34.9

    def test_parses_venue_lng_directly_too(self):
        body = {
            "status": "OK",
            "venue_info": {
                "venue_id": "v_abc",
                "venue_lat": -8.0,
                "venue_lng": -34.9,
            },
        }
        parsed = NewVenueResponse.model_validate(body)
        assert parsed.venue_info.venue_lng == -34.9

    def test_status_error_with_message_does_not_raise(self):
        body = {
            "status": "Error",
            "message": "Max amount of monthly venues (500) reached",
        }
        parsed = NewVenueResponse.model_validate(body)
        assert not parsed.is_ok()
        assert parsed.message.startswith("Max amount")

    def test_ok_without_venue_info_is_not_ok(self):
        # Pydantic enforces venue_id at the venue_info level. The
        # is_ok() contract therefore only treats responses with a
        # populated venue_info containing a venue_id as success.
        parsed = NewVenueResponse.model_validate({"status": "OK"})
        assert not parsed.is_ok()


# ---------------------------------------------------------------------------
# BestTime client.add_venue_to_account against captured fixtures (respx)
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return BestTimeAPIClient(
        base_url="https://besttime.app/api/v1",
        api_key_public="pub",
        api_key_private="priv",
        timeout=5.0,
    )


@pytest.mark.asyncio
@respx.mock
async def test_add_venue_handles_recoverable_400_error(client):
    """Mirrors the 'monthly cap exceeded' fixture captured live."""
    respx.post("https://besttime.app/api/v1/forecasts").mock(
        return_value=httpx.Response(
            400,
            json={
                "status": "Error",
                "message": "Error: Max amount of monthly venues (500) reached.",
            },
        )
    )
    result = await client.add_venue_to_account("X", "Y")
    assert not result.is_ok()
    assert "Max amount" in result.message
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_add_venue_5xx_raises(client):
    respx.post("https://besttime.app/api/v1/forecasts").mock(
        return_value=httpx.Response(500, json={"status": "Error"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.add_venue_to_account("X", "Y")
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_add_venue_transport_error_raises(client):
    respx.post("https://besttime.app/api/v1/forecasts").mock(
        side_effect=httpx.ConnectError("boom")
    )
    with pytest.raises(httpx.ConnectError):
        await client.add_venue_to_account("X", "Y")
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_add_venue_success_parses_venue_lon(client):
    respx.post("https://besttime.app/api/v1/forecasts").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "OK",
                "venue_info": {
                    "venue_id": "ven_fresh_001",
                    "venue_name": "Bar do Joao",
                    "venue_address": "Rua das Flores 123",
                    "venue_lat": -8.05,
                    "venue_lon": -34.88,
                },
                "analysis": [],
            },
        )
    )
    result = await client.add_venue_to_account("Bar do Joao", "Rua das Flores 123")
    assert result.is_ok()
    assert result.venue_info.venue_id == "ven_fresh_001"
    assert result.venue_info.venue_lng == -34.88
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_list_account_inventory_paginates(client):
    page0 = [
        {"venue_id": f"v{i}", "venue_name": f"V{i}", "venue_lat": 0.0, "venue_lng": 0.0}
        for i in range(1000)
    ]
    page1 = [
        {"venue_id": f"v{i}", "venue_name": f"V{i}", "venue_lat": 0.0, "venue_lng": 0.0}
        for i in range(1000, 1330)
    ]
    route = respx.get("https://besttime.app/api/v1/venues")
    route.side_effect = [
        httpx.Response(200, json=page0),
        httpx.Response(200, json=page1),
    ]

    seen = []
    async for v in client.list_account_inventory(page_size=1000):
        seen.append(v)

    assert len(seen) == 1330
    assert isinstance(seen[0], AccountInventoryVenue)
    assert route.call_count == 2
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_list_account_inventory_stops_on_short_page(client):
    respx.get("https://besttime.app/api/v1/venues").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"venue_id": "v1", "venue_lat": 0.0, "venue_lng": 0.0},
                {"venue_id": "v2", "venue_lat": 0.0, "venue_lng": 0.0},
            ],
        )
    )
    seen = [v async for v in client.list_account_inventory(page_size=1000)]
    assert len(seen) == 2
    await client.close()


# ---------------------------------------------------------------------------
# Inventory sync + discovery cap integration on the refresher
# ---------------------------------------------------------------------------


class _StubBesttime:
    """Minimal async stub for VenuesRefresherService dependencies."""

    def __init__(self, inventory_pages=None, filter_response=None):
        self.inventory_pages = inventory_pages or []
        self.filter_response = filter_response
        self.calls = []

    async def list_account_inventory(self, page_size: int = 1000):
        self.calls.append("list_account_inventory")
        for page in self.inventory_pages:
            for v in page:
                yield v

    async def venue_filter(self, params):
        self.calls.append(("venue_filter", params))
        return self.filter_response


@pytest.fixture
def refresher_pair():
    fake = fakeredis.FakeRedis(decode_responses=True)
    venue_dao = RedisVenueDAO(GeoRedisClient(fake))
    budget_dao = VenueBudgetDao(fake)
    budget = VenueBudgetService(
        redis_client=fake,
        budget_dao=budget_dao,
        year_month_provider=lambda: "2026-05",
    )
    return fake, venue_dao, budget


@pytest.mark.asyncio
async def test_sync_account_inventory_skips_existing_and_upserts_missing(refresher_pair):
    fake, venue_dao, _budget = refresher_pair
    pages = [
        [
            AccountInventoryVenue(
                venue_id="v_present",
                venue_name="Already Here",
                venue_address="addr",
                venue_lat=-8.0,
                venue_lng=-34.9,
            ),
            AccountInventoryVenue(
                venue_id="v_missing",
                venue_name="New Inventory",
                venue_address="addr2",
                venue_lat=-8.1,
                venue_lng=-34.95,
            ),
        ]
    ]
    venue_dao.upsert_venue(
        Venue(
            processed=True,
            forecast=True,
            venue_id="v_present",
            venue_name="Already Here",
            venue_address="addr",
            venue_lat=-8.0,
            venue_lng=-34.9,
        )
    )

    refresher = VenuesRefresherService(
        venue_dao=venue_dao,
        besttime_api=_StubBesttime(inventory_pages=pages),
        redis_client=fake,
    )
    summary = await refresher.sync_account_inventory_to_redis()
    assert summary == {"seen": 2, "upserted": 1, "skipped": 1, "errors": 0}
    assert fake.get("venues_geo_place_v1:v_missing") is not None


@pytest.mark.asyncio
async def test_sync_does_not_increment_monthly_counter(refresher_pair):
    fake, venue_dao, budget = refresher_pair
    pages = [
        [
            AccountInventoryVenue(
                venue_id="v_new",
                venue_name="New",
                venue_address="a",
                venue_lat=-8.0,
                venue_lng=-34.9,
            )
        ]
    ]
    refresher = VenuesRefresherService(
        venue_dao=venue_dao,
        besttime_api=_StubBesttime(inventory_pages=pages),
        redis_client=fake,
    )
    refresher.set_budget_service(budget)
    await refresher.sync_account_inventory_to_redis()
    assert fake.get("venue_add_counter_v1:2026-05") is None


@pytest.mark.asyncio
async def test_sync_continues_on_per_venue_error(refresher_pair):
    fake, venue_dao, _budget = refresher_pair
    # First row is malformed (no venue_id) → counted as error; second is fine.
    pages = [
        [
            AccountInventoryVenue(venue_id="", venue_name="bad"),
            AccountInventoryVenue(
                venue_id="v_ok",
                venue_name="ok",
                venue_address="a",
                venue_lat=0.0,
                venue_lng=0.0,
            ),
        ]
    ]
    refresher = VenuesRefresherService(
        venue_dao=venue_dao,
        besttime_api=_StubBesttime(inventory_pages=pages),
        redis_client=fake,
    )
    summary = await refresher.sync_account_inventory_to_redis()
    assert summary["seen"] == 2
    assert summary["errors"] == 1
    assert summary["upserted"] == 1
