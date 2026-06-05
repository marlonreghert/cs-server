"""Operator CLI: the schema-normalization equivalence gate against live infra.

Read-only. Runs two full-dataset comparisons and exits non-zero if either finds a
mismatch (so it can gate a deploy/cutover step):

  1. RDS golden diff — for every venue, the v2 reconstruction (columns + residual)
     vs the retained v1 `payload`. Proves the column/residual split lost nothing;
     this MUST pass before the 0003b contract migration drops `payload`.
  2. RDS↔Redis serving diff — for every active venue, the v2 RDS reconstruction
     vs the venue currently served from Redis. Proves the live projection matches
     RDS. Live busyness is a separate serving key and is not compared.

Neither writes anything (no shadow keyspace). Run over an SSM tunnel:

    python -m scripts.verify_schema_equivalence            # both checks
    python -m scripts.verify_schema_equivalence --rds      # golden diff only
    python -m scripts.verify_schema_equivalence --redis    # serving diff only

See plans/260605_rds-schema-normalization.md (Data integrity & equivalence
verification).
"""
from __future__ import annotations

import argparse
import sys

import redis

from app.config import settings
from app.dao.redis_venue_dao import RedisVenueDAO
from app.dao.rds_venue_store import RdsVenueStore
from app.db.geo_redis_client import GeoRedisClient
from app.services.equivalence_verify import (
    DiffResult,
    rds_venue_golden_diff,
    redis_vs_rds_serving_diff,
)

_MAX_PRINT = 50


def _report(title: str, result: DiffResult) -> None:
    print(f"[{title}] checked={result.checked} mismatches={result.mismatch_count}")
    for m in result.mismatches[:_MAX_PRINT]:
        print(f"  MISMATCH {m}")
    if result.mismatch_count > _MAX_PRINT:
        print(f"  … {result.mismatch_count - _MAX_PRINT} more")


def _redis_dao() -> RedisVenueDAO:
    client = redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        db=settings.redis_db,
        decode_responses=True,
    )
    return RedisVenueDAO(GeoRedisClient(client))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Schema-normalization equivalence gate")
    parser.add_argument("--rds", action="store_true", help="run only the RDS golden diff")
    parser.add_argument("--redis", action="store_true", help="run only the RDS↔Redis serving diff")
    args = parser.parse_args(argv)
    run_rds = args.rds or not args.redis
    run_redis = args.redis or not args.rds

    store = RdsVenueStore(settings.rds_sqlalchemy_url)
    ok = True

    if run_rds:
        result = rds_venue_golden_diff(store)
        _report("rds_golden_diff", result)
        ok = ok and result.passing

    if run_redis:
        result = redis_vs_rds_serving_diff(store, _redis_dao())
        _report("redis_vs_rds_serving_diff", result)
        ok = ok and result.passing

    print("RESULT: EQUIVALENT — safe to proceed." if ok
          else "RESULT: NOT EQUIVALENT — do not contract/cut over.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
