"""Behave steps for
tests/bdd/enrichment/event-ticket-info-and-attractions.feature.

See plans/260808_event-ticket-info-and-attractions.md. Drives the REAL
EventExtractionService (+ its automatic app.services.event_merge call after
every post) over the SAME `context.ee_*` harness
instagram_event_extraction_steps.py already builds — its Given step "an
event-candidate venue with an Instagram handle" is reused UNMODIFIED as a
plain function call, the same reuse pattern one_event_many_posts_steps.py
and multi_event_posts_steps.py already establish for sibling feature files.

Single-post scenarios (tickets, attractions, malformed/truncated, empty)
program one flat event JSON and run extraction once. The cross-post
scenarios (merge accumulation, stage fill-in, two stages apart, operator
protection) SEED the earlier event directly through the DAO — the same
pattern one_event_many_posts_steps.py's own confirmed/price scenarios use —
rather than running a real extraction for it, since what those scenarios
test is the merge fold, not the first post's own extraction (already
covered by the single-post scenarios above and by
tests/bdd/enrichment/instagram-event-extraction.feature). The LATER post in
each of those scenarios DOES run through the real extraction pipeline, so
the merge itself (`app.services.event_merge.merge_touched_events`, invoked
automatically by `EventExtractionService._extract_one`) is exercised
end-to-end, not just at the pure-function level `tests/test_event_merge.py`
already covers.

One step text ("no event is persisted for that post") would otherwise be
VERBATIM identical to existing Then text in multi_event_posts_steps.py — an
AmbiguousStep collision (same step type, same literal text, bound to a
completely different, promoter-crawl-specific fixture there). Reworded in
the feature file to "no event is stored for that post", extending the same
distinguishing-wording convention venue_post_multi_event_steps.py's and
one_event_many_posts_steps.py's docstrings already document for this exact
situation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.services.event_reconciliation import new_event_id
from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post,
    _run_extraction,
    _stored_event,
)
from tests.bdd.steps.instagram_event_extraction_steps import (
    step_given_an_event_candidate_venue_with_an_instagram_handle as _seed_venue,
)

_TS_1 = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
_TS_2 = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
_EVENT_TITLE = "Noite da Patroa"
_EVENT_DATE_TEXT = "08/08"
# America/Recife, matching exactly what event_date_resolver.resolve_event_
# datetime produces for date_text="08/08"/time_text="22h" — a seeded fixture
# using a different tzinfo (e.g. bare UTC) for the "same" 22:00 would be a
# genuinely different instant to the real resolver's output, and would
# register as a scalar disagreement on starts_at the moment a real post
# merges against it (caught by the "never overwrite ... / not flagged"
# scenario below).
_EVENT_STARTS_AT = datetime(2026, 8, 8, 22, 0, tzinfo=ZoneInfo("America/Recife"))


def _snapshot_metric(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


def _tia_event_json(**overrides) -> str:
    """One flat (single-event) model response — the fake OpenAI client
    (instagram_event_extraction_steps._FakeOpenAIClient) wraps it into the
    real `{"events": [...]}` shape automatically. No "lineup" key by
    default: every scenario that needs a derived lineup gets it from
    "attractions" alone, proving the derivation rather than a pass-through.
    """
    payload = {
        "title": _EVENT_TITLE, "description": None, "date_text": _EVENT_DATE_TEXT,
        "time_text": "22h", "is_recurring": False, "recurrence_text": None,
        "attractions": [], "ticket_url": None, "ticket_info": None,
        "price_text": None, "location_text": None, "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _the_event(context) -> dict:
    """The single surviving event for this scenario's venue, by title — used
    by the cross-post merge scenarios, where the canonical event's id is not
    known ahead of time (a non-confirmed merge keeps the OLDEST event_id,
    which is the seeded teaser's, but asserting on title is more direct than
    asserting on which id "won")."""
    events = [
        e for e in context.ee_dao.list_events(venue_id=context.ee_venue_id)
        if e.get("title") == _EVENT_TITLE
    ]
    assert len(events) == 1, f"expected exactly one {_EVENT_TITLE!r} event; got {events}"
    return events[0]


def _seed_existing_event(context, shortcode: str, **overrides) -> str:
    """Seed an event directly through the DAO — the "already announced by a
    teaser/post" precondition several scenarios below need, without running
    a real extraction for it (see module docstring)."""
    event_id = new_event_id()
    fields = {
        "event_id": event_id, "venue_id": context.ee_venue_id,
        "source_kind": "venue_post", "source_handle": context.ee_handle,
        "source_shortcode": shortcode, "source_event_key": f"{shortcode}_key",
        "status": "accepted", "title": _EVENT_TITLE, "starts_at": _EVENT_STARTS_AT,
        "raw_extraction": {"time_known": True},
    }
    fields.update(overrides)
    context.ee_dao.insert_event(fields)
    context.tia_seeded_event_id = event_id
    return event_id


# ── Background ────────────────────────────────────────────────────────────────
@given("the event extraction pipeline is configured for a known venue")
def step_given_the_pipeline_is_configured_for_a_known_venue(context):
    _seed_venue(context)


# ── shared When: single-post extraction ──────────────────────────────────────
@when("the post is extracted")
def step_when_the_post_is_extracted(context):
    _run_extraction(context)


# ── Ticket references ─────────────────────────────────────────────────────────
@given("a post whose caption offers tickets without giving a URL")
def step_given_ticket_reference_without_url(context):
    _add_post(context, "tia_ticket_no_url", timestamp=_TS_1)
    context.ee_openai.program(_tia_event_json(ticket_info="\U0001f3ab TICKETS", ticket_url=None))


@then("the event records the ticket reference verbatim")
def step_then_event_records_ticket_reference_verbatim(context):
    assert _stored_event(context)["ticket_info"] == "\U0001f3ab TICKETS"


@then("the event has no ticket URL")
def step_then_event_has_no_ticket_url(context):
    assert _stored_event(context).get("ticket_url") is None


@given("a post whose caption gives both a ticketing link and a purchase note")
def step_given_ticket_link_and_purchase_note(context):
    _add_post(context, "tia_ticket_both", timestamp=_TS_1)
    context.ee_openai.program(_tia_event_json(
        ticket_url="https://sympla.com.br/evento-x",
        ticket_info="lote promocional até 23:30",
    ))


@then("the event records the ticketing link")
def step_then_event_records_the_ticketing_link(context):
    assert _stored_event(context)["ticket_url"] == "https://sympla.com.br/evento-x"


@then("the event records the purchase note verbatim")
def step_then_event_records_the_purchase_note_verbatim(context):
    assert _stored_event(context)["ticket_info"] == "lote promocional até 23:30"


@then("neither value suppresses the other")
def step_then_neither_value_suppresses_the_other(context):
    row = _stored_event(context)
    assert row["ticket_url"] == "https://sympla.com.br/evento-x"
    assert row["ticket_info"] == "lote promocional até 23:30"


@given("a post whose caption never mentions tickets, entry or price")
def step_given_caption_never_mentions_tickets(context):
    _add_post(context, "tia_no_ticket_mention", timestamp=_TS_1)
    context.ee_openai.program(_tia_event_json(ticket_url=None, ticket_info=None, price_text=None))


@then("the event has no ticket reference")
def step_then_event_has_no_ticket_reference(context):
    assert _stored_event(context).get("ticket_info") is None


# ── Attractions ────────────────────────────────────────────────────────────────
@given("a post whose caption announces two dance floors with different acts")
def step_given_two_dance_floors_with_different_acts(context):
    _add_post(context, "tia_two_stages", timestamp=_TS_1)
    context.ee_openai.program(_tia_event_json(attractions=[
        {"name": "DJ Ramon", "type": "dj", "stage": "Pista NY", "styles": ["Pop"]},
        {"name": "DJ Harry", "type": "dj", "stage": "Pista Brasil", "styles": ["Funk"]},
    ]))


@then("each attraction is tagged with the stage it plays")
def step_then_each_attraction_tagged_with_the_stage_it_plays(context):
    attractions = _stored_event(context)["attractions"]
    stages = {a["name"]: a["stage"] for a in attractions}
    assert stages == {"DJ Ramon": "Pista NY", "DJ Harry": "Pista Brasil"}, attractions


@then("the acts announced for one stage are not attributed to the other")
def step_then_acts_not_attributed_to_the_other_stage(context):
    attractions = _stored_event(context)["attractions"]
    ny = {a["name"] for a in attractions if a["stage"] == "Pista NY"}
    brasil = {a["name"] for a in attractions if a["stage"] == "Pista Brasil"}
    assert ny == {"DJ Ramon"}, attractions
    assert brasil == {"DJ Harry"}, attractions
    assert ny.isdisjoint(brasil)


@given("a post whose caption announces a live show alongside DJ sets")
def step_given_live_show_alongside_dj_sets(context):
    _add_post(context, "tia_live_and_dj", timestamp=_TS_1)
    context.ee_openai.program(_tia_event_json(attractions=[
        {"name": "Eliza Mello", "type": "live", "stage": None, "styles": []},
        {"name": "DJ Thomas Henry", "type": "dj", "stage": None, "styles": []},
        {"name": "DJ Lea Farsaid", "type": "dj", "stage": None, "styles": []},
    ]))


@then("the live act is recorded as a live attraction")
def step_then_live_act_recorded_as_live(context):
    attractions = _stored_event(context)["attractions"]
    live = next(a for a in attractions if a["name"] == "Eliza Mello")
    assert live["type"] == "live", attractions


@then("each DJ is recorded as a DJ attraction")
def step_then_each_dj_recorded_as_dj_attraction(context):
    attractions = _stored_event(context)["attractions"]
    djs = {a["name"] for a in attractions if a["type"] == "dj"}
    assert djs == {"DJ Thomas Henry", "DJ Lea Farsaid"}, attractions


@given("a post whose caption names one known musical style and one unknown one")
def step_given_known_and_unknown_musical_style(context):
    _add_post(context, "tia_style_filter", timestamp=_TS_1)
    context.ee_openai.program(_tia_event_json(attractions=[
        {"name": "DJ Style", "type": "dj", "stage": None, "styles": ["Pop", "Bogus Style"]},
    ]))


@then("the attraction carries the known style")
def step_then_attraction_carries_the_known_style(context):
    attractions = _stored_event(context)["attractions"]
    assert attractions[0]["styles"] == ["Pop"], attractions


@then("the unknown style is not stored")
def step_then_unknown_style_is_not_stored(context):
    attractions = _stored_event(context)["attractions"]
    assert "Bogus Style" not in attractions[0]["styles"], attractions


@given("a post whose caption announces several named acts")
def step_given_several_named_acts(context):
    _add_post(context, "tia_lineup_from_attractions", timestamp=_TS_1)
    context.ee_openai.program(_tia_event_json(attractions=[
        {"name": "DJ A", "type": "dj", "stage": None, "styles": []},
        {"name": "DJ B", "type": "dj", "stage": None, "styles": []},
        {"name": "DJ C", "type": "dj", "stage": None, "styles": []},
    ]))


@then("the event lineup lists exactly the attractions' names in the order announced")
def step_then_lineup_lists_attraction_names_in_order(context):
    row = _stored_event(context)
    assert row["lineup"] == ["DJ A", "DJ B", "DJ C"], row["lineup"]


@given("an extraction response in which one attraction entry is malformed")
def step_given_one_attraction_entry_malformed(context):
    _add_post(context, "tia_malformed_attraction", timestamp=_TS_1)
    context.tia_malformed_attractions_before = _snapshot_metric(
        "event_extraction_malformed_attractions_total",
    )
    context.ee_openai.program(_tia_event_json(attractions=[
        {"name": "DJ Good", "type": "dj", "stage": None, "styles": []},
        {"type": "dj"},  # malformed: no usable name
    ]))


@then("the well-formed attractions are persisted")
def step_then_well_formed_attractions_are_persisted(context):
    attractions = _stored_event(context)["attractions"]
    assert [a["name"] for a in attractions] == ["DJ Good"], attractions


@then("the malformed attraction is skipped and counted")
def step_then_malformed_attraction_is_skipped_and_counted(context):
    before = context.tia_malformed_attractions_before
    after = _snapshot_metric("event_extraction_malformed_attractions_total")
    assert after - before == 1.0, (before, after)


@given("an extraction response that is truncated before it completes")
def step_given_extraction_response_truncated(context):
    _add_post(context, "tia_truncated", timestamp=_TS_1)
    context.ee_openai.program_truncated('{"events": [{"title": "Festa", "attracti')


@then("no event is stored for that post")
def step_then_no_event_stored_for_that_post(context):
    row = context.ee_dao.get_event_by_source(context.ee_handle, context.ee_last_shortcode)
    assert row is None, row


@then("the truncation is counted")
def step_then_the_truncation_is_counted(context):
    assert context.ee_result is not None
    assert context.ee_result["outcomes"].get("truncated", 0) >= 1, context.ee_result


@given("a post whose caption announces an event with no acts and no ticket information")
def step_given_event_with_no_acts_and_no_ticket_information(context):
    _add_post(context, "tia_nothing_stated", timestamp=_TS_1)
    context.ee_openai.program(_tia_event_json(attractions=[], ticket_url=None, ticket_info=None))


@then("the event has no attractions")
def step_then_event_has_no_attractions(context):
    assert _stored_event(context).get("attractions") in (None, []), _stored_event(context)


# ── Merging across the posts that announce one event ─────────────────────────
@given("an event already announced by a teaser naming two acts")
def step_given_event_announced_by_teaser_naming_two_acts(context):
    _seed_existing_event(context, "tia_teaser_two_acts", attractions=[
        {"name": "DJ A", "type": "dj", "stage": None, "styles": []},
        {"name": "DJ B", "type": "dj", "stage": None, "styles": []},
    ])


@when("a later post announcing the same event names three further acts")
def step_when_later_post_names_three_further_acts(context):
    _add_post(context, "tia_later_three_acts", timestamp=_TS_2)
    context.ee_openai.program(_tia_event_json(attractions=[
        {"name": "DJ C", "type": "dj", "stage": None, "styles": []},
        {"name": "DJ D", "type": "dj", "stage": None, "styles": []},
        {"name": "DJ E", "type": "dj", "stage": None, "styles": []},
    ]))
    _run_extraction(context)


@then("the event carries all five acts")
def step_then_event_carries_all_five_acts(context):
    names = {a["name"] for a in _the_event(context)["attractions"]}
    assert names == {"DJ A", "DJ B", "DJ C", "DJ D", "DJ E"}, names


@then("no act announced by the teaser is lost")
def step_then_no_act_announced_by_teaser_is_lost(context):
    names = {a["name"] for a in _the_event(context)["attractions"]}
    assert {"DJ A", "DJ B"} <= names, names


@given("an event already announced by a teaser naming an act with no stage")
def step_given_event_announced_by_teaser_act_no_stage(context):
    _seed_existing_event(context, "tia_teaser_no_stage", attractions=[
        {"name": "DJ A", "type": "dj", "stage": None, "styles": []},
    ])


@when("a later post announcing the same event places that act on a named stage")
def step_when_later_post_places_act_on_a_named_stage(context):
    _add_post(context, "tia_later_named_stage", timestamp=_TS_2)
    context.ee_openai.program(_tia_event_json(attractions=[
        {"name": "DJ A", "type": "dj", "stage": "Pista NY", "styles": []},
    ]))
    _run_extraction(context)


@then("the event records that act once, on the named stage")
def step_then_event_records_act_once_on_the_named_stage(context):
    attractions = _the_event(context)["attractions"]
    matches = [a for a in attractions if a["name"] == "DJ A"]
    assert len(matches) == 1, attractions
    assert matches[0]["stage"] == "Pista NY", matches


@given("an event already announced by a post placing an act on one stage")
def step_given_event_announced_by_post_act_on_one_stage(context):
    _seed_existing_event(context, "tia_teaser_stage_a", attractions=[
        {"name": "DJ A", "type": "dj", "stage": "Pista NY", "styles": []},
    ])


@when("a later post announcing the same event places that act on a second stage")
def step_when_later_post_places_act_on_a_second_stage(context):
    _add_post(context, "tia_later_stage_b", timestamp=_TS_2)
    context.ee_openai.program(_tia_event_json(attractions=[
        {"name": "DJ A", "type": "dj", "stage": "Pista Brasil", "styles": []},
    ]))
    _run_extraction(context)


@then("the event records that act once per stage")
def step_then_event_records_act_once_per_stage(context):
    attractions = _the_event(context)["attractions"]
    matches = [a for a in attractions if a["name"] == "DJ A"]
    stages = {a["stage"] for a in matches}
    assert len(matches) == 2, attractions
    assert stages == {"Pista NY", "Pista Brasil"}, attractions


# ── Operator protection ────────────────────────────────────────────────────────
@given("an event whose ticket reference an operator has edited")
def step_given_event_whose_ticket_reference_operator_edited(context):
    event_id = _seed_existing_event(
        context, "tia_confirmed_ticket",
        status="confirmed", ticket_info="Ingressos na entrada (confirmado)",
        operator_edited_fields=["ticket_info"],
    )
    context.tia_confirmed_event_id = event_id


@when("a later post announcing the same event states a different ticket reference")
def step_when_later_post_states_different_ticket_reference(context):
    _add_post(context, "tia_confirmed_later", timestamp=_TS_2)
    context.ee_openai.program(_tia_event_json(ticket_info="Ingressos pelo WhatsApp"))
    _run_extraction(context)


@then("the event keeps the operator's ticket reference")
def step_then_event_keeps_the_operators_ticket_reference(context):
    row = context.ee_dao.get_event(context.tia_confirmed_event_id)
    assert row["ticket_info"] == "Ingressos na entrada (confirmado)", row


@then("the event is flagged as diverging from the confirmed record")
def step_then_event_flagged_diverging_from_confirmed_record(context):
    row = context.ee_dao.get_event(context.tia_confirmed_event_id)
    assert row["review_reason"] == "model_diverges_from_confirmed_record", row


@given("an event that already records a ticket reference")
def step_given_event_that_already_records_ticket_reference(context):
    event_id = _seed_existing_event(
        context, "tia_has_ticket", ticket_info="Ingressos na bilheteria",
    )
    context.tia_has_ticket_event_id = event_id


@when("a later post announcing the same event states no ticket reference")
def step_when_later_post_states_no_ticket_reference(context):
    _add_post(context, "tia_has_ticket_later", timestamp=_TS_2)
    context.ee_openai.program(_tia_event_json(ticket_info=None))
    _run_extraction(context)


@then("the event keeps the ticket reference it already had")
def step_then_event_keeps_the_ticket_reference_it_already_had(context):
    row = context.ee_dao.get_event(context.tia_has_ticket_event_id)
    assert row["ticket_info"] == "Ingressos na bilheteria", row


@then("the event is not flagged")
def step_then_event_is_not_flagged(context):
    row = context.ee_dao.get_event(context.tia_has_ticket_event_id)
    assert row.get("review_reason") is None, row
