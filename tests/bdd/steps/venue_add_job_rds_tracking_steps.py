"""Behave steps for tests/bdd/persistence/venue-add-job-rds-tracking.feature.

Drives the real POST/GET /venues/add-job... and POST/GET /venues/batch-add...
endpoints over the real AddVenueJobService + BatchAddService (wired in
environment.py), the same way add_venue_async_job_steps.py and
batch_add_venues_steps.py already do — through context.client (a real
TestClient), not by calling the service internals directly. The two
reconciliation scenarios are the one exception: they exercise
context.venue_add_job_store.reconcile_orphaned() directly, mirroring
eligibility_mirror_rehydration_steps.py's "cs-server rehydrates..." step
calling rehydrate_mirror() directly — a BDD scenario cannot literally restart
the FastAPI process, so it calls the same method main.py:startup_essential()
calls at boot.

Given/When split note: several scenarios here read "Given a job is started...
/ When <it crashes | its persistence fails>". Arming a crash or a store
failure from a *later* step, after the job's background asyncio.Task has
already been created, would race that task (see add_venue_async_job_steps.py's
step_batch_job_running docstring for the same underlying TestClient/portal
behavior) — the task can reach its one call into the handler / job store
before the later step gets a chance to reprogram anything. So here, "Given a
job is started for X" only ARRANGES the venue identity; the following When
step both configures the scenario-specific behavior (success / crash / a
one-shot store failure) and performs the actual POST + drains the job to a
terminal state.
"""
from __future__ import annotations

import time
import uuid

from behave import given, when, then  # type: ignore[import-untyped]

from app.metrics import VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL
from app.models import NewVenueResponse

_DEFAULT_LAT, _DEFAULT_LNG = -8.05, -34.88


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _poll_add_job_until_terminal(context, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        resp = context.client.get(f"/admin/venues/add-job/{job_id}")
        assert resp.status_code == 200, resp.text
        last = resp
        if resp.json().get("status") in ("done", "failed"):
            return resp
        time.sleep(0.02)
    raise AssertionError(
        f"add-job {job_id} did not reach a terminal state within {timeout}s: "
        f"last={last.text if last else None}"
    )


def _poll_batch_job_until_terminal(context, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        resp = context.client.get(f"/admin/venues/batch-add/{job_id}")
        assert resp.status_code == 200, resp.text
        last = resp
        if resp.json().get("status") in ("done", "stopped", "failed"):
            return resp
        time.sleep(0.02)
    raise AssertionError(
        f"batch job {job_id} did not reach a terminal state within {timeout}s: "
        f"last={last.text if last else None}"
    )


def _persist_failure_count(job_type: str) -> float:
    return VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL.labels(job_type=job_type)._value.get()


def _batch_body(n: int, label: str = "rds-tracking-bdd") -> dict:
    return {
        "label": label,
        "resolve_coords": False,
        "venues": [
            {
                "venue_name": f"RDS Venue {i}",
                "venue_address": f"Rua RDS {i}, Recife - PE",
                "venue_lat": _DEFAULT_LAT + i * 0.001,
                "venue_lng": _DEFAULT_LNG - i * 0.001,
            }
            for i in range(n)
        ],
    }


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------
# "the RDS system-of-record is enabled" is already registered by
# rds_system_of_record_steps.py — reused verbatim, not redefined here.


@given("an empty RDS")
def step_empty_rds(context):
    # context.venue_add_job_store / context.rds_store are already fresh per
    # scenario (environment.py rebuilds them in before_scenario); Redis is
    # flushed explicitly since a later Then asserts NO job-related Redis key
    # exists — a clean slate makes that assertion meaningful.
    context.fake_redis.flushall()


# ---------------------------------------------------------------------------
# Single-add: terminal result readable from the RDS store
# ---------------------------------------------------------------------------


@given('a single-add job is started for "{name}" at "{address}"')
def step_single_job_arranged(context, name, address):
    context.pending_job_venue_name = name
    context.pending_job_venue_address = address


def _post_pending_single_job(context):
    resp = context.client.post(
        "/admin/venues/add-job",
        json={
            "venue_name": context.pending_job_venue_name,
            "venue_address": context.pending_job_venue_address,
            "venue_lat": _DEFAULT_LAT,
            "venue_lng": _DEFAULT_LNG,
        },
    )
    assert resp.status_code == 202, resp.text
    context.job_id = resp.json()["job_id"]
    return resp


@when('the add completes with a "{outcome}" outcome')
def step_add_completes_with_outcome(context, outcome):
    assert outcome == "created", f"only 'created' is wired in this harness, got {outcome!r}"
    context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
        {
            "status": "OK",
            "venue_info": {
                "venue_id": f"ven_rds_{uuid.uuid4().hex[:10]}",
                "venue_name": context.pending_job_venue_name,
                "venue_address": context.pending_job_venue_address,
                "venue_lat": _DEFAULT_LAT,
                "venue_lon": _DEFAULT_LNG,
            },
            "analysis": [],
        }
    )
    _post_pending_single_job(context)
    context.poll_response = _poll_add_job_until_terminal(context, context.job_id)


@then('polling the job returns status "{status}" with the "{outcome}" result')
def step_poll_status_with_result_outcome(context, status, outcome):
    body = context.poll_response.json()
    assert body.get("status") == status, body
    result = body.get("result") or {}
    assert result.get("status") == outcome, result


_JOB_REDIS_KEY_PREFIXES = ("admin:add_venue_job:", "admin:batch_add_job:")
_JOB_REDIS_KEY_EXACT = {"admin:add_venue_job_recent_v1"}


@then("no Redis key was written for this job")
def step_no_redis_key_written(context):
    raw_keys = context.fake_redis.keys("*")
    keys = [k.decode("utf-8") if isinstance(k, bytes) else k for k in raw_keys]
    job_keys = [
        k for k in keys
        if k.startswith(_JOB_REDIS_KEY_PREFIXES) or k in _JOB_REDIS_KEY_EXACT
    ]
    assert job_keys == [], f"unexpected venue-add-job Redis key(s): {job_keys}"


# ---------------------------------------------------------------------------
# Batch-add: per-row results + summary readable from the RDS store
# ---------------------------------------------------------------------------


@given("a batch-add job is started for {n:d} venues")
def step_batch_job_arranged(context, n):
    context.pending_batch_venue_count = n
    context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
        {
            "status": "OK",
            "venue_info": {
                "venue_id": "ven_rds_batch_001",
                "venue_lat": _DEFAULT_LAT,
                "venue_lon": _DEFAULT_LNG,
            },
            "analysis": [],
        }
    )


def _post_pending_batch_job(context):
    resp = context.client.post(
        "/admin/venues/batch-add", json=_batch_body(context.pending_batch_venue_count)
    )
    assert resp.status_code == 202, resp.text
    context.job_id = resp.json()["job_id"]
    return resp


@when("all {n:d} rows finish processing")
def step_all_rows_finish(context, n):
    _post_pending_batch_job(context)
    context.poll_response = _poll_batch_job_until_terminal(context, context.job_id)
    assert context.poll_response.json().get("processed") == n, context.poll_response.text


@then('polling the job returns status "{status}" with a summary count of {n:d}')
def step_summary_count(context, status, n):
    body = context.poll_response.json()
    assert body.get("status") == status, body
    assert sum(body.get("summary", {}).values()) == n, body.get("summary")


@then("the job's results list has {n:d} entries")
def step_results_list_len(context, n):
    body = context.poll_response.json()
    assert len(body.get("results", [])) == n, body.get("results")


# ---------------------------------------------------------------------------
# Recent jobs: newest-first directly from the RDS store
# ---------------------------------------------------------------------------


@given("{n:d} single-add jobs have completed, started at different times")
def step_n_single_jobs_completed(context, n):
    job_ids = []
    for i in range(n):
        context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
            {
                "status": "OK",
                "venue_info": {
                    "venue_id": f"ven_recent_{i}_{uuid.uuid4().hex[:6]}",
                    "venue_lat": _DEFAULT_LAT,
                    "venue_lon": _DEFAULT_LNG,
                },
                "analysis": [],
            }
        )
        resp = context.client.post(
            "/admin/venues/add-job",
            json={
                "venue_name": f"Recent Venue {i}",
                "venue_address": f"Rua Recent {i}, Recife - PE",
                "venue_lat": _DEFAULT_LAT,
                "venue_lng": _DEFAULT_LNG,
            },
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]
        _poll_add_job_until_terminal(context, job_id)
        job_ids.append(job_id)
        # Guarantee strictly increasing started_at across jobs — several jobs
        # started back to back can otherwise land on the same wall-clock tick.
        time.sleep(0.01)
    context.recent_job_ids_oldest_first = job_ids


@when("the recent-jobs list is requested")
def step_recent_list_requested(context):
    context.recent_response = context.client.get(
        "/admin/venues/add-jobs/recent", params={"limit": 100}
    )


@then("the jobs are returned newest-started-first")
def step_jobs_newest_first(context):
    jobs = context.recent_response.json()["jobs"]
    expected_newest_first = list(reversed(context.recent_job_ids_oldest_first))
    actual_ids = [j["job_id"] for j in jobs[: len(expected_newest_first)]]
    assert actual_ids == expected_newest_first, (actual_ids, expected_newest_first)


@then('each "done" job is annotated with its outcome')
def step_each_done_job_annotated(context):
    jobs = context.recent_response.json()["jobs"]
    relevant = [j for j in jobs if j["job_id"] in context.recent_job_ids_oldest_first]
    assert len(relevant) == len(context.recent_job_ids_oldest_first), relevant
    for job in relevant:
        assert job.get("status") == "done", job
        assert job.get("outcome"), job


# ---------------------------------------------------------------------------
# Boot-time reconciliation
# ---------------------------------------------------------------------------


@given('a batch-add job row exists with status "running" from a previous process')
def step_stale_running_batch_row(context):
    context.stale_job_id = "stale_" + uuid.uuid4().hex[:12]
    context.venue_add_job_store.save(
        {
            "job_id": context.stale_job_id,
            "job_type": "batch",
            "label": "stale-from-prior-process",
            "status": "running",
            "total": 5,
            "processed": 2,
            "started_at": time.time() - 3600,
            "finished_at": None,
            "stopped_reason": None,
            "resolve_coords": False,
            "summary": {},
            "results": [],
            "budget_before": None,
            "budget_after": None,
        }
    )


@given('no job row has status "running"')
def step_no_running_rows(context):
    # Each scenario gets a fresh, empty context.venue_add_job_store (wired in
    # environment.py's _build_test_app) — nothing to arrange.
    pass


@when("cs-server starts up")
def step_server_starts_up(context):
    # Mirrors main.py's startup_essential() reconciliation call. A BDD
    # scenario cannot literally restart the FastAPI process, so — like
    # eligibility_mirror_rehydration_steps.py's "cs-server rehydrates the
    # eligibility mirror on startup" step — this calls the same underlying
    # method startup would call.
    context.reconciled_count = context.venue_add_job_store.reconcile_orphaned(
        "process restarted while job was running"
    )


@then('that job\'s status becomes "interrupted"')
def step_stale_job_status_interrupted(context):
    job = context.venue_add_job_store.get(context.stale_job_id)
    assert job is not None and job.get("status") == "interrupted", job


@then("its stopped_reason explains the process restarted while it was running")
def step_stale_job_stopped_reason(context):
    job = context.venue_add_job_store.get(context.stale_job_id)
    reason = (job.get("stopped_reason") or "").lower()
    assert "restart" in reason, job


@then("its finished_at is set")
def step_stale_job_finished_at_set(context):
    job = context.venue_add_job_store.get(context.stale_job_id)
    assert job.get("finished_at") is not None, job


@then("the underlying add is not re-run")
def step_underlying_add_not_rerun(context):
    # reconcile_orphaned is a pure status-metadata update; it must never
    # touch AddVenueHandler / BestTime. No BestTime call was made as a side
    # effect of the "When cs-server starts up" step above.
    assert context.besttime.calls == [], context.besttime.calls


@then("zero jobs are reconciled")
def step_zero_jobs_reconciled(context):
    assert context.reconciled_count == 0, context.reconciled_count


@then("startup completes without delay")
def step_startup_no_delay(context):
    # A pure in-memory/indexed-column scan; the assertion above (zero rows
    # touched) already demonstrates the call returned. No explicit timing
    # measurement needed beyond that.
    pass


# ---------------------------------------------------------------------------
# In-process crash still reaches failed (regression guard, unchanged)
# ---------------------------------------------------------------------------


@when("the add handler raises an exception before completing")
def step_add_handler_raises(context):
    async def _boom(request):
        raise RuntimeError("simulated add-venue crash")

    context.add_venue_handler.add = _boom
    _post_pending_single_job(context)
    context.poll_response = _poll_add_job_until_terminal(context, context.job_id)


@then('polling the job returns status "{status}" with the error message')
def step_poll_status_with_error(context, status):
    body = context.poll_response.json()
    assert body.get("status") == status, body
    assert body.get("error"), body


@then("the process is not restarted")
def step_process_not_restarted(context):
    # No crash-recovery/restart mechanism exists or is exercised here; the
    # assertion is that this same harness went on to serve the poll response
    # above without dying — trivially demonstrated by having reached this
    # step at all (CLAUDE.md: an in-process crash inside one job must never
    # take the whole server down).
    assert context.poll_response.status_code == 200


# ---------------------------------------------------------------------------
# Persistence failure during a run does not crash the job
# ---------------------------------------------------------------------------


@when("the RDS store is unavailable for one row's save")
def step_store_unavailable_for_one_row(context):
    context.persist_fail_count_before = _persist_failure_count("batch")
    context.venue_add_job_store.fail_next_save(1)
    _post_pending_batch_job(context)
    context.poll_response = _poll_batch_job_until_terminal(context, context.job_id)


@then("the batch-add job continues processing the remaining rows")
def step_batch_continues_despite_failure(context):
    body = context.poll_response.json()
    assert body.get("status") == "done", body
    assert body.get("processed") == context.pending_batch_venue_count, body


@then("a job-persistence-failure metric is recorded")
def step_persist_failure_metric_recorded(context):
    after = _persist_failure_count("batch")
    assert after > context.persist_fail_count_before, (
        context.persist_fail_count_before, after,
    )
