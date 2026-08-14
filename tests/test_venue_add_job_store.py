"""Unit tests for VenueAddJobStore's behaviour contract.

Exercised through tests.venue_add_job_fake.InMemoryVenueAddJobStore — no live
Postgres (see app/dao/venue_add_job_store.py's own module docstring: its SQL
is validated by the post-provisioning smoke test, not the offline suite,
matching this repo's established RdsVenueStore/InMemoryRdsVenueStore
convention). `shape_job_row` and the epoch<->datetime helpers are pure
functions with no I/O, so they are tested directly as well.
"""
import pytest

from app.dao.venue_add_job_store import _dt_to_epoch, _epoch_to_dt, shape_job_row
from tests.venue_add_job_fake import InMemoryVenueAddJobStore


def _single_job(job_id="j1", status="running", started_at=100.0, **overrides):
    job = {
        "job_id": job_id, "job_type": "single", "status": status,
        "started_at": started_at, "venue_name": "V", "venue_address": "A",
    }
    job.update(overrides)
    return job


def _batch_job(job_id="b1", status="running", started_at=100.0, **overrides):
    job = {
        "job_id": job_id, "job_type": "batch", "label": "L", "status": status,
        "total": 3, "processed": 0, "started_at": started_at, "finished_at": None,
        "stopped_reason": None, "resolve_coords": False, "summary": {}, "results": [],
        "budget_before": None, "budget_after": None,
    }
    job.update(overrides)
    return job


# ── save / get ────────────────────────────────────────────────────────────────
class TestSaveAndGet:
    def test_save_then_get_round_trips(self):
        store = InMemoryVenueAddJobStore()
        store.save(_single_job())
        job = store.get("j1")
        assert job["job_id"] == "j1"
        assert job["status"] == "running"

    def test_get_unknown_returns_none(self):
        store = InMemoryVenueAddJobStore()
        assert store.get("nope") is None

    def test_second_save_for_the_same_job_id_updates_not_duplicates(self):
        store = InMemoryVenueAddJobStore()
        store.save(_single_job(status="running"))
        store.save(_single_job(
            status="done", finished_at=200.0, http_status=201,
            result={"status": "created"},
        ))
        assert len(store.rows) == 1
        job = store.get("j1")
        assert job["status"] == "done"
        assert job["finished_at"] == 200.0
        assert job["result"] == {"status": "created"}


# ── shape_job_row: single-add presence rules ────────────────────────────────
class TestShapeJobRowSinglePresence:
    def test_running_single_job_omits_terminal_fields_entirely(self):
        """AddVenueJobService never initializes finished_at/http_status/
        result/error up front — they must be ABSENT (not null) until set,
        matching the byte-for-byte HTTP response contract."""
        store = InMemoryVenueAddJobStore()
        store.save(_single_job())
        job = store.get("j1")
        for key in ("finished_at", "http_status", "result", "error"):
            assert key not in job, job
        assert "job_type" not in job

    def test_done_single_job_includes_result_but_not_error(self):
        store = InMemoryVenueAddJobStore()
        store.save(_single_job(
            status="done", finished_at=1.0, http_status=201, result={"venue_id": "v1"},
        ))
        job = store.get("j1")
        assert job["result"] == {"venue_id": "v1"}
        assert job["http_status"] == 201
        assert job["finished_at"] == 1.0
        assert "error" not in job

    def test_failed_single_job_includes_error_but_not_result(self):
        store = InMemoryVenueAddJobStore()
        store.save(_single_job(status="failed", finished_at=1.0, error="boom"))
        job = store.get("j1")
        assert job["error"] == "boom"
        assert "result" not in job
        assert "http_status" not in job

    def test_single_job_never_carries_batch_only_fields(self):
        store = InMemoryVenueAddJobStore()
        store.save(_single_job())
        job = store.get("j1")
        for key in (
            "label", "total", "processed", "resolve_coords", "summary",
            "results", "budget_before", "budget_after", "stopped_reason",
        ):
            assert key not in job, (key, job)


# ── shape_job_row: batch presence rules ─────────────────────────────────────
class TestShapeJobRowBatchPresence:
    def test_batch_job_always_includes_its_own_fields_even_when_null(self):
        """BatchAddService initializes every one of these keys at
        start_job() time, several to an explicit None — they must stay
        PRESENT (null), never disappear, unlike single-add's optional
        fields."""
        store = InMemoryVenueAddJobStore()
        store.save(_batch_job())
        job = store.get("b1")
        for key in ("finished_at", "stopped_reason", "budget_before", "budget_after"):
            assert key in job, job
            assert job[key] is None

    def test_batch_job_never_carries_single_only_fields(self):
        store = InMemoryVenueAddJobStore()
        store.save(_batch_job())
        job = store.get("b1")
        for key in ("venue_name", "venue_address", "http_status", "result", "error"):
            assert key not in job, (key, job)
        assert "job_type" not in job

    def test_unrecognised_job_type_defaults_to_batch_shape_without_raising(self):
        row = {"job_id": "x", "job_type": "something-unexpected", "status": "done"}
        shaped = shape_job_row(row)
        assert shaped["job_id"] == "x"
        assert "venue_name" not in shaped


# ── list_recent ──────────────────────────────────────────────────────────────
class TestListRecent:
    def test_ordered_newest_first(self):
        store = InMemoryVenueAddJobStore()
        store.save(_single_job(job_id="a", started_at=100.0))
        store.save(_single_job(job_id="b", started_at=300.0))
        store.save(_single_job(job_id="c", started_at=200.0))
        jobs = store.list_recent(10)
        assert [j["job_id"] for j in jobs] == ["b", "c", "a"]

    def test_respects_limit(self):
        store = InMemoryVenueAddJobStore()
        for i in range(5):
            store.save(_single_job(job_id=f"j{i}", started_at=float(i)))
        jobs = store.list_recent(2)
        assert [j["job_id"] for j in jobs] == ["j4", "j3"]

    def test_job_type_filter_excludes_the_other_type(self):
        store = InMemoryVenueAddJobStore()
        store.save(_single_job(job_id="s1", started_at=1.0))
        store.save(_batch_job(job_id="b1", started_at=2.0))
        jobs = store.list_recent(10, job_type="single")
        assert [j["job_id"] for j in jobs] == ["s1"]

    def test_no_job_type_filter_returns_both_types(self):
        store = InMemoryVenueAddJobStore()
        store.save(_single_job(job_id="s1", started_at=1.0))
        store.save(_batch_job(job_id="b1", started_at=2.0))
        jobs = store.list_recent(10)
        assert {j["job_id"] for j in jobs} == {"s1", "b1"}


# ── reconcile_orphaned ───────────────────────────────────────────────────────
class TestReconcileOrphaned:
    def test_running_row_becomes_interrupted(self):
        store = InMemoryVenueAddJobStore()
        store.save(_batch_job(job_id="b1", status="running"))
        fixed = store.reconcile_orphaned("process restarted while job was running")
        assert fixed == 1
        job = store.get("b1")
        assert job["status"] == "interrupted"
        assert "restart" in job["stopped_reason"].lower()
        assert job["finished_at"] is not None

    def test_non_running_statuses_are_left_untouched(self):
        store = InMemoryVenueAddJobStore()
        store.save(_batch_job(job_id="done1", status="done", finished_at=5.0))
        store.save(_batch_job(
            job_id="stopped1", status="stopped", finished_at=5.0, stopped_reason="quota",
        ))
        store.save(_single_job(job_id="failed1", status="failed", finished_at=5.0, error="boom"))
        store.save(_batch_job(
            job_id="interrupted1", status="interrupted", finished_at=5.0,
            stopped_reason="an earlier reconciliation",
        ))
        fixed = store.reconcile_orphaned("process restarted while job was running")
        assert fixed == 0
        assert store.get("done1")["status"] == "done"
        assert store.get("stopped1")["status"] == "stopped"
        assert store.get("failed1")["status"] == "failed"
        assert store.get("interrupted1")["status"] == "interrupted"

    def test_only_running_rows_among_a_mix_are_touched(self):
        store = InMemoryVenueAddJobStore()
        store.save(_batch_job(job_id="r1", status="running"))
        store.save(_batch_job(job_id="d1", status="done", finished_at=5.0))
        store.save(_single_job(job_id="r2", status="running"))
        fixed = store.reconcile_orphaned("reason")
        assert fixed == 2
        assert store.get("d1")["status"] == "done"
        assert store.get("r1")["status"] == "interrupted"
        assert store.get("r2")["status"] == "interrupted"

    def test_no_running_rows_reconciles_zero(self):
        store = InMemoryVenueAddJobStore()
        store.save(_batch_job(job_id="d1", status="done", finished_at=5.0))
        assert store.reconcile_orphaned("reason") == 0


# ── fail_next_save (persistence-failure BDD scenario support) ──────────────
class TestFailNextSave:
    def test_fail_next_save_raises_once_then_recovers(self):
        store = InMemoryVenueAddJobStore()
        store.fail_next_save(1)
        with pytest.raises(RuntimeError):
            store.save(_single_job())
        assert store.get("j1") is None
        store.save(_single_job())  # succeeds now
        assert store.get("j1") is not None


# ── epoch <-> datetime conversion (real store's timestamptz boundary) ──────
class TestEpochDatetimeConversion:
    def test_epoch_round_trips_through_datetime(self):
        epoch = 1_800_000_000.5
        dt = _epoch_to_dt(epoch)
        assert dt is not None
        assert _dt_to_epoch(dt) == pytest.approx(epoch)

    def test_none_round_trips_as_none(self):
        assert _epoch_to_dt(None) is None
        assert _dt_to_epoch(None) is None

    def test_dt_to_epoch_passes_through_a_float_unchanged(self):
        # Defensive: a value that is already a float (never happens against
        # real Postgres, but keeps the helper total) must not raise.
        assert _dt_to_epoch(123.0) == 123.0
