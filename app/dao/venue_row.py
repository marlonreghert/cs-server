"""Venue ⇄ RDS row mapping for the column-as-source-of-truth schema (Ex1).

`venues.venue` stores every scalar Venue field in its own column; only the
genuinely-nested fields the columns cannot hold live in a slim residual JSON
(`extra`). This module is the single place that knows that split, so the store,
the repository, and the projector reconstruct a `Venue` identically and no scalar
can drift between a column and the JSON.

See plans/260605_rds-schema-normalization.md (Step Ex1).
"""
from __future__ import annotations

from typing import Mapping

from app.models import Venue

# Scalar Venue fields promoted to their own `venues.venue` columns (the source of
# truth). Address columns (venue_address/lat/lng) stay here until Ex3 moves them
# into venues.address.
COLUMN_FIELDS: tuple[str, ...] = (
    "venue_id",
    "venue_name",
    "venue_address",
    "venue_lat",
    "venue_lng",
    "venue_type",
    "price_level",
    # Promoted price-signal columns (raw/auditable). Structured `price_range` is a
    # jsonb column; the rest are scalars. All projector-rebuildable.
    "price_range",
    "google_price_level",
    "besttime_price_level",
    "price_level_source",
    "rating",
    "reviews",
    "forecast",
    "processed",
    "priority",
    "lifecycle_status",
    "deprecated_at",
    "deprecated_reason",
    "deprecated_source",
    "google_business_status",
)

# Genuinely-nested fields columns cannot hold — the only contents of the residual
# JSON ("extra"). The "justified exception" the RDS plan anticipated.
RESIDUAL_FIELDS: tuple[str, ...] = (
    "venue_dwell_time_min",
    "venue_dwell_time_max",
    "venue_foot_traffic_forecast",
    # Geo-link provenance (see Venue.geo_linked docstring) — low-traffic
    # metadata read only by undo_geo_link, not worth a promoted column.
    "geo_linked",
    "geo_linked_year_month",
)

# Invariant: columns ∪ residual == the full Venue field set, so reconstruction
# never silently drops a field. Guarded by tests/test_venue_row.py.
ALL_VENUE_FIELDS: frozenset[str] = frozenset(COLUMN_FIELDS) | frozenset(RESIDUAL_FIELDS)

# Columns the system manages OUT OF BAND of the venue serving projection:
# `priority` is set by direct SQL (one-time tiering + manual edits) and is
# intentionally NOT projected to Redis; lifecycle/deprecation and
# `google_business_status` are set by soft_delete_venue / _preserve_deprecation.
# The redis↔rds serving diff excludes them because the Redis-served venue does
# not carry these out-of-band fields, so a difference there is expected — not
# data loss.
COLUMN_AUTHORITATIVE_FIELDS: frozenset[str] = frozenset({
    "priority",
    "lifecycle_status",
    "deprecated_at",
    "deprecated_reason",
    "deprecated_source",
    "google_business_status",
})


def split_venue_for_storage(venue: Venue) -> tuple[dict, dict]:
    """Split a Venue into (column values, residual JSON) for an RDS upsert.

    Values are JSON-mode dumped (by alias) so they are directly storable and
    byte-comparable with a payload-derived reconstruction.
    """
    dumped = venue.model_dump(by_alias=True, mode="json")
    columns = {f: dumped.get(f) for f in COLUMN_FIELDS}
    residual = {f: dumped.get(f) for f in RESIDUAL_FIELDS}
    return columns, residual


def venue_from_row(row: Mapping) -> Venue:
    """Reconstruct a Venue from an RDS row: scalar columns + the residual `extra`.

    The single reconstruction path for `RdsVenueStore`/`VenueRepository.get_venue`,
    `list_all_venues`, and the Redis projector. Deliberately ignores any retained
    `payload` so a green test proves the columns+residual round-trip, not the
    legacy blob.
    """
    data = {f: row.get(f) for f in COLUMN_FIELDS}
    extra = row.get("extra") or {}
    for f in RESIDUAL_FIELDS:
        if f in extra:
            data[f] = extra[f]
    return Venue.model_validate(data)
