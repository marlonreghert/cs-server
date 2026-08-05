"""Behave steps for tests/bdd/api/add-venue-google-only.feature.

Rides the same httpx.MockTransport harness as besttime_rejection_venue_info_steps
(the real BestTime client over a mocked HTTP boundary) for consistency — both
features share the Background givens "no nearby venue matches in the geo
fallback" / "a nearby venue matches in the geo fallback", imported from there
rather than redefined (Behave's step registry is global and duplicate
phrasings raise AmbiguousStep).

The BDD harness wires AddVenueHandler.venue_dao to a plain Redis-only DAO
(context.venue_dao), not the RDS-backed repository (context.repository /
context.rds_store) — see environment.py's docstring: "the RDS feature uses
context.repository / context.rds_store". Scenarios here that need the created
venue visible to RDS-backed refresh-selection queries therefore mirror it into
context.rds_store explicitly after the add, rather than relying on the add
path itself to reach RDS (it doesn't, in this harness).
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import httpx
from behave import given, when, then  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.handlers.add_venue_handler import _address_hash
from app.models.vibe_attributes import GooglePlacesDetailsResponse
from besttime_add_response_parse_steps import (  # type: ignore[import-not-found]
    _ADD_VENUE_METRIC,
    _VENUE,
    _install_real_besttime,
    _wire_inline_enrichment,
    _post_add,
)
from priority_bounded_besttime_refresh_steps import _refresher  # type: ignore[import-not-found]

_GOOGLE_ONLY_REJECTION_MESSAGE = (
    "Venue found, but could not forecast this venue. Potential issues: This "
    "place is too new, or does not have enough visitor volume to make a "
    "forecast."
)
# Unlike besttime_rejection_venue_info_steps's _REJECTION_BODY, this carries
# rating/reviews/price_level — BestTime's rejection still reports them even
# though nothing was created, and the plan uses them to fill the minted venue
# where Google is silent.
_GOOGLE_ONLY_REJECTION_BODY = {
    "status": "Error",
    "message": _GOOGLE_ONLY_REJECTION_MESSAGE,
    "venue_info": {
        "venue_name": _VENUE["venue_name"],
        "venue_address": _VENUE["venue_address"],
        "rating": 4.3,
        "reviews": 87,
        "price_level": 2,
    },
}

_GOOGLE_PLACE_ID = "place_google_only_001"


def _google_details() -> GooglePlacesDetailsResponse:
    return GooglePlacesDetailsResponse(
        place_id=_GOOGLE_PLACE_ID,
        display_name=_VENUE["venue_name"],
        primary_type="bar",
        business_status="OPERATIONAL",
    )


def _minted_id() -> str:
    return f"vsg_{_address_hash(_VENUE['venue_name'], _VENUE['venue_address'])[:24]}"


def _current_minted_id(context) -> str:
    """The minted id for THIS scenario's venue. Some scenarios reach a Then
    step without ever running one that captured context.minted_venue_id off
    the response (e.g. checking population/serving/cost-guarantee assertions
    straight after the shared 'an operator adds the venue' When); the id is
    deterministic on name+address, so falling back to _minted_id() is exactly
    equivalent, not a guess."""
    return getattr(context, "minted_venue_id", None) or _minted_id()


def _sample(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


_RESULT_LABELS = (
    "created_google_only",
    "google_only_enrichment_failed",
    # Flag-off deliberately reuses the pre-existing label (not a new one) so
    # that deploy stays a genuine metric no-op — see
    # AddVenueHandler._create_from_google_metadata.
    "besttime_rejected_no_geo_match",
)


def _ensure_counter_baseline(context) -> None:
    """Snapshot the tracked result labels, before the add runs, so 'the
    counter records the result X' can assert a delta rather than an absolute
    value (the Prometheus Counter is a process-global singleton that
    accumulates across every scenario in the whole test run — behave's
    `context` does NOT reset plain attributes between scenarios the way its
    fixture layers do, but re-snapshotting on every call here is harmless: it
    only runs from Given steps, before the When that could move the counter,
    so repeated calls within one scenario always see the same value)."""
    context._add_venue_counter_before = {
        label: _sample(_ADD_VENUE_METRIC, {"result": label})
        for label in _RESULT_LABELS
    }


# ---------------------------------------------------------------------------
# Given: Background / flag / Google outcome
# ---------------------------------------------------------------------------


@given(
    "BestTime rejects the create as unforecastable with a venue info block "
    "that has no venue id"
)
def step_rejects_unforecastable(context):
    context.besttime_http_body = _GOOGLE_ONLY_REJECTION_BODY
    context.besttime_http_status = 404


@given("the Google-only add path is enabled")
def step_google_only_enabled(context):
    _ensure_counter_baseline(context)
    context.admin_config_service.set(
        "add_venue_google_only", {"enabled": True}, updated_by="bdd-test"
    )


@given("the Google-only add path is disabled")
def step_google_only_disabled(context):
    _ensure_counter_baseline(context)
    context.admin_config_service.set(
        "add_venue_google_only", {"enabled": False}, updated_by="bdd-test"
    )


@given("the admin configuration cannot be read")
def step_admin_config_unreadable(context):
    _ensure_counter_baseline(context)

    def _boom(key):
        raise RuntimeError("admin config read failed (simulated RDS outage)")

    context.rds_store.get_admin_config = _boom


@given("Google Places resolves the submitted venue")
def step_google_resolves(context):
    _ensure_counter_baseline(context)
    context.google_places_client.search_place_id = AsyncMock(
        return_value=_GOOGLE_PLACE_ID
    )
    context._google_search_place_id_overridden = True
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_google_details()
    )


@given("Google Places cannot resolve the submitted venue")
def step_google_cannot_resolve(context):
    _ensure_counter_baseline(context)
    context.google_places_client.search_place_id = AsyncMock(return_value=None)
    context._google_search_place_id_overridden = True


@given(
    "Google Places resolves the submitted venue but the details request fails"
)
def step_google_details_fail(context):
    _ensure_counter_baseline(context)
    context.google_places_client.search_place_id = AsyncMock(
        return_value=_GOOGLE_PLACE_ID
    )
    context._google_search_place_id_overridden = True
    context.google_places_client.get_place_details = AsyncMock(return_value=None)


@given("the geo fallback request to BestTime fails")
def step_geo_fallback_transport_fails(context):
    context.besttime_filter_http_error = httpx.ConnectError("connection refused")


@given(
    "BestTime rejects the create because its monthly venue cap is reached"
)
def step_monthly_cap_rejection(context):
    context.besttime_http_body = {
        "status": "Error",
        "message": (
            "Max amount of monthly venues (500) reached. Venue counter will "
            "reset in 12 days."
        ),
    }
    context.besttime_http_status = 404


@given("BestTime accepts the create and returns a forecast")
def step_besttime_accepts_and_forecasts(context):
    from besttime_add_response_parse_steps import _real_success_body  # type: ignore[import-not-found]

    context.besttime_http_body = _real_success_body()


@given("an active venue that was created from BestTime")
def step_seed_besttime_venue(context):
    from app.models import Venue

    context.control_venue_id = "ven_control_001"
    context.repository.upsert_venue(
        Venue(
            forecast=True,
            processed=True,
            venue_id=context.control_venue_id,
            venue_name="Control Venue",
            venue_address="Control addr",
            venue_lat=_VENUE["venue_lat"],
            venue_lng=_VENUE["venue_lng"],
            priority=0,
            reviews=10,
            rating=4.0,
            venue_source="besttime",
        )
    )


@given("an operator has added the venue by name and address")
def step_operator_has_added(context):
    _post_add(context)
    assert context.response.status_code == 201, context.response.text
    context.minted_venue_id = context.response.json()["venue_id"]
    # Mirror into the RDS-backed store so refresh-selection scenarios (which
    # read priority via RDS) can see the venue — see module docstring.
    venue = context.venue_dao.get_venue(_current_minted_id(context))
    assert venue is not None, "minted venue not found in Redis after add"
    context.rds_store.upsert_venue(venue)


@given("the minted venue is the only active venue")
def step_only_active_venue(context):
    active = context.rds_store.list_active_venue_ids()
    assert active == [_current_minted_id(context)], active


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("an operator adds the same venue by name and address again")
def step_add_again(context):
    _post_add(context)


@when("the bounded live forecast refresh selects venues")
def step_live_refresh_runs(context):
    context.besttime.calls.clear()
    from priority_bounded_besttime_refresh_steps import _live_ok  # type: ignore[import-not-found]

    context.besttime.programmed_live_forecast = _live_ok("vlive")
    asyncio.run(_refresher(context).refresh_live_forecasts_for_all_venues())
    context.live_requested_ids = [
        c["venue_id"] for c in context.besttime.calls if c["method"] == "get_live_forecast"
    ]


@when("the bounded weekly forecast refresh selects venues")
def step_weekly_refresh_runs(context):
    from types import SimpleNamespace

    context.besttime.calls.clear()
    context.besttime.programmed_week_forecast = SimpleNamespace(
        status="OK", analysis=SimpleNamespace(week_raw=[])
    )
    asyncio.run(_refresher(context).refresh_weekly_forecasts_for_all_venues())
    context.weekly_requested_ids = [
        c["venue_id"]
        for c in context.besttime.calls
        if c["method"] == "get_week_raw_forecast"
    ]


@when("a batch add job processes a row for the venue")
def step_batch_process_row(context):
    _install_real_besttime(context)
    _wire_inline_enrichment(context)
    body = {
        "label": "google-only-bdd",
        "resolve_coords": False,
        "venues": [
            {
                "venue_name": _VENUE["venue_name"],
                "venue_address": _VENUE["venue_address"],
                "venue_lat": _VENUE["venue_lat"],
                "venue_lng": _VENUE["venue_lng"],
            }
        ],
    }
    resp = context.client.post("/admin/venues/batch-add", json=body)
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    last = None
    for _ in range(200):
        poll = context.client.get(f"/admin/venues/batch-add/{job_id}")
        assert poll.status_code == 200, poll.text
        last = poll.json()
        if last["status"] in ("done", "stopped", "failed"):
            break
        time.sleep(0.02)
    context.batch_job = last


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the add succeeds as created from Google metadata")
def step_add_succeeds_google(context):
    assert context.response.status_code == 201, context.response.text
    body = context.response.json()
    assert body.get("status") == "created_google_only", body
    assert body.get("source") == "google_places", body
    context.minted_venue_id = body["venue_id"]


@then("the persisted venue carries a minted venue id that is not a BestTime id")
def step_minted_id_format(context):
    vid = _current_minted_id(context)
    assert vid.startswith("vsg_"), vid
    assert not vid.startswith("ven_"), vid
    assert vid == _minted_id(), (vid, _minted_id())


@then("the persisted venue is recorded as having no forecast")
def step_no_forecast(context):
    venue = context.venue_dao.get_venue(_current_minted_id(context))
    assert venue is not None, _current_minted_id(context)
    assert venue.forecast is False, venue.forecast


@then("the persisted venue is recorded as sourced from Google")
def step_sourced_from_google(context):
    venue = context.venue_dao.get_venue(_current_minted_id(context))
    assert venue.venue_source == "google_only", venue.venue_source


@then(
    "the persisted venue carries the name, address and coordinates Google "
    "returned"
)
def step_name_address_coords(context):
    venue = context.venue_dao.get_venue(_current_minted_id(context))
    assert venue.venue_name == _VENUE["venue_name"], venue.venue_name
    assert venue.venue_address == _VENUE["venue_address"], venue.venue_address
    assert venue.venue_lat == _VENUE["venue_lat"], venue.venue_lat
    assert venue.venue_lng == _VENUE["venue_lng"], venue.venue_lng


@then(
    "the persisted venue carries the rating and review count from "
    "BestTime's rejection block"
)
def step_rating_reviews_from_rejection(context):
    venue = context.venue_dao.get_venue(_current_minted_id(context))
    assert venue.rating == 4.3, venue.rating
    assert venue.reviews == 87, venue.reviews


@then("the persisted venue carries no BestTime venue type")
def step_no_venue_type(context):
    venue = context.venue_dao.get_venue(_current_minted_id(context))
    assert venue.venue_type is None, venue.venue_type


@then(
    "the minted venue is eligible for serving despite having no BestTime "
    "venue type"
)
def step_eligible_for_serving(context):
    venue = context.venue_dao.get_venue(_current_minted_id(context))
    assert venue is not None
    assert venue.venue_type is None, venue.venue_type
    context.rds_store.upsert_venue(venue)
    servable = context.rds_store.list_servable_venue_ids()
    assert _current_minted_id(context) in servable, servable


@then("the minted venue is returned by the public nearby venues endpoint")
def step_returned_by_nearby(context):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.handlers import VenueHandler
    from app.routers.venue_router import router as venue_router, set_venue_handler

    handler = VenueHandler(
        context.venue_dao, admin_config_service=context.admin_config_service
    )
    set_venue_handler(handler)
    app = FastAPI()
    app.include_router(venue_router)
    client = TestClient(app)
    resp = client.get(
        "/v1/venues/nearby",
        params={
            "lat": _VENUE["venue_lat"],
            "lon": _VENUE["venue_lng"],
            "radius": 5.0,
        },
    )
    assert resp.status_code == 200, resp.text
    ids = [v.get("venue_id") for v in resp.json()]
    assert _current_minted_id(context) in ids, ids


@then("the BestTime create endpoint is not called a second time")
def step_create_not_called_twice(context):
    create_calls = [
        r for r in context.besttime_http_requests if r["path"].endswith("/forecasts")
    ]
    assert len(create_calls) == 1, create_calls


@then("no paid BestTime endpoint is called")
def step_no_extra_paid_calls(context):
    paths = [r["path"] for r in context.besttime_http_requests]
    unexpected = [
        p for p in paths if not (p.endswith("/forecasts") or p.endswith("/venues/filter"))
    ]
    assert not unexpected, f"unexpected BestTime calls: {unexpected}"


@then("the monthly BestTime new-venue counter does not increase")
def step_counter_unchanged(context):
    year_month = context.fixed_year_month
    counter = int(context.fake_redis.get(f"venue_add_counter_v1:{year_month}") or 0)
    assert counter == 0, counter


@then("the venue is not recorded as touched in the BestTime ledger")
def step_not_touched(context):
    year_month = context.fixed_year_month
    assert not context.fake_redis.sismember(
        f"besttime_touched_v1:{year_month}", _current_minted_id(context)
    )


@then("the minted venue is not selected")
def step_minted_not_selected(context):
    assert _current_minted_id(context) not in context.live_requested_ids, (
        context.live_requested_ids
    )


@then("no BestTime live forecast is requested for the minted venue")
def step_no_live_requested(context):
    assert _current_minted_id(context) not in context.live_requested_ids, (
        context.live_requested_ids
    )


@then("no venues are selected")
def step_no_venues_selected(context):
    assert context.weekly_requested_ids == [], context.weekly_requested_ids


@then("that venue is selected")
def step_control_venue_selected(context):
    assert context.control_venue_id in context.live_requested_ids, (
        context.live_requested_ids
    )


@then("the add fails as a rejection with the geo-fallback message")
def step_fails_geo_fallback_message(context):
    assert context.response.status_code == 502, context.response.text
    body = context.response.json()
    detail = (body.get("detail") or "").lower()
    assert "geo fallback" in detail and "rejected the address" in detail, body


@then("no venue is persisted")
def step_no_venue_persisted(context):
    assert context.venue_dao.get_venue(_minted_id()) is None, _minted_id()


@then("no partial venue row remains")
def step_no_partial_row(context):
    assert context.venue_dao.get_venue(_minted_id()) is None
    assert context.rds_store.get_venue(_minted_id()) is None


@then("the add reports that the venue already exists")
def step_reports_already_exists(context):
    assert context.response.status_code == 200, context.response.text
    body = context.response.json()
    assert body.get("status") == "already_exists", body
    assert body.get("venue_id") == _current_minted_id(context), body


@then("only one venue is persisted for that name and address")
def step_only_one_persisted(context):
    assert context.venue_dao.get_venue(_current_minted_id(context)) is not None


@then("no venue id is minted")
def step_no_venue_minted(context):
    assert context.venue_dao.get_venue(_minted_id()) is None


@then("the add fails with a geo-fallback unavailable error")
def step_geo_fallback_unavailable(context):
    assert context.response.status_code == 502, context.response.text
    detail = (context.response.json().get("detail") or "").lower()
    assert "geo fallback" in detail and "unavailable" in detail, context.response.text


@then("the add fails with the BestTime monthly cap status")
def step_monthly_cap_status(context):
    assert context.response.status_code == 429, context.response.text
    body = context.response.json()
    assert body.get("detail") == "BestTime monthly venue cap reached", body


@then("the add succeeds as a normal creation")
def step_add_normal_creation(context):
    assert context.response.status_code == 201, context.response.text
    assert context.response.json().get("status") == "created", context.response.text


@then("the persisted venue is recorded as sourced from BestTime")
def step_sourced_from_besttime(context):
    vid = context.response.json()["venue_id"]
    venue = context.venue_dao.get_venue(vid)
    assert venue.venue_source == "besttime", venue.venue_source


@then("the row is counted as a success in the job summary")
def step_row_success(context):
    summary = context.batch_job["summary"]
    assert summary.get("created_google_only", 0) == 1, summary


@then("the job is not stopped")
def step_job_not_stopped(context):
    assert context.batch_job["status"] == "done", context.batch_job


@then("the add-venue counter records the result {result}")
def step_counter_records_result(context, result):
    before = context._add_venue_counter_before.get(result, 0.0)
    after = _sample(_ADD_VENUE_METRIC, {"result": result})
    assert after == before + 1, (
        f"expected {result!r} to increment by 1 (before={before}, after={after})"
    )


@then("the gauge of active Google-sourced venues reports one venue")
def step_gauge_reports_one(context):
    from app.services.venues_refresher_service import VenuesRefresherService

    refresher = VenuesRefresherService(
        venue_dao=context.venue_dao,
        besttime_api=context.besttime,
        redis_client=context.fake_redis,
    )
    refresher.update_data_quality_metrics()
    value = REGISTRY.get_sample_value("venues_google_only_total") or 0.0
    assert value == 1, value
