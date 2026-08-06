"""Behave steps for tests/bdd/enrichment/venue-post-multi-event.feature.

Drives the REAL EventExtractionService (now multi-event capable) over the
SAME `context.ee_*` harness instagram_event_extraction_steps.py's Background
step already builds — its Given step
`an event-candidate venue with an Instagram handle` is reused UNMODIFIED
(redefining identical step text would be an AmbiguousStep collision), so
every venue scenario here shares that file's `_add_post`/`_run_extraction`
helpers and its `_FakeOpenAIClient` (now `extract_events`-capable, see that
file's docstring).

One scenario ("Flag a divergence on a confirmed promoter event too") proves
the divergence flag the PROMOTER path gains by moving onto the shared
app/services/event_reconciliation.py. It drives the REAL PromoterCrawlService
via multi_event_posts_steps.py's own fixtures (`_reset_context`, `_seed_post`,
`_run_crawl`, `_row_by_title`), called as plain functions — the same reuse
pattern instagram_post_recency_and_unknown_time_steps.py already establishes
for a sibling file.

Several assertions in this feature use EXACT SAME wording as scenarios
already defined in multi_event_posts_steps.py ("three events are persisted
for that post", "each event carries its own title", "the two events carry
different start dates", "both dates are resolved against the post
timestamp", "two events are persisted for that post", and the Given "the
operator confirmed one of them with a corrected title") — these are
intentionally REUSED, not redefined (redefining would be an AmbiguousStep
collision), by pointing that file's data-driven (title/domain-agnostic)
`context.mep_dao`/`context.mep_handle`/`context.mep_shortcode` at THIS
file's venue fixture via `_alias_shared_context`. Those four steps were
generalized (still verified against multi-event-posts.feature's own 14
scenarios, unedited and passing) from hardcoded promoter-specific titles/
dates into data-driven assertions precisely so this reuse is possible without
weakening anything.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.models.venue import Venue
from tests.bdd.steps.instagram_event_extraction_steps import (
    RECIFE_LAT,
    RECIFE_LNG,
    _add_post,
    _run_extraction,
)
from tests.bdd.steps.multi_event_posts_steps import (
    GENERIC_EVENT_CAPTION,
    _events_json,
)
from tests.bdd.steps.multi_event_posts_steps import _reset_context as _reset_mep_context
from tests.bdd.steps.multi_event_posts_steps import _row_by_title as _mep_row_by_title
from tests.bdd.steps.multi_event_posts_steps import _run_crawl as _run_mep_crawl
from tests.bdd.steps.multi_event_posts_steps import _seed_post as _mep_seed_post

# ── local helpers over the ee_ (instagram_event_extraction_steps) harness ───
def _rows(context) -> list[dict]:
    return context.ee_dao.list_events_by_source(context.ee_handle, context.ee_last_shortcode)


def _row_by_title(context, title: str) -> dict:
    row = next((r for r in _rows(context) if r["title"] == title), None)
    assert row is not None, f"no persisted venue event titled {title!r}; rows={_rows(context)}"
    return row


def _snapshot_metric(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


def _alias_shared_context(context, shortcode: str) -> None:
    """Point multi_event_posts_steps.py's generic, data-driven step
    implementations at THIS scenario's venue fixture, so genuinely identical
    wording ("three events are persisted for that post", etc.) is reused
    verbatim rather than re-implemented under a different phrase. See the
    module docstring."""
    context.mep_dao = context.ee_dao
    context.mep_handle = context.ee_handle
    context.mep_shortcode = shortcode


_TS = datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)
THREE_TITLES = ("Noite de Abertura", "Noite Principal", "Noite de Encerramento")


# ── Background ────────────────────────────────────────────────────────────────
@given("its posts are archived with their captions and flyer images")
def step_given_its_posts_are_archived(context):
    pass  # fixture note; posts are added per-scenario via _add_post


# ── Scenarios 1, 2, 15: three events from one venue post ────────────────────
@given("a venue post whose flyer announces three events")
def step_given_a_venue_post_whose_flyer_announces_three_events(context):
    _add_post(context, "vpme_three", timestamp=_TS)
    _alias_shared_context(context, "vpme_three")
    context.vpme_events_per_post_snapshot = _snapshot_metric(
        "event_extraction_events_per_post_sum",
    )
    context.ee_openai.program(_events_json([
        {"title": THREE_TITLES[0], "date_text": "01/07", "time_text": "20h"},
        {"title": THREE_TITLES[1], "date_text": "01/07", "time_text": "21h"},
        {"title": THREE_TITLES[2], "date_text": "01/07", "time_text": "22h"},
    ]))


@when("venue event extraction runs")
def step_when_venue_event_extraction_runs(context):
    _run_extraction(context)


@then("all three events are attributed to the posting venue")
def step_then_all_three_events_are_attributed_to_the_posting_venue(context):
    rows = _rows(context)
    assert len(rows) == 3, rows
    assert {r["venue_id"] for r in rows} == {context.ee_venue_id}, rows


@then("the events-per-post measurement records three for that venue post")
def step_then_the_events_per_post_measurement_records_three_for_venue(context):
    before = context.vpme_events_per_post_snapshot
    after = _snapshot_metric("event_extraction_events_per_post_sum")
    assert after - before == 3.0, (before, after)


# ── Scenario 3: independent date resolution ──────────────────────────────────
@given("a venue post announcing one event on a date and one on a later date")
def step_given_a_venue_post_announcing_one_event_on_a_date_and_one_later(context):
    _add_post(context, "vpme_two_dates", timestamp=_TS)
    _alias_shared_context(context, "vpme_two_dates")
    # The reused "both dates are resolved against the post timestamp" Then
    # (multi_event_posts_steps.py) reads context.mep_now as its anchor.
    context.mep_now = _TS
    context.ee_openai.program(_events_json([
        {"title": "Evento de Hoje Venue", "date_text": "hoje"},
        {"title": "Evento Futuro Venue", "date_text": "15/08"},
    ]))


# ── Scenario 4: single-event venue post, unchanged behaviour ────────────────
@given("a venue post whose flyer announces a single party")
def step_given_a_venue_post_whose_flyer_announces_a_single_party(context):
    _add_post(context, "vpme_single", timestamp=_TS)
    _alias_shared_context(context, "vpme_single")
    context.ee_openai.program(_events_json([
        {"title": "Festa Unica Venue", "date_text": "01/07", "time_text": "20h"},
    ]))


# ── Scenarios 5, 6, 10: a venue post that already produced three events ─────
@given("a venue post that already produced three events")
def step_given_a_venue_post_that_already_produced_three_events(context):
    step_given_a_venue_post_whose_flyer_announces_three_events(context)
    _run_extraction(context)
    rows = _rows(context)
    assert len(rows) == 3, rows
    context.vpme_event_ids_by_title = {r["title"]: r["event_id"] for r in rows}
    # The reused "the operator confirmed one of them with a corrected title"
    # Given (multi_event_posts_steps.py) reads context.mep_event_ids_by_title.
    context.mep_event_ids_by_title = context.vpme_event_ids_by_title


@when("venue event extraction runs again and returns them in a different order")
def step_when_venue_event_extraction_runs_again_reordered(context):
    context.ee_openai.program(_events_json([
        {"title": THREE_TITLES[2], "date_text": "01/07", "time_text": "22h"},
        {"title": THREE_TITLES[0], "date_text": "01/07", "time_text": "20h"},
        {"title": THREE_TITLES[1], "date_text": "01/07", "time_text": "21h"},
    ]))
    _run_extraction(context)


@then("no duplicate venue event is created")
def step_then_no_duplicate_venue_event_is_created(context):
    current_ids = {r["event_id"] for r in _rows(context)}
    assert current_ids == set(context.vpme_event_ids_by_title.values()), (
        current_ids, context.vpme_event_ids_by_title,
    )


@then("the confirmed venue event's corrected title is unchanged")
def step_then_the_confirmed_venue_events_corrected_title_is_unchanged(context):
    row = context.ee_dao.get_event(context.mep_confirmed_event_id)
    assert row is not None
    assert row["title"] == context.mep_confirmed_title, row


@when("venue event extraction runs again and returns only two of them")
def step_when_venue_event_extraction_runs_again_only_two(context):
    context.ee_openai.program(_events_json([
        {"title": THREE_TITLES[0], "date_text": "01/07", "time_text": "20h"},
        {"title": THREE_TITLES[1], "date_text": "01/07", "time_text": "21h"},
    ]))
    _run_extraction(context)


@then('the missing venue event has the status "{status}"')
def step_then_the_missing_venue_event_has_the_status(context, status):
    missing_id = context.vpme_event_ids_by_title[THREE_TITLES[2]]
    row = context.ee_dao.get_event(missing_id)
    assert row is not None
    assert row["status"] == status, row


@then("the missing venue event is not deleted")
def step_then_the_missing_venue_event_is_not_deleted(context):
    missing_id = context.vpme_event_ids_by_title[THREE_TITLES[2]]
    assert context.ee_dao.get_event(missing_id) is not None


# ── Scenarios 7, 8: confirmed venue event divergence ────────────────────────
@given("a confirmed venue event")
def step_given_a_confirmed_venue_event(context):
    _add_post(context, "vpme_confirmed_single", timestamp=_TS)
    context.ee_openai.program(_events_json([
        {"title": "Festa Confirmada Venue", "date_text": "01/07", "time_text": "20h"},
    ]))
    _run_extraction(context)
    row = _row_by_title(context, "Festa Confirmada Venue")
    context.vpme_confirmed_event_id = row["event_id"]
    context.ee_dao.update_event(row["event_id"], {"status": "confirmed"})
    context.vpme_confirmed_lookup = lambda: context.ee_dao.get_event(
        context.vpme_confirmed_event_id
    )


@when("venue event extraction runs again and returns a different title")
def step_when_venue_event_extraction_runs_again_different_title(context):
    context.ee_openai.program(_events_json([
        {"title": "Festa Totalmente Diferente Venue", "date_text": "01/07", "time_text": "20h"},
    ]))
    _run_extraction(context)


@when("venue event extraction runs again and returns a different date")
def step_when_venue_event_extraction_runs_again_different_date(context):
    context.ee_openai.program(_events_json([
        {"title": "Festa Confirmada Venue", "date_text": "15/08", "time_text": "20h"},
    ]))
    _run_extraction(context)


@then("the confirmed venue event is flagged as diverging from the model")
def step_then_the_confirmed_venue_event_is_flagged_as_diverging(context):
    row = context.ee_dao.get_event(context.vpme_confirmed_event_id)
    assert row is not None
    assert row["review_reason"] == "model_diverges_from_confirmed_record", row


@then("its status is still confirmed")
def step_then_its_status_is_still_confirmed(context):
    row = context.vpme_confirmed_lookup()
    assert row is not None
    assert row["status"] == "confirmed", row


# ── Scenario 9: the promoter path gains divergence flagging too ────────────
@given("a confirmed promoter event")
def step_given_a_confirmed_promoter_event(context):
    _reset_mep_context(context)
    context.mep_dao.upsert_promoter_account(context.mep_handle, {"status": "active"})
    _mep_seed_post(context, caption=GENERIC_EVENT_CAPTION)
    context.mep_openai.program(_events_json([{"title": "Festa Confirmada Promoter"}]))
    _run_mep_crawl(context)
    row = _mep_row_by_title(context, "Festa Confirmada Promoter")
    context.vpme_promoter_confirmed_event_id = row["event_id"]
    context.mep_dao.update_event(row["event_id"], {"status": "confirmed"})
    context.vpme_confirmed_lookup = lambda: context.mep_dao.get_event(
        context.vpme_promoter_confirmed_event_id
    )


@when("the multi-event extraction runs again and returns a different title")
def step_when_the_multi_event_extraction_runs_again_different_title(context):
    context.mep_openai.program(_events_json([{"title": "Festa Totalmente Diferente Promoter"}]))
    _run_mep_crawl(context)


@then("the confirmed promoter event is flagged as diverging from the model")
def step_then_the_confirmed_promoter_event_is_flagged_as_diverging(context):
    row = context.mep_dao.get_event(context.vpme_promoter_confirmed_event_id)
    assert row is not None
    assert row["review_reason"] == "model_diverges_from_confirmed_record", row


# ── Scenarios 11, 12: never supersede confirmed/manual on disappearance ─────
@given("a venue post that already produced a confirmed event")
def step_given_a_venue_post_that_already_produced_a_confirmed_event(context):
    _add_post(context, "vpme_confirmed_disappear", timestamp=_TS)
    context.ee_openai.program(_events_json([
        {"title": "Festa Confirmada Sumiu", "date_text": "01/07", "time_text": "20h"},
    ]))
    _run_extraction(context)
    row = _row_by_title(context, "Festa Confirmada Sumiu")
    context.vpme_confirmed_disappear_event_id = row["event_id"]
    context.ee_dao.update_event(row["event_id"], {"status": "confirmed"})


@given("a venue post that already produced a manually linked event")
def step_given_a_venue_post_that_already_produced_a_manually_linked_event(context):
    manual_venue_id = "vpme_manual_other_venue"
    context.ee_dao.upsert_venue(Venue(
        venue_id=manual_venue_id, venue_name="Manually Chosen Other Venue",
        venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG,
    ))
    _add_post(context, "vpme_manual_disappear", timestamp=_TS)
    context.ee_openai.program(_events_json([
        {"title": "Festa Vinculada Manualmente Venue", "date_text": "01/07", "time_text": "20h"},
    ]))
    _run_extraction(context)
    row = _row_by_title(context, "Festa Vinculada Manualmente Venue")
    context.vpme_manual_event_id = row["event_id"]
    context.vpme_manual_venue_id = manual_venue_id
    context.ee_dao.update_event(row["event_id"], {
        "venue_id": manual_venue_id, "location_resolution": "manual",
        "linked_by": "operator_x", "linked_at": _TS,
    })


@when("venue event extraction runs again and no longer returns that event")
def step_when_venue_event_extraction_runs_again_no_longer_returns_that_event(context):
    context.ee_openai.program(_events_json([]))
    _run_extraction(context)


@then("the confirmed venue event keeps its status")
def step_then_the_confirmed_venue_event_keeps_its_status(context):
    row = context.ee_dao.get_event(context.vpme_confirmed_disappear_event_id)
    assert row is not None
    assert row["status"] == "confirmed", row


@then("that venue event keeps its manual link")
def step_then_that_venue_event_keeps_its_manual_link(context):
    row = context.ee_dao.get_event(context.vpme_manual_event_id)
    assert row is not None
    assert row["location_resolution"] == "manual", row
    assert row["venue_id"] == context.vpme_manual_venue_id, row


# ── Scenario 13: truncated response persists nothing ────────────────────────
@given("a venue post whose extraction response is cut off mid-list")
def step_given_a_venue_post_whose_extraction_response_is_cut_off(context):
    _add_post(context, "vpme_truncated", timestamp=_TS)
    context.vpme_truncated_snapshot = _snapshot_metric(
        "event_extraction_posts_total", {"outcome": "truncated"},
    )
    context.ee_openai.program_truncated(
        '{"events": [{"title": "Festa Cortada Venue", "date_text": "01/07'
    )


@then('the post is counted with the venue outcome "{outcome}"')
def step_then_the_post_is_counted_with_the_venue_outcome(context, outcome):
    before = context.vpme_truncated_snapshot
    after = _snapshot_metric("event_extraction_posts_total", {"outcome": outcome})
    assert after - before == 1.0, (before, after)


@then("no venue event is persisted for that post")
def step_then_no_venue_event_is_persisted_for_that_post(context):
    assert _rows(context) == []


# ── Scenario 14: one malformed event, siblings survive ──────────────────────
@given("a venue post whose extraction returns three events and one is malformed")
def step_given_a_venue_post_whose_extraction_returns_three_events_one_malformed(context):
    _add_post(context, "vpme_malformed", timestamp=_TS)
    _alias_shared_context(context, "vpme_malformed")
    context.vpme_malformed_snapshot = _snapshot_metric("event_extraction_malformed_events_total")
    base = {
        "description": None, "date_text": "01/07", "time_text": "20h",
        "is_recurring": False, "recurrence_text": None, "lineup": [],
        "ticket_url": None, "price_text": None, "location_text": None,
        "confidence": 0.9,
    }
    raw = json.dumps({"events": [
        {**base, "title": "Valid Venue Event A"},
        "this is not an event object",
        {**base, "title": "Valid Venue Event B"},
    ]})
    context.ee_openai.program(raw)


@then("the malformed venue event is counted")
def step_then_the_malformed_venue_event_is_counted(context):
    before = context.vpme_malformed_snapshot
    after = _snapshot_metric("event_extraction_malformed_events_total")
    assert after - before == 1.0, (before, after)


# ── Scenario 16: location recorded, never re-attributed ─────────────────────
@given("a venue post whose flyer names a location")
def step_given_a_venue_post_whose_flyer_names_a_location(context):
    _add_post(context, "vpme_location", timestamp=_TS)
    context.ee_openai.program(_events_json([
        {"title": "Festa Com Local Venue", "date_text": "01/07", "time_text": "20h",
         "location_text": "Praça do Arsenal"},
    ]))


@then("the event records that location text")
def step_then_the_event_records_that_location_text(context):
    row = _row_by_title(context, "Festa Com Local Venue")
    assert row["location_text"] == "Praça do Arsenal", row


@then("the event is still attributed to the posting venue")
def step_then_the_event_is_still_attributed_to_the_posting_venue(context):
    row = _row_by_title(context, "Festa Com Local Venue")
    assert row["venue_id"] == context.ee_venue_id, row
