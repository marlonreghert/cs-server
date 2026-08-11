"""Behave steps for tests/bdd/enrichment/post-items-and-categories.feature.

See plans/260811_post-items-and-categories.md. Drives the REAL
`EventExtractionService` over the SAME `context.ee_*` harness
`instagram_event_extraction_steps.py` already built — its Background step
("the event extraction pipeline is configured for a known venue") and its
`_add_post`/`_add_handle_post`/`_run_extraction`/`_stored_event`/
`_build_admin_events_app` helpers are reused verbatim, the same pattern
every sibling extraction feature file in this suite already follows. "When
the post is extracted" is ALREADY registered (event_ticket_info_and_
attractions_steps.py) — reused, never redefined here (a second identical
decorator would be an AmbiguousStep collision).

Two scenarios need machinery beyond a single-venue extraction:

  - "Queue an item of any type that needs a decision" needs a genuinely
    UNRESOLVED venue. A venue's own post is always attributed to a constant,
    already-resolved venue_id (never the ladder) — the ONLY way this
    harness can produce an unresolved venue_id is the "handles" eligibility
    mode's multi-venue branch (plans/260811_extract-by-handle.md), which
    resolves each event's OWN `location_text` through the SAME resolution
    ladder the shared-handle crawl uses. So this scenario adds a SECOND
    venue sharing the Background venue's handle and a `location_text`
    naming neither — mirroring tests/test_extract_by_handle.py's own
    `TestMultiVenueHandleAttribution.test_a_post_naming_neither_venue_is_
    attributed_to_no_venue_and_queued`, the proven shape for this exact
    outcome.

  - "Read the vocabulary from configuration" needs a live admin-config
    read, which `EventExtractionService.redis_client` (plans/260811_post-
    items-and-categories.md §C) is the seam for. `_reset_context` builds the
    service with `redis_client=None` (no other feature file needs a Redis
    mirror), so this scenario wires a fresh `fakeredis.FakeRedis` onto the
    ALREADY-CONSTRUCTED `context.ee_service` directly — a plain attribute,
    safe to set post-construction — rather than touching the shared harness
    every other scenario in this suite (and several sibling feature files)
    depends on staying exactly as it is.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import fakeredis
from behave import given, then, when  # type: ignore[import-untyped]

from app.models.post_category import ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY
from app.models.venue import Venue
from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_handle_post,
    _add_post,
    _build_admin_events_app,
    _extraction_json,
    _run_extraction,
    _stored_event,
)

RECIFE_LAT, RECIFE_LNG = -8.05, -34.88


def _off_vocabulary_total(category: str) -> float:
    from app.metrics import POST_CATEGORY_OFF_VOCABULARY_TOTAL

    return POST_CATEGORY_OFF_VOCABULARY_TOTAL.labels(category=category)._value.get()


# ── §A: the type is kept ──────────────────────────────────────────────────
@given("a post announcing a discount on a fixed weekday")
def step_given_post_announcing_a_discount_on_a_fixed_weekday(context):
    _add_post(context, "pic_promo")
    context.ee_openai.program(_extraction_json(
        kind="promotion", title="Terça do Chopp em Dobro",
        is_recurring=True, recurrence_text="toda terça",
    ))


@given("a post announcing a dish available on weekdays")
def step_given_post_announcing_a_dish_available_on_weekdays(context):
    _add_post(context, "pic_menu")
    context.ee_openai.program(_extraction_json(
        kind="menu", title="Prato Executivo",
        is_recurring=True, recurrence_text="toda semana",
    ))


@given("a post announcing live music on a stated date")
def step_given_post_announcing_live_music_on_a_stated_date(context):
    ts = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    _add_post(context, "pic_event", timestamp=ts)
    context.ee_openai.program(_extraction_json(
        kind="event", title="Show ao Vivo", date_text="20/08", time_text="21h",
    ))


@given("a post whose extraction names a type this pipeline does not know")
def step_given_post_names_an_unknown_type(context):
    _add_post(context, "pic_unknown")
    context.pic_expected_post_type = "giveaway"
    context.ee_openai.program(_extraction_json(kind="giveaway", title="Sorteio"))


@then("the stored item is a promotion")
def step_then_stored_item_is_a_promotion(context):
    assert _stored_event(context)["post_type"] == "promotion"


@then("the stored item is a menu item")
def step_then_stored_item_is_a_menu_item(context):
    assert _stored_event(context)["post_type"] == "menu"


@then("the stored item is an event")
def step_then_stored_item_is_an_event(context):
    assert _stored_event(context)["post_type"] == "event"


@then("the stored item keeps that type exactly as given")
def step_then_stored_item_keeps_that_type_exactly_as_given(context):
    row = _stored_event(context)
    assert row["post_type"] == context.pic_expected_post_type, row


# ── §A: let an operator correct a misclassified item ─────────────────────────
@given("a stored item recorded as a menu item")
def step_given_a_stored_item_recorded_as_a_menu_item(context):
    ts = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    _add_post(context, "pic_correct", timestamp=ts)
    context.ee_openai.program(_extraction_json(
        kind="menu", title="Prato Executivo", date_text="20/08", time_text="12h",
    ))
    _run_extraction(context)
    row = _stored_event(context)
    assert row["post_type"] == "menu", row
    context.pic_event_id = row["event_id"]


@when("an operator changes its type to an event")
def step_when_an_operator_changes_its_type_to_an_event(context):
    # PATCH then confirm: `post_type` is protected via `operator_edited_
    # fields` (plans/260811_post-items-and-categories.md §B) the SAME way
    # every other operator-editable field already is — that protection only
    # activates for a CONFIRMED row (app.services.event_reconciliation.
    # reconcile_post_events), so "an operator corrects it" is genuinely a
    # correct-and-confirm action, mirroring auto_accept_and_field_
    # protection_steps.py's own `_make_confirmed_event` pattern.
    _build_admin_events_app(context)
    resp = context.ee_client.patch(
        f"/admin/events/{context.pic_event_id}", json={"post_type": "event"},
    )
    assert resp.status_code == 200, resp.text
    resp = context.ee_client.post(f"/admin/events/{context.pic_event_id}/confirm")
    assert resp.status_code == 200, resp.text


@then("that correction survives the next extraction of the same post")
def step_then_correction_survives_the_next_extraction(context):
    # Re-extract the SAME post (same shortcode) with the model repeating its
    # ORIGINAL (wrong) answer — proving the operator's correction holds
    # against exactly the input that would otherwise overwrite it.
    context.ee_openai.program(_extraction_json(
        kind="menu", title="Prato Executivo", date_text="20/08", time_text="12h",
    ))
    _run_extraction(context)
    row = _stored_event(context)
    assert row["event_id"] == context.pic_event_id, row
    assert row["post_type"] == "event", row


# ── §B: the queue still shows decisions, not types ───────────────────────────
@given("a post announcing a dish at a resolved venue")
def step_given_post_announcing_a_dish_at_a_resolved_venue(context):
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "pic_clean_menu", timestamp=ts)
    context.ee_openai.program(_extraction_json(
        kind="menu", title="Prato Executivo", date_text="01/07", time_text="12h", confidence=0.9,
    ))


@then("that item is accepted")
def step_then_that_item_is_accepted(context):
    assert _stored_event(context)["status"] == "accepted"


@then("that item is absent from the review queue")
def step_then_that_item_is_absent_from_the_review_queue(context):
    ids = {r["event_id"] for r in context.ee_dao.list_events_awaiting_decision()}
    assert _stored_event(context)["event_id"] not in ids


@given("a post announcing a discount at an unresolved venue")
def step_given_post_announcing_a_discount_at_an_unresolved_venue(context):
    """A venue's own post is always attributed to its constant, already-
    resolved venue_id — the ONLY way this harness produces an unresolved
    venue_id is the "handles" eligibility mode's multi-venue branch
    (plans/260811_extract-by-handle.md), resolving each event's OWN
    `location_text` through the shared resolution ladder. A second venue,
    sharing the Background venue's handle, plus a `location_text` naming
    NEITHER, is the proven shape for this outcome — see this module's own
    docstring for the pytest precedent this mirrors."""
    second_venue_id = "pic_second_venue"
    context.ee_dao.upsert_venue(Venue(
        venue_id=second_venue_id, venue_name="Segundo Local",
        venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG,
    ))
    from app.models.instagram import VenueInstagram

    context.ee_dao.set_venue_instagram(
        VenueInstagram(venue_id=second_venue_id, instagram_handle=context.ee_handle, status="found")
    )
    _add_handle_post(context, "pic_unresolved")
    context.ee_openai.program(_extraction_json(
        kind="promotion", title="Happy Hour", date_text="01/07", time_text="18h",
        location_text="Praça de Alimentação, Shopping Recife",
    ))
    context.ee_run_config = {"eligibility": {"mode": "handles", "handles": [context.ee_handle]}}


@then("that item awaits a human decision")
def step_then_that_item_awaits_a_human_decision(context):
    row = _stored_event(context)
    ids = {r["event_id"] for r in context.ee_dao.list_events_awaiting_decision()}
    assert row["event_id"] in ids, (row, ids)


# ── §C: categories ────────────────────────────────────────────────────────────
@given("a post announcing a samba night")
def step_given_post_announcing_a_samba_night(context):
    ts = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    _add_post(context, "pic_samba", timestamp=ts)
    context.ee_openai.program(_extraction_json(
        kind="event", title="Noite de Samba", date_text="20/08", time_text="21h",
        category="samba",
    ))


@then("the stored item's category is samba")
def step_then_stored_items_category_is_samba(context):
    assert _stored_event(context)["category"] == "samba"


@given("a post whose extraction returns a category in different case")
def step_given_post_returns_a_category_in_different_case(context):
    ts = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    _add_post(context, "pic_rock", timestamp=ts)
    context.ee_openai.program(_extraction_json(
        kind="event", title="Noite do Rock", date_text="20/08", time_text="21h",
        category="ROCK",
    ))


@then("the stored item's category uses the known spelling")
def step_then_stored_items_category_uses_the_known_spelling(context):
    assert _stored_event(context)["category"] == "rock"


@given("a post whose extraction returns a category nobody listed")
def step_given_post_returns_a_category_nobody_listed(context):
    ts = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    _add_post(context, "pic_bingo", timestamp=ts)
    context.pic_off_vocab_category = "Bingo"
    context.pic_off_vocab_before = _off_vocabulary_total(context.pic_off_vocab_category)
    context.ee_openai.program(_extraction_json(
        kind="event", title="Noite de Bingo", date_text="20/08", time_text="21h",
        category="Bingo",
    ))


@then("the stored item keeps that category")
def step_then_stored_item_keeps_that_category(context):
    assert _stored_event(context)["category"] == context.pic_off_vocab_category


@then("the run counts it as outside the vocabulary")
def step_then_run_counts_it_as_outside_the_vocabulary(context):
    after = _off_vocabulary_total(context.pic_off_vocab_category)
    assert after - context.pic_off_vocab_before == 1.0, (context.pic_off_vocab_before, after)


@given("an operator has added a category to the configured vocabulary")
def step_given_operator_added_a_category_to_the_configured_vocabulary(context):
    from app.models.post_category import DEFAULT_CATEGORY_VOCABULARY

    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    vocabulary = list(DEFAULT_CATEGORY_VOCABULARY) + ["Bingo Night"]
    fake_redis.set(ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY, json.dumps(vocabulary))
    context.ee_service.redis_client = fake_redis
    context.pic_configured_spelling = "Bingo Night"


@when("a post using that category is extracted")
def step_when_a_post_using_that_category_is_extracted(context):
    ts = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    _add_post(context, "pic_configured", timestamp=ts)
    context.ee_openai.program(_extraction_json(
        kind="event", title="Noite de Bingo", date_text="20/08", time_text="21h",
        category="bingo night",
    ))
    _run_extraction(context)


@then("the stored item's category uses the configured spelling")
def step_then_stored_items_category_uses_the_configured_spelling(context):
    assert _stored_event(context)["category"] == context.pic_configured_spelling
