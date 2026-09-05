"""The events serving projection's row-level selection predicate, as a pure
function. plans/260905_events-serving-projection.md, Phase 4 §3 pytest list:
"Projection selection predicate, as a pure function over rows."

Deliberately narrower than the full projection selection: this predicate
covers only what a single `events.post_item` row can answer about ITSELF —
`post_type`, `status`, `venue_id` presence, `superseded_by`, and the
recency window. It does NOT cover venue servability (external: `serving.
eligible_venue`) or promoter-source visibility (external: a grouped read of
every attached `events.post_item_source` row) — those need data beyond one
row and stay in RedisProjectionService.project_events. `last_seen_at` (the
recurring-event source-freshness bound below) IS treated as row-local here,
for the same reason `venue_name` already is even though it too comes from a
join: `_EVENT_SELECT`'s own `agg` LEFT JOIN LATERAL (the fake's
`_merged_view`) puts it on the row BEFORE this predicate ever runs, so no
second, per-row fetch is needed. Both stores'
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

# Bounds a RECURRING row's selectability by SOURCE FRESHNESS (plans/260905_
# events-serving-projection.md's phantom-recurring-event fix) — see
# is_selectable's own docstring for the full rule. Mirrors
# `events_recurring_max_source_age_days` in app/config.py as an INDEPENDENT
# literal, the same way event_date_resolver.DEFAULT_YEAR_ROLL_GRACE_DAYS
# mirrors its own externally-configured value: this module stays pure/no I/O
# and never reads app.config itself, so this is only the default for a
# caller that does not thread the live setting through (e.g. a test that
# does not care about the bound). Every production caller (both stores'
# list_events_for_projection) always computes and passes the live setting
# explicitly — see that method's own docstring.
DEFAULT_RECURRING_MAX_SOURCE_AGE = timedelta(days=45)


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_selectable(
    row: dict, *, now: datetime,
    max_recurring_source_age: timedelta = DEFAULT_RECURRING_MAX_SOURCE_AGE,
) -> bool:
    """True when `row` (an `events.post_item` row, in the same shape
    `RdsVenueStore._EVENT_SELECT`/the fake's `_merged_view` produce) passes
    every ROW-LOCAL projection criterion:

    - `post_type == "event"` (never a menu/promotion/food/other row).
    - `status` is `accepted` or `confirmed` (never pending_review/rejected/
      extraction_failed/superseded-by-status).
    - `venue_id` is present (an unresolved promoter event is never served).
    - `superseded_by` is None (a row absorbed by a cross-post merge is never
      served under its own, now-defunct identity).
    - Temporal: a RECURRING row ignores its OWN (possibly long-stale)
      `starts_at` — see app.services.event_occurrences' own module
      docstring for why: its concrete days are re-derived from "today"
      forward, from the weekday PATTERN, never from this row's single
      stored date. That must NOT mean forever, though: a recurring row is
      bounded instead by SOURCE FRESHNESS — `row["last_seen_at"]` (the MAX
      `last_seen_at` across every attached `post_item_source` row) must be
      at or after `now - max_recurring_source_age`, inclusive at the
      boundary (seen EXACTLY on the boundary is still fresh — mirrors
      PAST_GRACE's own boundary convention below).
      `last_seen_at is None` — an event with NO source rows at all; should
      not occur in practice (every event is created FROM a source — see
      `RdsVenueStore.insert_event`/the fake's identical pairing) but never
      assumed — is treated as NOT selectable: there is no evidence this
      announcement has EVER been (re)confirmed, so nothing here can be
      vouched for as fresh either. This mirrors
      `app.models.menu_lifecycle.is_menu_item_current`'s identical
      NULL-handling for the exact same shape of question ("is this
      still-live claim backed by a recent-enough sighting?"), and the same
      "an absent signal is never assumed to mean yes" posture this repo
      takes throughout event_reconciliation.py's own field-merge rules.
      A NON-recurring row with no `starts_at` at all is still selectable
      here (it will simply expand to zero occurrences —
      app.services.event_occurrences already handles that, and there is no
      reason to duplicate the check here); a NON-recurring row with a
      concrete `starts_at` must be at or after `now - PAST_GRACE`, EXACTLY
      as before this change — the new bound applies ONLY to recurring rows.
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

    if is_recurring:
        last_seen_at: Optional[datetime] = row.get("last_seen_at")
        if last_seen_at is None:
            return False
        return _as_aware(last_seen_at) >= _as_aware(now) - max_recurring_source_age

    if starts_at is None:
        return True
    return _as_aware(starts_at) >= _as_aware(now) - PAST_GRACE


__all__ = [
    "is_selectable", "SELECTABLE_STATUSES", "PAST_GRACE",
    "DEFAULT_RECURRING_MAX_SOURCE_AGE",
]
