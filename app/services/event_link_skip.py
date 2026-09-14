"""The skip table: which event rows an automated venue-link pass must never
touch, and never use as evidence.

Promoted verbatim out of `scripts/backfill_event_venue_links.py`
(plans/260914_agentic-venue-resolution-fallback.md §1) because that module's
own docstring already called `_skip_reason` *"the ONE place this check is
expressed"* — and this plan adds a fourth consumer
(`app.services.venue_link_audit_reviewer`), which would have made it a
fourth copy.

## Nothing here changed in the move

Same six checks, in the same order, with the same return values. The script
re-exports these names, so `decide_one`, `decide_one_disputed` and
`decide_one_force_reassign` keep importing the identical objects and their
behaviour is byte-for-byte what it was at `408da88`. The order is
load-bearing and must not be "tidied": an operator's own prior action always
wins, unconditionally, regardless of which mode or selection reached the row
— `confirmed` outranks `manual_link` outranks the status checks outranks the
per-field `operator_edited_fields` guard.

## Why a service module and not a shared script helper

`scripts/` is not importable from `app/` by this repository's layering, and
the reviewer pass is a service. Moving the definition down into
`app/services/` and re-exporting upward is the only direction that keeps one
definition; the reverse (a service importing a script) would invert the
dependency.
"""
from __future__ import annotations

from typing import Optional

from app.services.event_reconciliation import STATUS_CONFIRMED, STATUS_SUPERSEDED
from app.services.event_venue_resolution import RESOLUTION_MANUAL

# Skip reasons — the per-status/per-protection table.
SKIP_CONFIRMED = "confirmed"
SKIP_MANUAL_LINK = "manual_link"
SKIP_SUPERSEDED = "superseded"
SKIP_EXTRACTION_FAILED = "extraction_failed"
SKIP_REJECTED = "rejected"
SKIP_OPERATOR_EDITED_VENUE = "operator_edited_venue"

# Statuses not named by an event_reconciliation constant — the same bare
# literals `ALL_STATUSES` itself uses.
_STATUS_EXTRACTION_FAILED = "extraction_failed"
_STATUS_REJECTED = "rejected"


def skip_reason(event: dict) -> Optional[str]:
    """`None` when an automated pass may act on / read this row; otherwise
    the reason constant naming the protection that applies.

    Order is deliberate and must not be reordered — see the module
    docstring. An operator's own prior action always wins, unconditionally,
    regardless of which mode or selection reached this row."""
    status = event.get("status")
    edited = event.get("operator_edited_fields") or []
    if status == STATUS_CONFIRMED:
        return SKIP_CONFIRMED
    if event.get("location_resolution") == RESOLUTION_MANUAL:
        return SKIP_MANUAL_LINK
    if status == STATUS_SUPERSEDED:
        return SKIP_SUPERSEDED
    if status == _STATUS_EXTRACTION_FAILED:
        return SKIP_EXTRACTION_FAILED
    if status == _STATUS_REJECTED:
        return SKIP_REJECTED
    if "venue_id" in edited:
        return SKIP_OPERATOR_EDITED_VENUE
    return None


__all__ = [
    "SKIP_CONFIRMED", "SKIP_MANUAL_LINK", "SKIP_SUPERSEDED",
    "SKIP_EXTRACTION_FAILED", "SKIP_REJECTED", "SKIP_OPERATOR_EDITED_VENUE",
    "skip_reason",
]
