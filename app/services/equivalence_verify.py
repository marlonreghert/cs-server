"""Data-integrity equivalence harness for the RDS schema-normalization steps.

The cutover gate for every step is a full-dataset comparison, not a spot check:
keep the previous shape as a golden "v1" and prove the new "v2" shape
reconstructs identically — in RDS (golden diff) and in the Redis serving
projection (shadow projection). Mismatches are reported by venue_id + field name
only, never values, so the report carries no payload secrets/PII.

See plans/260605_rds-schema-normalization.md (Data integrity & equivalence
verification).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.dao.redis_venue_dao import (
    VENUES_GEO_KEY_V1,
    VENUES_GEO_PLACE_MEMBER_FORMAT_V1,
)
from app.dao.venue_row import venue_from_row
from app.models import Venue

logger = logging.getLogger(__name__)

_COORD_PRECISION = 6  # ~0.1 m; Redis geohash is lossy, so don't over-compare
_FLOAT_PRECISION = 9


# ── canonicalization: the definition of "equal" ──────────────────────────────
def _canonicalize(value):
    """Recursively normalize for comparison: sort dict keys, round floats so two
    semantically-equal values compare equal regardless of float representation."""
    if isinstance(value, dict):
        return {k: _canonicalize(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonicalize(v) for v in value]
    if isinstance(value, float):
        return round(value, _FLOAT_PRECISION)
    return value


def canonical_venue(venue: Venue) -> dict:
    """Canonical, comparable form of a Venue (by-alias JSON dump, normalized)."""
    return _canonicalize(venue.model_dump(by_alias=True, mode="json"))


def venue_diff_fields(a: Venue, b: Venue) -> list[str]:
    """The top-level field names where two venues differ (empty == equal)."""
    ca, cb = canonical_venue(a), canonical_venue(b)
    return sorted(f for f in set(ca) | set(cb) if ca.get(f) != cb.get(f))


# ── result ───────────────────────────────────────────────────────────────────
@dataclass
class DiffResult:
    """Outcome of a full-dataset equivalence pass. `mismatches` lists dicts with a
    `venue_id` and the diverging `fields`/`reason` — never any field values."""

    checked: int = 0
    mismatches: list[dict] = field(default_factory=list)

    @property
    def passing(self) -> bool:
        return not self.mismatches

    @property
    def mismatch_count(self) -> int:
        return len(self.mismatches)


# ── RDS golden diff: v2 reconstruction vs retained v1 payload ────────────────
def rds_venue_golden_diff(rds_store) -> DiffResult:
    """Reconstruct each venue from the v2 shape (columns + residual) and from the
    retained v1 `payload`, and report every row where they diverge. This is the
    Ex1 cutover gate; it must pass for 100% of rows before `payload` is dropped."""
    result = DiffResult()
    for row in rds_store.list_all_venue_rows():
        result.checked += 1
        v2 = venue_from_row(row)
        v1 = Venue.model_validate(row["payload"])
        fields = venue_diff_fields(v2, v1)
        if fields:
            result.mismatches.append({"venue_id": row.get("venue_id"), "fields": fields})
    logger.info(
        "[equivalence] rds_venue_golden_diff checked=%d mismatches=%d",
        result.checked, result.mismatch_count,
    )
    return result


# ── Redis serving equivalence: shadow projection vs pre-change snapshot ───────
def _raw_redis(redis_only_dao):
    """The underlying redis client behind a RedisVenueDAO (DAO.client is a
    GeoRedisClient whose `.client` is the redis connection)."""
    return redis_only_dao.client.client


def _round_pos(pos) -> list:
    if not pos or pos[0] is None:
        return []
    lon, lat = pos[0]
    return [round(float(lon), _COORD_PRECISION), round(float(lat), _COORD_PRECISION)]


def serving_snapshot(redis_only_dao) -> dict:
    """Capture the Redis serving state: per-venue canonical serving value + geo
    membership/coordinates. Live busyness lives under its own key and is NOT read
    here, so it is exempt from the comparison by construction."""
    raw = _raw_redis(redis_only_dao)
    snap: dict = {}
    for venue in redis_only_dao.list_all_venues():
        member = VENUES_GEO_PLACE_MEMBER_FORMAT_V1.format(venue.venue_id)
        snap[venue.venue_id] = {
            "venue": canonical_venue(venue),
            "coords": _round_pos(raw.geopos(VENUES_GEO_KEY_V1, member)),
        }
    return snap


def project_v1_from_payload(rds_store, redis_only_dao) -> None:
    """Reference v1 projection: reconstruct active venues from the retained full
    `payload` (the pre-Ex1 behavior) into the given Redis-only DAO. Used to build
    the pre-change golden snapshot the shadow projection is diffed against."""
    for venue_id in rds_store.list_active_venue_ids():
        row = rds_store.get_venue(venue_id)
        if row is None:
            continue
        redis_only_dao.upsert_venue(Venue.model_validate(row["payload"]))


def redis_vs_rds_serving_diff(rds_store, redis_only_dao) -> DiffResult:
    """Read-only RDS↔Redis serving check: for every active venue, compare the v2
    RDS reconstruction (columns + residual) against the venue currently served
    from Redis. Confirms the live projection matches what RDS reconstructs — no
    writes, no shadow keyspace, so it is safe to run against production at any
    time. Live busyness is a separate serving key and is not compared."""
    result = DiffResult()
    for venue_id in rds_store.list_active_venue_ids():
        row = rds_store.get_venue(venue_id)
        if row is None:
            continue
        result.checked += 1
        served = redis_only_dao.get_venue(venue_id)
        if served is None:
            result.mismatches.append({"venue_id": venue_id, "reason": "missing_in_redis"})
            continue
        fields = venue_diff_fields(venue_from_row(row), served)
        if fields:
            result.mismatches.append({"venue_id": venue_id, "fields": fields})
    logger.info(
        "[equivalence] redis_vs_rds_serving_diff checked=%d mismatches=%d",
        result.checked, result.mismatch_count,
    )
    return result


def diff_serving_snapshots(expected: dict, actual: dict) -> DiffResult:
    """Compare a pre-change serving snapshot against the shadow projection."""
    result = DiffResult()
    for vid in sorted(set(expected) | set(actual)):
        result.checked += 1
        e, a = expected.get(vid), actual.get(vid)
        if e is None:
            result.mismatches.append({"venue_id": vid, "reason": "extra_in_shadow"})
        elif a is None:
            result.mismatches.append({"venue_id": vid, "reason": "missing_in_shadow"})
        elif e != a:
            reasons = []
            if e["venue"] != a["venue"]:
                reasons.append("serving_value")
            if e["coords"] != a["coords"]:
                reasons.append("geo_coords")
            result.mismatches.append({"venue_id": vid, "reason": ",".join(reasons) or "differs"})
    logger.info(
        "[equivalence] redis_shadow_diff checked=%d mismatches=%d",
        result.checked, result.mismatch_count,
    )
    return result
