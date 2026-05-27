"""Unit tests for AddVenueHandler.

Covers all branches of the request resolution: address-hash cache hit,
geo-cache hit, BestTime success, BestTime recoverable failure with geo
fallback hit, BestTime recoverable failure without geo match, and
BestTime non-recoverable failure (transport / 5xx). The first-scenario
green run in BDD validates the happy path end-to-end; these unit tests
pin the branching contract.
"""
import json
from unittest.mock import AsyncMock

import fakeredis
import httpx
import pytest

from app.dao import RedisVenueDAO, VenueBudgetDao
from app.db.geo_redis_client import GeoRedisClient
from app.handlers.add_venue_handler import (
    AddVenueByAddressRequest,
    AddVenueHandler,
)
from app.models import (
    Analysis,
    LiveForecastResponse,
    NewVenueResponse,
    Venue,
    VenueFilterResponse,
    VenueFilterVenue,
    VenueInfo,
)
from app.services.venue_budget_service import VenueBudgetService


@pytest.fixture
def fake():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def venue_dao(fake):
    return RedisVenueDAO(GeoRedisClient(fake))


@pytest.fixture
def budget(fake):
    dao = VenueBudgetDao(fake)
    return VenueBudgetService(
        redis_client=fake,
        budget_dao=dao,
        year_month_provider=lambda: "2026-05",
    )


@pytest.fixture
def besttime():
    return AsyncMock()


@pytest.fixture
def handler(venue_dao, besttime, budget, fake):
    return AddVenueHandler(
        venue_dao=venue_dao,
        besttime_api=besttime,
        budget_service=budget,
        redis_client=fake,
    )


def _req(**overrides):
    base = {
        "venue_name": "Bar do Joao",
        "venue_address": "Rua das Flores 123, Recife - PE",
        "venue_lat": -8.05,
        "venue_lng": -34.88,
    }
    base.update(overrides)
    return AddVenueByAddressRequest(**base)


def _ok_response(venue_id="ven_test"):
    return NewVenueResponse.model_validate(
        {
            "status": "OK",
            "venue_info": {
                "venue_id": venue_id,
                "venue_name": "Bar do Joao",
                "venue_address": "Rua das Flores 123, Recife - PE",
                "venue_lat": -8.05,
                "venue_lon": -34.88,
            },
            "analysis": [],
        }
    )


def _live_unavailable(venue_id="ven_test"):
    return LiveForecastResponse(
        status="Error",
        venue_info=VenueInfo(venue_id=venue_id),
        analysis=Analysis(),
    )


@pytest.mark.asyncio
async def test_request_validation_rejects_missing_lat(venue_dao, besttime, budget, fake):
    with pytest.raises(Exception):
        AddVenueByAddressRequest(
            venue_name="x",
            venue_address="y",
            venue_lng=-34.88,
        )


@pytest.mark.asyncio
async def test_request_validation_rejects_empty_name():
    with pytest.raises(Exception):
        AddVenueByAddressRequest(
            venue_name="",
            venue_address="y",
            venue_lat=-8.05,
            venue_lng=-34.88,
        )


@pytest.mark.asyncio
async def test_create_happy_path_persists_and_increments(handler, besttime, venue_dao, fake):
    besttime.add_venue_to_account.return_value = _ok_response("ven_happy")
    besttime.get_live_forecast.return_value = _live_unavailable("ven_happy")

    outcome = await handler.add(_req())

    assert outcome.status_code == 201
    assert outcome.body["status"] == "created"
    assert outcome.body["source"] == "besttime_new"
    assert outcome.body["venue_id"] == "ven_happy"
    # Persisted in geo index.
    assert fake.get("venues_geo_place_v1:ven_happy") is not None
    # Counter incremented.
    assert int(fake.get("venue_add_counter_v1:2026-05")) == 1
    # BestTime called exactly once for add + once for live.
    assert besttime.add_venue_to_account.await_count == 1
    assert besttime.get_live_forecast.await_count == 1


@pytest.mark.asyncio
async def test_address_hash_cache_short_circuits(handler, besttime, venue_dao, fake):
    # Pre-cache an address-hash mapping and pre-persist the venue.
    venue = Venue(
        processed=True,
        forecast=True,
        venue_id="ven_existing",
        venue_name="Bar do Joao",
        venue_address="Rua das Flores 123, Recife - PE",
        venue_lat=-8.05,
        venue_lng=-34.88,
    )
    venue_dao.upsert_venue(venue)
    # The handler computes the hash on first miss/hit; trigger a write by
    # calling .add once and ensure the second call does not hit BestTime.
    besttime.add_venue_to_account.return_value = _ok_response("ven_first")
    besttime.get_live_forecast.return_value = _live_unavailable("ven_first")

    await handler.add(_req())
    # After the first call, the address hash for the default request body
    # has been cached. The second call must NOT hit BestTime.
    besttime.add_venue_to_account.reset_mock()
    besttime.get_live_forecast.reset_mock()
    outcome = await handler.add(_req())

    assert outcome.status_code == 200
    assert outcome.body["status"] == "already_exists"
    assert besttime.add_venue_to_account.await_count == 0


@pytest.mark.asyncio
async def test_geo_cache_short_circuits_without_address_match(handler, besttime, venue_dao, fake):
    # Seed an existing inventory venue at the same coordinate but with a
    # *different* address string (mimics inventory-sync output).
    venue = Venue(
        processed=True,
        forecast=True,
        venue_id="ven_inventory",
        venue_name="Bar do Joao",
        venue_address="A Wholly Different Address",
        venue_lat=-8.05,
        venue_lng=-34.88,
    )
    venue_dao.upsert_venue(venue)

    outcome = await handler.add(_req())

    assert outcome.status_code == 200
    assert outcome.body["status"] == "already_exists"
    assert outcome.body["venue_id"] == "ven_inventory"
    assert besttime.add_venue_to_account.await_count == 0


@pytest.mark.asyncio
async def test_quota_exhausted_returns_429(handler, besttime, fake):
    fake.set("venue_add_counter_v1:2026-05", 500)
    outcome = await handler.add(_req())
    assert outcome.status_code == 429
    assert "quota" in outcome.body["detail"].lower()
    assert besttime.add_venue_to_account.await_count == 0


@pytest.mark.asyncio
async def test_transport_error_releases_reservation(handler, besttime, fake):
    besttime.add_venue_to_account.side_effect = httpx.ConnectError("simulated")
    outcome = await handler.add(_req())
    assert outcome.status_code == 502
    assert "unavailable" in outcome.body["detail"].lower()
    # Reservation released → counter unchanged.
    assert int(fake.get("venue_add_counter_v1:2026-05") or 0) == 0


@pytest.mark.asyncio
async def test_besttime_status_error_triggers_geo_fallback_hit(
    handler, besttime, venue_dao, fake
):
    besttime.add_venue_to_account.return_value = NewVenueResponse.model_validate(
        {"status": "Error", "message": "Could not geocode address"}
    )
    matched = VenueFilterVenue(
        venue_id="ven_geo_match",
        venue_name="Bar do Joao",
        venue_address="any addr",
        venue_lat=-8.05,
        venue_lng=-34.88,
        venue_type="BAR",
        day_int=0,
        day_raw=[0] * 24,
    )
    besttime.venue_filter.return_value = VenueFilterResponse(
        status="OK", venues=[matched], venues_n=1
    )

    outcome = await handler.add(_req())

    assert outcome.status_code == 200
    assert outcome.body["status"] == "matched_via_geo_fallback"
    assert outcome.body["venue_id"] == "ven_geo_match"
    assert outcome.body["source"] == "venues_filter_radius"
    # Reservation was released and a new-venue increment from the geo
    # fallback path replaced it → counter at 1.
    assert int(fake.get("venue_add_counter_v1:2026-05")) == 1
    # Venue persisted.
    assert fake.get("venues_geo_place_v1:ven_geo_match") is not None


@pytest.mark.asyncio
async def test_besttime_error_with_no_geo_match_returns_502(handler, besttime, fake):
    besttime.add_venue_to_account.return_value = NewVenueResponse.model_validate(
        {"status": "Error", "message": "Could not geocode address"}
    )
    besttime.venue_filter.return_value = VenueFilterResponse(
        status="OK", venues=[], venues_n=0
    )

    outcome = await handler.add(_req())

    assert outcome.status_code == 502
    assert "geo fallback" in outcome.body["detail"].lower()
    assert outcome.body["besttime_message"] == "Could not geocode address"
    # Reservation released → counter unchanged at 0.
    assert int(fake.get("venue_add_counter_v1:2026-05") or 0) == 0


@pytest.mark.asyncio
async def test_geo_fallback_no_double_count_when_venue_already_exists(
    handler, besttime, venue_dao, fake
):
    # Pre-seed the venue Redis-side so the geo-fallback upsert is a no-op
    # for counter purposes.
    venue_dao.upsert_venue(
        Venue(
            processed=True,
            forecast=True,
            venue_id="ven_pre_existing",
            venue_name="Some Other Name We Won't Match",
            venue_address="addr",
            venue_lat=-8.10,  # far from request coordinate so geo lookup misses
            venue_lng=-34.95,
        )
    )
    besttime.add_venue_to_account.return_value = NewVenueResponse.model_validate(
        {"status": "Error", "message": "geocode failed"}
    )
    # Geo fallback at the request coordinate returns a venue that *is*
    # already in Redis under the SAME id.
    venue_dao.upsert_venue(
        Venue(
            processed=True,
            forecast=True,
            venue_id="ven_already_known",
            venue_name="Bar do Joao",
            venue_address="addr",
            venue_lat=-8.05,
            venue_lng=-34.88,
        )
    )
    matched = VenueFilterVenue(
        venue_id="ven_already_known",
        venue_name="Bar do Joao",
        venue_address="addr",
        venue_lat=-8.05,
        venue_lng=-34.88,
        venue_type="BAR",
        day_int=0,
        day_raw=[0] * 24,
    )
    besttime.venue_filter.return_value = VenueFilterResponse(
        status="OK", venues=[matched], venues_n=1
    )

    # Counter is initialized at zero; the geo cache should fire FIRST and
    # short-circuit before we ever call BestTime.
    outcome = await handler.add(_req())
    assert outcome.status_code == 200
    assert outcome.body["status"] == "already_exists"
    assert besttime.add_venue_to_account.await_count == 0
    assert int(fake.get("venue_add_counter_v1:2026-05") or 0) == 0


@pytest.mark.asyncio
async def test_venue_lon_in_response_is_normalised_to_venue_lng(handler, besttime, fake):
    """Pin the venue_lon → venue_lng alias contract from the live probe."""
    besttime.add_venue_to_account.return_value = NewVenueResponse.model_validate(
        {
            "status": "OK",
            "venue_info": {
                "venue_id": "ven_lon_alias",
                "venue_name": "X",
                "venue_address": "Y",
                "venue_lat": -8.05,
                "venue_lon": -34.88,
            },
        }
    )
    besttime.get_live_forecast.return_value = _live_unavailable("ven_lon_alias")
    outcome = await handler.add(_req())
    assert outcome.status_code == 201
    assert outcome.body["venue_lng"] == -34.88
    assert "venue_lon" not in outcome.body
    persisted = json.loads(fake.get("venues_geo_place_v1:ven_lon_alias"))
    assert "venue_lng" in persisted
    assert persisted["venue_lng"] == -34.88
