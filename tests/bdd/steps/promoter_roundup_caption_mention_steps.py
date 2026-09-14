"""Behave steps for tests/bdd/enrichment/promoter-roundup-caption-mention.feature.

See plans/260914_promoter-roundup-caption-mention.md.

The rung-5 scenarios drive the SAME real `PromoterCrawlService._process_post`
harness `tests/bdd/steps/event_attribution_and_dates_steps.py` already built
(`context.eadb_*`) — reused verbatim (`_ensure_eadb`, `_add_venue`,
`_eadb_event`, `_event_payload`, `_run`), never rebuilt, per that module's
own "one ladder call site" convention. The Background venue steps ("the
venue ... at handle ...") are ALSO reused verbatim from that module (and,
by extension, `handle_attribution_hardening_steps.py`'s own venue-catalog
persistence-across-scenarios design) — never redefined here, which would
raise behave's AmbiguousStep.

The backfill scenarios run `scripts.backfill_event_venue_links.run_backfill`
directly against the SAME `context.eadb_dao` the ladder scenarios use —
not a second, isolated fixture — specifically so this file can reuse
`handle_attribution_hardening_steps.py`'s already-registered
`the stored event is queued for review with reason "{reason}"` step (its
own `_eadb_event(context)` call is hardwired to `context.eadb_dao`); a
second `@then` on that identical literal text would raise AmbiguousStep.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.services.event_identity import compute_source_event_key
from app.services.event_venue_resolution import (
    RESOLUTION_AUTO,
    build_handle_index,
    build_venue_catalog,
)
from scripts.backfill_event_venue_links import run_backfill
from tests.bdd.steps.event_attribution_and_dates_steps import (
    _ensure_eadb,
    _eadb_event,
    _event_payload,
    _run,
)

_STORED_EVENT_STARTS_AT = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)


# ── the post-level caption Given, one handle only (no "and"/"only"/"first"
# suffix — those variants are already registered in
# event_attribution_and_dates_steps.py and reused by other features) ────────
@given('a promoter post whose caption mentions "@{handle1}"')
def step_given_caption_mentions_one_handle(context, handle1):
    _ensure_eadb(context)
    context.eadb_caption_text = (
        f"Roteiro da noite: @{handle1} bomba hoje! Ingressos abertos, vem cedo."
    )


# ── the post's own extracted events, one row per event, "(none)" for blank ──
def _run_promoter_post_with_location_texts(context, location_texts: list) -> None:
    _ensure_eadb(context)
    context.eadb_shortcode_counter += 1
    shortcode = f"rcm_post_{context.eadb_shortcode_counter}"
    caption = getattr(context, "eadb_caption_text", None) or "Confira! Ingressos abertos."
    events = [
        _event_payload(title=f"Evento RCM {i}", location_text=text)
        for i, text in enumerate(location_texts)
    ]
    context.eadb_openai.program_events(events)
    post = {
        "shortcode": shortcode, "caption": caption,
        "permalink": f"https://instagram.com/p/{shortcode}",
        "timestamp": context.eadb_now.isoformat(),
        "image_urls": [], "location_tag": None,
    }
    venues = build_venue_catalog(context.eadb_dao)
    handle_index = build_handle_index(context.eadb_dao)
    _run(context.eadb_service._process_post(
        handle=context.eadb_promoter_handle, post=post, venues=venues,
        handle_index=handle_index, now=context.eadb_now,
        location_text_fallback_to_caption=False,
    ))
    context.eadb_last_shortcode = shortcode
    rows = context.eadb_dao.list_events_by_source(context.eadb_promoter_handle, shortcode)
    # Keyed by the event's OWN location_text (the "Then the event whose
    # location text is ..." steps below look up by this same value) — a
    # plain list per key since a scenario could, in principle, repeat a
    # text, though none in this feature do.
    context.rcm_events_by_location_text: dict = {}
    for row in rows:
        context.rcm_events_by_location_text.setdefault(row.get("location_text"), []).append(row)


@given("the post's own extracted events have location texts:")
def step_given_post_own_events_have_location_texts(context):
    # behave treats a table's FIRST row as `headings` and the REST as
    # `rows` — this table has no real header (every row is a plain value),
    # so both must be read back together to get every row the Gherkin wrote.
    raw_values = [context.table.headings[0]] + [row[0] for row in context.table.rows]
    location_texts = [None if v.strip() == "(none)" else v for v in raw_values]
    _run_promoter_post_with_location_texts(context, location_texts)



# "When the events are attributed" is already registered in
# event_attribution_and_dates_steps.py (a no-op — the Given step above
# already ran the post, exactly like that module's own 20-event roundup
# scenario) and reused verbatim here; redefining the identical literal text
# would raise behave's AmbiguousStep.


def _rcm_event(context, location_text: str) -> dict:
    events = context.rcm_events_by_location_text.get(location_text)
    assert events, (location_text, list(context.rcm_events_by_location_text.keys()))
    assert len(events) == 1, events
    return events[0]


@then('the event whose location text is "{location_text}" links to no venue')
def step_then_event_with_location_text_links_to_no_venue(context, location_text):
    row = _rcm_event(context, location_text)
    assert row["venue_id"] is None, row


@then('the event whose location text is "{location_text}" is queued for review with reason "{reason}"')
def step_then_event_with_location_text_queued_with_reason(context, location_text, reason):
    row = _rcm_event(context, location_text)
    assert reason in (row.get("review_reason") or ""), row


@then('the event whose location text is "{location_text}" links to "{venue_name}"')
def step_then_event_with_location_text_links_to(context, location_text, venue_name):
    row = _rcm_event(context, location_text)
    expected_id = context.eadb_venues_by_name[venue_name]
    assert row["venue_id"] == expected_id, (venue_name, row)


@then('every event links to "{venue_name}"')
def step_then_every_event_links_to(context, venue_name):
    expected_id = context.eadb_venues_by_name[venue_name]
    rows = context.eadb_dao.list_events_by_source(
        context.eadb_promoter_handle, context.eadb_last_shortcode,
    )
    assert rows, "no events found for the last processed post"
    for row in rows:
        assert row["venue_id"] == expected_id, row


# ── the backfill's new `--mode caption-handle-mention` ───────────────────────
@given('a stored event linked by "{linked_by}" to "{venue_name}"')
def step_given_stored_event_linked_by(context, linked_by, venue_name):
    """Shaped like the 10 live rows the plan's Evidence section measured:
    the event's OWN stored extraction names a bare `@handle` the catalog
    does not carry — `decide_one`'s `caption=None` re-resolution then falls
    all the way through to `_venue_not_in_catalog_result`, never rung 5."""
    _ensure_eadb(context)
    venue_id = context.eadb_venues_by_name[venue_name]
    context.eadb_shortcode_counter += 1
    n = context.eadb_shortcode_counter
    event_id = f"evt_rcm_bf_{n:04d}"
    shortcode = f"rcm_bf_post_{n}"
    title = f"Festa RCM {n}"
    location_text = "@eskinaprime"
    fields = {
        "event_id": event_id, "venue_id": venue_id, "starts_at": _STORED_EVENT_STARTS_AT,
        "title": title, "status": "accepted", "review_reason": None,
        "location_resolution": RESOLUTION_AUTO, "location_confidence": 1.0,
        "linked_by": linked_by, "linked_at": datetime.now(timezone.utc),
        "operator_edited_fields": None, "confidence": 0.9, "location_text": location_text,
        "source_kind": "promoter_post", "source_handle": context.eadb_promoter_handle,
        "source_shortcode": shortcode, "source_permalink": f"https://instagram.com/p/{shortcode}",
        "source_event_key": compute_source_event_key(title, _STORED_EVENT_STARTS_AT),
        "source_event_index": 1, "raw_extraction": {"location_text": location_text},
        "first_seen_at": datetime.now(timezone.utc), "last_seen_at": datetime.now(timezone.utc),
    }
    context.eadb_dao.insert_event(fields)
    context.eadb_last_shortcode = shortcode


def _run_venue_link_backfill_mode(context, *, mode: str, apply: bool) -> None:
    _ensure_eadb(context)
    context.rcm_backfill_report = run_backfill(context.eadb_dao, apply=apply, mode=mode)


@when('the venue-link backfill runs in mode "{mode}" with apply')
def step_when_venue_link_backfill_runs_in_mode(context, mode):
    _run_venue_link_backfill_mode(context, mode=mode, apply=True)


@when('the venue-link backfill runs again in mode "{mode}" with apply')
def step_when_venue_link_backfill_runs_again_in_mode(context, mode):
    _run_venue_link_backfill_mode(context, mode=mode, apply=True)


@then("the stored event links to no venue")
def step_then_stored_event_links_to_no_venue(context):
    row = _eadb_event(context)
    assert row["venue_id"] is None, row


@then('the stored event still links to "{venue_name}"')
def step_then_stored_event_still_links_to(context, venue_name):
    row = _eadb_event(context)
    expected_id = context.eadb_venues_by_name[venue_name]
    assert row["venue_id"] == expected_id, (venue_name, row)
