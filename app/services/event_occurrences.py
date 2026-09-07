"""Expand one `events.post_item` row into the concrete occurrences it
contributes to the events serving projection.

See plans/260905_events-serving-projection.md, Phase 3. Pure: no I/O, no
`datetime.now()` — the caller supplies `reference_time` (matching
app.services.event_date_resolver's own "this module is pure" discipline).

## Why this exists

A recurring announcement ("toda quinta") resolves, at EXTRACTION time, to
exactly ONE `starts_at` — the next occurrence of that weekday after the post
was seen (event_date_resolver.resolve_event_datetime). That single instant is
what makes a weekly forró night appear once in the admin console and then
vanish from any date-range read the moment it is in the past, until the venue
posts again. This module fixes that FOR SERVING ONLY, by re-deriving every
matching local day within a rolling horizon from the announcement's own
WEEKDAY PATTERN — never from its stale single `starts_at` date. The instant
that pattern comes from (`app.services.event_date_resolver.
weekdays_from_recurrence_text`) is the SAME one the resolver itself computes;
this module never re-parses recurrence prose.

## What is deliberately NOT re-validated here

By the time a row reaches this module it has already passed the projection's
SQL selection (status/venue/servability/promoter-visibility/recency) — see
RedisProjectionService.project_events. This module does not re-check any of
that; it answers exactly one question, "which occurrences does this row
contribute", assuming the row was already worth considering.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.services.event_date_resolver import RECIFE_TZ, weekdays_from_recurrence_text

# ── the frozen cross-repo nightlife cutoff ────────────────────────────────
# Before this Recife LOCAL hour, the nightlife "today" is still last night's
# calendar date: a party that started at 22:00 is still running at 00:30 and
# must still be in the serving index.
#
# THIS VALUE IS CROSS-REPO AND FROZEN. It must equal:
#   - vibes_bot `app/services/events_service.py::_EFFECTIVE_DAY_CUTOFF_HOUR`
#     (promoted by that repo's plan to
#     `Settings.EVENTS_NIGHTLIFE_CUTOFF_HOUR`, default 6), which decides the
#     SERVED window, and
#   - vibe_sense_mobile's `RECIFE_NIGHTLIFE_CUTOFF_HOUR`, which decides the
#     day LABELS the user reads.
#
# Change it in one repo only and the three desynchronise: cs-server stops
# projecting hours vibes_bot still queries, or keeps projecting hours mobile
# labels as yesterday. Changing it is a coordinated three-repo release, not
# a local edit — and deliberately NOT a setting and NOT an admin-config key,
# so no runtime flip can break the agreement.
NIGHTLIFE_CUTOFF_HOUR = 6


@dataclass(frozen=True)
class Occurrence:
    """One materialised occurrence's identity + timing — everything Phase 4
    needs to attach venue fields, `city_slug` and the rest of the contract
    payload to. `starts_at` is always UTC (the cross-repo contract's own
    requirement), regardless of what timezone the source row's `starts_at`
    carried."""
    occurrence_id: str
    event_id: str
    occurrence_date: str  # local (Recife) YYYY-MM-DD
    starts_at: datetime    # UTC, tz-aware


def _as_aware_utc(value: datetime) -> datetime:
    """Normalise a stored `starts_at` to a tz-aware UTC instant. Postgres
    (via psycopg) always returns a tz-aware `timestamptz`; a naive value
    (only ever seen from a test fixture or the in-memory fake) is treated as
    UTC, mirroring redis_projection_service._age_seconds's own convention."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def nightlife_date(reference_time: datetime) -> date:
    """The Recife nightlife date at `reference_time`: the local calendar
    date, rolled back one day while the local hour is strictly below
    `NIGHTLIFE_CUTOFF_HOUR`. Same rule, same direction and the same strict
    `<` boundary as vibes_bot's `_effective_local_date`, so the projected
    window and the served window agree at both 05:59 and 06:00.

    Exported so a future caller (or a test) never re-derives the rule — a
    second, subtly different copy of it is exactly how the cross-repo
    agreement above would rot."""
    local = _as_aware_utc(reference_time).astimezone(RECIFE_TZ)
    if local.hour < NIGHTLIFE_CUTOFF_HOUR:
        return (local - timedelta(days=1)).date()
    return local.date()


def _single_occurrence(event_id: str, starts_at: datetime) -> list[Occurrence]:
    """The one-occurrence shape shared by the non-recurring path and the
    recurring-with-no-computable-day fallback (Phase 3 §1: "a single
    occurrence at the resolved starts_at, exactly as today" — deliberately
    the IDENTICAL shape, since inventing days for prose this repo has
    refused to parse would be a fabrication)."""
    starts_at_utc = _as_aware_utc(starts_at)
    local_date = starts_at_utc.astimezone(RECIFE_TZ).date()
    return [
        Occurrence(
            occurrence_id=event_id,
            event_id=event_id,
            occurrence_date=local_date.isoformat(),
            starts_at=starts_at_utc,
        )
    ]


def expand_occurrences(
    row: dict,
    *,
    horizon_days: int,
    reference_time: datetime,
) -> list[Occurrence]:
    """Given one selected `events.post_item` row (a dict carrying at least
    `event_id`, `starts_at`, `is_recurring`, `recurrence_text`), return the
    occurrences it contributes.

    - `starts_at` is None: no occurrences — it cannot be placed on any day,
      so it cannot be served in a date-grouped list.
    - Not recurring: exactly one occurrence, id `event_id`, `starts_at`
      unchanged (converted to UTC).
    - Recurring with a resolvable weekday set: one occurrence per matching
      local (Recife) day in the CLOSED interval
      [nightlife_date(reference_time), local_calendar_date + horizon_days]
      — both endpoints included, so `horizon_days=21` spans 22 calendar
      days from 06:00 onward and 23 while the nightlife day is rolled back.
      The two edges move independently ON PURPOSE: the near edge follows
      the nightlife day so last night's 22:00 party is still generated at
      00:30, while the forward edge stays anchored to the calendar date so
      the horizon tail does not flap nightly. Each occurrence's id is
      `<event_id>_<YYYY-MM-DD>` (see the module-level separator rule in the
      plan's Data section — never `#`) and its `starts_at` is that local
      date at the announcement's OWN clock time (the stored `starts_at`'s
      time-of-day, read in Recife local time), converted back to UTC. The
      day pattern comes from `recurrence_text` alone — the stored
      `starts_at`'s own (possibly long-stale) DATE is discarded entirely,
      only its time-of-day survives. This is deliberate: it is exactly what
      lets a recurring announcement keep appearing on every one of its
      nights long after the post that announced it was last crawled.
    - Recurring with NO computable day ("toda semana", "sempre", or any
      recurrence prose this repo does not parse): the SAME single-occurrence
      shape as the non-recurring path. Inventing days here would be a
      fabrication this repo has explicitly refused elsewhere.
    """
    event_id = row["event_id"]
    starts_at = row.get("starts_at")
    if starts_at is None:
        return []

    is_recurring = bool(row.get("is_recurring"))
    if not is_recurring:
        return _single_occurrence(event_id, starts_at)

    weekdays = weekdays_from_recurrence_text(row.get("recurrence_text"))
    if not weekdays:
        return _single_occurrence(event_id, starts_at)

    starts_at_utc = _as_aware_utc(starts_at)
    clock = starts_at_utc.astimezone(RECIFE_TZ).timetz()
    # Two independent bounds, deliberately not one offset range:
    #  - the NEAR edge follows the nightlife day, so between 00:00 and 06:00
    #    local it rolls back to yesterday and last night's 22:00 party is
    #    still generated (and therefore still survives the projector's
    #    prune-what-is-not-fresh pass);
    #  - the FORWARD edge stays anchored to the CALENDAR date. Rolling it
    #    back too would delete and re-create every horizon-edge occurrence
    #    once a night, flapping the tail of the served window for six hours
    #    and churning payload keys for no benefit.
    # Net effect: at most one extra expanded day per recurring event, and
    # only while the local clock is before the cutoff.
    start_day = nightlife_date(reference_time)
    end_day = _as_aware_utc(reference_time).astimezone(RECIFE_TZ).date() + timedelta(
        days=horizon_days
    )

    occurrences: list[Occurrence] = []
    for offset in range((end_day - start_day).days + 1):
        day: date = start_day + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        local_dt = datetime.combine(day, clock, tzinfo=RECIFE_TZ)
        occurrences.append(
            Occurrence(
                occurrence_id=f"{event_id}_{day.isoformat()}",
                event_id=event_id,
                occurrence_date=day.isoformat(),
                starts_at=local_dt.astimezone(timezone.utc),
            )
        )
    return occurrences


__all__ = [
    "NIGHTLIFE_CUTOFF_HOUR",
    "Occurrence",
    "expand_occurrences",
    "nightlife_date",
]
