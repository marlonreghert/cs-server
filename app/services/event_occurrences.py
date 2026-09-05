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
      [today, today + horizon_days] — both endpoints included, so
      `horizon_days=21` spans 22 calendar days. Each occurrence's id is
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
    today = _as_aware_utc(reference_time).astimezone(RECIFE_TZ).date()

    occurrences: list[Occurrence] = []
    for offset in range(0, horizon_days + 1):
        day: date = today + timedelta(days=offset)
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


__all__ = ["Occurrence", "expand_occurrences"]
