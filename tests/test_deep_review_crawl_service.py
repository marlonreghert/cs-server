"""Unit tests for app/services/deep_review_crawl_service.py.

Covers the plan's own pytest plan (plans/260813_deep-review-corpus.md):
  - window boundary (inclusive/exclusive at exactly N days)
  - the incremental cursor (the `since` argument passed to the actor client)
  - merge/dedup with and without `review_id`
  - the truncation flag
  - candidate resolution and skip reporting
  - the id-list-is-authoritative rule

PLUS coverage the approved BDD (tests/bdd/enrichment/deep-review-corpus.feature)
does NOT exercise (see the execution report): the ACCOUNT-level Apify headroom
gate — the plan's Implementation Approach is explicit this is a required
second gate ("the local review counter is necessary but NOT sufficient"), but
no Gherkin scenario asserts it. These tests exist so that gate has real
coverage somewhere rather than none.

BDD covers the externally observable run/estimate behavior end to end; these
tests pin the lower-level functions and edge cases directly, per CLAUDE.md's
"Do not duplicate BDD assertions in pytest unless the unit test protects a
lower level edge case or failure mode."
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from app.dao.review_crawl_budget_dao import ReviewCrawlBudgetDao
from app.dao.venue_repository import VenueRepository
from app.db.geo_redis_client import GeoRedisClient
from app.models.venue import Venue
from app.models.vibe_attributes import VibeAttributes
from app.services.deep_review_crawl_service import DeepReviewCrawlService, _dedup_key
from app.models.venue_review import VenueReview, VenueReviewsDeep
from app.config import settings
from tests.rds_fake import InMemoryRdsVenueStore

_LAT, _LNG = -8.05, -34.88
NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _venue(vid):
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=vid,
        venue_address=f"{vid} addr", venue_lat=_LAT, venue_lng=_LNG, venue_type="BAR",
    )


class _FakeApify:
    def __init__(self):
        self._queues = {}
        self.calls = []

    def program(self, place_id, items_or_exc):
        self._queues.setdefault(place_id, []).append(items_or_exc)

    async def fetch_reviews(self, place_ids, max_reviews, language="pt-BR", since=None):
        place_id = place_ids[0]
        self.calls.append({"place_id": place_id, "max_reviews": max_reviews, "since": since})
        q = self._queues.get(place_id)
        if not q:
            return []
        item = q.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeAccountUsage:
    def __init__(self, headroom=1000.0, fail=False):
        self.headroom = headroom
        self.fail = fail
        self.calls = 0

    async def get_headroom_usd(self):
        self.calls += 1
        if self.fail:
            return None
        return self.headroom


def _raw(review_id, author, text, published: datetime, rating=5):
    return {
        "reviewId": review_id, "name": author, "text": text,
        "publishedAtDate": published.isoformat(), "stars": rating,
    }


@pytest.fixture
def repo():
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    geo = GeoRedisClient(fake_redis)
    store = InMemoryRdsVenueStore()
    return VenueRepository(geo, rds_store=store)


@pytest.fixture
def budget_dao():
    return ReviewCrawlBudgetDao(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def apify():
    return _FakeApify()


@pytest.fixture
def account_usage():
    return _FakeAccountUsage()


@pytest.fixture
def service(repo, apify, budget_dao, account_usage):
    return DeepReviewCrawlService(
        repo=repo, apify_client=apify, budget_dao=budget_dao,
        account_usage_client=account_usage, now_fn=lambda: NOW,
    )


def _seed(repo, vid, with_place_id=True):
    repo.upsert_venue(_venue(vid))
    if with_place_id:
        repo.set_vibe_attributes(VibeAttributes(venue_id=vid, google_place_id=f"place_{vid}"))


# ── window boundary ──────────────────────────────────────────────────────────
class TestWindowBoundary:
    def test_review_exactly_at_window_days_is_inside(self, service, repo, apify):
        _seed(repo, "v1")
        window = settings.reviews_deep_window_days
        # Exactly `window` days old — the boundary itself.
        apify.program("place_v1", [_raw("r1", "A", "t", NOW - timedelta(days=window))])
        _run(service.run(venue_ids=["v1"]))
        deep = repo.get_venue_reviews_deep("v1")
        assert len(deep.reviews) == 1

    def test_review_one_second_past_the_window_is_outside(self, service, repo, apify):
        _seed(repo, "v1")
        window = settings.reviews_deep_window_days
        apify.program(
            "place_v1", [_raw("r1", "A", "t", NOW - timedelta(days=window, seconds=1))]
        )
        _run(service.run(venue_ids=["v1"]))
        deep = repo.get_venue_reviews_deep("v1")
        assert len(deep.reviews) == 0

    def test_review_well_inside_the_window_is_kept(self, service, repo, apify):
        _seed(repo, "v1")
        apify.program("place_v1", [_raw("r1", "A", "t", NOW - timedelta(days=1))])
        _run(service.run(venue_ids=["v1"]))
        assert len(repo.get_venue_reviews_deep("v1").reviews) == 1


# ── incremental cursor ───────────────────────────────────────────────────────
class TestIncrementalCursor:
    def test_first_run_passes_no_since(self, service, repo, apify):
        _seed(repo, "v1")
        apify.program("place_v1", [])
        _run(service.run(venue_ids=["v1"]))
        assert apify.calls[0]["since"] is None

    def test_rerun_passes_the_stored_newest_publish_time_as_since(self, service, repo, apify):
        _seed(repo, "v1")
        existing = VenueReviewsDeep(
            venue_id="v1",
            reviews=[VenueReview(
                author_name="A", rating=5, text="t", relative_time="",
                publish_time=(NOW - timedelta(days=5)).isoformat(),
                review_id="v1-r0", source="apify_gmaps",
            )],
            window_days=180, fetched_at=(NOW - timedelta(days=1)).isoformat(),
            oldest_publish_time=(NOW - timedelta(days=5)).isoformat(),
            newest_publish_time=(NOW - timedelta(days=5)).isoformat(),
            truncated=False,
        )
        repo.set_venue_reviews_deep(existing)
        apify.program("place_v1", [])
        _run(service.run(venue_ids=["v1"]))
        assert apify.calls[0]["since"] == (NOW - timedelta(days=5)).isoformat()


# ── merge / dedup ────────────────────────────────────────────────────────────
class TestMergeDedup:
    def test_dedup_key_prefers_review_id(self):
        r1 = VenueReview(author_name="A", rating=5, text="t", relative_time="", review_id="x")
        r2 = VenueReview(author_name="B", rating=1, text="different", relative_time="", review_id="x")
        assert _dedup_key(r1) == _dedup_key(r2)

    def test_dedup_key_falls_back_without_review_id(self):
        common_prefix = "a" * 64
        r1 = VenueReview(
            author_name="A", rating=5, text=common_prefix + " tail one", relative_time="",
            publish_time="2026-01-01T00:00:00+00:00",
        )
        r2 = VenueReview(
            author_name="A", rating=5, text=common_prefix + " a completely different tail",
            relative_time="", publish_time="2026-01-01T00:00:00+00:00",
        )
        # Same first 64 chars -> same fallback key even with different tails.
        assert r1.text[:64] == r2.text[:64]
        assert _dedup_key(r1) == _dedup_key(r2)

    def test_duplicate_with_review_id_is_not_stored_twice(self, service, repo, apify):
        _seed(repo, "v1")
        apify.program("place_v1", [_raw("r1", "A", "t", NOW - timedelta(days=1))])
        _run(service.run(venue_ids=["v1"]))
        assert len(repo.get_venue_reviews_deep("v1").reviews) == 1

        apify.program("place_v1", [_raw("r1", "A", "t", NOW - timedelta(days=1))])
        result = _run(service.run(venue_ids=["v1"]))
        assert len(repo.get_venue_reviews_deep("v1").reviews) == 1
        assert result["reviews_billed_total"] == 1  # still billed; dedup is a storage decision

    def test_duplicate_without_review_id_via_fallback_key_is_not_stored_twice(self, service, repo, apify):
        _seed(repo, "v1")
        published = NOW - timedelta(days=1)
        item = {"name": "A", "text": "same text", "publishedAtDate": published.isoformat()}
        apify.program("place_v1", [item])
        _run(service.run(venue_ids=["v1"]))
        apify.program("place_v1", [dict(item)])
        _run(service.run(venue_ids=["v1"]))
        assert len(repo.get_venue_reviews_deep("v1").reviews) == 1

    def test_aged_out_existing_review_is_retained_across_a_rerun(self, service, repo, apify):
        _seed(repo, "v1")
        window = settings.reviews_deep_window_days
        aged = VenueReview(
            author_name="Old", rating=5, text="old", relative_time="",
            publish_time=(NOW - timedelta(days=window + 30)).isoformat(),
            review_id="v1-aged", source="apify_gmaps",
        )
        repo.set_venue_reviews_deep(VenueReviewsDeep(
            venue_id="v1", reviews=[aged], window_days=window,
            fetched_at=NOW.isoformat(), oldest_publish_time=aged.publish_time,
            newest_publish_time=aged.publish_time, truncated=False,
        ))
        apify.program("place_v1", [])
        _run(service.run(venue_ids=["v1"]))
        ids = [r.review_id for r in repo.get_venue_reviews_deep("v1").reviews]
        assert "v1-aged" in ids


# ── truncation ───────────────────────────────────────────────────────────────
class TestTruncation:
    def test_exceeding_the_cap_truncates_to_the_newest(self, service, repo, apify, monkeypatch):
        monkeypatch.setattr(settings, "reviews_deep_max_per_venue", 3)
        _seed(repo, "v1")
        items = [_raw(f"r{i}", "A", "t", NOW - timedelta(days=i)) for i in range(5)]
        apify.program("place_v1", items)
        result = _run(service.run(venue_ids=["v1"]))
        deep = repo.get_venue_reviews_deep("v1")
        assert len(deep.reviews) == 3
        assert deep.truncated is True
        assert "v1" in result["truncated_venues"]
        # Newest 3 = days 0, 1, 2 (smallest offsets).
        kept_ids = {r.review_id for r in deep.reviews}
        assert kept_ids == {"r0", "r1", "r2"}

    def test_under_the_cap_is_not_truncated(self, service, repo, apify, monkeypatch):
        monkeypatch.setattr(settings, "reviews_deep_max_per_venue", 300)
        _seed(repo, "v1")
        apify.program("place_v1", [_raw("r0", "A", "t", NOW - timedelta(days=1))])
        _run(service.run(venue_ids=["v1"]))
        assert repo.get_venue_reviews_deep("v1").truncated is False


# ── candidate resolution / id-list-is-authoritative ────────────────────────
class TestCandidateResolution:
    def test_venue_without_place_id_is_skipped_and_reported(self, service, repo):
        _seed(repo, "v1", with_place_id=False)
        selection = service.resolve_candidates(venue_ids=["v1"])
        assert selection.candidates == []
        assert selection.skipped_no_place_id == ["v1"]

    def test_filter_alone_defines_candidates_when_no_id_list_given(self, service, repo):
        _seed(repo, "v1")
        _seed(repo, "v2")
        selection = service.resolve_candidates(filter_spec={"venue_type": "BAR"})
        assert set(selection.candidates) == {"v1", "v2"}

    def test_id_list_is_authoritative_filter_only_narrows(self, service, repo):
        _seed(repo, "v1")
        _seed(repo, "v2")
        _seed(repo, "v3")
        # Filter matches ALL three, but only v1/v2 were explicitly selected —
        # the widened set (v3) must never appear.
        selection = service.resolve_candidates(
            venue_ids=["v1", "v2"], filter_spec={"venue_type": "BAR"}
        )
        assert set(selection.candidates) == {"v1", "v2"}

    def test_filter_can_narrow_the_explicit_id_list(self, service, repo):
        repo.upsert_venue(Venue(
            forecast=True, processed=True, venue_id="v1", venue_name="v1",
            venue_address="a", venue_lat=_LAT, venue_lng=_LNG, venue_type="BAR",
        ))
        repo.set_vibe_attributes(VibeAttributes(venue_id="v1", google_place_id="place_v1"))
        repo.upsert_venue(Venue(
            forecast=True, processed=True, venue_id="v2", venue_name="v2",
            venue_address="a", venue_lat=_LAT, venue_lng=_LNG, venue_type="CAFE",
        ))
        repo.set_vibe_attributes(VibeAttributes(venue_id="v2", google_place_id="place_v2"))
        selection = service.resolve_candidates(
            venue_ids=["v1", "v2"], filter_spec={"venue_type": "BAR"}
        )
        assert selection.candidates == ["v1"]

    def test_duplicate_ids_are_deduplicated(self, service, repo):
        _seed(repo, "v1")
        selection = service.resolve_candidates(venue_ids=["v1", "v1", "v1"])
        assert selection.candidates == ["v1"]


# ── estimate ─────────────────────────────────────────────────────────────────
class TestEstimate:
    def test_estimate_is_an_upper_bound_and_spends_nothing(self, service, repo, apify):
        _seed(repo, "v1")
        _seed(repo, "v2")
        result = service.estimate(venue_ids=["v1", "v2"])
        assert result["selected_venue_count"] == 2
        assert result["worst_case_review_count"] == 2 * settings.reviews_deep_max_per_venue
        assert result["is_upper_bound"] is True
        assert apify.calls == []


# ── local budget: check before every call ───────────────────────────────────
class TestLocalBudgetGate:
    def test_exhausted_budget_refuses_before_any_call(self, service, repo, apify, budget_dao):
        _seed(repo, "v1")
        year_month = ReviewCrawlBudgetDao.current_year_month_utc(NOW)
        budget_dao.increment_month(year_month, settings.reviews_deep_monthly_review_budget)
        result = _run(service.run(venue_ids=["v1"]))
        assert apify.calls == []
        assert result["budget_stopped"] is True
        assert result["outcome"] == "budget_stopped"

    def test_budget_checked_before_each_call_not_after(self, service, repo, apify, budget_dao, monkeypatch):
        monkeypatch.setattr(settings, "reviews_deep_monthly_review_budget", 5)
        _seed(repo, "v1")
        _seed(repo, "v2")
        apify.program("place_v1", [_raw(f"r{i}", "A", "t", NOW - timedelta(days=i)) for i in range(5)])
        apify.program("place_v2", [_raw("r0", "A", "t", NOW - timedelta(days=1))])
        result = _run(service.run(venue_ids=["v1", "v2"]))
        assert repo.get_venue_reviews_deep("v1") is not None
        assert repo.get_venue_reviews_deep("v2") is None
        assert result["not_reached_venues"] == ["v2"]
        assert result["outcome"] == "partial"
        # v2's actor call must never have happened.
        assert all(c["place_id"] != "place_v2" for c in apify.calls)


# ── account-level headroom gate (NOT covered by the approved BDD) ──────────
class TestAccountHeadroomGate:
    def test_sufficient_headroom_allows_the_batch(self, repo, apify, budget_dao):
        account_usage = _FakeAccountUsage(headroom=1000.0)
        svc = DeepReviewCrawlService(
            repo=repo, apify_client=apify, budget_dao=budget_dao,
            account_usage_client=account_usage, now_fn=lambda: NOW,
        )
        _seed(repo, "v1")
        apify.program("place_v1", [_raw("r0", "A", "t", NOW - timedelta(days=1))])
        result = _run(svc.run(venue_ids=["v1"]))
        assert result["stored_venues"] == ["v1"]
        assert account_usage.calls == 1

    def test_insufficient_headroom_refuses_the_whole_batch(self, repo, apify, budget_dao, monkeypatch):
        monkeypatch.setattr(settings, "reviews_deep_reserved_headroom_usd", 8.0)
        monkeypatch.setattr(settings, "reviews_deep_max_per_venue", 300)
        monkeypatch.setattr(settings, "apify_review_cost_usd", 0.0003)
        # worst-case batch cost for 1 venue = 300 * 0.0003 = 0.09; headroom
        # must be at least 8.09 to pass. 8.0 exactly is insufficient.
        account_usage = _FakeAccountUsage(headroom=8.0)
        svc = DeepReviewCrawlService(
            repo=repo, apify_client=apify, budget_dao=budget_dao,
            account_usage_client=account_usage, now_fn=lambda: NOW,
        )
        _seed(repo, "v1")
        apify.program("place_v1", [_raw("r0", "A", "t", NOW - timedelta(days=1))])
        result = _run(svc.run(venue_ids=["v1"]))
        assert apify.calls == []
        assert result["not_reached_venues"] == ["v1"]
        assert result["outcome"] == "partial"
        assert result["budget_stopped"] is True

    def test_a_failed_headroom_lookup_is_treated_as_zero_and_refuses(self, repo, apify, budget_dao):
        """The plan's explicit rule: 'A lookup failure is a refusal, not a
        pass. An unknown headroom is treated as zero headroom.'"""
        account_usage = _FakeAccountUsage(fail=True)
        svc = DeepReviewCrawlService(
            repo=repo, apify_client=apify, budget_dao=budget_dao,
            account_usage_client=account_usage, now_fn=lambda: NOW,
        )
        _seed(repo, "v1")
        apify.program("place_v1", [_raw("r0", "A", "t", NOW - timedelta(days=1))])
        result = _run(svc.run(venue_ids=["v1"]))
        assert apify.calls == [], "a failed headroom lookup must never permit an actor call"
        assert result["not_reached_venues"] == ["v1"]

    def test_a_raising_account_usage_client_also_refuses(self, repo, apify, budget_dao):
        """A client that raises (network error, bad token) must degrade the
        same as one that returns None — never bubble up and crash the run,
        and never be mistaken for available headroom."""
        class _RaisingUsage:
            async def get_headroom_usd(self):
                raise RuntimeError("network unreachable")

        svc = DeepReviewCrawlService(
            repo=repo, apify_client=apify, budget_dao=budget_dao,
            account_usage_client=_RaisingUsage(), now_fn=lambda: NOW,
        )
        _seed(repo, "v1")
        apify.program("place_v1", [_raw("r0", "A", "t", NOW - timedelta(days=1))])
        result = _run(svc.run(venue_ids=["v1"]))
        assert apify.calls == []
        assert result["not_reached_venues"] == ["v1"]


# ── per-venue failure isolation ──────────────────────────────────────────────
class TestPerVenueIsolation:
    def test_one_venues_actor_error_does_not_abort_the_run(self, service, repo, apify):
        _seed(repo, "v1")
        _seed(repo, "v2")
        _seed(repo, "v3")
        apify.program("place_v1", [_raw("r0", "A", "t", NOW - timedelta(days=1))])
        apify.program("place_v2", RuntimeError("actor crashed"))
        apify.program("place_v3", [_raw("r0", "A", "t", NOW - timedelta(days=1))])
        result = _run(service.run(venue_ids=["v1", "v2", "v3"]))
        assert set(result["stored_venues"]) == {"v1", "v3"}
        assert result["failed_venues"] == ["v2"]
