"""Redis DAO for tracking the monthly count of new venue additions.

Key format: `venue_add_counter_v1:YYYY-MM`. The counter is incremented
each time a venue_id that was not previously in our Redis geo index is
added (either via the add-by-address path or via discovery). It is never
explicitly reset — the calendar month rollover produces a fresh key,
which implicitly starts at zero. Past months' counters are preserved.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

VENUE_ADD_COUNTER_KEY_V1 = "venue_add_counter_v1:{year_month}"


class VenueBudgetDao:
    """Atomic monthly counter for new BestTime account-inventory additions."""

    def __init__(self, redis_client) -> None:
        self.redis = redis_client

    @staticmethod
    def current_year_month_utc(now: Optional[datetime] = None) -> str:
        """Return the current calendar month as YYYY-MM in UTC."""
        now = now or datetime.now(timezone.utc)
        return now.strftime("%Y-%m")

    def _key(self, year_month: str) -> str:
        return VENUE_ADD_COUNTER_KEY_V1.format(year_month=year_month)

    def get_month_count(self, year_month: str) -> int:
        """Read the current count for a given YYYY-MM. Returns 0 if unset."""
        try:
            raw = self.redis.get(self._key(year_month))
        except Exception as e:
            logger.error(
                f"[VenueBudgetDao] get_month_count({year_month}) failed: {e}"
            )
            raise
        if raw is None:
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.error(
                f"[VenueBudgetDao] non-integer counter at {year_month}: {raw!r}"
            )
            return 0

    def increment_month(self, year_month: str, n: int = 1) -> int:
        """Atomically increment and return the new value.

        Uses Redis INCRBY so concurrent callers (manual add + discovery)
        cannot race on a check-then-set.
        """
        if n <= 0:
            return self.get_month_count(year_month)
        try:
            return int(self.redis.incrby(self._key(year_month), n))
        except Exception as e:
            logger.error(
                f"[VenueBudgetDao] increment_month({year_month}, {n}) failed: {e}"
            )
            raise

    def decrement_month(self, year_month: str, n: int = 1) -> int:
        """Decrement the month counter, clamping at zero.

        Used to release a reservation when BestTime rejects the add. The
        clamp guards against drift if a reservation is double-released.
        """
        if n <= 0:
            return self.get_month_count(year_month)
        try:
            new_value = int(self.redis.decrby(self._key(year_month), n))
        except Exception as e:
            logger.error(
                f"[VenueBudgetDao] decrement_month({year_month}, {n}) failed: {e}"
            )
            raise
        if new_value < 0:
            # Clamp by restoring zero. There's a small window where another
            # writer could observe a negative value, but our other readers
            # use get_month_count which would just see whatever int Redis
            # has — the clamp matters mostly for INCRBY/DECRBY consistency.
            self.redis.set(self._key(year_month), 0)
            logger.warning(
                f"[VenueBudgetDao] clamped negative counter at {year_month} "
                f"(was about to be {new_value}) back to 0"
            )
            return 0
        return new_value
