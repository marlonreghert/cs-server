"""Behave steps for tests/bdd/refresh/live-refresh-interval-admin.feature."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from behave import given, then, when  # type: ignore[import-untyped]

ADMIN_KEY = "admin_config:live_refresh_minutes"
DEFAULT_MINUTES = 5


class _FakeScheduler:
    """Captures reschedule calls; exposes the last applied interval."""

    def __init__(self) -> None:
        self.reschedule_calls: list[tuple[str, object]] = []

    def reschedule_job(self, job_id: str, trigger=None) -> None:
        self.reschedule_calls.append((job_id, trigger))


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _watcher_module():
    """Import inside steps so a missing implementation is a true red failure."""
    try:
        from app.services import refresh_interval_watch
    except ImportError as exc:  # pragma: no cover - red phase
        raise AssertionError(f"refresh_interval_watch service not implemented: {exc}")
    return refresh_interval_watch


def _run_watcher(context) -> None:
    asyncio.run(context.watcher.run())


def _trigger_minutes(trigger) -> float:
    return trigger.interval.total_seconds() / 60


def _job_error_count(context) -> float:
    from app.metrics import BACKGROUND_JOB_RUNS_TOTAL

    return BACKGROUND_JOB_RUNS_TOTAL.labels(
        job_name="refresh_interval_watch", status="error"
    )._value.get()


def _gauge_value() -> float:
    from app.metrics import LIVE_REFRESH_INTERVAL_MINUTES

    return LIVE_REFRESH_INTERVAL_MINUTES._value.get()


# ── background ───────────────────────────────────────────────────────────────
@given(
    "the scheduler is running with live_forecast_refresh at the settings "
    "default interval of 5 minutes"
)
def step_scheduler_running(context):
    module = _watcher_module()
    context.fake_scheduler = _FakeScheduler()
    context.log_handler = _ListHandler()
    logging.getLogger(module.__name__).addHandler(context.log_handler)
    context.watcher = module.RefreshIntervalWatcher(
        redis_client=context.fake_redis,
        scheduler=context.fake_scheduler,
        default_minutes=DEFAULT_MINUTES,
    )


# ── given/when ───────────────────────────────────────────────────────────────
@when('the admin config key "live_refresh_minutes" is set to {value}')
def step_set_admin_key(context, value):
    context.fake_redis.set(ADMIN_KEY, value)


@given('the admin config key "live_refresh_minutes" is set to 15 and applied')
def step_set_and_apply(context):
    context.fake_redis.set(ADMIN_KEY, "15")
    _run_watcher(context)
    assert context.watcher.applied_minutes == 15
    context.calls_after_apply = len(context.fake_scheduler.reschedule_calls)


@when('the admin config key "live_refresh_minutes" is deleted')
def step_delete_admin_key(context):
    context.fake_redis.delete(ADMIN_KEY)


@when("the interval watcher runs")
def step_run_watcher(context):
    _run_watcher(context)


@when("the interval watcher runs again with the key still at 15")
def step_run_watcher_again(context):
    _run_watcher(context)


@given("the admin config Redis read raises an error")
def step_redis_read_raises(context):
    context.errors_before = _job_error_count(context)

    def _boom(key):
        raise ConnectionError("BDD harness: simulated Redis failure")

    context.healthy_redis = context.watcher._redis
    context.watcher._redis = SimpleNamespace(get=_boom)


# ── then ─────────────────────────────────────────────────────────────────────
@then(
    "the live_forecast_refresh job must be rescheduled to every " "{minutes:d} minutes"
)
def step_assert_rescheduled(context, minutes):
    calls = context.fake_scheduler.reschedule_calls
    assert calls, "expected a reschedule call, got none"
    job_id, trigger = calls[-1]
    assert job_id == "live_forecast_refresh", job_id
    actual = _trigger_minutes(trigger)
    assert actual == minutes, f"expected {minutes} minutes, got {actual}"


@then("the live refresh interval gauge must report {minutes:d}")
def step_assert_gauge(context, minutes):
    assert (
        _gauge_value() == minutes
    ), f"gauge reports {_gauge_value()}, expected {minutes}"


@then("an info log must record the change from 5 to 15")
def step_assert_info_log(context):
    infos = [
        r.getMessage() for r in context.log_handler.records if r.levelno == logging.INFO
    ]
    assert any("5 -> 15" in m for m in infos), f"info logs: {infos}"


@then("the live_forecast_refresh job must not be rescheduled")
def step_assert_no_new_reschedule(context):
    calls = len(context.fake_scheduler.reschedule_calls)
    assert (
        calls == context.calls_after_apply
    ), f"expected no new reschedule, found {calls - context.calls_after_apply}"


@then("the live_forecast_refresh job must keep its current interval")
def step_assert_interval_kept(context):
    expected = getattr(context, "calls_after_apply", 0)
    calls = len(context.fake_scheduler.reschedule_calls)
    assert calls == expected, f"expected no reschedule beyond {expected}, found {calls}"
    assert context.watcher.applied_minutes in (DEFAULT_MINUTES, 15)


@then("a warning log must record the rejected value")
def step_assert_warning_log(context):
    warnings = [
        r.getMessage()
        for r in context.log_handler.records
        if r.levelno == logging.WARNING
    ]
    assert warnings, "expected a warning log for the rejected value"


@then("the watcher error must be counted in the background job metrics")
def step_assert_error_counted(context):
    after = _job_error_count(context)
    assert (
        after == context.errors_before + 1
    ), f"error count {after}, expected {context.errors_before + 1}"


@then("the watcher must keep running on its next cycle")
def step_assert_keeps_running(context):
    context.watcher._redis = context.healthy_redis
    _run_watcher(context)  # must not raise
