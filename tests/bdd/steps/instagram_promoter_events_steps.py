"""Behave steps for tests/bdd/enrichment/instagram-promoter-events.feature.

Drives the REAL PromoterRegistryService, PromoterCrawlService, the resolution
ladder (app/services/event_venue_resolution.py), and the admin events router
over a REAL VenueRepository backed by the shared in-memory RDS fake
(tests/rds_fake.py — the deterministic stand-in required by CLAUDE.md; no
live Apify/S3/OpenAI). The only test-only fakes are at the true external
boundary: the Apify posts client, the OpenAI extraction client, the media
archive store, and the HTTP image downloader — mirroring
instagram_event_extraction_steps.py's pattern.

The fake OpenAI client raises `AssertionError` the moment it is called more
times than programmed, rather than returning a default — a silent default
here is exactly the false-green trap a `program()`-per-call contract exists
to catch (see this plan's execution notes).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from app.services.event_extraction_service import ArchivedPost, new_event_id
from app.services.instagram_handle_sources import normalize_handle
from app.services.promoter_crawl_service import PromoterAccountUnavailable, PromoterCrawlService
from app.services.promoter_registry_service import PromoterRegistryService
from tests.rds_fake import InMemoryRdsVenueStore

_LOOP: "asyncio.AbstractEventLoop | None" = None
RECIFE_LAT, RECIFE_LNG = -8.05, -34.88


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ── fakes at the true boundary (no live Apify/S3/OpenAI) ─────────────────────
class _FakePromoterPostsClient:
    """Posts programmed per handle. Raises PromoterAccountUnavailable for a
    handle placed in `unavailable`, exactly like a real private/missing
    account would (see PromoterCrawlService's ApifyPromoterPostsClient
    docstring on why that detection is unverified against live data)."""

    def __init__(self):
        self.posts_by_handle: dict[str, list[dict]] = {}
        self.unavailable: set[str] = set()
        self.calls: dict[str, int] = {}

    async def fetch_recent_posts(self, handle: str, *, results_limit: int) -> list[dict]:
        self.calls[handle] = self.calls.get(handle, 0) + 1
        if handle in self.unavailable:
            raise PromoterAccountUnavailable(f"@{handle} is private or no longer exists")
        return list(self.posts_by_handle.get(handle, []))


class _FakeOpenAIClient:
    """Programmed with one response (a raw JSON string, or an Exception) per
    call, IN ORDER. Exhaustion RAISES rather than returning a default — a
    fake that quietly ran out of programmed responses and returned something
    plausible instead is exactly the false-green trap this guards against."""

    def __init__(self):
        self._responses: list = []
        self.calls = 0

    def program(self, response) -> None:
        self._responses.append(response)

    async def extract(self, *, caption, image_data_uri=None):
        self.calls += 1
        if not self._responses:
            raise AssertionError("fake OpenAI client called more times than programmed")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def extract_events(self, *, caption, image_data_uri=None, max_events):
        """PromoterCrawlService now calls extract_events (plans/260806_
        multi-event-posts.md), never extract. Every scenario in THIS file
        still programs a single flat event JSON (via _extraction_json) —
        wrapping it into the {"events": [...]} shape here is what makes "a
        single-event post behaves exactly as it did today" true with zero
        changes to any of this file's Given steps."""
        self.calls += 1
        if not self._responses:
            raise AssertionError("fake OpenAI client called more times than programmed")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple) and item and item[0] == "__truncated__":
            return item[1], True
        wrapped = json.dumps({"events": [json.loads(item)]})
        return wrapped, False


class _FakeDownloader:
    """No real HTTP call — every url resolves to the same trivial bytes."""

    def __init__(self):
        self.calls = 0

    async def download(self, url, timeout=15.0, max_bytes=None):
        self.calls += 1
        return b"FAKE_IMAGE_BYTES", "image/jpeg"


class _FakeMediaStore:
    """Records every promoter-keyed write so a scenario can assert on the S3
    KEY SHAPE (source=instagram_posts/..., promoter=<handle>/..., never
    venue_id=) without touching real S3."""

    def __init__(self):
        self.images: list[str] = []
        self.manifests: list[tuple] = []

    async def put_promoter_image(self, *, prefix, handle, photo_id, data, content_type, category=None):
        key = f"{prefix}promoter={handle}/media/{photo_id}.jpg"
        self.images.append(key)
        return key

    async def put_promoter_manifest(self, *, prefix, handle, manifest):
        self.manifests.append((prefix, handle, manifest))
        return f"{prefix}promoter={handle}/info/_manifest.json"


class _FakeEventPostSource:
    """Posts injected directly by the scenario (no S3 manifest walking) —
    the same role _FakePostSource plays in instagram_event_extraction_steps.py,
    reused here for PromoterRegistryService.run_discovery, which walks
    already-extracted VENUE posts to find mentions worth proposing."""

    def __init__(self):
        self.posts_by_venue: dict[str, list[ArchivedPost]] = {}

    async def posts_for_venue(self, venue_id, since):
        return self.posts_by_venue.get(venue_id, [])


def _extraction_json(**overrides) -> str:
    payload = {
        "title": "Festa", "description": None, "date_text": None, "time_text": None,
        "is_recurring": False, "recurrence_text": None, "lineup": [], "ticket_url": None,
        "price_text": None, "location_text": None, "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


# ── context setup (Background resets everything fresh per scenario) ─────────
def _reset_context(context) -> None:
    context.pe_store = InMemoryRdsVenueStore()
    context.pe_dao = VenueRepository(client=None, rds_store=context.pe_store)
    context.pe_posts_client = _FakePromoterPostsClient()
    context.pe_openai = _FakeOpenAIClient()
    context.pe_media_store = _FakeMediaStore()
    context.pe_downloader = _FakeDownloader()
    context.pe_post_source = _FakeEventPostSource()
    context.pe_now = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)
    context.pe_service = PromoterCrawlService(
        venue_dao=context.pe_dao,
        posts_client=context.pe_posts_client,
        media_store=context.pe_media_store,
        downloader=context.pe_downloader,
        openai_client=context.pe_openai,
        confidence_floor=0.55,
        margin=0.08,
        min_confidence=0.0,
        now_provider=lambda: context.pe_now,
    )
    context.pe_registry = PromoterRegistryService(
        venue_dao=context.pe_dao, post_source=context.pe_post_source,
        now_provider=lambda: context.pe_now,
    )
    context.pe_venue_counter = 0
    context.pe_venues_by_name: dict[str, str] = {}
    context.pe_run_config: dict = {}
    context.pe_discovery_config: dict = {}
    context.pe_last_report = None
    context.pe_discovery_result = None
    context.pe_last_handle = None
    context.pe_last_shortcode = None
    context.pe_last_event_id = None
    context.pe_expected_venue_id = None
    context.pe_target_handle = None
    context.pe_two_candidate_venue_ids = []
    context.pe_queue_candidates = []
    context.pe_client = None
    context.pe_review_response = None


def _ensure_context(context) -> None:
    if getattr(context, "pe_dao", None) is None:
        _reset_context(context)


def _create_venue(context, name: str, *, handle=None, lat=RECIFE_LAT, lng=RECIFE_LNG) -> str:
    context.pe_venue_counter += 1
    venue_id = f"pe_venue_{context.pe_venue_counter}"
    context.pe_dao.upsert_venue(Venue(venue_id=venue_id, venue_name=name, venue_lat=lat, venue_lng=lng))
    if handle:
        context.pe_dao.set_venue_instagram(
            VenueInstagram(venue_id=venue_id, instagram_handle=normalize_handle(handle), status="found")
        )
    context.pe_venues_by_name[name] = venue_id
    return venue_id


def _active_promoter(context, handle: str, *, status: str = "active") -> str:
    normalized = normalize_handle(handle)
    context.pe_registry.register(normalized, status=status)
    context.pe_last_handle = normalized
    return normalized


def _seed_post(
    context, handle: str, shortcode: str, *,
    caption: str = "Ingressos abertos! Vem pro role.",
    image_urls=None, location_tag=None, timestamp=None,
) -> dict:
    post = {
        "shortcode": shortcode,
        "caption": caption,
        "permalink": f"https://instagram.com/p/{shortcode}",
        "timestamp": (timestamp or context.pe_now).isoformat(),
        "image_urls": image_urls if image_urls is not None else ["https://cdn.example.com/img.jpg"],
        "location_tag": location_tag,
    }
    context.pe_posts_client.posts_by_handle.setdefault(handle, []).append(post)
    context.pe_last_handle = handle
    context.pe_last_shortcode = shortcode
    return post


def _sync_last_event_id(context) -> None:
    if context.pe_last_handle and context.pe_last_shortcode:
        row = context.pe_dao.get_event_by_source(context.pe_last_handle, context.pe_last_shortcode)
        if row is not None:
            context.pe_last_event_id = row["event_id"]


def _current_event(context) -> dict:
    assert context.pe_last_event_id, "no event id tracked yet"
    row = context.pe_dao.get_event(context.pe_last_event_id)
    assert row is not None, f"no event stored for {context.pe_last_event_id}"
    return row


def _build_admin_events_app(context) -> None:
    app = FastAPI()
    app.include_router(admin_events_router)
    set_events_container(type("C", (), {"pipeline_repository": context.pe_dao})())
    context.pe_client = TestClient(app)


# ── Background ────────────────────────────────────────────────────────────────
@given("the catalog holds venues with confirmed Instagram handles")
def step_given_the_catalog_holds_venues_with_confirmed_instagram_handles(context):
    _reset_context(context)


@given("the event extractor is available")
def step_given_the_event_extractor_is_available(context):
    _ensure_context(context)  # already wired in _reset_context; nothing further


# ── Scenario 1: register + crawl within bound, correct archive location ─────
@given('the operator registers the promoter account "festadorecife"')
def step_given_the_operator_registers_festadorecife(context):
    _ensure_context(context)
    handle = normalize_handle("festadorecife")
    context.pe_registry.register(handle)
    context.pe_last_handle = handle
    for i in range(8):
        _seed_post(context, handle, f"reg_post_{i}", caption="Segue a gente!")


@given("that account is active")
def step_given_that_account_is_active(context):
    context.pe_registry.update(context.pe_last_handle, {"status": "active"})


@given("the run caps posts per account at 5")
def step_given_the_run_caps_posts_per_account_at_5(context):
    context.pe_run_config["max_posts_per_account"] = 5


@then("{n:d} posts of that account are crawled")
def step_then_n_posts_of_that_account_are_crawled(context, n):
    entry = next(a for a in context.pe_last_report["accounts"] if a["handle"] == context.pe_last_handle)
    assert entry["posts_crawled"] == n, entry


@then('its posts are archived under the source folder "{source}"')
def step_then_its_posts_are_archived_under_the_source_folder(context, source):
    assert context.pe_media_store.images, "expected at least one archived image"
    assert all(f"source={source}/" in key for key in context.pe_media_store.images)


@then("its posts are not archived under any venue id")
def step_then_its_posts_are_not_archived_under_any_venue_id(context):
    assert context.pe_media_store.images
    assert all("venue_id=" not in key for key in context.pe_media_store.images)


# ── Scenario 2: discovery proposes, never crawls ─────────────────────────────
@given('confirmed events whose captions mention "{mention}" {n:d} times')
def step_given_confirmed_events_whose_captions_mention_n_times(context, mention, n):
    _ensure_context(context)
    venue_id = _create_venue(context, "Host Venue", handle="hostvenue_ig")
    for i in range(n):
        shortcode = f"disc_post_{i}"
        caption = f"Festa incrível hoje, bora {mention}! Ingressos na entrada."
        post = ArchivedPost(
            shortcode=shortcode, permalink=f"https://instagram.com/p/{shortcode}",
            caption=caption, timestamp=context.pe_now,
            flyer_photo_key=None, flyer_confidence=None, any_photo_key=None,
        )
        context.pe_post_source.posts_by_venue.setdefault(venue_id, []).append(post)
        context.pe_dao.insert_event({
            "event_id": f"evt_disc_{i}", "venue_id": venue_id, "source_kind": "venue_post",
            "source_handle": "hostvenue_ig", "source_shortcode": shortcode, "status": "confirmed",
        })
    context.pe_target_handle = normalize_handle(mention)


@given("the mention threshold is {n:d}")
def step_given_the_mention_threshold_is_n(context, n):
    context.pe_discovery_config["mention_threshold"] = n


@when("promoter discovery runs")
def step_when_promoter_discovery_runs(context):
    context.pe_discovery_result = _run(context.pe_registry.run_discovery(context.pe_discovery_config))


@then('"{mention}" is registered with the status "{status}"')
def step_then_mention_is_registered_with_the_status(context, mention, status):
    row = context.pe_dao.get_promoter_account(normalize_handle(mention))
    assert row is not None, f"expected {mention!r} to be registered"
    assert row["status"] == status, row


@then('its discovery source is "{source}"')
def step_then_its_discovery_source_is(context, source):
    row = context.pe_dao.get_promoter_account(context.pe_target_handle)
    assert row is not None and row["discovery_source"] == source, row


@then("no post of that account is crawled")
def step_then_no_post_of_that_account_is_crawled(context):
    handle = context.pe_target_handle or context.pe_last_handle
    assert context.pe_posts_client.calls.get(handle, 0) == 0


# ── Scenario 3: candidate never crawled until activated ─────────────────────
@given('a promoter account with the status "{status}"')
def step_given_a_promoter_account_with_the_status(context, status):
    _ensure_context(context)
    handle = normalize_handle("candidatepromoter")
    context.pe_registry.register(handle, status=status)
    context.pe_last_handle = handle
    _seed_post(context, handle, "candidate_post", caption="Ingressos abertos!")
    context.pe_openai.program(_extraction_json())


@when("the promoter crawl runs")
def step_when_the_promoter_crawl_runs(context):
    context.pe_last_report = _run(context.pe_service.run(context.pe_run_config))
    _sync_last_event_id(context)


@when("the operator sets that account active")
def step_when_the_operator_sets_that_account_active(context):
    context.pe_registry.update(context.pe_last_handle, {"status": "active"})


@then("its posts are crawled")
def step_then_its_posts_are_crawled(context):
    entry = next(
        (a for a in context.pe_last_report["accounts"] if a["handle"] == context.pe_last_handle), None,
    )
    assert entry is not None and entry["posts_crawled"] > 0, context.pe_last_report


# ── Scenario 4: handle mention wins, auto ────────────────────────────────────
@given("a promoter post whose caption mentions a known venue's handle")
def step_given_a_promoter_post_whose_caption_mentions_a_known_venues_handle(context):
    _ensure_context(context)
    venue_id = _create_venue(context, "Mentioned Venue", handle="mentionedvenue_ig")
    handle = _active_promoter(context, "eventspromoter")
    _seed_post(context, handle, "s4_post", caption="Ingressos abertos! Hoje é festa lá no @mentionedvenue_ig, vem!")
    context.pe_openai.program(_extraction_json())
    context.pe_expected_venue_id = venue_id


@then("the event is linked to that venue")
def step_then_the_event_is_linked_to_that_venue(context):
    assert _current_event(context)["venue_id"] == context.pe_expected_venue_id


@then('the link method is "{method}"')
def step_then_the_link_method_is(context, method):
    assert _current_event(context)["linked_by"] == method, _current_event(context)


@then('the link resolution is "{resolution}"')
def step_then_the_link_resolution_is(context, resolution):
    assert _current_event(context)["location_resolution"] == resolution


# ── Scenario 5: certainty beats score ────────────────────────────────────────
@given("a promoter post that mentions a known venue's handle")
def step_given_a_promoter_post_that_mentions_a_known_venues_handle(context):
    _ensure_context(context)
    venue_a = _create_venue(context, "Mentioned Venue A", handle="mentionedvenuea_ig")
    handle = _active_promoter(context, "eventspromoter2")
    _seed_post(context, handle, "s5_post", caption="Ingressos abertos! Bora pro @mentionedvenuea_ig hoje!")
    context.pe_expected_venue_id = venue_a


@given("its location text matches a different venue by name with a higher score")
def step_given_its_location_text_matches_a_different_venue_with_a_higher_score(context):
    _create_venue(context, "Casa Rosa Exata")
    # An exact name match scores 1.0 on the fuzzy path — deliberately a
    # "higher score" than the handle mention would carry IF it were scored.
    # It is not scored: rung 1 is an identity, and this is exactly the case
    # that proves certainty outranks it.
    context.pe_openai.program(_extraction_json(location_text="Casa Rosa Exata"))


@then("the event is linked to the venue whose handle was mentioned")
def step_then_the_event_is_linked_to_the_venue_whose_handle_was_mentioned(context):
    assert _current_event(context)["venue_id"] == context.pe_expected_venue_id


# ── Scenario 6: location tag ─────────────────────────────────────────────────
@given("a promoter post with no handle mention")
def step_given_a_promoter_post_with_no_handle_mention(context):
    _ensure_context(context)
    handle = _active_promoter(context, "tagpromoter")
    _seed_post(context, handle, "s6_post", caption="Ingressos abertos! Vem pro role de hoje.")
    context.pe_openai.program(_extraction_json())


@given("the post carries a location tag matching a known venue")
def step_given_the_post_carries_a_location_tag_matching_a_known_venue(context):
    venue_id = _create_venue(context, "Tag Venue")
    post = context.pe_posts_client.posts_by_handle[context.pe_last_handle][-1]
    post["location_tag"] = {"name": "Tag Venue", "lat": RECIFE_LAT, "lng": RECIFE_LNG}
    context.pe_expected_venue_id = venue_id


# ── Scenario 7: name match, no handle/tag ────────────────────────────────────
@given("a promoter post whose location text names a known venue")
def step_given_a_promoter_post_whose_location_text_names_a_known_venue(context):
    _ensure_context(context)
    venue_id = _create_venue(context, "Name Match Venue")
    handle = _active_promoter(context, "namematchpromoter")
    _seed_post(context, handle, "s7_post", caption="Ingressos abertos! Vem pro role.")
    context.pe_openai.program(_extraction_json(location_text="Name Match Venue"))
    context.pe_expected_venue_id = venue_id


@given("the post carries no handle mention and no location tag")
def step_given_the_post_carries_no_handle_mention_and_no_location_tag(context):
    pass  # already true by construction of the previous step


# ── Scenario 8: margin gate — near tie queues for review ────────────────────
@given("a promoter post whose location text matches two venues with scores 0.91 and 0.89")
def step_given_a_promoter_post_whose_location_text_matches_two_venues(context):
    _ensure_context(context)
    v1 = _create_venue(context, "Casa Rosa Centro")
    v2 = _create_venue(context, "Casa Rosa Sul")
    handle = _active_promoter(context, "marginpromoter")
    _seed_post(context, handle, "s8_post", caption="Ingressos abertos! Vem pro role.")
    context.pe_openai.program(_extraction_json(location_text="Casa Rosa"))
    context.pe_two_candidate_venue_ids = [v1, v2]


@given("the auto-link margin is 0.05")
def step_given_the_auto_link_margin_is_005(context):
    context.pe_service.margin = 0.05


@then('the event has the review status "{status}"')
def step_then_the_event_has_the_review_status(context, status):
    assert _current_event(context)["status"] == status, _current_event(context)


@then("the event has no venue id")
def step_then_the_event_has_no_venue_id(context):
    assert _current_event(context)["venue_id"] is None


@then("both venues are recorded as ranked candidates")
def step_then_both_venues_are_recorded_as_ranked_candidates(context):
    event = _current_event(context)
    candidates = context.pe_dao.list_event_venue_link_candidates(event["event_id"])
    assert len(candidates) == 2, candidates
    assert {c["venue_id"] for c in candidates} == set(context.pe_two_candidate_venue_ids)


# ── Scenario 9: below the floor -> unresolved, single-candidate branch ──────
@given("a promoter post whose best candidate scores below the confidence floor")
def step_given_a_promoter_post_whose_best_candidate_scores_below_the_floor(context):
    _ensure_context(context)
    _create_venue(context, "Zetta Lounge")
    handle = _active_promoter(context, "floorpromoter")
    _seed_post(context, handle, "s9_post", caption="Ingressos abertos! Vem pro role.")
    context.pe_openai.program(_extraction_json(location_text="Random Unrelated Text"))


@then('the event location resolution is "{resolution}"')
def step_then_the_event_location_resolution_is(context, resolution):
    assert _current_event(context)["location_resolution"] == resolution


@then("the event is distinguishable from one queued for review")
def step_then_the_event_is_distinguishable_from_one_queued_for_review(context):
    row = _current_event(context)
    # A queued event's location_resolution is NULL (never decided); this one
    # is the string "unresolved" (decided: below the floor). The two must
    # never look the same.
    assert row["location_resolution"] == "unresolved"
    assert row["location_resolution"] is not None


# ── Scenario 10/11: review queue presentation + manual link ─────────────────
@given("an event queued for review with three candidate venues")
def step_given_an_event_queued_for_review_with_three_candidate_venues(context):
    _ensure_context(context)
    vids = [_create_venue(context, f"Queue Venue {i}") for i in range(1, 4)]
    event_id = new_event_id()
    context.pe_dao.insert_event({
        "event_id": event_id, "venue_id": None, "source_kind": "promoter_post",
        "source_handle": "queuepromoter", "source_shortcode": "queue_post",
        "status": "pending_review", "location_resolution": None,
    })
    candidates = [
        {"venue_id": vids[0], "rank": 1, "score": 0.9, "method": "name_match",
         "evidence": {"location_text": "Queue Venue", "name_score": 0.9}},
        {"venue_id": vids[1], "rank": 2, "score": 0.8, "method": "name_match",
         "evidence": {"location_text": "Queue Venue", "name_score": 0.8}},
        {"venue_id": vids[2], "rank": 3, "score": 0.7, "method": "name_match",
         "evidence": {"location_text": "Queue Venue", "name_score": 0.7}},
    ]
    context.pe_dao.replace_event_venue_link_candidates(event_id, candidates)
    context.pe_last_event_id = event_id
    context.pe_queue_candidates = candidates
    _build_admin_events_app(context)


@when("the review queue is requested")
def step_when_the_review_queue_is_requested(context):
    context.pe_review_response = context.pe_client.get("/admin/events/review")


@then("the three candidates are returned in rank order")
def step_then_the_three_candidates_are_returned_in_rank_order(context):
    assert context.pe_review_response.status_code == 200, context.pe_review_response.text
    body = context.pe_review_response.json()
    item = next(i for i in body if i["event_id"] == context.pe_last_event_id)
    ranks = [c["rank"] for c in item["candidates"]]
    assert ranks == [1, 2, 3], ranks


@then("each candidate carries its score, its method and its evidence")
def step_then_each_candidate_carries_its_score_method_and_evidence(context):
    body = context.pe_review_response.json()
    item = next(i for i in body if i["event_id"] == context.pe_last_event_id)
    assert len(item["candidates"]) == 3
    for candidate in item["candidates"]:
        assert candidate["score"] is not None
        assert candidate["method"]
        assert candidate["evidence"]


@when("the operator links it to the second candidate")
def step_when_the_operator_links_it_to_the_second_candidate(context):
    second = context.pe_queue_candidates[1]
    context.pe_expected_venue_id = second["venue_id"]
    context.pe_link_response = context.pe_client.post(
        f"/admin/events/{context.pe_last_event_id}/link",
        json={"candidate_rank": 2, "linked_by": "operator_maria"},
    )
    assert context.pe_link_response.status_code == 200, context.pe_link_response.text


@then("the operator who linked it is recorded")
def step_then_the_operator_who_linked_it_is_recorded(context):
    assert _current_event(context)["linked_by"] == "operator_maria"


# ── Scenario 12: manual link survives a later crawl ──────────────────────────
@given("an event an operator linked manually")
def step_given_an_event_an_operator_linked_manually(context):
    _ensure_context(context)
    auto_venue = _create_venue(context, "Auto Venue", handle="autovenue_ig")
    manual_venue = _create_venue(context, "Manually Chosen Venue")
    handle = _active_promoter(context, "manualpromoter")
    _seed_post(context, handle, "s12_post", caption="Ingressos abertos! Hoje é no @autovenue_ig!")
    context.pe_openai.program(_extraction_json())
    _run(context.pe_service.run({}))
    row = context.pe_dao.get_event_by_source(handle, "s12_post")
    assert row is not None and row["venue_id"] == auto_venue, row  # sanity: auto-linked first
    context.pe_manual_venue_id = manual_venue
    context.pe_dao.update_event(row["event_id"], {
        "venue_id": manual_venue, "location_resolution": "manual",
        "location_confidence": None, "linked_by": "operator_x", "linked_at": context.pe_now,
    })
    context.pe_last_event_id = row["event_id"]


@when("the promoter crawl runs again over its post")
def step_when_the_promoter_crawl_runs_again_over_its_post(context):
    context.pe_openai.program(_extraction_json())
    context.pe_last_report = _run(context.pe_service.run({}))


@then("the event stays linked to the venue the operator chose")
def step_then_the_event_stays_linked_to_the_venue_the_operator_chose(context):
    row = context.pe_dao.get_event(context.pe_last_event_id)
    assert row["venue_id"] == context.pe_manual_venue_id, row
    assert row["location_resolution"] == "manual", row


# ── Scenario 13: unlink a wrong auto-link ────────────────────────────────────
@given("an event linked automatically to the wrong venue")
def step_given_an_event_linked_automatically_to_the_wrong_venue(context):
    _ensure_context(context)
    wrong_venue = _create_venue(context, "Wrong Venue")
    event_id = new_event_id()
    context.pe_dao.insert_event({
        "event_id": event_id, "venue_id": wrong_venue, "source_kind": "promoter_post",
        "source_handle": "unlinkpromoter", "source_shortcode": "unlink_post",
        "status": "pending_review", "location_resolution": "auto",
        "location_confidence": 0.9, "linked_by": "name_match", "linked_at": context.pe_now,
    })
    context.pe_last_event_id = event_id
    _build_admin_events_app(context)


@when("the operator unlinks it")
def step_when_the_operator_unlinks_it(context):
    context.pe_unlink_response = context.pe_client.post(f"/admin/events/{context.pe_last_event_id}/unlink")
    assert context.pe_unlink_response.status_code == 200, context.pe_unlink_response.text


# ── Scenario 14: one dead handle must not cost the others their crawl ──────
@given("three active promoter accounts and the second one is unavailable")
def step_given_three_active_promoter_accounts_and_the_second_one_is_unavailable(context):
    _ensure_context(context)
    for handle in ("acct_1", "acct_2", "acct_3"):
        _active_promoter(context, handle)
        _seed_post(context, handle, f"{handle}_post", caption="Ingressos abertos!")
    context.pe_posts_client.unavailable.add("acct_2")
    for _ in ("acct_1", "acct_3"):
        context.pe_openai.program(_extraction_json())


@then("the unavailable account is marked and skipped")
def step_then_the_unavailable_account_is_marked_and_skipped(context):
    row = context.pe_dao.get_promoter_account("acct_2")
    assert row is not None and row.get("notes") and "unavailable" in row["notes"].lower(), row
    entry = next(a for a in context.pe_last_report["accounts"] if a["handle"] == "acct_2")
    assert entry["outcome"] == "unavailable", entry


@then("the third account is still crawled")
def step_then_the_third_account_is_still_crawled(context):
    entry = next(a for a in context.pe_last_report["accounts"] if a["handle"] == "acct_3")
    assert entry["outcome"] == "crawled" and entry["posts_crawled"] > 0, entry


# ── Scenario 15: never self-link ─────────────────────────────────────────────
@given("a promoter post whose caption mentions the promoter's own handle")
def step_given_a_promoter_post_whose_caption_mentions_the_promoters_own_handle(context):
    _ensure_context(context)
    context.pe_self_venue_id = _create_venue(context, "Selfpromo Venue", handle="selfpromo")
    handle = _active_promoter(context, "selfpromo")  # same handle as the venue's own — the trap
    _seed_post(context, handle, "s15_post", caption="Ingressos abertos! Segue a gente @selfpromo pra mais.")
    context.pe_openai.program(_extraction_json())


@then("that mention is not used to resolve the venue")
def step_then_that_mention_is_not_used_to_resolve_the_venue(context):
    row = _current_event(context)
    assert row["venue_id"] is None, row
    assert row.get("linked_by") != "handle_mention"


# ── Scenario 16: dry run reports, writes nothing ─────────────────────────────
@given("two active promoter accounts")
def step_given_two_active_promoter_accounts(context):
    _ensure_context(context)
    for handle in ("dry_1", "dry_2"):
        _active_promoter(context, handle)
        _seed_post(context, handle, f"{handle}_post", caption="Ingressos abertos!")


@when("the promoter crawl runs as a dry run")
def step_when_the_promoter_crawl_runs_as_a_dry_run(context):
    cfg = dict(context.pe_run_config)
    cfg["dry_run"] = True
    context.pe_last_report = _run(context.pe_service.run(cfg))


@then("the accounts and post counts it would crawl are reported")
def step_then_the_accounts_and_post_counts_it_would_crawl_are_reported(context):
    assert context.pe_last_report["dry_run"] is True
    assert len(context.pe_last_report["accounts"]) == 2, context.pe_last_report
    assert all(a["posts_crawled"] > 0 for a in context.pe_last_report["accounts"])


@then("no post is crawled")
def step_then_no_post_is_crawled(context):
    assert context.pe_openai.calls == 0
    assert context.pe_media_store.images == []


@then("no promoter event is written")
def step_then_no_promoter_event_is_written(context):
    assert context.pe_dao.list_events() == []
