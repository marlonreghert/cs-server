"""Operator CLI: measure the fuzzy-title/shared-lineup dedup bands across the
existing corpus (dry-run report), and — with `--apply` — sweep the AUTO band
over it once, historically.

See plans/260812_event-dedup-fuzzy-title.md §B ("Ship the measurement as a
script under scripts/... run it against a restored snapshot after fix/event-
attribution-and-dates lands, and put the post-fix numbers in the PR") and §C2
("ship an apply-mode sweep alongside the read-only measure_event_dedup.py —
same predicate, same bands, same config, same choose_canonical/
merge_event_fields/_finish_absorption path").

## One predicate, never two

Dry-run measurement and `--apply` both import `app.services.event_dedup.
evaluate_pair`/`in_candidate_window` — the SAME pure functions
`app.services.event_merge.run_title_similarity_pass` evaluates at runtime —
and `--apply` calls `run_title_similarity_pass` itself (per venue) rather
than re-deriving absorption/grouping logic here. If the measurement and the
sweep could ever disagree about a pair, the measurement would stop being
evidence (plan §C2's own words); importing the SAME functions is how that is
made structurally impossible, not merely promised.

## Dry-run vs `--apply`

Dry-run (default): READ-ONLY. Groups every `post_type == "event"` row by
venue, evaluates every same-venue pair inside the candidate window, and
reports auto/suggest/refuse counts plus every auto pair by title — split by
whether every source attached to BOTH rows is promoter-sourced (plan's own
operator ask: promoter events are hidden from admin reads today, so the
operator needs to see what dedup does to the venue-only corpus they are
actually reviewing, separately from the whole-corpus number). Writes
NOTHING — no suggestion rows, no absorptions, no admin-config read even
(auto_merge_enabled is forced True for the PURPOSES OF THE REPORT only,
never read from or written to the live key).

`--apply`: calls `app.services.event_merge.run_title_similarity_pass` for
every venue with 2+ candidate rows, with `record_suggestions=False` (plan
§C2: "Only the auto band sweeps ... a historical backlog of suggestions
nobody asked for is queue landfill — the suggest band writes suggestions,
which the existing pipeline hook produces anyway as rows are touched") and
`config.auto_merge_enabled` forced True for THIS sweep call only — the LIVE
`admin_config:event_dedup_auto_merge_enabled` key the ongoing pipeline reads
is never touched by this script, so running the sweep does not silently
switch the ongoing pipeline's own behaviour. Idempotent (a second `--apply`
finds nothing new to merge, since absorbed rows are excluded from the next
run's candidate pool) and resumable via `--since-venue-id`.

## Order

Run this AFTER `260812_backfill-misattributed-links.md` has repaired the
~487 mis-attributed `venue_id`s, not merely after `fix/event-attribution-
and-dates` has landed — `venue_id` is half of §D's candidate window, so
measuring before the backfill measures a corpus about to change underneath
the thresholds. See the plan's own Manual/integration Test Plan entry.

Usage:
    python -m scripts.measure_event_dedup                         # dry-run: report only
    python -m scripts.measure_event_dedup --apply                 # sweep: write the auto absorptions
    python -m scripts.measure_event_dedup --apply --since-venue-id V   # resume after venue_id V

Capture the dry-run report BEFORE running --apply. There is no revert for a
row `--apply` writes other than the admin `reverse-merge` action, per pair.
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from typing import Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.models.event_kind import KIND_EVENT
from app.models.promoter_event_visibility import is_promoter_only_item
from app.services import event_dedup
from app.services.event_merge import _venue_name_of, run_title_similarity_pass  # noqa: E501 — the one shared venue-name reader; see its own docstring

logger = logging.getLogger("measure_event_dedup")


@dataclass
class AutoPair:
    venue_id: str
    venue_name: Optional[str]
    surviving_title: str
    absorbed_title: str
    reasons: tuple
    promoter_sourced: bool  # True when EVERY source on BOTH rows is promoter_post


@dataclass
class SuggestPair:
    venue_id: str
    venue_name: Optional[str]
    title_a: str
    title_b: str
    promoter_sourced: bool


@dataclass
class Report:
    venues_considered: int = 0
    candidate_rows: int = 0
    auto_pairs: list = field(default_factory=list)  # [AutoPair]
    suggest_pairs: list = field(default_factory=list)  # [SuggestPair]
    refused_disjoint: int = 0
    refused_no_distinctive_tokens: int = 0

    @property
    def auto_count(self) -> int:
        return len(self.auto_pairs)

    @property
    def suggest_count(self) -> int:
        return len(self.suggest_pairs)

    @property
    def auto_venue_sourced(self) -> int:
        return sum(1 for p in self.auto_pairs if not p.promoter_sourced)

    @property
    def auto_promoter_sourced(self) -> int:
        return sum(1 for p in self.auto_pairs if p.promoter_sourced)

    @property
    def suggest_venue_sourced(self) -> int:
        return sum(1 for p in self.suggest_pairs if not p.promoter_sourced)

    @property
    def suggest_promoter_sourced(self) -> int:
        return sum(1 for p in self.suggest_pairs if p.promoter_sourced)


def _both_promoter_sourced(venue_dao, event_id_a: str, event_id_b: str) -> bool:
    """plan's operator ask (see this module's docstring): whether BOTH rows
    in a pair are promoter-sourced, using the SAME unanimity predicate
    `app.routers.admin_events_router` already applies per row
    (`is_promoter_only_item`) — a pair counts as promoter-sourced only when
    NEITHER side has a shred of venue-post evidence."""
    sources_a = venue_dao.list_event_sources(event_id_a)
    sources_b = venue_dao.list_event_sources(event_id_b)
    return is_promoter_only_item(sources_a) and is_promoter_only_item(sources_b)


def measure(venue_dao, *, config: Optional[event_dedup.DedupConfig] = None) -> Report:
    """Read-only. Groups every `post_type == KIND_EVENT` row by `venue_id`,
    evaluates every same-venue pair inside the candidate window via the
    SAME `event_dedup.evaluate_pair`/`in_candidate_window` the runtime path
    uses, and reports bands. Never calls anything in `app.services.
    event_merge` that writes — this function cannot mutate a row."""
    if config is None:
        # The report always reflects what WOULD happen with the auto band
        # on — that is the entire point of measuring before flipping the
        # flag (plan §B: "Re-measure before setting anything live"). The
        # live admin-config key is never read or written here.
        config = event_dedup.DedupConfig(
            generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
            stopwords=event_dedup.DEFAULT_STOPWORDS,
            lineup_threshold=event_dedup.DEFAULT_LINEUP_THRESHOLD,
            candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
            undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
            auto_merge_enabled=True,
        )

    all_events = [e for e in venue_dao.list_events() if e.get("post_type") == KIND_EVENT and e.get("status") != "superseded"]
    by_venue: dict[str, list[dict]] = {}
    for e in all_events:
        vid = e.get("venue_id")
        if vid:
            by_venue.setdefault(vid, []).append(e)

    report = Report()
    venue_name_cache: dict[str, Optional[str]] = {}
    for venue_id, rows in by_venue.items():
        if len(rows) < 2:
            continue
        report.venues_considered += 1
        report.candidate_rows += len(rows)
        venue_name = venue_name_cache.setdefault(venue_id, _venue_name_of(venue_dao, venue_id))

        for a, b in combinations(rows, 2):
            if not event_dedup.in_candidate_window(
                a.get("starts_at"), b.get("starts_at"), window_hours=config.candidate_window_hours,
            ):
                continue
            decision = event_dedup.evaluate_pair(a, b, venue_name=venue_name, config=config)
            if decision is None:
                venue_tokens = event_dedup.venue_name_tokens(venue_name)
                set_a = event_dedup.distinctive_set(
                    a.get("title"), venue_tokens=venue_tokens,
                    generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
                )
                set_b = event_dedup.distinctive_set(
                    b.get("title"), venue_tokens=venue_tokens,
                    generic_vocabulary=config.generic_vocabulary, stopwords=config.stopwords,
                )
                if not set_a or not set_b:
                    report.refused_no_distinctive_tokens += 1
                else:
                    report.refused_disjoint += 1
                continue

            promoter = _both_promoter_sourced(venue_dao, a["event_id"], b["event_id"])
            if decision.band == event_dedup.BAND_AUTO:
                report.auto_pairs.append(AutoPair(
                    venue_id=venue_id, venue_name=venue_name,
                    surviving_title=a.get("title"), absorbed_title=b.get("title"),
                    reasons=decision.reasons, promoter_sourced=promoter,
                ))
            else:
                report.suggest_pairs.append(SuggestPair(
                    venue_id=venue_id, venue_name=venue_name,
                    title_a=a.get("title"), title_b=b.get("title"), promoter_sourced=promoter,
                ))
    return report


def _print_report(report: Report, *, applied: bool) -> None:
    mode = "APPLY" if applied else "DRY RUN"
    logger.info("=== measure_event_dedup (%s) ===", mode)
    logger.info(
        "venues considered: %d | candidate rows: %d",
        report.venues_considered, report.candidate_rows,
    )
    logger.info(
        "auto pairs: %d (venue-sourced: %d, promoter-sourced: %d)",
        report.auto_count, report.auto_venue_sourced, report.auto_promoter_sourced,
    )
    logger.info(
        "suggest pairs: %d (venue-sourced: %d, promoter-sourced: %d)",
        report.suggest_count, report.suggest_venue_sourced, report.suggest_promoter_sourced,
    )
    logger.info(
        "refused: disjoint=%d no_distinctive_tokens=%d",
        report.refused_disjoint, report.refused_no_distinctive_tokens,
    )
    logger.info("-- auto pairs (surviving <- absorbed, reasons, promoter-sourced) --")
    for p in report.auto_pairs:
        logger.info(
            "  [%s] %r <- %r  reasons=%s promoter_sourced=%s",
            p.venue_name or p.venue_id, p.surviving_title, p.absorbed_title, p.reasons, p.promoter_sourced,
        )
    logger.info("-- suggest pairs (title_a / title_b, promoter-sourced) --")
    for p in report.suggest_pairs:
        logger.info(
            "  [%s] %r / %r  promoter_sourced=%s",
            p.venue_name or p.venue_id, p.title_a, p.title_b, p.promoter_sourced,
        )


def sweep(venue_dao, *, since_venue_id: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """`--apply`: calls `app.services.event_merge.run_title_similarity_pass`
    for every venue with 2+ `post_type == KIND_EVENT` rows, forcing
    `auto_merge_enabled=True` for THIS call only (never touching the live
    admin-config key) and `record_suggestions=False` (plan §C2). Idempotent:
    a row this call absorbs is excluded from the NEXT call's candidate list
    (its status moves to `superseded`), so a second `--apply` finds nothing
    left to merge. Resumable via `since_venue_id` (plain string comparison
    over venue_id, matching this repo's other resumable scripts — venue_id
    is a ULID here too). Returns `{venues_swept: int}`.
    """
    now = now or datetime.now(timezone.utc)
    config = event_dedup.DedupConfig(
        generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
        stopwords=event_dedup.DEFAULT_STOPWORDS,
        lineup_threshold=event_dedup.DEFAULT_LINEUP_THRESHOLD,
        candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
        undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
        auto_merge_enabled=True,
    )
    all_events = [e for e in venue_dao.list_events() if e.get("post_type") == KIND_EVENT and e.get("status") != "superseded"]
    venue_counts: Counter = Counter(e["venue_id"] for e in all_events if e.get("venue_id"))
    venue_ids = sorted(
        vid for vid, count in venue_counts.items()
        if count >= 2 and (since_venue_id is None or vid > since_venue_id)
    )
    for venue_id in venue_ids:
        run_title_similarity_pass(
            venue_dao, venue_id, now, config=config, record_suggestions=False,
        )
    return {"venues_swept": len(venue_ids)}


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Measure the event-dedup fuzzy-title/shared-lineup bands across the "
        "existing corpus (dry-run report), and sweep the auto band once with --apply.",
    )
    ap.add_argument("--apply", action="store_true", help="write the auto-band absorptions (default: dry-run report only)")
    ap.add_argument("--since-venue-id", default=None, help="resume: only sweep venues with venue_id greater than this")
    args = ap.parse_args(argv)

    venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))

    report = measure(venue_dao)
    _print_report(report, applied=False)

    if args.apply:
        result = sweep(venue_dao, since_venue_id=args.since_venue_id)
        logger.info("swept %d venue(s)", result["venues_swept"])
        after = measure(venue_dao)
        logger.info("=== post-sweep re-measurement (should show 0 remaining auto pairs) ===")
        _print_report(after, applied=True)
        if after.auto_count:
            logger.error(
                "%d auto pair(s) remain after --apply — investigate before re-running",
                after.auto_count,
            )
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
