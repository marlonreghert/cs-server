"""Behave steps for tests/bdd/enrichment/multi-event-posts.feature.

Drives the REAL PromoterCrawlService, the resolution ladder
(app/services/event_venue_resolution.py), and event_identity.compute_source_
event_key over a REAL VenueRepository backed by the shared in-memory RDS fake
(tests/rds_fake.py — the deterministic stand-in required by CLAUDE.md; no
live Apify/S3/OpenAI). The only test-only fakes are at the true external
boundary: the Apify posts client, the OpenAI extraction client, and the HTTP
image downloader — mirroring instagram_promoter_events_steps.py's pattern.

The fake OpenAI client raises `AssertionError` the moment it is called more
times than programmed, rather than returning a default — a silent default
here is exactly the false-green trap a `program()`-per-call contract exists
to catch (per this repo's convention; see the sibling promoter-events steps).

The acceptance case this whole plan exists for: `@recifequecabenobolso`'s
2026-08-05 post announcing three events at three different venues (Cinema
São Luiz, RioMar, Terra) in one caption with NO @-mentions — so every event
falls to fuzzy name matching (ladder rung 3), never the free identity rung.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.dao.venue_repository import VenueRepository
from app.metrics import EVENT_EXTRACTION_MALFORMED_EVENTS_TOTAL
from app.models.venue import Venue
from app.services.promoter_crawl_service import PromoterCrawlService
from tests.rds_fake import InMemoryRdsVenueStore

_LOOP: "asyncio.AbstractEventLoop | None" = None
RECIFE_LAT, RECIFE_LNG = -8.05, -34.88

# The real evidence caption (plans/260806_multi-event-posts.md), with an
# explicit numeric date added so the free caption-marker cost-gate
# (matches_event_marker) admits it — the original wording alone carries
# neither a date/month pair, a weekday+time, nor a ticketing term, so a
# byte-for-byte copy would be rejected before ever reaching the model.
ROUNDUP_CAPTION = (
    "Quarta (05/08) com exibição dO Homem do Fraque Verde no Cinema São Luiz, "
    "Adilson Ramos no RioMar, Khrystal no Terra e muito mais #quarta "
    "#cinemasaoluiz #recifeantigo #recife #olinda"
)
GENERIC_EVENT_CAPTION = "Ingressos abertos! Vem pro role de hoje."


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ── fakes at the true boundary (no live Apify/S3/OpenAI) ─────────────────────
class _FakePromoterPostsClient:
    def __init__(self):
        self.posts_by_handle: dict[str, list[dict]] = {}

    async def fetch_recent_posts(self, handle: str, *, results_limit: int) -> list[dict]:
        return list(self.posts_by_handle.get(handle, []))


class _FakeMultiEventOpenAIClient:
    """Programmed with one response per call, IN ORDER. Each programmed item
    is either a raw `{"events": [...]}` JSON string (non-truncated) or a
    `("__truncated__", raw_text)` pair. Exhaustion RAISES rather than
    returning a default."""

    def __init__(self):
        self._responses: list = []
        self.calls = 0

    def program(self, raw_json: str) -> None:
        self._responses.append(raw_json)

    def program_truncated(self, raw_text: str) -> None:
        self._responses.append(("__truncated__", raw_text))

    async def extract_events(self, *, caption, image_data_uri=None, max_events):
        self.calls += 1
        if not self._responses:
            raise AssertionError("fake OpenAI client called more times than programmed")
        item = self._responses.pop(0)
        if isinstance(item, tuple) and item and item[0] == "__truncated__":
            return item[1], True
        return item, False


class _FakeDownloader:
    async def download(self, url, timeout=15.0, max_bytes=None):
        return b"FAKE_IMAGE_BYTES", "image/jpeg"


def _events_json(events: list[dict]) -> str:
    base = {
        "description": None, "date_text": None, "time_text": None,
        "is_recurring": False, "recurrence_text": None, "lineup": [],
        "ticket_url": None, "price_text": None, "location_text": None,
        "confidence": 0.9,
    }
    return json.dumps({"events": [{**base, **e} for e in events]})


# ── context setup (Background resets everything fresh per scenario) ─────────
def _reset_context(context) -> None:
    context.mep_store = InMemoryRdsVenueStore()
    context.mep_dao = VenueRepository(client=None, rds_store=context.mep_store)
    context.mep_posts_client = _FakePromoterPostsClient()
    context.mep_openai = _FakeMultiEventOpenAIClient()
    context.mep_downloader = _FakeDownloader()
    context.mep_now = datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc)
    context.mep_service = PromoterCrawlService(
        venue_dao=context.mep_dao,
        posts_client=context.mep_posts_client,
        media_store=None,
        downloader=context.mep_downloader,
        openai_client=context.mep_openai,
        confidence_floor=0.55,
        margin=0.08,
        min_confidence=0.0,
        max_events_per_post=20,
        now_provider=lambda: context.mep_now,
    )
    context.mep_handle = "recifequecabenobolso"
    context.mep_shortcode = "Dbo4GGvDunO"
    context.mep_venues_by_name: dict[str, str] = {}
    context.mep_last_report = None
    context.mep_event_ids_by_title: dict[str, str] = {}
    context.mep_metric_snapshot: dict = {}


def _ensure_context(context) -> None:
    if getattr(context, "mep_dao", None) is None:
        _reset_context(context)


def _create_venue(context, name: str, *, lat=RECIFE_LAT, lng=RECIFE_LNG) -> str:
    venue_id = f"mep_venue_{len(context.mep_venues_by_name) + 1}"
    context.mep_dao.upsert_venue(Venue(venue_id=venue_id, venue_name=name, venue_lat=lat, venue_lng=lng))
    context.mep_venues_by_name[name] = venue_id
    return venue_id


def _seed_post(context, *, caption: str, timestamp=None) -> None:
    post = {
        "shortcode": context.mep_shortcode,
        "caption": caption,
        "permalink": f"https://instagram.com/p/{context.mep_shortcode}",
        "timestamp": (timestamp or context.mep_now).isoformat(),
        "image_urls": ["https://cdn.example.com/img.jpg"],
        "location_tag": None,
    }
    context.mep_posts_client.posts_by_handle[context.mep_handle] = [post]


def _run_crawl(context) -> None:
    context.mep_last_report = _run(context.mep_service.run({}))


def _rows(context) -> list[dict]:
    return context.mep_dao.list_events_by_source(context.mep_handle, context.mep_shortcode)


def _row_by_title(context, title: str) -> dict:
    row = next((r for r in _rows(context) if r["title"] == title), None)
    assert row is not None, f"no persisted event titled {title!r}; rows={_rows(context)}"
    return row


def _snapshot_metric(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or None) or 0.0


# ── Background ────────────────────────────────────────────────────────────────
@given("an active promoter account")
def step_given_an_active_promoter_account(context):
    _reset_context(context)
    context.mep_dao.upsert_promoter_account(context.mep_handle, {"status": "active"})


@given("the catalog holds the venues named in its posts")
def step_given_the_catalog_holds_the_venues_named_in_its_posts(context):
    # Venue creation happens in each scenario's own Given step, once the
    # scenario knows which venues its post actually names.
    _ensure_context(context)


# ── Scenarios 1, 2, 14: the roundup — three events, three venues ────────────
@given("a post whose caption announces three events at three different venues")
def step_given_a_post_announcing_three_events_at_three_venues(context):
    _ensure_context(context)
    _create_venue(context, "Cinema São Luiz")
    _create_venue(context, "RioMar")
    _create_venue(context, "Terra")
    _seed_post(context, caption=ROUNDUP_CAPTION)
    context.mep_openai.program(_events_json([
        {"title": "O Homem do Fraque Verde", "date_text": "Quarta (05)", "location_text": "Cinema São Luiz"},
        {"title": "Adilson Ramos", "date_text": "Quarta (05)", "location_text": "RioMar"},
        {"title": "Khrystal", "date_text": "Quarta (05)", "location_text": "Terra"},
    ]))
    # Prometheus metrics are process-global and accumulate across scenarios
    # in the same behave run — snapshot the baseline here, before extraction
    # runs, so the Then step can assert the DELTA this scenario caused.
    context.mep_metric_snapshot["events_per_post_sum"] = _snapshot_metric(
        "event_extraction_events_per_post_sum",
    )


@when("the multi-event extraction runs")
def step_when_event_extraction_runs(context):
    _run_crawl(context)


@then("three events are persisted for that post")
def step_then_three_events_are_persisted(context):
    assert len(_rows(context)) == 3, _rows(context)


@then("three events exist for that post")
def step_then_three_events_exist_for_that_post(context):
    assert len(_rows(context)) == 3, _rows(context)


@then("each event carries its own title")
def step_then_each_event_carries_its_own_title(context):
    # Data-driven (not hardcoded to this scenario's specific titles) so this
    # generic phrase is reusable by any other scenario that persists several
    # events for one post — e.g. tests/bdd/enrichment/venue-post-multi-
    # event.feature's venue-side equivalent. The guarantee itself is
    # unchanged: every row got the model's OWN title, not a sibling's or a
    # collapsed default — proven by every row having a distinct, non-empty
    # title.
    rows = _rows(context)
    titles = {r["title"] for r in rows}
    assert all(titles), rows
    assert len(titles) == len(rows), rows


@then("the three events are linked to three different venues")
def step_then_the_three_events_are_linked_to_three_different_venues(context):
    venue_ids = {r["venue_id"] for r in _rows(context)}
    assert None not in venue_ids, _rows(context)
    assert len(venue_ids) == 3, _rows(context)
    assert venue_ids == {
        context.mep_venues_by_name["Cinema São Luiz"],
        context.mep_venues_by_name["RioMar"],
        context.mep_venues_by_name["Terra"],
    }


@then("the events-per-post measurement records three for that post")
def step_then_the_events_per_post_measurement_records_three(context):
    before = context.mep_metric_snapshot.get("events_per_post_sum", 0.0)
    after = _snapshot_metric("event_extraction_events_per_post_sum")
    assert after - before == 3.0, (before, after)


# ── Scenario 3: independent date resolution ──────────────────────────────────
@given("a post announcing one event today and one event on a later date")
def step_given_a_post_announcing_one_event_today_and_one_later(context):
    _ensure_context(context)
    _seed_post(context, caption=GENERIC_EVENT_CAPTION)
    context.mep_openai.program(_events_json([
        {"title": "Evento de Hoje", "date_text": "hoje"},
        {"title": "Evento Futuro", "date_text": "15/08"},
    ]))


@then("the two events carry different start dates")
def step_then_the_two_events_carry_different_start_dates(context):
    # Data-driven (not hardcoded to this scenario's specific titles/dates) so
    # this generic phrase is reusable by any other scenario asserting
    # independent per-event date resolution — e.g. tests/bdd/enrichment/
    # venue-post-multi-event.feature's venue-side equivalent.
    rows = sorted(_rows(context), key=lambda r: r["starts_at"])
    assert len(rows) == 2, rows
    assert rows[0]["starts_at"] is not None and rows[1]["starts_at"] is not None
    assert rows[0]["starts_at"] != rows[1]["starts_at"]


@then("both dates are resolved against the post timestamp")
def step_then_both_dates_resolved_against_post_timestamp(context):
    # The earlier event is anchored directly to the post's own timestamp
    # ("today"); the later one forward-fills to its next occurrence on or
    # after that SAME anchor — never the run clock. Comparing against
    # `context.mep_now` (the fixture's own source of truth for the post
    # timestamp) rather than a hardcoded literal date keeps this reusable —
    # see the sibling assertion above.
    rows = sorted(_rows(context), key=lambda r: r["starts_at"])
    post_ts = context.mep_now
    assert rows[0]["starts_at"].date() == post_ts.date()
    assert rows[1]["starts_at"].date() > post_ts.date()


# ── Scenario 4: single-party flyer, unchanged behaviour ──────────────────────
@given("a post whose flyer announces a single party")
def step_given_a_post_whose_flyer_announces_a_single_party(context):
    _ensure_context(context)
    _seed_post(context, caption=GENERIC_EVENT_CAPTION)
    context.mep_openai.program(_events_json([{"title": "Festa Única"}]))


@then("exactly one event is persisted for that post")
def step_then_exactly_one_event_is_persisted(context):
    assert len(_rows(context)) == 1, _rows(context)


# ── Scenarios 5, 6, 8: a post that already produced three events ────────────
@given("a post that already produced three events")
def step_given_a_post_that_already_produced_three_events(context):
    step_given_a_post_announcing_three_events_at_three_venues(context)
    _run_crawl(context)
    assert len(_rows(context)) == 3, _rows(context)
    context.mep_event_ids_by_title = {r["title"]: r["event_id"] for r in _rows(context)}


@when("the multi-event extraction runs again and the model returns them in a different order")
def step_when_event_extraction_runs_again_reordered(context):
    context.mep_openai.program(_events_json([
        {"title": "Khrystal", "date_text": "Quarta (05)", "location_text": "Terra"},
        {"title": "O Homem do Fraque Verde", "date_text": "Quarta (05)", "location_text": "Cinema São Luiz"},
        {"title": "Adilson Ramos", "date_text": "Quarta (05)", "location_text": "RioMar"},
    ]))
    _run_crawl(context)


@then("no duplicate event is created")
def step_then_no_duplicate_event_is_created(context):
    current_ids = {r["event_id"] for r in _rows(context)}
    assert current_ids == set(context.mep_event_ids_by_title.values()), (
        current_ids, context.mep_event_ids_by_title,
    )


@given("the operator confirmed one of them with a corrected title")
def step_given_the_operator_confirmed_one_with_a_corrected_title(context):
    # Any one of the several existing events — WHICH one is irrelevant to
    # this scenario's guarantee (a confirmed row's operator-corrected title
    # survives whichever of them the next extraction reorders). Picking
    # deterministically (sorted by title) rather than hardcoding a specific
    # title keeps this reusable by any other scenario with its own fixture
    # titles — e.g. tests/bdd/enrichment/venue-post-multi-event.feature's
    # venue-side equivalent — while still selecting "Adilson Ramos" (and
    # "Adilson Ramos - CORRIGIDO") for THIS scenario's own three titles,
    # unchanged from before.
    original_title, event_id = sorted(context.mep_event_ids_by_title.items())[0]
    context.mep_confirmed_event_id = event_id
    context.mep_confirmed_title = f"{original_title} - CORRIGIDO"
    context.mep_dao.update_event(event_id, {
        "status": "confirmed", "title": context.mep_confirmed_title,
    })


@then("the confirmed event's corrected title is unchanged")
def step_then_the_corrected_title_is_unchanged(context):
    row = context.mep_dao.get_event(context.mep_confirmed_event_id)
    assert row is not None
    assert row["title"] == context.mep_confirmed_title, row


@then("it is still attached to the same event")
def step_then_it_is_still_attached_to_the_same_event(context):
    # Re-extraction must find and update the SAME row by content-derived
    # key, never insert a second one for the confirmed event's content.
    matching = [r for r in _rows(context) if r["event_id"] == context.mep_confirmed_event_id]
    assert len(matching) == 1, _rows(context)
    assert matching[0]["title"] == context.mep_confirmed_title


@when("the multi-event extraction runs again and returns only two of them")
def step_when_event_extraction_runs_again_with_only_two(context):
    context.mep_openai.program(_events_json([
        {"title": "O Homem do Fraque Verde", "date_text": "Quarta (05)", "location_text": "Cinema São Luiz"},
        {"title": "Adilson Ramos", "date_text": "Quarta (05)", "location_text": "RioMar"},
    ]))
    _run_crawl(context)


@then('the missing event has the status "{status}"')
def step_then_the_missing_event_has_the_status(context, status):
    missing_id = context.mep_event_ids_by_title["Khrystal"]
    row = context.mep_dao.get_event(missing_id)
    assert row is not None
    assert row["status"] == status, row


@then("the missing event is not deleted")
def step_then_the_missing_event_is_not_deleted(context):
    missing_id = context.mep_event_ids_by_title["Khrystal"]
    assert context.mep_dao.get_event(missing_id) is not None


# ── Scenario 7: manual link survives re-extraction ───────────────────────────
@given("a post whose event an operator linked manually")
def step_given_a_post_whose_event_an_operator_linked_manually(context):
    _ensure_context(context)
    _create_venue(context, "Auto Linked Venue")
    manual_venue_id = _create_venue(context, "Manually Chosen Venue")
    _seed_post(context, caption=GENERIC_EVENT_CAPTION)
    context.mep_openai.program(_events_json([
        {"title": "Festa Manual", "location_text": "Auto Linked Venue"},
    ]))
    _run_crawl(context)
    row = _row_by_title(context, "Festa Manual")
    assert row["venue_id"] == context.mep_venues_by_name["Auto Linked Venue"], row  # sanity: auto-linked first
    context.mep_manual_venue_id = manual_venue_id
    context.mep_manual_event_id = row["event_id"]
    context.mep_dao.update_event(row["event_id"], {
        "venue_id": manual_venue_id, "location_resolution": "manual",
        "linked_by": "operator_x", "linked_at": context.mep_now,
    })


@when("the multi-event extraction runs again over that post")
def step_when_event_extraction_runs_again_over_that_post(context):
    context.mep_openai.program(_events_json([
        {"title": "Festa Manual", "location_text": "Auto Linked Venue"},
    ]))
    _run_crawl(context)


@then("the manually linked event stays linked to the venue the operator chose")
def step_then_the_event_stays_linked_to_the_venue_the_operator_chose(context):
    row = context.mep_dao.get_event(context.mep_manual_event_id)
    assert row["venue_id"] == context.mep_manual_venue_id, row
    assert row["location_resolution"] == "manual", row


# ── Scenarios 9, 10: never supersede confirmed/manual on disappearance ──────
@given("a post that already produced a confirmed event")
def step_given_a_post_that_already_produced_a_confirmed_event(context):
    _ensure_context(context)
    _seed_post(context, caption=GENERIC_EVENT_CAPTION)
    context.mep_openai.program(_events_json([{"title": "Festa Confirmada"}]))
    _run_crawl(context)
    row = _row_by_title(context, "Festa Confirmada")
    context.mep_confirmed_event_id = row["event_id"]
    context.mep_dao.update_event(row["event_id"], {"status": "confirmed"})


@given("a post that already produced a manually linked event")
def step_given_a_post_that_already_produced_a_manually_linked_event(context):
    _ensure_context(context)
    manual_venue_id = _create_venue(context, "Manually Chosen Venue")
    _seed_post(context, caption=GENERIC_EVENT_CAPTION)
    context.mep_openai.program(_events_json([{"title": "Festa Vinculada"}]))
    _run_crawl(context)
    row = _row_by_title(context, "Festa Vinculada")
    context.mep_manual_event_id = row["event_id"]
    context.mep_manual_venue_id = manual_venue_id
    context.mep_dao.update_event(row["event_id"], {
        "venue_id": manual_venue_id, "location_resolution": "manual",
        "linked_by": "operator_x", "linked_at": context.mep_now,
    })


@when("the multi-event extraction runs again and no longer returns that event")
def step_when_event_extraction_runs_again_and_no_longer_returns_that_event(context):
    context.mep_openai.program(_events_json([]))
    _run_crawl(context)


@then("the confirmed event keeps its status")
def step_then_the_confirmed_event_keeps_its_status(context):
    row = context.mep_dao.get_event(context.mep_confirmed_event_id)
    assert row is not None
    assert row["status"] == "confirmed", row


@then("that event keeps its manual link")
def step_then_that_event_keeps_its_manual_link(context):
    row = context.mep_dao.get_event(context.mep_manual_event_id)
    assert row is not None
    assert row["location_resolution"] == "manual", row
    assert row["venue_id"] == context.mep_manual_venue_id, row


# ── Scenario 11: no venue text -> unresolved, no inheritance ────────────────
@given("a post announcing two events where only the first names a venue")
def step_given_a_post_announcing_two_events_only_first_names_a_venue(context):
    _ensure_context(context)
    _create_venue(context, "Named Venue")
    _seed_post(context, caption=GENERIC_EVENT_CAPTION)
    context.mep_openai.program(_events_json([
        {"title": "First Event", "location_text": "Named Venue"},
        {"title": "Second Event", "location_text": None},
    ]))


@then("the first event is linked to that venue")
def step_then_the_first_event_is_linked_to_that_venue(context):
    row = _row_by_title(context, "First Event")
    assert row["venue_id"] == context.mep_venues_by_name["Named Venue"], row


@then("the second event is unresolved")
def step_then_the_second_event_is_unresolved(context):
    row = _row_by_title(context, "Second Event")
    assert row["location_resolution"] == "unresolved", row
    assert row["venue_id"] is None, row


@then("the second event does not inherit the first event's venue")
def step_then_the_second_event_does_not_inherit_the_first_events_venue(context):
    second = _row_by_title(context, "Second Event")
    first = _row_by_title(context, "First Event")
    assert second["venue_id"] != first["venue_id"]
    assert second["venue_id"] is None


# ── Scenario 12: truncated response persists nothing ────────────────────────
@given("a post whose extraction response is cut off mid-list")
def step_given_a_post_whose_extraction_response_is_cut_off(context):
    _ensure_context(context)
    _seed_post(context, caption=GENERIC_EVENT_CAPTION)
    context.mep_metric_snapshot["truncated_outcome"] = _snapshot_metric(
        "promoter_crawl_posts_total", {"outcome": "truncated"},
    )
    context.mep_raw_truncated_text = '{"events": [{"title": "O Homem do Fraque Verde", "date_text": "05/08'
    context.mep_openai.program_truncated(context.mep_raw_truncated_text)


@then('the post is counted with the outcome "truncated"')
def step_then_the_post_is_counted_with_outcome_truncated(context):
    before = context.mep_metric_snapshot.get("truncated_outcome", 0.0)
    after = _snapshot_metric("promoter_crawl_posts_total", {"outcome": "truncated"})
    assert after - before == 1.0, (before, after)


@then("no event is persisted for that post")
def step_then_no_event_is_persisted_for_that_post(context):
    assert _rows(context) == []


@then("the raw response is kept")
def step_then_the_raw_response_is_kept(context):
    kept = [
        entry for entry in context.mep_service.last_truncated
        if entry["handle"] == context.mep_handle and entry["shortcode"] == context.mep_shortcode
    ]
    assert kept, context.mep_service.last_truncated
    assert kept[-1]["raw_response"] == context.mep_raw_truncated_text


# ── Scenario 13: one malformed event, siblings survive ──────────────────────
@given("a post whose extraction returns three events and one is malformed")
def step_given_a_post_whose_extraction_returns_three_events_one_malformed(context):
    _ensure_context(context)
    _seed_post(context, caption=GENERIC_EVENT_CAPTION)
    context.mep_metric_snapshot["malformed_total"] = _snapshot_metric(
        "event_extraction_malformed_events_total",
    )
    base = {
        "description": None, "date_text": None, "time_text": None,
        "is_recurring": False, "recurrence_text": None, "lineup": [],
        "ticket_url": None, "price_text": None, "location_text": None,
        "confidence": 0.9,
    }
    raw = json.dumps({"events": [
        {**base, "title": "Valid Event A"},
        "this is not an event object",
        {**base, "title": "Valid Event B"},
    ]})
    context.mep_openai.program(raw)


@then("two events are persisted for that post")
def step_then_two_events_are_persisted_for_that_post(context):
    assert len(_rows(context)) == 2, _rows(context)


@then("the malformed event is counted")
def step_then_the_malformed_event_is_counted(context):
    before = context.mep_metric_snapshot.get("malformed_total", 0.0)
    after = _snapshot_metric("event_extraction_malformed_events_total")
    assert after - before == 1.0, (before, after)
