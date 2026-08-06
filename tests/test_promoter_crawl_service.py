"""Unit coverage for PromoterCrawlService: config parsing, account
selection, and the manual-link-survives-a-later-crawl guarantee. See
plans/260804_instagram-promoter-events.md §C.

BDD (instagram-promoter-events.feature) proves the full crawl->extract->
resolve pipeline end-to-end; this isolates the pieces a plain assertion
protects better than a scenario: config validation, the per-account bound
never silently disappearing, and the fake's own contract (exhaustion RAISES,
never returns a default — the false-green trap two previous executors of
this plan's sibling work fell into).
"""
import asyncio
from datetime import datetime, timezone

import pytest

from app.dao.venue_repository import VenueRepository
from app.models.venue import Venue
from app.services.promoter_crawl_service import (
    DEFAULT_MAX_ACCOUNTS,
    InvalidPromoterCrawlConfig,
    PromoterAccountUnavailable,
    PromoterCrawlService,
    parse_promoter_crawl_config,
)
from tests.rds_fake import InMemoryRdsVenueStore


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakePostsClient:
    def __init__(self):
        self.posts_by_handle: dict[str, list[dict]] = {}
        self.unavailable: set[str] = set()
        self.calls: list[str] = []

    async def fetch_recent_posts(self, handle, *, results_limit):
        self.calls.append(handle)
        if handle in self.unavailable:
            raise PromoterAccountUnavailable(f"{handle} unavailable")
        return list(self.posts_by_handle.get(handle, []))[:results_limit]


class _FakeOpenAIClient:
    """Exhaustion RAISES rather than returning a default — a fake that
    silently ran out of programmed responses and returned something
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
        multi-event-posts.md), never extract. Every test in this file still
        programs a single flat event JSON (via _extraction_json) — wrapping
        it into the {"events": [...]} shape here means a single-event post
        behaves exactly as it did before, with zero changes to any existing
        test's Given/programming code."""
        import json as _json

        self.calls += 1
        if not self._responses:
            raise AssertionError("fake OpenAI client called more times than programmed")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple) and item and item[0] == "__truncated__":
            return item[1], True
        wrapped = _json.dumps({"events": [_json.loads(item)]})
        return wrapped, False


def _extraction_json(**overrides) -> str:
    import json

    payload = {
        "title": "Festa", "description": None, "date_text": None, "time_text": None,
        "is_recurring": False, "recurrence_text": None, "lineup": [], "ticket_url": None,
        "price_text": None, "location_text": None, "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


@pytest.fixture
def dao():
    store = InMemoryRdsVenueStore()
    return VenueRepository(client=None, rds_store=store)


class TestParsePromoterCrawlConfig:
    def test_defaults(self):
        cfg = parse_promoter_crawl_config(None, default_max_posts_per_account=15)
        assert cfg["handles"] == []
        assert cfg["max_accounts"] == DEFAULT_MAX_ACCOUNTS
        assert cfg["max_posts_per_account"] == 15
        assert cfg["dry_run"] is False

    def test_the_per_account_bound_is_not_optional(self):
        """0 (or omitted) falls back to the configured default rather than
        meaning 'no cap' — a promoter posts far more than a venue does, and
        this guarantee must never disappear behind a permissive value."""
        cfg = parse_promoter_crawl_config(
            {"max_posts_per_account": 0}, default_max_posts_per_account=15,
        )
        assert cfg["max_posts_per_account"] == 15

    def test_handles_accepts_a_comma_separated_string(self):
        cfg = parse_promoter_crawl_config(
            {"handles": "a, @b, c"}, default_max_posts_per_account=15,
        )
        assert cfg["handles"] == ["a", "@b", "c"]

    def test_handles_accepts_a_list(self):
        cfg = parse_promoter_crawl_config(
            {"handles": ["a", "b"]}, default_max_posts_per_account=15,
        )
        assert cfg["handles"] == ["a", "b"]

    def test_a_negative_value_is_rejected(self):
        with pytest.raises(InvalidPromoterCrawlConfig):
            parse_promoter_crawl_config(
                {"max_accounts": -1}, default_max_posts_per_account=15,
            )

    def test_a_non_integer_value_is_rejected(self):
        with pytest.raises(InvalidPromoterCrawlConfig):
            parse_promoter_crawl_config(
                {"lookback_days": "not a number"}, default_max_posts_per_account=15,
            )


class TestAccountSelection:
    def test_only_active_accounts_are_selected_by_default(self, dao):
        dao.upsert_promoter_account("active1", {"status": "active"})
        dao.upsert_promoter_account("candidate1", {"status": "candidate"})
        service = PromoterCrawlService(venue_dao=dao, posts_client=_FakePostsClient())
        report = _run(service.run({}))
        handles = [a["handle"] for a in report["accounts"]]
        assert handles == ["active1"]

    def test_an_explicit_handle_list_still_excludes_a_non_active_account(self, dao):
        """A candidate or paused account named directly must not be
        crawlable by working around the empty-list default."""
        dao.upsert_promoter_account("candidate1", {"status": "candidate"})
        service = PromoterCrawlService(venue_dao=dao, posts_client=_FakePostsClient())
        report = _run(service.run({"handles": "candidate1"}))
        assert report["accounts"] == []

    def test_max_accounts_bounds_the_selection(self, dao):
        for i in range(5):
            dao.upsert_promoter_account(f"acct_{i}", {"status": "active"})
        service = PromoterCrawlService(venue_dao=dao, posts_client=_FakePostsClient())
        report = _run(service.run({"max_accounts": 2}))
        assert len(report["accounts"]) == 2


class TestAccountUnavailability:
    def test_one_unavailable_account_does_not_stop_the_run(self, dao):
        dao.upsert_promoter_account("bad", {"status": "active"})
        dao.upsert_promoter_account("good", {"status": "active"})
        posts_client = _FakePostsClient()
        posts_client.unavailable.add("bad")
        posts_client.posts_by_handle["good"] = [{
            "shortcode": "p1", "caption": "sem marcador de evento",
            "permalink": "https://instagram.com/p/p1", "timestamp": None, "image_urls": [],
        }]
        service = PromoterCrawlService(venue_dao=dao, posts_client=posts_client)
        report = _run(service.run({}))

        outcomes = {a["handle"]: a["outcome"] for a in report["accounts"]}
        assert outcomes["bad"] == "unavailable"
        assert outcomes["good"] == "crawled"
        # Both were attempted — the bad one did not short-circuit the run.
        assert set(posts_client.calls) == {"bad", "good"}


class TestManualLinkSurvivesReResolution:
    def test_a_manual_link_is_not_overwritten_by_a_later_crawl(self, dao):
        dao.upsert_venue(Venue(venue_id="v_auto", venue_name="Auto Venue", venue_lat=-8.05, venue_lng=-34.88))
        dao.upsert_venue(
            Venue(venue_id="v_manual", venue_name="Manually Chosen", venue_lat=-8.05, venue_lng=-34.88)
        )
        from app.models.instagram import VenueInstagram

        dao.set_venue_instagram(VenueInstagram(venue_id="v_auto", instagram_handle="autovenue", status="found"))

        dao.upsert_promoter_account("promo", {"status": "active"})
        post = {
            "shortcode": "p1", "caption": "Ingressos abertos! Hoje é no @autovenue!",
            "permalink": "https://instagram.com/p/p1",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": [],
        }
        posts_client = _FakePostsClient()
        posts_client.posts_by_handle["promo"] = [post]
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())

        service = PromoterCrawlService(venue_dao=dao, posts_client=posts_client, openai_client=openai_client)
        _run(service.run({}))

        row = dao.get_event_by_source("promo", "p1")
        assert row["venue_id"] == "v_auto"  # sanity: auto-linked first

        dao.update_event(row["event_id"], {
            "venue_id": "v_manual", "location_resolution": "manual",
            "linked_by": "operator_x", "linked_at": datetime.now(timezone.utc),
        })

        # A second crawl of the SAME post must not move it back.
        openai_client.program(_extraction_json())
        _run(service.run({}))

        row = dao.get_event_by_source("promo", "p1")
        assert row["venue_id"] == "v_manual"
        assert row["location_resolution"] == "manual"
        assert row["linked_by"] == "operator_x"


class TestFakeOpenAIClientExhaustion:
    """The fake's own contract, proven in isolation: exhaustion must RAISE,
    never silently return a default that could pass a test by accident."""

    def test_calling_past_the_programmed_responses_raises(self):
        client = _FakeOpenAIClient()
        client.program(_extraction_json())
        _run(client.extract(caption="x"))
        with pytest.raises(AssertionError):
            _run(client.extract(caption="y"))
