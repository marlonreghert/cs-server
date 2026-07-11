"""Behave steps for tests/bdd/api/prev-day-weekly-forecast.feature.

Under the BestTime day_raw convention, day index 0 is 6 AM of that calendar
day and indices 18-23 are the following calendar morning, so a moment between
00:00 and 05:59 lives in the PREVIOUS day's array. These steps drive the real
/v1/venues/nearby route (router + VenueHandler) over the fakeredis DAO built
in environment.py, so the flag gate, the day-selection math, and the
byte-for-byte flag-off response shape all run end-to-end.

The "current Recife weekday" is pinned deterministically by patching
`app.handlers.venue_handler.datetime` (the same symbol test_handlers.py
patches) for the duration of each request, defaulting to Saturday when a
scenario doesn't set one explicitly (only "Disabling the flag..." omits the
Given, and it stores day_int 5 = Saturday, so the default must match).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytz
from behave import given, when, then  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.handlers import VenueHandler
from app.models import Venue, WeekRawDay
from app.routers.venue_router import router as venue_router, set_venue_handler

# Single seeded venue at a fixed location; queried with a wide radius.
_LAT, _LNG = -8.05, -34.88

_DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


def _day_raw(day_int: int) -> list[int]:
    """Deterministic per-day forecast so each day is distinguishable."""
    return [day_int] * 24


def _date_for_weekday(name: str) -> datetime:
    """A naive datetime whose .weekday() matches the named day (0=Mon..6=Sun),
    computed relative to an arbitrary anchor so nothing here depends on
    memorizing which real-world date falls on which weekday."""
    target = _DAY_NAMES.index(name)
    anchor = datetime(2024, 1, 1)
    delta_days = (target - anchor.weekday()) % 7
    return anchor + timedelta(days=delta_days)


def _override_setting(context, name, value):
    """Set a global setting for the scenario, remembering the original so
    environment.after_scenario can restore it (no cross-scenario leakage)."""
    store = getattr(context, "_settings_overrides", None)
    if store is None:
        store = {}
        context._settings_overrides = store
    if name not in store:
        store[name] = getattr(settings, name)
    setattr(settings, name, value)


def _request(context, params: dict) -> None:
    """Issue a real HTTP request against a fresh app mounting the venue router,
    with the Recife "now" pinned to the scenario's requested weekday."""
    handler = VenueHandler(
        context.venue_dao, admin_config_service=context.admin_config_service
    )
    set_venue_handler(handler)
    app = FastAPI()
    app.include_router(venue_router)
    client = TestClient(app)
    query = {"lat": _LAT, "lon": _LNG, "radius": 5.0, **params}

    weekday_name = getattr(context, "mocked_weekday", "Saturday")
    recife_tz = pytz.timezone("America/Recife")
    naive = _date_for_weekday(weekday_name)
    mocked_now = datetime(
        naive.year, naive.month, naive.day, 12, 0, 0, tzinfo=recife_tz
    )
    with patch("app.handlers.venue_handler.datetime") as mock_datetime:
        mock_datetime.now.return_value = mocked_now
        context.response = client.get("/v1/venues/nearby", params=query)


def _venue_id_of(v: dict) -> str:
    """venue_id lives top-level in MinifiedVenue, nested under "venue" in
    verbose VenueWithLive."""
    return v.get("venue_id") or v.get("venue", {}).get("venue_id", "")


def _venue(context) -> dict:
    resp = context.response
    assert resp.status_code == 200, f"status {resp.status_code}: {resp.text}"
    for v in resp.json():
        if _venue_id_of(v) == context.venue_id:
            return v
    raise AssertionError(f"venue {context.venue_id!r} not in response")


# ── Given ──────────────────────────────────────────────────────────────────────
@given("the weekly forecast prev-day attachment flag is enabled")
def step_flag_enabled(context):
    _override_setting(context, "weekly_forecast_prev_day_enabled", True)


@given("the weekly forecast prev-day attachment flag is disabled")
def step_flag_disabled(context):
    _override_setting(context, "weekly_forecast_prev_day_enabled", False)


@given('a servable venue "{name}" exists in the Redis geo index')
def step_seed_venue(context, name):
    context.venue_id = name
    context.venue_dao.upsert_venue(
        Venue(
            venue_id=name,
            venue_name=name,
            venue_address="Rua Teste 1",
            venue_lat=_LAT,
            venue_lng=_LNG,
            forecast=True,
            processed=True,
            # A few populated (and, by omission, a few null) optional fields so
            # the byte-for-byte equivalence check below is meaningful rather
            # than trivially true over an all-null venue.
            rating=4.5,
            reviews=120,
            price_level=2,
        )
    )


@given("the current Recife weekday is {name}")
def step_set_weekday(context, name):
    assert name in _DAY_NAMES, f"unknown weekday {name!r}"
    context.mocked_weekday = name


@given(
    'a weekly forecast for day_int {day_int:d} with a distinct day_raw array '
    'is stored for "{venue}"'
)
def step_seed_one_forecast(context, day_int, venue):
    context.venue_dao.set_week_raw_forecast(
        venue, WeekRawDay(day_int=day_int, day_raw=_day_raw(day_int))
    )


@given(
    'weekly forecasts are stored for day_int {a:d} and day_int {b:d} for "{venue}"'
)
def step_seed_two_forecasts(context, a, b, venue):
    for day_int in (a, b):
        context.venue_dao.set_week_raw_forecast(
            venue, WeekRawDay(day_int=day_int, day_raw=_day_raw(day_int))
        )


@given('only a weekly forecast for day_int {day_int:d} is stored for "{venue}"')
def step_seed_only_one(context, day_int, venue):
    context.venue_dao.set_week_raw_forecast(
        venue, WeekRawDay(day_int=day_int, day_raw=_day_raw(day_int))
    )


# ── When ───────────────────────────────────────────────────────────────────────
@when('a client requests nearby venues around "{venue}" without a day offset')
def step_request_no_offset(context, venue):
    _request(context, {})


@when(
    'a client requests nearby venues around "{venue}" with target_day_offset {n:d}'
)
def step_request_with_offset(context, venue, n):
    _request(context, {"target_day_offset": n})


@when('a client requests nearby venues around "{venue}" in verbose mode')
def step_request_verbose(context, venue):
    _request(context, {"verbose": True})


@when(
    'a client requests nearby venues around "{venue}" with the flag enabled '
    'and then disabled'
)
def step_request_enabled_then_disabled(context, venue):
    _override_setting(context, "weekly_forecast_prev_day_enabled", True)
    _request(context, {})
    assert context.response.status_code == 200, context.response.text
    context.response_on_json = context.response.json()

    _override_setting(context, "weekly_forecast_prev_day_enabled", False)
    _request(context, {})
    assert context.response.status_code == 200, context.response.text
    context.response_off_json = context.response.json()


@when(
    'a client requests nearby venues around "{venue}" in verbose mode with the '
    'flag enabled and then disabled'
)
def step_request_verbose_enabled_then_disabled(context, venue):
    _override_setting(context, "weekly_forecast_prev_day_enabled", True)
    _request(context, {"verbose": True})
    assert context.response.status_code == 200, context.response.text
    context.response_on_json = context.response.json()

    _override_setting(context, "weekly_forecast_prev_day_enabled", False)
    _request(context, {"verbose": True})
    assert context.response.status_code == 200, context.response.text
    context.response_off_json = context.response.json()


# ── Then ───────────────────────────────────────────────────────────────────────
@then('the served venue\'s "{field}" must have day_int {n:d}')
def step_field_day_int(context, field, n):
    venue = _venue(context)
    assert venue.get(field) is not None, f"{field} missing/null in {venue!r}"
    assert venue[field]["day_int"] == n, (
        f"{field}: expected day_int {n}, got {venue[field]['day_int']}"
    )


@then(
    'the served venue\'s "{field}" day_raw must equal the stored day_int {n:d} '
    'array verbatim'
)
def step_field_day_raw(context, field, n):
    venue = _venue(context)
    assert venue[field]["day_raw"] == _day_raw(n), (
        f"{field}: expected {_day_raw(n)}, got {venue[field]['day_raw']}"
    )


@then('the served venue\'s "{field}" must be null')
def step_field_null(context, field):
    venue = _venue(context)
    assert field in venue, f"{field} absent from response: {venue!r}"
    assert venue[field] is None, f"{field}: expected null, got {venue[field]!r}"


@then("the venue must otherwise be served with its full field set")
def step_full_field_set(context):
    venue = _venue(context)
    assert _venue_id_of(venue) == context.venue_id
    assert "weekly_forecast" in venue
    assert len(venue) > 2, f"unexpectedly sparse venue payload: {venue!r}"


@then('the served venue must not carry a "{field}" value')
def step_field_absent(context, field):
    venue = _venue(context)
    assert field not in venue, f"expected {field!r} absent, found {venue[field]!r}"


@then('the disabled response equals the enabled response with "{field}" removed')
def step_flag_off_equals_flag_on_minus_field(context, field):
    """Proves the byte-for-byte rollback contract by transitivity: the flag-ON
    response already carries every legacy field verbatim (it's the same
    response the pre-flag code produced, plus this one additive key), so if
    flag-OFF equals flag-ON with only that key removed, flag-OFF is provably
    identical to the pre-flag response -- not merely asserted to be."""
    expected = [
        {k: v for k, v in venue.items() if k != field}
        for venue in context.response_on_json
    ]
    assert context.response_off_json == expected, (
        "flag-off response is not identical to the flag-on response minus "
        f"{field!r}:\noff={context.response_off_json!r}\n"
        f"on(minus field)={expected!r}"
    )
