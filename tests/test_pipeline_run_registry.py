"""Unit tests for the pipeline run registry.

The load-bearing property is the CARDINALITY CEILING: `job_id` is a Prometheus
label only because the ring bounds it. If eviction regresses, this metric grows
without bound exactly like the Loki label the design rejected — so the ceiling is
asserted by counting series, not by trusting that evict was called.
"""
import logging

import pytest
from prometheus_client import REGISTRY

from app.services import pipeline_run_registry as reg


def _series(pipeline=None):
    out = []
    for metric in REGISTRY.collect():
        if metric.name != "pipeline_run_info":
            continue
        for s in metric.samples:
            if pipeline is None or s.labels.get("pipeline") == pipeline:
                out.append((s.labels["pipeline"], s.labels["job_id"], s.labels["status"], s.value))
    return out


@pytest.fixture(autouse=True)
def clean_registry():
    reg.reset_registry()
    reg.configure(enabled=True, size=reg.DEFAULT_RING_SIZE)
    yield
    reg.reset_registry()
    reg.configure(enabled=True, size=reg.DEFAULT_RING_SIZE)


class TestRunId:
    def test_is_time_ordered_across_milliseconds(self):
        ids = [reg.new_run_id() for _ in range(5)]
        assert ids == sorted(ids)

    def test_is_monotonic_within_one_millisecond(self):
        """Two runs in the same ms would otherwise differ only in randomness and
        sort arbitrarily, breaking the ordering the dashboards rely on."""
        from datetime import datetime, timezone

        fixed = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
        ids = [reg.new_run_id(fixed) for _ in range(20)]
        assert ids == sorted(ids)
        assert len(set(ids)) == 20

    def test_has_ulid_shape(self):
        rid = reg.new_run_id()
        assert len(rid) == 26
        assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in rid)


class TestRunScope:
    def test_registers_running_then_success(self):
        with reg.run_scope("p1") as rid:
            running = [r for r in _series("p1") if r[1] == rid]
            assert running and running[0][2] == reg.STATUS_RUNNING
        done = [r for r in _series("p1") if r[1] == rid]
        assert done and done[0][2] == reg.STATUS_SUCCESS

    def test_a_run_never_appears_under_two_statuses(self):
        with reg.run_scope("p1") as rid:
            pass
        assert len([r for r in _series("p1") if r[1] == rid]) == 1

    def test_records_error_and_reraises(self):
        with pytest.raises(RuntimeError):
            with reg.run_scope("p1") as rid:
                raise RuntimeError("boom")
        rows = [r for r in _series("p1") if r[1] == rid]
        assert rows and rows[0][2] == reg.STATUS_ERROR

    def test_value_is_the_start_time(self):
        import time as _t

        before = _t.time()
        with reg.run_scope("p1") as rid:
            pass
        row = [r for r in _series("p1") if r[1] == rid][0]
        assert before - 1 <= row[3] <= _t.time() + 1

    def test_exposes_the_current_run_inside_the_scope(self):
        assert reg.current_run() == (None, None)
        with reg.run_scope("p1") as rid:
            assert reg.current_run() == ("p1", rid)
        assert reg.current_run() == (None, None)


class TestCardinalityCeiling:
    def test_keeps_only_the_last_n_runs(self):
        reg.configure(enabled=True, size=3)
        for _ in range(9):
            with reg.run_scope("p1"):
                pass
        assert len(_series("p1")) == 3

    def test_evicts_the_oldest_first(self):
        reg.configure(enabled=True, size=2)
        ids = []
        for _ in range(4):
            with reg.run_scope("p1") as rid:
                ids.append(rid)
        remaining = {r[1] for r in _series("p1")}
        assert remaining == set(ids[-2:])

    def test_pipelines_do_not_evict_each_other(self):
        reg.configure(enabled=True, size=2)
        for _ in range(5):
            with reg.run_scope("noisy"):
                pass
        with reg.run_scope("quiet"):
            pass
        assert len(_series("noisy")) == 2
        assert len(_series("quiet")) == 1

    def test_series_count_is_flat_under_sustained_load(self):
        """The whole justification for job_id being a Prometheus label."""
        reg.configure(enabled=True, size=5)
        for _ in range(200):
            with reg.run_scope("p1"):
                pass
        assert len(_series("p1")) == 5


class TestNeverBreaksAPipeline:
    def test_a_failing_registry_does_not_fail_the_run(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(reg._REGISTRY_IMPL, "register", _boom)
        with reg.run_scope("p1") as rid:  # must not raise
            assert rid

    def test_disabled_still_mints_ids_and_registers_nothing(self):
        reg.configure(enabled=False, size=5)
        with reg.run_scope("p1") as rid:
            assert rid
        assert _series("p1") == []


class TestLogStamping:
    def _capture(self, name):
        records = []

        class _H(logging.Handler):
            def emit(self, record):
                records.append(record)

        log = logging.getLogger(name)
        h = _H()
        h.addFilter(reg.RunIdLoggingFilter())
        log.addHandler(h)
        log.setLevel(logging.INFO)
        return log, records, h

    def test_stamps_lines_inside_a_run(self):
        log, records, h = self._capture("t.stamp")
        try:
            with reg.run_scope("p1") as rid:
                log.info("working")
        finally:
            log.removeHandler(h)
        assert f"job={rid}" in records[0].getMessage()

    def test_does_not_stamp_outside_a_run(self):
        log, records, h = self._capture("t.nostamp")
        try:
            log.info("idle")
        finally:
            log.removeHandler(h)
        assert "job=" not in records[0].getMessage()

    def test_does_not_double_stamp(self):
        """The photo archive already writes its own job=<id>."""
        log, records, h = self._capture("t.double")
        try:
            with reg.run_scope("p1") as rid:
                log.info(f"already tagged job={rid}")
        finally:
            log.removeHandler(h)
        assert records[0].getMessage().count(f"job={rid}") == 1
