"""Unit tests for ReviewCrawlBudgetDao — mirrors CrawlBudgetDao's own test
shape (tests/test_venue_budget.py's VenueBudgetDao tests use the same
fakeredis + fixture pattern). See plans/260813_deep-review-corpus.md.
"""
from datetime import datetime, timezone

import fakeredis
import pytest

from app.dao.review_crawl_budget_dao import (
    REVIEW_CRAWL_BUDGET_KEY_V1,
    ReviewCrawlBudgetDao,
)


@pytest.fixture
def fake():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def dao(fake):
    return ReviewCrawlBudgetDao(fake)


class TestReviewCrawlBudgetDao:
    def test_get_month_count_returns_zero_when_unset(self, dao):
        assert dao.get_month_count("2026-08") == 0

    def test_atomic_increment_then_get(self, dao):
        assert dao.increment_month("2026-08", 250) == 250
        assert dao.get_month_count("2026-08") == 250
        assert dao.increment_month("2026-08", 50) == 300

    def test_key_namespace_is_distinct_from_crawl_result_budget(self, fake, dao):
        """A distinct key family from CrawlBudgetDao's `crawl_result_budget_v1`
        — the plan is explicit this must never be conflated (different unit,
        different per-unit cost)."""
        dao.increment_month("2026-08", 100)
        assert REVIEW_CRAWL_BUDGET_KEY_V1.format(year_month="2026-08") == "review_crawl_budget_v1:2026-08"
        assert fake.get("crawl_result_budget_v1:2026-08") is None
        assert fake.get("review_crawl_budget_v1:2026-08") == "100"

    def test_month_rollover_in_utc_produces_separate_counters(self, dao):
        dao.increment_month("2026-08", 500)
        dao.increment_month("2026-09", 10)
        assert dao.get_month_count("2026-08") == 500
        assert dao.get_month_count("2026-09") == 10

    def test_current_year_month_utc_matches_utc_now(self, dao):
        now = datetime(2026, 8, 31, 23, 30, tzinfo=timezone.utc)
        assert ReviewCrawlBudgetDao.current_year_month_utc(now) == "2026-08"

    def test_negative_or_zero_increment_is_a_noop(self, dao):
        dao.increment_month("2026-08", 0)
        dao.increment_month("2026-08", -10)
        assert dao.get_month_count("2026-08") == 0

    def test_check_before_spend_ordering(self, dao):
        """The DAO exposes get (check) and increment (spend) as two separate
        calls with no combined check-and-spend primitive — the caller
        (DeepReviewCrawlService) is responsible for calling get BEFORE the
        actor call and increment only AFTER a successful one. This test pins
        that a get_month_count call preceding an increment observes the
        PRE-spend total, never a total that already reflects a spend the
        caller has not recorded yet."""
        year_month = "2026-08"
        before = dao.get_month_count(year_month)
        assert before == 0
        dao.increment_month(year_month, 42)
        after_first_call = dao.get_month_count(year_month)
        assert after_first_call == 42
        # A second check-before-spend must see the FIRST call's spend, so a
        # caller looping over multiple calls never double-spends the same
        # budget window.
        dao.increment_month(year_month, 8)
        assert dao.get_month_count(year_month) == 50

    def test_a_failed_read_never_reads_as_budget_available(self, dao, monkeypatch):
        """A Redis-level failure on get_month_count must propagate (raise),
        never silently degrade to 0 spent / "budget available" — a caller
        that swallowed this exception would spend against an unknown budget
        state, exactly the gate CLAUDE.md/the plan says must never run after
        the spend."""
        def _boom(*a, **kw):
            raise ConnectionError("redis unavailable")

        monkeypatch.setattr(dao.redis, "get", _boom)
        with pytest.raises(ConnectionError):
            dao.get_month_count("2026-08")

    def test_non_integer_counter_value_logs_and_reads_as_zero(self, fake, dao):
        """Mirrors CrawlBudgetDao's own choice: VALUE corruption (not a
        connection failure) degrades to 0 rather than raising — a corrupted
        counter is data corruption, not "unknown state", and this repo's
        precedent treats it the same way for the sibling DAO."""
        fake.set(REVIEW_CRAWL_BUDGET_KEY_V1.format(year_month="2026-08"), "not-a-number")
        assert dao.get_month_count("2026-08") == 0
