"""Operator CLI: measure the fuzzy-title/shared-lineup dedup bands across the
existing corpus (dry-run report), and — with `--apply` — sweep the AUTO band
over it once, historically.

See plans/260812_event-dedup-fuzzy-title.md §B ("Ship the measurement as a
script under scripts/... run it against a restored snapshot after fix/event-
attribution-and-dates lands, and put the post-fix numbers in the PR") and §C2
("ship an apply-mode sweep alongside the read-only measure_event_dedup.py —
same predicate, same bands, same config, same choose_canonical/
merge_event_fields/_finish_absorption path"), extended by
plans/260912_events-venue-night-duplication.md §B with the knobs that let
this script ANSWER a question instead of restating its own defaults.

## One predicate, never two

Dry-run measurement and `--apply` both import `app.services.event_dedup.
evaluate_pair`/`in_candidate_window_for_rows` — the SAME pure functions
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

## The measurement knobs (260912 §B)

Additive flags only — the DEFAULT invocation behaves exactly as it did
before they existed, and NONE of them reads or writes any admin-config key:

  --lineup-threshold N        force §B2's shared-lineup floor for this run
                              (the RCA's "cheapest experiment first": ask
                              what threshold 1 would actually merge before
                              deciding whether to move the live key)
  --recurring-window /        measure with §D's widened candidate window on
  --no-recurring-window       or off, so the threshold question is asked
                              against the window production will USE
  --single-night-venue ID     measure §E2's per-venue policy for named
                              venues WITHOUT writing the admin-config key
                              (repeatable)
  --max-auto-pairs N          THE BLAST-RADIUS GUARD. In --apply mode, write
                              nothing and exit non-zero when the dry-run
                              report's auto-pair count exceeds N. This is
                              what makes an unattended --apply safe: the
                              sweep stops on a surprise instead of merging
                              its way through one.
  --report-json PATH          the full report as JSON (counts plus every
                              auto and suggest pair, with both titles and
                              both event ids), so a before/after diff — or a
                              threshold-1-vs-threshold-2 diff — is
                              mechanical rather than a log read. In --apply
                              mode the POST-sweep re-measurement is written
                              alongside it as `<stem>.after<suffix>`.

## Order

Run this AFTER `260812_backfill-misattributed-links.md` has repaired the
~487 mis-attributed `venue_id`s, not merely after `fix/event-attribution-
and-dates` has landed — `venue_id` is half of §D's candidate window, so
measuring before the backfill measures a corpus about to change underneath
the thresholds. 260912 §F repeats the same rule for its own attribution
repair: attribution repair -> re-measure -> dedup sweep, in that order.

Usage:
    python -m scripts.measure_event_dedup                         # dry-run: report only
    python -m scripts.measure_event_dedup --lineup-threshold 1 --recurring-window \
        --report-json /app/reports/dedup_t1.json                  # dry-run: a threshold experiment
    python -m scripts.measure_event_dedup --apply                 # sweep: write the auto absorptions
    python -m scripts.measure_event_dedup --apply --since-venue-id V   # resume after venue_id V

Capture the dry-run report BEFORE running --apply. There is no revert for a
row `--apply` writes other than the admin `reverse-merge` action, per pair.

Exit codes: 0 ok; 1 auto pairs remain after --apply; 6 the auto-pair ceiling
was exceeded and NOTHING was written.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.models.event_kind import KIND_EVENT
from app.models.promoter_event_visibility import is_promoter_only_item
from app.services import event_dedup
from app.services.event_merge import _venue_name_of, run_title_similarity_pass  # noqa: E501 — the one shared venue-name reader; see its own docstring

logger = logging.getLogger("measure_event_dedup")

# `--apply` refused to write because the dry-run pass found more auto pairs
# than `--max-auto-pairs` allows. Distinct from 1 ("the sweep ran and left
# work behind") so an unattended runbook can tell "stopped before touching
# anything" from "wrote, then something is still wrong".
EXIT_CEILING_EXCEEDED = 6


def build_config(
    *,
    lineup_threshold: Optional[int] = None,
    recurring_window_enabled: bool = False,
    single_night_venue_ids=(),
    auto_merge_enabled: bool = True,
) -> event_dedup.DedupConfig:
    """The config every path in this script measures/sweeps with, built from
    the SHIPPED DEFAULTS plus this invocation's own explicit overrides —
    never from the live admin-config keys. That is deliberate and is the
    whole reason the dry run is safe to hand to an unattended runbook: the
    report always reflects what WOULD happen with the auto band on (plan §B:
    "Re-measure before setting anything live"), and running it can neither
    read nor change what the ongoing pipeline does."""
    return event_dedup.DedupConfig(
        generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
        stopwords=event_dedup.DEFAULT_STOPWORDS,
        lineup_threshold=(
            event_dedup.DEFAULT_LINEUP_THRESHOLD if lineup_threshold is None
            else lineup_threshold
        ),
        candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
        undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
        auto_merge_enabled=auto_merge_enabled,
        recurring_window_enabled=recurring_window_enabled,
        single_night_venues=tuple(single_night_venue_ids or ()),
    )


@dataclass
class AutoPair:
    venue_id: str
    venue_name: Optional[str]
    surviving_title: str
    absorbed_title: str
    reasons: tuple
    promoter_sourced: bool  # True when EVERY source on BOTH rows is promoter_post
    # 260912 §E1's decision input is "every auto pair present at threshold 1
    # and not at 2". Diffing that by TITLE alone is ambiguous the moment one
    # venue runs two same-titled nights; the ids make the diff exact.
    surviving_event_id: Optional[str] = None
    absorbed_event_id: Optional[str] = None


@dataclass
class SuggestPair:
    venue_id: str
    venue_name: Optional[str]
    title_a: str
    title_b: str
    promoter_sourced: bool
    event_id_a: Optional[str] = None
    event_id_b: Optional[str] = None


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
    SAME `event_dedup.evaluate_pair`/`in_candidate_window_for_rows` the
    runtime path uses, and reports bands. Never calls anything in
    `app.services.event_merge` that writes — this function cannot mutate a
    row."""
    if config is None:
        config = build_config()

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
                    surviving_event_id=a["event_id"], absorbed_event_id=b["event_id"],
                ))
            else:
                report.suggest_pairs.append(SuggestPair(
                    venue_id=venue_id, venue_name=venue_name,
                    title_a=a.get("title"), title_b=b.get("title"), promoter_sourced=promoter,
                    event_id_a=a["event_id"], event_id_b=b["event_id"],
                ))
    return report


def report_to_dict(report: Report, *, config: event_dedup.DedupConfig, applied: bool) -> dict:
    """`--report-json`'s payload. Carries the CONFIG it was measured with —
    a threshold comparison whose files cannot say which threshold produced
    them is not evidence, it is two numbers."""
    return {
        "mode": "apply" if applied else "dry_run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "lineup_threshold": config.lineup_threshold,
            "candidate_window_hours": config.candidate_window_hours,
            "undated_window_days": config.undated_window_days,
            "recurring_window_enabled": config.recurring_window_enabled,
            "single_night_venues": list(config.single_night_venues),
        },
        "counts": {
            "venues_considered": report.venues_considered,
            "candidate_rows": report.candidate_rows,
            "auto_pairs": report.auto_count,
            "auto_venue_sourced": report.auto_venue_sourced,
            "auto_promoter_sourced": report.auto_promoter_sourced,
            "suggest_pairs": report.suggest_count,
            "suggest_venue_sourced": report.suggest_venue_sourced,
            "suggest_promoter_sourced": report.suggest_promoter_sourced,
            "refused_disjoint": report.refused_disjoint,
            "refused_no_distinctive_tokens": report.refused_no_distinctive_tokens,
        },
        "auto_pairs": [{**asdict(p), "reasons": list(p.reasons)} for p in report.auto_pairs],
        "suggest_pairs": [asdict(p) for p in report.suggest_pairs],
    }


def write_report_json(path: str, report: Report, *, config: event_dedup.DedupConfig, applied: bool) -> None:
    payload = report_to_dict(report, config=config, applied=applied)
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    logger.info("wrote report JSON to %s", target)


def _after_report_path(path: str) -> str:
    target = Path(path)
    return str(target.with_name(f"{target.stem}.after{target.suffix or '.json'}"))


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


def sweep(
    venue_dao, *, since_venue_id: Optional[str] = None, now: Optional[datetime] = None,
    config: Optional[event_dedup.DedupConfig] = None,
    lineup_threshold: Optional[int] = None,
    recurring_window_enabled: bool = False,
    single_night_venue_ids=(),
) -> dict:
    """`--apply`: calls `app.services.event_merge.run_title_similarity_pass`
    for every venue with 2+ `post_type == KIND_EVENT` rows, forcing
    `auto_merge_enabled=True` for THIS call only (never touching the live
    admin-config key) and `record_suggestions=False` (plan §C2). Idempotent:
    a row this call absorbs is excluded from the NEXT call's candidate list
    (its status moves to `superseded`), so a second `--apply` finds nothing
    left to merge. Resumable via `since_venue_id` (plain string comparison
    over venue_id, matching this repo's other resumable scripts — venue_id
    is a ULID here too). Returns `{venues_swept: int}`.

    `config`, when given, is used AS-IS; otherwise one is built from the
    shipped defaults plus this call's own explicit overrides — the SAME
    `build_config` the dry-run measurement uses, so the sweep can never act
    on a different bar than the report an operator just read.
    """
    now = now or datetime.now(timezone.utc)
    if config is None:
        config = build_config(
            lineup_threshold=lineup_threshold,
            recurring_window_enabled=recurring_window_enabled,
            single_night_venue_ids=single_night_venue_ids,
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
    ap.add_argument(
        "--lineup-threshold", type=int, default=None, metavar="N",
        help="force the shared-lineup floor for this run (default: the shipped %d); "
             "never reads or writes the live admin-config key"
             % event_dedup.DEFAULT_LINEUP_THRESHOLD,
    )
    ap.add_argument(
        "--recurring-window", dest="recurring_window", action="store_true", default=False,
        help="measure/sweep with the recurring-aware candidate window ON",
    )
    ap.add_argument(
        "--no-recurring-window", dest="recurring_window", action="store_false",
        help="measure/sweep with the recurring-aware candidate window OFF (the default)",
    )
    ap.add_argument(
        "--single-night-venue", action="append", default=[], metavar="VENUE_ID",
        help="treat this venue as running one night rather than a programme, for THIS run "
             "only (repeatable); never writes the admin-config key",
    )
    ap.add_argument(
        "--max-auto-pairs", type=int, default=None, metavar="N",
        help="blast-radius guard: with --apply, write NOTHING and exit non-zero when the "
             "dry-run report finds more than N auto pairs",
    )
    ap.add_argument(
        "--report-json", default=None, metavar="PATH",
        help="write the full report (counts plus every auto and suggest pair) as JSON; "
             "with --apply the post-sweep re-measurement is written to <stem>.after<suffix>",
    )
    args = ap.parse_args(argv)

    venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))

    config = build_config(
        lineup_threshold=args.lineup_threshold,
        recurring_window_enabled=args.recurring_window,
        single_night_venue_ids=args.single_night_venue,
    )
    logger.info(
        "config: lineup_threshold=%d recurring_window=%s single_night_venues=%s",
        config.lineup_threshold, config.recurring_window_enabled,
        list(config.single_night_venues) or "[]",
    )

    report = measure(venue_dao, config=config)
    _print_report(report, applied=False)
    if args.report_json:
        write_report_json(args.report_json, report, config=config, applied=False)

    if args.apply:
        if args.max_auto_pairs is not None and report.auto_count > args.max_auto_pairs:
            # Nothing has been written at this point — `measure` cannot
            # mutate a row — so stopping here leaves the corpus exactly as
            # the report above describes it.
            logger.error(
                "REFUSING TO APPLY: the dry-run pass found %d auto pair(s), above the "
                "--max-auto-pairs ceiling of %d. Nothing was written. Review the report "
                "above (and --report-json, if given) before raising the ceiling.",
                report.auto_count, args.max_auto_pairs,
            )
            return EXIT_CEILING_EXCEEDED

        result = sweep(venue_dao, since_venue_id=args.since_venue_id, config=config)
        logger.info("swept %d venue(s)", result["venues_swept"])
        after = measure(venue_dao, config=config)
        logger.info("=== post-sweep re-measurement (should show 0 remaining auto pairs) ===")
        _print_report(after, applied=True)
        if args.report_json:
            write_report_json(
                _after_report_path(args.report_json), after, config=config, applied=True,
            )
        if after.auto_count:
            logger.error(
                "%d auto pair(s) remain after --apply — investigate before re-running",
                after.auto_count,
            )
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
