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
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.event_venue_resolution import build_handle_index, build_venue_catalog
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


# ── plans/260810_post-kind-and-post-extraction-attribution.md §A/§B/§C ─────
class TestNonEventPersistsWithItsOwnType:
    """plans/260811_post-items-and-categories.md §B retires the drop
    `260810_post-kind-and-post-extraction-attribution.md` introduced (a post
    classified as anything other than `event` produced no row at all) — see
    event_extraction_service.py's own TestNonEventPersistsWithItsOwnType for
    the venue-post half of this; this is the promoter-post half of the SAME
    rule, since both callers share `resolve_event_datetime`'s inputs but
    each builds its own `prepared_events` list."""

    def test_a_menu_kind_post_is_persisted_with_that_post_type(self, dao):
        dao.upsert_promoter_account("promo", {"status": "active"})
        post = {
            "shortcode": "menu1", "caption": "Ingressos abertos! Prato do dia!",
            "permalink": "https://instagram.com/p/menu1",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": [],
        }
        posts_client = _FakePostsClient()
        posts_client.posts_by_handle["promo"] = [post]
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(kind="menu", title="Risoto"))

        service = PromoterCrawlService(venue_dao=dao, posts_client=posts_client, openai_client=openai_client)
        _run(service.run({}))

        row = dao.get_event_by_source("promo", "menu1")
        assert row is not None
        assert row["post_type"] == "menu"
        assert row["title"] == "Risoto"
        assert dao.list_events() != []


class TestLocationTextFallbackToCaption:
    """plans/260810_post-kind-and-post-extraction-attribution.md §C: the
    caption fallback is opt-in (default False), so a REAL promoter
    account's resolution against its full servable catalog is unaffected —
    only `InstagramCrawlChainer._chain_shared_handle` (a bounded, two-or-
    three-venue candidate set) opts in. Called directly against
    `_process_post` rather than through `run()`, since the flag itself is
    the unit under test."""

    def _venues(self, dao):
        dao.upsert_venue(Venue(venue_id="v1", venue_name="Zetta Lounge", venue_lat=-8.05, venue_lng=-34.88))
        return build_venue_catalog(dao), build_handle_index(dao)

    def test_default_false_never_falls_back_to_the_caption(self, dao):
        venues, handle_index = self._venues(dao)
        post = {
            "shortcode": "fb1", "caption": "Zetta Lounge, sexta 22h",
            "permalink": "https://instagram.com/p/fb1",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": [],
        }
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(location_text=None))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=post, venues=venues, handle_index=handle_index,
            now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "fb1")
        assert row["venue_id"] is None, row

    def test_true_falls_back_to_the_caption_when_location_text_is_absent(self, dao):
        venues, handle_index = self._venues(dao)
        post = {
            "shortcode": "fb2", "caption": "Zetta Lounge, sexta 22h",
            "permalink": "https://instagram.com/p/fb2",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": [],
        }
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(location_text=None))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=post, venues=venues, handle_index=handle_index,
            now=datetime.now(timezone.utc), location_text_fallback_to_caption=True,
        ))

        row = dao.get_event_by_source("promo", "fb2")
        assert row["venue_id"] == "v1", row

    def test_true_still_prefers_a_real_location_text_over_the_caption(self, dao):
        dao.upsert_venue(Venue(venue_id="v1", venue_name="Zetta Lounge", venue_lat=-8.05, venue_lng=-34.88))
        dao.upsert_venue(Venue(venue_id="v2", venue_name="Casa Rosa", venue_lat=-8.06, venue_lng=-34.90))
        venues = build_venue_catalog(dao)
        handle_index = build_handle_index(dao)
        post = {
            "shortcode": "fb3", "caption": "Zetta Lounge, sexta 22h",
            "permalink": "https://instagram.com/p/fb3",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": [],
        }
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(location_text="Casa Rosa"))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=post, venues=venues, handle_index=handle_index,
            now=datetime.now(timezone.utc), location_text_fallback_to_caption=True,
        ))

        row = dao.get_event_by_source("promo", "fb3")
        assert row["venue_id"] == "v2", row  # Casa Rosa, from location_text -- never the caption's Zetta


class TestSourceKindAndMetricsParity:
    """plans/260810_date-correctness-review-reasons-and-path-parity.md §D:
    `_process_post` is the machinery `InstagramCrawlChainer._chain_shared_
    handle` reuses wholesale for a VENUE's own post -- these three new,
    optional parameters are what let that caller report the truth (venue_
    post, not promoter_post) and the same metrics the single-venue path
    already reports, without changing a byte of the real promoter path's
    default behaviour. Called directly against `_process_post`, since the
    parameters themselves are the unit under test (same pattern as
    TestLocationTextFallbackToCaption above)."""

    def _venues(self, dao):
        dao.upsert_venue(Venue(venue_id="v1", venue_name="Zetta Lounge", venue_lat=-8.05, venue_lng=-34.88))
        return build_venue_catalog(dao), build_handle_index(dao)

    def _post(self, shortcode: str, caption: str) -> dict:
        return {
            "shortcode": shortcode, "caption": caption,
            "permalink": f"https://instagram.com/p/{shortcode}",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": [],
        }

    def test_default_source_kind_is_still_promoter_post(self, dao):
        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(date_text="15/08", time_text="20h"))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=self._post("sk1", "Zetta Lounge, sexta 22h"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
        ))
        row = dao.get_event_by_source("promo", "sk1")
        assert row["source_kind"] == "promoter_post", row

    def test_source_kind_venue_post_is_stamped_when_passed(self, dao):
        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(date_text="15/08", time_text="20h"))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)

        _run(service._process_post(
            handle="sharedhandle", post=self._post("sk2", "Zetta Lounge, sexta 22h"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
            source_kind="venue_post",
        ))
        row = dao.get_event_by_source("sharedhandle", "sk2")
        assert row["source_kind"] == "venue_post", row

    def test_source_kind_venue_post_also_stamps_an_extraction_failure_placeholder(self, dao):
        """The provenance fix must hold on the FAILURE path too -- a shared-
        handle post whose extraction blew up must not fall back to the
        default `promoter_post` label."""
        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(RuntimeError("boom"))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)

        _run(service._process_post(
            handle="sharedhandle", post=self._post("sk3", "Zetta Lounge, sexta 22h"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
            source_kind="venue_post",
        ))
        row = dao.get_event_by_source("sharedhandle", "sk3")
        assert row is not None, "expected an extraction_failed placeholder row"
        assert row["source_kind"] == "venue_post", row
        assert row["status"] == "extraction_failed", row

    def test_attribution_outcomes_collects_the_resolution_ladders_answer(self, dao):
        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(date_text="15/08", time_text="20h", location_text=None))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)

        outcomes: list[str] = []
        _run(service._process_post(
            handle="promo", post=self._post("ao1", "Zetta Lounge, sexta 22h"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
            location_text_fallback_to_caption=True, attribution_outcomes=outcomes,
        ))
        assert outcomes == ["auto"], outcomes

    def test_attribution_outcomes_is_untouched_when_not_supplied(self, dao):
        """None (the default) costs nothing extra -- a real promoter
        account never has to pay for a list it does not use."""
        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(date_text="15/08", time_text="20h"))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)

        # No AttributeError/TypeError -- attribution_outcomes defaults to
        # None and is simply never appended to.
        _run(service._process_post(
            handle="promo", post=self._post("ao2", "Zetta Lounge, sexta 22h"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
        ))

    def test_report_kind_metric_false_by_default_never_bumps_event_extraction_posts_total(self, dao):
        from prometheus_client import REGISTRY

        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(date_text="15/08", time_text="20h"))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)
        before = REGISTRY.get_sample_value(
            "event_extraction_posts_total", {"outcome": "extracted", "kind": "event"},
        ) or 0.0

        _run(service._process_post(
            handle="promo", post=self._post("km1", "Zetta Lounge, sexta 22h"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
        ))
        after = REGISTRY.get_sample_value(
            "event_extraction_posts_total", {"outcome": "extracted", "kind": "event"},
        ) or 0.0
        assert after == before, (before, after)

    def test_report_kind_metric_true_bumps_event_extraction_posts_total_with_the_event_kind(self, dao):
        from prometheus_client import REGISTRY

        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(kind="event", date_text="15/08", time_text="20h"))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)
        before = REGISTRY.get_sample_value(
            "event_extraction_posts_total", {"outcome": "extracted", "kind": "event"},
        ) or 0.0

        _run(service._process_post(
            handle="sharedhandle", post=self._post("km2", "Zetta Lounge, sexta 22h"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
            report_kind_metric=True,
        ))
        after = REGISTRY.get_sample_value(
            "event_extraction_posts_total", {"outcome": "extracted", "kind": "event"},
        ) or 0.0
        assert after - before == 1.0, (before, after)

    def test_report_kind_metric_true_persists_a_non_event_kind_as_extracted(self, dao):
        """plans/260811_post-items-and-categories.md §B: a menu-kind post is
        persisted now (with `post_type="menu"`), not dropped — so it reports
        the SAME "extracted" outcome an event-kind post would, still labeled
        `kind="menu"` (the retired "not_an_event" outcome could only ever
        fire for a post that produced no row)."""
        from prometheus_client import REGISTRY

        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(kind="menu", date_text=None, time_text=None))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)
        before = REGISTRY.get_sample_value(
            "event_extraction_posts_total", {"outcome": "extracted", "kind": "menu"},
        ) or 0.0

        _run(service._process_post(
            # A caption naming a ticketing term so the pre-filter still
            # qualifies the post -- the MODEL's own kind answer, not the
            # caption, is what "menu" tests here.
            handle="sharedhandle", post=self._post("km3", "Ingressos e cardapio, confira"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
            report_kind_metric=True,
        ))
        after = REGISTRY.get_sample_value(
            "event_extraction_posts_total", {"outcome": "extracted", "kind": "menu"},
        ) or 0.0
        assert after - before == 1.0, (before, after)
        row = dao.get_event_by_source("sharedhandle", "km3")
        assert row is not None
        assert row["post_type"] == "menu"


# ── plans/260811_extract-by-handle.md: the attribution closure moved ────────
class TestAttributionBehaviourPinnedAcrossExtraction:
    """`_process_post`'s venue-attribution closure was extracted verbatim
    into `event_venue_resolution.build_location_text_attribute_fn`, so
    `EventExtractionService._run_handles` can resolve a multi-venue handle's
    re-extracted posts through the SAME ladder call rather than a second
    one. This pins the REAL promoter path's own outcome — same inputs, same
    persisted fields, same `EVENT_VENUE_LINK_TOTAL` metric — through
    `_process_post` itself (never the extracted function directly), so a
    future edit to the shared function that changes promoter-crawl
    behaviour fails HERE, not silently.
    """

    def _venues(self, dao):
        dao.upsert_venue(Venue(venue_id="v1", venue_name="Zetta Lounge", venue_lat=-8.05, venue_lng=-34.88))
        dao.upsert_venue(Venue(venue_id="v2", venue_name="Casa Rosa", venue_lat=-8.06, venue_lng=-34.90))
        return build_venue_catalog(dao), build_handle_index(dao)

    def _post(self, shortcode: str, caption: str) -> dict:
        return {
            "shortcode": shortcode, "caption": caption,
            "permalink": f"https://instagram.com/p/{shortcode}",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": [],
        }

    def _metric(self, method: str, result: str) -> float:
        from prometheus_client import REGISTRY

        return REGISTRY.get_sample_value(
            "event_venue_link_total", {"method": method, "result": result},
        ) or 0.0

    def test_a_confident_name_match_auto_links_and_records_the_link_fields(self, dao):
        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(
            date_text="15/08", time_text="20h", location_text="Casa Rosa",
        ))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)
        before = self._metric("name_match", "auto")

        _run(service._process_post(
            handle="promo", post=self._post("pin_auto", "Ingressos abertos!"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "pin_auto")
        assert row["venue_id"] == "v2", row
        assert row["location_resolution"] == "auto", row
        assert row["location_confidence"] is not None, row
        assert row["linked_by"] == "name_match", row
        after = self._metric("name_match", "auto")
        assert after - before == 1.0, (before, after)

    def test_a_score_below_the_floor_leaves_the_event_unresolved(self, dao):
        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(
            date_text="15/08", time_text="20h", location_text="Somewhere else entirely",
        ))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)
        before = self._metric("none", "unresolved")

        _run(service._process_post(
            handle="promo", post=self._post("pin_unresolved", "Ingressos abertos!"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "pin_unresolved")
        assert row["venue_id"] is None, row
        assert row["location_resolution"] == "unresolved", row
        assert row["location_confidence"] is None, row
        assert row["linked_by"] is None, row
        after = self._metric("none", "unresolved")
        assert after - before == 1.0, (before, after)

    def test_a_caption_at_mention_auto_links_when_the_event_has_no_location_text(self, dao):
        """plans/260812_event-attribution-and-dates.md §A demoted a CAPTION
        mention below every per-event signal: with `location_text=None`
        (the event's own text says nothing), the caption's single, known-
        venue mention is still good evidence and auto-links — but now
        through the distinct `caption_handle_mention` method, not
        `handle_mention` (that value is reserved for a mention found in the
        EVENT's own location_text — see the next test)."""
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="zettalounge", status="found"))
        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(date_text="15/08", time_text="20h", location_text=None))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)
        before = self._metric("caption_handle_mention", "auto")

        _run(service._process_post(
            handle="promo", post=self._post("pin_mention", "Hoje é no @zettalounge! Ingressos abertos!"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "pin_mention")
        assert row["venue_id"] == "v1", row
        assert row["linked_by"] == "caption_handle_mention", row
        assert row["location_confidence"] == 1.0, row
        after = self._metric("caption_handle_mention", "auto")
        assert after - before == 1.0, (before, after)

    def test_an_at_mention_in_the_events_own_location_text_auto_links_by_identity(self, dao):
        """The TOP rung: an @-mention found in the event's OWN
        `location_text` auto-links via `handle_mention`, outranking
        anything the post's caption says (plans/260812_event-attribution-
        and-dates.md §A)."""
        dao.set_venue_instagram(VenueInstagram(venue_id="v1", instagram_handle="zettalounge", status="found"))
        venues, handle_index = self._venues(dao)
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(
            date_text="15/08", time_text="20h", location_text="@zettalounge",
        ))
        service = PromoterCrawlService(venue_dao=dao, posts_client=None, openai_client=openai_client)
        before = self._metric("handle_mention", "auto")

        _run(service._process_post(
            handle="promo", post=self._post("pin_mention_own", "Ingressos abertos!"),
            venues=venues, handle_index=handle_index, now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "pin_mention_own")
        assert row["venue_id"] == "v1", row
        assert row["linked_by"] == "handle_mention", row
        assert row["location_confidence"] == 1.0, row
        after = self._metric("handle_mention", "auto")
        assert after - before == 1.0, (before, after)


class TestFakeOpenAIClientExhaustion:
    """The fake's own contract, proven in isolation: exhaustion must RAISE,
    never silently return a default that could pass a test by accident."""

    def test_calling_past_the_programmed_responses_raises(self):
        client = _FakeOpenAIClient()
        client.program(_extraction_json())
        _run(client.extract(caption="x"))
        with pytest.raises(AssertionError):
            _run(client.extract(caption="y"))


class _FakeMediaStore:
    """Records every promoter-keyed write, mirroring
    tests/bdd/steps/instagram_promoter_events_steps.py's fake so a scenario
    can assert on the S3 key shape without touching real S3."""

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


class _FakeDownloader:
    """No real HTTP call — every url resolves to the same trivial bytes,
    unless the url is in `fail_urls`."""

    def __init__(self):
        self.calls = 0
        self.fail_urls: set[str] = set()

    async def download(self, url, timeout=15.0, max_bytes=None):
        self.calls += 1
        if url in self.fail_urls:
            raise RuntimeError("download failed")
        return b"FAKE_IMAGE_BYTES", "image/jpeg"


class TestPromoterEventCoverPhoto:
    """Defect 3 (2026-08-07 RCA): `cover_photo_key` is written for a venue
    post's events (event_extraction_service.py:484) but never for a
    promoter post's, even though `_archive_post_images` already archives the
    post's images to S3. A promoter event therefore carries only
    `source_permalink` — a perishable Instagram link whose CDN url behind it
    expires within the hour. Every event a promoter post produces must carry
    the post's archived cover key alongside its permalink."""

    def _service(self, dao, *, posts_client=None, openai_client=None,
                 media_store=None, downloader=None):
        return PromoterCrawlService(
            venue_dao=dao,
            posts_client=posts_client or _FakePostsClient(),
            openai_client=openai_client,
            media_store=media_store,
            downloader=downloader,
        )

    def test_the_event_records_the_archived_cover_key(self, dao):
        dao.upsert_promoter_account("promo", {"status": "active"})
        post = {
            "shortcode": "p1", "caption": "Ingressos abertos! Vem pro role.",
            "permalink": "https://instagram.com/p/p1",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": ["https://instagram.cdn.example/p1.jpg"],
        }
        posts_client = _FakePostsClient()
        posts_client.posts_by_handle["promo"] = [post]
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())
        media_store = _FakeMediaStore()

        service = self._service(
            dao, posts_client=posts_client, openai_client=openai_client,
            media_store=media_store, downloader=_FakeDownloader(),
        )
        _run(service.run({}))

        row = dao.get_event_by_source("promo", "p1")
        assert row["cover_photo_key"], "the event has no cover_photo_key"
        assert row["cover_photo_key"] == media_store.images[0]

    def test_the_permalink_survives_alongside_the_cover_key(self, dao):
        dao.upsert_promoter_account("promo", {"status": "active"})
        post = {
            "shortcode": "p1", "caption": "Ingressos abertos! Vem pro role.",
            "permalink": "https://instagram.com/p/p1",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": ["https://instagram.cdn.example/p1.jpg"],
        }
        posts_client = _FakePostsClient()
        posts_client.posts_by_handle["promo"] = [post]
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())

        service = self._service(
            dao, posts_client=posts_client, openai_client=openai_client,
            media_store=_FakeMediaStore(), downloader=_FakeDownloader(),
        )
        _run(service.run({}))

        row = dao.get_event_by_source("promo", "p1")
        assert row["cover_photo_key"]
        assert row["source_permalink"] == "https://instagram.com/p/p1"

    def test_every_event_from_a_multi_event_post_gets_the_same_cover(self, dao):
        """One post, several events, one cover each (slide-to-event
        alignment is out of scope — 260806_multi-event-posts.md)."""
        dao.upsert_promoter_account("promo", {"status": "active"})
        post = {
            "shortcode": "roundup", "caption": "Ingressos abertos! 3 eventos essa semana.",
            "permalink": "https://instagram.com/p/roundup",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": ["https://instagram.cdn.example/roundup.jpg"],
        }
        posts_client = _FakePostsClient()
        posts_client.posts_by_handle["promo"] = [post]

        class _MultiEventClient(_FakeOpenAIClient):
            """Returns a raw multi-event payload as-is, unlike the base
            fake (which wraps a single flat event dict for every other test
            in this file)."""

            async def extract_events(self, *, caption, image_data_uri=None, max_events):
                self.calls += 1
                item = self._responses.pop(0)
                return item, False

        import json

        client = _MultiEventClient()
        client.program(json.dumps({"events": [
            json.loads(_extraction_json(title="Festa A")),
            json.loads(_extraction_json(title="Festa B")),
            json.loads(_extraction_json(title="Festa C")),
        ]}))
        media_store = _FakeMediaStore()

        service = self._service(
            dao, posts_client=posts_client, openai_client=client,
            media_store=media_store, downloader=_FakeDownloader(),
        )
        _run(service.run({}))

        rows = dao.list_events_by_source("promo", "roundup")
        assert len(rows) == 3
        cover = media_store.images[0]
        assert all(r["cover_photo_key"] == cover for r in rows)

    def test_no_image_leaves_the_cover_key_null_but_the_event_persists(self, dao):
        dao.upsert_promoter_account("promo", {"status": "active"})
        post = {
            "shortcode": "p1", "caption": "Ingressos abertos! Vem pro role.",
            "permalink": "https://instagram.com/p/p1",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": [],  # the post stored no image
        }
        posts_client = _FakePostsClient()
        posts_client.posts_by_handle["promo"] = [post]
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())

        service = self._service(
            dao, posts_client=posts_client, openai_client=openai_client,
            media_store=_FakeMediaStore(), downloader=_FakeDownloader(),
        )
        report = _run(service.run({}))

        row = dao.get_event_by_source("promo", "p1")
        assert row is not None, "the event must still be persisted"
        assert row["cover_photo_key"] is None

    def test_an_archive_failure_for_this_posts_image_also_leaves_the_key_null(self, dao):
        dao.upsert_promoter_account("promo", {"status": "active"})
        post = {
            "shortcode": "p1", "caption": "Ingressos abertos! Vem pro role.",
            "permalink": "https://instagram.com/p/p1",
            "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
            "image_urls": ["https://instagram.cdn.example/p1.jpg"],
        }
        posts_client = _FakePostsClient()
        posts_client.posts_by_handle["promo"] = [post]
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())
        downloader = _FakeDownloader()
        downloader.fail_urls.add("https://instagram.cdn.example/p1.jpg")

        service = self._service(
            dao, posts_client=posts_client, openai_client=openai_client,
            media_store=_FakeMediaStore(), downloader=downloader,
        )
        _run(service.run({}))

        row = dao.get_event_by_source("promo", "p1")
        assert row is not None
        assert row["cover_photo_key"] is None


class TestSourceProvenance:
    """plans/260813_promoter-source-provenance-parity.md §A/§B:
    `source_media_type`/`source_uploaded_at` are now written on the promoter
    path too. Called directly against `_process_post` (same pattern as
    TestLocationTextFallbackToCaption/TestSourceKindAndMetricsParity above),
    since the assignment itself is the unit under test.

    Timestamp-parsing coverage lives here rather than re-testing
    `event_venue_targeting._parse_timestamp` in isolation: that helper is
    REUSED unchanged (imported, not reimplemented — see the plan's own
    "reuse whatever parsing the venue path already applies" instruction),
    so what actually needs proving is that THIS service wires it into
    `source_uploaded_at` without ever substituting `now()` or
    `first_seen_at` for a value it could not parse.
    """

    def _post(self, shortcode: str, *, timestamp="", media_type=None, **overrides) -> dict:
        post = {
            "shortcode": shortcode, "caption": "Ingressos abertos! Vem pro role.",
            "permalink": f"https://instagram.com/p/{shortcode}",
            "timestamp": timestamp, "image_urls": [],
        }
        if media_type is not None:
            post["post_type"] = media_type
        post.update(overrides)
        return post

    def _service(self, dao, *, openai_client=None) -> PromoterCrawlService:
        return PromoterCrawlService(
            venue_dao=dao, posts_client=None,
            openai_client=openai_client or _FakeOpenAIClient(),
        )

    # ── timestamp parsing: valid instant, empty, missing key, malformed ─────
    def test_a_valid_iso_timestamp_is_recorded_as_the_upload_time(self, dao):
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())
        post = self._post("ts_valid", timestamp="2026-08-12T15:26:00.000Z")
        service = self._service(dao, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=post, venues=[], handle_index={},
            now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "ts_valid")
        assert row["source_uploaded_at"] == datetime(2026, 8, 12, 15, 26, tzinfo=timezone.utc), row

    def test_an_empty_string_timestamp_is_stored_as_null(self, dao):
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())
        post = self._post("ts_empty", timestamp="")
        service = self._service(dao, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=post, venues=[], handle_index={},
            now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "ts_empty")
        assert row["source_uploaded_at"] is None, row

    def test_a_missing_timestamp_key_is_stored_as_null(self, dao):
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())
        post = self._post("ts_missing")
        del post["timestamp"]
        service = self._service(dao, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=post, venues=[], handle_index={},
            now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "ts_missing")
        assert row["source_uploaded_at"] is None, row

    def test_a_malformed_timestamp_is_stored_as_null_and_does_not_raise(self, dao):
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())
        post = self._post("ts_bad", timestamp="not-a-date")
        service = self._service(dao, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=post, venues=[], handle_index={},
            now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "ts_bad")
        assert row["source_uploaded_at"] is None, row

    def test_a_malformed_timestamp_logs_a_warning_naming_the_post(self, dao, caplog):
        import logging

        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())
        post = self._post("ts_bad_log", timestamp="not-a-date")
        service = self._service(dao, openai_client=openai_client)

        with caplog.at_level(logging.WARNING, logger="app.services.promoter_crawl_service"):
            _run(service._process_post(
                handle="promo", post=post, venues=[], handle_index={},
                now=datetime.now(timezone.utc),
            ))

        assert any(
            "promo/ts_bad_log" in r.message and "not-a-date" in r.message
            for r in caplog.records
        ), caplog.records

    def test_a_missing_timestamp_never_logs_a_warning(self, dao, caplog):
        """An absent Apify timestamp is a normal, expected gap -- only a
        NON-empty value that fails to parse is evidence Apify changed its
        format. Logging a warning for the common case would drown out the
        signal the warning exists to carry."""
        import logging

        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())
        post = self._post("ts_none", timestamp="")
        service = self._service(dao, openai_client=openai_client)

        with caplog.at_level(logging.WARNING, logger="app.services.promoter_crawl_service"):
            _run(service._process_post(
                handle="promo", post=post, venues=[], handle_index={},
                now=datetime.now(timezone.utc),
            ))

        assert caplog.records == [], caplog.records

    def test_upload_time_is_never_substituted_with_the_crawl_time(self, dao):
        """The regression this plan exists to prevent: a missing upload time
        must stay NULL, never silently fall back to `now`/`first_seen_at` --
        see the plan's own Evidence section on why that would poison
        260813_history-repair-dates.md's anchor."""
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())
        post = self._post("ts_no_fallback", timestamp="")
        crawl_time = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
        service = self._service(dao, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=post, venues=[], handle_index={}, now=crawl_time,
        ))

        row = dao.get_event_by_source("promo", "ts_no_fallback")
        assert row["source_uploaded_at"] is None, row
        assert row["first_seen_at"] == crawl_time, row  # the crawl time IS recorded -- just not here

    # ── the naming collision: media type vs. item type ───────────────────────
    def test_source_media_type_is_the_raw_dicts_post_type_not_the_items(self, dao):
        """The regression test for the naming collision the plan's own
        Evidence section names: the raw Apify dict's `post_type` key is the
        MEDIA type ("Video"/"Image"/"Sidecar"), completely unrelated to
        `events.post_item.post_type` (event/promotion/menu/food/other). A
        row must hold BOTH, distinctly -- media type "Video", item type
        "event" -- never one overwriting the other."""
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json(kind="event"))
        post = self._post("kind_collision", timestamp="2026-08-12T15:26:00.000Z", media_type="Video")
        service = self._service(dao, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=post, venues=[], handle_index={},
            now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "kind_collision")
        assert row["source_media_type"] == "Video", row
        assert row["post_type"] == "event", row

    def test_source_media_type_is_null_when_the_raw_dict_carries_none(self, dao):
        openai_client = _FakeOpenAIClient()
        openai_client.program(_extraction_json())
        post = self._post("no_media_type")
        assert "post_type" not in post
        service = self._service(dao, openai_client=openai_client)

        _run(service._process_post(
            handle="promo", post=post, venues=[], handle_index={},
            now=datetime.now(timezone.utc),
        ))

        row = dao.get_event_by_source("promo", "no_media_type")
        assert row["source_media_type"] is None, row

    def test_every_event_from_one_post_shares_the_same_provenance(self, dao):
        """One post, several events (plans/260806_multi-event-posts.md) --
        `source_media_type`/`source_uploaded_at` are facts about the PARSE,
        not any one event, so every row this post yields must carry
        identical values."""
        import json

        events_payload = json.dumps({"events": [
            {
                "title": "Show A", "description": None, "date_text": "12/08", "time_text": None,
                "is_recurring": False, "recurrence_text": None, "lineup": [], "ticket_url": None,
                "price_text": None, "location_text": None, "confidence": 0.9,
            },
            {
                "title": "Show B", "description": None, "date_text": "13/08", "time_text": None,
                "is_recurring": False, "recurrence_text": None, "lineup": [], "ticket_url": None,
                "price_text": None, "location_text": None, "confidence": 0.9,
            },
        ]})

        class _RawMultiEventClient:
            async def extract_events(self, *, caption, image_data_uri=None, max_events):
                return events_payload, False

        post = self._post("multi_same", timestamp="2026-08-12T15:26:00.000Z", media_type="Sidecar")
        service = self._service(dao, openai_client=_RawMultiEventClient())

        _run(service._process_post(
            handle="promo", post=post, venues=[], handle_index={},
            now=datetime.now(timezone.utc),
        ))

        rows = dao.list_events_by_source("promo", "multi_same")
        assert len(rows) == 2, rows
        assert {r["source_media_type"] for r in rows} == {"Sidecar"}, rows
        assert {r["source_uploaded_at"] for r in rows} == {
            datetime(2026, 8, 12, 15, 26, tzinfo=timezone.utc),
        }, rows
