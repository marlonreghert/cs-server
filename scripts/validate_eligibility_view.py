"""Operator CLI: validate the eligibility serving-view cutover + reactivation
migration against live infra. READ-ONLY — nothing is written.

This is the data-quality gate for plans/260613_eligibility-serving-view.md. It
captures the RDS / serving-view / Redis state, diffs a before/after pair to prove
the cutover and migration deltas are exactly the expected (corrected-false-positive)
sets and nothing else, and reconciles Redis against the view.

Subcommands:

  snapshot [--out FILE]
      Capture the current state (RDS counts + deprecated_source breakdown +
      active-by-google-type; the serving view's size + ids + by-google-type;
      Redis geo ZCARD + served ids + by-google-type) and print it; optionally
      write JSON for a later compare.

  compare BEFORE.json AFTER.json
      Pure JSON diff (no infra). Prints how the served set changed (entered /
      left), the deprecated_source breakdown delta (eligibility_filter must reach
      0 after the reactivation migration), and per-google-type served deltas.

  reconcile
      Live Redis<->RDS<->view reconciliation. Asserts every Redis-served venue is
      active AND in the view, and every view venue is served (no orphans/leaks).
      Exits non-zero on any violation so it can gate a deploy/migration step.

Usage:
    python -m scripts.validate_eligibility_view snapshot --out before.json
    # deploy the gold-view code (cutover) / run the reactivation migration
    python -m scripts.validate_eligibility_view snapshot --out after.json
    python -m scripts.validate_eligibility_view compare before.json after.json
    python -m scripts.validate_eligibility_view reconcile
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import redis
from sqlalchemy import text

from app.config import settings
from app.dao.rds_venue_store import RdsVenueStore
from app.dao.redis_venue_dao import RedisVenueDAO
from app.db.geo_redis_client import GeoRedisClient

_MAX_PRINT = 50


# ── infra ────────────────────────────────────────────────────────────────────
def _store() -> RdsVenueStore:
    return RdsVenueStore(settings.rds_sqlalchemy_url)


def _redis_dao() -> RedisVenueDAO:
    client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        db=settings.redis_db,
        decode_responses=True,
    )
    return RedisVenueDAO(GeoRedisClient(client))


def _google_type_by_venue(store: RdsVenueStore) -> dict[str, str]:
    """venue_id -> lower(google_primary_type) for live (non-deleted) vibe rows."""
    with store.engine.connect() as conn:
        return {
            r[0]: r[1]
            for r in conn.execute(text(
                "SELECT venue_id, lower(google_primary_type) "
                "FROM google_places.vibe_attributes "
                "WHERE deleted_at IS NULL AND google_primary_type IS NOT NULL"
            ))
        }


def _bucket_by_type(ids, gtype_by_venue) -> dict[str, int]:
    out: dict[str, int] = {}
    for vid in ids:
        key = gtype_by_venue.get(vid, "(unlabeled)")
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ── snapshot ─────────────────────────────────────────────────────────────────
def snapshot(store: RdsVenueStore, redis_dao: RedisVenueDAO) -> dict:
    gtype_by_venue = _google_type_by_venue(store)
    with store.engine.connect() as conn:
        active = [r[0] for r in conn.execute(text(
            "SELECT venue_id FROM venues.venue WHERE lifecycle_status='active'"))]
        deprecated = [r[0] for r in conn.execute(text(
            "SELECT venue_id FROM venues.venue WHERE lifecycle_status='deprecated'"))]
        dep_source = {
            (r[0] or "(none)"): r[1]
            for r in conn.execute(text(
                "SELECT deprecated_source, count(*) FROM venues.venue "
                "WHERE lifecycle_status='deprecated' GROUP BY deprecated_source"))
        }

    view_ids = store.list_servable_venue_ids()
    served_ids = redis_dao.list_all_venue_ids()
    geo_zcard = redis_dao.client.zcard(  # type: ignore[attr-defined]
        "venues_geo_v1"
    ) if hasattr(redis_dao.client, "zcard") else len(served_ids)

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "rds": {
            "active": len(active),
            "deprecated": len(deprecated),
            "deprecated_source": dep_source,
            "active_by_google_type": _bucket_by_type(active, gtype_by_venue),
        },
        "view": {
            "size": len(view_ids),
            "by_google_type": _bucket_by_type(view_ids, gtype_by_venue),
            "ids": sorted(view_ids),
        },
        "redis": {
            "geo_zcard": geo_zcard,
            "served_count": len(served_ids),
            "by_google_type": _bucket_by_type(served_ids, gtype_by_venue),
            "served_ids": sorted(served_ids),
        },
    }


def _print_snapshot(snap: dict) -> None:
    rds, view, rds_redis = snap["rds"], snap["view"], snap["redis"]
    print(f"captured_at: {snap['captured_at']}")
    print(f"RDS    active={rds['active']} deprecated={rds['deprecated']} "
          f"deprecated_source={rds['deprecated_source']}")
    print(f"VIEW   size={view['size']}")
    print(f"REDIS  geo_zcard={rds_redis['geo_zcard']} served={rds_redis['served_count']}")
    print(f"VIEW by google_type (top): "
          f"{dict(list(view['by_google_type'].items())[:10])}")


# ── compare ──────────────────────────────────────────────────────────────────
def compare(before: dict, after: dict) -> int:
    b_served, a_served = set(before["redis"]["served_ids"]), set(after["redis"]["served_ids"])
    entered, left = sorted(a_served - b_served), sorted(b_served - a_served)

    print("── served-set delta ──")
    print(f"entered serving: {len(entered)}")
    for vid in entered[:_MAX_PRINT]:
        print(f"  + {vid}")
    if len(entered) > _MAX_PRINT:
        print(f"  … {len(entered) - _MAX_PRINT} more")
    print(f"left serving: {len(left)}")
    for vid in left[:_MAX_PRINT]:
        print(f"  - {vid}")
    if len(left) > _MAX_PRINT:
        print(f"  … {len(left) - _MAX_PRINT} more")

    print("── deprecated_source delta ──")
    b_src, a_src = before["rds"]["deprecated_source"], after["rds"]["deprecated_source"]
    for k in sorted(set(b_src) | set(a_src)):
        print(f"  {k}: {b_src.get(k, 0)} -> {a_src.get(k, 0)}")
    ef_after = a_src.get("eligibility_filter", 0)
    print(f"\nNOTE: confirm every 'left serving' venue is genuinely ineligible (cutover),")
    print(f"      every 'entered serving' venue is a corrected false-positive (migration),")
    print(f"      and eligibility_filter deprecated count is 0 post-migration "
          f"(currently {ef_after}).")
    return 0


# ── reconcile ────────────────────────────────────────────────────────────────
def reconcile(store: RdsVenueStore, redis_dao: RedisVenueDAO) -> int:
    view = set(store.list_servable_venue_ids())
    active = set(store.list_active_venue_ids())
    served = set(redis_dao.list_all_venue_ids())

    leaks_inactive = sorted(served - active)      # served but not active in RDS
    leaks_ineligible = sorted(served - view)      # served but not in the view
    orphans_missing = sorted(view - served)       # view-eligible but not served

    def _report(label, ids):
        print(f"{label}: {len(ids)}")
        for vid in ids[:_MAX_PRINT]:
            print(f"  {vid}")
        if len(ids) > _MAX_PRINT:
            print(f"  … {len(ids) - _MAX_PRINT} more")

    _report("served-but-INACTIVE (leak)", leaks_inactive)
    _report("served-but-NOT-in-view (leak)", leaks_ineligible)
    _report("in-view-but-NOT-served (orphan)", orphans_missing)

    ok = not (leaks_inactive or leaks_ineligible or orphans_missing)
    print("RESULT: RECONCILED — Redis serves exactly the view." if ok
          else "RESULT: NOT RECONCILED — investigate before proceeding "
               "(note: a projection cycle may be mid-flight; re-run after one cycle).")
    return 0 if ok else 1


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_snap = sub.add_parser("snapshot", help="capture current RDS/view/Redis state")
    p_snap.add_argument("--out", default=None, help="write the snapshot JSON to this path")
    p_cmp = sub.add_parser("compare", help="diff two snapshot JSON files")
    p_cmp.add_argument("before")
    p_cmp.add_argument("after")
    sub.add_parser("reconcile", help="live Redis<->RDS<->view reconciliation")
    args = parser.parse_args(argv)

    if args.cmd == "snapshot":
        snap = snapshot(_store(), _redis_dao())
        _print_snapshot(snap)
        if args.out:
            with open(args.out, "w") as fh:
                json.dump(snap, fh, indent=2)
            print(f"wrote {args.out}")
        return 0
    if args.cmd == "compare":
        with open(args.before) as fh:
            before = json.load(fh)
        with open(args.after) as fh:
            after = json.load(fh)
        return compare(before, after)
    if args.cmd == "reconcile":
        return reconcile(_store(), _redis_dao())
    return 2


if __name__ == "__main__":
    sys.exit(main())
