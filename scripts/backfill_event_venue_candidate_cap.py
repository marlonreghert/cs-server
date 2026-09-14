"""Operator CLI: shrink already-stored `events.event_venue_link_candidate`
rows down to `top_k` for events written before the cap
(`app.services.event_venue_resolution.DEFAULT_NAME_MATCH_TOP_K`) shipped.

See plans/260913_candidate-cap-and-handle-time-merge.md Part A. Confirmed
live on 2026-09-13: 93 events already store more than 1,000 ranked
candidates each (up to 2,661), and 22 of the 44 `RESOLUTION_QUEUED` events
already carry one of these oversized lists — exactly what a future
`EventVenueAdvisorService` call or `GET /admin/events/review` read would
stuff into a prompt/response today, before any new post for those events
is ever re-crawled.

## Simpler than a re-run, and strictly safer

The plan's own Implementation Approach describes re-deriving each event's
`location_text` and re-running `resolve_event_venue`. This script does
something simpler and more faithful instead: `events.event_venue_link_
candidate` rows are already ranked (`ORDER BY rank`, ties broken however
the original resolution broke them), so truncating the ALREADY-STORED list
to its first `top_k` rows is exactly what a fresh resolution would produce
— with no risk of the venue catalog having drifted between the original
write and this backfill (a new venue added in between could change a
re-run's ranking; it cannot change a pure truncation of what is already on
disk). No ladder call, no venue catalog read, no `location_text` read.

Touches ONLY `events.event_venue_link_candidate`, via `venue_dao.replace_
event_venue_link_candidates` (the same DELETE-then-INSERT primitive every
live resolution write already uses) — never `venue_id`,
`location_resolution`, `linked_by`, or `review_reason` on `events.event`
itself. No Apify, S3, or OpenAI call is possible: nothing in this module
imports a network client.

Dry-run by default, `--apply` to write. Idempotent (a second `--apply`
finds every event already at or below `top_k` and changes nothing) and
resumable (`--since-event-id`, plain string comparison over the ULID-shaped
`event_id`, matching this repo's other resumable scripts).

Usage:
    python -m scripts.backfill_event_venue_candidate_cap                    # dry-run report
    python -m scripts.backfill_event_venue_candidate_cap --top-k 20 --apply # write the truncation
    python -m scripts.backfill_event_venue_candidate_cap --apply --since-event-id evt_01ABC

Exit codes: 0 ok; 1 an event still exceeds `top_k` after `--apply` (should
never happen; investigate before re-running).
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.services.event_venue_resolution import DEFAULT_NAME_MATCH_TOP_K

logger = logging.getLogger("backfill_event_venue_candidate_cap")


@dataclass
class OversizedEvent:
    event_id: str
    current_count: int
    would_be_count: int


@dataclass
class Report:
    top_k: int
    oversized_events: list = field(default_factory=list)  # [OversizedEvent]

    @property
    def oversized_count(self) -> int:
        return len(self.oversized_events)

    @property
    def rows_that_would_be_removed(self) -> int:
        return sum(e.current_count - e.would_be_count for e in self.oversized_events)


def find_oversized_events(venue_dao, *, top_k: int, since_event_id: Optional[str] = None) -> list[dict]:
    """Read-only. Every `event_id` currently storing more than `top_k`
    ranked candidates, with its current count. Goes through the SAME DAO
    boundary every other caller of this table uses (`list_events` +
    `list_event_venue_link_candidates`) rather than a raw SQL GROUP BY
    against `venue_dao.rds_store.engine` — a query-per-event over a few
    hundred events is unremarkable for a one-off operator script (never a
    per-crawl hot path), and staying on the DAO boundary is what lets this
    function run identically against the real store and
    `tests.rds_fake.InMemoryRdsVenueStore`."""
    out = []
    for event in venue_dao.list_events():
        event_id = event["event_id"]
        if since_event_id is not None and not (event_id > since_event_id):
            continue
        n = len(venue_dao.list_event_venue_link_candidates(event_id))
        if n > top_k:
            out.append({"event_id": event_id, "n": n})
    out.sort(key=lambda r: r["event_id"])
    return out


def measure(venue_dao, *, top_k: int = DEFAULT_NAME_MATCH_TOP_K, since_event_id: Optional[str] = None) -> Report:
    """Read-only. Never calls `replace_event_venue_link_candidates` — this
    function cannot mutate a row."""
    oversized = find_oversized_events(venue_dao, top_k=top_k, since_event_id=since_event_id)
    report = Report(top_k=top_k)
    for row in oversized:
        report.oversized_events.append(OversizedEvent(
            event_id=row["event_id"], current_count=row["n"], would_be_count=top_k,
        ))
    return report


def apply_cap(venue_dao, *, top_k: int = DEFAULT_NAME_MATCH_TOP_K, since_event_id: Optional[str] = None) -> Report:
    """Writes: for every oversized event, truncate its ALREADY-STORED,
    already-ranked candidate list to its first `top_k` rows and replace it
    via `replace_event_venue_link_candidates` — the exact shape every live
    resolution write already uses, so a reader can never tell this
    backfill's rows apart from a freshly-resolved capped list."""
    report = measure(venue_dao, top_k=top_k, since_event_id=since_event_id)
    for oversized in report.oversized_events:
        current = venue_dao.list_event_venue_link_candidates(oversized.event_id)
        truncated = current[:top_k]
        venue_dao.replace_event_venue_link_candidates(oversized.event_id, [
            {
                "venue_id": c["venue_id"], "rank": c["rank"], "score": c.get("score"),
                "method": c["method"], "evidence": c.get("evidence") or {},
            }
            for c in truncated
        ])
        logger.info(
            "[backfill_event_venue_candidate_cap] %s: %d -> %d candidates",
            oversized.event_id, oversized.current_count, len(truncated),
        )
    return report


def report_to_dict(report: Report, *, applied: bool) -> dict:
    return {
        "mode": "apply" if applied else "dry_run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "top_k": report.top_k,
        "oversized_count": report.oversized_count,
        "rows_that_would_be_removed": report.rows_that_would_be_removed,
        "oversized_events": [asdict(e) for e in report.oversized_events],
    }


def write_report_json(path: str, report: Report, *, applied: bool) -> None:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report_to_dict(report, applied=applied), indent=2, default=str))
    logger.info("wrote report JSON to %s", target)


def _print_report(report: Report, *, applied: bool) -> None:
    mode = "APPLY" if applied else "DRY RUN"
    logger.info("=== backfill_event_venue_candidate_cap (%s, top_k=%d) ===", mode, report.top_k)
    logger.info(
        "oversized events: %d | rows that would be removed: %d",
        report.oversized_count, report.rows_that_would_be_removed,
    )
    for e in report.oversized_events:
        logger.info("  %s: %d -> %d", e.event_id, e.current_count, e.would_be_count)


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Shrink already-stored event_venue_link_candidate rows to top_k "
        "for events written before the cap shipped.",
    )
    ap.add_argument("--apply", action="store_true", help="write the truncation (default: dry-run report only)")
    ap.add_argument(
        "--top-k", type=int, default=DEFAULT_NAME_MATCH_TOP_K, metavar="N",
        help="keep at most N ranked candidates per event (default: %d)" % DEFAULT_NAME_MATCH_TOP_K,
    )
    ap.add_argument(
        "--since-event-id", default=None, metavar="EVENT_ID",
        help="resume: only consider events with event_id greater than this",
    )
    ap.add_argument("--report-json", default=None, metavar="PATH", help="write the full report as JSON")
    args = ap.parse_args(argv)

    venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))

    if args.apply:
        report = apply_cap(venue_dao, top_k=args.top_k, since_event_id=args.since_event_id)
        _print_report(report, applied=True)
        if args.report_json:
            write_report_json(args.report_json, report, applied=True)
        after = measure(venue_dao, top_k=args.top_k, since_event_id=args.since_event_id)
        if after.oversized_count:
            logger.error(
                "%d event(s) still exceed top_k=%d after --apply — investigate before re-running",
                after.oversized_count, args.top_k,
            )
            return 1
        return 0

    report = measure(venue_dao, top_k=args.top_k, since_event_id=args.since_event_id)
    _print_report(report, applied=False)
    if args.report_json:
        write_report_json(args.report_json, report, applied=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
