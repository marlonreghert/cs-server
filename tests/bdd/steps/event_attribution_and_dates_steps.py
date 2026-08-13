"""Behave steps for tests/bdd/enrichment/event-attribution-and-dates.feature.

See plans/260812_event-attribution-and-dates.md.

§A/§B (venue attribution) drive the REAL `PromoterCrawlService._process_post`
— the SAME per-post pipeline every live promoter/shared-handle crawl calls —
over a REAL `VenueRepository` backed by the in-memory RDS fake
(tests/rds_fake.py), with a fake OpenAI client at the true external boundary
(no live Apify/S3/OpenAI, per CLAUDE.md). §B's "two venues behind one
account" scenarios set `location_text_fallback_to_caption=True`, exactly
what `InstagramCrawlChainer._chain_shared_handle` already does for a
handle shared by several venues (app.services.event_venue_resolution.
build_location_text_attribute_fn's own docstring: "Both callers that pass
`venues` bounded to one handle's own two or three venues pass `True`").

§C/§D (dates) call `app.services.event_date_resolver.resolve_event_datetime`
directly — pure, deterministic, no DAO needed.

§E (post type) reuses the REAL `EventExtractionService` harness
(`context.ee_*`) already built by instagram_event_extraction_steps.py and
event_ticket_info_and_attractions_steps.py — the SAME "an event-candidate
venue with an Instagram handle" / "the post is extracted" step texts are
registered there and reused here verbatim (never redefined, which would
raise behave's AmbiguousStep).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.event_date_resolver import REASON_WEEKDAY_MISMATCH, resolve_event_datetime
from app.services.event_venue_resolution import build_handle_index, build_venue_catalog
from app.services.promoter_crawl_service import PromoterCrawlService
from tests.bdd.steps.instagram_event_extraction_steps import (
    _add_post,
    _run_extraction,
    _stored_event,
)
from tests.bdd.steps.instagram_event_extraction_steps import (
    step_given_an_event_candidate_venue_with_an_instagram_handle as _seed_ee_venue,
)
from tests.rds_fake import InMemoryRdsVenueStore

RECIFE = ZoneInfo("America/Recife")
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ── §A/§B fakes at the true external boundary ────────────────────────────────
class _FakeOpenAIClient:
    """Programmed with one FULL raw response text per call, in order —
    unlike sibling fakes in this suite, never auto-wraps a flat dict, so a
    scenario can program a genuine `{"events": [...]}` payload with several
    events (the 20-event roundup scenario needs this)."""

    def __init__(self):
        self._responses: list = []
        self.calls = 0

    def program_events(self, events: list[dict]) -> None:
        self._responses.append(json.dumps({"events": events}))

    async def extract_events(self, *, caption, image_data_uri=None, max_events):
        self.calls += 1
        if not self._responses:
            raise AssertionError("fake OpenAI client called more times than programmed")
        raw = self._responses.pop(0)
        return raw, False


def _event_payload(**overrides) -> dict:
    payload = {
        "kind": "event", "title": "Festa", "description": None,
        "date_text": "15/08", "time_text": "22h",
        "is_recurring": False, "recurrence_text": None,
        "attractions": [], "ticket_url": None, "ticket_info": None,
        "price_text": None, "location_text": None,
        "category": None, "confidence": 0.9,
    }
    payload.update(overrides)
    return payload


def _metric(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ── §A/§B context setup ───────────────────────────────────────────────────────
def _ensure_eadb(context) -> None:
    if getattr(context, "eadb_dao", None) is not None:
        return
    context.eadb_store = InMemoryRdsVenueStore()
    from app.dao.venue_repository import VenueRepository

    context.eadb_dao = VenueRepository(client=None, rds_store=context.eadb_store)
    context.eadb_openai = _FakeOpenAIClient()
    context.eadb_service = PromoterCrawlService(
        venue_dao=context.eadb_dao, posts_client=None, openai_client=context.eadb_openai,
        min_confidence=0.0,
    )
    context.eadb_venues_by_name: dict[str, str] = {}
    context.eadb_now = datetime(2026, 8, 12, 20, 0, tzinfo=timezone.utc)
    context.eadb_promoter_handle = "oquetemhojeemnatal"
    context.eadb_caption_mentions: list[str] = []
    context.eadb_location_text = None
    context.eadb_location_tag = None
    context.eadb_same_account_venues: "list | None" = None
    context.eadb_shared_handle_venues: "list | None" = None
    context.eadb_shortcode_counter = 0


def _add_venue(context, name: str, handle: str, *, address=None, lat=-8.05, lng=-34.88) -> str:
    _ensure_eadb(context)
    venue_id = f"v_{handle}"
    context.eadb_dao.upsert_venue(Venue(
        venue_id=venue_id, venue_name=name, venue_lat=lat, venue_lng=lng,
        venue_address=address or "",
    ))
    context.eadb_dao.set_venue_instagram(
        VenueInstagram(venue_id=venue_id, instagram_handle=handle, status="found")
    )
    context.eadb_venues_by_name[name] = venue_id
    return venue_id


def _process_one_event(context, event_overrides: dict) -> str:
    """Runs ONE post through the real `_process_post`, programming exactly
    one event. Returns the shortcode used, so the caller can look the
    persisted row back up."""
    _ensure_eadb(context)
    context.eadb_shortcode_counter += 1
    shortcode = f"eadb_post_{context.eadb_shortcode_counter}"
    context.eadb_openai.program_events([_event_payload(**event_overrides)])

    caption = context.eadb_caption_text if getattr(context, "eadb_caption_text", None) else "Confira! Ingressos abertos."
    post = {
        "shortcode": shortcode, "caption": caption,
        "permalink": f"https://instagram.com/p/{shortcode}",
        "timestamp": context.eadb_now.isoformat(),
        "image_urls": [], "location_tag": context.eadb_location_tag,
    }

    if context.eadb_shared_handle_venues is not None:
        venues = context.eadb_shared_handle_venues
        handle_index = build_handle_index(context.eadb_dao)
        fallback = True
    else:
        venues = build_venue_catalog(context.eadb_dao)
        handle_index = build_handle_index(context.eadb_dao)
        fallback = False

    _run(context.eadb_service._process_post(
        handle=context.eadb_promoter_handle, post=post, venues=venues,
        handle_index=handle_index, now=context.eadb_now,
        location_text_fallback_to_caption=fallback,
    ))
    context.eadb_last_shortcode = shortcode
    return shortcode


def _eadb_event(context, shortcode: "str | None" = None) -> dict:
    shortcode = shortcode or context.eadb_last_shortcode
    row = context.eadb_dao.get_event_by_source(context.eadb_promoter_handle, shortcode)
    assert row is not None, f"no event stored for {context.eadb_promoter_handle}/{shortcode}"
    return row


# ── Background ────────────────────────────────────────────────────────────────
@given('the venues "{name1}" at handle "{handle1}"')
def step_given_the_venues_plural(context, name1, handle1):
    # The Background's first line reads "the venueS" (plural) even though
    # it names only one — a phrasing quirk introducing the concept, not a
    # multi-venue capture; behave still binds exactly one name/handle pair.
    #
    # Background steps re-run at the START of EVERY scenario in this
    # feature (unlike `_ensure_eadb`'s one-time dao/venue setup, which
    # deliberately persists so the three Background venues stay available
    # across scenarios) — the natural place to clear any PER-SCENARIO
    # state that must never leak from one scenario into the next.
    # `eadb_resolved` in particular is read by the shared "the event is
    # queued for review with reason ..." step to decide which of two
    # fixtures (§A/§B's persisted event vs §C/§D's bare resolver call)
    # this scenario used — a stale value from an earlier §D scenario would
    # silently mis-route a later §A/§B scenario onto the wrong branch.
    context.eadb_resolved = None
    _add_venue(context, name1, handle1)


@given('the venue "{name}" at handle "{handle}"')
def step_given_a_venue_at_handle(context, name, handle):
    _add_venue(context, name, handle)


# ── §A: per-event evidence outranks post-level evidence ──────────────────────
@given('a promoter post whose caption mentions "@{handle1}" first')
def step_given_caption_mentions_first(context, handle1):
    _ensure_eadb(context)
    context.eadb_caption_text = f"Roteiro da noite: @{handle1} bomba hoje! Ingressos abertos, vem cedo."


@given('an extracted event whose location text is "{location_text}"')
def step_given_event_location_text(context, location_text):
    _ensure_eadb(context)
    context.eadb_location_text = location_text


@given("an extracted event with no location text")
def step_given_event_no_location_text(context):
    _ensure_eadb(context)
    context.eadb_location_text = None


@given('a promoter post whose caption mentions "@{handle1}" and "@{handle2}"')
def step_given_caption_mentions_two(context, handle1, handle2):
    _ensure_eadb(context)
    context.eadb_caption_text = (
        f"Roteiro: @{handle1}, @{handle2} e muito mais! Ingressos abertos, vem cedo."
    )


@given('a promoter post whose caption mentions only "@{handle1}"')
def step_given_caption_mentions_only(context, handle1):
    _ensure_eadb(context)
    context.eadb_caption_text = f"Hoje: @{handle1} com festa boa! Ingressos abertos."


@given('a promoter post from "@{handle}" that is itself a known venue handle')
def step_given_promoter_post_from_known_handle(context, handle):
    _ensure_eadb(context)
    _add_venue(context, "Self Promoter Venue", handle)
    context.eadb_promoter_handle = handle
    # A self-mention in the caption must never resolve to the promoter's
    # own venue -- included here so the self-exclusion guard is genuinely
    # exercised, not merely absent-by-omission.
    context.eadb_caption_text = f"Segue a gente @{handle}! Hoje tem festa, ingressos abertos."


@given('a post tagged at "{venue_name}"')
def step_given_post_tagged_at(context, venue_name):
    _ensure_eadb(context)
    venue_id = context.eadb_venues_by_name[venue_name]
    venue = context.eadb_dao.get_venue(venue_id)
    context.eadb_location_tag = {
        "name": venue_name, "lat": venue.venue_lat, "lng": venue.venue_lng,
    }


@when("the event is attributed")
def step_when_the_event_is_attributed(context):
    _process_one_event(context, {"location_text": context.eadb_location_text})


@when("the events are attributed")
def step_when_the_events_are_attributed(context):
    # Handled entirely by the "20 events" Given step, which runs the post
    # itself (all 20 events must be extracted in ONE call, so there is
    # nothing left for a separate When to do). No-op kept only so the
    # Gherkin reads naturally.
    pass


@then('the event links to "{venue_name}"')
def step_then_event_links_to(context, venue_name):
    row = _eadb_event(context)
    expected_id = context.eadb_venues_by_name[venue_name]
    assert row["venue_id"] == expected_id, (venue_name, row)


@then("the event links to no venue")
def step_then_event_links_to_no_venue(context):
    row = _eadb_event(context)
    assert row["venue_id"] is None, row


@then("the event does not link to \"{venue_name}\"")
def step_then_event_does_not_link_to(context, venue_name):
    row = _eadb_event(context)
    unwanted_id = context.eadb_venues_by_name[venue_name]
    assert row["venue_id"] != unwanted_id, row


@then("the link method records that the mention came from the event's own text")
def step_then_link_from_event_text(context):
    row = _eadb_event(context)
    assert row["linked_by"] == "handle_mention", row


@then("the link method records that the mention came from the post caption")
def step_then_link_from_caption(context):
    row = _eadb_event(context)
    assert row["linked_by"] == "caption_handle_mention", row


@then('the event is queued for review with reason "{reason}"')
def step_then_queued_with_reason(context, reason):
    # Shared between §A/§B (the eadb_dao-persisted event), §D's "leave an
    # unreadable date unresolved and queued" (a bare resolve_event_datetime
    # call, never persisted at all), and plans/260813_review-gate-and-date-
    # vocabulary.md's own "still queue an event that states no date" (the
    # `context.ee_*` EventExtractionService fixture instagram_event_
    # extraction_steps.py builds) — `context.eadb_resolved`/`eadb_dao`
    # disambiguate the first two; a THIRD fixture never sets either, so it
    # falls to the `ee_*` branch instead of guessing at an uninitialised
    # `eadb_dao`.
    resolved = getattr(context, "eadb_resolved", None)
    if resolved is not None:
        assert reason in (resolved.review_reason or ""), resolved
        return
    if getattr(context, "eadb_dao", None) is not None:
        row = _eadb_event(context)
        assert reason in (row.get("review_reason") or ""), row
        return
    row = _stored_event(context)
    assert reason in (row.get("review_reason") or ""), row


@then("the ambiguous-caption refusal is counted")
def step_then_ambiguous_caption_counted(context):
    value = _metric(
        "event_venue_link_total",
        {"method": "ambiguous_caption_refusal", "result": "unresolved"},
    )
    assert value >= 1.0, value


@given("a promoter roundup post listing 20 events at 20 different venues")
def step_given_roundup_20_events(context):
    _ensure_eadb(context)
    context.eadb_roundup_venues = []
    for i in range(20):
        name = f"Roundup Venue {i}"
        handle = f"roundupvenue{i}"
        _add_venue(context, name, handle)
        context.eadb_roundup_venues.append((name, handle))


@given("each event's location text names its own venue handle")
def step_given_each_event_names_own_handle(context):
    _ensure_eadb(context)
    caption = "Roteiro da noite, ingressos abertos: " + ", ".join(
        f"@{handle}" for _name, handle in context.eadb_roundup_venues
    )
    context.eadb_caption_text = caption
    context.eadb_shortcode_counter += 1
    shortcode = f"eadb_roundup_{context.eadb_shortcode_counter}"
    events = [
        _event_payload(title=f"Evento {i}", location_text=f"@{handle}")
        for i, (_name, handle) in enumerate(context.eadb_roundup_venues)
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
    context.eadb_roundup_shortcode = shortcode


@then("each event links to the venue its own location text named")
def step_then_each_event_links_to_own_venue(context):
    rows = context.eadb_dao.list_events_by_source(
        context.eadb_promoter_handle, context.eadb_roundup_shortcode,
    )
    assert len(rows) == 20, rows
    for row, (name, _handle) in zip(rows, context.eadb_roundup_venues):
        expected_id = context.eadb_venues_by_name[name]
        assert row["venue_id"] == expected_id, (name, row)


@then("no two events link to the same venue")
def step_then_no_two_events_same_venue(context):
    rows = context.eadb_dao.list_events_by_source(
        context.eadb_promoter_handle, context.eadb_roundup_shortcode,
    )
    venue_ids = [r["venue_id"] for r in rows]
    assert len(set(venue_ids)) == len(venue_ids), venue_ids


# ── §B: two venues behind one account ─────────────────────────────────────────
@given('a crawl target "{handle}" mapped to "{venue_name}"')
def step_given_crawl_target_mapped_to(context, handle, venue_name):
    _ensure_eadb(context)
    venue_id = _add_venue(context, venue_name, handle, address=f"Av. Central, 1 - {venue_name.split()[-1]}, Recife")
    context.eadb_promoter_handle = handle
    context.eadb_shared_handle_first_venue = venue_id
    context.eadb_caption_text = "Confira nossa unidade hoje! Ingressos abertos."


@given('a sibling venue "{venue_name}" in the "{neighbourhood}" neighbourhood')
def step_given_sibling_venue_named_neighbourhood(context, venue_name, neighbourhood):
    _add_sibling_venue(context, venue_name, neighbourhood)


@given('a sibling venue "{venue_name}" in the Casa Forte neighbourhood')
def step_given_sibling_venue_casa_forte(context, venue_name):
    _add_sibling_venue(context, venue_name, "Casa Forte")


def _add_sibling_venue(context, venue_name: str, neighbourhood: str) -> None:
    _ensure_eadb(context)
    handle = context.eadb_promoter_handle
    venue_id = _add_venue(
        context, venue_name, f"{handle}_{len(context.eadb_venues_by_name)}",
        address=f"Rua Local, 99 - {neighbourhood}, Recife",
    )
    first_id = context.eadb_shared_handle_first_venue
    first_venue = context.eadb_dao.get_venue(first_id)
    from app.services.event_venue_resolution import VenueLite

    context.eadb_shared_handle_venues = [
        VenueLite(
            venue_id=first_id, venue_name=first_venue.venue_name,
            lat=first_venue.venue_lat, lng=first_venue.venue_lng,
            address=first_venue.venue_address,
        ),
        VenueLite(
            venue_id=venue_id, venue_name=venue_name,
            lat=-8.05, lng=-34.88, address=f"Rua Local, 99 - {neighbourhood}, Recife",
        ),
    ]


@given('a venue "{venue_name}" in the Casa Forte neighbourhood')
def step_given_unrelated_venue_in_neighbourhood(context, venue_name):
    _ensure_eadb(context)
    _add_venue(context, venue_name, f"unrelated_{len(context.eadb_venues_by_name)}",
               address="Rua Distante, 1 - Casa Forte, Recife")


@given('an extracted event whose location text names neither neighbourhood')
def step_given_location_text_names_neither(context):
    context.eadb_location_text = "endereço não identificado"


# ── §C/§D: dates — the model interprets, Python computes ────────────────────
# Direct calls to `app.services.event_date_resolver.resolve_event_datetime`
# — pure, deterministic, no DAO/service orchestration needed. Fixture
# strings the deterministic finders cannot read at all ("É HOJE", "Quinta
# (02)") are paired with the SAME plausible `date_interpretation` payload a
# real model call would return alongside that verbatim date_text — offline,
# there is no live model to ask, so this fixture stands in for it, exactly
# the same substitution every OTHER fake-OpenAI-client step in this suite
# already makes.
_KNOWN_INTERPRETATIONS = {
    "é hoje": {"kind": "relative", "relative": "hoje"},
}


def _interpretation_for(date_text: str, *, weekday: "str | None" = None, day: "int | None" = None) -> "dict | None":
    key = date_text.strip().lower()
    if key in _KNOWN_INTERPRETATIONS:
        return dict(_KNOWN_INTERPRETATIONS[key])
    if weekday is not None and day is not None:
        return {"kind": "weekday_day", "weekday": weekday, "day": day}
    return None


@given("a post published on {date_str}")
def step_given_post_published_on(context, date_str):
    year, month, day = (int(x) for x in date_str.split("-"))
    context.eadb_date_post_ts = datetime(year, month, day, 20, 0, tzinfo=RECIFE)
    context.eadb_date_interpretation = None
    # plans/260813_review-gate-and-date-vocabulary.md §B: reset alongside
    # `eadb_date_interpretation` above so a cadence fixture set by an
    # EARLIER scenario in this run (this file's own, or another feature
    # file's, since this step is the common entry point every "the date is
    # resolved" scenario starts from) never leaks into a later one that
    # never touches recurrence at all.
    context.eadb_is_recurring = None
    context.eadb_recurrence_text = None


@given('an extracted event whose date text is "{date_text}"')
def step_given_event_date_text(context, date_text):
    context.eadb_date_text = date_text
    weekday = None
    day = None
    if date_text.strip().lower().startswith("quinta"):
        weekday, day = "quinta", 2  # "Quinta (02)" — the only weekday+day fixture this feature uses
    context.eadb_date_interpretation = _interpretation_for(date_text, weekday=weekday, day=day)


@given("an extracted event whose date text cannot be interpreted")
def step_given_date_text_cannot_be_interpreted(context):
    context.eadb_date_text = "uma expressão de data totalmente ilegível"
    context.eadb_date_interpretation = None


@given("the model also returns an absolute date of {absolute_date}")
def step_given_model_also_returns_absolute_date(context, absolute_date):
    context.eadb_date_interpretation = dict(context.eadb_date_interpretation or {})
    context.eadb_date_interpretation["absolute_date"] = absolute_date


@when("the date is resolved")
def step_when_the_date_is_resolved(context):
    context.eadb_resolved = resolve_event_datetime(
        date_text=context.eadb_date_text, time_text=None,
        post_timestamp=context.eadb_date_post_ts,
        date_interpretation=context.eadb_date_interpretation,
        # plans/260813_review-gate-and-date-vocabulary.md §B: additive --
        # `getattr(..., None)` so every scenario that never sets these (every
        # scenario in THIS feature file, today) passes `None` for both,
        # identical to omitting them entirely (resolve_event_datetime's own
        # defaults). Only review-gate-and-date-vocabulary.feature's cadence
        # scenarios ever set them.
        is_recurring=getattr(context, "eadb_is_recurring", None),
        recurrence_text=getattr(context, "eadb_recurrence_text", None),
    )


@then("the event starts on {date_str}")
def step_then_event_starts_on(context, date_str):
    resolved = context.eadb_resolved
    assert resolved.starts_at is not None, resolved
    assert resolved.starts_at.date().isoformat() == date_str, resolved


@then("the event is flagged as a collapsed range")
def step_then_flagged_collapsed_range(context):
    assert context.eadb_resolved.date_range is True, context.eadb_resolved


@then("the event's year is flagged as inferred")
def step_then_flagged_inferred_year(context):
    # Reworded from the feature file's more obvious phrasing to avoid an
    # AmbiguousStep collision with date_correctness_and_path_parity_steps.py's
    # OWN "the event is flagged as having an inferred year" (a different
    # fixture, context.ee_dao) — the same collision-avoidance convention
    # event_ticket_info_and_attractions_steps.py's own docstring documents.
    assert context.eadb_resolved.year_inferred is True, context.eadb_resolved


@then("the event is flagged with a weekday mismatch")
def step_then_flagged_weekday_mismatch(context):
    assert context.eadb_resolved.review_reason == REASON_WEEKDAY_MISMATCH, context.eadb_resolved


@then("the event resolves to no start date")
def step_then_no_start_date(context):
    # Reworded to avoid an AmbiguousStep collision with
    # date_resolution_correctness_steps.py's own "the event has no start
    # date" (a different fixture, context.edr_resolved).
    assert context.eadb_resolved.starts_at is None, context.eadb_resolved




# ── §C: the determinism guard ────────────────────────────────────────────────
@given('a post already extracted with date text "{date_text}" resolving to {date_str}')
def step_given_post_already_extracted_resolving_to(context, date_text, date_str):
    year, month, day = (int(x) for x in date_str.split("-"))
    context.eadb_date_post_ts = datetime(year, month, day, 20, 0, tzinfo=RECIFE)
    context.eadb_date_text = date_text
    stored_interpretation = _interpretation_for(date_text)
    context.eadb_stored_date_text = date_text
    context.eadb_stored_interpretation = stored_interpretation
    first = resolve_event_datetime(
        date_text=date_text, time_text=None, post_timestamp=context.eadb_date_post_ts,
        date_interpretation=stored_interpretation,
    )
    assert first.starts_at is not None and first.starts_at.date().isoformat() == date_str, first
    from app.services.event_identity import compute_source_event_key

    context.eadb_first_resolved = first
    context.eadb_first_key = compute_source_event_key("Festa", first.starts_at)


@when("the same post is extracted again and the model returns a different interpretation")
def step_when_extracted_again_different_interpretation(context):
    from app.services.event_date_resolver import select_date_interpretation_for_reuse
    from app.services.event_identity import compute_source_event_key

    # The model's FRESH answer this run -- deliberately different from what
    # is stored, to prove the reuse guard, not the model's consistency,
    # is what keeps the outcome stable.
    fresh_interpretation = {"kind": "relative", "relative": "amanha"}
    interpretation_to_use = select_date_interpretation_for_reuse(
        fresh_date_text=context.eadb_date_text,
        fresh_interpretation=fresh_interpretation,
        stored_date_text=context.eadb_stored_date_text,
        stored_interpretation=context.eadb_stored_interpretation,
    )
    second = resolve_event_datetime(
        date_text=context.eadb_date_text, time_text=None,
        post_timestamp=context.eadb_date_post_ts,
        date_interpretation=interpretation_to_use,
    )
    context.eadb_resolved = second
    context.eadb_second_key = compute_source_event_key("Festa", second.starts_at)


@then("the event still starts on {date_str}")
def step_then_event_still_starts_on(context, date_str):
    assert context.eadb_resolved.starts_at is not None, context.eadb_resolved
    assert context.eadb_resolved.starts_at.date().isoformat() == date_str, context.eadb_resolved


@then("the source event key is unchanged")
def step_then_source_event_key_unchanged(context):
    assert context.eadb_second_key == context.eadb_first_key, (
        context.eadb_first_key, context.eadb_second_key,
    )


# ── §E: a greeting is not an event ───────────────────────────────────────────
def _ensure_ee(context) -> None:
    if getattr(context, "ee_dao", None) is None:
        _seed_ee_venue(context)


@given('a post whose caption reads "{caption}"')
def step_given_post_caption_reads(context, caption):
    _ensure_ee(context)
    _add_post(context, "eadb_kind_1", caption=caption, timestamp=datetime(2026, 8, 8, 20, 0, tzinfo=RECIFE))


@given("the post names a venue and a date but no lineup and no time")
def step_given_names_venue_and_date_no_lineup_no_time(context):
    # The live production defect the plan names verbatim: a birthday
    # greeting typed "event" despite having no lineup and no stated time.
    # This step programs the fake model's answer as the CORRECTED verdict
    # ("other") this plan's prompt work asks for — offline, there is no
    # live model to actually judge the caption, so this proves the parse/
    # store pipeline faithfully preserves an "other" verdict rather than
    # coercing it back to "event" somewhere downstream.
    context.ee_openai.program(json.dumps({"events": [{
        "kind": "other", "title": "31 Anos", "description": None,
        "date_text": "08/08", "time_text": None, "is_recurring": False,
        "recurrence_text": None, "lineup": [], "ticket_url": None,
        "price_text": None, "location_text": "Bar Tal", "confidence": 0.9,
    }]}))


@given("a post whose caption thanks everyone who came last Friday")
def step_given_caption_thanks_last_friday(context):
    _ensure_ee(context)
    _add_post(
        context, "eadb_kind_2",
        caption="Valeu a todo mundo que veio na sexta passada!",
        timestamp=datetime(2026, 8, 8, 20, 0, tzinfo=RECIFE),
    )


@given("the post names the venue, the date and the acts that played")
def step_given_names_venue_date_and_acts(context):
    context.ee_openai.program(json.dumps({"events": [{
        "kind": "other", "title": "Recap Sexta", "description": None,
        "date_text": "sexta passada", "time_text": None, "is_recurring": False,
        "recurrence_text": None, "lineup": ["DJ Recap"], "ticket_url": None,
        "price_text": None, "location_text": "Bar Tal", "confidence": 0.9,
    }]}))


@given("a post announcing a party on Friday with its lineup and door price")
def step_given_genuine_announcement(context):
    _ensure_ee(context)
    _add_post(
        context, "eadb_kind_3",
        caption="Festa de Sexta! Ingressos abertos.",
        timestamp=datetime(2026, 8, 8, 20, 0, tzinfo=RECIFE),
    )
    context.ee_openai.program(json.dumps({"events": [{
        "kind": "event", "title": "Festa de Sexta", "description": None,
        "date_text": "sexta", "time_text": "22h", "is_recurring": False,
        "recurrence_text": None, "lineup": ["DJ X"], "ticket_url": None,
        "price_text": "R$20", "location_text": None, "confidence": 0.9,
    }]}))


@given('a post announcing "Especial do dia" with a price and no lineup')
def step_given_daily_food_offer(context):
    _ensure_ee(context)
    _add_post(
        context, "eadb_kind_4",
        caption="Especial do dia! Confira o cardapio.",
        timestamp=datetime(2026, 8, 8, 20, 0, tzinfo=RECIFE),
    )
    context.ee_openai.program(json.dumps({"events": [{
        "kind": "promotion", "title": "Especial do dia", "description": None,
        "date_text": None, "time_text": None, "is_recurring": False,
        "recurrence_text": None, "lineup": [], "ticket_url": None,
        "price_text": "R$25", "location_text": None, "confidence": 0.9,
    }]}))


@then('the item\'s post type is "{post_type}"')
def step_then_item_post_type_is(context, post_type):
    row = _stored_event(context)
    assert row["post_type"] == post_type, row


@then('the item\'s post type is not "{post_type}"')
def step_then_item_post_type_is_not(context, post_type):
    row = _stored_event(context)
    assert row["post_type"] != post_type, row


