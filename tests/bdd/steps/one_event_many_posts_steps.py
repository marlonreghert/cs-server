"""Behave steps for tests/bdd/enrichment/one-event-many-posts.feature.

Drives the REAL EventExtractionService + app.services.event_merge (invoked
automatically by the service after every post's reconciliation — see
event_extraction_service.py's `merge_touched_events` call) over the SAME
`context.ee_*` harness instagram_event_extraction_steps.py already builds
(a single event-candidate venue with an Instagram handle). Its Given step
`an event-candidate venue with an Instagram handle` is reused UNMODIFIED as
a plain function call (redefining identical step text would be an
AmbiguousStep collision) — the same reuse pattern
venue_post_multi_event_steps.py and multi_event_posts_steps.py already
establish for sibling feature files.

Every "collapse" scenario adds two or three posts via `_add_post` and
programs the fake OpenAI client via `_events_json` (imported from
multi_event_posts_steps.py, not duplicated), then runs ONE
"the events are extracted" step — `EventExtractionService.run()` processes
every queued post in a single call, each triggering its OWN reconciliation
AND this feature's cross-post merge in turn, exactly the order a real crawl
would. Posts are added with distinct, increasing `timestamp`s and
`context.ee_now` is left at its default (real wall-clock `datetime.now()`),
so each post's `last_seen_at` is genuinely, strictly later than the one
before it — the deterministic way this suite proves "the most recent
source" without needing to fake the clock per post.

Two scenarios (no venue resolved / no date) can never arise through the
venue-post extraction path at all: it always attributes every event to the
POSTING venue (never NULL) and a post either resolves a date or doesn't
reach this feature's merge check in the first place if it does. Those two
Given steps seed two independently-identified events directly through the
DAO (still the real `events.event` / `events.event_source` schema) and the
shared "the events are extracted" step recognises them (via
`context.oemp_direct_event_ids`) and calls the REAL
`app.services.event_merge.merge_touched_events` on them directly — the same
function every other scenario exercises indirectly through the extraction
service, just without a step of unrelated OpenAI/date-resolution plumbing
in between.

One step text ("the corrected title is unchanged") would otherwise be
VERBATIM identical to existing Then text in instagram_event_extraction_
steps.py — an AmbiguousStep collision (same type, same literal text) bound
to a completely different fixture there. Reworded in the feature file to
"the confirmed event's corrected title survives the merge", extending the
same distinguishing-wording convention venue_post_multi_event_steps.py's
docstring already documents for this exact situation.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.services.event_merge import merge_touched_events
from app.services.event_reconciliation import new_event_id
from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post,
    _build_admin_events_app,
    _run_extraction,
)
from tests.bdd.steps.instagram_event_extraction_steps import (
    step_given_an_event_candidate_venue_with_an_instagram_handle as _seed_venue,
)
from tests.bdd.steps.multi_event_posts_steps import _events_json

_TS_1 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
_TS_2 = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
_TS_3 = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
_EVENT_DATE = "2026-08-08"


def _events_for_date(context, date_iso: str = _EVENT_DATE) -> list[dict]:
    events = context.ee_dao.list_events(venue_id=context.ee_venue_id)
    return [
        e for e in events
        if e.get("starts_at") is not None and e["starts_at"].date().isoformat() == date_iso
    ]


def _the_one_event(context, date_iso: str = _EVENT_DATE) -> dict:
    events = _events_for_date(context, date_iso)
    assert len(events) == 1, f"expected exactly one event for {date_iso}; got {events}"
    return events[0]


# ── Background ────────────────────────────────────────────────────────────────
@given("a venue with archived Instagram posts")
def step_given_a_venue_with_archived_instagram_posts(context):
    _seed_venue(context)
    context.oemp_direct_event_ids = None


# ── shared When: dispatches to the real code path each scenario needs ───────
@when("the events are extracted")
def step_when_the_events_are_extracted(context):
    if getattr(context, "oemp_direct_event_ids", None):
        merge_touched_events(
            context.ee_dao, context.oemp_direct_event_ids, datetime.now(timezone.utc),
        )
    else:
        _run_extraction(context)


# ── Scenario 1 (+ 11, 12): three posts, one countdown campaign ──────────────
@given("three posts announcing the same event on the same date at the same venue")
def step_given_three_posts_same_event(context):
    _add_post(context, "oemp_s1", timestamp=_TS_1)
    _add_post(context, "oemp_s2", timestamp=_TS_2)
    _add_post(context, "oemp_s3", timestamp=_TS_3)
    context.ee_openai.program(_events_json([
        {"title": "NOITE DA PATROA", "date_text": "08/08", "time_text": "22h"},
    ]))
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "22h"},
    ]))
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "22h"},
    ]))


@then("one event exists for that date")
def step_then_one_event_exists_for_that_date(context):
    context.oemp_event = _the_one_event(context)


@then("that event carries three sources")
def step_then_that_event_carries_three_sources(context):
    sources = context.ee_dao.list_event_sources(context.oemp_event["event_id"])
    assert len(sources) == 3, sources


# ── Scenario 2: complement a thin teaser from a later detailed post ─────────
@given("a teaser post naming only the event and its date")
def step_given_a_teaser_post(context):
    _add_post(context, "oemp_teaser", timestamp=_TS_1)
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08"},
    ]))


@given("a later post for the same event naming a time and a lineup")
def step_given_a_later_detailed_post(context):
    _add_post(context, "oemp_detailed", timestamp=_TS_2)
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "22h",
         "lineup": ["DJ A", "DJ B"]},
    ]))


@then("the single event carries the date from the teaser")
def step_then_carries_the_date_from_the_teaser(context):
    event = _the_one_event(context)
    assert event["starts_at"].date().isoformat() == _EVENT_DATE, event


@then("it carries the time from the later post")
def step_then_carries_the_time_from_the_later_post(context):
    event = _the_one_event(context)
    assert event["starts_at"].hour == 22, event


@then("it carries the lineup from the later post")
def step_then_carries_the_lineup_from_the_later_post(context):
    event = _the_one_event(context)
    assert event["lineup"] == ["DJ A", "DJ B"], event


# ── Scenario 3: union the lineup ─────────────────────────────────────────────
@given("a post naming two performers and a later post naming five for the same event")
def step_given_lineup_union_posts(context):
    _add_post(context, "oemp_lineup_1", timestamp=_TS_1)
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "22h",
         "lineup": ["DJ A", "DJ B"]},
    ]))
    _add_post(context, "oemp_lineup_2", timestamp=_TS_2)
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "22h",
         "lineup": ["DJ A", "DJ B", "DJ C", "DJ D", "DJ E"]},
    ]))


@then("the single event lists all the distinct performers")
def step_then_lists_all_distinct_performers(context):
    event = _the_one_event(context)
    assert set(event["lineup"]) == {"DJ A", "DJ B", "DJ C", "DJ D", "DJ E"}, event


@then("no performer is listed twice")
def step_then_no_performer_listed_twice(context):
    event = _the_one_event(context)
    assert len(event["lineup"]) == len(set(event["lineup"])), event


# ── Scenario 4: flag a contradiction, keep the most recent ──────────────────
@given("two posts for the same event stating different times")
def step_given_two_posts_different_times(context):
    _add_post(context, "oemp_time_1", timestamp=_TS_1)
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "20h"},
    ]))
    _add_post(context, "oemp_time_2", timestamp=_TS_2)
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "22h"},
    ]))


@then("the event keeps the time from the most recent post")
def step_then_keeps_the_time_from_the_most_recent_post(context):
    event = _the_one_event(context)
    assert event["starts_at"].hour == 22, event


@then('the merged event is flagged with the reason "{reason}"')
def step_then_the_merged_event_is_flagged_with_the_reason(context, reason):
    event = _the_one_event(context)
    assert event["review_reason"] == reason, event


# ── Scenario 5: a confirmed event is never recomputed ────────────────────────
@given("a confirmed event with an operator's corrected title")
def step_given_a_confirmed_event_with_an_operators_corrected_title(context):
    # Seeded directly through the DAO, not through a real extraction+confirm
    # round trip — an already-confirmed row is exactly what a real crawl
    # would have produced days earlier; what THIS scenario tests is what
    # happens when a NEW post arrives after that, not how confirmation
    # itself works (covered by instagram-event-extraction.feature). The
    # title is the operator's "corrected" (properly cased) form — still the
    # SAME identity (`normalize_title`) the later post's own model-cased
    # title will resolve to, exactly as compute_event_identity requires for
    # ANY merge to happen at all (see the plan's honest limitation: a
    # materially different title never merges, confirmed or not).
    event_id = new_event_id()
    context.ee_dao.insert_event({
        "event_id": event_id, "venue_id": context.ee_venue_id,
        "source_kind": "venue_post", "source_handle": context.ee_handle,
        "source_shortcode": "oemp_confirmed_seed", "source_event_key": "oemp_seed_key",
        "status": "confirmed", "title": "Noite da Patroa",
        "starts_at": datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc),
        "price_text": "R$30", "raw_extraction": {"time_known": True},
    })
    context.oemp_confirmed_event_id = event_id


@given("a later post announcing the same event")
def step_given_a_later_post_announcing_the_same_event(context):
    _add_post(context, "oemp_confirmed_later", timestamp=_TS_2)
    context.ee_openai.program(_events_json([
        {"title": "NOITE DA PATROA", "date_text": "08/08", "time_text": "22h",
         "price_text": "R$40"},
    ]))


@then("the confirmed event's corrected title survives the merge")
def step_then_the_confirmed_events_corrected_title_survives_the_merge(context):
    row = context.ee_dao.get_event(context.oemp_confirmed_event_id)
    assert row is not None
    assert row["title"] == "Noite da Patroa", row


@then("the new source is attached to that event")
def step_then_the_new_source_is_attached_to_that_event(context):
    sources = context.ee_dao.list_event_sources(context.oemp_confirmed_event_id)
    shortcodes = {s["source_shortcode"] for s in sources}
    assert shortcodes == {"oemp_confirmed_seed", "oemp_confirmed_later"}, sources


@then("the divergence is flagged")
def step_then_the_divergence_is_flagged(context):
    row = context.ee_dao.get_event(context.oemp_confirmed_event_id)
    assert row is not None
    assert row["review_reason"] == "model_diverges_from_confirmed_record", row


# ── Scenario 5b: field-level protection extends to the merge path ───────────
# The coordination note on plans/260807_auto-accept-and-field-level-
# protection.md: event_merge.merge_event_fields's confirmed-canonical branch
# used to freeze the WHOLE row, same as event_reconciliation's confirmed
# branch did before that plan — fixed there, but left drifting here, so an
# operator's edit behaved two different ways depending on which runtime path
# touched it next. This scenario drives the REAL merge_touched_events (via a
# genuine second post, not a direct merge_event_fields call — that pure-
# function level is covered by tests/test_event_merge.py's
# TestFieldLevelProtectionOnTheMergePath) to prove the fix end to end.
#
# The edited/protected field here is PRICE, not title: compute_event_identity
# groups candidates by (venue_id, date, normalize_title(title)), so two
# events ever reaching merge_event_fields together are ALREADY guaranteed to
# agree on title post-normalization — a materially different title never
# merges at all (Scenario 5's own docstring already names this), so title
# specifically can never be the field that "genuinely differs" in an
# end-to-end scenario. Price has no such constraint and stands in for it.
@given("a confirmed event whose price an operator corrected")
def step_given_a_confirmed_event_whose_price_an_operator_corrected(context):
    event_id = new_event_id()
    context.ee_dao.insert_event({
        "event_id": event_id, "venue_id": context.ee_venue_id,
        "source_kind": "venue_post", "source_handle": context.ee_handle,
        "source_shortcode": "oemp_price_seed", "source_event_key": "oemp_price_seed_key",
        "status": "confirmed", "title": "Noite da Patroa",
        "starts_at": datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc),
        "price_text": "R$30 corrigido pelo operador",
        "operator_edited_fields": ["price_text"],
        "raw_extraction": {"time_known": True},
    })
    context.oemp_price_confirmed_event_id = event_id


@given("a later post for the same event stating a different price and a new description")
def step_given_a_later_post_stating_a_different_price_and_description(context):
    _add_post(context, "oemp_price_later", timestamp=_TS_2)
    context.ee_openai.program(_events_json([
        {"title": "NOITE DA PATROA", "date_text": "08/08", "time_text": "22h",
         "price_text": "R$99", "description": "Open bar até meia-noite"},
    ]))


@then("the operator's price survives the merge")
def step_then_the_operators_price_survives_the_merge(context):
    row = context.ee_dao.get_event(context.oemp_price_confirmed_event_id)
    assert row is not None
    assert row["price_text"] == "R$30 corrigido pelo operador", row


@then("the divergence from the confirmed record is flagged")
def step_then_the_divergence_from_the_confirmed_record_is_flagged(context):
    row = context.ee_dao.get_event(context.oemp_price_confirmed_event_id)
    assert row is not None
    assert row["review_reason"] == "model_diverges_from_confirmed_record", row


@then("the new description is absorbed from the later post")
def step_then_the_new_description_is_absorbed_from_the_later_post(context):
    row = context.ee_dao.get_event(context.oemp_price_confirmed_event_id)
    assert row is not None
    assert row.get("description") == "Open bar até meia-noite", row


# ── Scenario 6: single-post re-extraction stays idempotent ──────────────────
@given("a post that already produced an event")
def step_given_a_post_that_already_produced_an_event(context):
    _add_post(context, "oemp_idempotent", timestamp=_TS_1)
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "22h"},
    ]))
    _run_extraction(context)
    row = context.ee_dao.get_event_by_source(context.ee_handle, "oemp_idempotent")
    assert row is not None
    context.oemp_idempotent_event_id = row["event_id"]


@when("that post is extracted again")
def step_when_that_post_is_extracted_again(context):
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "22h"},
    ]))
    _run_extraction(context)


@then("the event carries one source")
def step_then_the_event_carries_one_source(context):
    sources = context.ee_dao.list_event_sources(context.oemp_idempotent_event_id)
    assert len(sources) == 1, sources


@then("no second event is created")
def step_then_no_second_event_is_created(context):
    events = context.ee_dao.list_events(venue_id=context.ee_venue_id)
    assert len(events) == 1, events


# ── Scenario 7: never merge without a venue ──────────────────────────────────
@given("two posts announcing the same title and date with no venue resolved")
def step_given_two_posts_same_title_date_no_venue(context):
    starts_at = datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc)
    ids = []
    for i, shortcode in enumerate(("oemp_novenue_1", "oemp_novenue_2"), start=1):
        event_id = new_event_id()
        context.ee_dao.insert_event({
            "event_id": event_id, "venue_id": None,
            "source_kind": "promoter_post", "source_handle": context.ee_handle,
            "source_shortcode": shortcode, "source_event_key": f"oemp_novenue_key_{i}",
            "status": "pending_review", "title": "Festa Sem Venue Resolvida",
            "starts_at": starts_at,
        })
        ids.append(event_id)
    context.oemp_direct_event_ids = ids


# ── Scenario 8: never merge without a date ───────────────────────────────────
@given("two posts announcing the same title at the same venue with no date")
def step_given_two_posts_same_title_venue_no_date(context):
    ids = []
    for i, shortcode in enumerate(("oemp_nodate_1", "oemp_nodate_2"), start=1):
        event_id = new_event_id()
        context.ee_dao.insert_event({
            "event_id": event_id, "venue_id": context.ee_venue_id,
            "source_kind": "venue_post", "source_handle": context.ee_handle,
            "source_shortcode": shortcode, "source_event_key": f"oemp_nodate_key_{i}",
            "status": "pending_review", "title": "Festa Sem Data", "starts_at": None,
        })
        ids.append(event_id)
    context.oemp_direct_event_ids = ids


@then("two separate events exist")
def step_then_two_separate_events_exist(context):
    if getattr(context, "oemp_direct_event_ids", None):
        rows = [context.ee_dao.get_event(i) for i in context.oemp_direct_event_ids]
        assert all(r is not None for r in rows), rows
        assert len({r["event_id"] for r in rows}) == 2, rows
    else:
        events = _events_for_date(context)
        assert len(events) == 2, events


# ── Scenario 9: never merge different same-night titles ─────────────────────
@given("two posts announcing different events on the same date at the same venue")
def step_given_two_posts_different_titles_same_date_venue(context):
    _add_post(context, "oemp_diff_1", timestamp=_TS_1)
    _add_post(context, "oemp_diff_2", timestamp=_TS_2)
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "22h"},
    ]))
    context.ee_openai.program(_events_json([
        {"title": "Equilibrium na Metropole", "date_text": "08/08", "time_text": "22h"},
    ]))


# ── Scenario 10: merge titles differing only in case and accents ────────────
@given('two posts for the same date and venue titled "NOITE DA PATROA" and "Noite da Patroa"')
def step_given_two_posts_case_and_accent_titles(context):
    _add_post(context, "oemp_case_1", timestamp=_TS_1)
    _add_post(context, "oemp_case_2", timestamp=_TS_2)
    context.ee_openai.program(_events_json([
        {"title": "NOITE DA PATROA", "date_text": "08/08", "time_text": "22h"},
    ]))
    context.ee_openai.program(_events_json([
        {"title": "Noite da Patroa", "date_text": "08/08", "time_text": "22h"},
    ]))


# ── Scenarios 11, 12: sourcing on the merged event, and the compat guarantee ─
@when("the merged event is requested")
def step_when_the_merged_event_is_requested(context):
    _run_extraction(context)
    event = _the_one_event(context)
    _build_admin_events_app(context)
    context.oemp_response = context.ee_client.get(f"/admin/events/{event['event_id']}")
    assert context.oemp_response.status_code == 200, context.oemp_response.text


@then("each source carries its own permalink")
def step_then_each_source_carries_its_own_permalink(context):
    sources = context.oemp_response.json()["sources"]
    assert len(sources) == 3, sources
    permalinks = [s["source_permalink"] for s in sources]
    assert all(permalinks), sources
    assert len(set(permalinks)) == 3, sources


@then("each source carries its own cover photo key")
def step_then_each_source_carries_its_own_cover_photo_key(context):
    sources = context.oemp_response.json()["sources"]
    cover_keys = [s["cover_photo_key"] for s in sources]
    assert all(cover_keys), sources
    assert len(set(cover_keys)) == 3, sources


@then("the event still carries a source permalink")
def step_then_the_event_still_carries_a_source_permalink(context):
    body = context.oemp_response.json()
    assert body.get("source_permalink"), body


@then("the event still carries a cover photo key")
def step_then_the_event_still_carries_a_cover_photo_key(context):
    body = context.oemp_response.json()
    assert body.get("cover_photo_key"), body


@then("those values come from the most recent source")
def step_then_those_values_come_from_the_most_recent_source(context):
    body = context.oemp_response.json()
    most_recent = max(body["sources"], key=lambda s: s["last_seen_at"])
    assert body["source_permalink"] == most_recent["source_permalink"], (body, most_recent)
    assert body["cover_photo_key"] == most_recent["cover_photo_key"], (body, most_recent)
