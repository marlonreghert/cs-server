"""Unit tests for AddVenueHandler.

Covers all branches of the request resolution: address-hash cache hit,
geo-cache hit, BestTime success, BestTime recoverable failure with geo
fallback hit, BestTime recoverable failure without geo match, and
BestTime non-recoverable failure (transport / 5xx). The first-scenario
green run in BDD validates the happy path end-to-end; these unit tests
pin the branching contract.
"""
import json
from datetime import datetime, timedelta, timezone
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
    # BestTime called exactly once for the add — and NEVER for live busyness:
    # live retrieval spends credits and belongs to the live pipeline only.
    assert besttime.add_venue_to_account.await_count == 1
    assert besttime.get_live_forecast.await_count == 0


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
async def test_besttime_monthly_cap_surfaced_not_laundered(handler, besttime, fake):
    # BestTime's real monthly-cap rejection must be surfaced clearly, not routed
    # through the geo fallback into a misleading "rejected the address".
    besttime.add_venue_to_account.return_value = NewVenueResponse.model_validate(
        {
            "status": "Error",
            "message": "Max amount of monthly venues (500) reached. Venue counter "
            "will reset at midnight on the first day of the month.",
        }
    )
    outcome = await handler.add(_req())
    assert outcome.status_code == 429
    assert "cap" in outcome.body["detail"].lower()
    assert "monthly venues" in outcome.body["besttime_message"].lower()
    # Never attempts the geo fallback for a cap rejection.
    assert besttime.venue_filter.await_count == 0
    # Reservation released → local counter unchanged.
    assert int(fake.get("venue_add_counter_v1:2026-05") or 0) == 0


@pytest.mark.asyncio
async def test_successful_add_marks_ledger(handler, besttime, budget, fake):
    besttime.add_venue_to_account.return_value = _ok_response("ven_marked")
    besttime.get_live_forecast.return_value = _live_unavailable("ven_marked")
    await handler.add(_req())
    # The new venue is recorded against the monthly unique-venue ledger so a
    # later refresh re-read is free and the backstop counts it.
    assert budget.unique_touched_count() == 1
    assert fake.sismember("besttime_touched_v1:2026-05", "ven_marked")


@pytest.mark.asyncio
async def test_transport_error_releases_reservation(handler, besttime, fake):
    besttime.add_venue_to_account.side_effect = httpx.ConnectError("simulated")
    outcome = await handler.add(_req())
    assert outcome.status_code == 502
    assert "unavailable" in outcome.body["detail"].lower()
    # Reservation released → counter unchanged.
    assert int(fake.get("venue_add_counter_v1:2026-05") or 0) == 0


def _add_venue_result_metric(result: str) -> float:
    from prometheus_client import REGISTRY

    return (
        REGISTRY.get_sample_value("add_venue_by_address_total", {"result": result})
        or 0.0
    )


@pytest.mark.asyncio
async def test_bad_response_returns_honest_502_and_releases_slot(
    handler, besttime, fake
):
    """A response-schema failure must not masquerade as a BestTime outage."""
    from app.api.besttime_client import BestTimeInvalidResponseError

    besttime.add_venue_to_account.side_effect = BestTimeInvalidResponseError(
        "unparseable POST /forecasts response envelope"
    )
    bad_before = _add_venue_result_metric("besttime_bad_response")
    error_before = _add_venue_result_metric("besttime_error")

    outcome = await handler.add(_req())

    assert outcome.status_code == 502
    assert "unparseable" in outcome.body["detail"].lower()
    assert "unavailable" not in outcome.body["detail"].lower()
    assert _add_venue_result_metric("besttime_bad_response") - bad_before == 1
    assert _add_venue_result_metric("besttime_error") - error_before == 0
    # Reservation released → counter unchanged.
    assert int(fake.get("venue_add_counter_v1:2026-05") or 0) == 0


@pytest.mark.asyncio
async def test_transport_error_keeps_besttime_error_metric(handler, besttime, fake):
    besttime.add_venue_to_account.side_effect = httpx.ConnectError("simulated")
    bad_before = _add_venue_result_metric("besttime_bad_response")
    error_before = _add_venue_result_metric("besttime_error")

    outcome = await handler.add(_req())

    assert outcome.status_code == 502
    assert _add_venue_result_metric("besttime_error") - error_before == 1
    assert _add_venue_result_metric("besttime_bad_response") - bad_before == 0


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


# ── add-time Google enrichment (inline, degrade-safe) ─────────────────────────
def _enrich_handler(venue_dao, besttime, budget, fake, enrichment):
    """Handler wired with an injected enrichment service (mock)."""
    return AddVenueHandler(
        venue_dao=venue_dao,
        besttime_api=besttime,
        budget_service=budget,
        redis_client=fake,
        google_places_enrichment_service=enrichment,
    )


@pytest.mark.asyncio
async def test_add_time_enrichment_called_with_request_place_id(
    venue_dao, besttime, budget, fake
):
    besttime.add_venue_to_account.return_value = _ok_response("ven_enr")
    besttime.get_live_forecast.return_value = _live_unavailable("ven_enr")
    enrichment = AsyncMock()
    handler = _enrich_handler(venue_dao, besttime, budget, fake, enrichment)

    outcome = await handler.add(_req(place_id="places/ChIJreq"))

    assert outcome.status_code == 201
    # enrich_venue called inline with the request's place_id, force_refresh=True.
    enrichment.enrich_venue.assert_awaited_once()
    kwargs = enrichment.enrich_venue.await_args.kwargs
    assert kwargs["venue_id"] == "ven_enr"
    assert kwargs["google_place_id"] == "places/ChIJreq"
    assert kwargs["force_refresh"] is True


@pytest.mark.asyncio
async def test_add_time_enrichment_resolves_place_id_when_absent(
    venue_dao, besttime, budget, fake
):
    besttime.add_venue_to_account.return_value = _ok_response("ven_res")
    besttime.get_live_forecast.return_value = _live_unavailable("ven_res")
    enrichment = AsyncMock()
    handler = _enrich_handler(venue_dao, besttime, budget, fake, enrichment)
    # No google_places_client -> _enrich_from_google cannot search; simulate one by
    # attaching a client mock whose search returns a resolved id.
    handler.google_places_client = AsyncMock()
    handler.google_places_client.search_place_id.return_value = "places/ChIJresolved"

    await handler.add(_req())  # no place_id on the request

    handler.google_places_client.search_place_id.assert_awaited_once()
    kwargs = enrichment.enrich_venue.await_args.kwargs
    assert kwargs["google_place_id"] == "places/ChIJresolved"


@pytest.mark.asyncio
async def test_add_time_enrichment_failure_does_not_fail_add(
    venue_dao, besttime, budget, fake
):
    besttime.add_venue_to_account.return_value = _ok_response("ven_deg")
    besttime.get_live_forecast.return_value = _live_unavailable("ven_deg")
    enrichment = AsyncMock()
    enrichment.enrich_venue.side_effect = RuntimeError("google down")
    handler = _enrich_handler(venue_dao, besttime, budget, fake, enrichment)

    outcome = await handler.add(_req(place_id="places/ChIJboom"))

    # The add still succeeds despite the enrichment blowing up (degrade-safe).
    assert outcome.status_code == 201
    assert outcome.body["venue_id"] == "ven_deg"


@pytest.mark.asyncio
async def test_add_time_no_place_id_and_no_client_skips_enrichment(
    venue_dao, besttime, budget, fake
):
    besttime.add_venue_to_account.return_value = _ok_response("ven_skip")
    besttime.get_live_forecast.return_value = _live_unavailable("ven_skip")
    enrichment = AsyncMock()
    handler = _enrich_handler(venue_dao, besttime, budget, fake, enrichment)
    handler.google_places_client = None  # cannot resolve a place_id

    outcome = await handler.add(_req())  # no place_id

    assert outcome.status_code == 201
    enrichment.enrich_venue.assert_not_awaited()  # nothing to enrich, add still ok


@pytest.mark.asyncio
async def test_add_without_enrichment_service_still_succeeds(handler, besttime, fake):
    # The default `handler` fixture has NO enrichment service wired.
    besttime.add_venue_to_account.return_value = _ok_response("ven_noenr")
    besttime.get_live_forecast.return_value = _live_unavailable("ven_noenr")

    outcome = await handler.add(_req(place_id="places/ChIJnone"))

    assert outcome.status_code == 201  # add unaffected by the absent optional dep


@pytest.mark.asyncio
async def test_add_with_place_id_fetches_google_details_once(
    venue_dao, besttime, budget, fake
):
    # Regression: _persist_new_venue must NOT fetch Google Details for the price
    # (place_id=None baseline); enrich_venue owns the single Details call. Two
    # fetches = a doubled paid API call per add.
    from app.api.google_places_client import GooglePlacesAPIClient
    from app.models.vibe_attributes import GooglePlacesDetailsResponse
    from app.services.google_places_enrichment_service import GooglePlacesEnrichmentService

    besttime.add_venue_to_account.return_value = _ok_response("ven_1fetch")
    besttime.get_live_forecast.return_value = _live_unavailable("ven_1fetch")
    # Real client (sync details_to_vibe_attributes intact); only stub the network
    # call so we can count it.
    gclient = GooglePlacesAPIClient(api_key="test")
    gclient.get_place_details = AsyncMock(return_value=GooglePlacesDetailsResponse(
        place_id="places/ChIJreq", business_status="OPERATIONAL",
        primary_type="bar", price_level="PRICE_LEVEL_MODERATE",
    ))
    enrichment = GooglePlacesEnrichmentService(
        google_places_client=gclient, venue_dao=venue_dao
    )
    handler = AddVenueHandler(
        venue_dao=venue_dao, besttime_api=besttime, budget_service=budget,
        redis_client=fake, google_places_client=gclient,
        google_places_enrichment_service=enrichment,
    )

    outcome = await handler.add(_req(place_id="places/ChIJreq"))

    assert outcome.status_code == 201
    assert gclient.get_place_details.await_count == 1
    # Google price won (enrichment overwrote the None baseline).
    persisted = venue_dao.get_venue("ven_1fetch")
    assert persisted.price_level == 2
    assert persisted.price_level_source == "google_enum"


# ── timeout self-recovery via the account inventory (free read) ───────────────
from app.handlers.add_venue_handler import _fold_text  # noqa: E402
from app.models import AccountInventoryVenue  # noqa: E402


def _inventory(rows):
    """An async-generator factory matching list_account_inventory's shape."""

    async def _iter(page_size: int = 1000):
        for row in rows:
            yield AccountInventoryVenue.model_validate(row)

    return _iter


def _timeout_handler(venue_dao, besttime, budget, fake, **kwargs):
    """Handler with the recovery grace delay disabled so tests stay fast."""
    return AddVenueHandler(
        venue_dao=venue_dao,
        besttime_api=besttime,
        budget_service=budget,
        redis_client=fake,
        timeout_recovery_grace_seconds=0.0,
        **kwargs,
    )


_INVENTORY_ROW = {
    "venue_id": "ven_recovered",
    # Accents, case, and punctuation differ from the submitted "Bar do Joao".
    "venue_name": "BAR DO JOÃO",
    "venue_address": "Rua das Flores, 123, Recife - PE",
    "venue_lat": -8.05,
    "venue_lng": -34.88,
    "venue_forecasted": True,
}


@pytest.mark.asyncio
async def test_timeout_recovers_created_venue_from_inventory(
    venue_dao, besttime, budget, fake
):
    besttime.add_venue_to_account.side_effect = httpx.ReadTimeout("simulated")
    besttime.list_account_inventory = _inventory([_INVENTORY_ROW])
    besttime.get_live_forecast.return_value = _live_unavailable("ven_recovered")
    handler = _timeout_handler(venue_dao, besttime, budget, fake)
    recovered_before = _add_venue_result_metric("created_recovered_timeout")
    created_before = _add_venue_result_metric("created")

    outcome = await handler.add(_req())

    assert outcome.status_code == 201
    assert outcome.body["status"] == "created"
    assert outcome.body["recovered_from_timeout"] is True
    assert outcome.body["venue_id"] == "ven_recovered"
    # Persisted, slot kept, ledger marked — exactly like a normal create.
    assert fake.get("venues_geo_place_v1:ven_recovered") is not None
    assert int(fake.get("venue_add_counter_v1:2026-05")) == 1
    assert fake.sismember("besttime_touched_v1:2026-05", "ven_recovered")
    # Never a second create (each POST /forecasts re-charges).
    assert besttime.add_venue_to_account.await_count == 1
    assert (
        _add_venue_result_metric("created_recovered_timeout") - recovered_before == 1
    )
    assert _add_venue_result_metric("created") - created_before == 0


@pytest.mark.asyncio
async def test_timeout_unconfirmed_releases_slot_with_honest_detail(
    venue_dao, besttime, budget, fake
):
    besttime.add_venue_to_account.side_effect = httpx.ReadTimeout("simulated")
    besttime.list_account_inventory = _inventory(
        [{**_INVENTORY_ROW, "venue_id": "ven_other", "venue_name": "Other Bar"}]
    )
    handler = _timeout_handler(venue_dao, besttime, budget, fake)
    unconfirmed_before = _add_venue_result_metric("timeout_unconfirmed")
    error_before = _add_venue_result_metric("besttime_error")

    outcome = await handler.add(_req())

    assert outcome.status_code == 502
    detail = outcome.body["detail"].lower()
    assert "timed out" in detail
    assert "not confirmed" in detail
    assert "retry" in detail and "same venue" in detail
    # Slot released; nothing persisted or ledgered.
    assert int(fake.get("venue_add_counter_v1:2026-05") or 0) == 0
    assert not fake.sismember("besttime_touched_v1:2026-05", "ven_other")
    # Timeout is classified as its own outcome, not a generic transport error.
    assert _add_venue_result_metric("timeout_unconfirmed") - unconfirmed_before == 1
    assert _add_venue_result_metric("besttime_error") - error_before == 0
    assert besttime.add_venue_to_account.await_count == 1


@pytest.mark.asyncio
async def test_timeout_reconcile_failure_degrades_to_timeout_error(
    venue_dao, besttime, budget, fake
):
    besttime.add_venue_to_account.side_effect = httpx.ReadTimeout("simulated")

    async def _broken_inventory(page_size: int = 1000):
        raise httpx.ConnectError("inventory down")
        yield  # pragma: no cover

    besttime.list_account_inventory = _broken_inventory
    handler = _timeout_handler(venue_dao, besttime, budget, fake)

    outcome = await handler.add(_req())

    assert outcome.status_code == 502
    assert "timed out" in outcome.body["detail"].lower()
    assert int(fake.get("venue_add_counter_v1:2026-05") or 0) == 0
    assert besttime.add_venue_to_account.await_count == 1


@pytest.mark.asyncio
async def test_non_timeout_transport_error_does_not_reconcile(
    venue_dao, besttime, budget, fake
):
    """Only timeouts trigger the inventory reconcile — a connect error means
    the create never reached BestTime, so there is nothing to recover."""
    besttime.add_venue_to_account.side_effect = httpx.ConnectError("refused")
    inventory_calls = []

    async def _tracking_inventory(page_size: int = 1000):
        inventory_calls.append(1)
        yield AccountInventoryVenue.model_validate(_INVENTORY_ROW)

    besttime.list_account_inventory = _tracking_inventory
    handler = _timeout_handler(venue_dao, besttime, budget, fake)

    outcome = await handler.add(_req())

    assert outcome.status_code == 502
    assert "unavailable" in outcome.body["detail"].lower()
    assert not inventory_calls


@pytest.mark.asyncio
async def test_timeout_recovery_runs_inline_enrichment(
    venue_dao, besttime, budget, fake
):
    besttime.add_venue_to_account.side_effect = httpx.ReadTimeout("simulated")
    besttime.list_account_inventory = _inventory([_INVENTORY_ROW])
    besttime.get_live_forecast.return_value = _live_unavailable("ven_recovered")
    enrichment = AsyncMock()
    handler = _timeout_handler(
        venue_dao, besttime, budget, fake,
        google_places_enrichment_service=enrichment,
    )

    outcome = await handler.add(_req(place_id="places/ChIJrec"))

    assert outcome.status_code == 201
    kwargs = enrichment.enrich_venue.await_args.kwargs
    assert kwargs["venue_id"] == "ven_recovered"
    assert kwargs["google_place_id"] == "places/ChIJrec"
    assert kwargs["force_refresh"] is True


@pytest.mark.asyncio
async def test_geo_fallback_unavailable_carries_besttime_message(
    handler, besttime, fake
):
    besttime.add_venue_to_account.return_value = NewVenueResponse.model_validate(
        {"status": "Error", "message": "Could not geocode address"}
    )
    besttime.venue_filter.side_effect = httpx.ConnectError("filter down")

    outcome = await handler.add(_req())

    assert outcome.status_code == 502
    assert outcome.body["besttime_message"] == "Could not geocode address"
    assert outcome.body["besttime_status"] == "Error"


def test_fold_text_normalizes_accents_case_and_punctuation():
    assert _fold_text("LAÇA, Pina!") == "laca pina"
    assert _fold_text("Beijupirá  Recife") == "beijupira recife"
    assert _fold_text("Bar do João") == _fold_text("bar do joao")
    assert _fold_text("") == ""


@pytest.mark.asyncio
async def test_inventory_match_disambiguates_by_address_overlap(
    venue_dao, besttime, budget, fake
):
    rows = [
        {
            "venue_id": "ven_wrong_branch",
            "venue_name": "Bar do João",
            "venue_address": "Av. Boa Viagem 999, Recife - PE",
        },
        {
            "venue_id": "ven_right_branch",
            "venue_name": "Bar do João",
            "venue_address": "Rua das Flores, 123, Recife - PE",
        },
    ]
    besttime.list_account_inventory = _inventory(rows)
    handler = _timeout_handler(venue_dao, besttime, budget, fake)

    match = await handler._find_in_account_inventory(
        "Bar do Joao", "Rua das Flores 123, Recife - PE"
    )

    assert match.venue_id == "ven_right_branch"


@pytest.mark.asyncio
async def test_inventory_match_returns_none_when_absent(
    venue_dao, besttime, budget, fake
):
    besttime.list_account_inventory = _inventory(
        [{"venue_id": "ven_x", "venue_name": "Something Else Entirely"}]
    )
    handler = _timeout_handler(venue_dao, besttime, budget, fake)

    match = await handler._find_in_account_inventory("Bar do Joao", "Rua das Flores")

    assert match is None


# ── geo-fallback matcher ranking (_find_name_match) ───────────────────────────
from types import SimpleNamespace  # noqa: E402

from app.handlers.add_venue_handler import (  # noqa: E402
    GEO_LINK_UNDO_SOURCE,
    VENUE_LOOKUP_BY_ADDRESS_KEY_V1,
    _address_hash,
    _find_name_match,
)


def _cand(venue_id, name, address=""):
    return SimpleNamespace(venue_id=venue_id, venue_name=name, venue_address=address)


def test_find_name_match_exact_beats_containment():
    # A containment candidate is listed first, but the exact one must win.
    venues = [
        _cand("ven_contain", "Bar do Joao e Filhos"),
        _cand("ven_exact", "Bar do Joao"),
    ]
    match, reason = _find_name_match(venues, "Bar do Joao", "")
    assert match.venue_id == "ven_exact"
    assert reason == "exact"


def test_find_name_match_overlap_tiebreak_among_exact():
    # Same folded name, different addresses: the one overlapping the request
    # address wins even though it is listed second.
    venues = [
        _cand("ven_far", "Bar do Joao", "Rua Distante 1, Olinda"),
        _cand("ven_near", "Bar do Joao", "Rua das Flores 123, Recife"),
    ]
    match, reason = _find_name_match(
        venues, "Bar do Joao", "Rua das Flores 123, Recife - PE"
    )
    assert match.venue_id == "ven_near"
    assert reason == "exact"


def test_find_name_match_containment_min_len_boundary():
    # 4-char shorter name never containment-links; 5-char does.
    assert _find_name_match([_cand("v", "Cafe Rio")], "Cafe", "") == (None, None)
    match, reason = _find_name_match([_cand("v", "Pizza Place")], "Pizza", "")
    assert match.venue_id == "v" and reason == "containment"


def test_find_name_match_short_generic_word_does_not_link():
    # The originating bug: "bar" must not link to "barcelona bar".
    assert _find_name_match([_cand("v", "Barcelona Bar")], "Bar", "") == (None, None)


def test_find_name_match_accent_and_punctuation_folding():
    match, reason = _find_name_match(
        [_cand("v", "Laca Burguer Boa Viagem")], "Laça Burguer, Boa Viagem!", ""
    )
    assert match.venue_id == "v" and reason == "exact"


def test_find_name_match_skips_empty_candidate_and_empty_submitted():
    # Empty candidate names are skipped; an empty submitted name matches nothing.
    assert _find_name_match([_cand("v", "")], "Bar do Joao", "") == (None, None)
    assert _find_name_match([_cand("v", "Bar do Joao")], "", "") == (None, None)


# ── geo-link undo (undo_geo_link) ─────────────────────────────────────────────
@pytest.fixture
def rds_fake_store():
    from tests.rds_fake import InMemoryRdsVenueStore

    return InMemoryRdsVenueStore()


@pytest.fixture
def undo_handler(venue_dao, besttime, budget, fake, rds_fake_store):
    return AddVenueHandler(
        venue_dao=venue_dao,
        besttime_api=besttime,
        budget_service=budget,
        redis_client=fake,
        rds_store=rds_fake_store,
    )


_UNDO_NAME = "Bar do Joao"
_UNDO_ADDRESS = "Rua das Flores 123, Recife - PE"


def _seed_linked_venue(rds_store, fake, venue_id="ven_linked", counter=1):
    """Mirror the post-link state: an RDS row (created just now), the month
    counter incremented, and the address-hash cache entry present."""
    rds_store.upsert_venue(
        Venue(
            processed=True,
            forecast=True,
            venue_id=venue_id,
            venue_name=_UNDO_NAME,
            venue_address=_UNDO_ADDRESS,
            venue_lat=-8.05,
            venue_lng=-34.88,
        )
    )
    fake.set("venue_add_counter_v1:2026-05", counter)
    fake.set(
        VENUE_LOOKUP_BY_ADDRESS_KEY_V1.format(hash=_address_hash(_UNDO_NAME, _UNDO_ADDRESS)),
        venue_id,
    )


@pytest.mark.asyncio
async def test_undo_geo_link_missing_returns_404(undo_handler):
    outcome = await undo_handler.undo_geo_link("ven_unknown")
    assert outcome.status_code == 404


@pytest.mark.asyncio
async def test_undo_geo_link_fresh_link_deprecates_returns_slot_drops_cache(
    undo_handler, rds_fake_store, fake
):
    _seed_linked_venue(rds_fake_store, fake)
    undone_before = _add_venue_result_metric("geo_link_undone")

    outcome = await undo_handler.undo_geo_link("ven_linked")

    assert outcome.status_code == 200
    assert outcome.body["status"] == "undone"
    row = rds_fake_store.get_venue("ven_linked")
    assert row["lifecycle_status"] == "deprecated"
    assert row["deprecated_source"] == GEO_LINK_UNDO_SOURCE
    # Discovery slot returned.
    assert int(fake.get("venue_add_counter_v1:2026-05")) == 0
    # Address cache dropped so a re-add is not short-circuited to the dead row.
    assert (
        fake.get(
            VENUE_LOOKUP_BY_ADDRESS_KEY_V1.format(
                hash=_address_hash(_UNDO_NAME, _UNDO_ADDRESS)
            )
        )
        is None
    )
    assert _add_venue_result_metric("geo_link_undone") - undone_before == 1


@pytest.mark.asyncio
async def test_undo_geo_link_older_than_24h_rejected_409(
    undo_handler, rds_fake_store, fake
):
    _seed_linked_venue(rds_fake_store, fake)
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    rds_fake_store.venues["ven_linked"]["created_at"] = old.isoformat()

    outcome = await undo_handler.undo_geo_link("ven_linked")

    assert outcome.status_code == 409
    # Counter untouched; venue still active.
    assert int(fake.get("venue_add_counter_v1:2026-05")) == 1
    assert rds_fake_store.get_venue("ven_linked")["lifecycle_status"] == "active"


@pytest.mark.asyncio
async def test_undo_geo_link_idempotent_no_double_decrement(
    undo_handler, rds_fake_store, fake
):
    _seed_linked_venue(rds_fake_store, fake)
    first = await undo_handler.undo_geo_link("ven_linked")
    assert first.body["status"] == "undone"
    assert int(fake.get("venue_add_counter_v1:2026-05")) == 0

    second = await undo_handler.undo_geo_link("ven_linked")

    assert second.status_code == 200
    assert second.body["status"] == "already_undone"
    # Counter did NOT go negative / move again.
    assert int(fake.get("venue_add_counter_v1:2026-05")) == 0


@pytest.mark.asyncio
async def test_undo_geo_link_deprecated_by_other_source_rejected_409(
    undo_handler, rds_fake_store, fake
):
    _seed_linked_venue(rds_fake_store, fake)
    rds_fake_store.soft_delete_venue(
        "ven_linked", "ineligible_google_type", "eligibility_filter"
    )

    outcome = await undo_handler.undo_geo_link("ven_linked")

    assert outcome.status_code == 409
    # Not laundered into an already_undone; the eligibility deprecation stands.
    assert rds_fake_store.get_venue("ven_linked")["deprecated_source"] == "eligibility_filter"


@pytest.mark.asyncio
async def test_readd_after_undo_reactivates_despite_stale_address_cache(
    besttime, budget, fake, rds_fake_store
):
    """A re-add must NOT be blocked by a stale address-cache entry left after an
    undo. The undo's cache drop is keyed on BestTime's stored name, so when the
    operator's submitted name differs (accents/punctuation — the folding case)
    the entry survives and points at the now-deprecated row. The add must fall
    through the deprecated hit to BestTime, which reactivates it."""
    from app.dao.venue_repository import VenueRepository

    repo = VenueRepository(GeoRedisClient(fake), rds_store=rds_fake_store)
    handler = AddVenueHandler(
        venue_dao=repo,
        besttime_api=besttime,
        budget_service=budget,
        redis_client=fake,
        rds_store=rds_fake_store,
    )
    vid = "ven_geo_link"
    submitted_name = "Laça Burguer, Boa Viagem!"     # operator input
    stored_name = "Laca Burguer Boa Viagem"          # BestTime normalized
    address = "Rua das Flores 123, Recife - PE"

    # Post-undo state: RDS row deprecated by an undo, and a stale address-cache
    # entry keyed on the SUBMITTED name still pointing at it.
    rds_fake_store.upsert_venue(
        Venue(
            processed=True, forecast=True, venue_id=vid,
            venue_name=stored_name, venue_address=address,
            venue_lat=-8.05, venue_lng=-34.88,
        )
    )
    rds_fake_store.soft_delete_venue(vid, "geo_link_undone", GEO_LINK_UNDO_SOURCE)
    fake.set(
        VENUE_LOOKUP_BY_ADDRESS_KEY_V1.format(hash=_address_hash(submitted_name, address)),
        vid,
    )

    besttime.add_venue_to_account.return_value = _ok_response(vid)
    besttime.get_live_forecast.return_value = _live_unavailable(vid)

    outcome = await handler.add(
        _req(venue_name=submitted_name, venue_address=address)
    )

    assert outcome.status_code == 201
    assert outcome.body["status"] == "created"
    assert besttime.add_venue_to_account.await_count == 1  # did NOT short-circuit
    assert rds_fake_store.get_venue(vid)["lifecycle_status"] == "active"


@pytest.mark.asyncio
async def test_readd_of_otherwise_deprecated_venue_still_short_circuits(
    besttime, budget, fake, rds_fake_store
):
    """The deprecated fall-through is scoped to the geo-link-undo source ONLY.
    A cached hit on a venue deprecated for any other reason (e.g. permanently
    closed) keeps the pre-existing free already_exists short-circuit — falling
    through would spend a BestTime create on a venue _preserve_deprecation
    keeps hidden anyway."""
    from app.dao.venue_repository import VenueRepository

    repo = VenueRepository(GeoRedisClient(fake), rds_store=rds_fake_store)
    handler = AddVenueHandler(
        venue_dao=repo,
        besttime_api=besttime,
        budget_service=budget,
        redis_client=fake,
        rds_store=rds_fake_store,
    )
    vid = "ven_closed_forever"
    name = "Bar Fechado"
    address = "Rua Antiga 1, Recife - PE"

    rds_fake_store.upsert_venue(
        Venue(
            processed=True, forecast=True, venue_id=vid,
            venue_name=name, venue_address=address,
            venue_lat=-8.05, venue_lng=-34.88,
        )
    )
    rds_fake_store.soft_delete_venue(
        vid, "google_places_closed_permanently", "google_places"
    )
    fake.set(
        VENUE_LOOKUP_BY_ADDRESS_KEY_V1.format(hash=_address_hash(name, address)),
        vid,
    )

    outcome = await handler.add(_req(venue_name=name, venue_address=address))

    assert outcome.status_code == 200
    assert outcome.body["status"] == "already_exists"
    assert besttime.add_venue_to_account.await_count == 0  # no BestTime spend
    assert (
        rds_fake_store.get_venue(vid)["deprecated_source"] == "google_places"
    )
