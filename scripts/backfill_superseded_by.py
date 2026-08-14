"""Operator CLI: back-fill `superseded_by` on rows superseded before this
was recorded.

See plans/260814_record-what-superseded-a-row.md §C.

`app.services.event_reconciliation.reconcile_post_events` now records which
event replaced a row retired by a RE-EXTRACTION of its own post (§A) — but
only going forward. Every row already sitting in `events.post_item` with
`status='superseded'` and `superseded_by IS NULL` was orphaned before that
fix landed and stays that way forever unless something re-derives the link
from data already in RDS. This script is that re-derivation: no model call,
no Apify, no S3 read — everything it needs was already persisted at
extraction time.

### The SAME predicate as §A, never a second one
For each orphan, its candidates are every OTHER, currently-LIVE (`status !=
'superseded'`) event sharing its OWN post (`venue_dao.list_events_by_source`
scoped to the orphan's own `source_handle`/`source_shortcode` — the exact
call `reconcile_post_events` itself makes, so a sibling from a DIFFERENT
post can never enter the candidate pool by construction, never by a filter
that could be forgotten). `app.services.event_reconciliation.find_successor_
candidates` — imported, never reimplemented — decides the rest: exactly one
candidate sharing the orphan's normalised title is a match; zero or several
is left alone, on purpose. See that function's own docstring for why this
must be the identical rule §A applies at write time, not a close cousin of
it.

### Never guess, never touch a live row
Only the orphan's OWN `superseded_by` column is ever written. Status is
never touched (a back-filled row is exactly as superseded as it already
was — this script only completes its bookkeeping) and no candidate row is
ever modified. A row with no `source_handle`/`source_shortcode` at all (no
post to scope by) is reported `no_source` rather than guessed against the
whole corpus.

Dry-run by default, `--apply` to write. Idempotent: a second `--apply` finds
nothing left to do, because the selection itself (`status='superseded' AND
superseded_by IS NULL`) excludes every row this script already linked —
there is no separate bookkeeping to keep in sync with that guarantee, and
an ambiguous orphan is re-evaluated identically every run (same stored
data in, same "ambiguous" verdict out) rather than ever being silently
resolved by a later run. A run that finds nothing to do still exits 0 —
"already correct" is not a failure.

Usage:
    python -m scripts.backfill_superseded_by            # dry-run: report only
    python -m scripts.backfill_superseded_by --apply     # write the recovered links

Capture the dry-run report BEFORE running --apply, and read it in full: it
names every selected row, whether linked, would-be-linked, ambiguous,
lacking a candidate, or lacking a source to scope by — never only the ones
this script acted on.
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.services.event_reconciliation import STATUS_SUPERSEDED, find_successor_candidates

logger = logging.getLogger("backfill_superseded_by")

# ── dispositions ─────────────────────────────────────────────────────────────
# `LINKED`/`WOULD_LINK` are deliberately DISTINCT tokens (never one "linked"
# token whose meaning depends on reading `Report.applied` alongside it, the
# convention `scripts.repair_event_dates` uses) — a dry-run report must say
# outright, per row, "this would have been linked" rather than requiring
# the reader to cross-reference the run's own mode.
DISPOSITION_LINKED = "linked"
DISPOSITION_WOULD_LINK = "would_link"
DISPOSITION_AMBIGUOUS = "ambiguous"
DISPOSITION_NO_CANDIDATE = "no_candidate"
DISPOSITION_NO_SOURCE = "no_source"


@dataclass
class Decision:
    """One row's verdict — one entry in `Report.rows`, naming every
    selected row whether or not this run touched it."""

    event_id: str
    action: str
    source_handle: Optional[str] = None
    source_shortcode: Optional[str] = None
    venue_name: Optional[str] = None
    title: Optional[str] = None
    candidate_ids: list = field(default_factory=list)
    successor_id: Optional[str] = None


@dataclass
class Report:
    applied: bool = False
    selected: int = 0
    by_disposition: Counter = field(default_factory=Counter)
    rows: list = field(default_factory=list)

    @property
    def linked_count(self) -> int:
        return self.by_disposition.get(DISPOSITION_LINKED, 0)


def _emit(report: Report, decision: Decision) -> None:
    report.by_disposition[decision.action] += 1
    report.rows.append(decision)


def run_backfill(venue_dao, *, apply: bool) -> Report:
    """The whole back-fill in one pass: select every orphan, decide each
    against its own post's live siblings via `find_successor_candidates`,
    write (when `apply`), report every row either way."""
    report = Report(applied=apply)
    orphans = [
        row for row in venue_dao.list_events(status=STATUS_SUPERSEDED)
        if row.get("superseded_by") is None
    ]
    report.selected = len(orphans)

    for row in orphans:
        event_id = row["event_id"]
        handle = row.get("source_handle")
        shortcode = row.get("source_shortcode")
        decision = Decision(
            event_id=event_id, action=DISPOSITION_NO_SOURCE,
            source_handle=handle, source_shortcode=shortcode,
            venue_name=row.get("venue_name"), title=row.get("title"),
        )
        if not handle or not shortcode:
            _emit(report, decision)
            continue

        # The SAME call reconcile_post_events itself makes to read one
        # post's rows — scoped to THIS orphan's own post, so a sibling from
        # a different post can never enter the candidate pool.
        siblings = [
            sibling for sibling in venue_dao.list_events_by_source(handle, shortcode)
            if sibling.get("status") != STATUS_SUPERSEDED
        ]
        candidates = find_successor_candidates(row.get("title"), siblings)
        decision.candidate_ids = candidates

        if len(candidates) > 1:
            decision.action = DISPOSITION_AMBIGUOUS
        elif not candidates:
            decision.action = DISPOSITION_NO_CANDIDATE
        else:
            decision.successor_id = candidates[0]
            if apply:
                updated = venue_dao.update_event(event_id, {"superseded_by": candidates[0]})
                if updated is None:
                    raise RuntimeError(
                        f"backfill_superseded_by: UPDATE affected zero rows for event_id={event_id}"
                    )
                decision.action = DISPOSITION_LINKED
            else:
                decision.action = DISPOSITION_WOULD_LINK

        _emit(report, decision)

    return report


def _print_report(report: Report) -> None:
    mode = "APPLY" if report.applied else "DRY RUN"
    logger.info("=== backfill_superseded_by (%s) ===", mode)
    logger.info("selected: %d", report.selected)
    logger.info("by disposition: %s", dict(report.by_disposition))
    for decision in report.rows:
        # Never the whole row — the event id and the chosen successor id
        # (plus enough context for an operator to recognise the row) are
        # enough; raw_extraction/description/etc. never appear here.
        logger.info(
            "  %s event_id=%s venue=%r title=%r source=%s/%s candidates=%s successor=%s",
            decision.action, decision.event_id, decision.venue_name, decision.title,
            decision.source_handle, decision.source_shortcode,
            decision.candidate_ids, decision.successor_id,
        )


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Back-fill superseded_by for rows superseded before it was recorded. "
        "Dry-run by default.",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="write the recovered links (default: dry-run report only)",
    )
    args = ap.parse_args(argv)

    venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))
    report = run_backfill(venue_dao, apply=args.apply)
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
