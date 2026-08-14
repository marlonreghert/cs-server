"""Unit tests for the single-add-venue background job service.

Mirrors tests/test_batch_add_service.py's harness shape (a scripted handler
stub + a small task-draining helper), since AddVenueJobService is a sibling
to BatchAddService with the same job-doc persistence pattern — now backed by
the shared RDS job store (tests.venue_add_job_fake.InMemoryVenueAddJobStore)
instead of Redis. See plans/260813_add-venue-async-job.md and
plans/260814_venue-add-job-rds-tracking.md.
"""
import asyncio

import pytest

from app.handlers.add_venue_handler import AddVenueByAddressRequest, AddVenueOutcome
from app.services.add_venue_job_service import (
    ADD_VENUE_RECENT_JOBS_CAP,
    AddVenueJobService,
)
from tests.venue_add_job_fake import InMemoryVenueAddJobStore


class _Handler:
    """Scripted handler: add() returns a scripted AddVenueOutcome for a
    venue_name, or raises a scripted exception. Records every call."""

    def __init__(self, script=None, raises=None):
        self.script = script or {}
        self.raises = raises or {}
        self.calls = []

    async def add(self, request):
        self.calls.append(request.venue_name)
        if request.venue_name in self.raises:
            raise self.raises[request.venue_name]
        return self.script[request.venue_name]


def _service(handler):
    return AddVenueJobService(handler=handler, job_store=InMemoryVenueAddJobStore())


def _req(name="A", address="addr A", lat=-9.6, lng=-35.7):
    return AddVenueByAddressRequest(
        venue_name=name, venue_address=address, venue_lat=lat, venue_lng=lng
    )


async def _drain(svc, job_id, iters=200):
    """Let the background task scheduled by start_job() run to completion."""
    for _ in range(iters):
        task = svc._tasks.get(job_id)
        if task is None:
            break
        await asyncio.sleep(0)
        if task.done():
            break
    await asyncio.sleep(0)


# ── start_job ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_job_returns_immediately_and_persists_running_doc():
    handler = _Handler(
        script={"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})}
    )
    svc = _service(handler)
    accepted = svc.start_job(_req())
    assert accepted["status"] == "running"
    assert accepted["job_id"]

    job = svc.get_job(accepted["job_id"])
    assert job["status"] == "running"
    assert job["venue_name"] == "A"
    assert job["venue_address"] == "addr A"
    assert "started_at" in job
    # start_job must never await handler.add() itself — only the scheduled
    # background task calls it.
    assert handler.calls == []
    await _drain(svc, accepted["job_id"])


@pytest.mark.asyncio
async def test_start_job_stores_the_row_as_job_type_single():
    """The job_store row must carry job_type="single" so shape_job_row
    applies the single (not batch) API-shape rules — checked via the raw
    stored row, since get_job() strips job_type from the API-facing dict by
    design (it must never appear in an HTTP response)."""
    handler = _Handler(
        script={"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})}
    )
    svc = _service(handler)
    accepted = svc.start_job(_req())
    stored = svc.job_store.rows[accepted["job_id"]]
    assert stored["job_type"] == "single"
    await _drain(svc, accepted["job_id"])


@pytest.mark.asyncio
async def test_persist_failure_is_swallowed_and_recorded_but_job_still_runs():
    """A job_store.save() failure must not crash the run (matches the
    pre-RDS Redis _save's own except-and-continue behaviour) and must
    increment VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL{job_type="single"} so a
    sustained RDS outage is observable."""
    from app.metrics import VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL

    before = VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL.labels(job_type="single")._value.get()

    handler = _Handler(
        script={"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})}
    )
    svc = _service(handler)
    svc.job_store.fail_next_save(1)  # fails the very first (initial) save
    accepted = svc.start_job(_req())
    await _drain(svc, accepted["job_id"])

    job = svc.get_job(accepted["job_id"])
    assert job["status"] == "done"  # the run completed despite the one failed save
    after = VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL.labels(job_type="single")._value.get()
    assert after == before + 1


# ── _run_job ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_job_persists_outcome_body_verbatim_on_success():
    outcome = AddVenueOutcome(
        201, {"status": "created", "venue_id": "vA", "source": "besttime_new"}
    )
    handler = _Handler(script={"A": outcome})
    svc = _service(handler)
    accepted = svc.start_job(_req())
    await _drain(svc, accepted["job_id"])

    job = svc.get_job(accepted["job_id"])
    assert job["status"] == "done"
    assert job["http_status"] == 201
    assert job["result"] == outcome.body
    assert "finished_at" in job
    assert handler.calls == ["A"]


@pytest.mark.asyncio
async def test_run_job_catches_an_exception_and_persists_failed_never_raises():
    handler = _Handler(raises={"A": RuntimeError("boom")})
    svc = _service(handler)
    accepted = svc.start_job(_req())

    # Draining must not propagate the handler's exception out of the task.
    await _drain(svc, accepted["job_id"])

    job = svc.get_job(accepted["job_id"])
    assert job["status"] == "failed"
    assert "RuntimeError" in job["error"]
    assert "boom" in job["error"]
    assert "finished_at" in job
    # A crashed job must never carry a stale/fabricated result.
    assert "result" not in job
    assert "http_status" not in job


@pytest.mark.asyncio
async def test_run_job_crash_path_resave_preserves_job_type():
    """Regression test: the crash path re-fetches via self.get_job(job_id)
    (job_type stripped by shape_job_row) before re-saving. _save() must
    re-stamp job_type before writing back, or this second save would
    silently drop the column (an explicit-column upsert, not a partial
    patch)."""
    handler = _Handler(raises={"A": RuntimeError("boom")})
    svc = _service(handler)
    accepted = svc.start_job(_req())
    await _drain(svc, accepted["job_id"])

    stored = svc.job_store.rows[accepted["job_id"]]
    assert stored["job_type"] == "single"
    assert stored["status"] == "failed"


@pytest.mark.asyncio
async def test_run_job_logs_the_crash_with_venue_context(caplog):
    handler = _Handler(raises={"A": RuntimeError("boom")})
    svc = _service(handler)
    with caplog.at_level("ERROR"):
        accepted = svc.start_job(_req(name="A", address="addr A"))
        await _drain(svc, accepted["job_id"])
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "A" in messages
    assert "addr A" in messages
    assert "RuntimeError" in messages


# ── get_job ───────────────────────────────────────────────────────────────


def test_get_job_unknown_returns_none():
    svc = _service(_Handler())
    assert svc.get_job("does-not-exist") is None


@pytest.mark.asyncio
async def test_get_job_round_trips_through_the_job_store():
    handler = _Handler(
        script={"A": AddVenueOutcome(200, {"status": "already_exists", "venue_id": "vA"})}
    )
    svc = _service(handler)
    accepted = svc.start_job(_req())
    await _drain(svc, accepted["job_id"])

    reread = svc.get_job(accepted["job_id"])
    assert reread["job_id"] == accepted["job_id"]
    assert reread["result"]["venue_id"] == "vA"


# ── list_recent ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_recent_is_newest_first():
    handler = _Handler(
        script={
            "A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"}),
            "B": AddVenueOutcome(201, {"status": "created", "venue_id": "vB"}),
        }
    )
    svc = _service(handler)
    first = svc.start_job(_req(name="A", address="a"))
    await _drain(svc, first["job_id"])
    second = svc.start_job(_req(name="B", address="b"))
    await _drain(svc, second["job_id"])

    jobs = svc.list_recent(limit=20)
    assert [j["job_id"] for j in jobs] == [second["job_id"], first["job_id"]]


@pytest.mark.asyncio
async def test_list_recent_excludes_batch_jobs_sharing_the_same_store():
    """The shared admin.venue_add_job_run table backs BOTH job types
    (plans/260814_venue-add-job-rds-tracking.md); list_recent() must filter
    to job_type="single" so a batch-add job never leaks into the single-add
    recent-jobs list."""
    handler = _Handler(
        script={"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})}
    )
    svc = _service(handler)
    svc.job_store.save({
        "job_id": "a-batch-job", "job_type": "batch", "label": "L", "status": "done",
        "total": 1, "processed": 1, "started_at": 999999.0, "finished_at": 999999.0,
        "stopped_reason": None, "resolve_coords": False, "summary": {"created": 1},
        "results": [], "budget_before": None, "budget_after": None,
    })
    accepted = svc.start_job(_req())
    await _drain(svc, accepted["job_id"])

    jobs = svc.list_recent(limit=20)
    assert [j["job_id"] for j in jobs] == [accepted["job_id"]]


@pytest.mark.asyncio
async def test_list_recent_respects_the_response_cap_regardless_of_storage_size():
    """RDS storage is unbounded (no TTL, no capped index) — the response cap
    is purely a response-shaping limit applied before the store is even
    queried."""
    total = ADD_VENUE_RECENT_JOBS_CAP + 5
    handler = _Handler(
        script={
            f"V{i}": AddVenueOutcome(201, {"status": "created", "venue_id": f"v{i}"})
            for i in range(total)
        }
    )
    svc = _service(handler)
    job_ids = []
    for i in range(total):
        accepted = svc.start_job(_req(name=f"V{i}", address=f"addr {i}"))
        await _drain(svc, accepted["job_id"])
        job_ids.append(accepted["job_id"])

    # All rows genuinely persist — nothing is trimmed from storage, unlike
    # the old capped Redis LIST index.
    assert len(svc.job_store.rows) == total

    jobs = svc.list_recent(limit=total)
    assert len(jobs) == ADD_VENUE_RECENT_JOBS_CAP
    # Newest (last-started) first.
    assert jobs[0]["job_id"] == job_ids[-1]
    assert job_ids[0] not in [j["job_id"] for j in jobs]


@pytest.mark.asyncio
async def test_list_recent_never_returns_more_than_the_cap_regardless_of_limit():
    handler = _Handler(
        script={"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})}
    )
    svc = _service(handler)
    accepted = svc.start_job(_req())
    jobs = svc.list_recent(limit=10_000)
    assert len(jobs) <= ADD_VENUE_RECENT_JOBS_CAP
    await _drain(svc, accepted["job_id"])


@pytest.mark.asyncio
async def test_list_recent_honors_a_smaller_limit_than_the_cap():
    handler = _Handler(
        script={
            "A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"}),
            "B": AddVenueOutcome(201, {"status": "created", "venue_id": "vB"}),
        }
    )
    svc = _service(handler)
    first = svc.start_job(_req(name="A", address="a"))
    await _drain(svc, first["job_id"])
    second = svc.start_job(_req(name="B", address="b"))
    await _drain(svc, second["job_id"])

    jobs = svc.list_recent(limit=1)
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == second["job_id"]


@pytest.mark.asyncio
async def test_list_recent_annotates_a_done_job_with_the_classify_outcome():
    outcome = AddVenueOutcome(200, {"status": "already_exists", "venue_id": "vA"})
    handler = _Handler(script={"A": outcome})
    svc = _service(handler)
    accepted = svc.start_job(_req())
    await _drain(svc, accepted["job_id"])

    jobs = svc.list_recent(limit=20)
    assert jobs[0]["outcome"] == "already_exists"


@pytest.mark.asyncio
async def test_list_recent_annotates_a_besttime_rejection_with_its_message():
    outcome = AddVenueOutcome(
        502,
        {
            "detail": "BestTime rejected the address and the geo fallback found no matching venue",
            "besttime_message": "too new",
            "candidates_seen": 0,
        },
    )
    handler = _Handler(script={"A": outcome})
    svc = _service(handler)
    accepted = svc.start_job(_req())
    await _drain(svc, accepted["job_id"])

    jobs = svc.list_recent(limit=20)
    assert jobs[0]["outcome"] == "besttime_rejected_no_geo_match"
    assert jobs[0]["result"]["besttime_message"] == "too new"


@pytest.mark.asyncio
async def test_list_recent_running_job_has_no_outcome_key():
    handler = _Handler(
        script={"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})}
    )
    svc = _service(handler)
    accepted = svc.start_job(_req())
    jobs = svc.list_recent(limit=20)
    assert jobs[0]["status"] == "running"
    assert "outcome" not in jobs[0]
    await _drain(svc, accepted["job_id"])


@pytest.mark.asyncio
async def test_list_recent_failed_job_has_no_outcome_key_but_has_error():
    handler = _Handler(raises={"A": RuntimeError("boom")})
    svc = _service(handler)
    accepted = svc.start_job(_req())
    await _drain(svc, accepted["job_id"])

    jobs = svc.list_recent(limit=20)
    assert jobs[0]["status"] == "failed"
    assert "outcome" not in jobs[0]
    assert jobs[0]["error"]
