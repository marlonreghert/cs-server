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

Usage:
    python -m scripts.backfill_event_venue_links                        # dry-run: report only
    python -m scripts.backfill_event_venue_links --apply                # write the repaired links
    python -m scripts.backfill_event_venue_links --apply --since-id X   # resume after event_id X

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
from app.services.instagram_handle_sources import normalize_handle

logger = logging.getLogger("backfill_event_venue_links")

# Skip reasons — plan §B's per-status/per-protection table.
SKIP_CONFIRMED = "confirmed"
SKIP_MANUAL_LINK = "manual_link"
SKIP_SUPERSEDED = "superseded"
SKIP_EXTRACTION_FAILED = "extraction_failed"
SKIP_REJECTED = "rejected"
SKIP_OPERATOR_EDITED_VENUE = "operator_edited_venue"

# Statuses not named by an event_reconciliation constant — the same bare
# literals ALL_STATUSES itself uses.
_STATUS_EXTRACTION_FAILED = "extraction_failed"
_STATUS_REJECTED = "rejected"


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
    status = event.get("status")
    edited = event.get("operator_edited_fields") or []
    old_venue_id = event.get("venue_id")

    def _skip(reason: str) -> Decision:
        return Decision(
            event_id=event_id, action="skip", skip_reason=reason,
            old_venue_id=old_venue_id, old_status=status,
            old_linked_by=event.get("linked_by"),
            old_location_resolution=event.get("location_resolution"),
            venue_name_before=event.get("venue_name"),
        )

    # ── plan §B: the per-status/per-protection skip table ────────────────
    if status == STATUS_CONFIRMED:
        return _skip(SKIP_CONFIRMED)
    if event.get("location_resolution") == RESOLUTION_MANUAL:
        return _skip(SKIP_MANUAL_LINK)
    if status == STATUS_SUPERSEDED:
        return _skip(SKIP_SUPERSEDED)
    if status == _STATUS_EXTRACTION_FAILED:
        return _skip(SKIP_EXTRACTION_FAILED)
    if status == _STATUS_REJECTED:
        return _skip(SKIP_REJECTED)
    if "venue_id" in edited:
        return _skip(SKIP_OPERATOR_EDITED_VENUE)

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
    # plans/260813_review-gate-and-date-vocabulary.md §C: `post_type` is a
    # real, NOT-NULL column on every already-persisted row this script
    # reads, so `event.get("post_type")` should always resolve — the
    # `or KIND_EVENT` fallback mirrors event_reconciliation.
    # reconcile_post_events' own defensive default for the same call.
    clean = is_clean_extraction(
        review_reason=new_review_reason, starts_at=event.get("starts_at"),
        venue_id=new_venue_id, confidence=event.get("confidence"),
        min_confidence=min_confidence, post_type=event.get("post_type") or KIND_EVENT,
    )
    new_status = STATUS_ACCEPTED if clean else STATUS_PENDING_REVIEW
    if new_status == STATUS_PENDING_REVIEW and not new_review_reason:
        # Mirrors event_reconciliation.reconcile_post_events' own residual
        # fallback — a queued row must never carry a null reason.
        new_review_reason = REVIEW_REASON_NEEDS_REVIEW

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
        and new_status == status
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
        old_status=status, new_status=new_status,
        new_linked_at=new_linked_at, resolution_method=resolution.method,
        unrecognized_handle=unrecognized_handle,
        venue_name_before=event.get("venue_name"),
        venue_name_after=venue_names_by_id.get(new_venue_id) if new_venue_id else None,
    )


@dataclass
class Report:
    selected: int = 0
    skipped_by_reason: Counter = field(default_factory=Counter)
    repointed: int = 0
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
        return self.repointed + self.detached


def check_balance(report: Report) -> None:
    """plan Error Handling: "assert selected == sum(everything else) before
    exit ... exit non-zero if the arithmetic does not balance." A separate,
    directly testable function — a fabricated `Report` can be handed to it
    without running a real backfill at all."""
    total = report.repointed + report.detached + report.unchanged + sum(report.skipped_by_reason.values())
    if total != report.selected:
        report.balanced = False
        raise ArithmeticImbalance(
            f"totals did not balance: selected={report.selected} but "
            f"repointed({report.repointed}) + detached({report.detached}) + "
            f"unchanged({report.unchanged}) + skipped({sum(report.skipped_by_reason.values())}) "
            f"= {total}",
            report=report,
        )
    report.balanced = True


def run_backfill(
    venue_dao, *, apply: bool, since_id: Optional[str] = None, now: Optional[datetime] = None,
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
    """
    _assert_forward_fix_landed()
    now = now or datetime.now(timezone.utc)

    all_events = venue_dao.list_events()
    venues = build_venue_catalog(venue_dao)
    handle_index = build_handle_index(venue_dao)
    venue_names_by_id = {v.venue_id: v.venue_name for v in venues}

    report = Report(applied=apply)
    report.before_linked_by = Counter(e.get("linked_by") for e in all_events if e.get("linked_by"))
    report.before_venue_counts = Counter(e.get("venue_name") for e in all_events if e.get("venue_id"))

    candidates = sorted(
        (
            e for e in all_events
            if e.get("linked_by") == METHOD_HANDLE_MENTION
            and (since_id is None or e["event_id"] > since_id)
        ),
        key=lambda e: e["event_id"],
    )
    report.selected = len(candidates)

    decisions_by_id: dict[str, Decision] = {}
    for event in candidates:
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
        if decision.action == "detach":
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
    def _final_venue_id(e: dict) -> Optional[str]:
        d = decisions_by_id.get(e["event_id"])
        return d.new_venue_id if (d and d.action in ("repoint", "detach")) else e.get("venue_id")

    def _final_linked_by(e: dict) -> Optional[str]:
        d = decisions_by_id.get(e["event_id"])
        return d.new_linked_by if (d and d.action in ("repoint", "detach")) else e.get("linked_by")

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
    logger.info("selected (linked_by=handle_mention): %d", report.selected)
    logger.info("skipped: %s", dict(report.skipped_by_reason))
    logger.info(
        "repointed: %d | detached: %d (venue_not_in_catalog=%d, unresolved=%d) | unchanged: %d",
        report.repointed, report.detached, report.detached_venue_not_in_catalog,
        report.detached_unresolved, report.unchanged,
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
    args = ap.parse_args(argv)

    venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))

    try:
        report = run_backfill(venue_dao, apply=args.apply, since_id=args.since_id)
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
