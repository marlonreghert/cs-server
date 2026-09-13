"""Operator CLI: choose a display title for merged listings that already
exist, once, historically.

See plans/260912_events-venue-night-duplication.md §G. The runtime pass
(`EventDisplayTitleService`, run at the end of every extraction run) covers
events a crawl TOUCHES. That fixes the future and leaves the past exactly as
it is: a listing merged last week never gets a chosen title, because nothing
re-touches it. This script is the one-off pass over what is already stored.

It is not a second implementation: it imports `EventDisplayTitleService` and
drives the SAME `obvious_canonical_title` predicate, the SAME one-call-per-
group rule and the SAME deterministic validation gate. If this script and the
runtime pass could ever disagree about a title, the tests pinning one would
stop being evidence about the other.

## Discipline, identical to this repo's other history-repair scripts

  - **dry-run by default**, `--apply` to write;
  - **idempotent** — a row that already carries a `display_title` is skipped,
    and `_finish_absorption` is what clears one when a group grows, so a
    second `--apply` re-decides nothing;
  - **resumable** via `--since-id` (plain string comparison over `event_id`,
    which is a time-ordered ULID);
  - **`--max-calls` is the cost ceiling.** Every model call is one text-only
    request; this is what stops an unattended run from discovering a corpus
    ten times larger than expected and paying for it. Groups beyond the
    ceiling are left for a later, resumed run.

## Cost, stated before it is spent

Zero calls for a group with an obvious canonical title, a single source, an
operator-edited title, or a display title it already carries. One text-only
call for every other merged listing — sized by the RCA at 55 venue-night
groups corpus-wide. Run the dry run first: it reports exactly how many calls
`--apply` would make, without making any.

Usage:
    python -m scripts.backfill_event_display_titles                     # dry run: count the calls
    python -m scripts.backfill_event_display_titles --apply --max-calls 60
    python -m scripts.backfill_event_display_titles --apply --since-id evt_X

The display-title flag (`event_display_title_enabled`) gates the RUNTIME
pass. This script is an explicit operator act, so it runs regardless — but it
still writes nothing without `--apply`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.models.event_kind import KIND_EVENT
from app.services.event_dedup import load_dedup_config
from app.services.event_display_title import (
    EventDisplayTitleService,
    obvious_canonical_title,
    validate_display_title,
)

logger = logging.getLogger("backfill_event_display_titles")

STATUS_SUPERSEDED = "superseded"


@dataclass
class Report:
    selected: int = 0
    skipped_single_source: int = 0
    skipped_already_titled: int = 0
    skipped_operator_edited: int = 0
    obvious: int = 0
    would_call: int = 0
    llm_accepted: int = 0
    rejected: int = 0
    errors: int = 0
    over_ceiling: int = 0
    applied: bool = False
    rows: list = field(default_factory=list)  # [(event_id, outcome, title)]


def _is_candidate(row: dict) -> bool:
    return (
        row.get("post_type") == KIND_EVENT
        and row.get("status") != STATUS_SUPERSEDED
        and row.get("superseded_by") is None
        and bool(row.get("venue_id"))
    )


def plan_backfill(
    venue_dao, *, since_id: Optional[str] = None, max_calls: Optional[int] = None,
) -> tuple:
    """READ-ONLY. Returns `(report, groups_needing_a_call)` — the second is
    the list of event ids `--apply` would spend one model call on each,
    already truncated to `max_calls`. Makes no model call itself, so the dry
    run costs nothing at all."""
    config = load_dedup_config(None)
    service = EventDisplayTitleService(venue_dao, None)
    report = Report()
    needs_call: list = []

    rows = sorted(
        (r for r in venue_dao.list_events() if _is_candidate(r)),
        key=lambda r: r["event_id"],
    )
    for row in rows:
        if since_id is not None and row["event_id"] <= since_id:
            continue
        report.selected += 1
        if row.get("display_title"):
            report.skipped_already_titled += 1
            continue
        if "title" in (row.get("operator_edited_fields") or []):
            report.skipped_operator_edited += 1
            continue
        source_titles = service._source_titles(row["event_id"])
        if len(source_titles) < 2:
            report.skipped_single_source += 1
            continue
        venue_name = service._venue_name(row.get("venue_id"))
        obvious = obvious_canonical_title(
            source_titles, venue_name=venue_name, config=config,
        )
        if obvious is not None:
            report.obvious += 1
            report.rows.append((row["event_id"], "obvious", obvious))
            continue
        if max_calls is not None and len(needs_call) >= max_calls:
            report.over_ceiling += 1
            continue
        needs_call.append(row["event_id"])
        report.would_call += 1
        report.rows.append((row["event_id"], "would_call", None))
    return report, needs_call


async def run_backfill(
    venue_dao, openai_client, *, apply: bool, since_id: Optional[str] = None,
    max_calls: Optional[int] = None,
) -> Report:
    report, needs_call = plan_backfill(
        venue_dao, since_id=since_id, max_calls=max_calls,
    )
    report.applied = apply
    if not apply:
        return report

    config = load_dedup_config(None)
    service = EventDisplayTitleService(venue_dao, openai_client)

    # The OBVIOUS picks first: deterministic, free, and they need no client
    # at all, so they land even if every model call later fails.
    for event_id, outcome, title in report.rows:
        if outcome == "obvious":
            venue_dao.update_event(event_id, {"display_title": title})

    for event_id in needs_call:
        row = venue_dao.get_event(event_id)
        if row is None:
            continue
        source_titles = service._source_titles(event_id)
        venue_name = service._venue_name(row.get("venue_id"))
        starts_at = row.get("starts_at")
        try:
            raw = await openai_client.pick_display_title(
                venue_name=venue_name,
                local_date=starts_at.date().isoformat() if starts_at else None,
                source_titles=list(source_titles), lineup=list(row.get("lineup") or []),
            )
        except Exception as e:
            logger.warning("title pick failed for %s: %s", event_id, e)
            report.errors += 1
            continue
        from app.services.event_display_title import parse_display_title_response

        accepted = validate_display_title(
            parse_display_title_response(raw), source_titles,
            venue_name=venue_name, config=config,
        )
        if accepted is None:
            report.rejected += 1
            continue
        venue_dao.update_event(event_id, {"display_title": accepted})
        report.llm_accepted += 1
    return report


def _print_report(report: Report) -> None:
    mode = "APPLY" if report.applied else "DRY RUN"
    logger.info("=== backfill_event_display_titles (%s) ===", mode)
    logger.info("selected (live, venue-linked event rows): %d", report.selected)
    logger.info(
        "skipped: already_titled=%d single_source=%d operator_edited=%d",
        report.skipped_already_titled, report.skipped_single_source,
        report.skipped_operator_edited,
    )
    logger.info(
        "obvious (no model call): %d | model calls %s: %d",
        report.obvious, "made" if report.applied else "that WOULD be made",
        report.would_call,
    )
    if report.over_ceiling:
        logger.info(
            "%d group(s) left for a later run by --max-calls; resume with --since-id",
            report.over_ceiling,
        )
    if report.applied:
        logger.info(
            "accepted: %d | rejected by the validation gate: %d | errors: %d",
            report.llm_accepted, report.rejected, report.errors,
        )
    for event_id, outcome, title in report.rows:
        logger.info("  %s %s %r", outcome, event_id, title)


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Choose display titles for already-merged event listings. "
        "Dry-run by default; the dry run makes NO model call.",
    )
    ap.add_argument("--apply", action="store_true", help="write the chosen display titles")
    ap.add_argument("--since-id", default=None, help="resume: only consider event ids greater than this")
    ap.add_argument(
        "--max-calls", type=int, default=None, metavar="N",
        help="cost ceiling: never make more than N model calls in this run",
    )
    args = ap.parse_args(argv)

    venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))

    openai_client = None
    if args.apply:
        from app.api.openai_event_extraction_client import OpenAIEventExtractionClient

        if not settings.openai_api_key:
            logger.error("--apply needs an OpenAI key; none is configured")
            return 2
        openai_client = OpenAIEventExtractionClient(api_key=settings.openai_api_key)

    report = asyncio.run(run_backfill(
        venue_dao, openai_client, apply=args.apply,
        since_id=args.since_id, max_calls=args.max_calls,
    ))
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
