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

Several of this feature's phrases would otherwise be VERBATIM identical to
existing Then/Given text in multi_event_posts_steps.py ("three events are
persisted for that post", "each event carries its own title", "the two
events carry different start dates", "both dates are resolved against the
post timestamp", "two events are persisted for that post", and the Given
"the operator confirmed one of them with a corrected title") — an
AmbiguousStep collision (same step TYPE, same literal text) if redefined.
Rather than dodge that by loosening the PROMOTER file's own assertions
(which protect real captured @recifequecabenobolso evidence and must stay
exactly as strong as before) or by aliasing this file's fixtures onto that
one's generic context variables, every such line here is reworded with its
own "venue event(s)" phrasing — extending the SAME distinguishing-wording
convention the rest of this feature already uses ("venue event extraction
runs", "no duplicate venue event", "the confirmed venue event's...") — and
given its OWN step implementation below, asserting the SAME exact,
non-generic expectations (specific titles, specific dates) the equivalent
promoter steps do.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.models.venue import Venue
from app.services.event_extraction_service import KIND_LABEL_NOT_APPLICABLE
from tests.bdd.steps.instagram_event_extraction_steps import (
    RECIFE_LAT,
    RECIFE_LNG,
    _add_post,
    _run_extraction,
    _run_reextraction,
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


@then("three venue events are persisted for that post")
def step_then_three_venue_events_are_persisted_for_that_post(context):
    assert len(_rows(context)) == 3, _rows(context)


@then("each venue event carries its own title")
def step_then_each_venue_event_carries_its_own_title(context):
    titles = {r["title"] for r in _rows(context)}
    assert titles == set(THREE_TITLES), titles


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
    context.ee_openai.program(_events_json([
        {"title": "Evento de Hoje Venue", "date_text": "hoje"},
        {"title": "Evento Futuro Venue", "date_text": "15/08"},
    ]))


@then("the two venue events carry different start dates")
def step_then_the_two_venue_events_carry_different_start_dates(context):
    today_row = _row_by_title(context, "Evento de Hoje Venue")
    later_row = _row_by_title(context, "Evento Futuro Venue")
    assert today_row["starts_at"] is not None and later_row["starts_at"] is not None
    assert today_row["starts_at"] != later_row["starts_at"]


@then("both venue event dates are resolved against the post timestamp")
def step_then_both_venue_event_dates_are_resolved_against_the_post_timestamp(context):
    today_row = _row_by_title(context, "Evento de Hoje Venue")
    later_row = _row_by_title(context, "Evento Futuro Venue")
    # The post's own timestamp is 2026-07-01: "hoje" anchors there, "15/08"
    # forward-fills to the next occurrence at or after it.
    assert today_row["starts_at"].date().isoformat() == "2026-07-01"
    assert later_row["starts_at"].date().isoformat() == "2026-08-15"


# ── Scenario 4: single-event venue post, unchanged behaviour ────────────────
@given("a venue post whose flyer announces a single party")
def step_given_a_venue_post_whose_flyer_announces_a_single_party(context):
    _add_post(context, "vpme_single", timestamp=_TS)
    context.ee_openai.program(_events_json([
        {"title": "Festa Unica Venue", "date_text": "01/07", "time_text": "20h"},
    ]))


@then("exactly one venue event is persisted for that post")
def step_then_exactly_one_venue_event_is_persisted_for_that_post(context):
    assert len(_rows(context)) == 1, _rows(context)


# ── Scenarios 5, 6, 10: a venue post that already produced three events ─────
@given("a venue post that already produced three events")
def step_given_a_venue_post_that_already_produced_three_events(context):
    step_given_a_venue_post_whose_flyer_announces_three_events(context)
    _run_extraction(context)
    rows = _rows(context)
    assert len(rows) == 3, rows
    context.vpme_event_ids_by_title = {r["title"]: r["event_id"] for r in rows}


@when("venue event extraction runs again and returns them in a different order")
def step_when_venue_event_extraction_runs_again_reordered(context):
    # plans/260826_skip-already-extracted-posts.md: the scheduled path now
    # skips an already-successfully-extracted post, so every "runs again"
    # step in this file goes through the deliberate re-extraction path
    # (`_run_reextraction`, mode="handles") to still force a genuine second
    # model call — see that helper's own docstring for why this is
    # behaviour-preserving for this fixture (single-venue handle).
    context.ee_openai.program(_events_json([
        {"title": THREE_TITLES[2], "date_text": "01/07", "time_text": "22h"},
        {"title": THREE_TITLES[0], "date_text": "01/07", "time_text": "20h"},
        {"title": THREE_TITLES[1], "date_text": "01/07", "time_text": "21h"},
    ]))
    _run_reextraction(context)


@then("three venue events exist for that post")
def step_then_three_venue_events_exist_for_that_post(context):
    assert len(_rows(context)) == 3, _rows(context)


@then("no duplicate venue event is created")
def step_then_no_duplicate_venue_event_is_created(context):
    current_ids = {r["event_id"] for r in _rows(context)}
    assert current_ids == set(context.vpme_event_ids_by_title.values()), (
        current_ids, context.vpme_event_ids_by_title,
    )


@given("the operator confirmed one of the venue events with a corrected title")
def step_given_the_operator_confirmed_one_of_the_venue_events_with_a_corrected_title(context):
    # Any one of the three — WHICH one is irrelevant to this scenario's
    # guarantee (a confirmed row's operator-corrected title survives
    # whichever of them the next extraction reorders). Picked
    # deterministically for a stable, repeatable assertion.
    event_id = context.vpme_event_ids_by_title[THREE_TITLES[1]]
    context.vpme_confirmed_event_id = event_id
    context.vpme_confirmed_title = f"{THREE_TITLES[1]} - CORRIGIDO"
    context.ee_dao.update_event(event_id, {
        "status": "confirmed", "title": context.vpme_confirmed_title,
    })


@then("the confirmed venue event's corrected title is unchanged")
def step_then_the_confirmed_venue_events_corrected_title_is_unchanged(context):
    row = context.ee_dao.get_event(context.vpme_confirmed_event_id)
    assert row is not None
    assert row["title"] == context.vpme_confirmed_title, row


@when("venue event extraction runs again and returns only two of them")
def step_when_venue_event_extraction_runs_again_only_two(context):
    # See the "runs again ... different order" step above for why this is
    # `_run_reextraction`, not `_run_extraction`.
    context.ee_openai.program(_events_json([
        {"title": THREE_TITLES[0], "date_text": "01/07", "time_text": "20h"},
        {"title": THREE_TITLES[1], "date_text": "01/07", "time_text": "21h"},
    ]))
    _run_reextraction(context)


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
    # See the "runs again ... different order" step above for why this is
    # `_run_reextraction`, not `_run_extraction`.
    context.ee_openai.program(_events_json([
        {"title": "Festa Totalmente Diferente Venue", "date_text": "01/07", "time_text": "20h"},
    ]))
    _run_reextraction(context)


@when("venue event extraction runs again and returns a different date")
def step_when_venue_event_extraction_runs_again_different_date(context):
    context.ee_openai.program(_events_json([
        {"title": "Festa Confirmada Venue", "date_text": "15/08", "time_text": "20h"},
    ]))
    _run_reextraction(context)


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
    # A real, resolvable date, held constant across both extractions — this
    # scenario is about the TITLE diverging while the model still agrees on
    # WHEN (same normalized title XOR same date is what lets the shared
    # reconciliation still recognize this as the same event; two dateless
    # answers would never count as "the same date" — see
    # app.services.event_reconciliation._plausibly_same_event).
    context.mep_openai.program(_events_json([
        {"title": "Festa Confirmada Promoter", "date_text": "10/08"},
    ]))
    _run_mep_crawl(context)
    row = _mep_row_by_title(context, "Festa Confirmada Promoter")
    context.vpme_promoter_confirmed_event_id = row["event_id"]
    context.mep_dao.update_event(row["event_id"], {"status": "confirmed"})
    context.vpme_confirmed_lookup = lambda: context.mep_dao.get_event(
        context.vpme_promoter_confirmed_event_id
    )


@when("the multi-event extraction runs again and returns a different title")
def step_when_the_multi_event_extraction_runs_again_different_title(context):
    context.mep_openai.program(_events_json([
        {"title": "Festa Totalmente Diferente Promoter", "date_text": "10/08"},
    ]))
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
    # See the "runs again ... different order" step above for why this is
    # `_run_reextraction`, not `_run_extraction`.
    context.ee_openai.program(_events_json([]))
    _run_reextraction(context)


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
    # plans/260810_post-kind-and-post-extraction-attribution.md §Error
    # Handling: event_extraction_posts_total gained a required `kind` label
    # — a truncated post never got far enough to read one, so its kind is
    # KIND_LABEL_NOT_APPLICABLE (see event_extraction_service.py).
    context.vpme_truncated_snapshot = _snapshot_metric(
        "event_extraction_posts_total",
        {"outcome": "truncated", "kind": KIND_LABEL_NOT_APPLICABLE},
    )
    context.ee_openai.program_truncated(
        '{"events": [{"title": "Festa Cortada Venue", "date_text": "01/07'
    )


@then('the post is counted with the venue outcome "{outcome}"')
def step_then_the_post_is_counted_with_the_venue_outcome(context, outcome):
    before = context.vpme_truncated_snapshot
    after = _snapshot_metric(
        "event_extraction_posts_total",
        {"outcome": outcome, "kind": KIND_LABEL_NOT_APPLICABLE},
    )
    assert after - before == 1.0, (before, after)


@then("no venue event is persisted for that post")
def step_then_no_venue_event_is_persisted_for_that_post(context):
    assert _rows(context) == []


# ── Scenario 14: one malformed event, siblings survive ──────────────────────
@given("a venue post whose extraction returns three events and one is malformed")
def step_given_a_venue_post_whose_extraction_returns_three_events_one_malformed(context):
    _add_post(context, "vpme_malformed", timestamp=_TS)
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


@then("two venue events are persisted for that post")
def step_then_two_venue_events_are_persisted_for_that_post(context):
    assert len(_rows(context)) == 2, _rows(context)


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
