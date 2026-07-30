"""Behave steps for tests/bdd/observability/archive-run-outcome-honesty.feature.

Drives the REAL VenuePhotoArchiveService over the existing archive fakes, and
asserts only what an operator can actually see afterwards: the run status, the
per-venue outcome labels, and the last-success timestamp.

The `no_match` scenarios are negative on purpose. That label is being retired
because it absorbed three unrelated situations — no query, no result, and (until
#123) a poll timeout — so the property worth protecting is that nothing emits it
again, not merely that the new labels exist.
"""
from __future__ import annotations

from behave import given, then, when  # type: ignore[import-untyped]

from app.services.archive_sources import SOURCE_APIFY_GMAPS
from tests.bdd.steps.archive_source_registry_steps import _apify_photos, _attach_apify
from tests.bdd.steps.photo_archive_pipeline_v2_steps import _metric, _run_v2, _seed
from tests.bdd.steps.venue_photo_archive_steps import _build

VENUES = "media_archive_venues_total"
RUNS = "media_archive_runs_total"
LAST_OK = "media_archive_last_success_timestamp"

# Every terminal per-venue bucket. The exhaustiveness scenario sums these, so a
# new outcome added without updating this list makes that scenario fail — which
# is the point: an unaccounted venue is exactly the bug this feature exists for.
BUCKETS = (
    "archived", "skipped_existing", "no_place_id", "info_only",
    "timeout", "no_query", "no_result", "failed",
)


def _harness(context):
    if not hasattr(context, "service"):
        _build(context)
        _attach_apify(context, configured=True)
    return context.service


def _snapshot(context):
    context.before = {
        b: _metric(VENUES, source=SOURCE_APIFY_GMAPS, result=b) for b in BUCKETS
    }
    context.before_r = {
        s: _metric(RUNS, source=SOURCE_APIFY_GMAPS, status=s)
        for s in ("success", "partial", "error")
    }
    context.before_ok = _metric(LAST_OK)


# ── Given ─────────────────────────────────────────────────────────────────────
@given("the archive source is the Apify Google Maps extractor")
def step_source_apify(context):
    _harness(context)
    context.venue_ids = []


def _add(context, vid):
    context.venue_ids.append(vid)
    context.venue_id = vid


@given("the run includes a venue with {n:d} photos")
def step_findable(context, n):
    vid = f"ven_ok{len(context.venue_ids)}"
    _seed(context, vid)
    context.apify.photos_by_query[f"Venue {vid}"] = _apify_photos(n)
    _add(context, vid)


@given("the run includes a venue the source cannot find")
def step_unfindable(context):
    vid = f"ven_nf{len(context.venue_ids)}"
    _seed(context, vid)          # seeded, but the fake returns None for it
    _add(context, vid)


@given("the run includes a venue the source cannot address")
def step_no_query(context):
    from app.models import Venue
    vid = f"ven_nq{len(context.venue_ids)}"
    _seed(context, vid)
    # Blank name AND address, so `_venue_context` builds an empty search_query
    # and the source can never be called for it — the free case that must not
    # hide inside the billed one.
    context.repository.upsert_venue(
        Venue(
            forecast=True, processed=True, venue_id=vid,
            venue_name="", venue_address="",
            venue_lat=-8.05, venue_lng=-34.88, priority=1,
        )
    )
    _add(context, vid)


@given("the run includes a venue whose fetch times out")
def step_times_out(context):
    vid = f"ven_to{len(context.venue_ids)}"
    _seed(context, vid)
    context.apify.timeout_queries.add(f"Venue {vid}")
    _add(context, vid)


@given("the run includes a venue whose fetch fails")
def step_fetch_fails(context):
    vid = f"ven_er{len(context.venue_ids)}"
    _seed(context, vid)
    context.apify.error_queries.add(f"Venue {vid}")
    _add(context, vid)


@given("every selected venue was already archived by the previous run")
def step_all_skipped(context):
    vid = "ven_skip"
    _seed(context, vid)
    context.apify.photos_by_query[f"Venue {vid}"] = _apify_photos(2)
    _add(context, vid)
    _run_v2(context, venue_ids=vid, source=SOURCE_APIFY_GMAPS)   # first pass archives it
    context.preseeded = True


@given("the Apify balance runs out mid-run")
def step_no_credits(context):
    context.apify.no_credit = True


# ── When ──────────────────────────────────────────────────────────────────────
@when("the archive job runs and reports its outcome")
def step_run(context):
    _snapshot(context)
    try:
        _run_v2(context, venue_ids=",".join(context.venue_ids), source=SOURCE_APIFY_GMAPS)
        context.raised = None
    except Exception as exc:      # a stopped run must still report, not explode
        context.raised = exc


# ── Then ──────────────────────────────────────────────────────────────────────
@then('the run status must be "{status}"')
def step_run_status(context, status):
    assert context.summary.get("status") == status, (
        f"summary status={context.summary.get('status')!r}, expected {status!r}"
    )
    delta = _metric(RUNS, source=SOURCE_APIFY_GMAPS, status=status) - context.before_r.get(status, 0.0)
    assert delta >= 1, f"{RUNS}{{status={status}}} did not rise"


@then('the run status must not be "{status}"')
def step_run_status_not(context, status):
    assert context.summary.get("status") != status, context.summary.get("status")
    delta = _metric(RUNS, source=SOURCE_APIFY_GMAPS, status=status) - context.before_r.get(status, 0.0)
    assert delta == 0, f"{RUNS}{{status={status}}} rose but must not"


@then("the last-success timestamp must advance")
def step_last_ok_moves(context):
    assert _metric(LAST_OK) > context.before_ok, "the last-success timestamp did not advance"


@then("the last-success timestamp must not advance")
def step_last_ok_frozen(context):
    # The freshness signal an alert would watch. If a run that lost most of its
    # venues moves it, the alert can never fire.
    assert _metric(LAST_OK) == context.before_ok, (
        "a run that did not fully succeed advanced the last-success timestamp"
    )


# `the venue outcome must (not) be reported as "X"` is defined once, in
# apify_poll_timeout_recovery_steps.py, and reused here — same metric, same
# semantics, and one definition keeps them from drifting apart.


@then("the run summary must count {n:d} {bucket}")
def step_summary_bucket(context, n, bucket):
    assert context.summary.get(bucket) == n, (
        f"summary[{bucket}]={context.summary.get(bucket)!r}, expected {n}"
    )


@then("no venue must be reported as a failure")
def step_no_failures(context):
    for bad in ("failed", "google_error", "timeout"):
        assert not context.summary.get(bad), f"{bad}={context.summary.get(bad)}"


@then("the outcome buckets must sum to the number of venues considered")
def step_buckets_exhaustive(context):
    total = sum(int(context.summary.get(b) or 0) for b in BUCKETS)
    considered = int(context.summary.get("considered") or 0)
    assert total == considered, (
        f"buckets sum to {total} but {considered} venues were considered — "
        f"a venue vanished without an outcome: "
        f"{dict((b, context.summary.get(b)) for b in BUCKETS)}"
    )
