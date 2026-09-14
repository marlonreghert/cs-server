"""Operator CLI: run the audit-sourced venue-link reviewer over the flagged
`(handle, venue)` pairs — plans/260914_agentic-venue-resolution-fallback.md §7.

## Dry-run by default, and that is the point

A dry run makes every model call and reaches every verdict but writes
nothing. That is the shape the FIRST production run is meant to take: an
operator reads the full verdict log against live data before anything is ever
suppressed. `--apply` is the deliberate second step.

Note the two-layer gate. `--apply` only permits the write; whether a verdict
may actually SUPPRESS a flag is still governed by the
`venue_link_audit_reviewer_auto_apply_enabled` admin-config key. With that
key false, `--apply` records verdicts and suppresses nothing — shadow mode.
Neither layer can be bypassed from the other.

## What it can and cannot touch

Reads `events.post_item` / `events.post_item_source` / `venues.venue` /
`venues.address` / `events.crawl_target` / `instagram.handle`. Writes exactly
one table, `events.venue_link_audit_review`. It cannot write an event row:
nothing in `app.services.venue_link_audit_reviewer` calls `update_event`, and
this module imports no other write path.

Spends OpenAI budget: `DEFAULT_REVIEW_CONSENSUS_K` (10) calls per reviewed
pair. A pair that is skipped (multi-mapped, operator-decided, already
confirmed, or fully protected) costs zero calls. At 12 flagged pairs today,
a full run is ~90 calls once the skips are taken out.

Usage:
    python -m scripts.review_venue_link_audit                      # dry-run, all pairs
    python -m scripts.review_venue_link_audit --handle lacasarecife
    python -m scripts.review_venue_link_audit --apply              # persist verdicts
    python -m scripts.review_venue_link_audit --report-json out.json

Exit codes: 0 ok; 2 the reviewer flag is off (nothing ran — not an error, but
distinguishable from a run that found nothing to do).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.api.venue_link_review_client import VenueLinkReviewClient
from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_repository import VenueRepository
from app.services.venue_link_audit_reviewer import (
    DEFAULT_REVIEW_CONSENSUS_K,
    MIN_REVIEW_CONSENSUS_K,
    VenueLinkAuditReviewerService,
)

logger = logging.getLogger("review_venue_link_audit")


def _redis_client():
    """The admin-config mirror. Optional: with no Redis reachable, both flags
    fall back to their shipped defaults (False), which means the pass does
    nothing — the correct degrade for a kill-switch."""
    try:
        import redis

        return redis.Redis(
            host=settings.redis_host, port=settings.redis_port, decode_responses=True,
        )
    except Exception as e:  # pragma: no cover - environment dependent
        logger.warning(f"redis unavailable, flags fall back to defaults: {e}")
        return None


def build_service(*, consensus_k: int = DEFAULT_REVIEW_CONSENSUS_K):
    store = RdsVenueStore(settings.rds_sqlalchemy_url)
    venue_dao = VenueRepository(client=None, rds_store=store)
    client = None
    if settings.openai_api_key:
        client = VenueLinkReviewClient(
            api_key=settings.openai_api_key, model=settings.event_extraction_model,
        )
    return VenueLinkAuditReviewerService(
        venue_dao, client, redis_client=_redis_client(), consensus_k=consensus_k,
    )


def report_to_dict(result: dict, *, applied: bool) -> dict:
    return {
        "mode": "apply" if applied else "dry_run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": result.get("enabled", False),
        "auto_apply": result.get("auto_apply", False),
        "consensus_k": result.get("consensus_k"),
        "counts": result.get("counts", {}),
        "outcomes": result.get("outcomes", []),
    }


def write_report_json(path: str, result: dict, *, applied: bool) -> None:
    Path(path).write_text(
        json.dumps(report_to_dict(result, applied=applied), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _print_report(result: dict, *, applied: bool) -> None:
    print(f"mode: {'apply' if applied else 'dry-run'}")
    print(f"reviewer enabled: {result.get('enabled')}   "
          f"auto-apply: {result.get('auto_apply')}   K: {result.get('consensus_k')}")
    counts = result.get("counts") or {}
    if counts:
        print("\noutcome counts:")
        for outcome, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {outcome:<32} {n}")
    print("\nper pair:")
    for row in result.get("outcomes") or []:
        print(f"  {row['handle']:<26} {row['outcome']:<28} "
              f"verdict={row.get('verdict')} field={row.get('matched_field')} "
              f"tally={row.get('tally')}")
        if row.get("reason"):
            print(f"      reason: {row['reason']}")
        if row.get("evidence_quote"):
            print(f"      quote : {row['evidence_quote']!r}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="persist review rows (default: dry-run, writes nothing)")
    parser.add_argument("--handle", action="append", dest="handles", default=None,
                        help="scope to one handle; repeatable")
    parser.add_argument("--consensus-k", type=int, default=DEFAULT_REVIEW_CONSENSUS_K,
                        help=f"independent calls per pair (default "
                             f"{DEFAULT_REVIEW_CONSENSUS_K}); below "
                             f"{MIN_REVIEW_CONSENSUS_K} verdicts are recorded "
                             f"but no flag is ever suppressed")
    parser.add_argument("--report-json", default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    service = build_service(consensus_k=args.consensus_k)
    result = asyncio.run(service.run(handles=args.handles, dry_run=not args.apply))

    _print_report(result, applied=args.apply)
    if args.report_json:
        write_report_json(args.report_json, result, applied=args.apply)

    if not result.get("enabled"):
        print("\nvenue_link_audit_reviewer_enabled is False — nothing ran.")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
