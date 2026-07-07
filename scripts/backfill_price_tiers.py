"""Operator CLI: backfill the served price tier under the range-first rule.

Re-derives every venue's `price_level` + `price_level_source` from its already
stored Google/BestTime signals (`google_price_level`, `price_range`,
`besttime_price_level`) via the production `derive_price_signal` rule, and writes
the corrected tier back to RDS. No external API calls. Idempotent — safe to re-run.

The daily enrichment cron applies the new rule to venues it re-touches; this
script corrects the *existing* catalog in one pass so the fix reaches the serving
projection immediately (the projector re-asserts RDS -> Redis on its next cycle).

Usage:
    python -m scripts.backfill_price_tiers            # dry-run: report only
    python -m scripts.backfill_price_tiers --apply    # write the corrected tiers
"""
from __future__ import annotations

import argparse
import logging
from collections import Counter

from sqlalchemy import text

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.venue_row import venue_from_row
from app.services.price_signal import derive_price_signal

logger = logging.getLogger("backfill_price_tiers")

_UPDATE = text(
    "UPDATE venues.venue SET price_level = :pl, price_level_source = :src "
    "WHERE venue_id = :vid"
)


def compute_backfill(store):
    """Scan every venue row and re-derive its tier under the current rule.

    Pure — performs no writes. Returns `(changes, before_src, after_src,
    before_lvl, after_lvl)` where `changes` is a list of
    `(venue_id, new_price_level, new_source)` for rows whose re-derived
    (tier, source) differs from what is stored, and the Counters are the
    before/after distributions for reporting.
    """
    changes: list[tuple[str, int | None, str | None]] = []
    before_src, after_src = Counter(), Counter()
    before_lvl, after_lvl = Counter(), Counter()
    for row in store.list_all_venue_rows():
        v = venue_from_row(row)
        sig = derive_price_signal(
            v.google_price_level, v.price_range, v.besttime_price_level
        )
        before_src[v.price_level_source] += 1
        after_src[sig.source] += 1
        before_lvl[v.price_level] += 1
        after_lvl[sig.price_level] += 1
        if (v.price_level, v.price_level_source) != (sig.price_level, sig.source):
            changes.append((v.venue_id, sig.price_level, sig.source))
    return changes, before_src, after_src, before_lvl, after_lvl


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    ap = argparse.ArgumentParser(
        description="Backfill served price tiers under the range-first rule."
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write the corrected tiers to RDS (default: dry-run report only)",
    )
    args = ap.parse_args()

    store = RdsVenueStore(settings.rds_sqlalchemy_url)
    changes, before_src, after_src, before_lvl, after_lvl = compute_backfill(store)

    logger.info("venues scanned: %d", sum(before_lvl.values()))
    logger.info("price_level_source before: %s", dict(before_src))
    logger.info("price_level_source after : %s", dict(after_src))
    logger.info("price_level        before: %s", dict(before_lvl))
    logger.info("price_level        after : %s", dict(after_lvl))
    logger.info("venues whose tier/source changes: %d", len(changes))

    if not changes:
        logger.info("nothing to backfill.")
        return 0
    if not args.apply:
        logger.info("DRY RUN — re-run with --apply to write. Sample: %s", changes[:5])
        return 0

    with store.engine.begin() as conn:
        for vid, pl, src in changes:
            conn.execute(_UPDATE, {"pl": pl, "src": src, "vid": vid})
    logger.info("applied %d updates.", len(changes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
