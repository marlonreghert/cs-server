"""Behave steps for tests/bdd/persistence/venue-type-override.feature.

Drives the real POST/DELETE /admin/venues/{id}/type-override routes through the
TestClient. The override endpoint reads/writes vibe_attributes via the RDS-backed
repository (pipeline_repository); the harness default wires pipeline_repository to
the Redis DAO, so these steps point it at the RDS-backed context.repository — the
same repository the enrichment service uses — so the write is durable and read
back consistently. Enrichment-guard behavior (lock survives re-enrichment) is
covered by unit tests, not here.
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

from app.models.vibe_attributes import VibeAttributes


def _use_rds_repo(context):
    # Point the admin endpoint at the RDS-backed repository (system of record),
    # matching production where pipeline_repository is the RDS VenueRepository.
    context.container.pipeline_repository = context.repository


@given('a venue "{venue_id}" whose google primary type is "{gtype}"')
def step_seed_venue(context, venue_id, gtype):
    _use_rds_repo(context)
    context.repository.set_vibe_attributes(
        VibeAttributes(
            venue_id=venue_id,
            google_place_id=f"place-{venue_id}",
            google_primary_type=gtype,
        )
    )


@when('the operator overrides venue "{venue_id}" to category "{category}"')
def step_override(context, venue_id, category):
    _use_rds_repo(context)
    context.response = context.client.post(
        f"/admin/venues/{venue_id}/type-override", json={"category": category}
    )


@given('the operator overrides venue "{venue_id}" to category "{category}"')
def step_override_given(context, venue_id, category):
    step_override(context, venue_id, category)


@when('the operator clears the type override for venue "{venue_id}"')
def step_clear(context, venue_id):
    _use_rds_repo(context)
    context.response = context.client.delete(
        f"/admin/venues/{venue_id}/type-override"
    )


@then('the response category is "{category}"')
def step_response_category(context, category):
    got = context.response.json().get("category")
    assert got == category, f"expected category {category}, got {got}"


@then('the response google primary type is "{gtype}"')
def step_response_gtype(context, gtype):
    got = context.response.json().get("google_primary_type")
    assert got == gtype, f"expected google_primary_type {gtype}, got {got}"


@then('venue "{venue_id}" has stored google primary type "{gtype}"')
def step_stored_gtype(context, venue_id, gtype):
    va = context.repository.get_vibe_attributes(venue_id)
    assert va is not None, f"venue {venue_id} has no vibe_attributes"
    assert va.google_primary_type == gtype, (
        f"expected stored {gtype}, got {va.google_primary_type}"
    )


@then('venue "{venue_id}" has its primary type locked')
def step_locked(context, venue_id):
    va = context.repository.get_vibe_attributes(venue_id)
    assert va is not None and getattr(va, "primary_type_locked", False), (
        f"venue {venue_id} is not primary-type-locked"
    )


@then('venue "{venue_id}" no longer has its primary type locked')
def step_unlocked(context, venue_id):
    va = context.repository.get_vibe_attributes(venue_id)
    assert va is not None and not getattr(va, "primary_type_locked", False), (
        f"venue {venue_id} is still primary-type-locked"
    )
