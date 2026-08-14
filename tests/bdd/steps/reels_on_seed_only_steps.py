"""Behave steps for tests/bdd/enrichment/reels-on-seed-only.feature.

Shares the SAME context/fakes `scheduled_incremental_instagram_crawl_steps.py`
already built (imported, never duplicated) — the Background step ("Given the
Instagram crawl scheduler is configured") and "When its scheduled crawl runs"
are defined THERE and picked up by behave's shared step registry; this module
only adds the step texts genuinely new to this feature: the seed-vs-seeded
Given states, and the reels-stream/posts-stream/skip-reason Then assertions.

See plans/260811_reels-on-seed-only.md. The gate under test
(`reels_skip_reason`, app/services/instagram_crawl_service.py) reads only the
reels cursor — never a separate "has run" flag — so "whose first run
recorded no reels cursor" below is deliberately given THE SAME cursor state
as "has never run": that is the whole point of gating on the cursor rather
than a flag (a failed seed's bookkeeping write leaves the cursor null, so the
retry looks, correctly, exactly like a brand-new target). `consecutive_
failures`/`last_run_at` are set here only to make the scenario's narrative
honest (a prior attempt genuinely happened), never because the gate reads
them — it does not.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then  # type: ignore[import-untyped]

from tests.bdd.steps.scheduled_incremental_instagram_crawl_steps import (
    _create_target,
    _ensure_context,
)

_OLD_REELS_CURSOR = datetime(2026, 7, 1, tzinfo=timezone.utc)
_PRIOR_ATTEMPT_AT = datetime(2026, 8, 1, tzinfo=timezone.utc)


@given("a crawl target with reels enabled that has never run")
def step_given_reels_enabled_never_run(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "reelsseedtarget", crawl_reels=True)


@given("a crawl target with reels enabled that has already seeded reels")
def step_given_reels_enabled_already_seeded(context):
    _ensure_context(context)
    context.ic_handle = _create_target(
        context, "reelsalreadyseededtarget", crawl_reels=True,
        cursor_reels_at=_OLD_REELS_CURSOR,
    )
    # plans/260814_seeded-state-and-config-validation.md §A: "seeded" is now
    # gated on `reels_seeded_at`, a fact recorded separately from
    # `cursor_reels_at` (which keeps its own, unchanged meaning — the
    # newest reel actually reached). A real completed non-empty seed leaves
    # both behind together; arranged the same way here.
    context.ic_dao.update_crawl_target(
        context.ic_handle, {"reels_seeded_at": _OLD_REELS_CURSOR},
    )


@given("a crawl target with reels disabled that has never run")
def step_given_reels_disabled_never_run(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "reelsdisabledseedtarget", crawl_reels=False)


@given("a crawl target with reels enabled whose first run recorded no reels cursor")
def step_given_reels_enabled_bookkeeping_never_recorded_cursor(context):
    _ensure_context(context)
    context.ic_handle = _create_target(
        context, "reelsneverrecordedtarget", crawl_reels=True, consecutive_failures=1,
    )
    # A prior attempt genuinely happened (its bookkeeping write failed
    # partway, per `run_target`'s OUTCOME_BOOKKEEPING_FAILED handling) —
    # evidenced by `last_run_at`/`consecutive_failures` above — but
    # `cursor_reels_at` itself was never written, which is the ONLY thing
    # the gate reads.
    context.ic_dao.update_crawl_target(context.ic_handle, {"last_run_at": _PRIOR_ATTEMPT_AT})


@then("the reels stream is scraped")
def step_then_reels_stream_scraped(context):
    types = [c["results_type"] for c in context.ic_apify.calls if c["handle"] == context.ic_handle]
    assert "reels" in types, types


@then("the reels stream is not scraped")
def step_then_reels_stream_not_scraped(context):
    types = [c["results_type"] for c in context.ic_apify.calls if c["handle"] == context.ic_handle]
    assert "reels" not in types, types


@then("the posts stream is scraped")
def step_then_posts_stream_scraped(context):
    types = [c["results_type"] for c in context.ic_apify.calls if c["handle"] == context.ic_handle]
    assert "posts" in types, types


@then("the run records that reels were already seeded")
def step_then_run_records_reels_already_seeded(context):
    reels_report = context.ic_last_report["streams"].get("reels")
    assert reels_report is not None, context.ic_last_report
    # Literal string, matching this suite's own convention for outcome
    # assertions (e.g. scheduled_incremental_instagram_crawl_steps.py's
    # "skipped_budget") rather than importing the OUTCOME_* constant.
    assert reels_report["outcome"] == "skipped_seeded", reels_report
