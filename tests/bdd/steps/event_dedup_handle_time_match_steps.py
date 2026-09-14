"""Behave steps for tests/bdd/enrichment/event-dedup-handle-time-match.feature.

See plans/260913_candidate-cap-and-handle-time-merge.md Part B. Reuses the
`context.dedup_*` harness `event_dedup_fuzzy_title_steps.py` already builds
(own fresh `InMemoryRdsVenueStore` + fakeredis-backed admin app) as PLAIN
FUNCTION CALLS — the same reuse pattern `events_venue_night_duplication_
steps.py` already establishes for its own Defect 1/3 scenarios, and the
same repo convention documented there: "Reusing [a step text] is this
repo's own documented convention for a step text two features share." The
Background's "the candidate window is {hours:d} hours" step and the "the
merge pass runs" step are NOT redefined here — both are already bound in
`event_dedup_fuzzy_title_steps.py`.

Own namespace (`context.htm_*`) for everything genuinely new to this
feature, matching this repo's per-feature context-prefix convention.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from behave import given, then, when  # type: ignore[import-untyped]

import scripts.measure_event_dedup as med
from app.services import event_dedup
from app.services.event_attribution_dispute import (
    REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE,
)
from tests.bdd.steps.event_dedup_fuzzy_title_steps import (
    _alive,
    _ensure_context as _ensure_dedup_context,
    _seed_item,
    _survivors,
)

RECIFE = ZoneInfo("America/Recife")


def _local_dt(date_str: str, time_str: str = "00:00") -> datetime:
    hour, minute = (int(p) for p in time_str.split(":"))
    year, month, day = (int(p) for p in date_str.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=RECIFE)


def _ensure(context) -> None:
    _ensure_dedup_context(context)
    if not hasattr(context, "htm_last_handle"):
        context.htm_last_handle = None
        context.htm_merged_before = _merge_total("merged_handle_time_match")


def _merge_total(outcome: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "event_merge_total", {"identity": "title", "outcome": outcome},
    ) or 0.0


def _seed_htm(
    context, handle, title, venue, date, time_str, *, time_known,
    disputed=False, already_linked=False,
):
    _ensure(context)
    starts_at = _local_dt(date, time_str or "00:00")
    event_id = _seed_item(context, title, venue, starts_at=starts_at, source_handle=handle)
    update: dict = {"time_known": time_known}
    if disputed:
        update["review_reason"] = REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE
    if already_linked:
        update["location_resolution"] = "auto"
        update["linked_by"] = "handle_mention"
    context.dedup_dao.update_event(event_id, update)
    context.htm_last_handle = handle
    return event_id


# ── the flag ─────────────────────────────────────────────────────────────
@given("the handle-time-match rule is enabled")
def step_given_enabled(context):
    _ensure(context)
    context.dedup_redis.set(
        event_dedup.ADMIN_CONFIG_HANDLE_TIME_MATCH_ENABLED_KEY, json.dumps(True),
    )


@given("the handle-time-match rule is disabled")
def step_given_disabled(context):
    _ensure(context)
    context.dedup_redis.set(
        event_dedup.ADMIN_CONFIG_HANDLE_TIME_MATCH_ENABLED_KEY, json.dumps(False),
    )


# ── item fixtures ────────────────────────────────────────────────────────
@given('the handle "{handle}" posted "{title}" at "{venue}" starting {date} {time}, with a known time and no attribution dispute')
def step_given_handle_posted_known(context, handle, title, venue, date, time):
    _seed_htm(context, handle, title, venue, date, time, time_known=True)


@given('the same handle posted "{title}" at "{venue}" starting {date} {time}, with a known time and no attribution dispute')
def step_given_same_handle_posted_known(context, title, venue, date, time):
    _seed_htm(context, context.htm_last_handle, title, venue, date, time, time_known=True)


@given('the same handle posted "{title}" at "{venue}" starting {date} {time}, with a known time and a location-text attribution dispute against the mapped venue')
def step_given_same_handle_posted_disputed(context, title, venue, date, time):
    _seed_htm(
        context, context.htm_last_handle, title, venue, date, time,
        time_known=True, disputed=True,
    )


@given('the same handle posted "{title}" at "{venue}" on {date}, with an unknown time and no attribution dispute')
def step_given_same_handle_posted_unknown(context, title, venue, date):
    _seed_htm(context, context.htm_last_handle, title, venue, date, "00:00", time_known=False)


@given('the handle "{handle}" posted "{title}" at "{venue}" on {date}, with an unknown time and no attribution dispute')
def step_given_handle_posted_unknown(context, handle, title, venue, date):
    _seed_htm(context, handle, title, venue, date, "00:00", time_known=False)


@given('the promoter handle "{handle}" posted "{title}" at "{venue}" starting {date} {time}, with a known time, already linked by a handle mention')
def step_given_promoter_handle_posted_linked(context, handle, title, venue, date, time):
    _seed_htm(
        context, handle, title, venue, date, time,
        time_known=True, already_linked=True,
    )


@given('the same promoter handle posted "{title}" at "{venue}" starting {date} {time}, with a known time, already linked by a handle mention')
def step_given_same_promoter_handle_posted_linked(context, title, venue, date, time):
    _seed_htm(
        context, context.htm_last_handle, title, venue, date, time,
        time_known=True, already_linked=True,
    )


@given("an operator has edited the title of the most recently posted event")
def step_given_operator_edited_title(context):
    event_id = context.dedup_event_ids[-1]
    context.dedup_dao.update_event(event_id, {"operator_edited_fields": ["title"]})


# ── Then: survivors ──────────────────────────────────────────────────────
@then("the three posts collapse into one surviving event")
def step_then_three_collapse(context):
    ids = context.dedup_event_ids[-3:]
    survivors = _survivors(context, ids)
    assert len(survivors) == 1, survivors
    context.dedup_survivor = survivors[0]


@then("all three posts remain as separate events")
def step_then_three_remain(context):
    ids = context.dedup_event_ids[-3:]
    survivors = _survivors(context, ids)
    assert len(survivors) == 3, survivors


@then("both posts remain as separate events")
def step_then_two_remain(context):
    ids = context.dedup_event_ids[-2:]
    survivors = _survivors(context, ids)
    assert len(survivors) == 2, survivors


@then("the two posts collapse into one surviving event")
def step_then_two_collapse(context):
    ids = context.dedup_event_ids[-2:]
    survivors = _survivors(context, ids)
    assert len(survivors) == 1, survivors
    context.dedup_survivor = survivors[0]


# ── Then: reasons and audit ──────────────────────────────────────────────
def _latest_auto_merged_reasons(context) -> tuple:
    audited = context.dedup_dao.list_event_merge_suggestions(
        event_id=context.dedup_survivor, decision="auto_merged",
    )
    assert audited, "no auto_merged audit row found for the surviving event"
    return tuple(audited[-1].get("reasons") or ())


@then("the merge is recorded with the handle-time-match reason")
def step_then_recorded_with_htm(context):
    assert event_dedup.REASON_HANDLE_TIME_MATCH in _latest_auto_merged_reasons(context)


@then("the merge is recorded with both the title and the handle-time-match reasons")
def step_then_recorded_with_both(context):
    reasons = _latest_auto_merged_reasons(context)
    assert event_dedup.REASON_TITLE in reasons, reasons
    assert event_dedup.REASON_HANDLE_TIME_MATCH in reasons, reasons


@then("the pair is recorded as a suggestion instead")
def step_then_recorded_as_suggestion(context):
    ida, idb = context.dedup_event_ids[-2:]
    pending = [
        s for s in context.dedup_dao.list_event_merge_suggestions(event_id=ida, decision="pending")
        if idb in (s["event_id"], s["candidate_event_id"])
    ]
    assert pending, "expected a pending suggestion for the blocked pair"


@then("the merge is counted under its own handle-time-match outcome, not the plain merged outcome")
def step_then_counted_separately(context):
    after = _merge_total("merged_handle_time_match")
    assert after > context.htm_merged_before, (after, context.htm_merged_before)


# ── the dry-run measurement ──────────────────────────────────────────────
@when("the dedup measurement script runs with handle-time-match forced on")
def step_when_measurement_runs(context):
    report_path = f"{tempfile.mkdtemp()}/htm_measure.json"
    context.htm_rows_before = {
        eid: dict(context.dedup_dao.get_event(eid)) for eid in context.dedup_event_ids
    }
    with patch.object(med, "RdsVenueStore", lambda url: context.dedup_dao), \
            patch.object(med, "VenueRepository", lambda client=None, rds_store=None: rds_store):
        med.main(["--handle-time-match", "--report-json", report_path])
    context.htm_measure_report = json.loads(Path(report_path).read_text())


@then("the report lists that pair as a new auto pair")
def step_then_report_lists_pair(context):
    ida, idb = context.dedup_event_ids[-2:]
    ids_in_pairs = {
        (p.get("surviving_event_id"), p.get("absorbed_event_id"))
        for p in context.htm_measure_report.get("auto_pairs", [])
    }
    assert (ida, idb) in ids_in_pairs or (idb, ida) in ids_in_pairs, ids_in_pairs


@then("no event row is changed")
def step_then_no_event_row_changed(context):
    for eid, before in context.htm_rows_before.items():
        after = dict(context.dedup_dao.get_event(eid))
        assert after == before, (eid, before, after)
