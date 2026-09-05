"""The events serving projection's row-level selection predicate, as a pure
function. plans/260905_events-serving-projection.md, Phase 4 §3 pytest list:
"Projection selection predicate, as a pure function over rows."

Deliberately narrower than the full projection selection: this predicate
covers only what a single `events.post_item` row can answer about ITSELF —
`post_type`, `status`, `venue_id` presence, `superseded_by`, and the
recency window. It does NOT cover venue servability (external: `serving.
eligible_venue`) or promoter-source visibility (external: a grouped read of
every attached `events.post_item_source` row) — those need data beyond one
row and stay in RedisProjectionService.project_events. Both stores'
`list_events_for_projection` implement the IDENTICAL logic this function
specifies: the real store's SQL WHERE clause is a direct transliteration of
it, and the fake store calls this function directly, so a BDD scenario
passing against the fake is evidence the real SQL should implement the same
rule — the two are kept in lockstep by hand rather than a shared query
representation, matching how `venue_eligibility.evaluate()` and the real
`serving.eligible_venue` view are already kept in lockstep (parity pinned by
a dedicated test once Postgres is available; there is no local Postgres in
CI/dev otherwise).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

SELECTABLE_STATUSES = ("accepted", "confirmed")

# A night that started yesterday evening is still tonight's event to a user
# at 01:00 — see the plan's own Evidence/Phase 4 §3 wording for this exact
# grace window.
PAST_GRACE = timedelta(days=1)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_selectable(row: dict, *, now: datetime) -> bool:
    """True when `row` (an `events.post_item` row, in the same shape
    `RdsVenueStore._EVENT_SELECT`/the fake's `_merged_view` produce) passes
    every ROW-LOCAL projection criterion:

    - `post_type == "event"` (never a menu/promotion/food/other row).
    - `status` is `accepted` or `confirmed` (never pending_review/rejected/
      extraction_failed/superseded-by-status).
    - `venue_id` is present (an unresolved promoter event is never served).
    - `superseded_by` is None (a row absorbed by a cross-post merge is never
      served under its own, now-defunct identity).
    - Temporal: a RECURRING row is ALWAYS selectable regardless of its own
      (possibly long-stale) `starts_at` — see
      app.services.event_occurrences' own module docstring for why: its
      concrete days are re-derived from "today" forward, from the weekday
      PATTERN, never from this row's single stored date. A row with no
      `starts_at` at all is also selectable here (it will simply expand to
      zero occurrences — app.services.event_occurrences already handles
      that, and there is no reason to duplicate the check here). A
      NON-recurring row with a concrete `starts_at` must be at or after
      `now - PAST_GRACE`.
    """
    if row.get("post_type") != "event":
        return False
    if row.get("status") not in SELECTABLE_STATUSES:
        return False
    if not row.get("venue_id"):
        return False
    if row.get("superseded_by") is not None:
        return False

    starts_at: Optional[datetime] = row.get("starts_at")
    is_recurring = bool(row.get("is_recurring"))
    if is_recurring or starts_at is None:
        return True
    return _as_aware(starts_at) >= _as_aware(now) - PAST_GRACE


__all__ = ["is_selectable", "SELECTABLE_STATUSES", "PAST_GRACE"]
