"""Behave steps for tests/bdd/enrichment/expose-time-known.feature.

See plans/260811_expose-time-known.md. Reuses the SAME context.ee_* harness
instagram_event_extraction_steps.py builds — the Background step ("the event
extraction pipeline is configured for a known venue") and the "the post is
extracted" When step are already bound there (by
event_ticket_info_and_attractions_steps.py), so this file does not redefine
either.

Every Then step here goes through the REAL admin API response (GET
/admin/events/{event_id}, response_model=EventOut) rather than the raw DAO
row. That distinction is the whole point: `time_known` is computed and
persisted well before this plan, but `EventOut(**row)` silently drops it —
a DAO-level assertion would stay green whether or not the field is actually
exposed. Only asserting on the served JSON can tell the two apart.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.services.event_reconciliation import new_event_id
from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post,
    _build_admin_events_app,
    _extraction_json,
    _stored_event,
)

_TS = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _served_event(context, event_id: str) -> dict:
    # Rebuilt every call (cheap: a bare FastAPI app + TestClient) rather than
    # cached on context — set_events_container is a module-level global that
    # must point at THIS scenario's context.ee_dao, never a stale one left
    # over from a prior scenario in the same feature run.
    _build_admin_events_app(context)
    resp = context.ee_client.get(f"/admin/events/{event_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _current_event_id(context) -> str:
    seeded = getattr(context, "etk_event_id", None)
    if seeded is not None:
        return seeded
    return _stored_event(context)["event_id"]


# ── Given: extraction-path fixtures ──────────────────────────────────────────
@given("a post stating the time an event starts")
def step_given_post_stating_the_time(context):
    _add_post(context, "etk_known_time", timestamp=_TS)
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="22h"))


@given("a post stating only the date an event happens")
def step_given_post_stating_only_the_date(context):
    _add_post(context, "etk_date_only", timestamp=_TS)
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text=None))


@given("a post stating an event starts at midnight")
def step_given_post_stating_midnight(context):
    _add_post(context, "etk_midnight", timestamp=_TS)
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="00h"))


# ── Given: a row that predates the flag entirely ─────────────────────────────
@given("a stored item with no recorded time-known flag")
def step_given_stored_item_with_no_flag(context):
    event_id = new_event_id()
    context.etk_event_id = event_id
    context.ee_dao.insert_event({
        "event_id": event_id, "venue_id": context.ee_venue_id,
        "source_kind": "venue_post", "source_handle": context.ee_handle,
        "source_shortcode": "etk_predates_flag",
        "source_event_key": "etk_predates_flag_key",
        "status": "pending_review", "title": "Predates The Flag",
        "starts_at": datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc),
        # No "time_known" key anywhere — not on the row, not folded into
        # raw_extraction — the exact shape of a row written before this
        # plan's column existed.
        "raw_extraction": {"title": "Predates The Flag"},
    })


@when("the item is served")
def step_when_the_item_is_served(context):
    context.etk_response = _served_event(context, _current_event_id(context))


# ── Then ──────────────────────────────────────────────────────────────────────
@then("the item reports its start time as known")
def step_then_reports_known(context):
    row = _served_event(context, _current_event_id(context))
    assert row["time_known"] is True, row


@then("the item reports its start time as unknown")
def step_then_reports_unknown(context):
    row = _served_event(context, _current_event_id(context))
    assert row["time_known"] is False, row
