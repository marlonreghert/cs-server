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
- A weekday CORROBORATES an explicit date; it never REPLACES one
  (plans/260807_date-resolution-correctness.md). When the text has no
  explicit date at all, a bare/qualified weekday ("sábado", "este sábado")
  still resolves to the next occurrence at or after the post date — but only
  when no day-of-month numeral is present and unconsumed. A numeral present
  with no month it can pair with (an unparseable month abbreviation, say) is
  never silently discarded in favor of the weekday's guess: that resolves to
  no date instead, the same visible blank as any other unparseable date. When
  BOTH an explicit date and a weekday are stated and they name different
  days, the explicit date wins (the more precise claim) and the disagreement
  is flagged via `REASON_WEEKDAY_MISMATCH` rather than silently trusted
  either way.
- A recurring announcement ("toda quinta") is flagged `is_recurring` and its
  `starts_at` is the next occurrence of that weekday at or after the post
  date — this path runs BEFORE any of the above and is never affected by the
  weekday-corroboration rule: there is no explicit date to corroborate, the
  weekday legitimately *is* the entire content.
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
# An explicit date parsed AND a weekday was stated AND they name different
# days (plans/260807_date-resolution-correctness.md, defect 2b). The explicit
# date is trusted (it is the more precise claim) but the disagreement is
# surfaced rather than silently resolved either way — a flyer typo somewhere,
# and which half is wrong is an operator's call, not this module's.
REASON_WEEKDAY_MISMATCH = "weekday_mismatch"

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

# Full names plus the twelve 3-letter pt-BR abbreviations Brazilian flyers
# actually use ("05/SET", "08/Ago") — plans/260807_date-resolution-
# correctness.md defect 1. Mapping several keys onto the same month number is
# fine; a dict has no trouble with that.
_MONTHS: dict[str, int] = {
    "janeiro": 1, "jan": 1,
    "fevereiro": 2, "fev": 2,
    "março": 3, "marco": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "maio": 5, "mai": 5,
    "junho": 6, "jun": 6,
    "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9,
    "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}
# Only the abbreviated forms are ever written month-first ("SET 05") — a
# flyer never writes "dezembro 20" that way, so the reversed pattern below is
# deliberately narrower than the day-first one rather than a blanket mirror
# of every _MONTHS key.
_MONTH_ABBREVIATIONS = (
    "jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez",
)

_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?\b")
# Longest keys first so "setembro" is tried before the abbreviated "set"
# inside the same alternation (purely cosmetic — the trailing \b already
# prevents "set" from matching inside "setembro"; this just avoids the
# wasted backtrack).
_MONTH_NAMES_BY_LENGTH = sorted(_MONTHS, key=len, reverse=True)
# "5 de setembro", "5 setembro", "05/set", "08.Ago" — day first, ANY month
# form. The day numeral must be immediately adjacent (a required \b on both
# ends): this is what stops "mar"/"set"/"mai" — ordinary Portuguese words —
# from matching inside free caption prose with no day beside them.
_TEXTUAL_DATE_RE = re.compile(
    r"\b(\d{1,2})(?:\s*de\s*|\s*[/.\-]\s*|\s+)?(" + "|".join(_MONTH_NAMES_BY_LENGTH) + r")\b",
    re.IGNORECASE,
)
# "SET 05" — the abbreviated form written month-first. Same adjacency
# requirement, same reason: a bare "set" with no day number beside it must
# never match.
_TEXTUAL_DATE_REVERSED_RE = re.compile(
    r"\b(" + "|".join(_MONTH_ABBREVIATIONS) + r")(?:\s*[/.\-]\s*|\s+)(\d{1,2})\b",
    re.IGNORECASE,
)
# A standalone 1-2 digit number ("15" in "sexta 15") — a day-of-month numeral
# the earlier date patterns did NOT consume. `\b` on both ends is what keeps
# this from firing on "22h" (no boundary between the digits and the "h") or
# on the digits inside an already-matched numeric/textual date (those
# branches return before this one is ever consulted). Defect 2's guard: this
# is the numeral the weekday fallback used to silently discard.
_BARE_DAY_NUMERAL_RE = re.compile(r"\b\d{1,2}\b")

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


# Sentinel distinguishing "this pattern did not fire at all" (try the next
# one) from "it fired and resolved to an invalid combination, like 31/02"
# (stop looking — that numeral is CONSUMED, never fall through to a weekday
# guess for it). `None` alone cannot carry that distinction since it is also
# the valid "invalid combination" result.
class _NoMatch:
    __slots__ = ()


_NO_MATCH = _NoMatch()


def _roll_forward(candidate: date, anchor: date, year: int, month: int, day: int) -> Optional[date]:
    """`candidate` if it is at/after `anchor`, else the same month/day one
    year later — the shared "no year stated" forward-fill rule."""
    if candidate >= anchor:
        return candidate
    try:
        return date(year + 1, month, day)
    except ValueError:
        return None  # e.g. 29/02 rolling into a non-leap year


def _numeric_date(text: str, anchor: date):
    """Day/month(/year), ALWAYS day-first — never month-first. Returns
    `_NO_MATCH` when the pattern never fired, `None` when it fired on an
    invalid combination (e.g. "31/02"), else a resolved `date`."""
    m = _NUMERIC_DATE_RE.search(text)
    if not m:
        return _NO_MATCH
    day, month, year_raw = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
            return date(year, month, day)
        candidate = date(anchor.year, month, day)
    except ValueError:
        return None  # e.g. "31/02" — not a real date; never guess one
    return _roll_forward(candidate, anchor, anchor.year, month, day)


def _textual_date(text: str, anchor: date):
    """"25 de dezembro", "3 agosto", "05/set", "SET 05" — day-first (any
    month form) or the abbreviated month-first order. Same `_NO_MATCH`/`None`
    contract as `_numeric_date`."""
    m = _TEXTUAL_DATE_RE.search(text)
    if m:
        day, month = int(m.group(1)), _MONTHS[m.group(2).lower()]
    else:
        m = _TEXTUAL_DATE_REVERSED_RE.search(text)
        if not m:
            return _NO_MATCH
        month, day = _MONTHS[m.group(1).lower()], int(m.group(2))
    try:
        candidate = date(anchor.year, month, day)
    except ValueError:
        return None
    return _roll_forward(candidate, anchor, anchor.year, month, day)


def _corroborate_with_weekday(text: str, explicit_date: date) -> tuple[date, Optional[str]]:
    """An explicit date has already been resolved. A weekday mentioned
    ALONGSIDE it only corroborates (plans/260807_date-resolution-
    correctness.md, defect 2b): agreement stays silent, disagreement is
    flagged `REASON_WEEKDAY_MISMATCH` — the explicit date is trusted either
    way, never overridden by the weekday."""
    m = _WEEKDAY_RE.search(text)
    if m is None:
        return explicit_date, None
    stated_weekday = _WEEKDAYS[m.group(0).lower()]
    if explicit_date.weekday() == stated_weekday:
        return explicit_date, None
    return explicit_date, REASON_WEEKDAY_MISMATCH


def _resolve_explicit_date(date_text: str, anchor: date) -> tuple[Optional[date], Optional[str]]:
    """A calendar date out of free text, resolved against `anchor` (the
    post's own date), plus a review reason when the resolution needs an
    operator's eye despite producing a date. Returns `(None, None)` when
    nothing recognisable is present at all — the caller treats that as
    "never invent a date", not as an error to raise.
    """
    text = date_text.strip().lower()

    if text in ("hoje", "hoje à noite", "hoje a noite"):
        return anchor, None
    if text in ("amanhã", "amanha"):
        return anchor + timedelta(days=1), None

    for finder in (_numeric_date, _textual_date):
        result = finder(text, anchor)
        if result is _NO_MATCH:
            continue
        if result is None:
            return None, None  # matched but invalid — consumed, never guess
        return _corroborate_with_weekday(text, result)

    # No explicit date pattern matched at all. A bare or qualified weekday
    # ("sábado", "este sábado", "esse domingo") not marked recurring resolves
    # to the next occurrence at or after the post date — UNLESS a
    # day-of-month numeral is ALSO present and was never consumed by the loop
    # above (an unparseable month abbreviation, say). Silently discarding
    # that numeral in favor of the weekday's guess is exactly defect 2: the
    # load-bearing guard is "numeral present AND unresolved", never merely
    # "weekday present" — `este sábado` with no competing numeral must keep
    # resolving, and the recurrence path never reaches this function at all
    # (resolve_event_datetime branches to it earlier).
    m = _WEEKDAY_RE.search(text)
    if m:
        if _BARE_DAY_NUMERAL_RE.search(text):
            return None, None
        weekday = _WEEKDAYS[m.group(0).lower()]
        return _next_weekday_on_or_after(anchor, weekday), None

    return None, None


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
    # True whenever an operator should look at this event's date, which is
    # NOT the same as "no date was resolved": a `REASON_WEEKDAY_MISMATCH`
    # flags `needs_review=True` alongside a real, non-None `starts_at` — the
    # explicit date is trusted and used, the disagreement is only surfaced,
    # never allowed to blank the result the way a genuinely unparseable date
    # does.
    needs_review: bool
    review_reason: Optional[str]
    # Whether a clock time was actually PARSED out of `time_text`, as opposed
    # to defaulted to midnight because none was stated. A stated "00h" and a
    # defaulted midnight land on the exact same `starts_at` instant, so this
    # is the only way the caller can tell them apart — checking `start_time`
    # (the parse result) rather than the hour value itself matters: (0, 0) is
    # a truthy tuple, so a naive `if not start_time_of_day` check would treat
    # a real stated midnight as unknown, the same class of bug already fixed
    # once in this repo for a temperature of 0.
    time_known: bool


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

    review_reason: Optional[str] = None
    if is_recurring:
        # The recurrence path never reaches _resolve_explicit_date, so it can
        # never trip the weekday-corroboration guard above — there is no
        # explicit date here to corroborate; the weekday IS the content.
        resolved_date = _next_weekday_on_or_after(anchor_date, recurring_weekday)
    elif date_text and date_text.strip():
        resolved_date, review_reason = _resolve_explicit_date(date_text, anchor_date)
    else:
        resolved_date = None

    if resolved_date is None:
        return ResolvedDate(
            starts_at=None, ends_at=None, is_recurring=is_recurring,
            recurrence_text=recurrence_text, needs_review=True,
            review_reason=review_reason or REASON_MISSING_DATE, time_known=False,
        )

    start_time, end_time = _parse_time_text(time_text)
    # A date with an unstated clock time defaults to midnight: the date itself
    # was resolved deterministically (never guessed), and only the ABSENT
    # time-of-day is defaulted here, not the date. `time_known` records
    # whether `start_time` itself is None — never whether the hour is
    # truthy, which a stated midnight would fail.
    time_known = start_time is not None
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
        recurrence_text=recurrence_text, needs_review=review_reason is not None,
        review_reason=review_reason, time_known=time_known,
    )


__all__ = [
    "RECIFE_TZ", "REASON_MISSING_DATE", "REASON_WEEKDAY_MISMATCH",
    "ResolvedDate", "resolve_event_datetime",
]
