"""Behave steps for tests/bdd/api/future-time-forecast.feature.

The nearby-venues endpoint must return the weekly-forecast day the caller asks
for via ``target_day_offset`` (interpreted modulo 7), while callers that omit it
keep today's behavior. These steps drive the real ``/v1/venues/nearby`` route
(router + VenueHandler) over the fakeredis DAO built in environment.py, so the
Pydantic Query validation and the handler's day selection run end-to-end.
"""
from __future__ import annotations

from datetime import datetime

import pytz
from behave import given, when, then  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.handlers import VenueHandler
from app.models import Venue, WeekRawDay
from app.routers.venue_router import router as venue_router, set_venue_handler

# Single seeded venue at a fixed location; queried with a wide radius.
_LAT, _LNG = -8.05, -34.88
_VENUE_ID = "central-recife-bar"


def _today_day_int() -> int:
    """BestTime day index for "today" in Recife (0=Mon..6=Sun)."""
    return datetime.now(pytz.timezone("America/Recife")).weekday()


def _day_raw(day_int: int) -> list[int]:
    """Deterministic per-day forecast so each day is distinguishable."""
    return [day_int] * 24


def _request(context, params: dict) -> None:
    """Issue a real HTTP request against a fresh app mounting the venue router."""
    handler = VenueHandler(
        context.venue_dao, admin_config_service=context.admin_config_service
    )
    set_venue_handler(handler)
    app = FastAPI()
    app.include_router(venue_router)
    client = TestClient(app)
    query = {"lat": _LAT, "lon": _LNG, "radius": 5.0, **params}
    context.response = client.get("/v1/venues/nearby", params=query)


def _venue(context) -> dict:
    resp = context.response
    assert resp.status_code == 200, f"status {resp.status_code}: {resp.text}"
    for v in resp.json():
        if v.get("venue_id") == _VENUE_ID:
            return v
    raise AssertionError(f"venue {_VENUE_ID!r} not in response")


# ── Given ──────────────────────────────────────────────────────────────────────
@given('a venue "{name}" exists near the requested location')
def step_seed_venue(context, name):
    context.venue_dao.upsert_venue(
        Venue(
            venue_id=_VENUE_ID,
            venue_name=name,
            venue_address="Rua Central 1",
            venue_lat=_LAT,
            venue_lng=_LNG,
            forecast=True,
            processed=True,
        )
    )


@given("the venue has a distinct weekly forecast stored for every day of the week")
def step_seed_all_days(context):
    for day_int in range(7):
        context.venue_dao.set_week_raw_forecast(
            _VENUE_ID, WeekRawDay(day_int=day_int, day_raw=_day_raw(day_int))
        )


@given("today's day index is known")
def step_today_known(context):
    context.today = _today_day_int()


# ── When ───────────────────────────────────────────────────────────────────────
@when("I request nearby venues without a target_day_offset")
def step_request_no_offset(context):
    _request(context, {})


@when("I request nearby venues with target_day_offset {offset}")
def step_request_with_offset(context, offset):
    _request(context, {"target_day_offset": int(offset)})


# ── Then ───────────────────────────────────────────────────────────────────────
@then("the venue's weekly_forecast day_int equals today's day index")
def step_day_today(context):
    venue = _venue(context)
    assert venue["weekly_forecast"]["day_int"] == _today_day_int(), (
        f"expected today {_today_day_int()}, got {venue['weekly_forecast']['day_int']}"
    )


@then("the venue's weekly_forecast day_raw equals today's stored forecast")
def step_dayraw_today(context):
    venue = _venue(context)
    assert venue["weekly_forecast"]["day_raw"] == _day_raw(_today_day_int())


@then(
    "the venue's weekly_forecast day_int equals today's day index shifted by {n:d} modulo 7"
)
def step_day_shifted(context, n):
    venue = _venue(context)
    expected = (_today_day_int() + n) % 7
    assert venue["weekly_forecast"]["day_int"] == expected, (
        f"expected {expected}, got {venue['weekly_forecast']['day_int']}"
    )


@then("the venue's weekly_forecast day_raw equals the stored forecast for that day")
def step_dayraw_shifted(context):
    venue = _venue(context)
    day_int = venue["weekly_forecast"]["day_int"]
    assert venue["weekly_forecast"]["day_raw"] == _day_raw(day_int)


@then("the response status is {code:d}")
def step_status(context, code):
    assert context.response.status_code == code, (
        f"expected {code}, got {context.response.status_code}: {context.response.text}"
    )


@then("the venue's weekly_forecast is a single day object, not a list")
def step_single_day(context):
    venue = _venue(context)
    wf = venue["weekly_forecast"]
    assert isinstance(wf, dict) and "day_int" in wf, f"weekly_forecast not a day object: {wf!r}"


@then("all other venue fields are present as in a normal nearby response")
def step_fields_present(context):
    venue = _venue(context)
    assert "venue_id" in venue and "weekly_forecast" in venue
    assert len(venue) > 2, f"unexpectedly sparse venue payload: {venue!r}"
