"""Behave steps for tests/bdd/enrichment/auto-accept-and-field-protection.feature.

See plans/260807_auto-accept-and-field-level-protection.md. Drives the REAL
`app.services.event_reconciliation.reconcile_post_events` DIRECTLY — not
through EventExtractionService/PromoterCrawlService — because this feature's
scope is the shared reconciliation module's clean-predicate and field-level
protection rules; venue/date resolution and the OpenAI client are orthogonal
to what is under test here. Reuses the SAME `context.ee_dao`/`context.
ee_handle`/`context.ee_last_shortcode` contract instagram_event_extraction_
steps.py already established (`_reset_context`, `_stored_event`,
`_build_admin_events_app`) so this file's own Given/When steps can feed the
Then step ALREADY registered there — `the event has the status "{status}"` —
without redefining it: a second identical decorator in this module would be
an AmbiguousStep collision even though the underlying fixtures differ.

One step text ("an event an operator linked manually") would otherwise be
VERBATIM identical to an existing Given in instagram_promoter_events_steps.py
(a completely different fixture, `context.pe_*`) — an AmbiguousStep
collision. Reworded in the feature file to "an event an operator manually
linked to a venue", the same distinguishing-wording convention
one_event_many_posts_steps.py's docstring already documents for this exact
situation.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.models.venue import Venue
from app.services.event_reconciliation import reconcile_post_events
from tests.bdd.steps.instagram_event_extraction_steps import (
    _build_admin_events_app,
    _reset_context,
    _stored_event,
)

_VENUE_ID = "venue_aa"
_HANDLE = "aa_handle"
_MIN_CONFIDENCE = 0.5
RECIFE_LAT, RECIFE_LNG = -8.05, -34.88


def _default_prepared() -> dict:
    return {
        "starts_at": datetime(2026, 8, 20, 22, 0, tzinfo=timezone.utc),
        "ends_at": None,
        "is_recurring": False,
        "recurrence_text": None,
        "title": "Clean Party",
        "description": None,
        "lineup": [],
        "ticket_url": None,
        "price_text": "R$30",
        "location_text": None,
        "cover_photo_key": None,
        "confidence": 0.9,
        "review_reason": None,
        "raw_extraction": {"time_known": True},
    }


def _seed_dao(context) -> None:
    _reset_context(context)
    context.ee_handle = _HANDLE
    context.ee_dao.upsert_venue(Venue(
        venue_id=_VENUE_ID, venue_name="AA Venue", venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG,
    ))


def _persist(context, shortcode: str, prepared: dict, *, venue_id) -> None:
    def _attribute(fields, event_id):
        return ({"venue_id": venue_id} if venue_id is not None else {}), None

    context.ee_last_shortcode = shortcode
    reconcile_post_events(
        venue_dao=context.ee_dao, source_kind="venue_post",
        source_handle=context.ee_handle, source_shortcode=shortcode,
        source_permalink=None, prepared_events=[prepared],
        now=datetime.now(timezone.utc), attribute=_attribute,
        min_confidence=_MIN_CONFIDENCE,
    )


def _make_accepted_event(context, shortcode: str = "aa_accepted_seed") -> None:
    _seed_dao(context)
    prepared = _default_prepared()
    context.aa_prepared = prepared
    context.aa_venue_id = _VENUE_ID
    context.aa_shortcode = shortcode
    _persist(context, shortcode, prepared, venue_id=_VENUE_ID)
    context.aa_event_id = _stored_event(context)["event_id"]
    # Belt-and-suspenders: every scenario reusing "an accepted event" as a
    # precondition depends on this actually being status=accepted, not
    # merely "whatever a clean extraction happens to produce today" — assert
    # it here so a broken predicate fails LOUDLY at the Given step rather
    # than silently invalidating whatever the scenario's own Then checks.
    assert _stored_event(context)["status"] == "accepted", _stored_event(context)


def _make_confirmed_event(
    context, shortcode: str, *, patch_fields: dict, prepared_overrides: dict = None,
) -> None:
    _seed_dao(context)
    prepared = _default_prepared()
    prepared.update(prepared_overrides or {})
    context.aa_prepared = prepared
    context.aa_venue_id = _VENUE_ID
    context.aa_shortcode = shortcode
    _persist(context, shortcode, prepared, venue_id=_VENUE_ID)
    context.aa_event_id = _stored_event(context)["event_id"]
    _build_admin_events_app(context)
    resp = context.ee_client.patch(f"/admin/events/{context.aa_event_id}", json=patch_fields)
    assert resp.status_code == 200, resp.text
    resp = context.ee_client.post(f"/admin/events/{context.aa_event_id}/confirm")
    assert resp.status_code == 200, resp.text


# ── Scenario: Accept an extraction with nothing wrong ────────────────────────
@given("an extraction with no review reason, a resolved date, a linked venue")
def step_given_extraction_no_review_reason_resolved_date_linked_venue(context):
    _seed_dao(context)
    context.aa_prepared = _default_prepared()
    context.aa_venue_id = _VENUE_ID
    context.aa_shortcode = "aa_clean"


@given("confidence above the floor")
def step_given_confidence_above_the_floor(context):
    context.aa_prepared["confidence"] = 0.9


@when("the event is persisted")
def step_when_the_event_is_persisted(context):
    _persist(context, context.aa_shortcode, context.aa_prepared, venue_id=context.aa_venue_id)


@then("the event is not in the review queue")
def step_then_event_not_in_review_queue(context):
    ids = {e["event_id"] for e in context.ee_dao.list_events_awaiting_decision()}
    assert _stored_event(context)["event_id"] not in ids


# ── Scenario: Keep a flagged event awaiting a human ──────────────────────────
@given("an extraction carrying a review reason")
def step_given_extraction_carrying_a_review_reason(context):
    _seed_dao(context)
    context.aa_prepared = _default_prepared()
    context.aa_prepared["review_reason"] = "weekday_mismatch"
    context.aa_venue_id = _VENUE_ID
    context.aa_shortcode = "aa_flagged"


@then("the event is in the review queue")
def step_then_event_in_review_queue(context):
    ids = {e["event_id"] for e in context.ee_dao.list_events_awaiting_decision()}
    assert _stored_event(context)["event_id"] in ids


# ── Scenario: Keep an event with no resolved date awaiting a human ──────────
@given("an extraction with no start date")
def step_given_extraction_with_no_start_date(context):
    _seed_dao(context)
    context.aa_prepared = _default_prepared()
    context.aa_prepared["starts_at"] = None
    context.aa_venue_id = _VENUE_ID
    context.aa_shortcode = "aa_nodate"


# ── Scenario: Keep an event with no venue awaiting a human ──────────────────
@given("an extraction with no venue")
def step_given_extraction_with_no_venue(context):
    _seed_dao(context)
    context.aa_prepared = _default_prepared()
    context.aa_venue_id = None
    context.aa_shortcode = "aa_novenue"


# ── Scenario: Keep a low-confidence event awaiting a human ──────────────────
@given("an extraction below the confidence floor")
def step_given_extraction_below_confidence_floor(context):
    _seed_dao(context)
    context.aa_prepared = _default_prepared()
    context.aa_prepared["confidence"] = 0.1
    context.aa_venue_id = _VENUE_ID
    context.aa_shortcode = "aa_lowconf"


# ── Scenario: Record only the fields an operator patched ────────────────────
@given("an accepted event")
def step_given_an_accepted_event(context):
    _make_accepted_event(context)


@when("the operator patches only its title")
def step_when_operator_patches_only_its_title(context):
    _build_admin_events_app(context)
    resp = context.ee_client.patch(
        f"/admin/events/{context.aa_event_id}", json={"title": "Operator's Title"},
    )
    assert resp.status_code == 200, resp.text


@then("the operator-edited fields record the title")
def step_then_operator_edited_fields_record_the_title(context):
    row = context.ee_dao.get_event(context.aa_event_id)
    assert row.get("operator_edited_fields") == ["title"], row


@then("they do not record any other field")
def step_then_no_other_field_recorded(context):
    row = context.ee_dao.get_event(context.aa_event_id)
    assert row.get("operator_edited_fields") == ["title"], row


# ── Scenario: Accumulate edited fields across successive patches ────────────
@given("an accepted event whose title the operator already patched")
def step_given_accepted_event_title_already_patched(context):
    _make_accepted_event(context, "aa_accepted_accum")
    _build_admin_events_app(context)
    resp = context.ee_client.patch(
        f"/admin/events/{context.aa_event_id}", json={"title": "First Correction"},
    )
    assert resp.status_code == 200, resp.text


@when("the operator patches only its price")
def step_when_operator_patches_only_its_price(context):
    resp = context.ee_client.patch(
        f"/admin/events/{context.aa_event_id}", json={"price_text": "R$50"},
    )
    assert resp.status_code == 200, resp.text


@then("the operator-edited fields record both the title and the price")
def step_then_operator_edited_fields_record_both(context):
    row = context.ee_dao.get_event(context.aa_event_id)
    assert sorted(row.get("operator_edited_fields") or []) == ["price_text", "title"], row


# ── Scenario: Update a field the operator never edited ──────────────────────
@given("an event whose title the operator patched")
def step_given_event_title_operator_patched(context):
    _make_confirmed_event(
        context, "aa_title_patch_seed", patch_fields={"title": "Operator's Title"},
    )


@given("a later post stating a different price")
def step_given_later_post_different_price(context):
    context.aa_new_prepared = dict(context.aa_prepared)
    context.aa_new_prepared["title"] = context.aa_prepared["title"]
    context.aa_new_prepared["price_text"] = "R$999"


@when("the event is re-extracted")
def step_when_event_is_re_extracted(context):
    _persist(context, context.aa_shortcode, context.aa_new_prepared, venue_id=context.aa_venue_id)


@then("the price is updated from the later post")
def step_then_price_updated_from_later_post(context):
    row = _stored_event(context)
    assert row["price_text"] == "R$999", row


# ── Scenario: Keep an operator-edited field when a later post contradicts it ─
@given("a later post stating a different title")
def step_given_later_post_different_title(context):
    context.aa_new_prepared = dict(context.aa_prepared)
    context.aa_new_prepared["title"] = "A Totally Different Title"


@then("the operator's title is unchanged")
def step_then_operators_title_unchanged(context):
    row = _stored_event(context)
    assert row["title"] == "Operator's Title", row


@then("the event is flagged as diverging from the operator's record")
def step_then_flagged_diverging_from_operators_record(context):
    row = _stored_event(context)
    assert row["review_reason"] == "model_diverges_from_confirmed_record", row


# ── Scenario: Never replace a known value with a null ────────────────────────
@given("an event carrying a price")
def step_given_event_carrying_a_price(context):
    _make_confirmed_event(
        context, "aa_price_seed", patch_fields={"title": "Operator's Title"},
    )


@given("a later post stating no price")
def step_given_later_post_no_price(context):
    context.aa_new_prepared = dict(context.aa_prepared)
    context.aa_new_prepared["title"] = context.aa_prepared["title"]
    context.aa_new_prepared["price_text"] = None


@then("the event still carries its price")
def step_then_event_still_carries_its_price(context):
    row = _stored_event(context)
    assert row["price_text"] == "R$30", row


# ── Scenario: Union the lineup even on an operator-edited event ─────────────
@given("an event whose title the operator patched and which lists two performers")
def step_given_event_title_patched_two_performers(context):
    _make_confirmed_event(
        context, "aa_lineup_seed", patch_fields={"title": "Operator's Title"},
        prepared_overrides={"lineup": ["DJ A", "DJ B"]},
    )


@given("a later post naming a third performer")
def step_given_later_post_third_performer(context):
    context.aa_new_prepared = dict(context.aa_prepared)
    context.aa_new_prepared["title"] = context.aa_prepared["title"]
    context.aa_new_prepared["lineup"] = ["DJ A", "DJ B", "DJ C"]


@then("the event lists all three performers")
def step_then_event_lists_three_performers(context):
    row = _stored_event(context)
    assert set(row["lineup"]) == {"DJ A", "DJ B", "DJ C"}, row


# ── Scenario: Supersede an auto-accepted event a later run no longer finds ──
@when("a later run no longer returns it")
def step_when_later_run_no_longer_returns_it(context):
    reconcile_post_events(
        venue_dao=context.ee_dao, source_kind="venue_post",
        source_handle=context.ee_handle, source_shortcode=context.aa_shortcode,
        source_permalink=None, prepared_events=[],
        now=datetime.now(timezone.utc),
        attribute=lambda fields, event_id: ({}, None),
        min_confidence=_MIN_CONFIDENCE,
    )


# ── Scenario: Never supersede a human-confirmed event that disappears ───────
@given("a human-confirmed event")
def step_given_human_confirmed_event(context):
    _seed_dao(context)
    prepared = _default_prepared()
    context.aa_prepared = prepared
    context.aa_venue_id = _VENUE_ID
    context.aa_shortcode = "aa_human_confirmed"
    _persist(context, context.aa_shortcode, prepared, venue_id=_VENUE_ID)
    context.aa_event_id = _stored_event(context)["event_id"]
    _build_admin_events_app(context)
    resp = context.ee_client.post(f"/admin/events/{context.aa_event_id}/confirm")
    assert resp.status_code == 200, resp.text


@then('the event keeps the status "confirmed"')
def step_then_event_keeps_status_confirmed(context):
    row = _stored_event(context)
    assert row["status"] == "confirmed", row


# ── Scenario: Never supersede a manually linked event that disappears ───────
@given("an event an operator manually linked to a venue")
def step_given_event_operator_manually_linked_to_a_venue(context):
    _seed_dao(context)
    prepared = _default_prepared()
    context.aa_prepared = prepared
    context.aa_shortcode = "aa_manual_link"
    _persist(context, context.aa_shortcode, prepared, venue_id=None)
    context.aa_event_id = _stored_event(context)["event_id"]
    context.ee_dao.update_event(context.aa_event_id, {
        "venue_id": _VENUE_ID, "location_resolution": "manual",
        "linked_by": "operator", "linked_at": datetime.now(timezone.utc),
    })


@then("the event keeps its manual link")
def step_then_event_keeps_manual_link(context):
    row = _stored_event(context)
    assert row["location_resolution"] == "manual", row
    assert row["venue_id"] == _VENUE_ID, row


# ── Scenario: Protect every field of a legacy confirmed row ─────────────────
@given("a confirmed event with no record of which fields were edited")
def step_given_legacy_confirmed_event(context):
    _seed_dao(context)
    prepared = _default_prepared()
    prepared["title"] = "Legacy Title"
    prepared["price_text"] = "R$30"
    context.aa_prepared = prepared
    context.aa_venue_id = _VENUE_ID
    context.aa_shortcode = "aa_legacy"
    _persist(context, context.aa_shortcode, prepared, venue_id=_VENUE_ID)
    context.aa_event_id = _stored_event(context)["event_id"]
    _build_admin_events_app(context)
    resp = context.ee_client.post(f"/admin/events/{context.aa_event_id}/confirm")
    assert resp.status_code == 200, resp.text
    # Never PATCHed — operator_edited_fields stays NULL, standing in for a
    # row confirmed before this column existed (or before the operator ever
    # touched a field), where which fields were edited is genuinely unknown.


@given("a later post stating a different title and a different price")
def step_given_later_post_diff_title_and_price(context):
    context.aa_new_prepared = dict(context.aa_prepared)
    context.aa_new_prepared["title"] = "Completely New Title"
    context.aa_new_prepared["price_text"] = "R$999"


@then("neither the title nor the price is changed")
def step_then_neither_title_nor_price_changed(context):
    row = _stored_event(context)
    assert row["title"] == "Legacy Title", row
    assert row["price_text"] == "R$30", row
