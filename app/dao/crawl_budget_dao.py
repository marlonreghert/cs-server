"""Redis DAO for the scheduled Instagram crawl's monthly RESULT budget.

See plans/260809_scheduled-incremental-instagram-crawl.md §F: this is the
direct successor to docs/venue-retrieval-storage.md §3's retired "No cron"
guarantee — a hard ceiling on steady-state spend that does not depend on an
operator remembering to look, checked BEFORE every actor call (never after —
a gate that runs after the spend is not a gate) and decremented by each run's
ACTUAL billed result count (not the requested cap, which is only an upper
bound).

Deliberately Redis, mirroring `VenueBudgetDao`'s existing monthly-counter
pattern (`venue_add_counter_v1:YYYY-MM`) rather than a new RDS table: the
counter is ephemeral operational state, not durable event/venue data, and an
atomic `INCRBY` is exactly what a check-then-spend gate needs under
concurrent crawls of different handles. A distinct key namespace from
`VenueBudgetDao` — this counts SCRAPED RESULTS (the number the Apify bill is
computed from), not distinct venue ids touched against BestTime's unique-
venue cap; conflating the two would size one budget off the wrong unit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

CRAWL_RESULT_BUDGET_KEY_V1 = "crawl_result_budget_v1:{year_month}"


class CrawlBudgetDao:
    """Atomic monthly counter of Instagram results scraped by the scheduled
    crawl (posts + reels combined — one shared ceiling, matching §F's "a
    hard monthly result budget" singular, not a per-stream one)."""

    def __init__(self, redis_client) -> None:
        self.redis = redis_client

    @staticmethod
    def current_year_month_utc(now: Optional[datetime] = None) -> str:
        """UTC, matching every other timestamp this feature stores/compares
        (§B/§D: the cursor and the actor's own filter are UTC; only the cron
        trigger is local) — a budget month must not drift by timezone."""
        now = now or datetime.now(timezone.utc)
        return now.strftime("%Y-%m")

    def _key(self, year_month: str) -> str:
        return CRAWL_RESULT_BUDGET_KEY_V1.format(year_month=year_month)

    def get_month_count(self, year_month: str) -> int:
        """Results already spent this calendar month. 0 if unset."""
        try:
            raw = self.redis.get(self._key(year_month))
        except Exception as e:
            logger.error(f"[CrawlBudgetDao] get_month_count({year_month}) failed: {e}")
            raise
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.error(f"[CrawlBudgetDao] non-integer counter at {year_month}: {raw!r}")
            return 0

    def increment_month(self, year_month: str, n: int) -> int:
        """Atomically add `n` (a run's actual result count) and return the
        new total. Called ONLY after a successful call — see the crawl
        service, which checks the budget BEFORE the call and records the
        spend after it, never the reverse."""
        if n <= 0:
            return self.get_month_count(year_month)
        try:
            return int(self.redis.incrby(self._key(year_month), n))
        except Exception as e:
            logger.error(f"[CrawlBudgetDao] increment_month({year_month}, {n}) failed: {e}")
            raise
