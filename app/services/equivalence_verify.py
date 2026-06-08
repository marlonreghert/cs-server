"""Data-integrity equivalence harness for the RDS schema-normalization steps.

After the batched contract dropped the retained `payload` baseline, the ongoing
gate is the **redis↔rds serving diff**: for every active venue, the v2 RDS
reconstruction (columns + residual + address table) is compared against the venue
currently served from Redis, proving the live projection matches RDS. (The
expand-era golden diff and shadow-projection-vs-payload checks were retired with
the `payload` drop.) Mismatches are reported by venue_id + field name only, never
values, so the report carries no secrets/PII.

See plans/260605_rds-schema-normalization.md (Data integrity & equivalence
verification).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.dao.venue_row import COLUMN_AUTHORITATIVE_FIELDS, venue_from_row
from app.models import Venue

logger = logging.getLogger(__name__)

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
    """The top-level field names where two venues differ (empty == equal).

    Excludes COLUMN_AUTHORITATIVE_FIELDS: those columns are managed out of band
    (priority tiering, soft-delete) and are not carried in the Redis serving
    projection, so a difference on them is expected and not data loss. Every other
    field must match."""
    ca, cb = canonical_venue(a), canonical_venue(b)
    return sorted(
        f for f in set(ca) | set(cb)
        if f not in COLUMN_AUTHORITATIVE_FIELDS and ca.get(f) != cb.get(f)
    )


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


# ── Redis serving equivalence: live projection vs RDS reconstruction ─────────
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
