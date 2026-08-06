"""Behave steps for tests/bdd/enrichment/instagram-event-extraction.feature.

Drives the REAL EventExtractionService, event_date_resolver, post_qualifies
pre-filter, and the admin events API over a REAL VenueRepository backed by the
shared in-memory RDS fake (tests/rds_fake.py — the deterministic stand-in
required by AGENTS.md/CLAUDE.md; no live Apify/S3/OpenAI). The only
test-only fakes are the archived-post source (no live S3 manifest walking)
and the OpenAI event-extraction client (programmable raw JSON responses, with
a call counter that proves the cost-gate assertions).

Self-contained per scenario (own fresh store/service), mirroring
event_venue_targeting_steps.py's pattern, rather than the shared FastAPI
harness in environment.py — most scenarios exercise the service directly and
never need HTTP. The two scenarios that DO need HTTP (the admin API, and "the
served venue response is unchanged") build their own minimal app the same way
tests/bdd/steps/prev_day_weekly_forecast_steps.py does.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import fakeredis
from behave import given, when, then  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.redis_venue_dao import RedisVenueDAO
from app.dao.venue_repository import VenueRepository
from app.db.geo_redis_client import GeoRedisClient
from app.handlers import VenueHandler
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from app.routers.venue_router import router as venue_router
from app.routers.venue_router import set_venue_handler
from app.services.event_extraction_service import (
    ArchivedPost,
    EventExtractionService,
)

_LOOP: "asyncio.AbstractEventLoop | None" = None
RECIFE_LAT, RECIFE_LNG = -8.05, -34.88


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ── fakes at the true boundary (no live S3/OpenAI) ───────────────────────────
class _FakePostSource:
    """Posts injected directly by the scenario (no S3 manifest walking)."""

    def __init__(self):
        self.posts_by_venue: dict[str, list[ArchivedPost]] = {}

    async def posts_for_venue(self, venue_id, since):
        return self.posts_by_venue.get(venue_id, [])

    async def image_data_uri(self, key):
        return f"data:image/jpeg;base64,FAKE_{key}" if key else None


class _FakeOpenAIClient:
    """Programmed with one response (a raw JSON string, or an Exception) per
    call, in order. `.calls` is the cost-gate proof: it must stay 0 for a
    non-qualifying post.

    `EventExtractionService` now calls `extract_events` (plans/260806_venue-
    post-multi-event.md), never the singular `extract`. Every scenario in
    this file still programs a single flat event JSON (via `_extraction_json`)
    or an intentionally-invalid string — `extract_events` wraps a flat dict
    into the `{"events": [...]}` shape automatically (mirroring
    tests/bdd/steps/multi_event_posts_steps.py's fake), so a single-event
    post behaves exactly as it did before with zero changes to any existing
    Given step. An intentionally-truncated tuple (`program_truncated`) or a
    string that is not valid JSON at all is returned VERBATIM, so the real
    parser — not this fake — is what raises on it or reports it truncated."""

    def __init__(self):
        self._responses: list = []
        self.calls = 0

    def program(self, response) -> None:
        self._responses.append(response)

    def program_truncated(self, raw_text: str) -> None:
        self._responses.append(("__truncated__", raw_text))

    async def extract(self, *, caption, image_data_uri=None):
        self.calls += 1
        if not self._responses:
            raise AssertionError("fake OpenAI client called more times than programmed")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def extract_events(self, *, caption, image_data_uri=None, max_events):
        self.calls += 1
        if not self._responses:
            raise AssertionError("fake OpenAI client called more times than programmed")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple) and item and item[0] == "__truncated__":
            return item[1], True
        try:
            flat = json.loads(item)
        except (json.JSONDecodeError, TypeError):
            return item, False
        if isinstance(flat, dict) and "events" not in flat:
            return json.dumps({"events": [flat]}), False
        return item, False


def _extraction_json(**overrides) -> str:
    import json

    payload = {
        "title": "Festa", "description": None, "date_text": None,
        "time_text": None, "is_recurring": False, "recurrence_text": None,
        "lineup": ["DJ A"], "ticket_url": None, "price_text": "R$30",
        "location_text": None, "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


# ── context setup ─────────────────────────────────────────────────────────────
def _reset_context(context) -> None:
    from tests.rds_fake import InMemoryRdsVenueStore

    context.ee_store = InMemoryRdsVenueStore()
    context.ee_dao = VenueRepository(client=None, rds_store=context.ee_store)
    context.ee_post_source = _FakePostSource()
    context.ee_openai = _FakeOpenAIClient()
    context.ee_now: "datetime | None" = None
    context.ee_service = EventExtractionService(
        context.ee_dao, context.ee_post_source, context.ee_openai,
        min_confidence=0.5, flyer_confidence_floor=0.6,
        now_provider=lambda: context.ee_now or datetime.now(timezone.utc),
    )
    context.ee_venue_id = None
    context.ee_handle = None
    context.ee_run_config: dict = {}
    context.ee_result = None
    context.ee_last_shortcode = None
    context.ee_last_post_ts = None
    context.ee_pre_event_response = None


def _ensure_context(context) -> None:
    if getattr(context, "ee_dao", None) is None:
        _reset_context(context)


def _add_post(context, shortcode: str, **overrides) -> ArchivedPost:
    base = dict(
        shortcode=shortcode, permalink=f"https://instagram.com/p/{shortcode}",
        caption="Ingressos abertos! Vem pro role.", timestamp=None,
        flyer_photo_key=f"{shortcode}.jpg", flyer_confidence=0.9,
        any_photo_key=f"{shortcode}.jpg",
    )
    base.update(overrides)
    post = ArchivedPost(**base)
    context.ee_post_source.posts_by_venue.setdefault(context.ee_venue_id, []).append(post)
    context.ee_last_shortcode = shortcode
    context.ee_last_post_ts = post.timestamp
    return post


def _run_extraction(context, **overrides) -> dict:
    cfg = dict(context.ee_run_config or {})
    cfg.update(overrides)
    context.ee_result = _run(context.ee_service.run(cfg))
    return context.ee_result


def _stored_event(context) -> dict:
    row = context.ee_dao.get_event_by_source(context.ee_handle, context.ee_last_shortcode)
    assert row is not None, (
        f"no event stored for {context.ee_handle}/{context.ee_last_shortcode}"
    )
    return row


# ── Background ────────────────────────────────────────────────────────────────
@given("an event-candidate venue with an Instagram handle")
def step_given_an_event_candidate_venue_with_an_instagram_handle(context):
    _reset_context(context)
    vid, handle = "venue_1", "venue_1_ig"
    context.ee_dao.upsert_venue(Venue(
        venue_id=vid, venue_name="Test Venue", venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG,
    ))
    context.ee_dao.set_venue_instagram(
        VenueInstagram(venue_id=vid, instagram_handle=handle, status="found")
    )
    context.ee_dao.upsert_venue_event_profile(
        vid, tier="evidence_confirmed", category_pass=True, category_reason="NIGHTCLUB",
        evidence_score=5, evidence_sample=None, evaluated_at=datetime.now(timezone.utc),
    )
    context.ee_venue_id = vid
    context.ee_handle = handle


@given("its Instagram posts are archived with their captions and flyer images")
def step_given_its_instagram_posts_are_archived(context):
    _ensure_context(context)  # no-op fixture note; posts are added per-scenario


# ── per-scenario post fixtures ────────────────────────────────────────────────
@given("a post whose flyer announces a party with a title, a date and a time")
def step_given_a_post_whose_flyer_announces_a_party(context):
    ts = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)
    _add_post(context, "flyer_full", timestamp=ts)
    context.ee_openai.program(_extraction_json(
        title="Festa da Casa", date_text="20/08", time_text="22h", confidence=0.9,
    ))


@given("a post whose image is a plate of food")
def step_given_a_post_whose_image_is_a_plate_of_food(context):
    _add_post(
        context, "food_plate", caption="Nosso prato do dia é maravilhoso",
        flyer_photo_key=None, flyer_confidence=None,
    )


@given("its caption carries no event marker")
def step_given_its_caption_carries_no_event_marker(context):
    pass  # already set by the previous step; nothing further to arrange


@given('a post published on a Thursday whose caption says "amanhã"')
def step_given_a_post_published_on_a_thursday_whose_caption_says_amanha(context):
    # 2026-07-16 is a Thursday.
    ts = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "relative_amanha", caption="Vem amanhã! Ingressos na porta.", timestamp=ts)
    context.ee_openai.program(_extraction_json(date_text="amanhã", time_text="20h"))


@given("the extraction runs three weeks after that post")
def step_given_the_extraction_runs_three_weeks_after_that_post(context):
    context.ee_now = context.ee_last_post_ts + timedelta(weeks=3)


@given('a post published in July 2026 whose flyer says "15/08"')
def step_given_a_post_published_in_july_2026_whose_flyer_says_1508(context):
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "no_year_july", timestamp=ts)
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="20h"))


@given('a post published in December 2026 whose flyer says "15/08"')
def step_given_a_post_published_in_december_2026_whose_flyer_says_1508(context):
    ts = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "no_year_december", timestamp=ts)
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="20h"))


@given('a post whose flyer says "05/08"')
def step_given_a_post_whose_flyer_says_0508(context):
    ts = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "day_first", timestamp=ts)
    context.ee_openai.program(_extraction_json(date_text="05/08", time_text="20h"))


@given("a post whose flyer announces an event with no determinable date")
def step_given_a_post_whose_flyer_announces_an_event_with_no_determinable_date(context):
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "no_date", timestamp=ts)
    context.ee_openai.program(_extraction_json(date_text=None, time_text=None, confidence=0.8))


@given('a post published on a Monday whose caption says "toda quinta"')
def step_given_a_post_published_on_a_monday_whose_caption_says_toda_quinta(context):
    # 2026-07-13 is a Monday.
    ts = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "recurring", caption="Toda quinta tem samba! Ingressos na entrada.", timestamp=ts)
    context.ee_openai.program(_extraction_json(
        date_text="toda quinta", time_text="22h",
        is_recurring=True, recurrence_text="toda quinta",
    ))


@given('a post whose flyer says the event starts at "22h"')
def step_given_a_post_whose_flyer_says_the_event_starts_at_22h(context):
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "time_only", timestamp=ts)
    # The flyer is understood in context as naming that same day — the model
    # never invents a date; "hoje" here is what the fixture's flyer states.
    context.ee_openai.program(_extraction_json(date_text="hoje", time_text="22h"))


@given("a post already extracted into an event")
def step_given_a_post_already_extracted_into_an_event(context):
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "reextract", timestamp=ts)
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="20h"))
    _run_extraction(context)
    context.ee_first_result = context.ee_dao.get_event_by_source(
        context.ee_handle, context.ee_last_shortcode,
    )


@given("an event an operator has confirmed with a corrected title")
def step_given_an_event_an_operator_has_confirmed_with_a_corrected_title(context):
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "confirmed_post", timestamp=ts)
    context.ee_openai.program(_extraction_json(title="Original Title", date_text="15/08", time_text="20h"))
    _run_extraction(context)
    stored = _stored_event(context)
    context.ee_dao.update_event(stored["event_id"], {
        "status": "confirmed", "title": "Corrected by operator",
    })


@given("a post that qualifies for extraction")
def step_given_a_post_that_qualifies_for_extraction(context):
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "will_fail", timestamp=ts)
    # A second, healthy qualifying post proves the run continues past the
    # failure (see the "run continues" Then step below).
    _add_post(context, "after_failure", timestamp=ts)


@given("the model returns unparseable output for it")
def step_given_the_model_returns_unparseable_output_for_it(context):
    context.ee_openai.program("not valid json at all {{{")
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="20h"))


@given("a post whose extraction confidence is below the minimum")
def step_given_a_post_whose_extraction_confidence_is_below_the_minimum(context):
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "low_conf", timestamp=ts)
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="20h", confidence=0.1))


@given("10 archived posts of which 4 qualify")
def step_given_10_archived_posts_of_which_4_qualify(context):
    for i in range(4):
        _add_post(
            context, f"qualifying_{i}", caption="Ingressos abertos!",
            flyer_photo_key=None, flyer_confidence=None,
        )
    for i in range(6):
        _add_post(
            context, f"nonqualifying_{i}", caption="Nosso prato do dia",
            flyer_photo_key=None, flyer_confidence=None,
        )


# ── admin API / public API fixtures ──────────────────────────────────────────
def _build_admin_events_app(context) -> None:
    app = FastAPI()
    app.include_router(admin_events_router)
    set_events_container(type("C", (), {"pipeline_repository": context.ee_dao})())
    context.ee_client = TestClient(app)


@given('an event with the status "pending_review"')
def step_given_an_event_with_the_status_pending_review(context):
    # Both posts added BEFORE the one run: a second `_run_extraction` call
    # would reprocess "confirm_me" too (it stays in the venue's post list),
    # which would silently consume a response meant for "reject_me" and
    # corrupt it to extraction_failed before the admin API ever touches it.
    ts = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    _add_post(context, "confirm_me", timestamp=ts)
    _add_post(context, "reject_me", timestamp=ts)
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="20h"))
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="20h"))
    _run_extraction(context)

    context.ee_confirm_event_id = context.ee_dao.get_event_by_source(
        context.ee_handle, "confirm_me",
    )["event_id"]
    context.ee_reject_event_id = context.ee_dao.get_event_by_source(
        context.ee_handle, "reject_me",
    )["event_id"]

    _build_admin_events_app(context)


@given("events exist for a venue")
def step_given_events_exist_for_a_venue(context):
    _ensure_context(context)
    # Real Redis-backed serving stack (fakeredis) for the public API side,
    # sharing the SAME rds_store so events land where VenueRepository reads.
    context.ee_fake_redis = fakeredis.FakeRedis(decode_responses=True)
    context.ee_geo_redis = GeoRedisClient(context.ee_fake_redis)
    context.ee_repository = VenueRepository(context.ee_geo_redis, rds_store=context.ee_store)
    context.ee_repository.upsert_venue(Venue(
        venue_id=context.ee_venue_id, venue_name="Test Venue",
        venue_lat=RECIFE_LAT, venue_lng=RECIFE_LNG,
    ))

    handler = VenueHandler(context.ee_repository, admin_config_service=None)
    set_venue_handler(handler)
    app = FastAPI()
    app.include_router(venue_router)
    context.ee_public_client = TestClient(app)

    query = {"lat": RECIFE_LAT, "lon": RECIFE_LNG, "radius": 5.0}
    context.ee_pre_event_response = context.ee_public_client.get(
        "/v1/venues/nearby", params=query,
    )

    context.ee_dao.insert_event({
        "event_id": "evt_served_test", "venue_id": context.ee_venue_id,
        "source_kind": "venue_post", "source_handle": context.ee_handle,
        "source_shortcode": "served_test", "status": "pending_review",
    })


# ── when ──────────────────────────────────────────────────────────────────────
@when("event extraction runs")
def step_when_event_extraction_runs(context):
    _run_extraction(context)


@when("event extraction runs as a dry run")
def step_when_event_extraction_runs_as_a_dry_run(context):
    _run_extraction(context, dry_run=True)


@when("event extraction runs again over that post")
def step_when_event_extraction_runs_again_over_that_post(context):
    # A fresh programmed response for the SECOND pass over the venue's post
    # list (which still includes the first post) — without this, the fake
    # client would raise "called more times than programmed" and the run
    # would be misread as a genuine re-extraction success.
    context.ee_openai.program(_extraction_json(date_text="15/08", time_text="20h"))
    _run_extraction(context)


@when("event extraction runs again over its post")
def step_when_event_extraction_runs_again_over_its_post(context):
    # Same date_text as the original extraction ("15/08") — only the title
    # diverges. The shared reconciliation's confirmed-row match now requires
    # EXACTLY ONE of (title, resolved date) to change to still recognize
    # this as the SAME event (plans/260806_venue-post-multi-event.md's
    # pairing fallback): a fresh answer that changes BOTH is, correctly,
    # indistinguishable from an unrelated event replacing this one, and is
    # no longer absorbed into the confirmed row. This scenario is about the
    # title diverging, so the date is held constant to isolate exactly that.
    context.ee_openai.program(_extraction_json(
        title="A completely different title", date_text="15/08", time_text="20h",
    ))
    _run_extraction(context)


@when("the operator confirms it")
def step_when_the_operator_confirms_it(context):
    context.ee_confirm_response = context.ee_client.post(
        f"/admin/events/{context.ee_confirm_event_id}/confirm"
    )


@when("the operator rejects another pending event")
def step_when_the_operator_rejects_another_pending_event(context):
    context.ee_reject_response = context.ee_client.post(
        f"/admin/events/{context.ee_reject_event_id}/reject"
    )


@when("the venue is served through the public API")
def step_when_the_venue_is_served_through_the_public_api(context):
    query = {"lat": RECIFE_LAT, "lon": RECIFE_LNG, "radius": 5.0}
    context.ee_post_event_response = context.ee_public_client.get(
        "/v1/venues/nearby", params=query,
    )


# ── then ──────────────────────────────────────────────────────────────────────
@then("an event is stored for that venue")
def step_then_an_event_is_stored_for_that_venue(context):
    events = context.ee_dao.list_events(venue_id=context.ee_venue_id)
    assert len(events) >= 1, "expected at least one stored event"


@then("the event carries the title from the flyer")
def step_then_the_event_carries_the_title_from_the_flyer(context):
    assert _stored_event(context)["title"] == "Festa da Casa"


@then("the event carries a start time")
def step_then_the_event_carries_a_start_time(context):
    assert _stored_event(context)["starts_at"] is not None


@then("the event records the permalink of the post it came from")
def step_then_the_event_records_the_permalink(context):
    row = _stored_event(context)
    assert row["source_permalink"] == f"https://instagram.com/p/{context.ee_last_shortcode}"


@then("no model call is made for that post")
def step_then_no_model_call_is_made_for_that_post(context):
    assert context.ee_openai.calls == 0


@then('that post is counted with the outcome "{outcome}"')
def step_then_that_post_is_counted_with_the_outcome(context, outcome):
    assert context.ee_result is not None
    assert context.ee_result["outcomes"].get(outcome, 0) >= 1, context.ee_result["outcomes"]


@then("the event starts on the Friday after the post was published")
def step_then_the_event_starts_on_the_friday_after_the_post(context):
    row = _stored_event(context)
    assert row["starts_at"].date().isoformat() == "2026-07-17"


@then("the event does not start on the day after the run")
def step_then_the_event_does_not_start_on_the_day_after_the_run(context):
    row = _stored_event(context)
    day_after_run = (context.ee_now + timedelta(days=1)).date()
    assert row["starts_at"].date() != day_after_run


@then("the event starts on 15 August 2026")
def step_then_the_event_starts_on_15_august_2026(context):
    assert _stored_event(context)["starts_at"].date().isoformat() == "2026-08-15"


@then("the event starts on 15 August 2027")
def step_then_the_event_starts_on_15_august_2027(context):
    assert _stored_event(context)["starts_at"].date().isoformat() == "2027-08-15"


@then("the event starts on 5 August")
def step_then_the_event_starts_on_5_august(context):
    row = _stored_event(context)
    assert row["starts_at"].month == 8
    assert row["starts_at"].day == 5


@then("the event does not start on 5 May")
def step_then_the_event_does_not_start_on_5_may(context):
    row = _stored_event(context)
    assert not (row["starts_at"].month == 5 and row["starts_at"].day == 5)


@then("the event is stored with no start time")
def step_then_the_event_is_stored_with_no_start_time(context):
    assert _stored_event(context)["starts_at"] is None


@then('the event has the status "{status}"')
def step_then_the_event_has_the_status(context, status):
    assert _stored_event(context)["status"] == status


@then("the review reason names the missing date")
def step_then_the_review_reason_names_the_missing_date(context):
    reason = _stored_event(context)["review_reason"] or ""
    assert "date" in reason.lower(), reason


@then("the event is marked recurring")
def step_then_the_event_is_marked_recurring(context):
    assert _stored_event(context)["is_recurring"] is True


@then("the event records the recurrence as stated")
def step_then_the_event_records_the_recurrence_as_stated(context):
    assert _stored_event(context)["recurrence_text"] == "toda quinta"


@then("the event starts on the Thursday of that week")
def step_then_the_event_starts_on_the_thursday_of_that_week(context):
    assert _stored_event(context)["starts_at"].date().isoformat() == "2026-07-16"


@then("the event starts at 22:00 America/Recife")
def step_then_the_event_starts_at_2200_america_recife(context):
    from zoneinfo import ZoneInfo

    row = _stored_event(context)
    starts_at = row["starts_at"]
    assert starts_at.hour == 22
    assert starts_at.utcoffset() == ZoneInfo("America/Recife").utcoffset(starts_at)


@then("exactly one event exists for that post")
def step_then_exactly_one_event_exists_for_that_post(context):
    events = [
        e for e in context.ee_dao.list_events(venue_id=context.ee_venue_id)
        if e["source_shortcode"] == context.ee_last_shortcode
    ]
    assert len(events) == 1, events


@then("its last seen timestamp is updated")
def step_then_its_last_seen_timestamp_is_updated(context):
    before = context.ee_first_result["last_seen_at"]
    after = _stored_event(context)["last_seen_at"]
    assert after >= before


@then("the corrected title is unchanged")
def step_then_the_corrected_title_is_unchanged(context):
    assert _stored_event(context)["title"] == "Corrected by operator"


@then("the divergence between the model and the operator is flagged")
def step_then_the_divergence_between_the_model_and_the_operator_is_flagged(context):
    row = _stored_event(context)
    assert row["status"] == "confirmed"
    assert row["review_reason"] == "model_diverges_from_confirmed_record"
    assert row["raw_extraction"]["title"] == "A completely different title"


@then("the raw model output is kept")
def step_then_the_raw_model_output_is_kept(context):
    row = context.ee_dao.get_event_by_source(context.ee_handle, "will_fail")
    assert row is not None
    assert row["raw_extraction"]["raw_response"] == "not valid json at all {{{"


@then("the run continues to the next post")
def step_then_the_run_continues_to_the_next_post(context):
    row = context.ee_dao.get_event_by_source(context.ee_handle, "after_failure")
    assert row is not None
    assert row["status"] != "extraction_failed"


@then("the review reason names the low confidence")
def step_then_the_review_reason_names_the_low_confidence(context):
    reason = _stored_event(context)["review_reason"] or ""
    assert "confidence" in reason.lower(), reason


@then("{n:d} posts are reported as qualifying")
def step_then_n_posts_are_reported_as_qualifying(context, n):
    assert context.ee_result["qualifying_posts"] == n, context.ee_result


@then("no model call is made during the dry run")
def step_then_no_model_call_is_made_during_the_dry_run(context):
    assert context.ee_openai.calls == 0


@then("no event is written")
def step_then_no_event_is_written(context):
    assert context.ee_dao.list_events() == []


@then('its status is "{status}"')
def step_then_its_status_is(context, status):
    assert context.ee_confirm_response.status_code == 200, context.ee_confirm_response.text
    assert context.ee_confirm_response.json()["status"] == status
    stored = context.ee_dao.get_event(context.ee_confirm_event_id)
    assert stored["status"] == status


@then("that event's status is \"{status}\"")
def step_then_that_events_status_is(context, status):
    assert context.ee_reject_response.status_code == 200, context.ee_reject_response.text
    assert context.ee_reject_response.json()["status"] == status
    stored = context.ee_dao.get_event(context.ee_reject_event_id)
    assert stored["status"] == status


@then("the response is unchanged from before events existed")
def step_then_the_response_is_unchanged_from_before_events_existed(context):
    assert context.ee_pre_event_response.status_code == context.ee_post_event_response.status_code
    assert context.ee_pre_event_response.json() == context.ee_post_event_response.json()
