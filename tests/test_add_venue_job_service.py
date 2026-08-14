"""Unit tests for the single-add-venue background job service.

Mirrors tests/test_batch_add_service.py's harness shape (a scripted handler
stub + fakeredis + a small task-draining helper), since AddVenueJobService
is a sibling to BatchAddService with the same job-doc persistence pattern.
See plans/260813_add-venue-async-job.md.
"""
import asyncio

import fakeredis
import pytest

from app.handlers.add_venue_handler import AddVenueByAddressRequest, AddVenueOutcome
from app.services.add_venue_job_service import (
    ADD_VENUE_RECENT_JOBS_CAP,
    JOB_KEY_FMT,
    JOB_TTL_SECONDS,
    RECENT_JOBS_KEY,
    AddVenueJobService,
)


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
    return AddVenueJobService(
        handler=handler, redis_client=fakeredis.FakeStrictRedis(decode_responses=False)
    )


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
async def test_start_job_sets_a_ttl_on_the_persisted_doc():
    handler = _Handler(
        script={"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})}
    )
    svc = _service(handler)
    accepted = svc.start_job(_req())
    ttl = svc.redis.ttl(JOB_KEY_FMT.format(job_id=accepted["job_id"]))
    assert 0 < ttl <= JOB_TTL_SECONDS
    await _drain(svc, accepted["job_id"])


@pytest.mark.asyncio
async def test_start_job_pushes_onto_the_recent_jobs_index():
    handler = _Handler(
        script={"A": AddVenueOutcome(201, {"status": "created", "venue_id": "vA"})}
    )
    svc = _service(handler)
    accepted = svc.start_job(_req())
    ids = svc.redis.lrange(RECENT_JOBS_KEY, 0, -1)
    ids = [i.decode("utf-8") if isinstance(i, bytes) else i for i in ids]
    assert ids == [accepted["job_id"]]
    await _drain(svc, accepted["job_id"])


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
async def test_get_job_round_trips_through_redis_json():
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


def test_list_recent_skips_a_missing_job_id_without_raising():
    svc = _service(_Handler())
    # Simulate an id that aged out past its TTL (or was trimmed): present in
    # the index, but with no job doc behind it.
    svc.redis.lpush(RECENT_JOBS_KEY, "ghost-job-id")
    jobs = svc.list_recent(limit=20)
    assert jobs == []


@pytest.mark.asyncio
async def test_list_recent_respects_the_cap_under_ltrim():
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

    jobs = svc.list_recent(limit=total)
    assert len(jobs) == ADD_VENUE_RECENT_JOBS_CAP
    # Newest (last-started) first; the oldest 5 were trimmed off the index.
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
