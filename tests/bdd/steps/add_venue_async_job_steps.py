"""Behave steps for tests/bdd/api/add-venue-async-job.feature.

Drives the real POST /venues/add-job, GET /venues/add-job/{job_id}, and GET
/venues/add-jobs/recent endpoints over the real AddVenueJobService +
AddVenueHandler (wired in environment.py), the same way
batch_add_venues_steps.py drives the batch endpoints: through
context.client (a real TestClient), not by calling the service directly.

Several Given steps here intentionally reuse literal step text already
registered by add_venue_timeout_recovery_steps.py ("a venue create that
exceeds the BestTime timeout", "the venue appears in the BestTime account
inventory", "BestTime rejects a venue create with an explanatory message") so
this feature does not duplicate step registrations for the same Gherkin
sentence. Those existing steps stash their setup on `context` for the
real-BestTime-client harness (besttime_add_response_parse_steps.py); since
this feature's own When/Given steps drive the plain _ProgrammableBestTime
stub instead (like add_venue_by_address_steps.py does), `_consume_reused_besttime_givens`
bridges that stashed setup onto `context.besttime` the first time a job is
started.
"""
from __future__ import annotations

import time
import uuid

from behave import given, when, then  # type: ignore[import-untyped]

from app.models import NewVenueResponse, VenueFilterResponse, VenueFilterVenue

# Matches besttime_add_response_parse_steps._VENUE exactly: the reconcile
# match row programmed by the reused "the venue appears in the BestTime
# account inventory" step ("LAÇA BURGUER, BOA VIAGEM" / "Av. Conselheiro
# Aguiar, 123, ...") folds to this name/address, so "a new venue" (used with
# that Given, and with the timeout/rejection Givens) must be this venue for
# the reconcile/geo-fallback matches in this feature to actually hit.
_DEFAULT_NEW_VENUE = {
    "venue_name": "Laca Burguer Boa Viagem",
    "venue_address": "Av. Conselheiro Aguiar 123, Recife - PE, Brazil",
    "venue_lat": -8.119,
    "venue_lng": -34.904,
}

# Generous relative to the programmed create delay (see
# step_create_is_delayed) so CI jitter cannot make a genuinely-immediate
# response look slow.
_IMMEDIATE_RESPONSE_THRESHOLD_SECONDS = 0.3
_CREATE_DELAY_SECONDS = 0.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pop_context_attr(context, name):
    """getattr + delattr for a plain (non-underscore) behave Context
    attribute. Context.__setattr__ stores these in a layered stack, not in
    context.__dict__ (only "_"-prefixed attributes go there — see
    behave.runner.Context.__setattr__/__getattr__), so a plain
    context.__dict__.pop(name, None) never finds or removes them; this is
    the correct one-shot read-and-consume for a plain attribute."""
    value = getattr(context, name, None)
    if value is not None:
        try:
            delattr(context, name)
        except AttributeError:
            pass
    return value


def _consume_reused_besttime_givens(context) -> None:
    """Bridge setup stashed by reused Given steps from the real-BestTime-
    client harness onto context.besttime (the plain stub this feature's own
    steps drive), then drop it so it can never leak into a later job start
    within the same scenario or a later scenario."""
    error = _pop_context_attr(context, "besttime_http_error")
    if error is not None:
        context.besttime.programmed_add_venue = error
        context._besttime_special_programmed = True

    reject_message = _pop_context_attr(context, "besttime_reject_message")
    if reject_message is not None:
        context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
            {"status": "Error", "message": reject_message}
        )
        context.expected_besttime_reject_message = reject_message
        context._besttime_special_programmed = True
    # Stashed alongside besttime_reject_message by the reused step but not
    # used by this stub-based harness; drop it too so it never leaks.
    _pop_context_attr(context, "besttime_http_body")

    inventory_body = _pop_context_attr(context, "besttime_inventory_body")
    if inventory_body is not None:
        context.besttime.programmed_inventory_pages = [inventory_body]


def _start_job_via_http(context, venue_name, venue_address, venue_lat, venue_lng):
    """POST /admin/venues/add-job, recording the response, the job_id, and
    the wall-clock latency. Programs a default successful BestTime create
    for the submitted venue UNLESS a preceding Given already programmed
    something specific (a timeout, a rejection, a geo-fallback match, or a
    batch job already in flight) — see _besttime_special_programmed."""
    _consume_reused_besttime_givens(context)
    if not getattr(context, "_besttime_special_programmed", False):
        venue_id = f"ven_job_{uuid.uuid4().hex[:10]}"
        context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
            {
                "status": "OK",
                "venue_info": {
                    "venue_id": venue_id,
                    "venue_name": venue_name,
                    "venue_address": venue_address,
                    "venue_lat": venue_lat,
                    "venue_lon": venue_lng,
                },
                "analysis": [],
            }
        )
        # A small deterministic baseline delay so a job started and then
        # immediately inspected (e.g. via the recent-jobs list) is reliably
        # still "running" rather than racing real fakeredis-speed
        # completion. Only applied when nothing else already set a delay
        # (an explicit "the response is delayed" Given, or a batch job
        # already in flight) so it never doubles up with those.
        if not context.besttime.programmed_add_venue_delay_seconds:
            context.besttime.programmed_add_venue_delay_seconds = 0.05
    context._besttime_special_programmed = False

    body = {
        "venue_name": venue_name,
        "venue_address": venue_address,
        "venue_lat": venue_lat,
        "venue_lng": venue_lng,
    }
    t0 = time.perf_counter()
    resp = context.client.post("/admin/venues/add-job", json=body)
    context.start_elapsed = time.perf_counter() - t0
    context.start_response = resp
    if resp.status_code == 202:
        context.job_id = resp.json()["job_id"]
    return resp


def _poll_until_terminal(context, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    last_resp = None
    while time.monotonic() < deadline:
        resp = context.client.get(f"/admin/venues/add-job/{job_id}")
        assert resp.status_code == 200, resp.text
        last_resp = resp
        if resp.json().get("status") in ("done", "failed"):
            return resp
        time.sleep(0.02)
    raise AssertionError(
        f"job {job_id} did not reach a terminal state within {timeout}s: "
        f"last={last_resp.text if last_resp else None}"
    )


# ---------------------------------------------------------------------------
# Givens / Whens: starting a job
# ---------------------------------------------------------------------------


@given("the BestTime account inventory does not contain either submitted address")
def step_inventory_missing_both(context):
    context.expect_inventory_hit = False


@given("BestTime's response to the create call is delayed")
def step_create_is_delayed(context):
    context.besttime.programmed_add_venue_delay_seconds = _CREATE_DELAY_SECONDS


@given(
    'the BestTime account inventory already contains a venue matching the '
    'submitted coordinates but not the submitted address'
)
def step_geo_fallback_coord_match(context):
    context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
        {"status": "Error", "message": "Could not geocode address"}
    )
    matched = VenueFilterVenue(
        venue_id="ven_geo_fallback_coord_001",
        venue_name=_DEFAULT_NEW_VENUE["venue_name"],
        venue_address="A totally different address string, Recife - PE",
        venue_lat=_DEFAULT_NEW_VENUE["venue_lat"],
        venue_lng=_DEFAULT_NEW_VENUE["venue_lng"],
        venue_type="BAR",
        day_int=0,
        day_raw=[0] * 24,
    )
    context.besttime.programmed_venue_filter = VenueFilterResponse(
        status="OK", venues=[matched], venues_n=1
    )
    context._besttime_special_programmed = True


@given("a batch-add job is already running")
def step_batch_job_running(context):
    # Driven through the real HTTP endpoint (like batch_add_venues_steps.py
    # does), not by calling batch_add_service.start_job() directly — that
    # calls asyncio.create_task() and behave steps run on the plain test
    # thread, not inside the TestClient's event loop, so a direct call
    # raises "no running event loop".
    #
    # No artificial BestTime delay here (unlike step_create_is_delayed):
    # context.client = TestClient(app) is built WITHOUT the `with` form in
    # environment.py, so httpx/starlette gives every single request its own
    # throwaway anyio BlockingPortal/event loop (see
    # starlette.testclient.TestClient._portal_factory) — a task created via
    # asyncio.create_task() during one request that is still suspended on a
    # real asyncio.sleep() when that request returns is permanently orphaned
    # (its loop is gone) and never resumes, no matter how many times a later
    # request polls it. A same-request task with no genuine suspension point
    # runs to completion before its request's portal closes, which is
    # exactly what batch_add_venues_steps.py's own (already-passing) job
    # already relies on — this scenario matches that same shape.
    context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
        {
            "status": "OK",
            "venue_info": {
                "venue_id": "ven_batch_running_001",
                "venue_name": "Batch Running Venue",
                "venue_address": "Rua Batch, 1 - Centro",
                "venue_lat": -8.05,
                "venue_lon": -34.88,
            },
            "analysis": [],
        }
    )
    context._besttime_special_programmed = True
    resp = context.client.post(
        "/admin/venues/batch-add",
        json={
            "label": "bdd-async-job-batch",
            "resolve_coords": False,
            "venues": [
                {
                    "venue_name": "Batch Running Venue",
                    "venue_address": "Rua Batch, 1 - Centro",
                    "venue_lat": -8.05,
                    "venue_lng": -34.88,
                }
            ],
        },
    )
    assert resp.status_code == 202, resp.text
    accepted = resp.json()
    assert accepted.get("status") == "running", accepted
    context.batch_job_id_running = accepted["job_id"]


@given("the add job's runner crashes unexpectedly")
def step_runner_crashes(context):
    async def _boom(request):
        raise RuntimeError("simulated add-venue crash")

    context.add_venue_handler.add = _boom


@given("more add jobs have been started than the recent-jobs cap allows")
def step_start_more_than_cap(context):
    from app.services.add_venue_job_service import ADD_VENUE_RECENT_JOBS_CAP

    context.besttime.programmed_add_venue_delay_seconds = 0.0
    context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
        {
            "status": "OK",
            "venue_info": {
                "venue_id": "ven_bulk_shared_001",
                "venue_name": "Bulk Shared Venue",
                "venue_lat": -8.05,
                "venue_lon": -34.88,
            },
            "analysis": [],
        }
    )
    context._besttime_special_programmed = True
    total = ADD_VENUE_RECENT_JOBS_CAP + 5
    last_job_id = None
    for i in range(total):
        # Keep the pre-programmed success response alive across every
        # iteration (the "is it already programmed" guard only protects the
        # FIRST call in _start_job_via_http).
        context._besttime_special_programmed = True
        resp = context.client.post(
            "/admin/venues/add-job",
            json={
                "venue_name": f"Bulk Venue {i:03d}",
                "venue_address": f"Rua Bulk {i:03d}, Recife - PE",
                "venue_lat": -8.05,
                "venue_lng": -34.88,
            },
        )
        assert resp.status_code == 202, resp.text
        last_job_id = resp.json()["job_id"]
    context.job_id = last_job_id


@given(
    'the operator has started an add job for venue_name "{venue_name}", '
    'venue_address "{venue_address}", venue_lat {venue_lat:f}, and '
    'venue_lng {venue_lng:f}'
)
@when(
    'the operator starts an add job for venue_name "{venue_name}", '
    'venue_address "{venue_address}", venue_lat {venue_lat:f}, and '
    'venue_lng {venue_lng:f}'
)
def step_start_named(context, venue_name, venue_address, venue_lat, venue_lng):
    _start_job_via_http(context, venue_name, venue_address, venue_lat, venue_lng)


@when(
    'the operator starts a second add job for venue_name "{venue_name}", '
    'venue_address "{venue_address}", venue_lat {venue_lat:f}, and '
    'venue_lng {venue_lng:f}'
)
def step_start_second_named(context, venue_name, venue_address, venue_lat, venue_lng):
    context.first_job_id = context.job_id
    context.first_start_response = context.start_response
    _start_job_via_http(context, venue_name, venue_address, venue_lat, venue_lng)
    context.second_job_id = context.job_id
    context.second_start_response = context.start_response


@given("the operator has started an add job for a new venue")
@when("the operator starts an add job for a new venue")
def step_start_default(context):
    _start_job_via_http(context, **_DEFAULT_NEW_VENUE)


# ---------------------------------------------------------------------------
# Whens / Givens: polling / listing
# ---------------------------------------------------------------------------


@when("the operator polls the job before BestTime responds")
def step_poll_before_finished(context):
    context.poll_response = context.client.get(f"/admin/venues/add-job/{context.job_id}")


@when("the job finishes and the operator polls it")
def step_job_finishes_and_polls(context):
    context.poll_response = _poll_until_terminal(context, context.job_id)


@when("the operator polls a job id that was never started")
def step_poll_unknown(context):
    context.poll_response = context.client.get(
        "/admin/venues/add-job/does-not-exist-job-id"
    )


@given("the job has finished")
def step_job_has_finished(context):
    _poll_until_terminal(context, context.job_id)


@when("the operator lists recent add jobs")
def step_list_recent(context):
    # A generous limit so the cap scenario genuinely exercises the
    # server-side 50-item cap ("regardless of limit") rather than just the
    # router's own default query-param limit.
    context.recent_response = context.client.get(
        "/admin/venues/add-jobs/recent", params={"limit": 100}
    )


# ---------------------------------------------------------------------------
# Thens: start response
# ---------------------------------------------------------------------------


@then("the start response status must be {status:d}")
def step_start_status(context, status):
    assert context.start_response.status_code == status, (
        f"expected {status}, got {context.start_response.status_code} "
        f"body={context.start_response.text[:500]}"
    )


@then('the start response body must include a "job_id"')
def step_start_has_job_id(context):
    body = context.start_response.json()
    assert body.get("job_id"), body


@then('the start response body must indicate status "{status}"')
def step_start_status_field(context, status):
    body = context.start_response.json()
    assert body.get("status") == status, body


@then("the start response must not wait for the BestTime create to resolve")
def step_start_is_immediate(context):
    assert context.start_elapsed < _IMMEDIATE_RESPONSE_THRESHOLD_SECONDS, (
        f"start response took {context.start_elapsed:.3f}s (programmed create "
        f"delay was {_CREATE_DELAY_SECONDS}s) — looks like it waited for the "
        "BestTime create instead of returning immediately"
    )


@then("the add job's start response status must be 202")
def step_add_job_start_202(context):
    assert context.start_response.status_code == 202, context.start_response.text


# ---------------------------------------------------------------------------
# Thens: poll response
# ---------------------------------------------------------------------------


@then("the poll response status must be {status:d}")
def step_poll_status(context, status):
    assert context.poll_response.status_code == status, (
        f"expected {status}, got {context.poll_response.status_code} "
        f"body={context.poll_response.text[:500]}"
    )


@then('the poll response body must indicate status "{status}"')
def step_poll_body_status(context, status):
    body = context.poll_response.json()
    assert body.get("status") == status, body


@then('the poll response body must not yet include a "result"')
def step_poll_no_result_yet(context):
    body = context.poll_response.json()
    assert "result" not in body, body


@then('the poll response "{field}" must be {value:d}')
def step_poll_field_eq_int(context, field, value):
    body = context.poll_response.json()
    assert body.get(field) == value, (
        f"{field}: expected {value}, got {body.get(field)!r} body={body}"
    )


@then('the poll response "result" must include the returned "venue_id"')
def step_poll_result_venue_id(context):
    result = context.poll_response.json().get("result") or {}
    assert result.get("venue_id"), f"venue_id missing in result: {result}"


@then('the poll response "result" must include "{field}" equal to "{expected}"')
def step_poll_result_field_eq(context, field, expected):
    result = context.poll_response.json().get("result") or {}
    assert result.get(field) == expected, (
        f"{field}: expected {expected!r}, got {result.get(field)!r} result={result}"
    )


@then('the poll response "result" must indicate "recovered_from_timeout" is true')
def step_poll_result_recovered(context):
    result = context.poll_response.json().get("result") or {}
    assert result.get("recovered_from_timeout") is True, result


@then(
    'the poll response "result" must indicate "matched_via_geo_fallback" '
    'with "newly_linked" true'
)
def step_poll_result_geo_fallback(context):
    result = context.poll_response.json().get("result") or {}
    assert result.get("status") == "matched_via_geo_fallback", result
    assert result.get("newly_linked") is True, result


@then('the poll response "result" must include the BestTime rejection message')
def step_poll_result_besttime_message(context):
    result = context.poll_response.json().get("result") or {}
    assert result.get("besttime_message"), f"besttime_message missing: {result}"
    expected = getattr(context, "expected_besttime_reject_message", None)
    if expected is not None:
        assert result.get("besttime_message") == expected, result


# ---------------------------------------------------------------------------
# Thens: two-jobs-concurrently
# ---------------------------------------------------------------------------


@then('both jobs must finish with status "{status}"')
def step_both_jobs_finish(context, status):
    r1 = _poll_until_terminal(context, context.first_job_id)
    r2 = _poll_until_terminal(context, context.second_job_id)
    assert r1.json().get("status") == status, r1.text
    assert r2.json().get("status") == status, r2.text


@then("neither job's start response must indicate it was refused as already running")
def step_neither_refused(context):
    for resp in (context.first_start_response, context.second_start_response):
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body.get("status") != "already_running", body


@then('the add job must finish with status "{status}"')
def step_add_job_finishes(context, status):
    resp = _poll_until_terminal(context, context.job_id)
    assert resp.json().get("status") == status, resp.text
    # Best-effort: let the batch job settle too so job_lock.BATCH_ADD_LOCK
    # (a module-global — see app/services/job_lock.py) is released before
    # this scenario ends instead of leaking into a later scenario.
    batch_job_id = getattr(context, "batch_job_id_running", None)
    if batch_job_id is not None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            job = context.batch_add_service.get_job(batch_job_id)
            if job and job.get("status") != "running":
                break
            time.sleep(0.02)


# ---------------------------------------------------------------------------
# Thens: recent-jobs list
# ---------------------------------------------------------------------------


@then("the recent-jobs response status must be {status:d}")
def step_recent_status(context, status):
    assert context.recent_response.status_code == status, context.recent_response.text


def _find_recent(context, job_id):
    jobs = context.recent_response.json()["jobs"]
    match = next((j for j in jobs if j["job_id"] == job_id), None)
    assert match is not None, f"job {job_id} not found in recent list: {jobs}"
    return match


# NOTE ON ORDERING: these three "...with status "{status}"[+ trailing
# clause]" step definitions must stay registered in longest-to-shortest
# order. behave's ambiguity check tests whether an EXISTING pattern matches
# a NEW pattern's own literal source text; the plain (shortest) pattern's
# trailing field can "absorb" any extra suffix text, so if it were
# registered FIRST it would falsely collide with the other two. Registering
# the more specific ones first avoids that (the plain pattern has no
# trailing text at all, so the longer patterns' extra required literal
# clauses never match its short source text).
@then(
    'the recent-jobs list must include that job with status "{status}" '
    'and outcome "{outcome}"'
)
def step_recent_status_and_outcome(context, status, outcome):
    match = _find_recent(context, context.job_id)
    assert match["status"] == status, match
    assert match.get("outcome") == outcome, match


@then(
    'the recent-jobs list must include that job with status "{status}" '
    "and its error text"
)
def step_recent_status_and_error(context, status):
    match = _find_recent(context, context.job_id)
    assert match["status"] == status, match
    assert match.get("error"), match


@then('the recent-jobs list must include that job with status "{status}"')
def step_recent_includes_status(context, status):
    match = _find_recent(context, context.job_id)
    assert match["status"] == status, match


@then(
    "the recent-jobs list must include that job showing the BestTime "
    "rejection message as its failure reason"
)
def step_recent_shows_reject_reason(context):
    match = _find_recent(context, context.job_id)
    assert match["status"] == "done", match
    result = match.get("result") or {}
    assert result.get("besttime_message"), match
    expected = getattr(context, "expected_besttime_reject_message", None)
    if expected is not None:
        assert result.get("besttime_message") == expected, match


@then("the recent-jobs list must contain at most the capped number of entries")
def step_recent_capped(context):
    from app.services.add_venue_job_service import ADD_VENUE_RECENT_JOBS_CAP

    jobs = context.recent_response.json()["jobs"]
    assert len(jobs) <= ADD_VENUE_RECENT_JOBS_CAP, len(jobs)


@then(
    "the recent-jobs list must show the most recently started jobs, not the oldest"
)
def step_recent_shows_newest(context):
    jobs = context.recent_response.json()["jobs"]
    assert jobs, "recent-jobs list is empty"
    assert jobs[0]["job_id"] == context.job_id, (
        f"expected the last-started job {context.job_id} first, "
        f"got {jobs[0]['job_id']}"
    )
