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

# Monthly ledger of distinct venue_ids touched against BestTime's unique-venue
# cap (Redis set). The key naturally rolls over each calendar month; the TTL
# (~40 days, past the rollover) lets the previous month's set self-evict.
BESTTIME_TOUCH_LEDGER_KEY_V1 = "besttime_touched_v1:{year_month}"
BESTTIME_TOUCH_LEDGER_TTL_SECONDS = 60 * 60 * 24 * 40


class VenueBudgetDao:
    """Atomic monthly counter for new BestTime account-inventory additions, plus
    the monthly distinct-venue touch ledger backing the unique-venue cap."""

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

    # ----- monthly distinct-venue touch ledger ----------------------------

    def _touch_key(self, year_month: str) -> str:
        return BESTTIME_TOUCH_LEDGER_KEY_V1.format(year_month=year_month)

    def is_touched(self, year_month: str, venue_id: str) -> bool:
        """True if venue_id was already counted against this month's cap."""
        try:
            return bool(self.redis.sismember(self._touch_key(year_month), venue_id))
        except Exception as e:
            logger.error(
                f"[VenueBudgetDao] is_touched({year_month}, {venue_id}) failed: {e}"
            )
            raise

    def touch_count(self, year_month: str) -> int:
        """Distinct venues touched this calendar month (set cardinality)."""
        try:
            return int(self.redis.scard(self._touch_key(year_month)))
        except Exception as e:
            logger.error(f"[VenueBudgetDao] touch_count({year_month}) failed: {e}")
            raise

    def add_touch(self, year_month: str, venue_id: str) -> tuple[bool, int]:
        """Add venue_id to the month's touch set. Returns (was_new, cardinality).
        Refreshes the TTL only when the set actually grew so a quiet month's set
        still self-evicts after the rollover."""
        key = self._touch_key(year_month)
        try:
            added = int(self.redis.sadd(key, venue_id))
            if added:
                try:
                    self.redis.expire(key, BESTTIME_TOUCH_LEDGER_TTL_SECONDS)
                except Exception as e:  # TTL is best-effort; never block the gate
                    logger.warning(
                        f"[VenueBudgetDao] expire on {key} failed: {e}"
                    )
            return bool(added), int(self.redis.scard(key))
        except Exception as e:
            logger.error(
                f"[VenueBudgetDao] add_touch({year_month}, {venue_id}) failed: {e}"
            )
            raise

    def remove_touch(self, year_month: str, venue_id: str) -> None:
        """Roll back a just-added touch that overshot the cap."""
        try:
            self.redis.srem(self._touch_key(year_month), venue_id)
        except Exception as e:
            logger.error(
                f"[VenueBudgetDao] remove_touch({year_month}, {venue_id}) failed: {e}"
            )
            raise
