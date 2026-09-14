"""Operator CLI: repair items whose venue came from a post-level caption
mention instead of the event's own stated location.

See plans/260812_backfill-misattributed-links.md.

Every stored `events.post_item` row with `linked_by = 'handle_mention'` is
re-decided by re-running the production resolution ladder
(`app.services.event_venue_resolution.resolve_event_venue`) against the
event's OWN stored `post_item_source.raw_extraction["location_text"]` — the
same frozen text the model originally extracted for THIS event, never a
sibling's, never a fresh model call, never an S3 read. An operator
correction recorded in `operator_edited_fields` (on `location_text` or
`venue_id`) always wins — see `_location_text_input` and `decide_one` below.

Touches ONLY the four link columns, `review_reason`, and `status` — never
`title`/`starts_at`/`location_text` (except reading it), so
`app.services.event_identity.compute_source_event_key` cannot move and no
row is ever orphaned or duplicated by this script. No Apify, S3, or OpenAI
call is possible: nothing in this module imports a network client.

Re-attribution can change `venue_id`, which changes
`app.services.event_merge.compute_event_identity` — two repaired rows can
collide. This script NEVER calls `merge_touched_events`; a collision is
written anyway (the correct venue beats a known-wrong one) and reported, left
for the next real reconciliation of those posts to merge correctly.

Dry-run by default, `--apply` to write. Idempotent (a second `--apply`
changes nothing) and resumable (`--since-id`). Exits non-zero, before any
row changes if the per-event attribution fix has not landed, and after
writing if the outcome arithmetic does not balance or an UPDATE ever
reports zero rows affected.

## Two selection modes (plans/260912_events-venue-night-duplication.md §C)

`--mode handle-mention` (the DEFAULT, and byte-for-byte the behaviour this
script shipped with) selects `linked_by = 'handle_mention'` rows and
re-decides them through the whole ladder, as described above.

`--mode disputed-location-text` is the HISTORICAL half of 260912's Defect 2:
it selects rows attributed by the fixed VENUE-POST path (a venue's own post,
so `linked_by IS NULL` — that path writes no link columns at all) whose own
stored `location_text` triggers `app.services.event_attribution_dispute.
evaluate_attribution_dispute`, and repairs them:
  - a dispute naming a DIFFERENT catalog venue REPOINTS the row to it
    (`linked_by` records which rung said so);
  - a dispute naming a place we do not carry (`venue_not_in_catalog`) is
    FLAGGED with `location_text_disputes_venue` and keeps its venue —
    there is nowhere to move it to, and detaching it would withdraw a row
    an operator has not been asked about.
This mode is a second SELECTION, never a second implementation: the dispute
rule is imported, so the sweep and the live pipeline can never disagree
about a row. It keeps every property the default mode has — dry-run
default, `--apply` to write, idempotent, resumable, no network client
importable, `operator_edited_fields` always wins — and, critically, it
still NEVER calls `merge_touched_events`: repaired rows are merged by the
dedup sweep in 260912 §F, as a separate, separately-guarded step.

`--withhold-disputed` (off by default) lets a flagged row's new review
reason withhold auto-accept. Off, the reason is recorded and the row's
status is computed as if it were not there — the SAME default the live
pipeline's `event_attribution_dispute_withhold_enabled` ships with, so this
repair cannot quietly withdraw content from serving.

## Two MORE modes (plans/260914_recife-venue-mapping-corrections.md) —
## an operator-pinned write, and a fair re-run for never-resolved rows

`--mode force-reassign` is an unconditional, operator-pinned `venue_id`
write, never a ladder re-run — the shape needed when a handle's
`instagram.handle` mapping itself was wrong (a spurious duplicate venue, or
a wrong force-assigned single venue) and the already-written events must be
corrected to match the fixed mapping. Takes `--handle <h>` (exactly one),
`--from-venue-id <id|none>`, `--to-venue-id <id|none>` — selection is
`source_handle == handle AND linked_by IS NULL AND venue_id == (from_venue_id
or NULL)`. `to-venue-id none` DETACHES (clears `venue_id`/
`location_resolution` to `NULL`, folds `unresolved_venue` INTO
`review_reason`); any other `--to-venue-id` ASSIGNS (`location_resolution =
RESOLUTION_MANUAL`, folds `unresolved_venue`/`venue_not_in_catalog` OUT of
`review_reason`). Neither branch writes `linked_by` — this is not a ladder
verdict, so there is no new link method to record. The SAME skip table
every mode applies protects an operator's own prior correction even from
this operator-pinned script run. `status` is recomputed through the SAME
`is_clean_extraction`-based tail every mode uses, never asserted.

`--mode unresolved-venue` reuses `decide_one` — and therefore
`resolve_event_venue`/`gate_auto_link` — completely UNCHANGED; only the
SELECTION is new: `venue_id IS NULL`, optionally scoped by one or more
repeated `--handle <h>` (catalog-wide when omitted). The intended use is a
second pass run AFTER a `force-reassign` detach, so the now-unresolved rows
get the identical fair shot at auto-resolution the live pipeline already
gives every future post.

Both new modes keep every property the two selections above already have:
dry-run default, `--apply` to write, idempotent (selection itself excludes
an already-corrected row on a second run), resumable, no network client
importable, `operator_edited_fields` always wins, and the same
`ArithmeticImbalance`/`WriteAffectedNoRows` hard-stops.

## A FIFTH mode (plans/260914_promoter-roundup-caption-mention.md) — the
## roundup-caption repair

`--mode caption-handle-mention` selects `linked_by = 'caption_handle_mention'`
rows — exactly the shape the OLD, pre-fix rung 5 produced: a roundup post
whose caption happened to name exactly one catalogued venue, auto-linked
to it even though the post's own sibling events named several different
places. Reuses `decide_one` completely UNCHANGED, the same engine
`handle-mention`/`unresolved-venue` already use: that function's own
`resolve_event_venue` call passes `caption=None` (re-deciding strictly from
the event's OWN stored `location_text`, never a fresh model call and never
the post's caption), so rung 5 can never fire in this engine regardless of
today's fix — no sibling-location-text plumbing is needed here. Keeps every
property the other modes have: dry-run default, `--apply` to write,
idempotent, resumable, no network client importable,
`operator_edited_fields` always wins.

Usage:
    python -m scripts.backfill_event_venue_links                        # dry-run: report only
    python -m scripts.backfill_event_venue_links --apply                # write the repaired links
    python -m scripts.backfill_event_venue_links --apply --since-id X   # resume after event_id X
    python -m scripts.backfill_event_venue_links --mode disputed-location-text
    python -m scripts.backfill_event_venue_links --mode disputed-location-text --apply
    python -m scripts.backfill_event_venue_links --mode force-reassign \\
        --handle real.botequim --from-venue-id none --to-venue-id ven_... --apply
    python -m scripts.backfill_event_venue_links --mode force-reassign \\
        --handle editaisculturape --from-venue-id ven_... --to-venue-id none --apply
    python -m scripts.backfill_event_venue_links --mode unresolved-venue \\
        --handle editaisculturape --apply
    python -m scripts.backfill_event_venue_links --mode caption-handle-mention
    python -m scripts.backfill_event_venue_links --mode caption-handle-mention --apply

Capture the dry-run report to a file BEFORE running --apply — it is the
only record of every changed row's previous venue_id. There is no revert
for a row once --apply has written it.
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.services import event_link_skip as _skip
from app.models.event_kind import KIND_EVENT
from app.services.event_merge import (
    _fold_review_reason,  # plan §C: "import it" — see _fold_no_venue_reason below for the one extension it needs.
    compute_event_identity,
    compute_handle_identity,
)
from app.services.event_reconciliation import (
    REVIEW_REASON_NEEDS_REVIEW,
    REVIEW_REASON_UNRESOLVED_VENUE,
    REVIEW_REASON_VENUE_NOT_IN_CATALOG,
    STATUS_ACCEPTED,
    STATUS_CONFIRMED,
    STATUS_PENDING_REVIEW,
    STATUS_SUPERSEDED,
    is_clean_extraction,
)
from app.services.event_venue_resolution import (
    METHOD_CAPTION_HANDLE_MENTION,
    METHOD_HANDLE_MENTION,
    METHOD_VENUE_NOT_IN_CATALOG,
    RESOLUTION_AUTO,
    RESOLUTION_MANUAL,
    RESOLUTION_UNRESOLVED,
    build_handle_index,
    build_venue_catalog,
    extract_mentions,
    resolve_event_venue,
)
from app.services.event_attribution_dispute import (
    REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE,
    evaluate_attribution_dispute,
    fold_review_reason,
)
from app.services.instagram_handle_sources import normalize_handle

logger = logging.getLogger("backfill_event_venue_links")

# plans/260912_events-venue-night-duplication.md §C: two SELECTIONS, one
# repair engine. See this module's docstring.
MODE_HANDLE_MENTION = "handle-mention"
MODE_DISPUTED_LOCATION_TEXT = "disputed-location-text"
# plans/260914_recife-venue-mapping-corrections.md: two MORE selections,
# alongside (never replacing) the two above. See this module's docstring.
MODE_FORCE_REASSIGN = "force-reassign"
MODE_UNRESOLVED_VENUE = "unresolved-venue"
# plans/260914_promoter-roundup-caption-mention.md: a FIFTH selection, same
# repair engine as `handle-mention`/`unresolved-venue` (`decide_one`,
# completely unchanged — see this module's docstring for why its own
# `caption=None` call can never even reach rung 5). Selects rows the OLD,
# pre-fix rung 5 wrongly auto-linked from a roundup post's caption.
MODE_CAPTION_HANDLE_MENTION = "caption-handle-mention"
MODES = (
    MODE_HANDLE_MENTION, MODE_DISPUTED_LOCATION_TEXT, MODE_FORCE_REASSIGN,
    MODE_UNRESOLVED_VENUE, MODE_CAPTION_HANDLE_MENTION,
)

# Skip reasons — plan §B's per-status/per-protection table. Re-exported from
# `app.services.event_link_skip`, which is now the ONE definition
# (plans/260914_agentic-venue-resolution-fallback.md §1 moved it there so the
# reviewer pass could share it instead of becoming a fourth copy). These
# names stay importable from this module unchanged: `decide_one`,
# `decide_one_disputed` and `decide_one_force_reassign` below, and every
# existing test, keep referring to the identical objects.
SKIP_CONFIRMED = _skip.SKIP_CONFIRMED
SKIP_MANUAL_LINK = _skip.SKIP_MANUAL_LINK
SKIP_SUPERSEDED = _skip.SKIP_SUPERSEDED
SKIP_EXTRACTION_FAILED = _skip.SKIP_EXTRACTION_FAILED
SKIP_REJECTED = _skip.SKIP_REJECTED
SKIP_OPERATOR_EDITED_VENUE = _skip.SKIP_OPERATOR_EDITED_VENUE


class BackfillError(Exception):
    """Base for every hard-stop this script can raise. `report`, when set,
    is whatever partial `Report` had been built before the failure — the
    CLI prints it before exiting non-zero, so an operator still sees exactly
    how far the run got."""

    def __init__(self, message: str, *, report: "Optional[Report]" = None):
        super().__init__(message)
        self.report = report


class DependencyNotLanded(BackfillError):
    """Raised before a single row is read — plan §A's mechanical guard."""


class ArithmeticImbalance(BackfillError):
    """`selected != repointed + detached + unchanged + sum(skipped)`."""


class WriteAffectedNoRows(BackfillError):
    def __init__(self, event_id: str, *, report: "Optional[Report]" = None):
        super().__init__(f"UPDATE affected zero rows for event_id={event_id}", report=report)
        self.event_id = event_id


def _assert_forward_fix_landed() -> None:
    """plan §A: "the script imports the per-event link-method value §A
    introduces ... and aborts before reading a single row if that symbol
    does not exist." Checked dynamically (an attribute lookup at CALL time,
    not a bare module-level `from ... import`) so a test can simulate "the
    fix has not landed" by deleting the attribute from the already-imported
    module, without needing a second process or a real old checkout.
    `METHOD_CAPTION_HANDLE_MENTION` is the landed name — the split rung-1
    (event's own text) vs rung-5 (post caption, demoted) method value
    plans/260812_event-attribution-and-dates.md §A introduced. Without it,
    the ladder's rung 1 still reads from the CAPTION (the pre-fix ordering)
    and this script's re-resolution would produce a THIRD answer, different
    from both what is stored and what the fixed pipeline produces — see this
    module's own plan for why that is worse than refusing to run at all.
    """
    import app.services.event_venue_resolution as _evr

    if not hasattr(_evr, "METHOD_CAPTION_HANDLE_MENTION"):
        raise DependencyNotLanded(
            "app.services.event_venue_resolution.METHOD_CAPTION_HANDLE_MENTION "
            "is missing — the per-event venue attribution fix "
            "(plans/260812_event-attribution-and-dates.md §A) has not landed "
            "in this build. Refusing to run before reading any item."
        )


def _location_text_input(event: dict) -> Optional[str]:
    """plan §B: "A row edited on location_text is not skipped — an operator
    who corrected the location text improved the input this script reads."
    An operator's correction lives on the row's own (event-level)
    `location_text` column (where a PATCH writes); everywhere else, the
    FROZEN model answer on the primary source's `raw_extraction` is the
    input — never a fresh model call, never a different source's text.
    """
    edited = event.get("operator_edited_fields") or []
    if "location_text" in edited:
        return event.get("location_text")
    raw = event.get("raw_extraction")
    if isinstance(raw, dict):
        return raw.get("location_text")
    return None


def _first_unrecognized_handle(
    location_text: Optional[str], promoter_handle: Optional[str], handle_index: dict,
) -> Optional[str]:
    """The first `@`-mention in `location_text` that is not the promoter's
    own handle and does not resolve against `handle_index` — the same
    "unrecognized handle" `event_venue_resolution._known_venue_mentions`
    computes internally for `METHOD_VENUE_NOT_IN_CATALOG`, re-derived here
    from PUBLIC API only (`extract_mentions`) since `resolve_event_venue`'s
    `ResolutionResult` does not carry the handle string itself — only the
    method sentinel. Used solely to build the venue-acquisition backlog
    (plan Error Handling And Observability)."""
    own = normalize_handle(promoter_handle) if promoter_handle else None
    for mention in extract_mentions(location_text):
        if own is not None and mention == own:
            continue
        if mention not in handle_index:
            return mention
    return None


def _fold_no_venue_reason(existing_reason: Optional[str], no_venue_reason: Optional[str]) -> Optional[str]:
    """plan §C: "only the unresolved_venue / venue_not_in_catalog tokens may
    be dropped ... and only when the row actually gained a venue.
    event_merge._fold_review_reason already implements exactly this
    token-wise drop-and-preserve; import it." True for `unresolved_venue` —
    `_fold_review_reason` drops exactly that token. It predates
    `venue_not_in_catalog` (plans/260813_handle-attribution-hardening.md)
    and was never extended to drop that SECOND token, so this wraps it with
    one extra explicit filter rather than duplicating the whole fold — see
    the PR notes' Deviations section for why this one line is not a literal
    import of the cited function alone.

    `existing_reason` is the row's CURRENT `"; "`-joined review_reason
    (e.g. "missing_date", or None); `no_venue_reason` is the fresh
    resolution's own reason for having no venue (unresolved_venue /
    venue_not_in_catalog), or None when a venue WAS found. Any OTHER
    reason (missing_date, year_inferred, ...) survives untouched, in
    first-seen order, never duplicated.
    """
    folded = _fold_review_reason(existing_reason, None)
    tokens = [
        token for token in (folded or "").split("; ")
        if token and token != REVIEW_REASON_VENUE_NOT_IN_CATALOG
    ]
    if no_venue_reason and no_venue_reason not in tokens:
        tokens.append(no_venue_reason)
    return "; ".join(tokens) if tokens else None


# The skip table itself now lives in `app.services.event_link_skip` — the
# ONE definition, shared with `app.services.venue_link_audit_reviewer`
# (plans/260914_agentic-venue-resolution-fallback.md §1). This name stays
# bound here so `decide_one`, `decide_one_disputed` and
# `decide_one_force_reassign` below call the identical function they always
# did; nothing about its six checks or their order changed in the move.
_skip_reason = _skip.skip_reason


def _skip_decision(event: dict, reason: str) -> "Decision":
    """The `action="skip"` `Decision` shape for a row `_skip_reason` already
    flagged — shared for the same reason `_skip_reason` itself is."""
    return Decision(
        event_id=event["event_id"], action="skip", skip_reason=reason,
        old_venue_id=event.get("venue_id"), old_status=event.get("status"),
        old_linked_by=event.get("linked_by"),
        old_location_resolution=event.get("location_resolution"),
        venue_name_before=event.get("venue_name"),
    )


def _restore_status(
    event: dict, *, gate_reason: Optional[str], new_venue_id: Optional[str],
    new_review_reason: Optional[str], min_confidence: float,
) -> tuple[str, Optional[str]]:
    """plan §C's status-restoration tail, shared by every decision function
    (plans/260914_recife-venue-mapping-corrections.md's Refactor note):
    recompute `status` through `is_clean_extraction` — never asserted — then
    apply the SAME `needs_review` fallback `event_reconciliation.
    reconcile_post_events` itself falls back to when a queued row would
    otherwise carry a null reason.

    `gate_reason` is the review reason FED TO THE GATE: equal to
    `new_review_reason` for `decide_one`/`decide_one_force_reassign`, but a
    withholding-adjusted variant for `decide_one_disputed` (which still
    PERSISTS the unadjusted `new_review_reason` — only the gate input
    differs, never what gets written).
    """
    clean = is_clean_extraction(
        review_reason=gate_reason, starts_at=event.get("starts_at"),
        venue_id=new_venue_id, confidence=event.get("confidence"),
        min_confidence=min_confidence, post_type=event.get("post_type") or KIND_EVENT,
    )
    new_status = STATUS_ACCEPTED if clean else STATUS_PENDING_REVIEW
    if new_status == STATUS_PENDING_REVIEW and not new_review_reason:
        # Mirrors event_reconciliation.reconcile_post_events' own residual
        # fallback — a queued row must never carry a null reason.
        new_review_reason = REVIEW_REASON_NEEDS_REVIEW
    return new_status, new_review_reason


@dataclass
class Decision:
    """The pure verdict for one candidate row — computed without touching
    the DAO, so it is unit-testable on a bare dict and replayable identically
    in dry-run and --apply modes."""

    event_id: str
    action: str  # "skip" | "unchanged" | "repoint" | "detach"
    skip_reason: Optional[str] = None
    old_venue_id: Optional[str] = None
    new_venue_id: Optional[str] = None
    old_linked_by: Optional[str] = None
    new_linked_by: Optional[str] = None
    old_location_resolution: Optional[str] = None
    new_location_resolution: Optional[str] = None
    old_location_confidence: Optional[float] = None
    new_location_confidence: Optional[float] = None
    old_review_reason: Optional[str] = None
    new_review_reason: Optional[str] = None
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    new_linked_at: Optional[datetime] = None
    resolution_method: Optional[str] = None
    unrecognized_handle: Optional[str] = None
    venue_name_before: Optional[str] = None
    venue_name_after: Optional[str] = None

    def write_fields(self) -> dict:
        """The partial-update payload for `venue_dao.update_event` — only
        ever called for action in ("repoint", "detach")."""
        return {
            "venue_id": self.new_venue_id,
            "location_resolution": self.new_location_resolution,
            "location_confidence": self.new_location_confidence,
            "linked_by": self.new_linked_by,
            "linked_at": self.new_linked_at,
            "review_reason": self.new_review_reason,
            "status": self.new_status,
        }


def decide_one(
    event: dict, *, venues: list, handle_index: dict, venue_names_by_id: dict,
    confidence_floor: float, margin: float, min_confidence: float, now: datetime,
) -> Decision:
    """The per-row decision — plan §B (skip table) then §A (re-resolution)
    then §C (review-reason fold + status restoration). Pure: takes and
    returns plain values, makes no DAO call."""
    event_id = event["event_id"]
    old_venue_id = event.get("venue_id")

    # ── plan §B: the per-status/per-protection skip table ────────────────
    reason = _skip_reason(event)
    if reason is not None:
        return _skip_decision(event, reason)

    # ── plan §A: re-resolve from the event's OWN stored location text ────
    location_text_input = _location_text_input(event)
    resolution = resolve_event_venue(
        caption=None, location_text=location_text_input, location_tag=None,
        promoter_handle=event.get("source_handle"), venues=venues, handle_index=handle_index,
        confidence_floor=confidence_floor, margin=margin, same_account_venues=None,
    )

    if resolution.resolution == RESOLUTION_AUTO:
        new_venue_id = resolution.venue_id
        new_location_resolution = RESOLUTION_AUTO
        new_location_confidence = resolution.confidence
        new_linked_by = resolution.method
        new_linked_at: Optional[datetime] = now
        no_venue_reason: Optional[str] = None
    else:
        # RESOLUTION_UNRESOLVED, or the (in this corpus, essentially never
        # hit) RESOLUTION_QUEUED — an ambiguous rung-4 name match this
        # script does not persist candidates for and must not silently
        # accept. Both detach honestly rather than guess.
        new_venue_id = None
        new_location_resolution = RESOLUTION_UNRESOLVED
        new_location_confidence = None
        new_linked_by = None
        new_linked_at = None
        no_venue_reason = (
            REVIEW_REASON_VENUE_NOT_IN_CATALOG if resolution.method == METHOD_VENUE_NOT_IN_CATALOG
            else REVIEW_REASON_UNRESOLVED_VENUE
        )

    old_review_reason = event.get("review_reason")
    new_review_reason = _fold_no_venue_reason(old_review_reason, no_venue_reason)

    # ── plan §C: status restoration goes through is_clean_extraction, never asserted ──
    new_status, new_review_reason = _restore_status(
        event, gate_reason=new_review_reason, new_venue_id=new_venue_id,
        new_review_reason=new_review_reason, min_confidence=min_confidence,
    )

    unrecognized_handle = None
    if resolution.method == METHOD_VENUE_NOT_IN_CATALOG:
        unrecognized_handle = _first_unrecognized_handle(
            location_text_input, event.get("source_handle"), handle_index,
        )

    unchanged = (
        new_venue_id == old_venue_id
        and new_location_resolution == event.get("location_resolution")
        and new_location_confidence == event.get("location_confidence")
        and new_linked_by == event.get("linked_by")
        and new_review_reason == old_review_reason
        and new_status == event.get("status")
    )
    action = "unchanged" if unchanged else ("detach" if new_venue_id is None else "repoint")

    return Decision(
        event_id=event_id, action=action,
        old_venue_id=old_venue_id, new_venue_id=new_venue_id,
        old_linked_by=event.get("linked_by"), new_linked_by=new_linked_by,
        old_location_resolution=event.get("location_resolution"),
        new_location_resolution=new_location_resolution,
        old_location_confidence=event.get("location_confidence"),
        new_location_confidence=new_location_confidence,
        old_review_reason=old_review_reason, new_review_reason=new_review_reason,
        old_status=event.get("status"), new_status=new_status,
        new_linked_at=new_linked_at, resolution_method=resolution.method,
        unrecognized_handle=unrecognized_handle,
        venue_name_before=event.get("venue_name"),
        venue_name_after=venue_names_by_id.get(new_venue_id) if new_venue_id else None,
    )


def decide_one_disputed(
    event: dict, *, venues: list, handle_index: dict, venue_names_by_id: dict,
    min_confidence: float, now: datetime, withhold_disputed: bool = False,
) -> Decision:
    """`--mode disputed-location-text`'s per-row decision — 260912 §C's
    historical half. Pure, like `decide_one`: takes and returns plain
    values, makes no DAO call.

    The SAME skip table as `decide_one` applies first (an operator's
    confirmation, manual link, or `venue_id` edit always wins), then the
    dispute rule is IMPORTED, never re-derived, so this repair and the live
    pipeline can never disagree about a row.
    """
    event_id = event["event_id"]
    edited = event.get("operator_edited_fields") or []
    old_venue_id = event.get("venue_id")

    reason = _skip_reason(event)
    if reason is not None:
        return _skip_decision(event, reason)

    location_text_input = _location_text_input(event)
    verdict = evaluate_attribution_dispute(
        mapped_venue_id=old_venue_id, location_text=location_text_input,
        venues=venues, handle_index=handle_index,
        # No `location_tag`: this is a re-decision over ALREADY-STORED text,
        # and the post's own Instagram tag is not stored on the row. Rungs 1
        # and 3 are what this repair acts on.
        location_tag=None, promoter_handle=event.get("source_handle"),
        operator_edited_fields=edited,
    )

    old_review_reason = event.get("review_reason")
    if verdict is None:
        return Decision(
            event_id=event_id, action="unchanged",
            old_venue_id=old_venue_id, new_venue_id=old_venue_id,
            old_linked_by=event.get("linked_by"), new_linked_by=event.get("linked_by"),
            old_location_resolution=event.get("location_resolution"),
            new_location_resolution=event.get("location_resolution"),
            old_location_confidence=event.get("location_confidence"),
            new_location_confidence=event.get("location_confidence"),
            old_review_reason=old_review_reason, new_review_reason=old_review_reason,
            old_status=event.get("status"), new_status=event.get("status"),
            venue_name_before=event.get("venue_name"),
            venue_name_after=event.get("venue_name"),
        )

    if verdict.has_target:
        new_venue_id = verdict.target_venue_id
        new_location_resolution = RESOLUTION_AUTO
        new_location_confidence = verdict.confidence
        new_linked_by = verdict.method
        new_linked_at: Optional[datetime] = now
        # The dispute is RESOLVED by the repoint — recording a reason for
        # something that is no longer true would queue a row nobody needs
        # to look at.
        new_review_reason = old_review_reason
    else:
        # `venue_not_in_catalog`: nowhere to move it to. Keep the venue,
        # record the dispute (plan §C: "never re-attribute ... and never
        # silently keep serving it at a venue we now have evidence is
        # wrong").
        new_venue_id = old_venue_id
        new_location_resolution = event.get("location_resolution")
        new_location_confidence = event.get("location_confidence")
        new_linked_by = event.get("linked_by")
        new_linked_at = None
        new_review_reason = fold_review_reason(
            old_review_reason, REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE,
        )

    # Status goes through `is_clean_extraction`, never asserted — the same
    # rule `decide_one` follows. `withhold_disputed` off (the default, and
    # the live pipeline's own default) hides the dispute token from the
    # gate, so this repair records the reason without withdrawing the row
    # from serving.
    gate_reason = new_review_reason
    if not withhold_disputed and gate_reason:
        remaining = [
            token for token in gate_reason.split("; ")
            if token and token != REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE
        ]
        gate_reason = "; ".join(remaining) if remaining else None
    new_status, new_review_reason = _restore_status(
        event, gate_reason=gate_reason, new_venue_id=new_venue_id,
        new_review_reason=new_review_reason, min_confidence=min_confidence,
    )

    unchanged = (
        new_venue_id == old_venue_id
        and new_linked_by == event.get("linked_by")
        and new_review_reason == old_review_reason
        and new_status == event.get("status")
    )
    if unchanged:
        action = "unchanged"
    elif new_venue_id != old_venue_id:
        action = "repoint"
    else:
        action = "flag"

    return Decision(
        event_id=event_id, action=action,
        old_venue_id=old_venue_id, new_venue_id=new_venue_id,
        old_linked_by=event.get("linked_by"), new_linked_by=new_linked_by,
        old_location_resolution=event.get("location_resolution"),
        new_location_resolution=new_location_resolution,
        old_location_confidence=event.get("location_confidence"),
        new_location_confidence=new_location_confidence,
        old_review_reason=old_review_reason, new_review_reason=new_review_reason,
        old_status=event.get("status"), new_status=new_status,
        new_linked_at=new_linked_at, resolution_method=verdict.method,
        unrecognized_handle=(
            _first_unrecognized_handle(
                location_text_input, event.get("source_handle"), handle_index,
            ) if not verdict.has_target else None
        ),
        venue_name_before=event.get("venue_name"),
        venue_name_after=venue_names_by_id.get(new_venue_id) if new_venue_id else None,
    )


def decide_one_force_reassign(
    event: dict, *, to_venue_id: Optional[str], venue_names_by_id: dict,
    min_confidence: float, now: datetime,
) -> Decision:
    """`--mode force-reassign`'s per-row decision (plans/260914_recife-
    venue-mapping-corrections.md §2): an unconditional, operator-pinned
    `venue_id` write, never a ladder re-run. `_select_candidates` already
    enforces `source_handle == handle`, `linked_by IS NULL`, and
    `venue_id == from_venue_id` — this function does not re-check
    `from_venue_id`, it only decides the write. The SAME skip table
    `decide_one`/`decide_one_disputed` apply protects an operator's own
    prior correction even from an operator-pinned script run.

    `to_venue_id is None` is the DETACH shape (`editaisculturape`):
    `venue_id`/`location_resolution` are cleared to `NULL` — an honest
    "never decided," not `RESOLUTION_UNRESOLVED` (no ladder ran here) —
    `location_confidence` is left exactly as it was (the plan's own Open
    Questions specify this), and `unresolved_venue` is folded IN.
    `to_venue_id` set is the ASSIGN shape (`real.botequim`/
    `marcellos_music_bar`): `location_resolution = RESOLUTION_MANUAL` (the
    existing sentinel for an operator-pinned link, otherwise unused by any
    automated path), `location_confidence = None` (no algorithmic score
    exists for a pinned write — the same value `POST /{event_id}/link`
    writes when called with a bare `venue_id`, no `candidate_rank`), and
    `unresolved_venue`/`venue_not_in_catalog` are folded OUT. Neither branch
    touches `linked_by` — the plan's Data Impact section lists only
    `venue_id`, `location_resolution`, `review_reason`, `status`, and
    `linked_at` as touched columns; this is not a ladder verdict, so there
    is no new link method to record.
    """
    reason = _skip_reason(event)
    if reason is not None:
        return _skip_decision(event, reason)

    event_id = event["event_id"]
    old_venue_id = event.get("venue_id")
    old_review_reason = event.get("review_reason")

    if to_venue_id is not None:
        new_venue_id = to_venue_id
        new_location_resolution = RESOLUTION_MANUAL
        new_location_confidence = None
        new_linked_at: Optional[datetime] = now
        no_venue_reason: Optional[str] = None
    else:
        new_venue_id = None
        new_location_resolution = None
        new_location_confidence = event.get("location_confidence")
        new_linked_at = None
        no_venue_reason = REVIEW_REASON_UNRESOLVED_VENUE

    new_review_reason = _fold_no_venue_reason(old_review_reason, no_venue_reason)
    new_status, new_review_reason = _restore_status(
        event, gate_reason=new_review_reason, new_venue_id=new_venue_id,
        new_review_reason=new_review_reason, min_confidence=min_confidence,
    )

    unchanged = (
        new_venue_id == old_venue_id
        and new_location_resolution == event.get("location_resolution")
        and new_location_confidence == event.get("location_confidence")
        and new_review_reason == old_review_reason
        and new_status == event.get("status")
    )
    action = "unchanged" if unchanged else ("detach" if new_venue_id is None else "repoint")

    return Decision(
        event_id=event_id, action=action,
        old_venue_id=old_venue_id, new_venue_id=new_venue_id,
        old_linked_by=event.get("linked_by"), new_linked_by=event.get("linked_by"),
        old_location_resolution=event.get("location_resolution"),
        new_location_resolution=new_location_resolution,
        old_location_confidence=event.get("location_confidence"),
        new_location_confidence=new_location_confidence,
        old_review_reason=old_review_reason, new_review_reason=new_review_reason,
        old_status=event.get("status"), new_status=new_status,
        new_linked_at=new_linked_at, resolution_method=None,
        venue_name_before=event.get("venue_name"),
        venue_name_after=venue_names_by_id.get(new_venue_id) if new_venue_id else None,
    )


@dataclass
class Report:
    mode: str = MODE_HANDLE_MENTION
    selected: int = 0
    skipped_by_reason: Counter = field(default_factory=Counter)
    repointed: int = 0
    # 260912 §C: a disputed row with nowhere to go — the review reason is
    # recorded, `venue_id` is left exactly as it was. Neither a repoint nor
    # a detach, so it gets its own bucket (and its own term in the balance).
    flagged: int = 0
    detached: int = 0
    detached_venue_not_in_catalog: int = 0
    detached_unresolved: int = 0
    unchanged: int = 0
    before_linked_by: Counter = field(default_factory=Counter)
    after_linked_by: Counter = field(default_factory=Counter)
    before_venue_counts: Counter = field(default_factory=Counter)
    after_venue_counts: Counter = field(default_factory=Counter)
    # [(identity_tuple, [event_id, ...]), ...] — every group of 2+ events
    # sharing (venue_id, date, normalized_title) after this run.
    venue_identity_collisions: list = field(default_factory=list)
    # [(detached_event_id, resolved_sibling_event_id, identity_tuple), ...]
    handle_identity_collisions: list = field(default_factory=list)
    _venue_acquisition_backlog_counter: Counter = field(default_factory=Counter)
    venue_acquisition_backlog: list = field(default_factory=list)  # [(handle, count), ...] desc
    rows: list = field(default_factory=list)  # every non-skip Decision, in event_id order
    failed_writes: list = field(default_factory=list)
    applied: bool = False
    balanced: bool = False

    @property
    def changed_count(self) -> int:
        return self.repointed + self.detached + self.flagged


def check_balance(report: Report) -> None:
    """plan Error Handling: "assert selected == sum(everything else) before
    exit ... exit non-zero if the arithmetic does not balance." A separate,
    directly testable function — a fabricated `Report` can be handed to it
    without running a real backfill at all."""
    total = (
        report.repointed + report.detached + report.flagged + report.unchanged
        + sum(report.skipped_by_reason.values())
    )
    if total != report.selected:
        report.balanced = False
        raise ArithmeticImbalance(
            f"totals did not balance: selected={report.selected} but "
            f"repointed({report.repointed}) + detached({report.detached}) + "
            f"flagged({report.flagged}) + unchanged({report.unchanged}) + "
            f"skipped({sum(report.skipped_by_reason.values())}) = {total}",
            report=report,
        )
    report.balanced = True


def _select_candidates(
    all_events: list, *, mode: str, since_id: Optional[str],
    handles: Optional[list] = None, from_venue_id: Optional[str] = None,
) -> list:
    """The ONE place any mode's selection is expressed.

    `handle-mention` (default): rows the caption-mention precedence bug
    linked, exactly as this script has always selected them.

    `disputed-location-text`: rows a VENUE POST's own fixed attribution
    produced — `post_type == "event"`, a venue already attached, and NO
    `linked_by`, because `EventExtractionService._extract_one`'s fixed-venue
    closure writes none of the four link columns. That last clause is what
    keeps this mode off every row any RUNG ever decided (those all carry a
    `linked_by`), so the two modes can never select the same row.

    `force-reassign` (plans/260914_recife-venue-mapping-corrections.md §2):
    ONE handle (`handles[0]` — the CLI enforces exactly one), `linked_by IS
    NULL` (an operator-pinned write never overrides a ladder verdict — an
    already-linked row is not this mode's business), and `venue_id ==
    from_venue_id` (`None` selects a currently-unlinked row for an ASSIGN;
    a real id selects a currently-mis-mapped row for a DETACH or a
    repoint) — this is also what makes the mode idempotent BY SELECTION: a
    row this mode already corrected no longer has `venue_id == from_venue_id`
    on a second run with the same arguments.

    `unresolved-venue`: `venue_id IS NULL`, optionally scoped to one or more
    `handles` (catalog-wide when omitted/empty) — the capability plan
    §2 opens up for a future general sweep, though every invocation this
    plan itself makes always scopes it to one handle.

    `caption-handle-mention` (plans/260914_promoter-roundup-caption-
    mention.md): `linked_by == METHOD_CAPTION_HANDLE_MENTION` — rows the
    OLD, pre-fix rung 5 auto-linked purely because a roundup post's caption
    happened to name exactly one catalogued venue, even though the post's
    own sibling events named several different places. Re-decided by the
    SAME `decide_one` engine as `handle-mention`/`unresolved-venue`
    (unchanged — its `caption=None` re-resolution can never itself reach
    rung 5, so no sibling-location-text plumbing belongs here); the mode
    only changes WHICH rows are selected.
    """
    if mode == MODE_DISPUTED_LOCATION_TEXT:
        candidates = (
            e for e in all_events
            if (e.get("post_type") or KIND_EVENT) == KIND_EVENT
            and e.get("venue_id")
            and not e.get("linked_by")
        )
    elif mode == MODE_FORCE_REASSIGN:
        handle = handles[0] if handles else None
        candidates = (
            e for e in all_events
            if e.get("source_handle") == handle
            and not e.get("linked_by")
            and e.get("venue_id") == from_venue_id
        )
    elif mode == MODE_UNRESOLVED_VENUE:
        handle_set = set(handles) if handles else None
        candidates = (
            e for e in all_events
            if e.get("venue_id") is None
            and (handle_set is None or e.get("source_handle") in handle_set)
        )
    elif mode == MODE_CAPTION_HANDLE_MENTION:
        candidates = (
            e for e in all_events if e.get("linked_by") == METHOD_CAPTION_HANDLE_MENTION
        )
    else:
        candidates = (e for e in all_events if e.get("linked_by") == METHOD_HANDLE_MENTION)
    return sorted(
        (e for e in candidates if since_id is None or e["event_id"] > since_id),
        key=lambda e: e["event_id"],
    )


def run_backfill(
    venue_dao, *, apply: bool, since_id: Optional[str] = None, now: Optional[datetime] = None,
    mode: str = MODE_HANDLE_MENTION, withhold_disputed: bool = False,
    handles: Optional[list] = None, from_venue_id: Optional[str] = None,
    to_venue_id: Optional[str] = None,
) -> Report:
    """The whole backfill in one pass: dependency guard, selection,
    per-row decisions, writes (when `apply`), balance/write-failure
    checks, then a final-state pass for collisions/backlog/after-counts.

    `since_id` resumes: only candidates with `event_id > since_id` (plain
    string comparison — event ids are time-ordered ULIDs, so this IS
    chronological order) are selected THIS run; every other row (already
    processed by an earlier invocation, or simply outside the resumed
    range) contributes its CURRENT stored state to the after-counts and
    collision detection unchanged.

    `handles`/`from_venue_id`/`to_venue_id` only matter for the two newest
    modes (plans/260914_recife-venue-mapping-corrections.md): `handles`
    scopes `force-reassign` (exactly one) and optionally `unresolved-venue`
    (zero or more — catalog-wide when empty/omitted); `from_venue_id`/
    `to_venue_id` are `force-reassign`'s own pinned write and are ignored by
    every other mode.
    """
    _assert_forward_fix_landed()
    now = now or datetime.now(timezone.utc)

    all_events = venue_dao.list_events()
    venues = build_venue_catalog(venue_dao)
    handle_index = build_handle_index(venue_dao)
    venue_names_by_id = {v.venue_id: v.venue_name for v in venues}

    report = Report(applied=apply, mode=mode)
    report.before_linked_by = Counter(e.get("linked_by") for e in all_events if e.get("linked_by"))
    report.before_venue_counts = Counter(e.get("venue_name") for e in all_events if e.get("venue_id"))

    candidates = _select_candidates(
        all_events, mode=mode, since_id=since_id, handles=handles, from_venue_id=from_venue_id,
    )
    report.selected = len(candidates)

    decisions_by_id: dict[str, Decision] = {}
    for event in candidates:
        if mode == MODE_DISPUTED_LOCATION_TEXT:
            decision = decide_one_disputed(
                event, venues=venues, handle_index=handle_index,
                venue_names_by_id=venue_names_by_id,
                min_confidence=settings.event_extraction_min_confidence,
                now=now, withhold_disputed=withhold_disputed,
            )
        elif mode == MODE_FORCE_REASSIGN:
            decision = decide_one_force_reassign(
                event, to_venue_id=to_venue_id, venue_names_by_id=venue_names_by_id,
                min_confidence=settings.event_extraction_min_confidence, now=now,
            )
        else:
            # MODE_HANDLE_MENTION, MODE_UNRESOLVED_VENUE, or
            # MODE_CAPTION_HANDLE_MENTION — the SAME engine; only
            # `_select_candidates` differs between them.
            decision = decide_one(
                event, venues=venues, handle_index=handle_index, venue_names_by_id=venue_names_by_id,
                confidence_floor=settings.promoter_link_confidence_floor,
                margin=settings.promoter_link_margin,
                min_confidence=settings.event_extraction_min_confidence,
                now=now,
            )
        decisions_by_id[event["event_id"]] = decision

        if decision.action == "skip":
            report.skipped_by_reason[decision.skip_reason] += 1
            continue
        report.rows.append(decision)
        if decision.action == "unchanged":
            report.unchanged += 1
            continue
        if decision.action == "flag":
            report.flagged += 1
            if decision.unrecognized_handle:
                report._venue_acquisition_backlog_counter[decision.unrecognized_handle] += 1
        elif decision.action == "detach":
            report.detached += 1
            if decision.resolution_method == METHOD_VENUE_NOT_IN_CATALOG:
                report.detached_venue_not_in_catalog += 1
                if decision.unrecognized_handle:
                    report._venue_acquisition_backlog_counter[decision.unrecognized_handle] += 1
            else:
                report.detached_unresolved += 1
        else:
            report.repointed += 1

        if apply:
            result = venue_dao.update_event(event["event_id"], decision.write_fields())
            if result is None:
                # Stop immediately rather than writing past a detected
                # anomaly (the row vanished, or a concurrent process
                # touched it) — this run's own selected/repointed/detached/
                # unchanged counts will legitimately be short of `selected`
                # as a result, so the write-failure is reported BEFORE the
                # balance check below, never alongside a confusing second
                # "did not balance" error for the same root cause. Resume
                # with --since-id once the anomaly is understood.
                report.failed_writes.append(event["event_id"])
                break

    if report.failed_writes:
        raise WriteAffectedNoRows(report.failed_writes[0], report=report)
    check_balance(report)

    # ── final-state projection, for after-counts and both collision shapes ──
    _WRITING_ACTIONS = ("repoint", "detach", "flag")

    def _final_venue_id(e: dict) -> Optional[str]:
        d = decisions_by_id.get(e["event_id"])
        return d.new_venue_id if (d and d.action in _WRITING_ACTIONS) else e.get("venue_id")

    def _final_linked_by(e: dict) -> Optional[str]:
        d = decisions_by_id.get(e["event_id"])
        return d.new_linked_by if (d and d.action in _WRITING_ACTIONS) else e.get("linked_by")

    for e in all_events:
        linked_by = _final_linked_by(e)
        if linked_by:
            report.after_linked_by[linked_by] += 1
        vid = _final_venue_id(e)
        if vid:
            name = venue_names_by_id.get(vid) or e.get("venue_name")
            report.after_venue_counts[name] += 1

    # Venue-identity collisions (plan §D, first shape): two events sharing
    # (venue_id, date, normalized title) once THIS run's re-attributions
    # land — reuses app.services.event_merge.compute_event_identity, the
    # SAME identity a real merge would compute, without ever calling the
    # merge itself.
    identity_map: dict[tuple, list[str]] = {}
    for e in all_events:
        identity = compute_event_identity(_final_venue_id(e), e.get("starts_at"), e.get("title"))
        if identity is not None:
            identity_map.setdefault(identity, []).append(e["event_id"])
    report.venue_identity_collisions = [
        (identity, ids) for identity, ids in identity_map.items() if len(ids) > 1
    ]

    # Handle-identity collisions (plan §D, second shape): a row THIS run
    # detached that shares (source_handle, date, normalized title) with a
    # sibling that still has a venue — a future merge_touched_events could
    # absorb it. Reuses compute_handle_identity; never merges anything.
    handle_groups: dict[tuple, list[str]] = {}
    for e in all_events:
        identity = compute_handle_identity(e)
        if identity is not None:
            handle_groups.setdefault(identity, []).append(e["event_id"])
    for e in all_events:
        decision = decisions_by_id.get(e["event_id"])
        if not (decision and decision.action == "detach"):
            continue
        identity = compute_handle_identity(e)
        if identity is None:
            continue
        for sibling_id in handle_groups.get(identity, []):
            if sibling_id == e["event_id"]:
                continue
            sibling = next(s for s in all_events if s["event_id"] == sibling_id)
            if _final_venue_id(sibling):
                report.handle_identity_collisions.append((e["event_id"], sibling_id, identity))

    report.venue_acquisition_backlog = sorted(
        report._venue_acquisition_backlog_counter.items(), key=lambda kv: (-kv[1], kv[0]),
    )
    return report


def _print_report(report: Report) -> None:
    mode = "APPLY" if report.applied else "DRY RUN"
    logger.info("=== backfill_event_venue_links (%s) ===", mode)
    logger.info("selected (mode=%s): %d", report.mode, report.selected)
    logger.info("skipped: %s", dict(report.skipped_by_reason))
    logger.info(
        "repointed: %d | flagged: %d | detached: %d (venue_not_in_catalog=%d, unresolved=%d) "
        "| unchanged: %d",
        report.repointed, report.flagged, report.detached,
        report.detached_venue_not_in_catalog, report.detached_unresolved, report.unchanged,
    )
    logger.info("linked_by before: %s", dict(report.before_linked_by))
    logger.info("linked_by after : %s", dict(report.after_linked_by))
    logger.info("per-venue item counts before: %s", dict(report.before_venue_counts.most_common(15)))
    logger.info("per-venue item counts after : %s", dict(report.after_venue_counts.most_common(15)))
    for decision in report.rows:
        logger.info(
            "  %s %s | venue: %r(%s) -> %r(%s) | linked_by: %s -> %s | status: %s -> %s | "
            "review_reason: %r -> %r",
            decision.action, decision.event_id,
            decision.venue_name_before, decision.old_venue_id,
            decision.venue_name_after, decision.new_venue_id,
            decision.old_linked_by, decision.new_linked_by,
            decision.old_status, decision.new_status,
            decision.old_review_reason, decision.new_review_reason,
        )
    if report.venue_identity_collisions:
        logger.info("VENUE IDENTITY COLLISIONS (written anyway, never merged):")
        for identity, ids in report.venue_identity_collisions:
            logger.info("  %s -> %s", identity, ids)
    if report.handle_identity_collisions:
        logger.info("HANDLE IDENTITY COLLISIONS (a future merge could absorb these):")
        for detached_id, sibling_id, identity in report.handle_identity_collisions:
            logger.info("  detached=%s resolved_sibling=%s identity=%s", detached_id, sibling_id, identity)
    if report.venue_acquisition_backlog:
        logger.info("venue-acquisition backlog (unrecognized handle -> items it would recover):")
        for handle, count in report.venue_acquisition_backlog:
            logger.info("  @%s: %d", handle, count)
    logger.info("balanced: %s", report.balanced)


def _parse_venue_id_arg(raw: str) -> Optional[str]:
    """`--from-venue-id`/`--to-venue-id` both accept a real venue_id or the
    literal `none` (case-insensitive — no real venue_id is ever spelled that
    way) meaning "no venue" / `NULL`."""
    return None if raw.strip().lower() == "none" else raw


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Repair events.post_item rows whose venue came from a "
        "post-level caption mention instead of the event's own stated "
        "location. Dry-run by default.",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="write the repaired links (default: dry-run report only)",
    )
    ap.add_argument(
        "--since-id", default=None,
        help="resume: only process candidates with event_id greater than this id",
    )
    ap.add_argument(
        "--mode", choices=MODES, default=MODE_HANDLE_MENTION,
        help="which rows to re-decide: 'handle-mention' (the default, unchanged), "
             "'disputed-location-text' (260912 §C's venue-post attribution repair), "
             "'force-reassign' (an unconditional, operator-pinned venue_id write — "
             "260914 §2), 'unresolved-venue' (re-run the unchanged ladder against "
             "venue_id IS NULL rows — 260914 §2), or 'caption-handle-mention' "
             "(re-run the unchanged ladder against rows the old, pre-fix rung 5 "
             "wrongly linked from a roundup post's caption)",
    )
    ap.add_argument(
        "--withhold-disputed", action="store_true",
        help="let a flagged row's new review reason withhold auto-accept "
             "(default: record the reason, leave the row's status as it was)",
    )
    ap.add_argument(
        "--handle", action="append", default=None,
        help="scope selection to this handle — 'force-reassign' requires exactly "
             "one; 'unresolved-venue' accepts zero or more (repeat the flag), "
             "catalog-wide when omitted",
    )
    ap.add_argument(
        "--from-venue-id", default=None,
        help="'force-reassign' only: the venue_id (or the literal 'none') a row "
             "must currently carry to be selected",
    )
    ap.add_argument(
        "--to-venue-id", default=None,
        help="'force-reassign' only: the venue_id (or the literal 'none') to write",
    )
    args = ap.parse_args(argv)

    from_venue_id = to_venue_id = None
    if args.mode == MODE_FORCE_REASSIGN:
        if not args.handle or len(args.handle) != 1:
            ap.error("--mode force-reassign requires exactly one --handle")
        if args.from_venue_id is None or args.to_venue_id is None:
            ap.error("--mode force-reassign requires --from-venue-id and --to-venue-id")
        from_venue_id = _parse_venue_id_arg(args.from_venue_id)
        to_venue_id = _parse_venue_id_arg(args.to_venue_id)

    venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))

    try:
        report = run_backfill(
            venue_dao, apply=args.apply, since_id=args.since_id,
            mode=args.mode, withhold_disputed=args.withhold_disputed,
            handles=args.handle, from_venue_id=from_venue_id, to_venue_id=to_venue_id,
        )
    except DependencyNotLanded as exc:
        logger.error(str(exc))
        return 2
    except WriteAffectedNoRows as exc:
        if exc.report is not None:
            _print_report(exc.report)
        logger.error(str(exc))
        return 3
    except ArithmeticImbalance as exc:
        if exc.report is not None:
            _print_report(exc.report)
        logger.error(str(exc))
        return 4

    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
