"""Behave steps for tests/bdd/enrichment/apify-poll-timeout-recovery.feature.

Drives the REAL ApifyGMapsExtractorClient — its real `_poll_run` loop and real
`fetch_venue_photos` — behind a fake httpx transport, wired into the REAL
VenuePhotoArchiveService over the same S3 fake the rest of the archive suite
uses. Only the network and S3 are faked.

That choice is deliberate. The sibling suites fake `fetch_venue_photos` itself,
which is exactly why the 35 lost venues were invisible to the tests: the poll
loop that dropped them was never executed. The properties under test here —
"a timeout is not a no-match", "recovery must not start a second actor run" —
live *inside* the client, so a fake at the client boundary cannot see them.

Runs are advanced by POLL COUNT, not by wall time. The fake decides a status
from how many times it has been polled, so the scenarios are deterministic and
need no clock. The module's poll constants are patched down for the duration of
the run so a "5 minute" budget costs milliseconds.
"""
from __future__ import annotations

import logging
from unittest.mock import patch

from behave import given, then, when  # type: ignore[import-untyped]

import app.api.apify_gmaps_extractor_client as apify_mod
from app.api.apify_gmaps_extractor_client import ApifyGMapsExtractorClient
from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.services.archive_sources import SOURCE_APIFY_GMAPS
from tests.bdd.steps.photo_archive_pipeline_v2_steps import _metric, _run_v2, _seed
from tests.bdd.steps.venue_photo_archive_steps import _build

# The base budget and the continuation window, in polls. Small so the real loop
# runs fast; the ratio is what the scenarios care about, not the absolute size.
BASE_ATTEMPTS = 4
CONTINUATION_ATTEMPTS = 4
POLL_INTERVAL = 0.001

TIMEOUT_METRIC = "media_archive_venues_total"
APIFY_ERRORS = "apify_api_errors_total"
POLL_TIMEOUTS = "apify_poll_timeouts_total"


class _RunPlan:
    """How one venue's actor run behaves, expressed in polls."""

    def __init__(
        self,
        stuck_status: str = "RUNNING",
        succeed_at_poll: int | None = 1,
        terminal_at_poll: int | None = None,
        terminal_status: str = "FAILED",
        items: list[dict] | None = None,
        credit_exhausted_at_poll: int | None = None,
    ):
        self.stuck_status = stuck_status
        self.succeed_at_poll = succeed_at_poll
        self.terminal_at_poll = terminal_at_poll
        self.terminal_status = terminal_status
        self.items = items
        self.credit_exhausted_at_poll = credit_exhausted_at_poll

    def status_at(self, poll: int) -> str:
        if self.terminal_at_poll is not None and poll >= self.terminal_at_poll:
            return self.terminal_status
        if self.succeed_at_poll is not None and poll >= self.succeed_at_poll:
            return "SUCCEEDED"
        return self.stuck_status


def _place_items():
    return [{
        "title": "Bar do Cuscuz",
        "address": "Recife Antigo",
        "phone": "+55",
        "website": "https://x",
        "categories": ["Bar"],
        "totalScore": 4.5,
        "placeId": "ChIJcuscuz",
        "imageUrls": [f"https://lh3.googleusercontent.com/c{i}" for i in range(3)],
    }]


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeApifyHttp:
    """Fake httpx surface for the real client: start a run, poll it, read it.

    Counts start-runs per run id so a scenario can assert the negative property
    that recovery reuses the run already paid for.
    """

    def __init__(self):
        self.default_plan = _RunPlan()
        self.plans: dict[str, _RunPlan] = {}
        self.starts: list[str] = []          # one entry per actor run started
        self.polls: dict[str, int] = {}      # run id -> times polled
        self.dataset_reads: list[str] = []
        self._run_query: dict[str, str] = {}

    # ── plan lookup ──────────────────────────────────────────────────────────
    def plan_for_query(self, query: str) -> _RunPlan:
        for needle, plan in self.plans.items():
            if needle in (query or ""):
                return plan
        return self.default_plan

    def _plan_for_run(self, run_id: str) -> _RunPlan:
        return self.plan_for_query(self._run_query.get(run_id, ""))

    # ── httpx surface ────────────────────────────────────────────────────────
    async def post(self, url, params=None, json=None, **kw):
        query = (json or {}).get("searchStringsArray", [""])[0]
        run_id = f"run_{len(self.starts) + 1}"
        self.starts.append(query)
        self._run_query[run_id] = query
        self.polls[run_id] = 0
        return _FakeResponse({"data": {"id": run_id, "defaultDatasetId": f"ds_{run_id}"}})

    async def get(self, url, params=None, **kw):
        if "/actor-runs/" in url:
            run_id = url.rstrip("/").split("/")[-1]
            self.polls[run_id] = self.polls.get(run_id, 0) + 1
            plan = self._plan_for_run(run_id)
            if (
                plan.credit_exhausted_at_poll is not None
                and self.polls[run_id] >= plan.credit_exhausted_at_poll
            ):
                return _FakeResponse({"error": "credits"}, status_code=402)
            return _FakeResponse({"data": {"status": plan.status_at(self.polls[run_id])}})

        run_id = url.rstrip("/").split("/")[-2].replace("ds_", "")
        self.dataset_reads.append(run_id)
        plan = self._plan_for_run(run_id)
        items = _place_items() if plan.items is None else plan.items
        return _FakeResponse(items)


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


# ── helpers ───────────────────────────────────────────────────────────────────
def _attach_real_client(context):
    _build(context)  # the same real service the rest of the archive suite drives
    context.http = _FakeApifyHttp()
    client = ApifyGMapsExtractorClient(api_token="test-token")
    client.client = context.http  # type: ignore[assignment]
    # Set unconditionally so the scenario states its own continuation window
    # rather than inheriting whatever the deployed default happens to be.
    client.poll_continuation_seconds = CONTINUATION_ATTEMPTS * POLL_INTERVAL
    context.service.apify_gmaps_extractor_client = client
    context.client = client
    return client


def _capture_logs(context):
    context.log_handler = _CaptureHandler()
    context.log_logger = logging.getLogger("app.api.apify_gmaps_extractor_client")
    context.log_logger.addHandler(context.log_handler)
    context.log_logger.setLevel(logging.DEBUG)


def _snapshot(context):
    context.before = {
        result: _metric(TIMEOUT_METRIC, source=SOURCE_APIFY_GMAPS, result=result)
        for result in ("archived", "no_query", "no_result", "timeout",
                       "google_error", "no_match")
    }
    context.duration_before = _metric(
        "apify_api_call_duration_seconds_count", endpoint="gmaps_archive_photos"
    )


def _delta(context, result):
    now = _metric(TIMEOUT_METRIC, source=SOURCE_APIFY_GMAPS, result=result)
    return now - context.before.get(result, 0.0)


def _archive(context, venue_ids):
    _snapshot(context)
    with patch.object(apify_mod, "MAX_POLL_ATTEMPTS", BASE_ATTEMPTS), \
            patch.object(apify_mod, "POLL_INTERVAL_SECONDS", POLL_INTERVAL):
        try:
            _run_v2(context, venue_ids=venue_ids, source=SOURCE_APIFY_GMAPS)
            context.raised = None
        except Exception as exc:  # a scenario asserts the run aborts
            context.raised = exc
            context.summary = getattr(context, "summary", {})


def _plan(context, **kw):
    context.http.default_plan = _RunPlan(**kw)


# ── Given ─────────────────────────────────────────────────────────────────────
@given("the archive source is the real Apify Google Maps extractor")
def step_real_apify(context):
    _attach_real_client(context)
    _capture_logs(context)


@given('the venue "{venue_id}" is in the catalog')
def step_venue_in_catalog(context, venue_id):
    _seed(context, venue_id)
    context.venue_id = venue_id


@given('the actor run for "{venue_id}" reaches SUCCEEDED inside the poll budget')
def step_succeeds_in_budget(context, venue_id):
    # Keyed per venue so the two-venue scenario can give each run its own fate.
    context.http.plans[f"Venue {venue_id}"] = _RunPlan(succeed_at_poll=2)


@given('the actor run for "{venue_id}" is still RUNNING when the poll budget ends')
def step_running_past_budget(context, venue_id):
    # Past the base budget by construction; a later Given may bring it home.
    _plan(context, stuck_status="RUNNING", succeed_at_poll=None)


@given("the actor run reaches SUCCEEDED during the continuation window")
def step_succeeds_in_continuation(context):
    context.http.default_plan.succeed_at_poll = BASE_ATTEMPTS + 2


@given("the actor run never reaches a terminal status")
def step_never_terminal(context):
    context.http.default_plan.succeed_at_poll = None


@given('the actor run for "{venue_id}" never reaches a terminal status')
def step_named_never_terminal(context, venue_id):
    context.http.plans[f"Venue {venue_id}"] = _RunPlan(
        stuck_status="RUNNING", succeed_at_poll=None
    )


@given('the actor run for "{venue_id}" stays {status} for the whole continuation window')
def step_stays_status(context, venue_id, status):
    _plan(context, stuck_status=status, succeed_at_poll=None)


@given('the actor run for "{venue_id}" reaches SUCCEEDED with an empty dataset')
def step_succeeds_empty(context, venue_id):
    _plan(context, succeed_at_poll=2, items=[])


@given('the actor run for "{venue_id}" reaches FAILED inside the poll budget')
def step_fails_in_budget(context, venue_id):
    _plan(context, succeed_at_poll=None, terminal_at_poll=2, terminal_status="FAILED")


@given('the venue "{venue_id}" has the search query "{query}"')
def step_second_venue(context, venue_id, query):
    _seed(context, venue_id)
    context.other_venue = venue_id


@given("Apify reports credit exhaustion during the continuation window")
def step_credit_exhausted_in_continuation(context):
    context.http.default_plan.credit_exhausted_at_poll = BASE_ATTEMPTS + 2


# ── When ──────────────────────────────────────────────────────────────────────
@when("I archive the venue")
def step_archive_venue(context):
    _archive(context, context.venue_id)


@when("I archive both venues")
def step_archive_both(context):
    _archive(context, f"{context.venue_id},{context.other_venue}")


# ── Then ──────────────────────────────────────────────────────────────────────
@then("the venue must be archived")
def step_archived(context):
    keys = [k for k in context.fake_s3.objects
            if f"venue_id={context.venue_id}/" in k and k.endswith(".jpg")]
    assert keys, f"nothing archived for {context.venue_id}: {list(context.fake_s3.objects)[:4]}"


@then("the venue must not be archived")
def step_not_archived(context):
    keys = [k for k in context.fake_s3.objects
            if f"venue_id={context.venue_id}/" in k and k.endswith(".jpg")]
    assert not keys, f"a timed-out venue was archived: {keys[:3]}"


@then('the venue outcome must be reported as "{result}"')
def step_outcome_is(context, result):
    assert _delta(context, result) >= 1, (
        f"expected {TIMEOUT_METRIC}{{result={result}}} to rise; "
        f"summary={context.summary}"
    )


@then('the venue outcome must not be reported as "{result}"')
def step_outcome_is_not(context, result):
    assert _delta(context, result) == 0, (
        f"{TIMEOUT_METRIC}{{result={result}}} rose but must not; "
        f"summary={context.summary}"
    )


@then("the run summary must count {n:d} timeout")
def step_summary_timeouts(context, n):
    assert context.summary.get("timeout") == n, context.summary


@then("no continuation poll must be attempted")
def step_no_continuation(context):
    for run_id, count in context.http.polls.items():
        assert count <= BASE_ATTEMPTS, (
            f"{run_id} was polled {count} times, past the {BASE_ATTEMPTS}-poll budget"
        )


@then("the call duration must be observed once")
def step_duration_observed(context):
    now = _metric("apify_api_call_duration_seconds_count", endpoint="gmaps_archive_photos")
    assert now - context.duration_before == 1, (
        f"duration observations moved by {now - context.duration_before}, expected 1"
    )


@then("the archived photos must be stored under the same run prefix as a first-pass success")
def step_same_prefix(context):
    prefix = context.summary["prefix"]
    keys = [k for k in context.fake_s3.objects
            if f"venue_id={context.venue_id}/" in k and k.endswith(".jpg")]
    assert keys, "the recovered venue stored no photos"
    for key in keys:
        assert key.startswith(prefix), f"{key} is outside the run prefix {prefix}"


@then("the log must record that the venue was recovered and how long it took")
def step_recovery_logged(context):
    blob = " ".join(context.log_handler.lines).lower()
    assert "recover" in blob, f"no recovery log line: {context.log_handler.lines[-4:]}"
    assert any(ch.isdigit() for ch in blob), "the recovery log states no duration"


@then("the archive run must finish rather than block indefinitely")
def step_run_finished(context):
    assert context.raised is None, f"the run raised instead of finishing: {context.raised!r}"
    assert context.summary.get("prefix"), context.summary


@then("exactly {n:d} actor run must have been started for the venue")
def step_start_count(context, n):
    assert len(context.http.starts) == n, (
        f"{len(context.http.starts)} actor runs started, expected {n}: "
        f"{context.http.starts}"
    )


@then("the continuation must poll the run that was already started")
def step_continuation_same_run(context):
    assert len(context.http.polls) == 1, (
        f"polls spread over {len(context.http.polls)} runs: {context.http.polls}"
    )
    run_id, count = next(iter(context.http.polls.items()))
    assert count > BASE_ATTEMPTS, (
        f"{run_id} was polled only {count} times — the continuation never ran"
    )


@then('the timeout log line must name the last non-terminal status "{status}"')
def step_log_names_status(context, status):
    blob = " ".join(context.log_handler.lines)
    assert status in blob, (
        f"{status!r} absent from the timeout logs: {context.log_handler.lines[-4:]}"
    )


@then('the timeout metric must carry the last non-terminal status "{status}"')
def step_metric_carries_status(context, status):
    value = _metric(
        POLL_TIMEOUTS, endpoint="gmaps_archive_photos", last_status=status
    )
    assert value >= 1, (
        f"{POLL_TIMEOUTS}{{last_status={status}}} was not recorded"
    )
    # The pre-existing series must keep moving too: prod dashboards and the query
    # that found these 35 failures both read it, and a rename would blind them.
    assert _metric(
        APIFY_ERRORS, endpoint="gmaps_archive_photos", error_type="timeout"
    ) >= 1, f"{APIFY_ERRORS}{{error_type=timeout}} stopped being recorded"


@then('the venue "{venue_id}" must be archived')
def step_named_archived(context, venue_id):
    keys = [k for k in context.fake_s3.objects
            if f"venue_id={venue_id}/" in k and k.endswith(".jpg")]
    assert keys, f"{venue_id} was not archived"


@then("the archive run must complete")
def step_run_complete(context):
    assert context.raised is None, f"the run raised: {context.raised!r}"


@then("the archive run must abort")
def step_run_aborts(context):
    # Credit exhaustion must stop the run rather than be absorbed as one
    # venue's failure — the balance is gone for every remaining venue too.
    aborted = (
        isinstance(context.raised, ApifyCreditExhaustedError)
        or context.summary.get("credit_exhausted") is True
        or context.summary.get("aborted") is True
    )
    assert aborted, (
        f"the run neither aborted nor reported credit exhaustion: "
        f"raised={context.raised!r} summary={context.summary}"
    )


@then("no completion marker must be written")
def step_no_marker(context):
    markers = [k for k in context.fake_s3.objects if k.endswith("_latest.json")]
    assert not markers, f"a completion marker was written despite the abort: {markers}"
