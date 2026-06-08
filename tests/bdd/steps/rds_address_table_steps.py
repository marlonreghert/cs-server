"""Behave steps for tests/bdd/persistence/rds-address-table.feature (Ex3).

Address-table-specific steps. The golden-diff and Redis shadow-projection steps
are reused from rds_schema_normalization_steps.py (Ex1), since the harness is
shared. Background steps come from rds_system_of_record_steps.py.
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

from app.models import Venue


def _venue(vid: str, address: str, lat: float, lng: float) -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=f"Bar {vid}",
        venue_address=address, venue_lat=lat, venue_lng=lng, venue_type="BAR",
    )


@given('a venue "{vid}" with address "{address}" at latitude {lat:f} and longitude {lng:f}')
def step_build_address_venue(context, vid, address, lat, lng):
    context.built = getattr(context, "built", {})
    context.built[vid] = _venue(vid, address, lat, lng)


@when('venue "{vid}" is stored under the address-table schema')
def step_store_address_schema(context, vid):
    venue = getattr(context, "built", {}).get(vid) or _venue(vid, f"{vid} address", -8.05, -34.88)
    context.repository.upsert_venue(venue)


@then('a venues.address row exists for "{vid}" with the raw text and coordinates')
def step_address_row_exists(context, vid):
    addr = context.rds_store.get_address(vid)
    built = context.built[vid]
    assert addr is not None, f"no venues.address row for {vid}"
    assert addr["raw_text"] == built.venue_address
    assert addr["lat"] == built.venue_lat and addr["lng"] == built.venue_lng


@then('reconstructing venue "{vid}" yields address "{address}" at latitude {lat:f} and longitude {lng:f}')
def step_reconstruct_address(context, vid, address, lat, lng):
    v = context.repository.get_venue(vid)
    assert v is not None
    assert v.venue_address == address
    assert abs(v.venue_lat - lat) < 1e-9 and abs(v.venue_lng - lng) < 1e-9


@when('the projector rebuilds Redis from RDS')
def step_projector_rebuild(context):
    context.redis_projection_service.rebuild_redis_from_rds()


@then('venue "{vid}" is served from Redis at latitude {lat:f} and longitude {lng:f}')
def step_served_from_redis(context, vid, lat, lng):
    served = context.redis_only_dao.get_venue(vid)
    assert served is not None, f"{vid} not served from Redis"
    assert abs(served.venue_lat - lat) < 1e-9 and abs(served.venue_lng - lng) < 1e-9


@given('a venue "{vid}" stored under the address-table schema from free text only')
def step_store_free_text_only(context, vid):
    context.built = getattr(context, "built", {})
    context.built[vid] = _venue(vid, f"{vid} Rua Sem Numero", -8.05, -34.88)
    context.repository.upsert_venue(context.built[vid])


@then('the venues.address row for "{vid}" has null street, neighborhood, city, and postal code')
def step_components_null(context, vid):
    addr = context.rds_store.get_address(vid)
    assert addr is not None
    for comp in ("street", "neighborhood", "city", "postal_code"):
        assert addr[comp] is None, f"{comp} should be null, got {addr[comp]!r}"


@then('reconstructing venue "{vid}" produces the same serving output as before the migration')
def step_reconstruct_unchanged(context, vid):
    v = context.repository.get_venue(vid)
    built = context.built[vid]
    assert v is not None
    assert v.venue_address == built.venue_address
    assert v.venue_lat == built.venue_lat and v.venue_lng == built.venue_lng
