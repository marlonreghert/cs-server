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
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

RECIFE_TZ = ZoneInfo("America/Recife")

# plans/260812_event-attribution-and-dates.md §D: the default grace window
# for `_roll_forward` — a candidate date up to this many days OLDER than its
# own anchor keeps the anchor's year rather than being rolled a full year
# forward. Runtime-configurable (see `app.models.date_resolution_config`,
# this project's standing pattern for a first-guess constant — the same
# shape `menu_expiry_days` and the category vocabulary already use); the
# resolver itself stays pure (no I/O, no Redis read) and simply accepts the
# resolved value as a keyword argument, defaulting to this constant so every
# pre-existing caller (and every existing test) that never cared about the
# grace window keeps working unchanged.
DEFAULT_YEAR_ROLL_GRACE_DAYS = 60

REASON_MISSING_DATE = "missing_date"
# An explicit date parsed AND a weekday was stated AND they name different
# days (plans/260807_date-resolution-correctness.md, defect 2b). The explicit
# date is trusted (it is the more precise claim) but the disagreement is
# surfaced rather than silently resolved either way — a flyer typo somewhere,
# and which half is wrong is an operator's call, not this module's.
REASON_WEEKDAY_MISMATCH = "weekday_mismatch"
# `_roll_forward` moved a date without a stated year across a year boundary —
# plans/260810_date-correctness-review-reasons-and-path-parity.md §A. A guess
# wearing `confidence=0.98` is the exact silent-wrong-date class
# plans/260807_date-resolution-correctness.md already fixed once for
# "Sábado • 05/SET"; this is the same failure through a different door. The
# roll itself is NOT stopped (a genuinely future-dated flyer usually rolls
# correctly) — only presented as an inference rather than a certainty, and
# kept out of auto-accept. `vote_on_sibling_years` below may still ADJUST the
# rolled date (when its siblings from the same post disagree with it), but
# never clears this flag: even a majority-corrected roll is still an
# inference, not a stated year.
REASON_YEAR_INFERRED = "year_inferred"
# The model returned several dates for one event ("01, 02 e 03 de julho")
# and only the FIRST is kept as `starts_at` — plans/260810_date-correctness-
# review-reasons-and-path-parity.md §B. Deliberately not `ends_at`: adding a
# column is a data-model change this plan defers; recording the right start
# and admitting a range existed is the honest minimum.
REASON_DATE_RANGE = "date_range"

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

# plans/260810_post-kind-and-post-extraction-attribution.md §D: the forms
# Brazilian venues actually use for a recurring WEEKLY schedule beyond a
# bare "toda"/"todo" + single weekday.
#
# A RANGE ("de segunda a sexta", "de sexta a sábado") — word-boundary
# anchored (`\b`) on purpose, unlike the loose, substring-matching
# `_WEEKDAY_RE` above: this pattern decides whether a whole PHRASE reads as
# a range, so it must never fire on a weekday name that merely appears
# somewhere inside unrelated prose.
_WEEKDAY_RANGE_RE = re.compile(
    r"\bde\s+(" + _WEEKDAY_PATTERN + r")\s+a\s+(" + _WEEKDAY_PATTERN + r")\b",
    re.IGNORECASE,
)
# A weekday named as a plural or bare word, with NO "toda"/"todo" marker and
# no "de X a Y" range shape — "sextas e sábados" (a LIST), or a single bare
# "sextas" (plans/260810 desired behaviour explicitly names both forms).
# `s?` optional so a singular canonical form still matches when scanning for
# more than one weekday in the same phrase; `\b` on both ends (unlike
# `_WEEKDAY_RE`) so this never fires on a word that merely CONTAINS a
# weekday name as a prefix.
_WEEKDAY_WORD_RE = re.compile(r"\b(" + _WEEKDAY_PATTERN + r")s?\b", re.IGNORECASE)

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

# "01, 02 e 03 de julho", "05 e 06/09" — a comma/"e"-joined list of BARE day
# numerals immediately followed by one fully-dated day (day+month, either
# accepted single-date shape below). Group 1 captures every day EXCEPT the
# last (the last is re-parsed by `_final_day_month` from the tail, using the
# SAME two single-date patterns this module already trusts — never a new,
# looser date grammar). The lookahead never consumes the final date itself,
# so `_final_day_month` sees it fresh, anchored at the tail's start.
# plans/260810_date-correctness-review-reasons-and-path-parity.md §B.
_DATE_RANGE_PREFIX_RE = re.compile(
    r"\b(\d{1,2}(?:\s*(?:,|e)\s*\d{1,2})*)\s*(?:,|e)\s*"
    r"(?=\d{1,2}\s*(?:de\s*|[/.\-]\s*|\s+))",
    re.IGNORECASE,
)

_NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?\b")
# Longest keys first so "setembro" is tried before the abbreviated "set"
# inside the same alternation (purely cosmetic — the trailing \b already
# prevents "set" from matching inside "setembro"; this just avoids the
# wasted backtrack).
_MONTH_NAMES_BY_LENGTH = sorted(_MONTHS, key=len, reverse=True)

# "De 06 a 09 de fevereiro" — the PREPOSITION range form
# plans/260812_event-attribution-and-dates.md §D adds: `de` DAY `a` DAY `de`
# MONTH, one shared month for both ends. Unrecognised before this plan, so
# the ordinary single-date finders below matched only the TRAILING date
# ("09 de fevereiro") and silently kept the last day of the range instead of
# the first — the dangerous failure mode named in the plan's own Evidence
# section (it does not fail, it silently returns the wrong day). Deliberately
# a SEPARATE pattern from `_WEEKDAY_RANGE_RE` above (that one matches WEEKDAY
# names between "de" and "a"; this one matches bare day NUMERALS) — the two
# can never fire on the same text.
_DATE_RANGE_PREPOSITION_RE = re.compile(
    r"\bde\s+(\d{1,2})\s+a\s+(\d{1,2})\s*(?:de\s*|[/.\-]\s*)("
    + "|".join(_MONTH_NAMES_BY_LENGTH) + r")\b",
    re.IGNORECASE,
)

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


def _next_matching_weekday_on_or_after(anchor: date, weekdays: set) -> date:
    """The generalisation of `_next_weekday_on_or_after` for a SET of target
    weekdays (a range or a list) — the next date at/after `anchor` whose
    weekday is any member of `weekdays`, inclusive of `anchor` itself. Never
    called with an empty set (every caller only ever builds a non-empty
    one), but falls back to `anchor` rather than looping forever if that
    ever changed."""
    for offset in range(7):
        candidate = anchor + timedelta(days=offset)
        if candidate.weekday() in weekdays:
            return candidate
    return anchor  # pragma: no cover - unreachable: weekdays always non-empty


def _weekday_range(start: int, end: int) -> set:
    """Every weekday from `start` to `end` inclusive, wrapping across the
    week boundary when `end` < `start` (e.g. "de sexta a domingo" ->
    {4, 5, 6})."""
    if end >= start:
        return set(range(start, end + 1))
    return set(range(start, 7)) | set(range(0, end + 1))


def _weekdays_from_recurrence_forms(text: str):
    """Every weekday `text` names as a RANGE ("de segunda a sexta") or as a
    bare/plural LIST with no "toda"/"todo" marker at all ("sextas e
    sábados", or a single bare "sextas") — the forms plans/260810_post-
    kind-and-post-extraction-attribution.md §D adds beyond the classic
    toda/todo + single-weekday marker (which `_detect_recurrence` below
    still handles on its own, unconditionally). None when nothing
    recognisable is present."""
    range_match = _WEEKDAY_RANGE_RE.search(text)
    if range_match:
        start = _WEEKDAYS[range_match.group(1).lower()]
        end = _WEEKDAYS[range_match.group(2).lower()]
        return _weekday_range(start, end)

    matches = [m.group(1).lower() for m in _WEEKDAY_WORD_RE.finditer(text)]
    weekdays = {_WEEKDAYS[w] for w in matches if w in _WEEKDAYS}
    return weekdays or None


# Sentinel distinguishing "this pattern did not fire at all" (try the next
# one) from "it fired and resolved to an invalid combination, like 31/02"
# (stop looking — that numeral is CONSUMED, never fall through to a weekday
# guess for it). `None` alone cannot carry that distinction since it is also
# the valid "invalid combination" result.
class _NoMatch:
    __slots__ = ()


_NO_MATCH = _NoMatch()


def _roll_forward(
    candidate: date, anchor: date, year: int, month: int, day: int,
    grace_days: int = DEFAULT_YEAR_ROLL_GRACE_DAYS,
) -> tuple[Optional[date], bool]:
    """`(candidate, False)` when it is at/after `anchor` (no guess needed,
    nothing was inferred). Otherwise a year must be chosen, and there are
    now two candidates rather than one always-correct answer
    (plans/260812_event-attribution-and-dates.md §D):

      - within `grace_days` of `anchor` (inclusive), KEEP the anchor's own
        year — a date a few days "in the past" is far more often a flyer
        posted slightly after the fact than a date meant a full year out;
      - older than that, ROLL forward one year, exactly as before this
        grace window existed.

    Either way the second element is `year_inferred`
    (plans/260810_date-correctness-review-reasons-and-path-parity.md §A):
    True whenever a year had to be CHOSEN at all (kept OR rolled) — the
    grace window turns the "keep" branch into a guess too (there were two
    plausible years to pick between), never merely because the year itself
    was absent from the text (a same-year candidate that never needed
    either choice, `candidate >= anchor`, is not a guess and stays
    unflagged)."""
    if candidate >= anchor:
        return candidate, False
    if (anchor - candidate).days <= grace_days:
        return candidate, True
    try:
        return date(year + 1, month, day), True
    except ValueError:
        return None, True  # e.g. 29/02 rolling into a non-leap year


def _numeric_date(text: str, anchor: date, grace_days: int = DEFAULT_YEAR_ROLL_GRACE_DAYS):
    """Day/month(/year), ALWAYS day-first — never month-first. Returns
    `_NO_MATCH` when the pattern never fired, else `(date_or_None,
    year_inferred)` — `date_or_None` is `None` when the pattern fired on an
    invalid combination (e.g. "31/02"), in which case `year_inferred` is
    always False (nothing was inferred; the numeral was simply invalid)."""
    m = _NUMERIC_DATE_RE.search(text)
    if not m:
        return _NO_MATCH
    day, month, year_raw = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
            return date(year, month, day), False
        candidate = date(anchor.year, month, day)
    except ValueError:
        return None, False  # e.g. "31/02" — not a real date; never guess one
    return _roll_forward(candidate, anchor, anchor.year, month, day, grace_days)


def _textual_date(text: str, anchor: date, grace_days: int = DEFAULT_YEAR_ROLL_GRACE_DAYS):
    """"25 de dezembro", "3 agosto", "05/set", "SET 05" — day-first (any
    month form) or the abbreviated month-first order. Same `_NO_MATCH`/
    `(date_or_None, year_inferred)` contract as `_numeric_date`."""
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
        return None, False
    return _roll_forward(candidate, anchor, anchor.year, month, day, grace_days)


def _final_day_month(tail: str) -> Optional[tuple[int, int, Optional[str]]]:
    """`(day, month, year_raw_or_None)` from the IMMEDIATE start of `tail` —
    the text right after a date-range prefix ("03 de julho", "06/09") — or
    `None`. Anchored with `.match` (never `.search`), so only the date
    PHYSICALLY ADJACENT to the day list counts, never one appearing later in
    unrelated prose. Reuses the exact two single-date shapes `_numeric_date`/
    `_textual_date` already accept — never a new, looser date grammar."""
    m = _NUMERIC_DATE_RE.match(tail)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(3)
    m = _TEXTUAL_DATE_RE.match(tail)
    if m:
        return int(m.group(1)), _MONTHS[m.group(2).lower()], None
    return None


def _date_range_candidate(
    text: str, anchor: date, grace_days: int = DEFAULT_YEAR_ROLL_GRACE_DAYS,
):
    """`_NO_MATCH` when no "day list + final date" shape (comma/"e"-joined,
    `_DATE_RANGE_PREFIX_RE`) AND no "de X a Y de MONTH" preposition shape
    (`_DATE_RANGE_PREPOSITION_RE`) is present at all — the overwhelmingly
    common case, falling through unchanged to the ordinary single-date
    finders. Otherwise `(date_or_None, year_inferred, date_range=True)`,
    resolved from the FIRST day of the range against `anchor` exactly like a
    single date would be (plans/260810_date-correctness-review-reasons-and-
    path-parity.md §B: "an event starts on the day it starts", never its
    last day). The preposition form is checked FIRST: it is more specific
    (both a start and an end day, one shared month) and the comma-list form
    could not accidentally match its text anyway (no comma/"e"-joined day
    list is present in "de 06 a 09 de fevereiro")."""
    prep_match = _DATE_RANGE_PREPOSITION_RE.search(text)
    if prep_match:
        first_day, month = int(prep_match.group(1)), _MONTHS[prep_match.group(3).lower()]
        try:
            candidate = date(anchor.year, month, first_day)
        except ValueError:
            return None, False, True  # matched but invalid — consumed, never guess
        resolved, rolled = _roll_forward(candidate, anchor, anchor.year, month, first_day, grace_days)
        return resolved, rolled, True

    m = _DATE_RANGE_PREFIX_RE.search(text)
    if not m:
        return _NO_MATCH
    final = _final_day_month(text[m.end():])
    if final is None:
        return _NO_MATCH  # a bare number list with nothing dated after it
    _, month, year_raw = final
    days = [int(d) for d in re.findall(r"\d{1,2}", m.group(1))]
    if not days:
        return _NO_MATCH  # unreachable: the prefix regex always captures >=1 digit
    first_day = days[0]
    try:
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
            return date(year, month, first_day), False, True
        candidate = date(anchor.year, month, first_day)
    except ValueError:
        return None, False, True  # matched but invalid — consumed, never guess
    resolved, rolled = _roll_forward(candidate, anchor, anchor.year, month, first_day, grace_days)
    return resolved, rolled, True


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


def _resolve_explicit_date(
    date_text: str, anchor: date, grace_days: int = DEFAULT_YEAR_ROLL_GRACE_DAYS,
) -> tuple[Optional[date], Optional[str], bool, bool]:
    """A calendar date out of free text, resolved against `anchor` (the
    post's own date). Returns `(date_or_None, review_reason, year_inferred,
    date_range)`:
      - `review_reason`: an operator's eye is needed despite producing a
        date (currently only `REASON_WEEKDAY_MISMATCH`);
      - `year_inferred`: the year was GUESSED by rolling forward across a
        year boundary (plans/260810_date-correctness-review-reasons-and-
        path-parity.md §A) — independent of `review_reason`, since a rolled
        date is never itself a mismatch;
      - `date_range`: several dates were stated for one event and only the
        FIRST was kept (§B).
    `(None, None, False, False)` when nothing recognisable is present at
    all — the caller treats that as "never invent a date", not as an error
    to raise (`resolve_event_datetime` then tries the model's structured
    `date_interpretation` as a fallback — see `_interpretation_to_date`)."""
    text = date_text.strip().lower()

    if text in ("hoje", "hoje à noite", "hoje a noite"):
        return anchor, None, False, False
    if text in ("amanhã", "amanha"):
        return anchor + timedelta(days=1), None, False, False

    # A day-list-plus-final-date shape ("01, 02 e 03 de julho") or the
    # preposition form ("de 06 a 09 de fevereiro") is checked BEFORE the
    # ordinary single-date finders below: those would still match the
    # TRAILING date alone and silently lose the range. Only ever fires on
    # shapes those finders already accept — see `_date_range_candidate`'s
    # own docstring.
    range_result = _date_range_candidate(text, anchor, grace_days)
    if range_result is not _NO_MATCH:
        resolved_date, rolled, is_range = range_result
        if resolved_date is None:
            return None, None, False, is_range  # matched but invalid — consumed, never guess
        date_val, reason = _corroborate_with_weekday(text, resolved_date)
        return date_val, reason, rolled, is_range

    for finder in (_numeric_date, _textual_date):
        result = finder(text, anchor, grace_days)
        if result is _NO_MATCH:
            continue
        resolved_date, rolled = result
        if resolved_date is None:
            return None, None, False, False  # matched but invalid — consumed, never guess
        date_val, reason = _corroborate_with_weekday(text, resolved_date)
        return date_val, reason, rolled, False

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
            return None, None, False, False
        weekday = _WEEKDAYS[m.group(0).lower()]
        return _next_weekday_on_or_after(anchor, weekday), None, False, False

    return None, None, False, False


def _detect_recurrence(
    date_text: Optional[str], *, is_recurring: Optional[bool] = None,
    recurrence_text: Optional[str] = None,
) -> Optional[set]:
    """The set of target weekdays (0=Monday..6=Sunday) when the post is a
    recurring announcement, else None. Two independent ways to reach a
    positive answer, checked in order:

      1. The classic toda/todo(s) + single-weekday marker in `date_text`
         ALONE — UNCONDITIONAL on `is_recurring`. This branch predates that
         parameter; every existing caller (and unit test) calls this with
         `date_text` only, so it must keep working exactly as it always has
         with `is_recurring`/`recurrence_text` never supplied at all. A
         marker present with no weekday found beside it falls through to
         (2) rather than giving up outright — the model may have written
         the actual weekday phrase in `recurrence_text` instead.
      2. plans/260810_post-kind-and-post-extraction-attribution.md §D: a
         weekday RANGE ("de segunda a sexta"), a LIST ("sextas e
         sábados"), or a bare plural weekday ("sextas"), found in EITHER
         `recurrence_text` or `date_text` — but ONLY when the model itself
         claimed `is_recurring=True`. These forms carry no "toda"/"todo"
         marker of their own, so trusting them unconditionally would
         re-open exactly the one-off-weekday-overriding-an-explicit-date
         hole plans/260807_date-resolution-correctness.md closed on
         purpose (a caption naming a weekday with no recurring cadence
         stated must resolve through the ONE-OFF path below, never this
         one) — the model's own claim is the gate that keeps this narrow.
    """
    if date_text:
        text = date_text.strip().lower()
        if _RECURRING_MARKER_RE.search(text):
            m = _WEEKDAY_RE.search(text)
            if m:
                return {_WEEKDAYS[m.group(0).lower()]}

    if is_recurring:
        for text in (recurrence_text, date_text):
            if not text:
                continue
            weekdays = _weekdays_from_recurrence_forms(text.strip().lower())
            if weekdays:
                return weekdays

    return None


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
    # plans/260810_date-correctness-review-reasons-and-path-parity.md §A: the
    # year was GUESSED by rolling a date without a stated year across a year
    # boundary — never merely because the year was absent (a same-year
    # candidate that never rolled is not a guess). Independent of
    # `review_reason`/`needs_review`'s OWN value: a rolled date sets
    # `needs_review=True` even when `review_reason` is otherwise None (no
    # weekday mismatch, a real starts_at). `vote_on_sibling_years` may adjust
    # `starts_at`'s year using this post's other events, but never clears
    # this flag — a majority-corrected roll is still an inference.
    year_inferred: bool = False
    # §B: several dates were stated for one event ("01, 02 e 03 de julho")
    # and only the FIRST was kept as `starts_at`.
    date_range: bool = False
    # plans/260812_event-attribution-and-dates.md §C/Error Handling: how
    # THIS date was actually reached — "deterministic" (a regex finder in
    # this module resolved it, recurrence included — the model's
    # `date_interpretation` was never even consulted), "structured_fallback"
    # (the deterministic finders found nothing and `date_interpretation`
    # resolved it), or "unresolved" (nothing resolved it). The caller
    # (EVENT_DATE_RESOLUTION_TOTAL) is what further splits a fallback into
    # "fresh" vs "reused" — this module has no notion of "stored", so it
    # cannot make that distinction itself.
    date_source: str = "unresolved"


# ── §C: the model interprets, Python computes ───────────────────────────────
# The tagged shape `date_interpretation` may hold — plans/260812_event-
# attribution-and-dates.md §C. Every kind Python recognises; a `kind` outside
# this set (or a non-dict payload) is simply unusable and never guessed at.
INTERPRETATION_KIND_RELATIVE = "relative"
INTERPRETATION_KIND_DAY_MONTH = "day_month"
INTERPRETATION_KIND_DAY_MONTH_YEAR = "day_month_year"
INTERPRETATION_KIND_WEEKDAY = "weekday"
INTERPRETATION_KIND_WEEKDAY_DAY = "weekday_day"
INTERPRETATION_KIND_RANGE = "range"

_INTERPRETATION_KINDS = frozenset({
    INTERPRETATION_KIND_RELATIVE, INTERPRETATION_KIND_DAY_MONTH,
    INTERPRETATION_KIND_DAY_MONTH_YEAR, INTERPRETATION_KIND_WEEKDAY,
    INTERPRETATION_KIND_WEEKDAY_DAY, INTERPRETATION_KIND_RANGE,
})


def _interpretation_to_date(
    interpretation, anchor: date, grace_days: int = DEFAULT_YEAR_ROLL_GRACE_DAYS,
) -> tuple[Optional[date], Optional[str], bool, bool]:
    """§C's fallback: `(date_or_None, review_reason, year_inferred,
    date_range)` — the SAME four-tuple shape `_resolve_explicit_date`
    returns, computed from the model's structured `interpretation` using the
    IDENTICAL arithmetic primitives every deterministic finder above already
    uses (`_roll_forward`, `_next_weekday_on_or_after`) — never a second,
    parallel notion of "what date is this". The model supplies literal,
    categorised FACTS it read off the flyer; Python performs every
    calculation. Any key besides the ones this function explicitly reads
    (including a model-invented "computed" date under some other name) is
    silently ignored — this is what makes a model-supplied absolute date a
    no-op by construction, never something this function has to specially
    detect and reject.

    `(None, None, False, False)` for anything absent, non-dict, or carrying
    an unrecognised/malformed `kind` — never a guess."""
    if not isinstance(interpretation, dict):
        return None, None, False, False
    kind = interpretation.get("kind")
    if kind not in _INTERPRETATION_KINDS:
        return None, None, False, False

    if kind == INTERPRETATION_KIND_RELATIVE:
        relative = interpretation.get("relative")
        if relative == "hoje":
            return anchor, None, False, False
        if relative == "amanha":
            return anchor + timedelta(days=1), None, False, False
        return None, None, False, False

    if kind in (
        INTERPRETATION_KIND_DAY_MONTH, INTERPRETATION_KIND_DAY_MONTH_YEAR,
        INTERPRETATION_KIND_RANGE,
    ):
        day = (
            interpretation.get("range_first_day")
            if kind == INTERPRETATION_KIND_RANGE
            else interpretation.get("day")
        )
        month = interpretation.get("month")
        if not isinstance(day, int) or not isinstance(month, int):
            return None, None, False, False
        if not (1 <= day <= 31 and 1 <= month <= 12):
            return None, None, False, False
        is_range = kind == INTERPRETATION_KIND_RANGE
        year = interpretation.get("year")
        try:
            if kind == INTERPRETATION_KIND_DAY_MONTH_YEAR and isinstance(year, int):
                # An EXPLICITLY-stated year is trusted outright, exactly like
                # the deterministic finders trust one — never rolled, never
                # inferred.
                return date(year, month, day), None, False, is_range
            candidate = date(anchor.year, month, day)
        except ValueError:
            return None, None, False, is_range  # invalid calendar date — never guess
        resolved, rolled = _roll_forward(candidate, anchor, anchor.year, month, day, grace_days)
        if resolved is None:
            return None, None, False, is_range
        return resolved, None, rolled, is_range

    # WEEKDAY / WEEKDAY_DAY
    weekday = _WEEKDAYS.get((interpretation.get("weekday") or "").strip().lower())
    if weekday is None:
        return None, None, False, False
    if kind == INTERPRETATION_KIND_WEEKDAY:
        return _next_weekday_on_or_after(anchor, weekday), None, False, False

    day = interpretation.get("day")
    if not isinstance(day, int) or not (1 <= day <= 31):
        return None, None, False, False
    year, month = anchor.year, anchor.month
    for _ in range(24):  # forward-search bounded to two years, always terminates
        try:
            candidate = date(year, month, day)
        except ValueError:
            candidate = None
        if candidate is not None and candidate >= anchor:
            if candidate.weekday() == weekday:
                return candidate, None, False, False
            return candidate, REASON_WEEKDAY_MISMATCH, False, False
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None, None, False, False  # pragma: no cover - unreachable in practice


def select_date_interpretation_for_reuse(
    *, fresh_date_text, fresh_interpretation, stored_date_text, stored_interpretation,
):
    """The determinism guard §C requires: `compute_source_event_key` hashes
    the RESOLVED date, so a re-extraction whose model answers a STRUCTURED
    interpretation differently for the SAME verbatim `date_text` would
    silently change the key — the exact duplicate-event failure
    `0025_multi_event_posts` was written to prevent, reopened through a new
    door.

    Reuses the STORED interpretation exactly when the fresh `date_text` is
    BYTE-IDENTICAL to the stored one and a stored interpretation exists to
    reuse; otherwise trusts the fresh model answer — a genuinely new or
    edited `date_text` has no stored interpretation worth pinning a re-
    extraction to. Pure and caller-supplied: this module never reads
    "stored" state itself (see the module's own no-I/O guarantee) — the two
    real callers (EventExtractionService, PromoterCrawlService) look up the
    matching existing source row themselves and pass its values in here."""
    if (
        fresh_date_text is not None
        and stored_date_text is not None
        and fresh_date_text == stored_date_text
        and isinstance(stored_interpretation, dict)
    ):
        return stored_interpretation
    return fresh_interpretation


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
    is_recurring: Optional[bool] = None, recurrence_text: Optional[str] = None,
    date_interpretation=None,
    year_roll_grace_days: int = DEFAULT_YEAR_ROLL_GRACE_DAYS,
) -> ResolvedDate:
    """Resolve a flyer/caption's raw date+time text against the post's own
    timestamp. Never reads the wall clock — the run time is not an input.

    `is_recurring`/`recurrence_text` are the MODEL's own answers (plans/
    260810_post-kind-and-post-extraction-attribution.md §D) — optional,
    keyword-only, defaulting to None so every pre-existing caller (which
    only ever passed `date_text`) keeps working unchanged. See
    `_detect_recurrence` for exactly how they extend recurrence detection
    beyond the toda/todo marker without widening the one-off weekday path.

    `date_interpretation` (plans/260812_event-attribution-and-dates.md §C)
    is the model's OWN structured reading of `date_text` — consulted ONLY as
    a FALLBACK, when the deterministic finders below `_resolve_explicit_date`
    calls return no match at all (never when they resolve something, even a
    flagged something). The caller is responsible for the determinism guard
    (`select_date_interpretation_for_reuse`) BEFORE calling this function —
    this function only ever computes from whatever interpretation it is
    given, never fetches or compares a stored one itself (this module stays
    pure, no I/O).

    `year_roll_grace_days` (§D) feeds `_roll_forward`'s grace window —
    defaults to `DEFAULT_YEAR_ROLL_GRACE_DAYS` so every pre-existing caller
    keeps its exact previous behaviour... except that the window itself
    IS the fix: a date up to 60 days "in the past" now keeps its anchor's
    year instead of always rolling.
    """
    anchor_dt = _as_recife(post_timestamp)
    anchor_date = anchor_dt.date()

    recurring_weekdays = _detect_recurrence(
        date_text, is_recurring=is_recurring, recurrence_text=recurrence_text,
    )
    recurring = recurring_weekdays is not None
    # The raw phrase to report back: prefer the model's own `recurrence_text`
    # (more descriptive — e.g. "de segunda a sexta" rather than a bare
    # "toda quinta" duplicated from date_text) when it was actually
    # supplied and non-empty, else fall back to `date_text` itself — the
    # exact value every pre-existing caller/test already expects when only
    # `date_text` carries the marker.
    result_recurrence_text: Optional[str] = None
    if recurring:
        if recurrence_text and recurrence_text.strip():
            result_recurrence_text = recurrence_text.strip()
        elif date_text:
            result_recurrence_text = date_text.strip()

    review_reason: Optional[str] = None
    year_inferred = False
    date_range = False
    date_source = "deterministic"
    if recurring:
        # The recurrence path never reaches _resolve_explicit_date, so it can
        # never trip the weekday-corroboration guard above — there is no
        # explicit date here to corroborate; the weekday IS the content. It
        # can never roll a year or collapse a range either — those are both
        # `_resolve_explicit_date`-only concepts.
        resolved_date = _next_matching_weekday_on_or_after(anchor_date, recurring_weekdays)
    elif date_text and date_text.strip():
        resolved_date, review_reason, year_inferred, date_range = _resolve_explicit_date(
            date_text, anchor_date, year_roll_grace_days,
        )
    else:
        resolved_date = None

    # §C: the model's structured interpretation is a FALLBACK, consulted
    # ONLY when the deterministic path above (recurrence aside) found no
    # date at all — never when it resolved something, even something merely
    # FLAGGED (a weekday mismatch still trusts the explicit date). This is
    # what keeps today's ~97% on their existing, proven path with zero
    # behaviour change.
    if not recurring and resolved_date is None:
        fallback_date, fallback_reason, fallback_year_inferred, fallback_range = (
            _interpretation_to_date(date_interpretation, anchor_date, year_roll_grace_days)
        )
        if fallback_date is not None:
            resolved_date = fallback_date
            review_reason = fallback_reason
            year_inferred = fallback_year_inferred
            date_range = fallback_range
            date_source = "structured_fallback"

    if resolved_date is None:
        return ResolvedDate(
            starts_at=None, ends_at=None, is_recurring=recurring,
            recurrence_text=result_recurrence_text, needs_review=True,
            review_reason=review_reason or REASON_MISSING_DATE, time_known=False,
            # Moot once there is no date at all — a review reason already
            # covers it, and "the year was inferred"/"a range was collapsed"
            # mean nothing without a resolved date to have inferred or
            # collapsed.
            year_inferred=False, date_range=date_range, date_source="unresolved",
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
        starts_at=starts_at, ends_at=ends_at, is_recurring=recurring,
        recurrence_text=result_recurrence_text,
        # A rolled year needs an operator's eye every bit as much as a
        # weekday mismatch does, even though `review_reason` itself may be
        # None (no OTHER problem) — see `year_inferred`'s own docstring.
        needs_review=review_reason is not None or year_inferred,
        review_reason=review_reason, time_known=time_known,
        year_inferred=year_inferred, date_range=date_range, date_source=date_source,
    )


def _correct_group_to(
    out: list[ResolvedDate], indexes: list[int], target_year: int,
) -> None:
    """Mutates `out` in place: every `year_inferred` member of `indexes`
    whose year disagrees with `target_year` is moved onto it. Never touches
    an explicitly-stated (non-inferred) date, and never clears
    `year_inferred`/`needs_review` — a corrected roll is still an inference."""
    for i in indexes:
        r = out[i]
        if not r.year_inferred or r.starts_at.year == target_year:
            continue
        try:
            new_starts_at = r.starts_at.replace(year=target_year)
        except ValueError:
            continue  # e.g. the sibling landed on a leap day; leave it be
        new_ends_at = r.ends_at
        if new_ends_at is not None:
            try:
                new_ends_at = new_ends_at.replace(
                    year=target_year + (r.ends_at.year - r.starts_at.year),
                )
            except ValueError:
                continue
        out[i] = replace(r, starts_at=new_starts_at, ends_at=new_ends_at)


def vote_on_sibling_years(resolved: list[ResolvedDate]) -> list[ResolvedDate]:
    """plans/260810_date-correctness-review-reasons-and-path-parity.md §A:
    let dates from the SAME post agree on a year. `resolved` must already be
    every date `resolve_event_datetime` produced for ONE post's events, in
    order — never mixed across posts (an unrelated event dragging its year
    onto another would be a worse bug than the one this fixes) and never
    called with anything but a single post's own list.

    Only a date whose year was ITSELF inferred (`year_inferred=True`) is
    ever adjusted — an explicitly stated year is trusted regardless of what
    its siblings say. The correction never clears `year_inferred`/
    `needs_review`: a corrected roll is still an inference, not a stated
    year, and must stay out of auto-accept exactly like an uncorrected one.

    Voting is scoped to siblings sharing the same CALENDAR MONTH. This is
    load-bearing, not cosmetic: a post can legitimately span a year
    boundary ("27, 28, 29 de dezembro e 02, 03 de janeiro" — a New Year's
    programme) where the December dates correctly stay in the post's own
    year and the January dates correctly roll into the next one. Without
    the month gate, those two genuinely-different-year groups would vote
    against each other and drag the January dates back — a worse bug than
    the one this function exists to fix. December and January never share
    a group, so they never vote on each other.

    Within one month's group of >=2 dated siblings:
      - a UNIQUE plurality year wins outright, and every inferred sibling
        disagreeing with it is corrected onto it (the common case: three
        siblings already agree, one rolled outlier does not);
      - an exact TIE prefers whichever tied year belongs to a NON-inferred
        (explicitly stated, or simply never rolled) sibling — a year
        nobody had to guess outranks one that was guessed, even when raw
        counts agree. `_roll_forward` always rolls every inferred sibling
        in a post to the SAME target year (anchor.year + 1), so two
        inferred siblings can never disagree with each other; the only way
        a tie is genuinely ambiguous is when two (or more) DIFFERENT
        non-inferred years are both present — two disagreeing trusted
        answers refuse to guess, exactly like the plan requires;
      - a group with no unique plurality and no single trusted tied year
        is left completely alone.

    A post with fewer than two dated events in ANY month (a genuine
    single-date post, or every event still dateless) is returned unchanged.
    """
    dated_indexes = [i for i, r in enumerate(resolved) if r.starts_at is not None]
    if len(dated_indexes) < 2:
        return resolved

    by_month: dict[int, list[int]] = {}
    for i in dated_indexes:
        by_month.setdefault(resolved[i].starts_at.month, []).append(i)

    out = list(resolved)
    for indexes in by_month.values():
        if len(indexes) < 2:
            continue  # nothing in this month to vote against

        year_counts: dict[int, int] = {}
        for i in indexes:
            year = out[i].starts_at.year
            year_counts[year] = year_counts.get(year, 0) + 1
        top_count = max(year_counts.values())
        leaders = [year for year, count in year_counts.items() if count == top_count]

        if len(leaders) == 1:
            target_year = leaders[0]
        else:
            # A tie: defer to whichever tied year a NON-inferred sibling
            # actually holds. Ambiguous (refuse to guess) when zero or more
            # than one tied year has a non-inferred holder.
            native_tied = {
                year for year in leaders
                if any(not out[i].year_inferred and out[i].starts_at.year == year for i in indexes)
            }
            if len(native_tied) != 1:
                continue
            target_year = next(iter(native_tied))

        _correct_group_to(out, indexes, target_year)

    return out


__all__ = [
    "RECIFE_TZ", "REASON_MISSING_DATE", "REASON_WEEKDAY_MISMATCH",
    "REASON_YEAR_INFERRED", "REASON_DATE_RANGE", "DEFAULT_YEAR_ROLL_GRACE_DAYS",
    "INTERPRETATION_KIND_RELATIVE", "INTERPRETATION_KIND_DAY_MONTH",
    "INTERPRETATION_KIND_DAY_MONTH_YEAR", "INTERPRETATION_KIND_WEEKDAY",
    "INTERPRETATION_KIND_WEEKDAY_DAY", "INTERPRETATION_KIND_RANGE",
    "ResolvedDate", "resolve_event_datetime", "vote_on_sibling_years",
    "select_date_interpretation_for_reuse",
]
