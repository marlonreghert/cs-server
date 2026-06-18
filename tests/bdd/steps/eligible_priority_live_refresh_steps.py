"""Behave steps for tests/bdd/refresh/eligible-priority-live-refresh.feature.

The bounded BestTime refresh must draw from the eligibility serving view
(`serving.eligible_venue`) ordered by priority — the served venues — instead of
all active venues. These steps drive the RDS layer built in environment.py
(context.repository = RDS-backed DAO, context.rds_store = fake truth,
context.budget_service = the monthly quota/ledger). Eligibility is exercised
through the real evaluate() path: an ineligible venue is given a blocked Google
type, so the fake serving view excludes it exactly as the SQL view would.
"""
from __future__ import annotations

import asyncio
import json

from behave import given, when, then  # type: ignore[import-untyped]

from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo
from app.models.vibe_attributes import VibeAttributes
from app.services.venues_refresher_service import VenuesRefresherService

# All seeded venues share this point so the geo index stays reachable.
_LAT, _LNG = -8.05, -34.88

# Eligible -> allowed Google type (in the serving view); ineligible -> a default
# blocked Google type (high-confidence ineligible -> excluded from the view).
_ELIGIBLE_GOOGLE_TYPE = "bar"
_INELIGIBLE_GOOGLE_TYPE = "pharmacy"


# ── helpers ──────────────────────────────────────────────────────────────────
def _refresher(context) -> VenuesRefresherService:
    """A refresher backed by the RDS-fake repository (priority + eligibility live
    in RDS) with the monthly budget/ledger service wired in."""
    refresher = VenuesRefresherService(
        venue_dao=context.repository,
        besttime_api=context.besttime,
        redis_client=context.fake_redis,
    )
    refresher.set_budget_service(context.budget_service)
    return refresher


def _live_ok(venue_id: str) -> LiveForecastResponse:
    return LiveForecastResponse(
        status="OK",
        venue_info=VenueInfo(venue_id=venue_id),
        analysis=Analysis(venue_live_busyness=50, venue_live_busyness_available=True),
    )


def _touched_key(context) -> str:
    return f"besttime_touched_v1:{context.fixed_year_month}"


def _seed_venue(context, vid: str, *, priority: int, eligible: bool) -> None:
    context.repository.upsert_venue(
        Venue(
            forecast=True,
            processed=True,
            venue_id=vid,
            venue_name=f"Venue {vid}",
            venue_address=f"addr {vid}",
            venue_lat=_LAT,
            venue_lng=_LNG,
            priority=priority,
        )
    )
    google_type = _ELIGIBLE_GOOGLE_TYPE if eligible else _INELIGIBLE_GOOGLE_TYPE
    context.repository.set_vibe_attributes(
        VibeAttributes(
            venue_id=vid,
            google_place_id=f"place_{vid}",
            google_primary_type=google_type,
        )
    )


def _live_reads(context) -> list[str]:
    return [
        c["venue_id"] for c in context.besttime.calls
        if c["method"] == "get_live_forecast"
    ]


# ── Background ────────────────────────────────────────────────────────────────
@given("a refresh budget of {n:d} venues")
def step_refresh_budget(context, n):
    # X = monthly_quota − manual_reserve. reserve=0 makes the refresh budget equal
    # the monthly quota, so "the cap is reached" and the budget share one number.
    context.fake_redis.set(
        "admin_config:venue_monthly_budget",
        json.dumps({"monthly_quota": n, "manual_reserve": 0}),
    )
    context.refresh_budget = n
    context.monthly_quota = n


@given("the following venues exist:")
def step_seed_venues(context):
    context.seeded_active_ids = []
    for row in context.table:
        vid = row["venue_id"].strip()
        lifecycle = row["lifecycle"].strip().lower()
        eligible = row["eligible"].strip().lower() == "yes"
        priority = int(row["priority"])
        _seed_venue(context, vid, priority=priority, eligible=eligible)
        if lifecycle == "deprecated":
            context.repository.soft_delete_venue(vid, "test_deprecated", "test")
        else:
            context.seeded_active_ids.append(vid)


# ── Given: monthly ledger state ───────────────────────────────────────────────
@given("the monthly unique-venue cap is reached")
def step_cap_reached(context):
    # Fill the month's ledger with `monthly_quota` distinct ids so any new venue
    # registration would overflow the cap.
    context.fake_redis.sadd(
        _touched_key(context),
        *[f"cap_filler_{i:04d}" for i in range(context.monthly_quota)],
    )


@given('venue "{vid}" was already touched this month')
def step_already_touched(context, vid):
    context.fake_redis.sadd(_touched_key(context), vid)


@given('venue "{vid}" was not yet touched this month')
def step_not_touched(context, vid):
    assert not context.fake_redis.sismember(_touched_key(context), vid), (
        f"{vid} unexpectedly already touched"
    )


@given("the serving view read fails")
def step_serving_view_fails(context):
    context.rds_store.set_unavailable(True)


# ── When ──────────────────────────────────────────────────────────────────────
@when("the bounded refresh selects venues")
def step_bounded_selects(context):
    context.selection = _refresher(context)._select_refresh_venue_ids("live_forecast")


@when("the bounded refresh runs")
def step_bounded_runs(context):
    context.besttime.programmed_live_forecast = _live_ok("vlive")
    context.besttime.calls.clear()
    context.refresh_error = None
    try:
        asyncio.run(_refresher(context).refresh_live_forecasts_for_all_venues())
    except Exception as e:  # fail-safe abort is allowed to surface as a raise
        context.refresh_error = e
    context.read_ids = _live_reads(context)


# ── Then: selection ───────────────────────────────────────────────────────────
@then('the selection is "{ids}"')
def step_selection_is(context, ids):
    expected = [s.strip() for s in ids.split(",") if s.strip()]
    assert context.selection == expected, f"{context.selection} != {expected}"


@then('the selection excludes "{vid}"')
def step_selection_excludes(context, vid):
    assert vid not in context.selection, f"{vid} unexpectedly selected: {context.selection}"


@then("the selection contains {n:d} venues")
def step_selection_count(context, n):
    assert len(context.selection) == n, (
        f"selected {len(context.selection)} (expected {n}): {context.selection}"
    )


# ── Then: bounded run + ledger gate ───────────────────────────────────────────
@then('a BestTime read is attempted for "{vid}"')
def step_read_attempted(context, vid):
    assert vid in context.read_ids, f"{vid} not read; reads={context.read_ids}"


@then('the BestTime read for "{vid}" is skipped due to the monthly cap')
def step_read_skipped(context, vid):
    assert vid not in context.read_ids, (
        f"{vid} was read but should be cap-skipped; reads={context.read_ids}"
    )


@then("no venues are refreshed")
def step_no_reads(context):
    assert context.read_ids == [], f"unexpected reads: {context.read_ids}"


@then("the refresh does not fall back to the full active set")
def step_no_active_fallback(context):
    # The fail-safe abort must not refresh active-but-non-served venues: 'e'
    # (active, ineligible) and 'd' (active, eligible, beyond budget) are the
    # tell-tales of an active-scoped fallback.
    assert "e" not in context.read_ids and "d" not in context.read_ids, (
        f"fell back to the active set: {context.read_ids}"
    )
    assert context.read_ids == [], f"unexpected reads on abort: {context.read_ids}"
