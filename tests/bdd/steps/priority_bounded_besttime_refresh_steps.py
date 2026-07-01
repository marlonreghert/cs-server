"""Behave steps for tests/bdd/refresh/priority_bounded_besttime_refresh.feature."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Optional

from behave import given, when, then  # type: ignore[import-untyped]
from prometheus_client import generate_latest

from app.handlers.add_venue_handler import AddVenueByAddressRequest
from app.models import (
    Analysis,
    LiveForecastResponse,
    NewVenueResponse,
    Venue,
    VenueFilterResponse,
    VenueInfo,
)
from app.services.venues_refresher_service import VenuesRefresherService

# All seeded venues share this point so the geo index is reachable.
_LAT = -8.05
_LNG = -34.88


# ── helpers ──────────────────────────────────────────────────────────────────
def _refresher(context) -> VenuesRefresherService:
    """A refresher backed by the RDS-fake repository (priority lives in RDS) with
    the monthly budget/ledger service wired in."""
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


def _seed_venue(context, vid: str, priority: int, reviews: int, rating: float) -> None:
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
            reviews=reviews,
            rating=rating,
        )
    )
    context.seeded.append((vid, priority, reviews, rating))


def _expected_selection(context, limit: int) -> list[str]:
    rows = sorted(context.seeded, key=lambda t: (t[1], -t[2], -t[3], t[0]))
    return [vid for vid, _, _, _ in rows[:limit]]


def _live_requested(context) -> list[str]:
    return [c["venue_id"] for c in context.besttime.calls if c["method"] == "get_live_forecast"]


def _metric_value(name: str, labels: Optional[dict] = None) -> float:
    labels = labels or {}
    prefix = f"{name}{{"
    plain = f"{name} "
    for line in generate_latest().decode("utf-8").splitlines():
        if line.startswith("#"):
            continue
        if labels:
            if not line.startswith(prefix):
                continue
            blob = line.split("{", 1)[1].split("}", 1)[0]
            parsed = {}
            for part in blob.split(","):
                key, value = part.split("=", 1)
                parsed[key] = value.strip('"')
            if any(parsed.get(k) != v for k, v in labels.items()):
                continue
            return float(line.rsplit(" ", 1)[1])
        if line.startswith(plain):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


class _RecordingScheduler:
    """Captures the job ids registered, without an APScheduler event loop."""

    def __init__(self) -> None:
        self.job_ids: list[str] = []

    def add_job(self, func, **kwargs) -> None:
        self.job_ids.append(kwargs.get("id"))


def _touched_key(context) -> str:
    return f"besttime_touched_v1:{context.fixed_year_month}"


# ── Background ───────────────────────────────────────────────────────────────
@given("the monthly venue quota is 500 and the manual reserve is 10")
def step_quota_reserve(context):
    context.seeded = []
    context.fake_redis.set(
        "admin_config:venue_monthly_budget",
        json.dumps({"monthly_quota": 500, "manual_reserve": 10}),
    )
    context.monthly_quota = 500
    context.manual_reserve = 10


@given("the refresh budget X is therefore 490")
def step_refresh_budget(context):
    context.refresh_budget = 490


# ── Scenario: live refresh selects top-X ─────────────────────────────────────
@given("600 active venues exist with assorted priorities 0 through 5")
def step_seed_600(context):
    for i in range(600):
        _seed_venue(context, f"v{i:04d}", priority=i % 6, reviews=10000 - i, rating=round(5.0 - (i % 50) * 0.01, 2))
    context.besttime.programmed_live_forecast = _live_ok("vlive")


@when("the live forecast refresh runs")
def step_live_runs(context):
    context.besttime.calls.clear()
    asyncio.run(_refresher(context).refresh_live_forecasts_for_all_venues())
    context.live_requested = _live_requested(context)


@then("it must request live forecasts for at most 490 distinct venues")
def step_at_most_490(context):
    distinct = set(context.live_requested)
    assert len(distinct) <= 490, f"requested {len(distinct)} distinct venues (> 490)"


@then("it must select them ordered by priority ascending")
def step_ordered_priority(context):
    by_vid = {vid: prio for vid, prio, _, _ in context.seeded}
    priorities = [by_vid[v] for v in context.live_requested]
    assert priorities == sorted(priorities), f"priorities not ascending: {priorities[:20]}..."


@then("no venue outside that selected set must be requested from BestTime")
def step_no_outside_set(context):
    expected = set(_expected_selection(context, context.refresh_budget))
    extra = set(context.live_requested) - expected
    assert not extra, f"requested {len(extra)} venues outside the selected set"


# ── Scenario: weekly reuses live's set ───────────────────────────────────────
@given("the live forecast refresh has selected its top-X venues")
def step_live_selected_set(context):
    step_seed_600(context)
    context.besttime.calls.clear()
    asyncio.run(_refresher(context).refresh_live_forecasts_for_all_venues())
    context.live_set = set(_live_requested(context))


@when("the weekly forecast refresh runs")
def step_weekly_runs(context):
    context.besttime.programmed_week_forecast = SimpleNamespace(
        status="OK", analysis=SimpleNamespace(week_raw=[])
    )
    context.besttime.calls.clear()
    asyncio.run(_refresher(context).refresh_weekly_forecasts_for_all_venues())
    context.weekly_set = {
        c["venue_id"] for c in context.besttime.calls if c["method"] == "get_week_raw_forecast"
    }


@then("it must request weekly forecasts for the same venue set as live refresh")
def step_weekly_same_set(context):
    assert context.weekly_set == context.live_set, (
        f"weekly set differs from live set "
        f"(live={len(context.live_set)}, weekly={len(context.weekly_set)})"
    )


@then("the union of venues touched by live and weekly refresh must not exceed 490 distinct venues")
def step_union_within_budget(context):
    union = context.live_set | context.weekly_set
    assert len(union) <= 490, f"union touched {len(union)} distinct venues (> 490)"


# ── Scenario: deterministic tie-break ────────────────────────────────────────
@given("multiple active venues share priority 0")
def step_seed_p0_ties(context):
    # Insert in scrambled order so insertion order != the required tie-break.
    for vid, rev, rat in [
        ("e", 10, 4.2),
        ("c", 30, 4.1),
        ("a", 50, 4.5),
        ("d", 20, 4.8),
        ("b", 40, 4.9),
    ]:
        _seed_venue(context, vid, priority=0, reviews=rev, rating=rat)
    context.besttime.programmed_live_forecast = _live_ok("vlive")


@when("the refresh selection is computed")
def step_selection_computed(context):
    context.besttime.calls.clear()
    asyncio.run(_refresher(context).refresh_live_forecasts_for_all_venues())
    context.order1 = _live_requested(context)
    context.besttime.calls.clear()
    asyncio.run(_refresher(context).refresh_live_forecasts_for_all_venues())
    context.order2 = _live_requested(context)


@then("ties must be broken by reviews descending then rating descending")
def step_tiebreak(context):
    expected = _expected_selection(context, context.refresh_budget)
    assert context.order1 == expected, f"order {context.order1} != expected {expected}"


@then("the selection must be stable across repeated runs")
def step_stable(context):
    assert context.order1 == context.order2, (context.order1, context.order2)


# ── Scenario: discovery disabled ─────────────────────────────────────────────
@given("discovery is disabled by configuration")
def step_discovery_disabled(context):
    context.discovery_enabled = False


@when("the scheduler starts")
def step_scheduler_starts(context):
    import main

    sched = _RecordingScheduler()
    settings_stub = SimpleNamespace(
        discovery_enabled=getattr(context, "discovery_enabled", False),
        venues_catalog_refresh_minutes=43200,
        venues_live_refresh_minutes=5,
        weekly_forecast_cron="0 0 * * 0",
    )
    main.register_refresh_jobs(sched, settings_stub)
    context.scheduled_job_ids = sched.job_ids


@then("the venue catalog discovery job must not be scheduled")
def step_catalog_not_scheduled(context):
    assert "venue_catalog_refresh" not in context.scheduled_job_ids, context.scheduled_job_ids


@then("a manual discovery trigger must be rejected as disabled")
def step_trigger_rejected(context):
    resp = context.client.post("/admin/trigger/venue_catalog")
    body = {}
    try:
        body = resp.json()
    except Exception:
        pass
    # Discovery is dormant: venue_catalog was removed from JOB_REGISTRY, so the
    # manual trigger is now rejected as an unknown job (404). Older builds rejected
    # it as 403 / status "disabled"; accept either as a valid rejection.
    rejected = (
        resp.status_code in (403, 404)
        or body.get("status") == "disabled"
    )
    assert rejected, f"expected rejection, got {resp.status_code}: {resp.text[:300]}"


@then("no BestTime venue-filter discovery call must be made")
def step_no_filter_call(context):
    assert not any(c["method"] == "venue_filter" for c in context.besttime.calls)


# ── Scenario: ledger refuses beyond cap ──────────────────────────────────────
@given("500 distinct venues have already been touched this calendar month")
def step_500_touched(context):
    context.fake_redis.sadd(_touched_key(context), *[f"touched_{i:04d}" for i in range(500)])
    context.new_vid = "newvenue001"
    _seed_venue(context, context.new_vid, priority=0, reviews=100, rating=4.5)
    context.besttime.programmed_live_forecast = _live_ok("vlive")


@when("any refresh requests a live forecast for a new venue id")
def step_refresh_new_venue(context):
    context.besttime.calls.clear()
    context.skip_before = _metric_value(
        "besttime_read_skipped_total", {"reason": "monthly_cap"}
    )
    asyncio.run(_refresher(context).refresh_live_forecasts_for_all_venues())
    context.skip_after = _metric_value(
        "besttime_read_skipped_total", {"reason": "monthly_cap"}
    )


@then("the request must be refused before calling BestTime")
def step_refused(context):
    called = any(
        c["method"] == "get_live_forecast" and c["venue_id"] == context.new_vid
        for c in context.besttime.calls
    )
    assert not called, "BestTime was called for a venue beyond the monthly cap"


@then("a skipped-by-cap metric must be incremented")
def step_skip_metric(context):
    assert context.skip_after > context.skip_before, (context.skip_before, context.skip_after)


# ── Scenario: re-read of touched venue ───────────────────────────────────────
@given("a venue was already touched this calendar month")
def step_one_touched(context):
    context.touched_vid = "touched_reread"
    context.fake_redis.sadd(_touched_key(context), context.touched_vid)
    _seed_venue(context, context.touched_vid, priority=0, reviews=100, rating=4.5)
    context.besttime.programmed_live_forecast = _live_ok("vlive")
    context.count_before = context.fake_redis.scard(_touched_key(context))


@when("the live refresh requests that same venue again")
def step_reread_touched(context):
    context.besttime.calls.clear()
    asyncio.run(_refresher(context).refresh_live_forecasts_for_all_venues())
    context.count_after = context.fake_redis.scard(_touched_key(context))


@then("it must be allowed without increasing the monthly unique-venue count")
def step_allowed_no_increase(context):
    called = any(
        c["method"] == "get_live_forecast" and c["venue_id"] == context.touched_vid
        for c in context.besttime.calls
    )
    assert called, "the already-touched venue should still be refreshed"
    assert context.count_after == context.count_before, (
        context.count_before, context.count_after
    )


# ── Scenario: 50m geo-fallback radius ────────────────────────────────────────
@given("BestTime rejects a manual add and the geo fallback runs")
def step_add_rejected(context):
    # A geocoder-style rejection (not the monthly cap) routes into the geo
    # fallback, which is what this scenario exercises.
    context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
        {"status": "Error", "message": "Could not geocode address"}
    )
    context.besttime.programmed_venue_filter = VenueFilterResponse(
        status="OK", venues=[], venues_n=0
    )


@when("a candidate venue lies more than 50 meters from the requested point")
def step_candidate_far(context):
    name = "Far Candidate Bar"
    # ~111m north (0.001 deg lat) — inside 200m, outside 50m.
    context.venue_dao.upsert_venue(
        Venue(
            forecast=True,
            processed=True,
            venue_id="far001",
            venue_name=name,
            venue_address="far addr",
            venue_lat=_LAT + 0.001,
            venue_lng=_LNG,
        )
    )
    request = AddVenueByAddressRequest(
        venue_name=name,
        venue_address="requested addr",
        venue_lat=_LAT,
        venue_lng=_LNG,
    )
    context.add_outcome = asyncio.run(context.add_venue_handler.add(request))
    context.filter_calls = [c for c in context.besttime.calls if c["method"] == "venue_filter"]


@then("it must not be matched")
def step_not_matched(context):
    assert context.add_outcome.body.get("status") != "already_exists", context.add_outcome.body


@then("the effective fallback radius must be 50 meters")
def step_radius_50(context):
    assert context.filter_calls, "geo fallback never called /venues/filter"
    params = context.filter_calls[-1]["params"]
    assert params.radius == 50, f"effective radius was {params.radius}, expected 50"
