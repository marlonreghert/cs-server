"""Behave steps for
tests/bdd/enrichment/scheduled-incremental-instagram-crawl.feature.

Drives the REAL `ScheduledInstagramCrawlService`, `CrawlBound`/cursor/pinned-
post logic (app/services/instagram_crawl_service.py), and a REAL
`EventExtractionService` (for the chaining scenario) over a REAL
`VenueRepository` backed by the shared in-memory RDS fake (tests/rds_fake.py
— the deterministic stand-in CLAUDE.md requires; no live Apify/S3/OpenAI/
Redis). The only test-only fakes are at the true external boundary: the
Apify posts/reels client, the monthly-budget counter (Redis in production),
the media archive store, the HTTP image downloader, and the OpenAI
extraction client — mirroring instagram_promoter_events_steps.py's and
instagram_event_extraction_steps.py's existing pattern in this suite.

`_FakeMediaStore` implements BOTH the write side (`put_image`/`put_manifest`,
what `InstagramCrawlChainer` calls) and the read side
(`list_run_prefixes`/`read_manifest`, what the REAL `EventPostSource` calls)
over the SAME in-memory dict, so the "chain" scenario proves archiving and
extraction are actually wired together end to end — not two independent
mock-call assertions that could both pass even if the manifest shape the
archiver writes were not what the extractor reads.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from app.api.apify_instagram_client import ApifyCreditExhaustedError, FetchPostsResult
from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.archive_sources import SOURCE_INSTAGRAM_POSTS
from app.services.event_extraction_service import EventExtractionService, EventPostSource
from app.services.instagram_crawl_service import (
    CrawlServiceConfig,
    InstagramCrawlChainer,
    ScheduledInstagramCrawlService,
    _format_utc,
)
from tests.rds_fake import InMemoryRdsVenueStore

_LOOP: "asyncio.AbstractEventLoop | None" = None
RECIFE_LAT, RECIFE_LNG = -8.05, -34.88


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


# ── fakes at the true boundary (no live Apify/S3/OpenAI/Redis) ──────────────
class _FakeApifyClient:
    """Posts/reels programmed per (handle, results_type). Records every
    call's exact arguments, in order — the unit test plan's own requirement
    ("asserted as the exact string passed"). `fail_next` arms the NEXT call
    to a given (handle, results_type) to raise a programmed exception
    (a plain failure, or `ApifyCreditExhaustedError`)."""

    def __init__(self):
        self._posts: dict[tuple[str, str], list[dict]] = {}
        self._next_error: dict[tuple[str, str], Exception] = {}
        # plans/260812_crawl-error-visibility.md §A: an Apify error item,
        # programmed per (handle, results_type) — distinct from
        # `_next_error` above (a transport-level EXCEPTION `fetch_recent_
        # posts` itself raises). This is the STRUCTURED item the real
        # client now surfaces via FetchPostsResult instead of dropping —
        # can coexist with programmed posts for the SAME key (Apify's own
        # evidence: a dataset can carry real posts and an error item
        # together).
        self._errors: dict[tuple[str, str], dict] = {}
        self.calls: list[dict] = []

    def program_posts(self, handle: str, results_type: str, posts: list[dict]) -> None:
        self._posts[(handle, results_type)] = posts

    def fail_next(self, handle: str, results_type: str, exc: Exception) -> None:
        self._next_error[(handle, results_type)] = exc

    def program_error(
        self, handle: str, results_type: str, *,
        code: str, request_error_count: int = 0, description: str | None = None,
    ) -> None:
        self._errors[(handle, results_type)] = {
            "code": code, "request_error_count": request_error_count,
            "description": description or "Empty or private data for provided input",
        }

    async def fetch_recent_posts(
        self, handle, results_limit=10, *, only_posts_newer_than=None, results_type="posts",
    ) -> FetchPostsResult:
        self.calls.append({
            "handle": handle, "results_limit": results_limit,
            "only_posts_newer_than": only_posts_newer_than, "results_type": results_type,
        })
        key = (handle, results_type)
        if key in self._next_error:
            raise self._next_error.pop(key)
        error = self._errors.get(key)
        return FetchPostsResult(
            posts=list(self._posts.get(key, [])),
            error_code=error.get("code") if error else None,
            error_description=error.get("description") if error else None,
            request_error_count=error.get("request_error_count", 0) if error else 0,
        )


class _FakeBudgetDao:
    """Mirrors `CrawlBudgetDao`'s public interface over a plain dict — the
    Redis-backed monthly counter, faked at the true external boundary."""

    def __init__(self):
        self._counts: dict[str, int] = {}

    def current_year_month_utc(self, now=None) -> str:
        now = now or datetime.now(timezone.utc)
        return now.strftime("%Y-%m")

    def get_month_count(self, year_month: str) -> int:
        return self._counts.get(year_month, 0)

    def increment_month(self, year_month: str, n: int) -> int:
        self._counts[year_month] = self._counts.get(year_month, 0) + n
        return self._counts[year_month]


class _FakeMediaStore:
    """Records every venue-keyed AND promoter-keyed write, and can answer
    the READ calls `EventPostSource` (real, unfaked) makes — see the module
    docstring for why both sides live on one fake."""

    def __init__(self):
        self.images: list[str] = []
        self.manifests: dict[tuple[str, str], dict] = {}
        self._prefixes: list[str] = []

    async def put_image(self, *, prefix, venue_id, photo_id, data, content_type, category=None):
        key = f"{prefix}venue_id={venue_id}/media/{photo_id}.jpg"
        self.images.append(key)
        return key

    async def put_manifest(self, *, prefix, venue_id, manifest):
        self.manifests[(prefix, venue_id)] = manifest
        if prefix not in self._prefixes:
            self._prefixes.append(prefix)
        return f"{prefix}venue_id={venue_id}/info/_manifest.json"

    async def put_promoter_image(self, *, prefix, handle, photo_id, data, content_type, category=None):
        key = f"{prefix}promoter={handle}/media/{photo_id}.jpg"
        self.images.append(key)
        return key

    async def put_promoter_manifest(self, *, prefix, handle, manifest):
        self.manifests[(prefix, f"promoter:{handle}")] = manifest
        if prefix not in self._prefixes:
            self._prefixes.append(prefix)
        return f"{prefix}promoter={handle}/info/_manifest.json"

    async def list_run_prefixes(self, source) -> list[str]:
        return sorted(self._prefixes)

    async def read_manifest(self, prefix, venue_id):
        return self.manifests.get((prefix, venue_id))

    async def read_image_data_uri(self, key):
        return f"data:image/jpeg;base64,FAKE_{key}" if key else None


class _FakeDownloader:
    """No real HTTP call — every url resolves to the same trivial bytes."""

    async def download(self, url, timeout=15.0, max_bytes=None):
        return b"FAKE_IMAGE_BYTES", "image/jpeg"


class _FakeOpenAIClient:
    """Programmed with one response (a raw JSON string, or an Exception) per
    call, in order. Exhaustion RAISES rather than returning a default —
    mirrors instagram_event_extraction_steps.py / instagram_promoter_events_
    steps.py's identical fakes."""

    def __init__(self):
        self._responses: list = []
        self.calls = 0

    def program(self, response) -> None:
        self._responses.append(response)

    def program_multi(self, raw_json: str) -> None:
        """Push an ALREADY-WRAPPED `{"events": [...]}` JSON string — unlike
        `program()` (which wraps a single event dict, the common case).
        Sentinel-tagged tuple, mirroring multi_event_posts_steps.py's
        `_FakeMultiEventOpenAIClient.program_truncated`'s identical trick,
        so `extract_events` can tell the two apart without guessing from
        shape."""
        self._responses.append(("__multi__", raw_json))

    async def extract_events(self, *, caption, image_data_uri=None, max_events):
        self.calls += 1
        if not self._responses:
            raise AssertionError("fake OpenAI client called more times than programmed")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple) and item and item[0] == "__multi__":
            return item[1], False
        wrapped = json.dumps({"events": [json.loads(item)]})
        return wrapped, False


def _extraction_json(**overrides) -> str:
    payload = {
        "title": "Festa", "description": None, "date_text": None, "time_text": None,
        "is_recurring": False, "recurrence_text": None, "lineup": [], "ticket_url": None,
        "price_text": None, "location_text": None, "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _post(shortcode, timestamp, *, is_pinned=False, image_urls=None, caption="") -> dict:
    return {
        "caption": caption, "likes_count": 0, "comments_count": 0,
        "timestamp": timestamp, "post_type": "image", "shortcode": shortcode,
        "permalink": f"https://instagram.com/p/{shortcode}",
        "image_urls": (
            image_urls if image_urls is not None else [f"https://cdn.example.com/{shortcode}.jpg"]
        ),
        "is_pinned": is_pinned,
    }


# ── context setup (Background resets everything fresh per scenario) ────────
def _reset_context(context) -> None:
    context.ic_store = InMemoryRdsVenueStore()
    context.ic_dao = VenueRepository(client=None, rds_store=context.ic_store)
    context.ic_apify = _FakeApifyClient()
    context.ic_budget = _FakeBudgetDao()
    context.ic_media_store = _FakeMediaStore()
    context.ic_downloader = _FakeDownloader()
    context.ic_openai = _FakeOpenAIClient()
    context.ic_now = datetime(2026, 8, 7, 22, 0, tzinfo=timezone.utc)
    context.ic_extraction = EventExtractionService(
        venue_dao=context.ic_dao,
        post_source=EventPostSource(
            media_store=context.ic_media_store, archive_source=SOURCE_INSTAGRAM_POSTS,
        ),
        openai_client=context.ic_openai,
        min_confidence=0.0,
        now_provider=lambda: context.ic_now,
    )
    context.ic_chainer = InstagramCrawlChainer(
        media_store=context.ic_media_store,
        downloader=context.ic_downloader,
        event_extraction_service=context.ic_extraction,
        promoter_crawl_service=None,
        archive_source=SOURCE_INSTAGRAM_POSTS,
    )
    context.ic_config = CrawlServiceConfig(
        overlap_hours=6.0, default_initial_lookback="3 months",
        default_results_limit=10, default_seed_results_limit=200,
        monthly_result_budget=1000,
        max_consecutive_failures=5, result_cost_usd=0.0027,
    )
    context.ic_service = ScheduledInstagramCrawlService(
        venue_dao=context.ic_dao, apify_client=context.ic_apify,
        budget_dao=context.ic_budget, config=context.ic_config,
        chainer=context.ic_chainer, now_provider=lambda: context.ic_now,
    )
    context.ic_handle = None
    context.ic_venue_ids: list[str] = []
    context.ic_venue_counter = 0
    context.ic_last_report = None
    context.ic_last_reports = None
    context.ic_cursor = None
    context.ic_handles: list[str] = []


def _ensure_context(context) -> None:
    if getattr(context, "ic_dao", None) is None:
        _reset_context(context)


def _create_venue(context, handle: str, *, lat=RECIFE_LAT, lng=RECIFE_LNG) -> str:
    context.ic_venue_counter += 1
    venue_id = f"ic_venue_{context.ic_venue_counter}"
    context.ic_dao.upsert_venue(
        Venue(venue_id=venue_id, venue_name=f"Venue {venue_id}", venue_lat=lat, venue_lng=lng)
    )
    context.ic_dao.set_venue_instagram(
        VenueInstagram(venue_id=venue_id, instagram_handle=handle, status="found")
    )
    context.ic_venue_ids.append(venue_id)
    return venue_id


def _create_target(
    context, handle: str, *, kind="venue", cron="0 22 * * 5,6", timezone_name="America/Recife",
    enabled=True, crawl_reels=False, results_limit=None, seed_results_limit=None,
    reels_results_limit=None, reels_seed_results_limit=None,
    initial_lookback=None,
    cursor_posts_at=None, cursor_reels_at=None, consecutive_failures=0, link_venue=True,
) -> str:
    fields = {
        "kind": kind, "cron": cron, "timezone": timezone_name, "enabled": enabled,
        "crawl_reels": crawl_reels,
    }
    if results_limit is not None:
        fields["results_limit"] = results_limit
    if seed_results_limit is not None:
        fields["seed_results_limit"] = seed_results_limit
    if reels_results_limit is not None:
        fields["reels_results_limit"] = reels_results_limit
    if reels_seed_results_limit is not None:
        fields["reels_seed_results_limit"] = reels_seed_results_limit
    if initial_lookback is not None:
        fields["initial_lookback"] = initial_lookback
    if cursor_posts_at is not None:
        fields["cursor_posts_at"] = cursor_posts_at
    if cursor_reels_at is not None:
        fields["cursor_reels_at"] = cursor_reels_at
    if consecutive_failures:
        fields["consecutive_failures"] = consecutive_failures
    context.ic_dao.upsert_crawl_target(handle, fields)
    if link_venue and kind == "venue":
        _create_venue(context, handle)
    return handle


def _program_posts(context, handle, stream, posts) -> None:
    context.ic_apify.program_posts(handle, stream, posts)


# ── Background ────────────────────────────────────────────────────────────
@given("the Instagram crawl scheduler is configured")
def step_given_scheduler_configured(context):
    _reset_context(context)


# ── Where a crawl starts ──────────────────────────────────────────────────
@given("a crawl target that has never been crawled")
def step_given_new_target(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "newtarget")


@given("a crawl target whose newest seen post is known")
def step_given_target_with_known_cursor_for_bound(context):
    _ensure_context(context)
    context.ic_cursor = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    context.ic_handle = _create_target(context, "resumetarget", cursor_posts_at=context.ic_cursor)


@given("a crawl target with its own initial lookback and no cursor")
def step_given_target_with_own_lookback(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "ownlookbacktarget", initial_lookback="45 days")


@when("its scheduled crawl runs")
def step_when_scheduled_crawl_runs(context):
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@then("the scrape is bounded to the default three-month lookback")
def step_then_bounded_to_default_lookback(context):
    call = context.ic_apify.calls[0]
    assert call["only_posts_newer_than"] == context.ic_config.default_initial_lookback, call


@then("the scrape is also bounded by the target's seed result cap")
def step_then_bounded_by_default_result_cap(context):
    call = context.ic_apify.calls[0]
    assert call["results_limit"] == context.ic_config.default_seed_results_limit, call


@given("a crawl target with a steady-state cap but no cursor")
def step_given_target_with_steady_state_cap_no_cursor(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "steadycapnocursortarget", results_limit=5)


@then("the scrape is bounded by the seed cap, not the steady-state cap")
def step_then_bounded_by_seed_cap_not_steady_state(context):
    call = context.ic_apify.calls[0]
    assert call["results_limit"] == context.ic_config.default_seed_results_limit, call
    assert call["results_limit"] != 5, call


@given("a crawl target with its own seed cap and no cursor")
def step_given_target_with_own_seed_cap_no_cursor(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "ownseedcaptarget", seed_results_limit=42)


@then("the scrape is bounded by that target's own seed cap")
def step_then_bounded_by_own_seed_cap(context):
    call = context.ic_apify.calls[0]
    assert call["results_limit"] == 42, call


@then("the scrape is bounded to that post's time minus the configured overlap")
def step_then_bounded_to_cursor_minus_overlap(context):
    call = context.ic_apify.calls[0]
    expected = context.ic_cursor - timedelta(hours=context.ic_config.overlap_hours)
    assert call["only_posts_newer_than"] == _format_utc(expected), call


@then("no post older than that bound is requested")
def step_then_bound_param_present(context):
    call = context.ic_apify.calls[0]
    assert call["only_posts_newer_than"], call


@then("the scrape is bounded to that target's lookback")
def step_then_bounded_to_targets_own_lookback(context):
    call = context.ic_apify.calls[0]
    assert call["only_posts_newer_than"] == "45 days", call


# ── How the cursor moves ─────────────────────────────────────────────────
@given("a crawl target that returns several posts of differing ages")
def step_given_target_returns_several_posts(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "multitarget")
    _program_posts(context, context.ic_handle, "posts", [
        _post("p1", "2026-08-01T10:00:00.000Z"),
        _post("p3", "2026-08-05T10:00:00.000Z"),  # newest, deliberately not last
        _post("p2", "2026-08-03T10:00:00.000Z"),
    ])


@when("the crawl succeeds")
def step_when_crawl_succeeds(context):
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@then("the target's cursor is the newest returned post's time")
def step_then_cursor_is_newest(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["cursor_posts_at"] == datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc), row


@then("the cursor is stored as UTC")
def step_then_cursor_stored_as_utc(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    cursor = row["cursor_posts_at"]
    assert cursor.tzinfo is not None and cursor.utcoffset() == timedelta(0), cursor


@given("a crawl target with a known cursor")
def step_given_target_with_a_known_cursor(context):
    _ensure_context(context)
    context.ic_cursor = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    context.ic_handle = _create_target(context, "cursortarget", cursor_posts_at=context.ic_cursor)


@when("its crawl fails partway through")
def step_when_crawl_fails(context):
    context.ic_apify.fail_next(context.ic_handle, "posts", RuntimeError("boom"))
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@then("the target's cursor is unchanged")
def step_then_cursor_unchanged(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["cursor_posts_at"] == context.ic_cursor, row


@when("its crawl returns no posts")
def step_when_crawl_returns_no_posts(context):
    _program_posts(context, context.ic_handle, "posts", [])
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@then("nothing is archived or extracted")
def step_then_nothing_archived_or_extracted(context):
    assert context.ic_media_store.images == []
    assert context.ic_media_store.manifests == {}
    assert context.ic_openai.calls == 0


# ── Paying once per handle ───────────────────────────────────────────────
@given("two venues that share one Instagram handle")
def step_given_two_venues_share_handle(context):
    _ensure_context(context)
    context.ic_shared_handle = "sharedhandle"
    _create_venue(context, context.ic_shared_handle)
    _create_venue(context, context.ic_shared_handle)


@given("a single crawl target for that handle")
def step_given_single_target_for_shared_handle(context):
    context.ic_handle = context.ic_shared_handle
    context.ic_dao.upsert_crawl_target(
        context.ic_handle, {"kind": "venue", "cron": "0 22 * * 5,6", "enabled": True},
    )


@when("the scheduled crawl runs")
def step_when_the_scheduled_crawl_runs(context):
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@then("the handle is scraped exactly once")
def step_then_handle_scraped_once(context):
    calls = [
        c for c in context.ic_apify.calls
        if c["handle"] == context.ic_handle and c["results_type"] == "posts"
    ]
    assert len(calls) == 1, calls


# ── Reels ─────────────────────────────────────────────────────────────────
@given("a crawl target with reels enabled")
def step_given_target_with_reels_enabled(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "reelstarget", crawl_reels=True)


@then("posts and reels are scraped as two separate runs")
def step_then_posts_and_reels_two_runs(context):
    types = [c["results_type"] for c in context.ic_apify.calls if c["handle"] == context.ic_handle]
    assert types == ["posts", "reels"], types


@given("a crawl target with reels enabled and both cursors set")
def step_given_target_reels_enabled_both_cursors(context):
    _ensure_context(context)
    context.ic_posts_cursor = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    context.ic_reels_cursor = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
    context.ic_handle = _create_target(
        context, "indepcursorstarget", crawl_reels=True,
        cursor_posts_at=context.ic_posts_cursor, cursor_reels_at=context.ic_reels_cursor,
    )


@when("a crawl returns new posts but no new reels")
def step_when_crawl_returns_new_posts_but_no_new_reels(context):
    _program_posts(context, context.ic_handle, "posts", [_post("np1", "2026-08-05T09:00:00.000Z")])
    _program_posts(context, context.ic_handle, "reels", [])
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@then("the posts cursor advances")
def step_then_posts_cursor_advances(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["cursor_posts_at"] == datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc), row


@then("the reels cursor is unchanged")
def step_then_reels_cursor_unchanged(context):
    row = context.ic_dao.get_crawl_target(context.ic_handle)
    assert row["cursor_reels_at"] == context.ic_reels_cursor, row


@given("a crawl target with reels disabled")
def step_given_target_with_reels_disabled(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "noreelstarget", crawl_reels=False)


@then("only posts are scraped")
def step_then_only_posts_scraped(context):
    types = [c["results_type"] for c in context.ic_apify.calls if c["handle"] == context.ic_handle]
    assert types == ["posts"], types


@given("a crawl target with reels enabled and only a posts seed cap")
def step_given_target_reels_enabled_only_posts_seed_cap(context):
    _ensure_context(context)
    context.ic_handle = _create_target(
        context, "reelsfallbackcaptarget", crawl_reels=True, seed_results_limit=33,
    )


@then("reels is bounded by that same posts seed cap")
def step_then_reels_bounded_by_posts_seed_cap(context):
    calls_by_type = {
        c["results_type"]: c["results_limit"]
        for c in context.ic_apify.calls if c["handle"] == context.ic_handle
    }
    assert calls_by_type["reels"] == 33, calls_by_type


@given("a crawl target with its own reels seed cap alongside a posts seed cap")
def step_given_target_with_own_reels_seed_cap(context):
    _ensure_context(context)
    context.ic_handle = _create_target(
        context, "reelsowncaptarget", crawl_reels=True,
        seed_results_limit=33, reels_seed_results_limit=7,
    )


@then("reels is bounded by its own cap and posts is bounded by its own")
def step_then_reels_and_posts_bounded_by_their_own_caps(context):
    calls_by_type = {
        c["results_type"]: c["results_limit"]
        for c in context.ic_apify.calls if c["handle"] == context.ic_handle
    }
    assert calls_by_type["posts"] == 33, calls_by_type
    assert calls_by_type["reels"] == 7, calls_by_type


# ── Pinned posts ──────────────────────────────────────────────────────────
@given("a crawl target whose profile pins an old post")
def step_given_target_pins_old_post(context):
    _ensure_context(context)
    context.ic_cursor = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    context.ic_handle = _create_target(context, "pinnedtarget", cursor_posts_at=context.ic_cursor)
    fresh = _post("freshpost", "2026-08-05T10:00:00.000Z")
    old_pinned = _post("oldpinnedpost", "2023-09-20T14:07:30.000Z", is_pinned=True)
    _program_posts(context, context.ic_handle, "posts", [fresh, old_pinned])


@then("the pinned post is not archived")
def step_then_pinned_not_archived(context):
    assert all("oldpinnedpost" not in key for key in context.ic_media_store.images), (
        context.ic_media_store.images
    )


@then("the pinned post is counted as dropped")
def step_then_pinned_counted_dropped(context):
    dropped = context.ic_last_report["streams"]["posts"]["dropped_pinned"]
    assert dropped == 1, context.ic_last_report


@given("a crawl whose only returned post is an old pinned one")
def step_given_crawl_only_returns_old_pinned(context):
    _ensure_context(context)
    context.ic_cursor = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    context.ic_handle = _create_target(context, "onlypinnedtarget", cursor_posts_at=context.ic_cursor)
    _program_posts(
        context, context.ic_handle, "posts",
        [_post("onlyoldpinned", "2023-09-20T14:07:30.000Z", is_pinned=True)],
    )


@when("the crawl completes")
def step_when_crawl_completes(context):
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


# ── Spending ceilings ─────────────────────────────────────────────────────
@given("the monthly result budget is exhausted")
def step_given_budget_exhausted(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "budgettarget")
    context.ic_budget.increment_month(
        context.ic_budget.current_year_month_utc(context.ic_now),
        context.ic_config.monthly_result_budget,
    )


@when("a scheduled crawl is due")
def step_when_scheduled_crawl_is_due(context):
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@then("no scrape is requested at all")
def step_then_no_scrape_requested_at_all(context):
    calls = [c for c in context.ic_apify.calls if c["handle"] == context.ic_handle]
    assert calls == [], calls


@then("the refusal is recorded")
def step_then_refusal_recorded(context):
    assert context.ic_last_report["streams"]["posts"]["outcome"] == "skipped_budget", (
        context.ic_last_report
    )


@given("a crawl target with a known cursor and a steady-state result cap")
def step_given_target_with_cursor_and_steady_state_cap(context):
    _ensure_context(context)
    # A CURSOR, deliberately — `results_limit` is now the STEADY-STATE cap
    # specifically (see the seed-cap scenarios above); a null-cursor target
    # would use the seed cap instead, and this scenario is about the other one.
    context.ic_cursor = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    context.ic_handle = _create_target(
        context, "captarget", results_limit=3, cursor_posts_at=context.ic_cursor,
    )


@then("the scrape carries both the date bound and the cap")
def step_then_scrape_carries_bound_and_cap(context):
    call = context.ic_apify.calls[0]
    expected_bound = _format_utc(context.ic_cursor - timedelta(hours=context.ic_config.overlap_hours))
    assert call["only_posts_newer_than"] == expected_bound, call
    assert call["results_limit"] == 3, call


@given("the monthly result budget has less remaining than the target's cap")
def step_given_budget_less_than_cap(context):
    _ensure_context(context)
    # No cursor -> this is a SEED run, capped by the seed cap (default 200,
    # per context.ic_config.default_seed_results_limit) — deliberately not
    # overridden, so "the target's cap" here IS that default.
    context.ic_handle = _create_target(context, "overbudgetcaptarget")
    # Default monthly budget is 1000; spend all but 50 of it so the
    # REMAINING (50) is smaller than the target's own (seed) cap (200).
    context.ic_budget.increment_month(
        context.ic_budget.current_year_month_utc(context.ic_now), 950,
    )


@then("the scrape is capped to the remaining budget, not the full cap")
def step_then_scrape_capped_to_remaining_budget(context):
    call = context.ic_apify.calls[0]
    assert call["results_limit"] == 50, call


@given("several crawl targets are due")
def step_given_several_targets_due(context):
    _ensure_context(context)
    context.ic_handles = [_create_target(context, f"cycletarget{i}") for i in range(3)]


@when("the first target's scrape reports credit exhaustion")
def step_when_first_target_credit_exhausted(context):
    context.ic_apify.fail_next(
        context.ic_handles[0], "posts", ApifyCreditExhaustedError("no credit remaining")
    )
    context.ic_last_reports = _run(context.ic_service.run_due_targets(context.ic_handles))


@then("no further target is scraped in that cycle")
def step_then_no_further_target_scraped(context):
    scraped_handles = {c["handle"] for c in context.ic_apify.calls}
    assert scraped_handles == {context.ic_handles[0]}, scraped_handles
    assert context.ic_last_reports["stopped_early"] is True, context.ic_last_reports


# ── Which targets run ─────────────────────────────────────────────────────
@given("a crawl target that is disabled")
def step_given_target_that_is_disabled(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "disabledtarget", enabled=False)


@when("its schedule fires")
def step_when_schedule_fires(context):
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_handle))


@then("no scrape is requested for it")
def step_then_no_scrape_requested_for_it(context):
    calls = [c for c in context.ic_apify.calls if c["handle"] == context.ic_handle]
    assert calls == [], calls


@given("a crawl target past its consecutive-failure threshold")
def step_given_target_past_failure_threshold(context):
    _ensure_context(context)
    context.ic_handle = _create_target(
        context, "failingtarget", consecutive_failures=context.ic_config.max_consecutive_failures,
    )


@given("one target scheduled for weekend nights and another scheduled monthly")
def step_given_two_targets_different_schedules(context):
    _ensure_context(context)
    context.ic_weekend_handle = _create_target(context, "weekendtarget", cron="0 22 * * 5,6")
    context.ic_monthly_handle = _create_target(context, "monthlytarget", cron="0 6 1 * *")


@when("only the weekend schedule fires")
def step_when_only_weekend_schedule_fires(context):
    # Each target is registered on its OWN CronTrigger (main.py's
    # CrawlScheduleSync builds one APScheduler job per handle) — this
    # simulates only THAT one job's fire, without needing a real clock or a
    # live APScheduler in this test. The scheduling-mechanics side (which
    # trigger fires when) is APScheduler's own well-tested behavior; what
    # this feature owns and must prove is that firing one target's job never
    # touches another target.
    context.ic_last_report = _run(context.ic_service.run_target(context.ic_weekend_handle))


@then("only the weekend target is scraped")
def step_then_only_weekend_target_scraped(context):
    scraped = {c["handle"] for c in context.ic_apify.calls}
    assert scraped == {context.ic_weekend_handle}, scraped


# ── Chaining ──────────────────────────────────────────────────────────────
@given("a crawl target that returns new posts")
def step_given_target_returns_new_posts_for_chaining(context):
    _ensure_context(context)
    context.ic_handle = _create_target(context, "chaintarget")
    _program_posts(context, context.ic_handle, "posts", [
        _post(
            "chainpost1", "2026-08-05T20:00:00.000Z",
            caption="Festa hoje! Ingressos abertos, vem cedo.",
            image_urls=["https://cdn.example.com/chainpost1.jpg"],
        ),
    ])
    context.ic_openai.program(_extraction_json())


@then("the new posts are archived")
def step_then_new_posts_are_archived(context):
    assert context.ic_media_store.images, "expected at least one archived image"
    assert any("venue_id=" in key for key in context.ic_media_store.images)


@then("event extraction runs for them")
def step_then_event_extraction_runs_for_them(context):
    assert context.ic_openai.calls >= 1, "expected the extraction OpenAI client to be called"
