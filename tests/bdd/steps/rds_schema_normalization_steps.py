"""Behave steps for tests/bdd/persistence/rds-schema-normalization.feature.

Exercises the contracted column-as-source-of-truth venue schema (scalars in
columns, nested fields in the residual `extra`, address in venues.address, no
`payload` baseline) against the in-memory fake (context.rds_store) and the real
RedisProjectionService. The harness (context.repository, context.rds_store,
context.redis_only_dao, context.redis_projection_service) and the Background
("RDS system-of-record is enabled", "empty RDS and empty Redis") are wired by
environment.py / rds_system_of_record_steps.py. The "projector rebuilds Redis from
RDS" step is shared from rds_address_table_steps.py.
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

from app.dao.venue_row import COLUMN_FIELDS, RESIDUAL_FIELDS
from app.models import Venue
from app.models.venue import FootTrafficForecast

from app.services.equivalence_verify import canonical_venue, redis_vs_rds_serving_diff

_LAT, _LNG = -8.05, -34.88


def _full_venue(vid: str) -> Venue:
    """A venue exercising every shape: scalar columns + dwell times + a nested
    foot-traffic forecast (the residual fields)."""
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=f"Bar {vid}",
        venue_address=f"{vid} Rua X, 100", venue_lat=_LAT, venue_lng=_LNG,
        venue_type="BAR", price_level=2, rating=4.5, reviews=321, priority=4,
        venue_dwell_time_min=30, venue_dwell_time_max=90,
        venue_foot_traffic_forecast=[FootTrafficForecast(day_int=0, day_raw=[10] * 24)],
    )


# ── reconstruction parity (columns + residual + address, no payload) ─────────
@given('a venue "{vid}" with full scalar fields, dwell times, and a foot-traffic forecast')
def step_full_venue(context, vid):
    context.built = getattr(context, "built", {})
    context.built[vid] = _full_venue(vid)


@when('venue "{vid}" is stored with scalars in columns and only nested fields in the residual JSON')
def step_store_normalized(context, vid):
    context.repository.upsert_venue(context.built[vid])


@then('reconstructing venue "{vid}" from the repository equals the original venue')
def step_reconstruction_roundtrip(context, vid):
    reconstructed = context.repository.get_venue(vid)        # columns + residual + address
    assert canonical_venue(reconstructed) == canonical_venue(context.built[vid]), (
        f"reconstruction differs from the original venue for {vid}"
    )


@then('the stored venue row carries no payload baseline')
def step_no_payload_baseline(context):
    # The contract dropped the retained `payload` JSONB: the stored venue row must
    # not carry it anymore (columns + residual are the sole source of truth).
    row = next(reversed(context.rds_store.venues.values()))
    assert "payload" not in row, sorted(row.keys())


# ── no scalar duplicated in the residual ─────────────────────────────────────
@given('a venue "{vid}" stored under the normalized venue schema')
def step_store_under_schema(context, vid):
    context.repository.upsert_venue(_full_venue(vid))


@then('the residual JSON for venue "{vid}" contains only nested fields')
def step_residual_only_nested(context, vid):
    extra = context.rds_store.get_venue(vid)["extra"]
    assert set(extra.keys()) <= set(RESIDUAL_FIELDS), extra.keys()


@then('it contains none of the scalar fields that exist as columns')
def step_residual_no_scalar(context, vid=None):
    # Re-read the most recently stored venue's residual (single-venue scenario).
    row = next(reversed(context.rds_store.venues.values()))
    extra_keys = set(row["extra"].keys())
    assert extra_keys.isdisjoint(set(COLUMN_FIELDS)), extra_keys & set(COLUMN_FIELDS)


# ── ongoing gate: redis↔rds serving diff after the projector runs ────────────
# "the projector rebuilds Redis from RDS" is shared from rds_address_table_steps.
@given('venues stored under the normalized venue schema')
def step_store_venues_plural(context):
    context.repository.upsert_venue(_full_venue("p1"))
    context.repository.upsert_venue(_full_venue("p2"))


@then('the redis-to-rds serving diff passes with zero mismatches')
def step_serving_diff_passes(context):
    result = redis_vs_rds_serving_diff(context.rds_store, context.redis_only_dao)
    assert result.passing and result.checked >= 1, result.mismatches
