"""Operator CLI: measure the venue-handle link audit — dry-run report.

See plans/260913_venue-handle-link-audit.md. Mirrors
`scripts/measure_agentic_mitigation_baseline.py`'s own structure: a
`measure_corpus()` function replaying the labeled fixture corpus (no DAO,
no network), a `measure()` function reading real production data
read-only, and a thin CLI wrapper. No writes anywhere in this file —
`compute_venue_link_audit`/`collect_venue_link_audit` are themselves
read-only (see their own docstrings), and this script adds no write path
of its own.

Run against the fixture corpus only (no database needed):
    python -m scripts.measure_venue_link_audit --corpus-only

Run against real production data (read-only):
    python -m scripts.measure_venue_link_audit
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.services.instagram_handle_sources import group_venue_ids_by_handle
from app.services.venue_link_audit import collect_venue_link_audit, compute_venue_link_audit

logger = logging.getLogger(__name__)

DEFAULT_CORPUS_PATH = Path(__file__).resolve().parent.parent / "tests/fixtures/venue_link_audit/corpus.json"


def load_corpus(path=DEFAULT_CORPUS_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def replay_case(case: dict) -> dict:
    """One corpus case through the REAL `compute_venue_link_audit` — never
    a parallel, simplified re-implementation of the comparison. Builds the
    exact `targets`/`events_by_handle` shape that function expects from the
    case's own `mapped_venues`/`events`."""
    target = {"handle": case["handle"], "mapped_venues": case["mapped_venues"]}
    events_by_handle = {case["handle"]: case["events"]}
    candidates = compute_venue_link_audit([target], events_by_handle)

    flagged_venue_ids: list = []
    for c in candidates:
        if c.handle != case["handle"]:
            continue
        flagged_venue_ids.extend(v.venue_id for v in c.mapped_venues if v.flagged)

    expected = sorted(case.get("expected_flagged_venue_ids") or [])
    actual = sorted(flagged_venue_ids)
    return {
        "case_id": case["case_id"],
        "handle": case["handle"],
        "flagged": bool(flagged_venue_ids),
        "flagged_venue_ids": actual,
        "expected_flagged_venue_ids": expected,
        "matches_expectation": actual == expected,
    }


def measure_corpus(corpus: Optional[dict] = None, *, path=DEFAULT_CORPUS_PATH) -> dict:
    """Read-only, pure — no DAO, no network. `corpus=None` (the convenience
    BDD steps use) loads the committed fixture at `path`; a caller with its
    own in-memory corpus (a unit test probing a synthetic case) can pass one
    directly. Keyed by `case_id`, one verdict per corpus case."""
    if corpus is None:
        corpus = load_corpus(path)
    return {r["case_id"]: r for r in (replay_case(c) for c in corpus.get("cases", []))}


@dataclass
class ProductionMeasurement:
    total_kind_venue_targets: int = 0
    targets_with_zero_mapped_venues: int = 0
    targets_checked: int = 0
    flagged_handle_count: int = 0
    flagged_venue_pairs: int = 0
    candidates: list = field(default_factory=list)  # [dict] — VenueLinkAuditCandidate.to_dict()


def measure(venue_dao, *, redis_like=None) -> ProductionMeasurement:
    """Read-only, against the real catalog. Every call here is a read:
    `venue_dao.list_crawl_targets`/`collect_venue_link_audit` (itself
    documented read-only — see its own docstring). Nothing here can mutate
    a row — verify by grepping this function for `insert_`/`update_`/
    `upsert_`/`replace_`/`set_` and finding none."""
    all_targets = venue_dao.list_crawl_targets(kind="venue") or []
    handles_map = venue_dao.list_instagram_handles() or {}
    by_handle = group_venue_ids_by_handle(handles_map)
    zero_mapped = sum(1 for t in all_targets if not by_handle.get(t["handle"]))

    candidates = collect_venue_link_audit(venue_dao, redis_like=redis_like)
    flagged_pairs = sum(
        1 for c in candidates for v in c.mapped_venues if v.flagged
    )

    return ProductionMeasurement(
        total_kind_venue_targets=len(all_targets),
        targets_with_zero_mapped_venues=zero_mapped,
        targets_checked=len(all_targets) - zero_mapped,
        flagged_handle_count=len(candidates),
        flagged_venue_pairs=flagged_pairs,
        candidates=[c.to_dict() for c in candidates],
    )


# ── reporting ────────────────────────────────────────────────────────────────
def report_to_dict(
    *, corpus_results: Optional[dict] = None, production: Optional[ProductionMeasurement] = None,
) -> dict:
    out: dict = {"generated_by": "scripts.measure_venue_link_audit"}
    if corpus_results is not None:
        out["corpus"] = {
            "source": "corpus",
            "case_count": len(corpus_results),
            "mismatches": [
                case_id for case_id, r in corpus_results.items() if not r["matches_expectation"]
            ],
            "cases": corpus_results,
        }
    if production is not None:
        out["production"] = {"source": "production", **asdict(production)}
    return out


def _print_report(
    *, corpus_results: Optional[dict] = None, production: Optional[ProductionMeasurement] = None,
) -> None:
    if corpus_results is not None:
        logger.info("=== corpus replay (source=corpus) ===")
        for case_id, r in corpus_results.items():
            ok = "OK" if r["matches_expectation"] else "*** MISMATCH ***"
            logger.info(
                "  [%s] handle=%s flagged=%s flagged_venue_ids=%s expected=%s  %s",
                case_id, r["handle"], r["flagged"], r["flagged_venue_ids"],
                r["expected_flagged_venue_ids"], ok,
            )
    if production is not None:
        p = production
        logger.info("=== production measurement (source=production) ===")
        logger.info(
            "kind='venue' crawl targets: %d total, %d with zero mapped venues (skipped), %d checked",
            p.total_kind_venue_targets, p.targets_with_zero_mapped_venues, p.targets_checked,
        )
        logger.info(
            "flagged: %d handle(s), %d (handle, venue) pair(s)",
            p.flagged_handle_count, p.flagged_venue_pairs,
        )
        for c in p.candidates:
            flagged = [v for v in c["mapped_venues"] if v["flagged"]]
            logger.info(
                "  [%s] %s",
                c["handle"],
                [(v["venue_id"], v["venue_name"], v["non_corroborating_count"], v["checkable_count"]) for v in flagged],
            )


def write_report_json(path: str, payload: dict) -> None:
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    logger.info("wrote report JSON to %s", target)


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(
        description="Measure which kind='venue' crawl targets the venue-handle "
        "link audit would flag. Read-only.",
    )
    ap.add_argument("--corpus-only", action="store_true", help="measure the fixture corpus only, no database read at all")
    ap.add_argument("--production-only", action="store_true", help="measure production only, skip the corpus")
    ap.add_argument("--corpus-path", default=str(DEFAULT_CORPUS_PATH), help="path to the labeled corpus JSON")
    ap.add_argument("--report-json", default=None, metavar="PATH", help="write the full report as JSON")
    args = ap.parse_args(argv)

    corpus_results = None
    production = None

    if not args.production_only:
        corpus_results = measure_corpus(path=args.corpus_path)

    if not args.corpus_only:
        venue_dao = VenueRepository(client=None, rds_store=RdsVenueStore(settings.rds_sqlalchemy_url))
        production = measure(venue_dao)

    _print_report(corpus_results=corpus_results, production=production)

    if args.report_json:
        write_report_json(
            args.report_json,
            report_to_dict(corpus_results=corpus_results, production=production),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
