"""Resolve a flyer/caption's raw date and time text into a real instant.

The single highest-risk piece of plans/260804_instagram-event-extraction.md.
Every rule below exists because getting it wrong produces a PLAUSIBLE, WRONG
date rather than a visible error:

- Relative expressions ("hoje", "amanhã", "este sábado") resolve against the
  POST'S OWN timestamp, never the run clock. A post crawled three weeks late
  would otherwise land on the wrong day with no sign anything went wrong.
- A numeric date is always day-first: "05/08" is 5 August, never 5 May.
- A date with no year resolves FORWARD to the next occurrence at or after the
  post date — including across a year boundary ("15/08" on a December post is
  next August, one year later).
- An unparseable or ambiguous date is NEVER guessed. It resolves to
  `starts_at=None` plus `needs_review=True` — an operator will scan a queue of
  blanks, but will not audit a field that looks answered.
- A recurring announcement ("toda quinta") is flagged `is_recurring` and its
  `starts_at` is the next occurrence of that weekday at or after the post date.
- Every resolved instant is timezone-aware, America/Recife (UTC-3, no DST
  since 2019), matching CLAUDE.md's Recife-timezone-preservation requirement.

This module is pure: no I/O, no `datetime.now()`. The caller supplies the
post's timestamp; that is the only clock this code is allowed to read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

RECIFE_TZ = ZoneInfo("America/Recife")

REASON_MISSING_DATE = "missing_date"

# Monday=0 .. Sunday=6, matching `date.weekday()`.
_WEEKDAYS: dict[str, int] = {
    "segunda": 0, "segunda-feira": 0,
    "terça": 1, "terca": 1, "terça-feira": 1, "terca-feira": 1,
    "quarta": 2, "quarta-feira": 2,
    "quinta": 3, "quinta-feira": 3,
    "sexta": 4, "sexta-feira": 4,
    "sábado": 5, "sabado": 5,
    "domingo": 6,
}
# Longest names first so "terça-feira" matches before the bare "terça" inside
# the same regex alternation.
_WEEKDAY_PATTERN = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_WEEKDAY_RE = re.compile(_WEEKDAY_PATTERN, re.IGNORECASE)
_RECURRING_MARKER_RE = re.compile(r"\b(?:toda|todo)s?\b", re.IGNORECASE)

_MONTHS: dict[str, int] = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}

_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?\b")
_TEXTUAL_DATE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:de)?\s*(" + "|".join(_MONTHS) + r")\b", re.IGNORECASE,
)

# "22h", "22h30", "22:00", "10pm", "10 pm" — captured separately so a range
# ("22h às 04h") can be split into two independent tokens and parsed the same
# way. `h` notation is always 24h (Brazilian convention: "22h" cannot mean
# 10am), so it never carries an am/pm suffix.
_TIME_TOKEN_RE = re.compile(
    r"(\d{1,2})(?::(\d{2})|h(\d{2})?)?\s*(am|pm)?", re.IGNORECASE,
)


def _find_time_tokens(text: str) -> list[tuple[int, int]]:
    """Every (hour, minute) parsed left-to-right out of a time expression."""
    out: list[tuple[int, int]] = []
    for m in _TIME_TOKEN_RE.finditer(text):
        if not m.group(0).strip():
            continue
        hour = int(m.group(1))
        minute = int(m.group(2) or m.group(3) or 0)
        meridiem = (m.group(4) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        out.append((hour, minute))
    return out


def _parse_time_text(time_text: Optional[str]) -> tuple[Optional[tuple[int, int]], Optional[tuple[int, int]]]:
    """(start (hour, minute), end (hour, minute) or None) from free text."""
    if not time_text or not time_text.strip():
        return None, None
    tokens = _find_time_tokens(time_text)
    if not tokens:
        return None, None
    start = tokens[0]
    end = tokens[1] if len(tokens) > 1 else None
    return start, end


def _next_weekday_on_or_after(anchor: date, weekday: int) -> date:
    """The next `weekday` (0=Monday..6=Sunday) at or after `anchor`, inclusive
    of `anchor` itself when it already falls on that weekday."""
    delta = (weekday - anchor.weekday()) % 7
    return anchor + timedelta(days=delta)


def _parse_date_text(date_text: str, anchor: date) -> Optional[date]:
    """A calendar date out of free text, resolved against `anchor` (the post's
    own date). Returns None when nothing recognisable is present — the caller
    treats that as "never invent a date", not as an error to raise.
    """
    text = date_text.strip().lower()

    if text in ("hoje", "hoje à noite", "hoje a noite"):
        return anchor
    if text in ("amanhã", "amanha"):
        return anchor + timedelta(days=1)

    # Numeric day/month(/year), ALWAYS day-first — never month-first.
    m = _NUMERIC_DATE_RE.search(text)
    if m:
        day, month, year_raw = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            if year_raw:
                year = int(year_raw)
                if year < 100:
                    year += 2000
                return date(year, month, day)
            candidate = date(anchor.year, month, day)
            if candidate < anchor:
                candidate = date(anchor.year + 1, month, day)
            return candidate
        except ValueError:
            return None  # e.g. "31/02" — not a real date; never guess one

    # Textual month name ("25 de dezembro", "3 agosto") — same forward-fill
    # rule as the numeric form when no year is stated (this format never
    # carries one).
    m = _TEXTUAL_DATE_RE.search(text)
    if m:
        day = int(m.group(1))
        month = _MONTHS[m.group(2).lower()]
        try:
            candidate = date(anchor.year, month, day)
        except ValueError:
            return None
        if candidate < anchor:
            try:
                candidate = date(anchor.year + 1, month, day)
            except ValueError:
                return None
        return candidate

    # A bare or qualified weekday ("sábado", "este sábado", "esse domingo")
    # not marked recurring — the next occurrence at or after the post date.
    m = _WEEKDAY_RE.search(text)
    if m:
        weekday = _WEEKDAYS[m.group(0).lower()]
        return _next_weekday_on_or_after(anchor, weekday)

    return None


def _detect_recurrence(date_text: Optional[str]) -> Optional[int]:
    """The target weekday (0=Monday..6=Sunday) when `date_text` names a
    recurring announcement ("toda quinta", "todo sábado"), else None."""
    if not date_text:
        return None
    text = date_text.strip().lower()
    if not _RECURRING_MARKER_RE.search(text):
        return None
    m = _WEEKDAY_RE.search(text)
    if not m:
        return None
    return _WEEKDAYS[m.group(0).lower()]


@dataclass(frozen=True)
class ResolvedDate:
    starts_at: Optional[datetime]
    ends_at: Optional[datetime]
    is_recurring: bool
    recurrence_text: Optional[str]
    needs_review: bool
    review_reason: Optional[str]


def _as_recife(post_timestamp: datetime) -> datetime:
    """A tz-aware America/Recife instant. A naive timestamp is assumed to
    already be Recife local time (the archive pipeline never stores naive
    UTC) rather than silently treated as UTC, which would shift the anchor
    date by up to three hours and could flip which calendar day "hoje" means."""
    if post_timestamp.tzinfo is None:
        return post_timestamp.replace(tzinfo=RECIFE_TZ)
    return post_timestamp.astimezone(RECIFE_TZ)


def resolve_event_datetime(
    *, date_text: Optional[str], time_text: Optional[str], post_timestamp: datetime,
) -> ResolvedDate:
    """Resolve a flyer/caption's raw date+time text against the post's own
    timestamp. Never reads the wall clock — the run time is not an input.
    """
    anchor_dt = _as_recife(post_timestamp)
    anchor_date = anchor_dt.date()

    recurring_weekday = _detect_recurrence(date_text)
    is_recurring = recurring_weekday is not None
    recurrence_text = date_text.strip() if is_recurring else None

    if is_recurring:
        resolved_date = _next_weekday_on_or_after(anchor_date, recurring_weekday)
    elif date_text and date_text.strip():
        resolved_date = _parse_date_text(date_text, anchor_date)
    else:
        resolved_date = None

    if resolved_date is None:
        return ResolvedDate(
            starts_at=None, ends_at=None, is_recurring=is_recurring,
            recurrence_text=recurrence_text, needs_review=True,
            review_reason=REASON_MISSING_DATE,
        )

    start_time, end_time = _parse_time_text(time_text)
    # A date with an unstated clock time defaults to midnight: the date itself
    # was resolved deterministically (never guessed), and only the ABSENT
    # time-of-day is defaulted here, not the date.
    start_hour, start_minute = start_time or (0, 0)
    starts_at = datetime(
        resolved_date.year, resolved_date.month, resolved_date.day,
        start_hour, start_minute, tzinfo=RECIFE_TZ,
    )

    ends_at: Optional[datetime] = None
    if end_time is not None:
        end_hour, end_minute = end_time
        end_date = resolved_date
        # A range that wraps past midnight ("22h às 04h") ends the NEXT day.
        if (end_hour, end_minute) <= (start_hour, start_minute):
            end_date = resolved_date + timedelta(days=1)
        ends_at = datetime(
            end_date.year, end_date.month, end_date.day,
            end_hour, end_minute, tzinfo=RECIFE_TZ,
        )

    return ResolvedDate(
        starts_at=starts_at, ends_at=ends_at, is_recurring=is_recurring,
        recurrence_text=recurrence_text, needs_review=False, review_reason=None,
    )


__all__ = [
    "RECIFE_TZ", "REASON_MISSING_DATE", "ResolvedDate", "resolve_event_datetime",
]
