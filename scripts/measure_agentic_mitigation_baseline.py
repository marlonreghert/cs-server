"""Operator CLI: measure how much of three named failure classes — cross-post
fragmentation, multi-location misattribution, and multi-post-for-the-same-
event duplication — the EXISTING deterministic machinery already catches,
before building any new agentic/LLM infrastructure to chase the residual.

See plans/260913_dedup-agentic-mitigation-discovery.md Phase 2. Mirrors
`scripts/measure_event_dedup.py`'s own shape and discipline exactly (a
`measure()` function callable directly, a thin CLI wrapper, zero writes) —
that script is this one's direct precedent, and this one's mirror-ness is
deliberate: a corpus/production measurement tool whose OWN behaviour is a
guess would undermine the very case it exists to make.

## Read-only, always

This script opens no write transaction anywhere in its code path. Every
function it defines either takes already-in-memory corpus data (pure, no DAO
at all) or calls a DAO/service READ (`venue_dao.list_events`,
`venue_dao.list_instagram_handles`, `app.services.event_dedup_backlog.
collect_dedup_backlog` — itself documented PURE-plus-one-read-only-adapter —
`app.services.event_dedup.evaluate_pair`, `app.services.
event_attribution_dispute.evaluate_attribution_dispute`). There is no
`--apply` flag, no `sweep`, and no call anywhere to `app.services.event_merge`
's absorbing paths, `venue_dao.update_event`, `venue_dao.insert_event`, or any
`upsert_*`/`replace_*`/`set_*` DAO method. Verify this claim yourself: grep
this file for any of those names and find none.

## Corpus vs production — never conflated

Every number this script reports is labelled with WHERE it came from:
`source: "corpus"` (Phase 1's committed `tests/fixtures/dedup_agentic_
discovery/corpus.json`, a small, fully controlled, hand-labelled set of real
incident cases) or `source: "production"` (a live read against the real
catalog/event table, run through the exact same read-only DAO pattern
`scripts/measure_event_dedup.py` and the 260912 fix's own verification steps
already use). A corpus recall number and a production rate answer two
different questions — "does the existing signal work on the cases we already
know about" vs. "how much of the live catalog is currently ambiguous" — and
this script's own `report_to_dict` keeps them in two distinct top-level keys
so no reader can mistake one for the other.

## One predicate, never two

`replay_dedup_signal` imports `app.services.event_dedup.evaluate_pair`
directly — the SAME pure function the runtime merge
(`app.services.event_merge.run_title_similarity_pass`) and `scripts.
measure_event_dedup.measure` both call. `replay_attribution_dispute` imports
`app.services.event_attribution_dispute.evaluate_attribution_dispute`
directly — the SAME function the live extraction path and `app.services.
event_dedup_backlog.collect_dedup_backlog` both call. This script computes
NOTHING with a locally re-derived notion of "these look like the same
event"/"this text disagrees with the mapped venue" — if it disagreed with
the runtime pipeline about a single case, the measurement would stop being
evidence, exactly the argument `event_dedup.py`'s own module docstring makes
for why `evaluate_pair` has one definition and two callers.

Usage:
    python -m scripts.measure_agentic_mitigation_baseline                    # corpus + production
    python -m scripts.measure_agentic_mitigation_baseline --corpus-only      # corpus only, no DB read at all
    python -m scripts.measure_agentic_mitigation_baseline --production-only  # production only
    python -m scripts.measure_agentic_mitigation_baseline --report-json /app/reports/agentic_baseline.json
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.models.event_kind import KIND_EVENT
from app.services import event_dedup
from app.services.event_attribution_dispute import evaluate_attribution_dispute
from app.services.event_dedup_backlog import collect_dedup_backlog
from app.services.event_venue_resolution import VenueLite, extract_mentions
from app.services.instagram_handle_sources import group_venue_ids_by_handle, normalize_handle
from app.services.promoter_registry_service import DEFAULT_MENTION_THRESHOLD
import scripts.measure_event_dedup as measure_event_dedup

logger = logging.getLogger("measure_agentic_mitigation_baseline")

DEFAULT_CORPUS_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests" / "fixtures" / "dedup_agentic_discovery" / "corpus.json"
)

# Two of the codebase's other admin-facing surfaces this plan's own §B asks
# whether operators actually watch. Grep-confirmed against this worktree
# (2026-09-13): neither `app.routers.admin_events_router` nor any DAO table
# records a last-viewed/last-accessed timestamp for either route — there is
# no "operator opened this" signal anywhere in this codebase today. Stated
# here as a fixed, honest finding rather than computed from anything, per
# the plan's own explicit allowance ("else a documented 'unknown — ask the
# operator' line item").
OPERATOR_BACKLOG_ENGAGEMENT_FINDING = (
    "unknown — no view/last-accessed tracking exists for GET "
    "/admin/events/dedup-backlog or GET /admin/events/review in this "
    "codebase as of 2026-09-13 (confirmed: neither route nor any DAO table "
    "records a last-viewed timestamp). This cannot be measured from data "
    "that does not exist; ask the operator directly whether either surface "
    "is part of their actual workflow."
)


# ── corpus loading ───────────────────────────────────────────────────────────
def load_corpus(path=DEFAULT_CORPUS_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _venue_lite_from_case(venue: dict) -> VenueLite:
    return VenueLite(
        venue_id=venue["venue_id"], venue_name=venue["venue_name"],
        lat=venue.get("lat"), lng=venue.get("lng"), address=venue.get("address"),
    )


# ── per-case signal replay (pure — no DAO, no network) ──────────────────────
def replay_dedup_signal(case: dict, *, config: Optional[event_dedup.DedupConfig] = None) -> dict:
    """Every pair of this case's own posts, through the SAME
    `event_dedup.evaluate_pair` the runtime merge and `scripts.
    measure_event_dedup.measure` both call. `auto_merge_enabled=True` here
    is measurement-only, exactly like `scripts.measure_event_dedup.
    build_config`'s own default — it never reads or writes the live
    `admin_config:event_dedup_auto_merge_enabled` key, it only answers "if
    the auto band were on, would this pair reach it"."""
    if config is None:
        config = measure_event_dedup.build_config(auto_merge_enabled=True)
    posts = case.get("posts") or []
    venue_name = (case.get("venue") or {}).get("venue_name")
    bands = {event_dedup.BAND_AUTO: 0, event_dedup.BAND_SUGGEST: 0, event_dedup.BAND_REFUSE: 0}
    pairs = []
    for a, b in combinations(posts, 2):
        decision = event_dedup.evaluate_pair(a, b, venue_name=venue_name, config=config)
        band = decision.band if decision is not None else event_dedup.BAND_REFUSE
        bands[band] += 1
        pairs.append({"a": a.get("title"), "b": b.get("title"), "band": band})
    return {"bands": bands, "pairs": pairs}


def replay_attribution_dispute(case: dict) -> dict:
    """Every one of this case's posts, through the SAME
    `event_attribution_dispute.evaluate_attribution_dispute` the live
    extraction path and `event_dedup_backlog.collect_dedup_backlog` both
    call — against the case's OWN small venue set (the mapped venue plus
    whatever sibling/unrelated venues the case defines), never the whole
    production catalog. A case whose posts carry NO `location_text` at all
    (a pure fragmentation case, e.g. Club Metrópole) is not evaluated here —
    there is nothing for this signal to even read. A case with no sibling/
    unrelated venues defined IS still run: the ladder's
    `METHOD_VENUE_NOT_IN_CATALOG` outcome (a post naming a specific handle
    the corpus's own tiny catalog does not carry) needs no sibling venue at
    all to fire, and skipping the call would silently under-report exactly
    the roundup-account shape this corpus's Natal-venue cases exist to
    measure."""
    venue = case.get("venue") or {}
    if not any(p.get("location_text") for p in (case.get("posts") or [])):
        return {"posts": [], "any_disputed": False, "note": "no post in this case carries a location_text"}
    others = (case.get("sibling_venues") or []) + (case.get("unrelated_venues") or [])

    venues = [_venue_lite_from_case(venue)] + [_venue_lite_from_case(v) for v in others]
    handle_index: dict = {}
    own_handle = case.get("instagram_handle")
    for v in others:
        if v.get("instagram_handle"):
            handle_index[normalize_handle(v["instagram_handle"])] = v["venue_id"]

    results = []
    for post in case.get("posts") or []:
        verdict = evaluate_attribution_dispute(
            mapped_venue_id=venue.get("venue_id"),
            location_text=post.get("location_text"),
            venues=venues, handle_index=handle_index,
            promoter_handle=own_handle,
        )
        results.append({
            "event_id": post.get("event_id"), "location_text": post.get("location_text"),
            "disputed": verdict is not None,
            "method": verdict.method if verdict else None,
            "target_venue_id": verdict.target_venue_id if verdict else None,
        })
    return {"posts": results, "any_disputed": any(r["disputed"] for r in results)}


def replay_promoter_discovery_mentions(
    case: dict, *, threshold: int = DEFAULT_MENTION_THRESHOLD,
) -> dict:
    """Would `PromoterRegistryService.run_discovery`'s mention-counting rule
    have proposed a handle this case's captions mention as a crawl
    candidate? A PURE replay of that rule's counting kernel — the SAME
    `extract_mentions`, the SAME distinct-per-post counting, the SAME
    exclusion of the posting handle's own mentions of itself — over the
    case's own captions. Not a call to `run_discovery` itself: that method
    is async and DAO/post-source-bound (it also excludes already-known
    catalog handles and already-registered accounts, neither of which a
    bounded corpus case has a meaningful stand-in for), so this is
    deliberately the smaller, corpus-appropriate slice of its logic — the
    exact arithmetic question Phase 2 needs answered, not a DAO
    integration test."""
    own_handle = normalize_handle(case.get("instagram_handle"))
    counts: dict = {}
    for post in case.get("posts") or []:
        caption = post.get("caption")
        if not caption:
            continue
        for mention in set(extract_mentions(caption)):
            if mention == own_handle:
                continue
            counts[mention] = counts.get(mention, 0) + 1
    proposed = sorted(h for h, c in counts.items() if c >= threshold)
    return {"mention_counts": counts, "threshold": threshold, "proposed_candidates": proposed}


@dataclass
class CaseResult:
    case_id: str
    failure_class: str
    synthetic: bool
    caught_by: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    # Only meaningful for failure_class="non_duplicate_control": True iff
    # `event_dedup.evaluate_pair` reached BAND_AUTO for at least one pair of
    # this case's own posts — i.e. the existing signal WRONGLY auto-merged a
    # pair the plan's own false-positive corpus says must stay apart. `None`
    # for every other failure class, where "no existing signal fired" is the
    # finding this plan exists to measure, not a malfunction.
    false_positive: Optional[bool] = None


def replay_case(case: dict) -> CaseResult:
    """The whole per-case replay: every existing deterministic signal this
    plan's Evidence section names, run against one corpus case, reporting
    which (if any) would have caught it. `caught_by` is empty exactly when
    every existing signal is silent — that IS the finding for a case like
    the unrelated-venue gap, not a script bug."""
    caught_by: list = []
    detail: dict = {}

    dedup = replay_dedup_signal(case)
    detail["dedup_bands"] = dedup
    if dedup["bands"][event_dedup.BAND_AUTO] > 0:
        caught_by.append("event_dedup.evaluate_pair(band=auto)")

    dispute = replay_attribution_dispute(case)
    detail["attribution_dispute"] = dispute
    if dispute["any_disputed"]:
        caught_by.append("event_attribution_dispute.evaluate_attribution_dispute")

    mentions = replay_promoter_discovery_mentions(case)
    detail["promoter_discovery_mentions"] = mentions
    if mentions["proposed_candidates"]:
        caught_by.append("promoter_registry_service.run_discovery(mention_threshold)")

    failure_class = case["failure_class"]
    false_positive = (
        dedup["bands"][event_dedup.BAND_AUTO] > 0
        if failure_class == "non_duplicate_control" else None
    )

    return CaseResult(
        case_id=case["id"], failure_class=failure_class,
        synthetic=bool(case.get("synthetic")), caught_by=caught_by, detail=detail,
        false_positive=false_positive,
    )


def measure_corpus(corpus: dict) -> list:
    """Read-only, pure — no DAO, no `venue_dao` parameter, because a
    committed JSON corpus needs none. One `CaseResult` per case in
    `corpus["cases"]`, in the corpus's own order."""
    return [replay_case(c) for c in corpus.get("cases", [])]


# ── production-only measures (read-only DAO calls) ──────────────────────────
@dataclass
class ProductionReport:
    live_events: int = 0
    resolution_queued: int = 0
    band_suggest_pairs: int = 0
    pending_merge_suggestions: int = 0
    attribution_disputed_rows: int = 0
    instagram_handles_total: int = 0
    handles_mapping_to_multiple_venues: int = 0
    handles_mapping_to_multiple_venues_sample: dict = field(default_factory=dict)
    operator_backlog_engagement: str = OPERATOR_BACKLOG_ENGAGEMENT_FINDING

    @property
    def ambiguous_band_events(self) -> int:
        """RESOLUTION_QUEUED events plus events sitting in a pending merge
        suggestion — the two per-EVENT ambiguous states. `band_suggest_pairs`
        is deliberately excluded from this sum: it counts PAIRS
        (`scripts.measure_event_dedup.measure`'s own unit), which can be
        more than one per event and would double-count against a per-event
        denominator. It is reported separately, never folded in."""
        return self.resolution_queued + self.pending_merge_suggestions

    @property
    def ambiguous_fraction(self) -> Optional[float]:
        if not self.live_events:
            return None
        return round(self.ambiguous_band_events / self.live_events, 4)


def measure(venue_dao, *, redis_like=None) -> ProductionReport:
    """Read-only, against the real catalog. Every call here is a read:
    `venue_dao.list_events`/`list_instagram_handles`, `scripts.
    measure_event_dedup.measure` (itself documented read-only, dry-run
    default), and `app.services.event_dedup_backlog.collect_dedup_backlog`
    (itself documented PURE-plus-one-read-only-adapter). Nothing here can
    mutate a row — verify by grepping this function for `update_`/`insert_`/
    `upsert_`/`replace_`/`set_` and finding none."""
    events = venue_dao.list_events() or []
    live = [e for e in events if e.get("post_type") == KIND_EVENT and e.get("status") != "superseded"]
    queued = [
        e for e in live
        if e.get("location_resolution") is None and e.get("venue_id") is None
    ]

    dedup_report = measure_event_dedup.measure(venue_dao)

    backlog = collect_dedup_backlog(venue_dao, redis_like=redis_like)

    handles = venue_dao.list_instagram_handles() or {}
    by_handle = group_venue_ids_by_handle(handles)
    multi = {h: ids for h, ids in by_handle.items() if len(ids) > 1}

    return ProductionReport(
        live_events=len(live),
        resolution_queued=len(queued),
        band_suggest_pairs=dedup_report.suggest_count,
        pending_merge_suggestions=backlog.pending_suggestions,
        attribution_disputed_rows=backlog.attribution_disputed_rows,
        instagram_handles_total=len(handles),
        handles_mapping_to_multiple_venues=len(multi),
        handles_mapping_to_multiple_venues_sample=dict(list(multi.items())[:20]),
    )


# ── reporting ────────────────────────────────────────────────────────────────
def report_to_dict(
    *, corpus_results: Optional[list] = None, production_report: Optional[ProductionReport] = None,
) -> dict:
    out: dict = {"generated_by": "scripts.measure_agentic_mitigation_baseline"}
    if corpus_results is not None:
        out["corpus"] = {
            "source": "corpus",
            "case_count": len(corpus_results),
            # "No existing signal fired" is a FINDING only for the three
            # named failure classes — for a non_duplicate_control case it is
            # the CORRECT, expected outcome (nothing should fire), so
            # controls are excluded here and judged by `false_positives`
            # instead.
            "cases_with_no_existing_signal": [
                r.case_id for r in corpus_results
                if not r.caught_by and not r.synthetic and r.failure_class != "non_duplicate_control"
            ],
            # A non-empty list here would be a SAFETY regression: an
            # existing signal auto-merging a pair the plan's own
            # false-positive corpus says must stay apart.
            "false_positives": [
                r.case_id for r in corpus_results if r.false_positive
            ],
            "cases": [asdict(r) for r in corpus_results],
        }
    if production_report is not None:
        out["production"] = {"source": "production", **asdict(production_report)}
    return out


def _print_report(
    *, corpus_results: Optional[list] = None, production_report: Optional[ProductionReport] = None,
) -> None:
    if corpus_results is not None:
        logger.info("=== corpus replay (source=corpus) ===")
        for r in corpus_results:
            logger.info(
                "  [%s] failure_class=%s synthetic=%s caught_by=%s%s",
                r.case_id, r.failure_class, r.synthetic, r.caught_by or "NONE",
                "  *** FALSE POSITIVE ***" if r.false_positive else "",
            )
    if production_report is not None:
        p = production_report
        logger.info("=== production measurement (source=production) ===")
        logger.info("live extracted events: %d", p.live_events)
        logger.info(
            "RESOLUTION_QUEUED events: %d | pending merge suggestions: %d | "
            "attribution-disputed rows: %d",
            p.resolution_queued, p.pending_merge_suggestions, p.attribution_disputed_rows,
        )
        logger.info(
            "ambiguous-band events (queued + pending suggestion): %d (%s of live events)",
            p.ambiguous_band_events,
            f"{p.ambiguous_fraction:.2%}" if p.ambiguous_fraction is not None else "n/a",
        )
        logger.info("dedup BAND_SUGGEST pairs (whole-catalog, from measure_event_dedup): %d", p.band_suggest_pairs)
        logger.info(
            "instagram handles: %d total, %d map to >1 venue_id",
            p.instagram_handles_total, p.handles_mapping_to_multiple_venues,
        )
        logger.info("operator backlog engagement: %s", p.operator_backlog_engagement)


def write_report_json(path: str, payload: dict) -> None:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    logger.info("wrote report JSON to %s", target)


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Measure how much of the three named agentic-mitigation-discovery "
        "failure classes the EXISTING deterministic machinery already catches. Read-only.",
    )
    ap.add_argument("--corpus-only", action="store_true", help="measure the corpus only, no database read at all")
    ap.add_argument("--production-only", action="store_true", help="measure production only, skip the corpus")
    ap.add_argument("--corpus-path", default=str(DEFAULT_CORPUS_PATH), help="path to the labeled corpus JSON")
    ap.add_argument("--report-json", default=None, metavar="PATH", help="write the full report as JSON")
    args = ap.parse_args(argv)

    corpus_results = None
    production_report = None

    if not args.production_only:
        corpus = load_corpus(args.corpus_path)
        corpus_results = measure_corpus(corpus)

    if not args.corpus_only:
        venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))
        production_report = measure(venue_dao)

    _print_report(corpus_results=corpus_results, production_report=production_report)

    if args.report_json:
        write_report_json(
            args.report_json,
            report_to_dict(corpus_results=corpus_results, production_report=production_report),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
