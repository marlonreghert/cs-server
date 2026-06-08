"""Operator CLI: the schema-normalization equivalence gate against live infra.

Read-only. Runs the RDS↔Redis serving diff: for every active venue, the v2 RDS
reconstruction (columns + residual + address table) is compared against the venue
currently served from Redis, proving the live projection matches RDS. Exits
non-zero on any mismatch so it can gate a deploy/verify step. Nothing is written
(no shadow keyspace). Live busyness is a separate serving key and is not compared.

The expand-era RDS golden diff (v2 reconstruction vs retained `payload`) was
retired when the batched contract dropped the `payload` baseline column.

    python -m scripts.verify_schema_equivalence

See plans/260605_rds-schema-normalization.md (Data integrity & equivalence
verification).
"""
from __future__ import annotations

import sys

import redis

from app.config import settings
from app.dao.redis_venue_dao import RedisVenueDAO
from app.dao.rds_venue_store import RdsVenueStore
from app.db.geo_redis_client import GeoRedisClient
from app.services.equivalence_verify import DiffResult, redis_vs_rds_serving_diff

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
    store = RdsVenueStore(settings.rds_sqlalchemy_url)
    result = redis_vs_rds_serving_diff(store, _redis_dao())
    _report("redis_vs_rds_serving_diff", result)
    ok = result.passing
    print("RESULT: EQUIVALENT — safe to proceed." if ok
          else "RESULT: NOT EQUIVALENT — investigate before proceeding.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
