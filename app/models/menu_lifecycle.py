"""Menu-item expiry: how long a dish stays "current" after it was last seen.

See plans/260811_menu-item-lifecycle.md §C. A dish has no end signal — no
post ever says the kitchen stopped serving the risotto — so staleness is
inferred purely from silence: a dish nobody has mentioned in
`menu_expiry_days` is no longer presented as current.

## Read-time, not a nightly job

Expiry is DERIVED at read time from `last_seen_at` (already a real, per-
source-aggregated timestamp — see `app.services.event_merge`'s module
docstring and `app.dao.rds_venue_store.RdsVenueStore._EVENT_SELECT`'s `agg`
LATERAL, which the fake mirrors in `tests.rds_fake.InMemoryRdsVenueStore.
_seen_bounds` — never a JSONB blob, so this is NOT the bug
`260811_expose-time-known.md` was written to fix), never written back to a
row on a schedule. A dish that goes quiet for a year and is then re-posted
becomes current again the moment that new source lands — a nightly
un-expire job would be a SECOND mechanism that can disagree with this one
about whether a given row is currently expired.

## Admin config, not a constant

`menu_expiry_days` is admin config, not a code constant — venue types
(`app.models.venue_category`), busyness labels, and the post-category
vocabulary (`app.models.post_category`, whose Redis-mirror-with-fallback
shape this module mirrors exactly) are all runtime-configurable in this
project for the same reason: 365 is a starting guess, and changing it must
not need a deploy.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY = "admin_config:menu_expiry_days"

DEFAULT_MENU_EXPIRY_DAYS = 365


def validate_menu_expiry_days_config(value) -> int:
    """Validate an admin write to `admin_config:menu_expiry_days` before
    persistence (AdminConfigService.set dispatches here). Body shape: a
    positive integer number of days. Raises TypeError/ValueError on a
    malformed shape — a bad override must never disable expiry entirely
    (0 or negative) or silently coerce a float/string."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("menu expiry window must be an integer number of days")
    if value <= 0:
        raise ValueError("menu expiry window must be a positive number of days")
    return value


def load_menu_expiry_days(redis_like) -> tuple[int, Optional[str]]:
    """Read the admin override, falling back to `DEFAULT_MENU_EXPIRY_DAYS`
    on any problem. Returns `(expiry_days, fallback_reason)`;
    `fallback_reason` is None unless the caller should count a fallback
    metric — a missing key is NOT a fallback, it is the expected
    pre-first-write state. Mirrors `app.models.post_category.
    load_post_category_vocabulary` exactly, including the None-redis_like
    escape hatch a caller with no wired Redis client (e.g. a bare test
    container) needs."""
    if redis_like is None:
        return DEFAULT_MENU_EXPIRY_DAYS, None
    try:
        raw = redis_like.get(ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[menu_lifecycle] config read failed, using default: {e}")
        return DEFAULT_MENU_EXPIRY_DAYS, "unreadable"
    if raw is None:
        return DEFAULT_MENU_EXPIRY_DAYS, None
    try:
        import json

        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.warning(f"[menu_lifecycle] config invalid JSON, using default: {e}")
        return DEFAULT_MENU_EXPIRY_DAYS, "invalid_json"
    try:
        return validate_menu_expiry_days_config(data), None
    except (TypeError, ValueError) as e:
        logger.warning(f"[menu_lifecycle] config invalid shape, using default: {e}")
        return DEFAULT_MENU_EXPIRY_DAYS, "invalid_shape"


def is_menu_item_current(
    last_seen_at: Optional[datetime], *, expiry_days: int, now: datetime,
) -> bool:
    """Whether a menu item's `last_seen_at` is still inside the expiry
    window, evaluated at READ TIME (see module docstring). Inclusive at the
    boundary: seen EXACTLY `expiry_days` ago is still current — only
    strictly MORE than `expiry_days` ago expires it, matching the plan's own
    "one day inside, exactly on, one day outside" boundary test.

    `last_seen_at is None` (a menu item with no source at all — should not
    occur in practice, since `insert_event` always attaches a first source,
    but never assumed) is treated as NOT current: there is no evidence this
    dish was ever seen, so it cannot be vouched for as current either."""
    if last_seen_at is None:
        return False
    return (now - last_seen_at) <= timedelta(days=expiry_days)


__all__ = [
    "ADMIN_CONFIG_MENU_EXPIRY_DAYS_KEY", "DEFAULT_MENU_EXPIRY_DAYS",
    "validate_menu_expiry_days_config", "load_menu_expiry_days", "is_menu_item_current",
]
