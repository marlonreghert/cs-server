"""Redis DAO for the deep-review-corpus crawl's monthly REVIEW budget.

Mirrors app/dao/crawl_budget_dao.py's CrawlBudgetDao EXACTLY (see
plans/260813_deep-review-corpus.md's Implementation Approach: "New
ReviewCrawlBudgetDao mirroring CrawlBudgetDao exactly") — an atomic monthly
Redis counter, checked BEFORE every actor call (never after — a gate that
runs after the spend is not a gate) and incremented by each call's ACTUAL
billed review count, never the requested cap.

Distinct key namespace (`review_crawl_budget_v1:{year_month}`) from
CrawlBudgetDao's `crawl_result_budget_v1:{year_month}` so the two counters
can never be conflated: one counts Instagram results, this one counts
Google reviews, and they are billed at different per-unit rates.

This is only the LOCAL half of the deep review crawl's budget gate. The
SECOND, account-level gate (refusing a batch that would leave less than
`settings.reviews_deep_reserved_headroom_usd` of shared Apify headroom) lives
in DeepReviewCrawlService, not here — see that module's docstring for why a
failed account-usage lookup must be treated as ZERO headroom, never a pass.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

REVIEW_CRAWL_BUDGET_KEY_V1 = "review_crawl_budget_v1:{year_month}"


class ReviewCrawlBudgetDao:
    """Atomic monthly counter of reviews billed by the deep review crawl."""

    def __init__(self, redis_client) -> None:
        self.redis = redis_client

    @staticmethod
    def current_year_month_utc(now: Optional[datetime] = None) -> str:
        """UTC, matching CrawlBudgetDao's own reasoning: every timestamp this
        feature stores/compares is UTC, so a budget month must not drift by
        timezone."""
        now = now or datetime.now(timezone.utc)
        return now.strftime("%Y-%m")

    def _key(self, year_month: str) -> str:
        return REVIEW_CRAWL_BUDGET_KEY_V1.format(year_month=year_month)

    def get_month_count(self, year_month: str) -> int:
        """Reviews already billed this calendar month. 0 if unset.

        A non-integer counter value (corruption) logs and reads as 0 rather
        than raising — matching CrawlBudgetDao's own choice there. It does
        NOT read as "budget exhausted": a check-before-spend gate that fails
        open on read corruption would refuse every future run for a reason
        an operator cannot see from the API; failing to "0 spent so far" is
        the same trade this repo already made for the sibling counter.
        """
        try:
            raw = self.redis.get(self._key(year_month))
        except Exception as e:
            logger.error(f"[ReviewCrawlBudgetDao] get_month_count({year_month}) failed: {e}")
            raise
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.error(f"[ReviewCrawlBudgetDao] non-integer counter at {year_month}: {raw!r}")
            return 0

    def increment_month(self, year_month: str, n: int) -> int:
        """Atomically add `n` (a call's actual billed review count) and
        return the new total. Called ONLY after a successful actor call —
        DeepReviewCrawlService checks the budget BEFORE the call and records
        the spend after it, never the reverse."""
        if n <= 0:
            return self.get_month_count(year_month)
        try:
            return int(self.redis.incrby(self._key(year_month), n))
        except Exception as e:
            logger.error(f"[ReviewCrawlBudgetDao] increment_month({year_month}, {n}) failed: {e}")
            raise
