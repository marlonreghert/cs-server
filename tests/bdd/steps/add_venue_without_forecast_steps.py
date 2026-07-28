"""Behave steps for tests/bdd/api/add-venue-without-forecast.feature.

Rides the same `_ProgrammableBestTime` harness (context.besttime) as
add_venue_by_address_steps.py: BestTime responses are programmed as
already-parsed models, and context.add_venue_handler / context.client /
context.batch_add_service (all built in environment.py) drive production
code end-to-end except at the BestTime HTTP boundary.

The "BestTime rejects the create" body is loaded from
tests/fixtures/besttime/forecasts_post_registered_no_forecast.json rather
than hardcoded here, so the real captured probe body (see that file's
`context.note` and PROBE_MANIFEST.md, Probe E) can replace the placeholder
by editing only the fixture.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx
from behave import given, when, then  # type: ignore[import-untyped]

from app.handlers.add_venue_handler import AddVenueByAddressRequest
from app.models import NewVenueResponse, VenueFilterResponse

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "besttime"
    / "forecasts_post_registered_no_forecast.json"
)

_VENUE = {
    "venue_name": "Bar Sem Previsao",
    "venue_address": "Rua Nova 45, Recife - PE, Brazil",
    "venue_lat": -8.06,
    "venue_lng": -34.90,
}


def _load_rejection_message() -> str:
    """The non-OK POST /forecasts message used by "BestTime rejects the
    create" scenarios. Swapping the fixture file's `response.body.message`
    is the only change needed once the real probe body is captured."""
    data = json.loads(_FIXTURE_PATH.read_text())
    return data["response"]["body"]["message"]


def _rejection_response() -> NewVenueResponse:
    return NewVenueResponse.model_validate(
        {"status": "Error", "message": _load_rejection_message()}
    )


def _no_geo_match() -> VenueFilterResponse:
    return VenueFilterResponse(status="OK", venues=[], venues_n=0)


class _StepResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


class _ListLogHandler(logging.Handler):
    def __init__(self, records: list) -> None:
        super().__init__()
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _post(context, overrides: Optional[dict] = None) -> None:
    payload = {**_VENUE, **(overrides or {})}
    request = AddVenueByAddressRequest.model_validate(payload)
    records: list[logging.LogRecord] = []
    handler = _ListLogHandler(records)
    app_logger = logging.getLogger("app")
    app_logger.addHandler(handler)
    try:
        outcome = asyncio.run(context.add_venue_handler.add(request))
    finally:
        app_logger.removeHandler(handler)
    context.captured_logs = records
    context.response = _StepResponse(outcome.status_code, outcome.body)


def _inventory_row(venue_id: str, name: str, address: str, lat: float, lng: float,
                    forecasted: bool) -> dict:
    return {
        "venue_id": venue_id,
        "venue_name": name,
        "venue_address": address,
        "venue_lat": lat,
        "venue_lng": lng,
        "venue_forecasted": forecasted,
    }


def _poll_job(context, job_id: str) -> dict:
    last = None
    for _ in range(200):
        poll = context.client.get(f"/admin/venues/batch-add/{job_id}")
        assert poll.status_code == 200, poll.text
        last = poll.json()
        if last["status"] in ("done", "stopped", "failed"):
            break
        time.sleep(0.02)
    assert last is not None, "batch job never finished"
    return last


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("the monthly new-venue quota has slots available")
def step_quota_has_slots(context):
    # Defaults (quota=500, reserve=10 -- see VenueBudgetService) already
    # leave headroom; make the precondition explicit and independent of
    # any counter a previous scenario in the same process might have set.
    context.fake_redis.delete(f"venue_add_counter_v1:{context.fixed_year_month}")


@given("the BestTime account inventory is reachable")
def step_inventory_reachable(context):
    # Explicit baseline: reachable, zero rows. Scenario-level Givens below
    # override this with the rows they actually need.
    context.besttime.programmed_inventory_pages = []


# ---------------------------------------------------------------------------
# Givens: rejection shape
# ---------------------------------------------------------------------------


@given("BestTime rejects the create for the submitted venue")
def step_besttime_rejects(context):
    context.besttime.programmed_add_venue = _rejection_response()
    # Deterministic geo-fallback default (no match) so any scenario that
    # falls through to it -- without itself programming a specific
    # /venues/filter response -- gets today's honest 502 instead of the
    # harness's "not programmed" RuntimeError.
    context.besttime.programmed_venue_filter = _no_geo_match()


@given("BestTime rejects the create with its monthly venue cap message")
def step_besttime_rejects_monthly_cap(context):
    context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
        {
            "status": "Error",
            "message": (
                "Error: Max amount of monthly venues (500) reached. Venue "
                "counter will reset at midnight on the first day of the month."
            ),
        }
    )


@given(
    "BestTime rejects the create for a submitted venue whose folded name is "
    "shorter than the containment floor"
)
def step_short_name_rejected(context):
    context.request_override = {
        "venue_name": "Vila",
        "venue_address": "Rua Curta 10, Recife - PE, Brazil",
    }
    context.besttime.programmed_add_venue = _rejection_response()
    context.besttime.programmed_venue_filter = _no_geo_match()


# ---------------------------------------------------------------------------
# Givens: inventory membership
# ---------------------------------------------------------------------------


@given(
    'the submitted venue is present in the BestTime account inventory with '
    '"venue_forecasted" false'
)
def step_inventory_present_no_forecast(context):
    context.expected_inventory_venue_id = "ven_reconciled_001"
    context.besttime.programmed_inventory_pages = [[
        _inventory_row(
            context.expected_inventory_venue_id,
            _VENUE["venue_name"],
            _VENUE["venue_address"],
            _VENUE["venue_lat"],
            _VENUE["venue_lng"],
            forecasted=False,
        )
    ]]


@given("the submitted venue is absent from the BestTime account inventory")
def step_inventory_absent(context):
    context.besttime.programmed_inventory_pages = [[
        _inventory_row(
            "ven_unrelated_999",
            "Totally Different Venue",
            "Somewhere else entirely, City",
            -22.90, -43.20,
            forecasted=True,
        )
    ]]


@given(
    "the account inventory holds a longer-named venue whose folded name "
    "contains the submitted one"
)
def step_inventory_longer_name_contains(context):
    context.besttime.programmed_inventory_pages = [[
        _inventory_row(
            "ven_unrelated_vila",
            "Vila Madalena Bar",
            "Rua Completamente Diferente 999, Sao Paulo - SP, Brazil",
            -23.50, -46.60,
            forecasted=True,
        )
    ]]


@given("the BestTime account listing fails during the reconcile")
def step_inventory_listing_fails(context):
    async def _broken_iter(page_size: int = 1000):
        raise httpx.ConnectError("simulated inventory list failure")
        yield  # pragma: no cover - never reached, keeps this an async generator

    context.besttime.list_account_inventory = _broken_iter


# ---------------------------------------------------------------------------
# Givens: cache window
# ---------------------------------------------------------------------------


@given("the inventory cache window is configured to a non-zero duration")
def step_cache_window_nonzero(context):
    context.add_venue_handler.inventory_cache_seconds = 300


@given("the inventory cache window is configured to zero")
def step_cache_window_zero(context):
    context.add_venue_handler.inventory_cache_seconds = 0


@given("a batch-add job whose rows are rejected by the BestTime create")
def step_batch_rows_rejected(context):
    context.besttime.programmed_add_venue = _rejection_response()
    context.besttime.programmed_venue_filter = _no_geo_match()
    context.batch_rows = [
        {"venue_name": "Bar Reconciliado Um", "venue_address": "Rua A, 1, Recife - PE",
         "venue_lat": -8.001, "venue_lng": -34.801},
        {"venue_name": "Bar Reconciliado Dois", "venue_address": "Rua B, 2, Recife - PE",
         "venue_lat": -8.002, "venue_lng": -34.802},
        {"venue_name": "Bar Reconciliado Tres", "venue_address": "Rua C, 3, Recife - PE",
         "venue_lat": -8.003, "venue_lng": -34.803},
    ]
    context.besttime.programmed_inventory_pages = [[
        _inventory_row(
            f"ven_batch_{i:02d}", row["venue_name"], row["venue_address"],
            row["venue_lat"], row["venue_lng"], forecasted=False,
        )
        for i, row in enumerate(context.batch_rows)
    ]]


@given("two rejected adds are submitted in immediate succession")
def step_two_rejected_adds_setup(context):
    context.besttime.programmed_add_venue = _rejection_response()
    context.besttime.programmed_venue_filter = _no_geo_match()
    context.two_adds_payloads = [
        {"venue_name": "Bar Cache Zero Um", "venue_address": "Rua D, 4, Recife - PE",
         "venue_lat": -8.011, "venue_lng": -34.811},
        {"venue_name": "Bar Cache Zero Dois", "venue_address": "Rua E, 5, Recife - PE",
         "venue_lat": -8.012, "venue_lng": -34.812},
    ]
    # Present in inventory so both reconciles actually read the listing
    # (a miss would still cost a listing, but a hit proves the reconcile
    # ran to completion for both).
    context.besttime.programmed_inventory_pages = [[
        _inventory_row(
            f"ven_cache_zero_{i}", p["venue_name"], p["venue_address"],
            p["venue_lat"], p["venue_lng"], forecasted=False,
        )
        for i, p in enumerate(context.two_adds_payloads)
    ]]


@given("a batch-add job containing a venue BestTime registers without a forecast")
def step_batch_contains_unforecastable_row(context):
    context.besttime.programmed_add_venue = _rejection_response()
    context.besttime.programmed_venue_filter = _no_geo_match()
    context.batch_rows = [
        {"venue_name": "Bar Sem Previsao Batch", "venue_address": "Rua F, 6, Recife - PE",
         "venue_lat": -8.021, "venue_lng": -34.821},
    ]
    context.besttime.programmed_inventory_pages = [[
        _inventory_row(
            "ven_batch_no_forecast",
            context.batch_rows[0]["venue_name"],
            context.batch_rows[0]["venue_address"],
            context.batch_rows[0]["venue_lat"],
            context.batch_rows[0]["venue_lng"],
            forecasted=False,
        )
    ]]


# ---------------------------------------------------------------------------
# Givens: timeout-recovery forecast-flag scenario
# ---------------------------------------------------------------------------


@given("the BestTime create times out for the submitted venue")
def step_create_times_out(context):
    context.besttime.programmed_add_venue = httpx.ReadTimeout("simulated create timeout")
    # Skip the production grace delay so the scenario stays fast.
    context.add_venue_handler.timeout_recovery_grace_seconds = 0.0


@given(
    'the venue is present in the account inventory with "venue_forecasted" false'
)
def step_timeout_inventory_present_no_forecast(context):
    context.besttime.programmed_inventory_pages = [[
        _inventory_row(
            "ven_timeout_recovered",
            _VENUE["venue_name"],
            _VENUE["venue_address"],
            _VENUE["venue_lat"],
            _VENUE["venue_lng"],
            forecasted=False,
        )
    ]]


# ---------------------------------------------------------------------------
# Whens
# ---------------------------------------------------------------------------


@when("the operator submits the venue name and address")
def step_submit(context):
    _post(context, getattr(context, "request_override", None))


@when("the timeout reconcile completes the add")
def step_timeout_reconcile_completes(context):
    _post(context)


@when("the job processes every row inside that window")
def step_batch_processes_rows(context):
    body = {
        "label": "bdd-inventory-cache",
        "resolve_coords": False,
        "venues": context.batch_rows,
    }
    resp = context.client.post("/admin/venues/batch-add", json=body)
    assert resp.status_code == 202, resp.text
    context.batch_job = _poll_job(context, resp.json()["job_id"])


@when("both reconciles run")
def step_both_reconciles_run(context):
    for payload in context.two_adds_payloads:
        _post(context, payload)


@when("the job completes")
def step_job_completes(context):
    body = {
        "label": "bdd-batch-no-forecast",
        "resolve_coords": False,
        "venues": context.batch_rows,
    }
    resp = context.client.post("/admin/venues/batch-add", json=body)
    assert resp.status_code == 202, resp.text
    context.batch_job = _poll_job(context, resp.json()["job_id"])


# ---------------------------------------------------------------------------
# Thens: response shape
# ---------------------------------------------------------------------------


# "the response status must be {status:d}" is already registered by
# add_venue_by_address_steps.py (shared Behave step registry) with the
# identical assertion this feature needs; not redefined here.


@then('the response status field must be "{expected}"')
def step_response_status_field(context, expected):
    body = context.response.json()
    assert body.get("status") == expected, body


@then('the response source field must be "{expected}"')
def step_response_source_field(context, expected):
    body = context.response.json()
    assert body.get("source") == expected, body


@then("the venue must be persisted with its forecast flag false")
def step_persisted_forecast_false(context):
    body = context.response.json()
    raw = context.fake_redis.get(f"venues_geo_place_v1:{body['venue_id']}")
    assert raw is not None, f"venue {body['venue_id']} not persisted"
    assert json.loads(raw).get("forecast") is False, json.loads(raw)


@then("the monthly new venue counter must include this venue")
def step_counter_includes_venue(context):
    counter = int(
        context.fake_redis.get(f"venue_add_counter_v1:{context.fixed_year_month}") or 0
    )
    assert counter == 1, f"expected the reserved slot kept (counter 1), got {counter}"


@then("the venue must be reachable through the nearby venues endpoint")
def step_reachable_nearby(context):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.handlers import VenueHandler
    from app.routers.venue_router import router as venue_router, set_venue_handler

    body = context.response.json()
    handler = VenueHandler(
        context.venue_dao, admin_config_service=context.admin_config_service
    )
    set_venue_handler(handler)
    app = FastAPI()
    app.include_router(venue_router)
    client = TestClient(app)
    resp = client.get(
        "/v1/venues/nearby",
        params={"lat": body["venue_lat"], "lon": body["venue_lng"], "radius": 5.0},
    )
    assert resp.status_code == 200, resp.text
    ids = [v.get("venue_id") for v in resp.json()]
    assert body["venue_id"] in ids, f"{body['venue_id']} not in nearby response: {ids}"


# ---------------------------------------------------------------------------
# Thens: call-count / spend assertions
# ---------------------------------------------------------------------------


@then("the BestTime add-venue endpoint must have been called exactly once")
def step_add_called_once(context):
    calls = [c for c in context.besttime.calls if c.get("method") == "add_venue_to_account"]
    assert len(calls) == 1, calls


@then("the BestTime venue-filter endpoint must not have been called")
def step_filter_not_called(context):
    calls = [c for c in context.besttime.calls if c.get("method") == "venue_filter"]
    assert not calls, calls


@then("the only inventory access must be the free account listing")
def step_only_free_listing(context):
    methods = {c.get("method") for c in context.besttime.calls}
    assert methods == {"add_venue_to_account", "list_account_inventory"}, methods


@then("the account inventory must not be listed")
def step_inventory_not_listed(context):
    calls = [c for c in context.besttime.calls if c.get("method") == "list_account_inventory"]
    assert not calls, f"unexpected inventory listing calls: {calls}"


@then("the account inventory must be listed exactly once")
def step_inventory_listed_once(context):
    calls = [c for c in context.besttime.calls if c.get("method") == "list_account_inventory"]
    assert len(calls) == 1, f"expected exactly one listing, got {len(calls)}: {calls}"


@then("the account inventory must be listed once per reconcile")
def step_inventory_listed_per_reconcile(context):
    calls = [c for c in context.besttime.calls if c.get("method") == "list_account_inventory"]
    assert len(calls) == len(context.two_adds_payloads), (
        f"expected {len(context.two_adds_payloads)} listings (cache disabled), "
        f"got {len(calls)}: {calls}"
    )


@then("each row must still be reconciled against the listed inventory")
def step_each_row_reconciled(context):
    job = context.batch_job
    assert job["summary"].get("created_without_forecast") == len(context.batch_rows), (
        job["summary"]
    )


# ---------------------------------------------------------------------------
# Thens: release / geo-fallback path unchanged
# ---------------------------------------------------------------------------


@then("the monthly slot reserved for this add must be released")
def step_slot_released(context):
    counter = int(
        context.fake_redis.get(f"venue_add_counter_v1:{context.fixed_year_month}") or 0
    )
    assert counter == 0, f"expected the reserved slot released (counter 0), got {counter}"


@then("the geo fallback must run against the submitted coordinate")
def step_geo_fallback_ran(context):
    calls = [c for c in context.besttime.calls if c.get("method") == "venue_filter"]
    assert len(calls) == 1, f"expected exactly one venue_filter call, got {calls}"
    params = calls[0]["params"]
    assert params.lat == _VENUE["venue_lat"], params
    assert params.lng == _VENUE["venue_lng"], params


@then("the geo fallback must still run")
def step_geo_fallback_still_runs(context):
    calls = [c for c in context.besttime.calls if c.get("method") == "venue_filter"]
    assert len(calls) == 1, calls


@then("the response must match the outcome the geo fallback produces")
def step_response_matches_geo_outcome(context):
    # No geo match was programmed -> today's honest 502.
    assert context.response.status_code == 502, context.response.text
    detail = (context.response.json().get("detail") or "").lower()
    assert "geo fallback" in detail and "no matching" in detail, context.response.text


@then(
    "the response must be the one the operator would have received before "
    "this feature"
)
def step_response_matches_pre_feature_behavior(context):
    step_response_matches_geo_outcome(context)


@then("the listing failure must be logged with the submitted venue name")
def step_listing_failure_logged(context):
    warnings = [r for r in context.captured_logs if r.levelno == logging.WARNING]
    assert any(_VENUE["venue_name"] in r.getMessage() for r in warnings), (
        f"no WARNING log mentioned {_VENUE['venue_name']!r}: "
        f"{[r.getMessage() for r in warnings]}"
    )


@then("the two venues must not be linked")
def step_not_linked(context):
    body = context.response.json()
    assert body.get("venue_id") != "ven_unrelated_vila", body


@then("the response must not be a created outcome")
def step_not_created_outcome(context):
    body = context.response.json()
    assert body.get("status") not in ("created", "created_without_forecast"), body


@then('the response must carry BestTime\'s status and message')
def step_response_carries_besttime_status(context):
    body = context.response.json()
    assert body.get("besttime_status") == "Error", body
    assert "monthly venues" in (body.get("besttime_message") or "").lower(), body


# ---------------------------------------------------------------------------
# Thens: batch triage
# ---------------------------------------------------------------------------


@then('that row\'s outcome must be "created_without_forecast"')
def step_row_outcome(context):
    job = context.batch_job
    assert job["results"][0]["outcome"] == "created_without_forecast", job["results"]


@then("the job summary must count the row as a success")
def step_summary_counts_success(context):
    job = context.batch_job
    assert job["summary"].get("created_without_forecast") == 1, job["summary"]


@then("the job must not stop early because of that row")
def step_job_not_stopped(context):
    assert context.batch_job["status"] == "done", context.batch_job


# ---------------------------------------------------------------------------
# Thens: timeout-recovery forecast flag
# ---------------------------------------------------------------------------


@then("the persisted venue's forecast flag must be false")
def step_persisted_forecast_flag_false(context):
    step_persisted_forecast_false(context)
