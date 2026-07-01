"""Serve-time live-busyness freshness gate.

A cached live forecast can outlive its usefulness: a BestTime fetch error or a
stalled refresh job leaves the last value in RDS/Redis, and the projector
re-asserts it with no age gate. This module derives freshness from the live
payload's own ``venue_current_gmttime`` so the serve handler can suppress a stale
live value (letting vibes_bot fall back to the forecast estimate) instead of
presenting it as current.

Design decisions (see plans/260701_live-busyness-freshness-gate.md):
- ``fresh`` iff ``now_utc - gmttime < max_age`` (boundary == max_age is stale),
  matching the vibes_bot admin dashboard's classifier.
- A missing/unparseable gmttime is treated as **stale** (fail toward forecast),
  so a payload/format drift degrades to the estimate rather than serving an
  un-datable "live" number. The caller makes this observable via a metric.
- The window default lives in settings; it is admin-overridable at runtime via
  ``admin_config:live_freshness_max_age_minutes`` (bounds-checked, never raises).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Admin override key (without the ``admin_config:`` prefix that AdminConfigService
# adds). Mirrors the live_refresh_minutes admin-tunable pattern.
ADMIN_LIVE_FRESHNESS_KEY = "live_freshness_max_age_minutes"

# Bounds for the admin override, in minutes. 1 minute floor; 30-day ceiling
# comfortably covers the 24h default without allowing a nonsensical value.
MIN_FRESHNESS_MINUTES = 1
MAX_FRESHNESS_MINUTES = 43200

# BestTime's ``venue_current_gmttime`` display formats. ISO 8601 is tried first;
# these are the documented display fallbacks (same set the vibes_bot admin
# dashboard tolerates), so a format drift undercounts fresh rather than raising.
_GMTTIME_FALLBACK_FORMATS = (
    "%A %Y-%m-%d %I:%M%p",     # "Friday 2026-06-05 03:07AM"
    "%A %Y-%m-%d %H:%M:%S",    # "Friday 2026-06-05 03:07:00"
    "%A %Y-%m-%d %H:%M",       # "Friday 2026-06-05 03:07"
)

# Freshness verdicts.
FRESH = "fresh"
STALE = "stale"
UNPARSEABLE = "unparseable"


def utc_now() -> datetime:
    """Current UTC time. Wrapped so callers don't import datetime directly and so
    tests can patch a single symbol if they ever need to."""
    return datetime.now(timezone.utc)


def parse_gmttime(raw) -> Optional[datetime]:
    """Parse BestTime's ``venue_current_gmttime`` into a UTC-aware datetime.

    Accepts ISO 8601 (with optional trailing ``Z``) and the BestTime display
    formats. Returns ``None`` for any missing/garbled value so the caller can
    treat the forecast as un-datable rather than raising.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _GMTTIME_FALLBACK_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def classify_live_freshness(live_forecast, now_utc: datetime, max_age: timedelta) -> str:
    """Classify a present live forecast as FRESH / STALE / UNPARSEABLE.

    Only call this when the forecast exists and is available; the caller owns the
    ``venue_live_busyness_available`` check and the no-live case.
    """
    venue_info = getattr(live_forecast, "venue_info", None)
    generated = parse_gmttime(getattr(venue_info, "venue_current_gmttime", None))
    if generated is None:
        return UNPARSEABLE
    return FRESH if (now_utc - generated) < max_age else STALE


def resolve_max_age_minutes(admin_config_service=None) -> int:
    """Effective freshness window in minutes: admin override if present and
    in-bounds, else the settings default. Never raises."""
    default = settings.live_freshness_max_age_minutes
    if admin_config_service is None:
        return default
    try:
        raw = admin_config_service.get(ADMIN_LIVE_FRESHNESS_KEY)
    except Exception as e:  # pragma: no cover - defensive; admin read is best-effort
        logger.warning(f"[live_freshness] admin override read failed: {e}; using default")
        return default
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"[live_freshness] {ADMIN_LIVE_FRESHNESS_KEY}={raw!r} is not an integer; "
            f"using default {default}"
        )
        return default
    if not (MIN_FRESHNESS_MINUTES <= value <= MAX_FRESHNESS_MINUTES):
        logger.warning(
            f"[live_freshness] {ADMIN_LIVE_FRESHNESS_KEY}={value} out of bounds "
            f"[{MIN_FRESHNESS_MINUTES}, {MAX_FRESHNESS_MINUTES}]; using default {default}"
        )
        return default
    return value
