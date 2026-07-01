"""Serve-time live-busyness freshness gate.

A cached live forecast can outlive its usefulness: a BestTime fetch error or a
stalled refresh job leaves the last value in RDS/Redis, and the projector
re-asserts it with no age gate. This module derives freshness from the live
payload's own ``venue_current_gmttime`` so the serve handler can suppress a stale
live value (letting vibes_bot fall back to the forecast estimate) instead of
presenting it as current.

Design decisions (see plans/260701_live-busyness-freshness-gate.md and
plans/260701_dynamic-freshness-window.md):
- ``fresh`` iff ``now_utc - gmttime < max_age`` (boundary == max_age is stale).
- A missing/unparseable gmttime is treated as **stale** (fail toward forecast),
  so a payload/format drift degrades to the estimate rather than serving an
  un-datable "live" number. The caller makes this observable via a metric.
- The window is DERIVED from the live refresh cadence, not set independently:
  ``max_age = live_freshness_refresh_factor × effective_refresh_minutes``,
  floored at ``live_freshness_min_minutes``. Keeping the two coupled means a
  slower refresh automatically widens the window (a venue is re-touched well
  within it), so the pair can never desync into mass-suppression.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# The live refresh cadence key (without the ``admin_config:`` prefix that
# AdminConfigService prepends). Value shape: {"minutes": int} (what the vibesadmin
# panel writes) or a bare int. Bounds MUST match refresh_interval_watch so the
# freshness window tracks exactly the interval the refresher actually runs on.
_REFRESH_MINUTES_KEY = "live_refresh_minutes"
_REFRESH_MIN_MINUTES = 1
_REFRESH_MAX_MINUTES = 120

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


def classify_live_freshness(live_forecast, now_utc: datetime, max_age: timedelta):
    """Classify a present live forecast, returning ``(verdict, age_minutes)``.

    ``verdict`` is FRESH / STALE / UNPARSEABLE; ``age_minutes`` is the payload's
    age in minutes (``now_utc - gmttime``) or ``None`` when the gmttime is
    unparseable. The age lets the caller record a distribution so "really stale"
    (age far beyond the window) can be told apart from normal refresh desync
    (age just past it). Only call when the forecast exists and is available.
    """
    venue_info = getattr(live_forecast, "venue_info", None)
    generated = parse_gmttime(getattr(venue_info, "venue_current_gmttime", None))
    if generated is None:
        return UNPARSEABLE, None
    delta = now_utc - generated
    verdict = FRESH if delta < max_age else STALE
    return verdict, delta.total_seconds() / 60.0


def _coerce_minutes(raw) -> Optional[int]:
    """Pull an int minute count out of the admin value, accepting the
    ``{"minutes": N}`` shape the panel writes or a bare int/str."""
    if isinstance(raw, dict):
        raw = raw.get("minutes")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def resolve_refresh_minutes(admin_config_service=None) -> int:
    """Effective live refresh cadence in minutes: the admin override
    (``admin_config:live_refresh_minutes``) if present and in-bounds, else the
    settings default. Mirrors refresh_interval_watch so the freshness window is
    derived from the SAME interval the refresher runs on. Never raises."""
    default = settings.venues_live_refresh_minutes
    if admin_config_service is None:
        return default
    try:
        raw = admin_config_service.get(_REFRESH_MINUTES_KEY)
    except Exception as e:  # pragma: no cover - defensive; admin read is best-effort
        logger.warning(f"[live_freshness] refresh-interval read failed: {e}; using default")
        return default
    minutes = _coerce_minutes(raw)
    if minutes is None or not (_REFRESH_MIN_MINUTES <= minutes <= _REFRESH_MAX_MINUTES):
        if raw is not None:
            logger.warning(
                f"[live_freshness] {_REFRESH_MINUTES_KEY}={raw!r} invalid/out-of-bounds "
                f"[{_REFRESH_MIN_MINUTES}, {_REFRESH_MAX_MINUTES}]; using default {default}"
            )
        return default
    return minutes


def resolve_max_age_minutes(admin_config_service=None) -> int:
    """Effective freshness window in minutes, DERIVED from the refresh cadence:
    ``factor × refresh_minutes``, floored at ``live_freshness_min_minutes`` so a
    very short interval still leaves room for BestTime/clock skew. Never raises."""
    interval = resolve_refresh_minutes(admin_config_service)
    window = round(settings.live_freshness_refresh_factor * interval)
    return max(settings.live_freshness_min_minutes, window)
