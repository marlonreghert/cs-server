"""Behave steps for tests/bdd/persistence/rds-schema-normalization.feature (Ex1).

Exercises the column-as-source-of-truth venue schema and the equivalence harness
against the in-memory fake (context.rds_store) and the real RedisProjectionService
over fresh fakeredis-backed DAOs (no Postgres, no live Redis). The Background steps
("RDS system-of-record is enabled", "empty RDS and empty Redis") are shared from
rds_system_of_record_steps.py.
"""
from __future__ import annotations

import fakeredis
from behave import given, when, then  # type: ignore[import-untyped]

from app.dao.redis_venue_dao import RedisVenueDAO
from app.dao.venue_row import COLUMN_FIELDS, RESIDUAL_FIELDS
from app.db.geo_redis_client import GeoRedisClient
from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo
from app.models.venue import FootTrafficForecast
from app.services.equivalence_verify import (
    canonical_venue,
    diff_serving_snapshots,
    project_v1_from_payload,
    rds_venue_golden_diff,
    serving_snapshot,
)
from app.services.redis_projection_service import RedisProjectionService

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


def _live(vid: str) -> LiveForecastResponse:
    return LiveForecastResponse(
        status="OK", venue_info=VenueInfo(venue_id=vid),
        analysis=Analysis(venue_live_busyness=55, venue_live_busyness_available=True),
    )


def _fresh_dao() -> RedisVenueDAO:
    """A Redis-only DAO over an isolated fakeredis (a separate keyspace)."""
    return RedisVenueDAO(GeoRedisClient(fakeredis.FakeRedis(decode_responses=True)))


# ── Ex1: reconstruction parity ───────────────────────────────────────────────
@given('a venue "{vid}" with full scalar fields, dwell times, and a foot-traffic forecast')
def step_full_venue(context, vid):
    context.built = getattr(context, "built", {})
    context.built[vid] = _full_venue(vid)


@when('venue "{vid}" is stored with scalars in columns and only nested fields in the residual JSON')
def step_store_normalized(context, vid):
    context.repository.upsert_venue(context.built[vid])


@then('reconstructing venue "{vid}" from the repository equals the venue rebuilt from the old full payload')
def step_reconstruction_parity(context, vid):
    row = context.rds_store.get_venue(vid)
    reconstructed = context.repository.get_venue(vid)         # columns + residual
    from_payload = Venue.model_validate(row["payload"])       # retained v1 baseline
    assert canonical_venue(reconstructed) == canonical_venue(from_payload), (
        f"v2 reconstruction differs from v1 payload for {vid}"
    )


@then('the projector projects venue "{vid}" to Redis identically to before the change')
def step_projection_parity(context, vid):
    v1_dao = _fresh_dao()
    project_v1_from_payload(context.rds_store, v1_dao)        # pre-change (payload)
    v2_dao = _fresh_dao()
    RedisProjectionService(v2_dao, context.rds_store).rebuild_redis_from_rds()  # v2
    result = diff_serving_snapshots(serving_snapshot(v1_dao), serving_snapshot(v2_dao))
    assert result.passing, result.mismatches


# ── Ex1: no scalar duplicated in the residual ────────────────────────────────
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


# ── Equivalence harness: RDS golden diff ─────────────────────────────────────
@given('a venue whose v2 reconstruction differs from its retained v1 reconstruction')
def step_drifted_venue(context):
    context.repository.upsert_venue(_full_venue("vd"))
    # Corrupt the retained v1 payload so it diverges from the v2 columns. The fake
    # returns the live row, so mutating here perturbs only the v1 baseline.
    context.rds_store.get_venue("vd")["payload"]["venue_name"] = "DRIFTED NAME"


@given('venues whose v2 reconstruction equals their retained v1 reconstruction')
def step_aligned_venues(context):
    context.repository.upsert_venue(_full_venue("va"))
    context.repository.upsert_venue(_full_venue("vb"))


@when("the step's RDS golden diff runs over all venues")
def step_run_golden_diff(context):
    context.diff = rds_venue_golden_diff(context.rds_store)


@then('the golden diff returns a non-passing result')
def step_diff_non_passing(context):
    assert not context.diff.passing


@then('it reports the mismatching venue id and field with no payload secrets')
def step_diff_reports_field(context):
    assert any(
        m["venue_id"] == "vd" and "venue_name" in m["fields"]
        for m in context.diff.mismatches
    ), context.diff.mismatches
    # The report carries only venue_id + field names, never values/secrets.
    for m in context.diff.mismatches:
        assert set(m.keys()) <= {"venue_id", "fields"}, m
        assert "DRIFTED NAME" not in str(m)


@then('the golden diff returns a passing result with zero mismatches')
def step_diff_passing(context):
    assert context.diff.passing and context.diff.mismatch_count == 0, context.diff.mismatches


# ── Equivalence harness: Redis shadow projection ─────────────────────────────
@given('a pre-change snapshot of the Redis serving state and geo index')
def step_pre_change_snapshot(context):
    context.repository.upsert_venue(_full_venue("v6"))
    context.repository.upsert_venue(_full_venue("v7"))
    context.repository.set_live_forecast(_live("v6"))  # exempt; must not affect parity
    context.v1_dao = _fresh_dao()
    project_v1_from_payload(context.rds_store, context.v1_dao)
    context.snapshot = serving_snapshot(context.v1_dao)


@when('the projector re-projects the v2 shape into a separate shadow keyspace')
def step_shadow_project(context):
    context.shadow_dao = _fresh_dao()
    RedisProjectionService(context.shadow_dao, context.rds_store).rebuild_redis_from_rds()
    context.shadow_snap = serving_snapshot(context.shadow_dao)


@then('the shadow serving values and geo membership and coordinates equal the snapshot')
def step_shadow_equals_snapshot(context):
    result = diff_serving_snapshots(context.snapshot, context.shadow_snap)
    assert result.passing, result.mismatches


@then('live busyness is exempt from the comparison')
def step_live_exempt(context):
    # The shadow projected live busyness; the v1 reference did not — yet the
    # serving snapshots match, proving live busyness is outside the comparison.
    assert context.shadow_dao.get_live_forecast("v6") is not None
    assert context.v1_dao.get_live_forecast("v6") is None
    assert diff_serving_snapshots(context.snapshot, context.shadow_snap).passing
