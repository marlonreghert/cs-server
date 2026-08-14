"""Behave steps for
tests/bdd/enrichment/seeded-state-and-config-validation.feature.

See plans/260814_seeded-state-and-config-validation.md.

## §A (reels seeded state)

Reuses the SAME context/fakes `scheduled_incremental_instagram_crawl_steps.py`
builds (`ic_dao`/`ic_apify`/`ic_service`/`_run`/`_create_target`/
`_ensure_context`) — mirrors `crawl_error_visibility_steps.py`'s and
`crawl_transport_failure_visibility_steps.py`'s own precedent for reusing
this module's context rather than building a second one. Every §A scenario
drives exactly one crawl target, `_HANDLE`.

## §B (config validated on write)

Uses the GLOBAL `context.admin_config_service`/`context.fake_redis`
`tests/bdd/environment.py` already builds fresh for every scenario in the
suite (the same harness `admin_config_rds_steps.py`'s own scenarios
exercise) — never a second admin-config service, so these scenarios
exercise the SAME validate-before-write path a real admin write goes
through, not a re-implementation of it.

## The admin-router import trap

`from app.routers import admin_crawl_router` binds that name to the ROUTER
INSTANCE (`app.routers.__init__` does `from app.routers.admin_crawl_router
import router as admin_crawl_router`), not the module — see
`crawl_error_visibility_steps.py`'s own docstring for this exact trap,
verified directly against this environment. Imported via `sys.modules`
instead.
"""
from __future__ import annotations

import json
import sys

from behave import given, then, when  # type: ignore[import-untyped]

from app.services import event_dedup
from tests.bdd.steps.scheduled_incremental_instagram_crawl_steps import (
    _create_target,
    _ensure_context,
    _post,
    _run,
)

import app.routers.admin_crawl_router  # noqa: F401 -- populates sys.modules

admin_crawl_router = sys.modules["app.routers.admin_crawl_router"]

_HANDLE = "seedstatetarget"


# ── §A: arranges ─────────────────────────────────────────────────────────────
@given("a crawl target whose reels have never been seeded")
def step_given_reels_never_seeded(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, _HANDLE, crawl_reels=True)


@given("a crawl target whose reels stream already completed and returned no items")
def step_given_reels_already_completed_empty(context):
    """Arranged DIRECTLY (never by first running an empty seed through
    `run_target`) — this feature's scenarios are independent of each other,
    matching the sibling crawl feature files' own convention of arranging
    a target's stored state by hand rather than chaining scenarios."""
    _ensure_context(context)
    context.ic_handle = _create_target(context, _HANDLE, crawl_reels=True)
    context.ic_dao.update_crawl_target(context.ic_handle, {"reels_seeded_at": context.ic_now})


class _BookkeepingFailsOnceDao:
    """Wraps the real per-scenario DAO and makes exactly the FIRST
    `update_crawl_target` call raise — mirrors
    tests/test_instagram_crawl_service.py's own `_BookkeepingFailsOnceDao`,
    the fake built for the SAME production incident
    (2026-08-09, entreamigos.praia) this scenario's non-negotiable protects
    against."""

    def __init__(self, inner):
        self._inner = inner
        self._armed = True

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def update_crawl_target(self, handle, fields):
        if self._armed:
            self._armed = False
            raise RuntimeError("simulated bookkeeping write failure")
        return self._inner.update_crawl_target(handle, fields)


@given("the post-run bookkeeping write fails")
def step_given_bookkeeping_write_fails(context):
    _ensure_context(context)
    context.ic_dao = _BookkeepingFailsOnceDao(context.ic_dao)
    context.ic_service.venue_dao = context.ic_dao


# ── §A: driving the crawl ────────────────────────────────────────────────────
@when("its reels stream completes and returns no items")
def step_when_reels_completes_empty(context):
    context.ic_apify.program_posts(context.ic_handle, "reels", [])
    context.ic_bookkeeping_error = None
    try:
        context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))
    except Exception as e:  # the bookkeeping-failure scenario expects this
        context.ic_bookkeeping_error = e


@when("its reels stream is blocked")
def step_when_reels_blocked(context):
    context.ic_apify.program_error(context.ic_handle, "reels", code="no_items", request_error_count=5)
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@when("its reels stream times out")
def step_when_reels_times_out(context):
    context.ic_apify.program_error(context.ic_handle, "reels", code="timeout", request_error_count=0)
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@when("its reels stream reports the handle does not exist")
def step_when_reels_handle_not_found(context):
    context.ic_apify.program_error(context.ic_handle, "reels", code="not_found", request_error_count=0)
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@when("its reels stream returns items")
def step_when_reels_returns_items(context):
    # Newest timestamp deliberately matches what stream_dedupe_and_venue_
    # attribution_steps.py's existing "the reels cursor advances to the
    # newest reel" step already asserts (2026-08-03T10:00:00Z) — that step
    # is REUSED here (never redefined: Behave's step registry is global and
    # text-keyed, and a second identical registration raises AmbiguousStep,
    # verified directly against this environment), so this scenario's own
    # data has to satisfy its hardcoded expectation rather than the other
    # way around.
    context.ic_apify.program_posts(context.ic_handle, "reels", [
        _post("seedreel1", "2026-08-01T09:00:00.000Z"),
        _post("seedreel2", "2026-08-03T10:00:00.000Z"),  # the newer of the two
    ])
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@when("the target is crawled again")
def step_when_crawled_again(context):
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@when("the crawl targets are read from the admin API")
def step_when_crawl_targets_read_from_admin_api(context):
    class _Container:
        pass

    container = _Container()
    container.pipeline_repository = context.ic_dao
    admin_crawl_router.set_container(container)
    # `list_crawl_targets` called as a plain Python function (no ASGI
    # pipeline), then wrapped through the SAME `CrawlTargetOut` model the
    # real HTTP response would validate against — mirrors
    # crawl_error_visibility_steps.py's own `step_when_operator_reads_
    # crawl_targets`.
    targets = [
        admin_crawl_router.CrawlTargetOut(**row)
        for row in admin_crawl_router.list_crawl_targets()
    ]
    context.ic_admin_targets = {t.handle: t for t in targets}


# ── §A: assertions ───────────────────────────────────────────────────────────
@then("the target is recorded as having seeded its reels")
def step_then_target_recorded_seeded(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["reels_seeded_at"] is not None, row


@then("the target is not recorded as having seeded its reels")
def step_then_target_not_recorded_seeded(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["reels_seeded_at"] is None, row


@then("no reels stream runs")
def step_then_no_reels_stream_runs(context):
    calls = [c for c in context.ic_apify.calls if c["results_type"] == "reels"]
    assert calls == [], calls


@then("the run reports that reels were skipped because they are already seeded")
def step_then_run_reports_skipped_seeded(context):
    assert context.ic_last_report["streams"]["reels"]["outcome"] == "skipped_seeded", (
        context.ic_last_report
    )


@then("the next crawl runs the reels stream again")
def step_then_next_crawl_runs_reels_again(context):
    before = len([c for c in context.ic_apify.calls if c["results_type"] == "reels"])
    context.ic_apify.program_posts(context.ic_handle, "reels", [])
    _run(context.ic_service.run_target(context.ic_handle))
    after = len([c for c in context.ic_apify.calls if c["results_type"] == "reels"])
    assert after == before + 1, (before, after)


# "the reels cursor advances to the newest reel" is intentionally NOT
# defined here — it already exists at stream_dedupe_and_venue_attribution_
# steps.py:200 (identical text; Behave dispatches by text, so a second
# registration would raise AmbiguousStep). See the module docstring on
# `step_when_reels_returns_items` above for why this scenario's own data is
# shaped to satisfy that existing step's assertion.


@then("the reels cursor is still empty")
def step_then_reels_cursor_still_empty(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["cursor_reels_at"] is None, row


@then("the target reports that its reels are seeded")
def step_then_target_reports_reels_seeded(context):
    target = context.ic_admin_targets[context.ic_handle]
    assert target.reels_seeded is True, target


# ── §B: config validated on write, not coerced on read ───────────────────────
_DEDUP_KEYS_MALFORMED = {
    "event_dedup_generic_vocabulary": "not-a-list",
    "event_dedup_stopwords": "not-a-list",
    "event_dedup_lineup_threshold": 0,
    "event_dedup_candidate_window_hours": -1,
    "event_dedup_undated_window_days": -1,
    "event_dedup_auto_merge_enabled": "yes",
}
_DEDUP_KEYS_VALID = {
    "event_dedup_generic_vocabulary": ["festa", "aniversario"],
    "event_dedup_stopwords": ["de", "da"],
    "event_dedup_lineup_threshold": 3,
    "event_dedup_candidate_window_hours": 10,
    "event_dedup_undated_window_days": 20,
    "event_dedup_auto_merge_enabled": True,
}


def _attempt_set(context, key, value):
    try:
        context.admin_config_service.set(key, value, updated_by="bdd-test")
        return None
    except Exception as e:  # noqa: BLE001 - the scenario asserts on this
        return e


@when("an operator sets the auto-merge flag to a value that is not a boolean")
def step_when_sets_auto_merge_non_boolean(context):
    key = "event_dedup_auto_merge_enabled"
    # A genuinely valid PRIOR write, so "the stored flag is unchanged"
    # below proves something real, not just "still absent".
    context.admin_config_service.set(key, True, updated_by="baseline")
    context.dedup_write_error = _attempt_set(context, key, "yes")


@when('an operator sets the auto-merge flag to the text "false"')
def step_when_sets_auto_merge_false_string(context):
    key = "event_dedup_auto_merge_enabled"
    context.dedup_write_error = _attempt_set(context, key, "false")


@when("an operator sets each event dedup config key to a malformed value")
def step_when_sets_each_key_malformed(context):
    context.dedup_write_errors = {
        key: _attempt_set(context, key, value) for key, value in _DEDUP_KEYS_MALFORMED.items()
    }


@when("an operator sets each event dedup config key to a valid value")
def step_when_sets_each_key_valid(context):
    context.dedup_write_errors = {
        key: _attempt_set(context, key, value) for key, value in _DEDUP_KEYS_VALID.items()
    }


@given("a stored dedup config value whose type is wrong")
def step_given_stored_value_wrong_type(context):
    # Bypasses AdminConfigService.set entirely -- exactly how a value
    # stored BEFORE §C's validators were registered (or hand-edited in
    # RDS) would look: valid JSON, wrong TYPE for the key.
    context.fake_redis.set(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, json.dumps("false"))
    # A DIFFERENT key's own valid, independent override.
    context.fake_redis.set(event_dedup.ADMIN_CONFIG_LINEUP_THRESHOLD_KEY, json.dumps(3))
    context.dedup_fallback_before = (
        event_dedup.EVENT_DEDUP_CONFIG_TYPE_FALLBACK_TOTAL
        .labels(key=event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY)._value.get()
    )


@when("the dedup configuration is read")
def step_when_dedup_config_read(context):
    context.dedup_config = event_dedup.load_dedup_config(context.fake_redis)


@then("the write is refused")
def step_then_write_refused(context):
    assert context.dedup_write_error is not None, "expected the write to be refused"


@then("the stored auto-merge flag is unchanged")
def step_then_stored_auto_merge_unchanged(context):
    assert context.admin_config_service.get("event_dedup_auto_merge_enabled") is True


@then("auto-merge is not enabled")
def step_then_auto_merge_not_enabled(context):
    config = event_dedup.load_dedup_config(context.fake_redis)
    assert config.auto_merge_enabled is False, config


@then("every one of those writes is refused")
def step_then_every_write_refused(context):
    succeeded = {k: v for k, v in context.dedup_write_errors.items() if v is None}
    assert not succeeded, f"expected every write to be refused, but these succeeded: {list(succeeded)}"


@then("every one of those writes is stored")
def step_then_every_write_stored(context):
    failed = {k: v for k, v in context.dedup_write_errors.items() if v is not None}
    assert not failed, f"expected every write to succeed, but these raised: {failed}"
    for key in _DEDUP_KEYS_VALID:
        assert context.admin_config_service.get(key) is not None, key


@then("the shipped default is used for that key")
def step_then_shipped_default_used(context):
    assert context.dedup_config.auto_merge_enabled == event_dedup.DEFAULT_AUTO_MERGE_ENABLED


@then("the type fallback is counted")
def step_then_type_fallback_counted(context):
    after = (
        event_dedup.EVENT_DEDUP_CONFIG_TYPE_FALLBACK_TOTAL
        .labels(key=event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY)._value.get()
    )
    assert after == context.dedup_fallback_before + 1, (context.dedup_fallback_before, after)


@then("every other dedup key keeps its own stored value")
def step_then_every_other_key_keeps_its_value(context):
    assert context.dedup_config.lineup_threshold == 3
