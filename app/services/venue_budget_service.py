"""Service that enforces the monthly new-venue budget for cs-server.

Quota and reserve come from the admin-config key
`admin_config:venue_monthly_budget`. The key is read on every call (no
in-memory caching) so admin updates take effect immediately without a
restart. Defaults: quota=500, reserve=10.

Concurrency model:
- Manual add uses INCR-then-validate as the reservation primitive. If
  the post-INCR value exceeds the quota, the service DECRs back and
  returns "exhausted". Same pattern as a token bucket.
- Discovery polls `discovery_effective_cap_remaining()` before each
  refresh batch. The reserve guarantees discovery stops short so manual
  adds can still use the last `reserve` slots.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from app.dao.venue_budget_dao import VenueBudgetDao

logger = logging.getLogger(__name__)

ADMIN_CONFIG_BUDGET_KEY = "admin_config:venue_monthly_budget"

DEFAULT_MONTHLY_QUOTA = 500
DEFAULT_MANUAL_RESERVE = 10


@dataclass(frozen=True)
class QuotaSettings:
    monthly_quota: int
    manual_reserve: int


@dataclass(frozen=True)
class BudgetSnapshot:
    """Read-only view of the current budget state for the admin UI."""

    quota: int
    manual_reserve: int
    month_counter: int
    year_month: str
    discovery_effective_cap_remaining: int
    manual_add_available: int


class VenueBudgetService:
    def __init__(
        self,
        redis_client,
        budget_dao: VenueBudgetDao,
        year_month_provider=None,
    ) -> None:
        self.redis = redis_client
        self.dao = budget_dao
        # Allows tests / scenarios to pin "today's month" without monkey
        # patching datetime.now globally.
        self._year_month_provider = year_month_provider or VenueBudgetDao.current_year_month_utc

    # ----- quota loading --------------------------------------------------

    def get_quota_settings(self) -> QuotaSettings:
        """Read the live quota/reserve from Redis admin config.

        On any failure, fall back to defaults so the service stays
        functional even if Redis blips.
        """
        try:
            raw = self.redis.get(ADMIN_CONFIG_BUDGET_KEY)
        except Exception as e:
            logger.error(
                f"[VenueBudgetService] failed to read {ADMIN_CONFIG_BUDGET_KEY}: {e}"
            )
            return QuotaSettings(DEFAULT_MONTHLY_QUOTA, DEFAULT_MANUAL_RESERVE)
        if raw is None:
            return QuotaSettings(DEFAULT_MONTHLY_QUOTA, DEFAULT_MANUAL_RESERVE)
        try:
            cfg = json.loads(raw)
        except Exception as e:
            logger.error(
                f"[VenueBudgetService] invalid JSON in {ADMIN_CONFIG_BUDGET_KEY}: {e}"
            )
            return QuotaSettings(DEFAULT_MONTHLY_QUOTA, DEFAULT_MANUAL_RESERVE)
        quota = int(cfg.get("monthly_quota", DEFAULT_MONTHLY_QUOTA))
        reserve = int(cfg.get("manual_reserve", DEFAULT_MANUAL_RESERVE))
        # Defensive clamps.
        if quota < 0:
            quota = 0
        if reserve < 0:
            reserve = 0
        if reserve > quota:
            reserve = quota
        return QuotaSettings(quota, reserve)

    def get_refresh_budget(self) -> int:
        """The single source of truth for the bounded-refresh selection size:
        `X = monthly_quota − manual_reserve`. This is the count of unique venues
        refresh may touch, leaving the reserve for manual adds so the two
        disjoint slices stay within the monthly cap. Clamped at zero — a budget
        of 0 refreshes nothing (it never silently falls back to unbounded)."""
        settings = self.get_quota_settings()
        return max(0, settings.monthly_quota - settings.manual_reserve)

    def current_year_month(self) -> str:
        """The calendar month the ledger/counter key are scoped to."""
        return self._year_month_provider()

    # ----- monthly unique-venue ledger (hard ceiling) --------------------

    def unique_touched_count(self) -> int:
        return self.dao.touch_count(self._year_month_provider())

    def mark_touched(self, venue_id: str) -> None:
        """Record a definite BestTime interaction with venue_id (e.g. a
        successful manual add) in this month's ledger. Unconditional — the add
        already reserved a slot; this keeps the unique-venue count accurate so a
        re-read of the new venue is free and the backstop counts it."""
        self.dao.add_touch(self._year_month_provider(), venue_id)

    def try_register_touch(self, venue_id: str) -> bool:
        """Hard-ceiling gate on a BestTime read for `venue_id`.

        Returns True when the read may proceed — either the venue was already
        counted this month (a re-read costs no new unique), or registering it
        keeps the month's distinct-venue count within `monthly_quota`. Returns
        False when admitting this new venue would exceed the cap; the caller
        must skip the read. Uses add-then-validate-then-rollback, mirroring the
        manual-add reservation primitive.
        """
        settings = self.get_quota_settings()
        year_month = self._year_month_provider()
        if self.dao.is_touched(year_month, venue_id):
            return True
        added, count = self.dao.add_touch(year_month, venue_id)
        if not added:
            # Raced with another writer that just added it — already counted.
            return True
        if count > settings.monthly_quota:
            self.dao.remove_touch(year_month, venue_id)
            return False
        return True

    # ----- snapshot for admin UI -----------------------------------------

    def get_snapshot(self) -> BudgetSnapshot:
        settings = self.get_quota_settings()
        year_month = self._year_month_provider()
        counter = self.dao.get_month_count(year_month)
        discovery_remaining = max(
            0, (settings.monthly_quota - settings.manual_reserve) - counter
        )
        manual_available = max(0, settings.monthly_quota - counter)
        return BudgetSnapshot(
            quota=settings.monthly_quota,
            manual_reserve=settings.manual_reserve,
            month_counter=counter,
            year_month=year_month,
            discovery_effective_cap_remaining=discovery_remaining,
            manual_add_available=manual_available,
        )

    # ----- discovery side -------------------------------------------------

    def discovery_effective_cap_remaining(self) -> int:
        snap = self.get_snapshot()
        return snap.discovery_effective_cap_remaining

    # ----- manual-add side ------------------------------------------------

    def can_manual_add(self) -> bool:
        snap = self.get_snapshot()
        return snap.manual_add_available > 0

    def reserve_manual_slot(self) -> tuple[bool, Optional[BudgetSnapshot]]:
        """Atomically reserve one manual-add slot.

        Returns (granted, post_snapshot). When granted is False, the
        snapshot reflects the state at the moment of denial so the
        caller can produce a clear 429 body.
        """
        settings = self.get_quota_settings()
        year_month = self._year_month_provider()
        new_value = self.dao.increment_month(year_month, 1)
        if new_value > settings.monthly_quota:
            # Roll back; we just overshot.
            self.dao.decrement_month(year_month, 1)
            counter = settings.monthly_quota  # report cap as the saturation point
            snap = BudgetSnapshot(
                quota=settings.monthly_quota,
                manual_reserve=settings.manual_reserve,
                month_counter=counter,
                year_month=year_month,
                discovery_effective_cap_remaining=0,
                manual_add_available=0,
            )
            return False, snap
        return True, BudgetSnapshot(
            quota=settings.monthly_quota,
            manual_reserve=settings.manual_reserve,
            month_counter=new_value,
            year_month=year_month,
            discovery_effective_cap_remaining=max(
                0, (settings.monthly_quota - settings.manual_reserve) - new_value
            ),
            manual_add_available=max(0, settings.monthly_quota - new_value),
        )

    def release_manual_slot(self) -> None:
        """Release a previously reserved slot. Idempotent at the floor."""
        year_month = self._year_month_provider()
        self.dao.decrement_month(year_month, 1)

    # ----- discovery counter recording -----------------------------------

    def record_new_venue_from_discovery(self) -> int:
        """Increment the counter for a new venue discovered via /venues/filter."""
        year_month = self._year_month_provider()
        return self.dao.increment_month(year_month, 1)
