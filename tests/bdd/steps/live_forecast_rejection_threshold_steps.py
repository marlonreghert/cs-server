"""Behave steps specific to tests/bdd/refresh/live-forecast-rejection-threshold.feature.

Venue seeding, response scripting, the "When the live forecast refresh runs
against the scripted BestTime transport" trigger, and the cached/not-cached
assertions are all reused from live_forecast_pacing_retry_steps.py (same
domain, same harness — behave's step registry is global, not per-file, so
those decorators already cover this feature's matching Given/When/Then
lines). This file only adds what's genuinely new: overriding the rejection
streak threshold, and asserting on the two metric outcomes this feature
introduces.
"""
from __future__ import annotations

from behave import given, then  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.config import settings


def _override_setting(context, name: str, value) -> None:
    """Patch a global setting for this scenario only; restored by
    environment.py's after_scenario (see _settings_overrides there)."""
    store = getattr(context, "_settings_overrides", None)
    if store is None:
        store = {}
        context._settings_overrides = store
    if name not in store:
        store[name] = getattr(settings, name)
    setattr(settings, name, value)


@given("the live-forecast rejection streak threshold is {n:d} consecutive rejections")
def step_streak_threshold(context, n):
    _override_setting(context, "live_forecast_rejection_streak_threshold", n)


@then('a rejection-below-threshold outcome is recorded for "{vid}"')
def step_then_below_threshold_recorded(context, vid):
    # No venue_id label exists on this counter (see app/metrics.py) — this
    # asserts the aggregate outcome increased, matching
    # live_forecast_pacing_retry_steps.step_then_error_recorded's own
    # aggregate-only pattern. `vid` documents intent in the Gherkin.
    after = REGISTRY.get_sample_value(
        "live_forecast_fetch_results_total",
        {"result": "rejected_streak_below_threshold"},
    )
    before = context.live_forecast_rejected_streak_below_threshold_baseline
    assert after is not None and after >= before + 1, (
        "expected live_forecast_fetch_results_total"
        "{result='rejected_streak_below_threshold'} to increase by at least 1 "
        f"(before={before}, after={after}) for {vid!r}"
    )


@then('a cache-deleted outcome is recorded for "{vid}"')
def step_then_deleted_recorded(context, vid):
    after = REGISTRY.get_sample_value(
        "live_forecast_fetch_results_total", {"result": "deleted_not_ok"}
    )
    before = context.live_forecast_deleted_not_ok_baseline
    assert after is not None and after >= before + 1, (
        "expected live_forecast_fetch_results_total{result='deleted_not_ok'} "
        f"to increase by at least 1 (before={before}, after={after}) for {vid!r}"
    )
