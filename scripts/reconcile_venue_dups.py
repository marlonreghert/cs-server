"""Operator CLI: deprecate venue rows that duplicate an already-stored place.

Rows created before the ingestion dedup fix can hold one physical place under
two venue_ids, each enriched separately (the "Adega do Futuro shown twice" bug).
This finds those groups — same place = within 50m AND a folded-name match — and,
per group, keeps the earliest-created row and soft-deletes (deprecates) the rest.

Soft-delete is reversible and the serving view excludes deprecated venues, so the
extra card disappears once the projector re-asserts RDS -> Redis on its next
cycle. Matching is name-based, never google_place_id-based, so two genuinely
different venues that wrongly share a place_id are never merged.

Usage:
    python -m scripts.reconcile_venue_dups            # dry-run: report only
    python -m scripts.reconcile_venue_dups --apply    # deprecate the redundant rows
"""

from __future__ import annotations

import argparse
import logging

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.services.venue_reconciliation import find_duplicate_groups

logger = logging.getLogger("reconcile_venue_dups")

DEPRECATE_REASON = "duplicate_place"
DEPRECATE_SOURCE = "reconcile_venue_dups"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deprecate duplicate venue rows (same physical place)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Deprecate the redundant rows (default: dry-run report only).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    store = RdsVenueStore(settings.rds_sqlalchemy_url)
    rows = store.list_all_venue_rows()
    groups = find_duplicate_groups(rows)
    total_dups = sum(len(g.duplicates) for g in groups)

    logger.info(
        f"Scanned {len(rows)} venue rows; found {len(groups)} duplicate-place "
        f"group(s), {total_dups} redundant row(s)."
    )
    for g in groups:
        logger.info(
            f"  KEEP  {g.canonical['venue_id']}  "
            f"{g.canonical.get('venue_name')!r}  "
            f"(created {g.canonical.get('created_at')})"
        )
        for d in g.duplicates:
            logger.info(
                f"  DROP  {d['venue_id']}  {d.get('venue_name')!r}  "
                f"(created {d.get('created_at')})"
            )

    if not args.apply:
        logger.info(
            "\nDry-run only — no changes made. Re-run with --apply to deprecate "
            "the DROP rows."
        )
        return

    for g in groups:
        for d in g.duplicates:
            store.soft_delete_venue(
                d["venue_id"], reason=DEPRECATE_REASON, source=DEPRECATE_SOURCE
            )
    logger.info(f"\nDeprecated {total_dups} duplicate row(s).")


if __name__ == "__main__":
    main()
