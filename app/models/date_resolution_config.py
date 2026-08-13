"""How many days "in the past" a date without a stated year still counts as
this year, rather than being rolled a full year forward.

See plans/260812_event-attribution-and-dates.md §D. `app.services.
event_date_resolver._roll_forward` is what actually applies this — it stays
pure (no I/O, no Redis read) and accepts the value as a plain keyword
argument; this module is the ONE place that reads the admin override,
mirroring `app.models.menu_lifecycle.load_menu_expiry_days` exactly (the
same Redis-mirror-with-fallback shape every runtime-configurable first-guess
in this project already uses — venue types, busyness labels, the post-
category vocabulary, menu_expiry_days).

## Why admin config, not a code constant

60 days is a first guess, not a measured constant — the same reasoning
`menu_expiry_days` documents for itself. An operator watching the queue
after deploy needs to be able to widen or narrow the window without a
redeploy if the real distribution of "how late do our sources post" turns
out to need it.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

ADMIN_CONFIG_DATE_YEAR_ROLL_GRACE_DAYS_KEY = "admin_config:date_year_roll_grace_days"

# Mirrors app.services.event_date_resolver.DEFAULT_YEAR_ROLL_GRACE_DAYS —
# kept as a SEPARATE literal (not imported) so this config module never
# needs to import the pure resolver module just for one integer; a unit
# test pins the two values equal.
DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS = 60


def validate_date_year_roll_grace_days_config(value) -> int:
    """Validate an admin write to `admin_config:date_year_roll_grace_days`
    before persistence (`AdminConfigService.set` dispatches here). Body
    shape: a non-negative integer number of days. Raises TypeError/
    ValueError on a malformed shape. Zero is valid (and meaningful — it
    disables the grace window entirely, reverting to the pre-§D
    always-roll behaviour) — unlike `menu_expiry_days`, a zero-day window is
    not a degenerate no-op here."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("date year-roll grace window must be an integer number of days")
    if value < 0:
        raise ValueError("date year-roll grace window must not be negative")
    return value


def load_date_year_roll_grace_days(redis_like) -> tuple[int, Optional[str]]:
    """Read the admin override, falling back to
    `DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS` on any problem. Returns
    `(grace_days, fallback_reason)`; `fallback_reason` is None unless the
    caller should count a fallback metric — a missing key is NOT a fallback,
    it is the expected pre-first-write state. Mirrors `app.models.
    menu_lifecycle.load_menu_expiry_days` exactly, including the None-
    redis_like escape hatch a caller with no wired Redis client needs."""
    if redis_like is None:
        return DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS, None
    try:
        raw = redis_like.get(ADMIN_CONFIG_DATE_YEAR_ROLL_GRACE_DAYS_KEY)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[date_resolution_config] config read failed, using default: {e}")
        return DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS, "unreadable"
    if raw is None:
        return DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS, None
    try:
        import json

        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning(f"[date_resolution_config] config invalid JSON, using default: {e}")
        return DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS, "invalid_json"
    try:
        return validate_date_year_roll_grace_days_config(data), None
    except (TypeError, ValueError) as e:
        logger.warning(f"[date_resolution_config] config invalid shape, using default: {e}")
        return DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS, "invalid_shape"


__all__ = [
    "ADMIN_CONFIG_DATE_YEAR_ROLL_GRACE_DAYS_KEY", "DEFAULT_DATE_YEAR_ROLL_GRACE_DAYS",
    "validate_date_year_roll_grace_days_config", "load_date_year_roll_grace_days",
]
