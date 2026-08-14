"""Operator CLI: mark `events.crawl_target.reels_seeded_at` for targets
whose reels stream had already reached its one-time seed to completion
BEFORE plans/260814_seeded-state-and-config-validation.md §A started
recording that fact going forward.

See plans/260814_seeded-state-and-config-validation.md §B.

## The defect this recovers from (context, not re-derived here)

Before §A, `reels_already_seeded` gated on `cursor_reels_at` alone, which a
genuinely empty (but successfully completed) reels result never sets — so
an empty account's one-time seed was re-purchased on every scheduled crawl,
forever. §A fixed the FORWARD path (a completed run — success OR empty —
now writes `reels_seeded_at` in the same commit as the rest of its
bookkeeping). This script fixes the STANDING state: a target that got stuck
in the bug BEFORE the fix deployed has no future run that will ever re-touch
it (once `crawl_reels` is on and nothing else changes, the only thing that
would prompt a write is a scheduled fire — and every prior scheduled fire
already happened and left the row exactly where it is now).

## The join: `last_run_reels_fetched`, not a fresh probe

plans/260811_reels-on-seed-only.md's own design means reels can ONLY EVER
run as a target's one-time seed attempt — once a stream reaches a
trustworthy conclusion, `reels_already_seeded` gates every later call out.
So for a target still stuck (`cursor_reels_at IS NULL`), whether its reels
stream has EVER reached a trustworthy conclusion is answered by ONE existing
column: `last_run_reels_fetched` (migration 0033) is written ONLY when
`run_target` sees a reels-stream outcome of `OUTCOME_SUCCESS` or
`OUTCOME_EMPTY` (see `app.services.instagram_crawl_service.run_target`) —
never on a failure, a skip, or a run that never attempted reels at all —
and it is left standing (never cleared) by a later run that does not itself
reach a trustworthy conclusion, exactly like `last_failure_kind`'s own
"answers what was last true, even if that was a while ago" convention. So:

  - `last_run_reels_fetched IS NOT NULL` -> a trustworthy (billed) reels
    answer WAS recorded at some point -> the seed is DONE -> mark it.
  - `last_run_reels_fetched IS NULL` AND `last_run_at IS NULL` -> the
    target has never run at all -> correctly still pending -> leave it.
  - `last_run_reels_fetched IS NULL` AND `last_failure_kind` names a
    recorded failure -> the reason nothing was ever recorded is on file
    (blocked / handle_not_found / failed) -> correctly still pending ->
    leave it.
  - Anything else (has run, no failure on record, still no reels answer)
    does NOT cleanly fit either bucket -> report it as AMBIGUOUS, never
    guessed.

Verified against the plan's own Evidence table (measured in production,
2026-08-13) by `tests/test_backfill_reels_seeded.py`'s
`TestMatchesThePlansEvidenceTable`, built from synthetic rows shaped exactly
like that table describes — never by running this script against
production, which this repo's execute-feature workflow forbids.

## Scope, dry-run, idempotency

Only `crawl_reels=True AND cursor_reels_at IS NULL` targets are even
considered (`NOT_CANDIDATE` for everything else, including a target this
script already marked on a prior `--apply` — idempotent by construction: a
second `--apply` finds zero remaining MARK_SEEDED candidates because they
now carry `reels_seeded_at` and fall out at the NOT_CANDIDATE gate).

Dry-run by default; `--apply` writes. Every touched (or would-touch)
target is named in the report, by handle, disposition, and reason — never
silently summarized away.

Usage:
    python -m scripts.backfill_reels_seeded            # dry-run: report only
    python -m scripts.backfill_reels_seeded --apply     # write the marks
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository

logger = logging.getLogger("backfill_reels_seeded")

# ── dispositions ─────────────────────────────────────────────────────────────
DISPOSITION_NOT_CANDIDATE = "not_candidate"
DISPOSITION_MARK_SEEDED = "mark_seeded"
DISPOSITION_LEAVE_UNSEEDED = "leave_unseeded"
DISPOSITION_AMBIGUOUS = "ambiguous"

REASON_REELS_DISABLED = "crawl_reels is off"
REASON_CURSOR_ALREADY_SET = (
    "cursor_reels_at already set -- the seed provably completed, so record it"
)
REASON_ALREADY_MARKED = "reels_seeded_at already set (idempotent no-op)"
REASON_TRUSTWORTHY_ANSWER_ON_RECORD = "last_run_reels_fetched is set -- a trustworthy reels answer was recorded"
REASON_NEVER_RUN = "never run (last_run_at is null)"
REASON_FAILURE_ON_RECORD = "last_failure_kind on record"
REASON_NO_POSITIVE_EVIDENCE_EITHER_WAY = (
    "has run before, no recorded failure, but no recorded reels answer either"
)


@dataclass
class Decision:
    handle: str
    disposition: str
    reason: str
    last_run_reels_fetched: Optional[int] = None
    last_run_at: Optional[object] = None
    last_failure_kind: Optional[str] = None
    cursor_reels_at: Optional[object] = None


def classify_target(target: dict) -> Decision:
    """The pure per-target verdict — computed without writing, so it is
    identical in dry-run and --apply and unit-testable on a bare dict. See
    the module docstring for the full reasoning behind each branch."""
    handle = target["handle"]
    common = {
        "last_run_reels_fetched": target.get("last_run_reels_fetched"),
        "last_run_at": target.get("last_run_at"),
        "last_failure_kind": target.get("last_failure_kind"),
        "cursor_reels_at": target.get("cursor_reels_at"),
    }

    if not target.get("crawl_reels"):
        return Decision(handle, DISPOSITION_NOT_CANDIDATE, REASON_REELS_DISABLED, **common)
    # Idempotency first: an already-marked row is a no-op regardless of what
    # else it carries, so this check must precede the cursor branch below (a
    # row this script marked on a prior --apply may also have a cursor).
    if target.get("reels_seeded_at") is not None:
        return Decision(handle, DISPOSITION_NOT_CANDIDATE, REASON_ALREADY_MARKED, **common)
    # A SET cursor is unambiguous proof the seed completed: `cursor_reels_at`
    # is only ever written from a stream whose outcome was OUTCOME_SUCCESS.
    # These rows must be MARKED, not skipped. `reels_already_seeded` now reads
    # `reels_seeded_at` ALONE, so leaving them unmarked would make every
    # healthy target look unseeded and fire one more paid reels run apiece on
    # its next scheduled crawl -- 8 targets in production the day this
    # shipped, several with a 50-result seed cap. That is precisely the waste
    # this plan exists to stop, re-introduced by the fix for it.
    if target.get("cursor_reels_at") is not None:
        return Decision(handle, DISPOSITION_MARK_SEEDED, REASON_CURSOR_ALREADY_SET, **common)

    if target.get("last_run_reels_fetched") is not None:
        return Decision(
            handle, DISPOSITION_MARK_SEEDED,
            f"{REASON_TRUSTWORTHY_ANSWER_ON_RECORD} (last_run_reels_fetched="
            f"{target['last_run_reels_fetched']})",
            **common,
        )
    if target.get("last_run_at") is None:
        return Decision(handle, DISPOSITION_LEAVE_UNSEEDED, REASON_NEVER_RUN, **common)
    if target.get("last_failure_kind") is not None:
        return Decision(
            handle, DISPOSITION_LEAVE_UNSEEDED,
            f"{REASON_FAILURE_ON_RECORD} (last_failure_kind={target['last_failure_kind']!r})",
            **common,
        )
    return Decision(handle, DISPOSITION_AMBIGUOUS, REASON_NO_POSITIVE_EVIDENCE_EITHER_WAY, **common)


# ── report ────────────────────────────────────────────────────────────────────
@dataclass
class Report:
    selected: int = 0
    dispositions: Counter = field(default_factory=Counter)
    rows: list = field(default_factory=list)  # every non-NOT_CANDIDATE Decision
    marked_handles: list = field(default_factory=list)
    ambiguous_handles: list = field(default_factory=list)
    applied: bool = False
    balanced: bool = False


def check_balance(report: Report) -> None:
    total = sum(report.dispositions.values())
    if total != report.selected:
        report.balanced = False
        raise AssertionError(
            f"totals did not balance: selected={report.selected} but "
            f"dispositions sum to {total}: {dict(report.dispositions)}"
        )
    report.balanced = True


def run_backfill(
    venue_dao, *, apply: bool, now_provider: Optional[Callable[[], datetime]] = None,
) -> Report:
    """The whole backfill in one pass: selection, per-target decisions,
    writes (when `apply`), then the balance check. Every target the DAO
    knows about is `selected` and gets exactly one disposition — mirrors
    scripts/backfill_source_provenance.py's own shape."""
    now = (now_provider or (lambda: datetime.now(timezone.utc)))()
    targets = sorted(venue_dao.list_crawl_targets() or [], key=lambda t: t["handle"])

    report = Report(applied=apply)
    report.selected = len(targets)

    for target in targets:
        decision = classify_target(target)
        report.dispositions[decision.disposition] += 1
        if decision.disposition != DISPOSITION_NOT_CANDIDATE:
            report.rows.append(decision)
        if decision.disposition == DISPOSITION_MARK_SEEDED:
            report.marked_handles.append(decision.handle)
            if apply:
                venue_dao.update_crawl_target(decision.handle, {"reels_seeded_at": now})
        elif decision.disposition == DISPOSITION_AMBIGUOUS:
            report.ambiguous_handles.append(decision.handle)

    check_balance(report)
    return report


def _print_report(report: Report) -> None:
    mode = "APPLY" if report.applied else "DRY RUN"
    logger.info("=== backfill_reels_seeded (%s) ===", mode)
    logger.info("selected: %d", report.selected)
    logger.info("dispositions: %s", dict(report.dispositions))
    for d in report.rows:
        logger.info(
            "  %s handle=%s | reason=%s | last_run_reels_fetched=%r last_run_at=%r "
            "last_failure_kind=%r cursor_reels_at=%r",
            d.disposition, d.handle, d.reason, d.last_run_reels_fetched,
            d.last_run_at, d.last_failure_kind, d.cursor_reels_at,
        )
    logger.info("balanced: %s", report.balanced)
    if report.marked_handles:
        verb = "marked" if report.applied else "would mark"
        logger.info("%s seeded (%d): %s", verb, len(report.marked_handles), report.marked_handles)
    if report.ambiguous_handles:
        logger.warning(
            "AMBIGUOUS -- reported, never guessed (%d): %s -- inspect these by "
            "hand before deciding; this script has deliberately left them alone",
            len(report.ambiguous_handles), report.ambiguous_handles,
        )


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Mark events.crawl_target.reels_seeded_at for targets whose "
        "reels stream already completed (possibly empty) before that fact was "
        "recorded (plans/260814_seeded-state-and-config-validation.md §B). "
        "Dry-run by default.",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="write the recovered reels_seeded_at values (default: dry-run report only)",
    )
    args = ap.parse_args(argv)

    venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))
    report = run_backfill(venue_dao, apply=args.apply)
    _print_report(report)
    return 5 if report.ambiguous_handles else 0


if __name__ == "__main__":
    raise SystemExit(main())
