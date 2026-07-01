"""Behave steps for tests/bdd/api/live-busyness-freshness-gate.feature.

The serve handler must suppress a cached live busyness value whose payload
``venue_current_gmttime`` is older than the freshness window, so the minified
nearby response omits ``venue_live_busyness`` and vibes_bot falls back to the
forecast estimate. These steps drive the real VenueHandler over the fakeredis
DAO built in environment.py (context.venue_dao) with the real AdminConfigService
(context.admin_config_service), so freshness resolution + suppression run
end-to-end. Age is expressed relative to the wall clock: the handler stamps its
own ``now`` at serve time, so "N minutes old" is set as now-N and judged against
the handler's now a few milliseconds later.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytz
from behave import given, when, then  # type: ignore[import-untyped]

from app.handlers import VenueHandler
from app.metrics import VENUE_SERVE_LIVE_BUSYNESS_TOTAL
from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo, WeekRawDay

# Single seeded venue location; queried with a wide radius so geo always matches.
_LAT, _LNG = -8.05, -34.88
_VENUE_ID = "freshness-venue"
_LIVE_BUSYNESS = 50

_METRIC_OUTCOMES = ("served", "suppressed_stale", "suppressed_unparseable")


def _metric(outcome: str) -> float:
    return VENUE_SERVE_LIVE_BUSYNESS_TOTAL.labels(outcome=outcome)._value.get()


def _write_live(context, gmttime: str) -> None:
    """Persist the seeded venue's live forecast with the given gmttime string."""
    context.venue_dao.set_live_forecast(
        LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id=_VENUE_ID, venue_current_gmttime=gmttime),
            analysis=Analysis(
                venue_live_busyness=_LIVE_BUSYNESS, venue_live_busyness_available=True
            ),
        )
    )


def _find(context):
    """Return the seeded venue from the minified serve result."""
    for v in context.result:
        if v.venue_id == _VENUE_ID:
            return v
    raise AssertionError(f"venue {_VENUE_ID!r} not in serve result")


# ── Background ────────────────────────────────────────────────────────────────
@given("the live refresh interval is set to {minutes:d} minutes")
def step_set_refresh_interval(context, minutes):
    # The freshness window is derived as factor x this interval, so setting it via
    # the same admin key the refresher reads keeps the gate and refresher in sync.
    context.admin_config_service.set(
        "live_refresh_minutes", {"minutes": minutes}, updated_by="test"
    )


@given('the current time is "{ts}" UTC')
def step_current_time(context, ts):
    # The handler stamps its own now at serve time; this line documents intent.
    context.scenario_ts = ts


# ── Given: seed venue + live forecast ─────────────────────────────────────────
@given("a venue has a cached live forecast that is available")
def step_seed_available_venue(context):
    context.venue_dao.upsert_venue(
        Venue(
            venue_id=_VENUE_ID,
            venue_name="Freshness Bar",
            venue_address="Rua Teste 1",
            venue_lat=_LAT,
            venue_lng=_LNG,
            forecast=True,
            processed=True,
        )
    )
    # The gmttime steps below write the live forecast once the age is known.


@given("the live forecast venue_current_gmttime is {minutes:d} minutes old")
@given("the live forecast venue_current_gmttime is exactly {minutes:d} minutes old")
def step_gmttime_minutes_old(context, minutes):
    gmttime = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    _write_live(context, gmttime)


@given("the live forecast venue_current_gmttime is not a parseable timestamp")
def step_gmttime_unparseable(context):
    _write_live(context, "definitely-not-a-timestamp")


@given("the venue has a cached weekly forecast for the current day")
def step_seed_weekly(context):
    day_int = datetime.now(pytz.timezone("America/Recife")).weekday()
    context.venue_dao.set_week_raw_forecast(
        _VENUE_ID, WeekRawDay(day_int=day_int, day_raw=[10] * 24)
    )


@given('the admin config "{key}" is set to {value}')
def step_admin_override(context, key, value):
    parsed = value.strip().strip('"')
    try:
        parsed = int(parsed)
    except ValueError:
        pass
    context.admin_config_service.set(key, parsed, updated_by="test")


# ── When ──────────────────────────────────────────────────────────────────────
@when("the nearby-venues endpoint is queried in minified mode")
def step_query_nearby(context):
    context.metric_baseline = {o: _metric(o) for o in _METRIC_OUTCOMES}
    handler = VenueHandler(
        context.venue_dao, admin_config_service=context.admin_config_service
    )
    context.result = handler.get_venues_nearby(_LAT, _LNG, 5.0, verbose=False)


# ── Then ──────────────────────────────────────────────────────────────────────
@then('the venue response must include "venue_live_busyness" from the live forecast')
def step_live_served(context):
    venue = _find(context)
    assert venue.venue_live_busyness == _LIVE_BUSYNESS, (
        f"expected live busyness {_LIVE_BUSYNESS}, got {venue.venue_live_busyness!r}"
    )


@then('the venue response "venue_live_busyness" must be null')
def step_live_null(context):
    venue = _find(context)
    assert venue.venue_live_busyness is None, (
        f"expected venue_live_busyness None, got {venue.venue_live_busyness!r}"
    )


@then('the venue response must still include the "weekly_forecast"')
def step_weekly_present(context):
    venue = _find(context)
    assert venue.weekly_forecast is not None, "weekly_forecast was dropped"


@then('the serve metric outcome "{outcome}" must be incremented for that venue')
def step_metric_incremented(context, outcome):
    delta = _metric(outcome) - context.metric_baseline[outcome]
    assert delta == 1, f"expected {outcome} +1, got +{delta}"
