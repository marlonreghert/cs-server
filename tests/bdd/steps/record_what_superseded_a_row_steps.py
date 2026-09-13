"""Behave steps for tests/bdd/enrichment/record-what-superseded-a-row.feature.

See plans/260814_record-what-superseded-a-row.md.

Self-contained (own fresh `InMemoryRdsVenueStore` + fakeredis), mirroring
`tests/bdd/steps/event_dedup_fuzzy_title_steps.py`'s own pattern — nothing
here needs a real extraction pipeline, only `app.services.
event_reconciliation.reconcile_post_events` (the re-extraction supersede
path this feature's §A changes) and, for one scenario,
`app.services.event_merge.merge_touched_events` (the CROSS-post merge path
this feature's own Non-goals say stays untouched — see plan §"Re-running
dedup").

### Reusing "the event is read from the admin API" without redefining it
`review_gate_and_date_vocabulary_steps.py` already registers `@when("the
event is read from the admin API")` over its OWN `context.rgv_admin_event_id`
/ `context.ee_dao` / `context.rgv_admin_response` fixture (itself reusing
`instagram_event_extraction_steps.py`'s `_build_admin_events_app`). Behave
allows only ONE registration of an identical step pattern across the whole
`tests/bdd/steps/` directory — redefining that text here would raise
AmbiguousStep. This module's admin-API scenarios instead set `context.
ee_dao`/`context.rgv_admin_event_id` themselves (the exact contract that
step already reads) and let it run unmodified — the SAME reuse-by-shared-
context-attribute convention `backfill_misattributed_links_steps.py`'s own
docstring documents for "the backfill runs with apply".

### The back-fill script may not exist yet
`scripts.backfill_superseded_by` is imported LAZILY, inside `_run_backfill`,
never at module top level — so a `record-what-superseded-a-row` red capture
taken before that script exists still collects and runs every scenario in
this feature (the runtime-reconciliation and admin-API scenarios exercise
already-existing code and need no such guard); the back-fill scenarios
instead redden on a real, controlled assertion (`report is not None`) via
the exact same try/except-and-inspect convention `history_repair_dates_
steps.py`'s own `_run_repair` already established for its own script.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import fakeredis
from behave import given, then, when  # type: ignore[import-untyped]

import app.metrics as _metrics_module
from app.dao.venue_repository import VenueRepository
from app.models.venue import Venue
from app.services import event_dedup
from app.services.event_merge import merge_touched_events
from app.services.event_reconciliation import (
    STATUS_SUPERSEDED,
    new_event_id,
    reconcile_post_events,
)
from tests.rds_fake import InMemoryRdsVenueStore

_NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
_RECIFE = ZoneInfo("America/Recife")
_SHARED_TITLE = "SECRET CLUB"
_RESOLVED_DATE = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)
_RESOLVED_DATE_2 = datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)
_BF_TITLE = "Backfill Night"


# ── context / fixture setup ──────────────────────────────────────────────────
def _ensure(context) -> None:
    if hasattr(context, "rws_dao"):
        return
    context.rws_store = InMemoryRdsVenueStore()
    context.rws_dao = VenueRepository(client=None, rds_store=context.rws_store)
    context.rws_redis = fakeredis.FakeRedis(decode_responses=True)
    context.rws_venue_id = "rws_venue"
    context.rws_dao.upsert_venue(Venue(
        venue_id=context.rws_venue_id, venue_name="RWS Test Venue",
        venue_lat=-8.05, venue_lng=-34.88, venue_address="",
    ))
    context.rws_seq = 0
    context.rws_handle = "rws_handle"
    context.rws_shortcode = "rws_post"
    context.rws_old_id = None
    context.rws_old_title = None
    context.rws_prepared = None
    context.rws_outcome_deltas = {}
    context.rws_bf_orphan_id = None
    context.rws_bf_report = None
    context.rws_bf_exception = None


def _next_seq(context) -> int:
    _ensure(context)
    context.rws_seq += 1
    return context.rws_seq


def _bf_ensure(context) -> None:
    _ensure(context)
    if getattr(context, "rws_bf_handle", None) is None:
        context.rws_bf_handle = "rws_bf_handle"
        context.rws_bf_shortcode = "rws_bf_post"


def _attribute_for(context):
    venue_id = context.rws_venue_id

    def attribute(fields, event_id):
        del fields, event_id
        return {"venue_id": venue_id}, None

    return attribute


def _event(title, starts_at, **overrides) -> dict:
    base = {
        "starts_at": starts_at, "ends_at": None, "is_recurring": False,
        "recurrence_text": None, "title": title, "description": None,
        "lineup": [], "ticket_url": None, "price_text": None,
        "location_text": None, "confidence": 0.9, "review_reason": None,
        "raw_extraction": {"title": title},
    }
    base.update(overrides)
    return base


def _reconcile(context, events, *, handle=None, shortcode=None) -> int:
    _ensure(context)
    return reconcile_post_events(
        venue_dao=context.rws_dao, source_kind="venue_post",
        source_handle=handle or context.rws_handle,
        source_shortcode=shortcode or context.rws_shortcode,
        source_permalink=None, prepared_events=events, now=_NOW,
        attribute=_attribute_for(context),
    )


def _post_rows(context, handle=None, shortcode=None) -> list:
    _ensure(context)
    return context.rws_dao.list_events_by_source(
        handle or context.rws_handle, shortcode or context.rws_shortcode,
    )


def _insert_direct(
    context, title, *, starts_at, first_seen_at, status="pending_review",
    source_handle=None, source_shortcode=None,
) -> str:
    _ensure(context)
    seq = _next_seq(context)
    event_id = new_event_id()
    context.rws_dao.insert_event({
        "event_id": event_id, "venue_id": context.rws_venue_id, "starts_at": starts_at,
        "title": title, "post_type": "event", "lineup": [], "status": status,
        "source_kind": "venue_post",
        "source_handle": source_handle or f"rws_direct_handle_{seq}",
        "source_shortcode": source_shortcode or f"rws_direct_post_{seq}",
        "first_seen_at": first_seen_at, "last_seen_at": first_seen_at,
    })
    return event_id


def _supersede_outcome_counts() -> dict:
    counter = getattr(_metrics_module, "EVENT_SUPERSEDE_REPLACEMENT_TOTAL", None)
    counts: dict = {}
    if counter is None:
        return counts
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                counts[sample.labels.get("outcome")] = sample.value
    return counts


def _target_orphan_id(context) -> str:
    if getattr(context, "rws_bf_orphan_id", None) is not None:
        return context.rws_bf_orphan_id
    return context.rws_old_id


def _run_backfill(context, *, apply: bool) -> None:
    _ensure(context)
    context.rws_bf_exception = None
    try:
        from scripts.backfill_superseded_by import run_backfill  # lazy: see module docstring

        context.rws_bf_report = run_backfill(context.rws_dao, apply=apply)
    except Exception as exc:  # noqa: BLE001 - captured for Then steps to inspect
        context.rws_bf_exception = exc
        context.rws_bf_report = None


# ── Given: the runtime re-extraction supersede path ─────────────────────────
@given("a stored event superseded by a re-extraction of its own post")
def step_given_stored_event_to_be_superseded(context):
    _ensure(context)
    _reconcile(context, [_event(_SHARED_TITLE, None)])
    row = _post_rows(context)[0]
    context.rws_old_id = row["event_id"]
    context.rws_old_title = _SHARED_TITLE
    # Overridden by the "And" step that follows in every scenario using
    # this Given — an empty default means "this post's re-extraction found
    # nothing at all" for the one scenario ("Never link across posts") that
    # never overrides it.
    context.rws_prepared = []


@given("the post's new events include exactly one with the same title")
def step_given_new_events_one_matching_title(context):
    context.rws_prepared = [_event(context.rws_old_title, _RESOLVED_DATE)]


@given("the post's new events carry no matching title")
def step_given_new_events_no_matching_title(context):
    context.rws_prepared = [_event("A Totally Different Night", _RESOLVED_DATE)]


@given("two of the post's new events carry the same title")
def step_given_two_new_events_same_title(context):
    context.rws_prepared = [
        _event(context.rws_old_title, _RESOLVED_DATE),
        # Same normalised title, different casing — proves the ambiguity
        # check normalises rather than string-matches raw titles.
        _event(context.rws_old_title.lower(), _RESOLVED_DATE_2),
    ]


@given("a different post has an event with the same title")
def step_given_different_post_same_title(context):
    _ensure(context)
    other_handle, other_shortcode = "rws_other_handle", "rws_other_post"
    _reconcile(
        context, [_event(context.rws_old_title, _RESOLVED_DATE)],
        handle=other_handle, shortcode=other_shortcode,
    )
    other_row = _post_rows(context, other_handle, other_shortcode)[0]
    context.rws_other_post_event_id = other_row["event_id"]
    # This post's own re-extraction still yields nothing — the old row is
    # superseded with no candidate from its OWN post either way.
    context.rws_prepared = []


@given("a post whose re-extraction supersedes one linkable and one ambiguous event")
def step_given_post_supersedes_one_linkable_one_ambiguous(context):
    _ensure(context)
    _reconcile(context, [
        _event("Linkable Night", None),
        _event("Ambiguous Night", None),
    ])
    context.rws_prepared = [
        _event("Linkable Night", _RESOLVED_DATE),
        _event("Ambiguous Night", _RESOLVED_DATE),
        _event("ambiguous night", _RESOLVED_DATE_2),
    ]


@when("the post is reconciled")
def step_when_post_is_reconciled(context):
    _ensure(context)
    before = _supersede_outcome_counts()
    _reconcile(context, context.rws_prepared or [])
    after = _supersede_outcome_counts()
    context.rws_outcome_deltas = {
        label: after.get(label, 0) - before.get(label, 0)
        for label in set(before) | set(after)
    }


# ── Then: the runtime re-extraction supersede path ──────────────────────────
@then("the superseded event records that event as its replacement")
def step_then_superseded_records_replacement(context):
    orphan_id = _target_orphan_id(context)
    row = context.rws_dao.get_event(orphan_id)
    assert row is not None, orphan_id
    assert row["status"] == STATUS_SUPERSEDED, row
    if getattr(context, "rws_bf_orphan_id", None) is not None:
        expected = context.rws_bf_successor_id
    else:
        expected = next(
            r["event_id"] for r in _post_rows(context)
            if r["event_id"] != orphan_id and r.get("status") != STATUS_SUPERSEDED
        )
    assert row.get("superseded_by") == expected, (row.get("superseded_by"), expected)


@then("the superseded event records no replacement")
def step_then_superseded_records_no_replacement(context):
    row = context.rws_dao.get_event(context.rws_old_id)
    assert row is not None, context.rws_old_id
    assert row["status"] == STATUS_SUPERSEDED, row
    assert row.get("superseded_by") is None, row


@then("the unlinked supersede is counted")
def step_then_unlinked_supersede_counted(context):
    deltas = context.rws_outcome_deltas
    total = deltas.get("unlinked_no_candidate", 0) + deltas.get("unlinked_ambiguous", 0)
    assert total >= 1, deltas


@then("the superseded event does not record that event as its replacement")
def step_then_superseded_does_not_record_other(context):
    row = context.rws_dao.get_event(context.rws_old_id)
    assert row is not None, context.rws_old_id
    assert row.get("superseded_by") != context.rws_other_post_event_id, row


@then("the linked and unlinked supersedes are counted separately")
def step_then_linked_and_unlinked_counted_separately(context):
    deltas = context.rws_outcome_deltas
    linked = deltas.get("linked", 0)
    unlinked = deltas.get("unlinked_no_candidate", 0) + deltas.get("unlinked_ambiguous", 0)
    assert linked >= 1, deltas
    assert unlinked >= 1, deltas


# ── the merge path's own (unchanged) replacement recording ─────────────────
@given("two events merged by title similarity")
def step_given_two_events_title_similar(context):
    _ensure(context)
    context.rws_redis.set(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, json.dumps(True))
    starts_at = datetime(2026, 8, 7, 21, 0, tzinfo=_RECIFE)
    # "Rodolpho" / "Rodolpho Produções": one title contains the other, same
    # venue, same start time — the exact fixture
    # event-dedup-fuzzy-title.feature's own "Supersede rather than delete
    # the item a title merge absorbs" scenario already proves reaches the
    # AUTO band.
    first_id = _insert_direct(context, "Rodolpho", starts_at=starts_at, first_seen_at=_NOW)
    second_id = _insert_direct(
        context, "Rodolpho Produções", starts_at=starts_at, first_seen_at=_NOW + timedelta(seconds=1),
    )
    context.rws_merge_ids = [first_id, second_id]


@when("the merge is applied")
def step_when_merge_is_applied(context):
    merge_touched_events(context.rws_dao, context.rws_merge_ids, _NOW, redis_like=context.rws_redis)


@then("the absorbed event still records the surviving event as its replacement")
def step_then_absorbed_records_surviving(context):
    rows = [context.rws_dao.get_event(eid) for eid in context.rws_merge_ids]
    absorbed = next(r for r in rows if r["status"] == STATUS_SUPERSEDED)
    survivor = next(r for r in rows if r["event_id"] != absorbed["event_id"])
    assert absorbed.get("superseded_by") == survivor["event_id"], (absorbed, survivor)


# ── admin API ────────────────────────────────────────────────────────────────
@given("a superseded event that records a replacement")
def step_given_superseded_event_with_replacement(context):
    _ensure(context)
    successor_id = _insert_direct(context, "Live Event", starts_at=_RESOLVED_DATE, first_seen_at=_NOW)
    orphan_id = _insert_direct(
        context, "Live Event", starts_at=None, first_seen_at=_NOW - timedelta(days=1),
    )
    context.rws_dao.update_event(orphan_id, {"status": STATUS_SUPERSEDED, "superseded_by": successor_id})
    context.rws_expected_successor_id = successor_id
    context.ee_dao = context.rws_dao
    context.rgv_admin_event_id = orphan_id


@given("a superseded event that records no replacement")
def step_given_superseded_event_no_replacement(context):
    _ensure(context)
    orphan_id = _insert_direct(context, "Orphan Event", starts_at=None, first_seen_at=_NOW)
    context.rws_dao.update_event(orphan_id, {"status": STATUS_SUPERSEDED})
    context.ee_dao = context.rws_dao
    context.rgv_admin_event_id = orphan_id


# "When the event is read from the admin API" is ALREADY registered by
# review_gate_and_date_vocabulary_steps.py over the SAME context.ee_dao /
# context.rgv_admin_event_id / context.rgv_admin_response contract set
# above — see this module's own docstring. Never redefined here.


@then("the event reports which event replaced it")
def step_then_api_reports_replacement(context):
    assert context.rgv_admin_response.status_code == 200, context.rgv_admin_response.text
    body = context.rgv_admin_response.json()
    assert body.get("superseded_by") == context.rws_expected_successor_id, body


@then("the event reports no replacement")
def step_then_api_reports_no_replacement(context):
    assert context.rgv_admin_response.status_code == 200, context.rgv_admin_response.text
    body = context.rgv_admin_response.json()
    assert body.get("superseded_by") is None, body


# ── the back-fill (§C) ───────────────────────────────────────────────────────
@given("a superseded event with no recorded replacement")
def step_given_superseded_no_replacement_bf(context):
    _bf_ensure(context)
    orphan_id = _insert_direct(
        context, _BF_TITLE, starts_at=None, first_seen_at=_NOW,
        source_handle=context.rws_bf_handle, source_shortcode=context.rws_bf_shortcode,
    )
    context.rws_dao.update_event(orphan_id, {"status": STATUS_SUPERSEDED})
    context.rws_bf_orphan_id = orphan_id
    context.rws_bf_successor_id = None


@given("exactly one live event from the same post carries the same title")
def step_given_one_live_sibling_same_title(context):
    _bf_ensure(context)
    successor_id = _insert_direct(
        context, _BF_TITLE, starts_at=_RESOLVED_DATE, first_seen_at=_NOW, status="accepted",
        source_handle=context.rws_bf_handle, source_shortcode=context.rws_bf_shortcode,
    )
    context.rws_bf_successor_id = successor_id


@given("two live events from the same post carry the same title")
def step_given_two_live_siblings_same_title(context):
    _bf_ensure(context)
    _insert_direct(
        context, _BF_TITLE, starts_at=_RESOLVED_DATE, first_seen_at=_NOW, status="accepted",
        source_handle=context.rws_bf_handle, source_shortcode=context.rws_bf_shortcode,
    )
    _insert_direct(
        context, _BF_TITLE.lower(), starts_at=_RESOLVED_DATE_2, first_seen_at=_NOW, status="accepted",
        source_handle=context.rws_bf_handle, source_shortcode=context.rws_bf_shortcode,
    )


@given("the only same-titled live event comes from a different post")
def step_given_only_match_from_different_post(context):
    _bf_ensure(context)
    _insert_direct(
        context, _BF_TITLE, starts_at=_RESOLVED_DATE, first_seen_at=_NOW, status="accepted",
        source_handle="rws_bf_other_handle", source_shortcode="rws_bf_other_post",
    )


@given("a back-fill has already linked every unambiguous orphan")
def step_given_backfill_already_linked(context):
    _bf_ensure(context)
    orphan_id = _insert_direct(
        context, _BF_TITLE, starts_at=None, first_seen_at=_NOW,
        source_handle=context.rws_bf_handle, source_shortcode=context.rws_bf_shortcode,
    )
    context.rws_dao.update_event(orphan_id, {"status": STATUS_SUPERSEDED})
    successor_id = _insert_direct(
        context, _BF_TITLE, starts_at=_RESOLVED_DATE, first_seen_at=_NOW, status="accepted",
        source_handle=context.rws_bf_handle, source_shortcode=context.rws_bf_shortcode,
    )
    context.rws_bf_orphan_id = orphan_id
    context.rws_bf_successor_id = successor_id
    _run_backfill(context, apply=True)
    assert context.rws_bf_exception is None, context.rws_bf_exception
    assert context.rws_bf_report is not None


@when("the back-fill runs with apply")
def step_when_backfill_runs_with_apply(context):
    _run_backfill(context, apply=True)


@when("the back-fill runs without apply")
def step_when_backfill_runs_without_apply(context):
    _run_backfill(context, apply=False)


@when("the back-fill runs with apply again")
def step_when_backfill_runs_with_apply_again(context):
    _bf_ensure(context)
    context.rws_bf_snapshot = {
        context.rws_bf_orphan_id: context.rws_dao.get_event(context.rws_bf_orphan_id).get("superseded_by"),
    }
    _run_backfill(context, apply=True)


@then("the superseded event still records no replacement")
def step_then_superseded_still_records_no_replacement(context):
    row = context.rws_dao.get_event(context.rws_bf_orphan_id)
    assert row is not None, context.rws_bf_orphan_id
    assert row["status"] == STATUS_SUPERSEDED, row
    assert row.get("superseded_by") is None, row


@then("the report names it as ambiguous")
def step_then_report_names_ambiguous(context):
    # A string literal, deliberately never an imported disposition constant
    # (unlike the runtime-path steps above, which import freely from
    # already-existing app.services.event_reconciliation): this module may
    # not exist yet — see this file's own docstring — and `report is None`
    # already turns that into a genuine, controlled assertion failure
    # rather than a raw ModuleNotFoundError surfacing as a Behave "error".
    report = context.rws_bf_report
    assert report is not None, context.rws_bf_exception
    decision = next(d for d in report.rows if d.event_id == context.rws_bf_orphan_id)
    assert decision.action == "ambiguous", decision


@then("the report says it would have been linked")
def step_then_report_would_have_linked(context):
    # See step_then_report_names_ambiguous, immediately above, for why this
    # compares against a string literal rather than an imported constant.
    report = context.rws_bf_report
    assert report is not None, context.rws_bf_exception
    decision = next(d for d in report.rows if d.event_id == context.rws_bf_orphan_id)
    assert decision.action == "would_link", decision


@then("no event's replacement changes")
def step_then_no_replacement_changes(context):
    assert context.rws_bf_exception is None, context.rws_bf_exception
    row = context.rws_dao.get_event(context.rws_bf_orphan_id)
    assert row is not None, context.rws_bf_orphan_id
    before = context.rws_bf_snapshot[context.rws_bf_orphan_id]
    assert row.get("superseded_by") == before, (row.get("superseded_by"), before)
