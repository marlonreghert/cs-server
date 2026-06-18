"""America/Recife day bucketing — the repo's canonical local-day convention.

Activity counts, BestTime day indices, and operator-facing "today" all use the
Recife calendar day. Centralized here so callers don't re-derive the timezone.
"""
from __future__ import annotations

from datetime import date, datetime

import pytz

RECIFE_TZ = pytz.timezone("America/Recife")


def recife_now() -> datetime:
    """Current wall-clock time in America/Recife."""
    return datetime.now(RECIFE_TZ)


def recife_today() -> date:
    """Current calendar date in America/Recife."""
    return recife_now().date()
