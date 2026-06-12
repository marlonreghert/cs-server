"""Unit tests for app/services/refresh_interval_watch.py."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.metrics import BACKGROUND_JOB_RUNS_TOTAL, LIVE_REFRESH_INTERVAL_MINUTES
from app.services.refresh_interval_watch import (
    ADMIN_LIVE_REFRESH_MINUTES_KEY,
    LIVE_REFRESH_JOB_ID,
    RefreshIntervalWatcher,
)


class FakeRedis:
    def __init__(self, value=None):
        self.value = value

    def get(self, key):
        assert key == ADMIN_LIVE_REFRESH_MINUTES_KEY
        return self.value


class BoomRedis:
    def get(self, key):
        raise ConnectionError("boom")


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def reschedule_job(self, job_id, trigger=None):
        self.calls.append((job_id, trigger))


def make_watcher(value=None, default=5):
    redis = FakeRedis(value)
    scheduler = FakeScheduler()
    watcher = RefreshIntervalWatcher(
        redis_client=redis, scheduler=scheduler, default_minutes=default
    )
    return watcher, scheduler, redis


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("15", 15),
        ("1", 1),
        ("120", 120),
    ],
)
def test_valid_values_reschedule(raw, expected):
    watcher, scheduler, _ = make_watcher(raw)
    watcher.check_once()
    assert watcher.applied_minutes == expected
    job_id, trigger = scheduler.calls[-1]
    assert job_id == LIVE_REFRESH_JOB_ID
    assert trigger.interval.total_seconds() / 60 == expected
    assert LIVE_REFRESH_INTERVAL_MINUTES._value.get() == expected


@pytest.mark.parametrize(
    "raw",
    ["0", "-5", "121", '"fast"', "true", "7.5", "[15]", "not json {"],
)
def test_invalid_values_keep_current(raw):
    watcher, scheduler, _ = make_watcher(raw)
    watcher.check_once()
    assert watcher.applied_minutes == 5
    assert scheduler.calls == []


def test_absent_key_uses_default_without_reschedule():
    watcher, scheduler, _ = make_watcher(None)
    watcher.check_once()
    assert watcher.applied_minutes == 5
    assert scheduler.calls == []


def test_deleting_key_reverts_to_default():
    watcher, scheduler, redis = make_watcher("15")
    watcher.check_once()
    assert watcher.applied_minutes == 15
    redis.value = None
    watcher.check_once()
    assert watcher.applied_minutes == 5
    assert len(scheduler.calls) == 2


def test_unchanged_value_does_not_reschedule_again():
    watcher, scheduler, _ = make_watcher("15")
    watcher.check_once()
    watcher.check_once()
    assert len(scheduler.calls) == 1


def test_bytes_value_is_decoded():
    watcher, scheduler, _ = make_watcher(b"30")
    watcher.check_once()
    assert watcher.applied_minutes == 30


def test_warns_once_per_distinct_rejected_value(caplog):
    watcher, _, redis = make_watcher("999")
    with caplog.at_level(logging.WARNING):
        watcher.check_once()
        watcher.check_once()
        assert sum("Rejected" in r.message for r in caplog.records) == 1
        redis.value = '"fast"'
        watcher.check_once()
        assert sum("Rejected" in r.message for r in caplog.records) == 2


def test_run_counts_redis_errors_and_does_not_raise():
    scheduler = FakeScheduler()
    watcher = RefreshIntervalWatcher(
        redis_client=BoomRedis(), scheduler=scheduler, default_minutes=5
    )
    counter = BACKGROUND_JOB_RUNS_TOTAL.labels(
        job_name="refresh_interval_watch", status="error"
    )
    before = counter._value.get()
    asyncio.run(watcher.run())
    assert counter._value.get() == before + 1
    assert watcher.applied_minutes == 5
    assert scheduler.calls == []


def test_run_counts_success():
    watcher, _, _ = make_watcher("15")
    counter = BACKGROUND_JOB_RUNS_TOTAL.labels(
        job_name="refresh_interval_watch", status="success"
    )
    before = counter._value.get()
    asyncio.run(watcher.run())
    assert counter._value.get() == before + 1
