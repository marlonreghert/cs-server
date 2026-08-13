"""Behave steps for
tests/bdd/enrichment/review-gate-and-date-vocabulary.feature.

See plans/260813_review-gate-and-date-vocabulary.md.

§A (relative-date vocabulary) and most of §B (cadence vocabulary) need NO
steps of their own at all: "a post published on {date_str}", "an extracted
event whose date text is ...", "the date is resolved" and "the event starts
on {date_str}" are already registered by event_attribution_and_dates_steps.py
and are reused verbatim (this feature's plan is what motivated extending that
module's shared "the date is resolved" step to also thread
is_recurring/recurrence_text through — see its own docstring). Redefining any
of those texts here would be an AmbiguousStep collision.

§B's "no stored event reports both a recurrence and a missing date" scenario
reuses auto_accept_and_field_protection_steps.py's OWN "When the event is
persisted" step (registered there, over ITS OWN `context.aa_*` fixture) —
again, never redefined here. That step persists through
`app.services.event_reconciliation.reconcile_post_events` DIRECTLY, with no
date resolution of its own, so THIS module's Given step resolves the date
itself, with the exact same pure function (`resolve_event_datetime`)
event_extraction_service.py calls, and builds the `aa_prepared` dict that
step reads.

§C (post-type gate) and §D (unread time) drive the REAL EventExtractionService
via `context.ee_*`, the SAME harness instagram_event_extraction_steps.py
already builds (`_add_post`, `_extraction_json`, `_run_extraction`,
`_stored_event`, `_build_admin_events_app`) — "When the post is extracted" is
ALREADY registered (event_ticket_info_and_attractions_steps.py) and reused
here unmodified, and "Then the event is not queued for review" is ALREADY
registered (instagram_post_recency_and_unknown_time_steps.py) and reused here
unmodified too.
"""
from __future__ import annotations

from behave import given, then, when  # type: ignore[import-untyped]

from app.models.event_kind import KIND_EVENT, KIND_MENU, KIND_OTHER, KIND_PROMOTION
from app.services.event_date_resolver import resolve_event_datetime
from tests.bdd.steps.auto_accept_and_field_protection_steps import (
    _default_prepared as _aa_default_prepared,
)
from tests.bdd.steps.auto_accept_and_field_protection_steps import (
    _seed_dao as _aa_seed_dao,
)
from tests.bdd.steps.auto_accept_and_field_protection_steps import _VENUE_ID as _AA_VENUE_ID
from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post,
    _build_admin_events_app,
    _extraction_json,
    _run_extraction,
    _stored_event,
)
from tests.bdd.steps.instagram_event_extraction_steps import (
    step_given_an_event_candidate_venue_with_an_instagram_handle as _seed_ee_venue,
)

# ── §B: cadences with no computable day ──────────────────────────────────────
@given('an extracted event the model marked recurring with recurrence text "{text}"')
def step_given_event_marked_recurring_with_recurrence_text(context, text):
    # The bare-resolver scenarios ("When the date is resolved", reused from
    # event_attribution_and_dates_steps.py) read these directly.
    context.eadb_date_text = None
    context.eadb_date_interpretation = None
    context.eadb_is_recurring = True
    context.eadb_recurrence_text = text

    # "No stored event reports both a recurrence and a missing date" instead
    # reuses auto_accept_and_field_protection_steps.py's "When the event is
    # persisted" — see this module's own docstring for why the date is
    # resolved right here rather than inside that step. For this fixture,
    # `review_reason=resolved.review_reason` IS the complete, correct
    # formula: no OTHER item in event_extraction_service.py's own reasons
    # list (year_inferred/date_range/unread_time/low_confidence) is ever
    # triggered by a bare recurrence claim with a fixed high confidence.
    _aa_seed_dao(context)
    resolved = resolve_event_datetime(
        date_text=None, time_text=None, post_timestamp=context.eadb_date_post_ts,
        is_recurring=True, recurrence_text=text,
    )
    prepared = _aa_default_prepared()
    prepared.update({
        "starts_at": resolved.starts_at,
        # Mirrors event_extraction_service.py's own
        # `resolved.is_recurring or bool(parsed["is_recurring"])` — the
        # model's raw claim (always True for this fixture) is what keeps a
        # stored row self-consistent even when the resolver itself could not
        # compute a weekday.
        "is_recurring": resolved.is_recurring or True,
        "recurrence_text": resolved.recurrence_text or text,
        "review_reason": resolved.review_reason,
        "raw_extraction": {"time_known": resolved.time_known},
    })
    context.aa_prepared = prepared
    context.aa_venue_id = _AA_VENUE_ID
    context.aa_shortcode = "rgv_recurring_persisted"


# Deliberately NOT phrased "an extracted event whose date text is ..." — that
# prefix already exists in event_attribution_and_dates_steps.py and behave's
# `{placeholder}` is greedy, so the two would be an AmbiguousStep.
@given('an extracted event stating date text "{date_text}" alongside the cadence '
       '"{recurrence_text}"')
def step_given_event_with_both_a_date_and_a_cadence(context, date_text, recurrence_text):
    """Both claims at once — the collision §B's daily form makes common: a
    venue with a standing happy hour posts a dated special, and the model
    reports the cadence AND the date. Only the bare-resolver scenarios
    ("When the date is resolved") read these."""
    context.eadb_date_text = date_text
    context.eadb_date_interpretation = None
    context.eadb_is_recurring = True
    context.eadb_recurrence_text = recurrence_text


@then("the event is recorded as recurring")
def step_then_event_recorded_as_recurring(context):
    assert context.eadb_resolved.is_recurring is True, context.eadb_resolved


@then('the event is not queued for review with reason "{reason}"')
def step_then_not_queued_with_reason(context, reason):
    # Mirrors event_attribution_and_dates_steps.py's own (positive)
    # `step_then_queued_with_reason` disambiguation between fixtures.
    resolved = getattr(context, "eadb_resolved", None)
    if resolved is not None:
        assert reason not in (resolved.review_reason or ""), resolved
        return
    row = _stored_event(context)
    assert reason not in (row.get("review_reason") or ""), row


@then("no stored event reports both a recurrence and a missing date")
def step_then_no_recurrence_and_missing_date(context):
    row = _stored_event(context)
    reason = row.get("review_reason") or ""
    if row.get("is_recurring"):
        assert "missing_date" not in reason, row


# ── §C: only an event is required to carry a date ────────────────────────────
_KIND_PHRASE_TO_KIND = {
    "a promotion": KIND_PROMOTION,
    "an event": KIND_EVENT,
    "a menu": KIND_MENU,
    "other": KIND_OTHER,
}


@given("a venue post typed as {kind_phrase} with no date anywhere in it")
def step_given_venue_post_typed_as_no_date(context, kind_phrase):
    kind = _KIND_PHRASE_TO_KIND[kind_phrase]
    _seed_ee_venue(context)
    _add_post(context, f"rgv_{kind}_no_date", caption="Confira, sem data marcada.")
    context.ee_openai.program(_extraction_json(
        kind=kind, title=f"Post {kind}",
        date_text=None, time_text=None, is_recurring=False, recurrence_text=None,
    ))


@then("the event is accepted")
def step_then_event_is_accepted(context):
    assert _stored_event(context).get("status") == "accepted", _stored_event(context)


@then("the stored review reason does not mention a missing date")
def step_then_stored_reason_no_missing_date(context):
    row = _stored_event(context)
    reason = row.get("review_reason") or ""
    assert "missing_date" not in reason, row


@then("the item's status is not decided by its start date")
def step_then_status_not_decided_by_start_date(context):
    row = _stored_event(context)
    assert row.get("post_type") == KIND_MENU, row
    assert row.get("starts_at") is None, row
    assert row.get("status") == "accepted", row


@then("the menu still reports whether it is current from when it was last seen")
def step_then_menu_reports_current_from_last_seen(context):
    _build_admin_events_app(context)
    row = _stored_event(context)
    resp = context.ee_client.get(f"/admin/events/{row['event_id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("is_current") is True, body


# ── §D: an unread clock time is worth counting, not queueing ────────────────
@given("a post whose flyer was classified as naming a time")
def step_given_post_flyer_classified_as_naming_a_time(context):
    _seed_ee_venue(context)
    _add_post(context, "rgv_unread_time", flyer_names_time="yes")


@given("an extracted event with a resolvable date and no time text")
def step_given_extracted_event_resolvable_date_no_time_text(context):
    context.ee_openai.program(_extraction_json(date_text="hoje", time_text=None))


@then("the unread time is counted")
def step_then_unread_time_is_counted(context):
    assert context.ee_result is not None
    assert context.ee_result["outcomes"].get("unread_time", 0) >= 1, context.ee_result["outcomes"]


@given("an accepted event whose flyer named a time that was not read")
def step_given_accepted_event_with_unread_time(context):
    _seed_ee_venue(context)
    _add_post(context, "rgv_unread_time_admin", flyer_names_time="yes")
    context.ee_openai.program(_extraction_json(date_text="hoje", time_text=None, confidence=0.9))
    _run_extraction(context)
    context.rgv_admin_event_id = _stored_event(context)["event_id"]


@when("the event is read from the admin API")
def step_when_event_read_from_admin_api(context):
    _build_admin_events_app(context)
    context.rgv_admin_response = context.ee_client.get(
        f"/admin/events/{context.rgv_admin_event_id}"
    )


@then("the event reports that its time went unread")
def step_then_event_reports_unread_time(context):
    assert context.rgv_admin_response.status_code == 200, context.rgv_admin_response.text
    body = context.rgv_admin_response.json()
    assert body.get("unread_time") is True, body


@then("the event's review reason is empty")
def step_then_event_review_reason_is_empty(context):
    body = context.rgv_admin_response.json()
    assert not body.get("review_reason"), body
