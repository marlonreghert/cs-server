"""Behave steps for tests/bdd/refresh/live-forecast-pacing-retry.feature.

This repo's other tests/bdd/refresh/ scenarios (see
eligible_priority_live_refresh_steps.py) drive VenuesRefresherService against
context.besttime, a `_ProgrammableBestTime` stand-in
(tests/bdd/environment.py) that replaces the *entire* BestTimeAPIClient. That
fake has no retry/pacing logic of its own, so it structurally cannot exercise
retry behavior that lives inside the real client. This feature instead wires
a real BestTimeAPIClient with its `.client` attribute replaced by an
httpx.AsyncClient(transport=httpx.MockTransport(...)) scripted per venue_id
(the same technique tests/test_besttime_client.py's unit tests already use to
reach into `.client`), into a real VenuesRefresherService — a "real client
over a scripted transport" pattern, applied here to the refresh domain the
way plans/260703_add-venue-no-live-besttime-rate-limit.md applied it to the
api domain for the create-429 case.
"""
from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict

import httpx
from behave import given, when, then  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.api.besttime_client import BestTimeAPIClient
from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo
from app.models.vibe_attributes import VibeAttributes
from app.services.venues_refresher_service import VenuesRefresherService

# All seeded venues share this point so the geo index stays reachable.
_LAT, _LNG = -8.05, -34.88

# An allowed Google type (in the serving view) — matches
# eligible_priority_live_refresh_steps.py's _ELIGIBLE_GOOGLE_TYPE, so every
# venue seeded by this feature is servable without needing an eligibility
# scenario of its own.
_ELIGIBLE_GOOGLE_TYPE = "bar"


def _live_ok_body(vid: str) -> dict:
    return {
        "status": "OK",
        "venue_info": {"venue_id": vid},
        "analysis": {
            "venue_live_busyness": 55,
            "venue_live_busyness_available": True,
            "venue_forecasted_busyness": 50,
            "venue_forecast_busyness_available": True,
            "venue_live_forecasted_delta": 5,
        },
    }


class _ScriptedLiveForecastTransport:
    """Scripts POST /forecasts/live responses per venue_id (read off the
    request's venue_id query param) and counts calls per venue_id."""

    def __init__(self) -> None:
        self.scripts: dict[str, list] = {}
        self.repeat_timeout: set[str] = set()
        self.call_counts: dict[str, int] = defaultdict(int)

    def handler(self, request: httpx.Request) -> httpx.Response:
        vid = request.url.params.get("venue_id", "")
        self.call_counts[vid] += 1

        if vid in self.repeat_timeout:
            raise httpx.ReadTimeout(f"BDD harness: {vid} always times out", request=request)

        script = self.scripts.get(vid)
        if not script:
            raise AssertionError(
                f"BDD harness: no scripted live-forecast response left for "
                f"venue_id={vid!r} (call #{self.call_counts[vid]})"
            )
        step = script.pop(0)

        if step == "timeout":
            raise httpx.ReadTimeout(f"BDD harness: simulated timeout for {vid}", request=request)
        if step == "rejected":
            body = {
                "status": "Error",
                "message": "BDD harness: simulated business rejection",
                "venue_info": {"venue_id": vid},
                "analysis": {},
            }
            return httpx.Response(200, json=body, request=request)
        if step == "ok":
            return httpx.Response(200, json=_live_ok_body(vid), request=request)
        if isinstance(step, int):
            # Retry-After: 0 keeps any status-code-triggered retry backoff
            # near-instant in this suite — no scenario needs a real wait.
            return httpx.Response(
                step,
                json={"status": "Error", "message": f"BDD harness: simulated HTTP {step}"},
                headers={"Retry-After": "0"},
                request=request,
            )
        raise AssertionError(f"BDD harness: unknown script step {step!r} for {vid}")


def _transport(context) -> _ScriptedLiveForecastTransport:
    if not hasattr(context, "live_forecast_transport"):
        context.live_forecast_transport = _ScriptedLiveForecastTransport()
    return context.live_forecast_transport


def _build_live_client(context) -> BestTimeAPIClient:
    """A real BestTimeAPIClient wired to the scripted transport. Tolerates the
    new live_min_interval_seconds/live_retry_max_attempts constructor
    parameters not existing yet (true RED, before besttime_client.py is
    changed) by only passing what the current constructor actually accepts —
    the scenario's own assertions (call counts, cached/not-cached) are what
    must fail meaningfully in that case, not a TypeError crash."""
    kwargs = dict(
        base_url="https://besttime.app/api/v1",
        api_key_public="pub_test",
        api_key_private="priv_test",
    )
    accepted = inspect.signature(BestTimeAPIClient.__init__).parameters
    if "live_min_interval_seconds" in accepted:
        # Pacing itself is pytest-only (tests/test_besttime_client.py) —
        # disabled here so these scenarios test retry, not pacing delay.
        kwargs["live_min_interval_seconds"] = 0.0
    if "live_retry_max_attempts" in accepted:
        kwargs["live_retry_max_attempts"] = context.live_retry_max_attempts
    client = BestTimeAPIClient(**kwargs)
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(_transport(context).handler))
    return client


# ── Background ──────────────────────────────────────────────────────────────
@given("the live-forecast retry budget is {n:d} attempts")
def step_retry_budget(context, n):
    context.live_retry_max_attempts = n


# ── Given: venues and scripted BestTime responses ───────────────────────────
@given('venue "{vid}" exists')
def step_venue_exists(context, vid):
    context.repository.upsert_venue(
        Venue(
            forecast=True,
            processed=True,
            venue_id=vid,
            venue_name=f"Venue {vid}",
            venue_address=f"addr {vid}",
            venue_lat=_LAT,
            venue_lng=_LNG,
        )
    )
    context.repository.set_vibe_attributes(
        VibeAttributes(
            venue_id=vid,
            google_place_id=f"place_{vid}",
            google_primary_type=_ELIGIBLE_GOOGLE_TYPE,
        )
    )


@given('BestTime times out on the first live-forecast call for "{vid}"')
def step_timeout_once(context, vid):
    _transport(context).scripts.setdefault(vid, []).append("timeout")


@given('BestTime times out on every live-forecast call for "{vid}"')
def step_timeout_always(context, vid):
    _transport(context).repeat_timeout.add(vid)


@given('BestTime answers the first live-forecast call for "{vid}" with HTTP {code:d}')
def step_http_error_once(context, vid, code):
    _transport(context).scripts.setdefault(vid, []).append(code)


@given('BestTime then answers "{vid}" with status "OK" and busyness available')
@given('BestTime answers "{vid}" with status "OK" and busyness available')
def step_answers_ok(context, vid):
    _transport(context).scripts.setdefault(vid, []).append("ok")


@given('BestTime answers "{vid}" with status "Error"')
def step_answers_rejected(context, vid):
    _transport(context).scripts.setdefault(vid, []).append("rejected")


@given('the live forecast for "{vid}" is already cached')
def step_seed_cached(context, vid):
    context.repository.set_live_forecast(
        LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id=vid),
            analysis=Analysis(venue_live_busyness=1, venue_live_busyness_available=True),
        )
    )


# ── When ──────────────────────────────────────────────────────────────────────
@when("the live forecast refresh runs against the scripted BestTime transport")
def step_run_refresh(context):
    context.live_forecast_error_baseline = (
        REGISTRY.get_sample_value("live_forecast_fetch_results_total", {"result": "error"})
        or 0.0
    )
    client = _build_live_client(context)
    refresher = VenuesRefresherService(
        venue_dao=context.repository,
        besttime_api=client,
        redis_client=context.fake_redis,
    )
    context.refresh_error = None
    try:
        asyncio.run(refresher.refresh_live_forecasts_for_all_venues())
    except Exception as e:  # a cycle-aborting failure is still worth capturing
        context.refresh_error = e
    finally:
        asyncio.run(client.close())


# ── Then ──────────────────────────────────────────────────────────────────────
@then('the live forecast for "{vid}" is cached')
def step_then_cached(context, vid):
    assert context.repository.get_live_forecast(vid) is not None, (
        f"expected a cached live forecast for {vid!r}, found none "
        f"(refresh_error={context.refresh_error!r})"
    )


@then('the live forecast for "{vid}" is not cached')
def step_then_not_cached(context, vid):
    assert context.repository.get_live_forecast(vid) is None, (
        f"expected no cached live forecast for {vid!r}"
    )


@then('exactly {n:d} live-forecast calls were made for "{vid}"')
@then('exactly {n:d} live-forecast call was made for "{vid}"')
def step_then_call_count(context, n, vid):
    actual = _transport(context).call_counts.get(vid, 0)
    assert actual == n, f"expected {n} live-forecast call(s) for {vid!r}, got {actual}"


@then('a live-forecast error is recorded for "{vid}"')
def step_then_error_recorded(context, vid):
    after = REGISTRY.get_sample_value("live_forecast_fetch_results_total", {"result": "error"})
    before = context.live_forecast_error_baseline
    assert after is not None and after >= before + 1, (
        f"expected live_forecast_fetch_results_total{{result='error'}} to "
        f"increase by at least 1 (before={before}, after={after})"
    )
