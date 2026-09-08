"""Resolve the `ends_at` the events serving projection attaches to ONE
occurrence.

plans/260907_events-non-event-and-recurrence-normalisation.md §3a (defect
C3). Pure: no I/O, no config, no clock — a sibling of
`app.services.event_ticket_url` and `app.services.event_recurrence_text`,
so the six-way table below is unit-testable as a pure-input matrix rather
than only through the projector.

## The defect

The projection used to copy the source row's single stored absolute
`ends_at` onto EVERY occurrence it expanded, while `starts_at` came from the
re-derived `Occurrence`. For a recurring row whose days are re-derived from
the weekday PATTERN (`app.services.event_occurrences`), the stored `ends_at`
stays pinned to the announcement's original date. Measured on 2026-09-07:
15 of 44 live occurrences carried `ends_at`, and **15 of 15 ended strictly
before their own `starts_at`** — up to 54 days before it.

## The discriminator is DERIVATION, never `is_recurring`

`event_occurrences.expand_occurrences` sends a row with `is_recurring: true`
whose `recurrence_text` yields no weekday set to `_single_occurrence` — ONE
occurrence whose `starts_at` IS the stored `starts_at` and whose
`occurrence_id` is the bare `event_id`. For that occurrence the stored
`ends_at` is simply correct, and keying on `is_recurring` would null a
genuine multi-day interval ("de quinta a domingo") — exactly the deletion
vibes_bot's own 1.4.1 plan refuses to make at serve time. So the test is
`occ.occurrence_id != occ.event_id`, read off the `Occurrence` the projector
already has in hand. (`occ.starts_at != row["starts_at"]` is equivalent but
weaker: it depends on tz-awareness normalisation and would misread a
re-derived occurrence that happens to land on the stored date.)

| branch | condition | projected `ends_at` | outcome |
|---|---|---|---|
| any | `stored_end` is None/absent | `None` | `absent` |
| carried | `stored_end < stored_start` | `None` | `dropped_inverted` |
| carried | anything else, **any length** | `stored_end` **verbatim** | `carried` |
| derived | `duration < 0` | `None` | `dropped_inverted` |
| derived | `duration == 0` | `None` | `dropped_zero` |
| derived | `0 < duration <= MAX_DERIVED_DURATION` | `occurrence_start + duration` | `derived` |
| derived | `duration > MAX_DERIVED_DURATION` | `None` | `dropped_implausible` |

**The carried branch has NO length bound at all**, on purpose: there the
occurrence IS the stored announcement, so its stored end is the end of that
exact interval and a three-day festival's end is real data, not a stale
instant. The bound belongs only where the stored end's DATE has been
discarded — the derived branch — and there it is not a plausibility
heuristic about events but a statement about what survives the discard: a
>24h "duration" on a weekly night is indistinguishable from the
announcement's own stale absolute end, which is the defect this module
exists to fix.

`dropped_zero` is deliberately NOT folded into `dropped_inverted`: a
zero-length interval is not an inversion, folding them makes the counter
unreadable as the data-defect signal it exists to be, and the two branches
treat zero differently — it is CARRIED on the carried branch (matching
vibes_bot, whose serve-time guard permits equality) and DROPPED on the
derived branch, where deriving it would produce an end equal to the start it
came from, which tells a reader nothing.

Projecting `None` rather than a wrong instant is the same posture
`time_known: false` already takes for a defaulted midnight: suppress a
misleading value rather than serve it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

# The longest duration worth carrying onto a RE-DERIVED occurrence. Frozen
# module constant, never a setting — the same posture as
# `event_occurrences.NIGHTLIFE_CUTOFF_HOUR` and
# `event_projection_selection.PAST_GRACE`, and for the same reason: a
# runtime flip would silently change what the projection means.
MAX_DERIVED_DURATION = timedelta(hours=24)

# Outcome labels for `events_projection_ends_at_total{outcome}`.
OUTCOME_ABSENT = "absent"
OUTCOME_CARRIED = "carried"
OUTCOME_DERIVED = "derived"
OUTCOME_DROPPED_IMPLAUSIBLE = "dropped_implausible"
OUTCOME_DROPPED_INVERTED = "dropped_inverted"
OUTCOME_DROPPED_ZERO = "dropped_zero"


def _as_aware_utc(value: datetime) -> datetime:
    """Mirrors `event_occurrences._as_aware_utc`: Postgres always returns a
    tz-aware `timestamptz`; a naive value (only ever seen from a fixture or
    the in-memory fake) is treated as UTC. Used ONLY for comparison and
    arithmetic — a carried `stored_end` is handed back exactly as it came
    in, so this module never changes what the projection already served on
    that branch."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_occurrence_end(
    stored_start: Optional[datetime],
    stored_end: Optional[datetime],
    occurrence_start: Optional[datetime],
    *,
    derived: bool,
) -> tuple[Optional[datetime], str]:
    """`(ends_at_or_None, outcome_label)` for ONE occurrence.

    `derived` is `occ.occurrence_id != occ.event_id` — whether this
    occurrence's `starts_at` was re-derived from a weekday pattern (and so
    the stored end's DATE was thrown away) or is the announcement's own
    stored instant.

    A null `stored_start` is UNREACHABLE from the projector
    (`event_occurrences.expand_occurrences` returns no occurrences at all
    when `starts_at` is None, so such a row never enters the payload loop);
    it answers `absent` rather than raising because a pure function must be
    total, not because that outcome is expected to be observed.
    """
    if stored_end is None or stored_start is None:
        return None, OUTCOME_ABSENT

    start = _as_aware_utc(stored_start)
    end = _as_aware_utc(stored_end)

    if not derived:
        if end < start:
            return None, OUTCOME_DROPPED_INVERTED
        # Any length, including zero: on this branch the occurrence IS the
        # stored announcement. Returned byte-for-byte as stored.
        return stored_end, OUTCOME_CARRIED

    duration = end - start
    if duration < timedelta(0):
        return None, OUTCOME_DROPPED_INVERTED
    if duration == timedelta(0):
        return None, OUTCOME_DROPPED_ZERO
    if duration > MAX_DERIVED_DURATION:
        return None, OUTCOME_DROPPED_IMPLAUSIBLE
    if occurrence_start is None:  # pragma: no cover - unreachable, see above
        return None, OUTCOME_ABSENT
    return _as_aware_utc(occurrence_start) + duration, OUTCOME_DERIVED


__all__ = [
    "MAX_DERIVED_DURATION",
    "OUTCOME_ABSENT",
    "OUTCOME_CARRIED",
    "OUTCOME_DERIVED",
    "OUTCOME_DROPPED_IMPLAUSIBLE",
    "OUTCOME_DROPPED_INVERTED",
    "OUTCOME_DROPPED_ZERO",
    "resolve_occurrence_end",
]
