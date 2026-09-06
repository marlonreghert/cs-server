"""Behave steps for tests/bdd/enrichment/venue-address-components.feature.

Reuses the shared RDS layer environment.py already builds per scenario
(context.rds_store, context.repository, context.enrichment_service,
context.google_places_client) — the same harness
venue_google_enrichment_steps.py exercises, so "Casa Bacurau" is enriched
through the real GooglePlacesEnrichmentService.enrich_venue, never a
bespoke shortcut.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import respx
from behave import given, then, when  # type: ignore[import-untyped]

from app.api.google_places_client import GOOGLE_PLACES_API_BASE, VIBE_FIELDS_MASK
from app.models import Venue
from app.models.vibe_attributes import GooglePlacesDetailsResponse

_VENUE_ID = "vac_casa_bacurau"
_PLACE_ID = "places/ChIJvacCasaBacurau"


def _component(text: str, *types: str) -> dict:
    return {"longText": text, "shortText": text, "types": list(types)}


def _details(address_components=None) -> GooglePlacesDetailsResponse:
    return GooglePlacesDetailsResponse(
        place_id=_PLACE_ID, address_components=address_components,
    )


# ── Background ───────────────────────────────────────────────────────────
@given('"Casa Bacurau" is a venue with a Google place id')
def step_given_venue_with_place_id(context):
    context.rds_store.upsert_venue(
        Venue(
            venue_id=_VENUE_ID, venue_name="Casa Bacurau",
            venue_address="R. Test, 1 - Recife PE", venue_lat=-8.05, venue_lng=-34.88,
        )
    )
    context.vac_place_id = _PLACE_ID
    context.vac_admin_area_level_2 = None


# ── Given: programmed Places responses ────────────────────────────────────
@given('Google Places returns address components with sublocality level 1 "{value}"')
def step_given_sublocality_level_1(context, value):
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_details([_component(value, "sublocality_level_1", "political")])
    )


@given('Google Places returns address components with sublocality "{value}" and no sublocality level 1')
def step_given_sublocality_only(context, value):
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_details([_component(value, "sublocality", "political")])
    )


@given("Google Places returns address components with only an administrative area level 2")
def step_given_admin_area_level_2_only(context):
    context.vac_admin_area_level_2 = "Igarassu"
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_details(
            [_component(context.vac_admin_area_level_2, "administrative_area_level_2", "political")]
        )
    )


@given("Google Places returns address components with no sublocality of any kind")
def step_given_no_sublocality_of_any_kind(context):
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_details([_component("BR", "country", "political")])
    )


@given("Google Places returns route, sublocality, locality and postal code components")
def step_given_full_component_set(context):
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_details([
            _component("Rua Abdon Batista", "route"),
            _component("Santo Amaro", "sublocality_level_1", "political"),
            _component("Recife", "locality", "political"),
            _component("50100-460", "postal_code"),
        ])
    )


@given('the stored address neighborhood is "{value}"')
def step_given_stored_neighborhood(context, value):
    context.repository.update_venue_address_components(_VENUE_ID, source="google", neighborhood=value)


# ── When ─────────────────────────────────────────────────────────────────
@when('"Casa Bacurau" is enriched')
def step_when_enriched(context):
    asyncio.run(
        context.enrichment_service.enrich_venue(_VENUE_ID, context.vac_place_id, force_refresh=True)
    )


@when('the Places details request for "Casa Bacurau" is built')
def step_when_details_request_built(context):
    """Exercises the REAL client + real HTTP layer (respx intercepts only the
    transport), so the captured `X-Goog-FieldMask` header is what the client
    code path actually builds — not a re-statement of the source constant."""
    with respx.mock:
        route = respx.get(url__regex=rf"{GOOGLE_PLACES_API_BASE}/places/.*").mock(
            return_value=httpx.Response(200, json={"id": _PLACE_ID})
        )
        asyncio.run(context.google_places_client.get_place_details(_PLACE_ID))
        assert route.called, "no request reached the Places details endpoint"
        context.vac_field_mask = route.calls.last.request.headers.get("x-goog-fieldmask")


# ── Then ─────────────────────────────────────────────────────────────────
@then('the stored address neighborhood is "{value}"')
def step_then_stored_neighborhood_is(context, value):
    addr = context.rds_store.get_address(_VENUE_ID)
    assert addr is not None
    assert addr["neighborhood"] == value, addr


@then("the stored address neighborhood is the administrative area level 2")
def step_then_stored_neighborhood_is_admin_area(context):
    addr = context.rds_store.get_address(_VENUE_ID)
    assert addr is not None
    assert addr["neighborhood"] == context.vac_admin_area_level_2, addr


@then('the stored address neighborhood is still "{value}"')
def step_then_stored_neighborhood_is_still(context, value):
    step_then_stored_neighborhood_is(context, value)


@then("the stored address street, city and postal code are populated")
def step_then_street_city_postal_populated(context):
    addr = context.rds_store.get_address(_VENUE_ID)
    assert addr is not None
    assert addr.get("street"), addr
    assert addr.get("city"), addr
    assert addr.get("postal_code"), addr


@then("the field mask includes the address components")
def step_then_field_mask_includes_address_components(context):
    fields = (context.vac_field_mask or "").split(",")
    assert "addressComponents" in fields, context.vac_field_mask
    # Sent on the SAME request as everything else — never a second, separate
    # call — so VIBE_FIELDS_MASK itself must carry it too.
    assert "addressComponents" in VIBE_FIELDS_MASK.split(",")


@then("the field mask requests no tier above the one it already requested")
def step_then_field_mask_no_new_tier(context):
    """This mask was ALREADY billed at the top Enterprise + Atmosphere tier
    (reviews, priceRange, generativeSummary, the whole atmosphere block) —
    proving addressComponents rides the SAME request rather than opening a
    second, separately-tiered one. Confirming addressComponents' OWN tier
    against Google's current SKU table is explicitly a manual check (see
    the plan's Evidence section) — nothing on disk can verify Google's
    pricing page from inside a test."""
    fields = set((context.vac_field_mask or "").split(","))
    already_top_tier = {"reviews", "priceRange", "generativeSummary", "liveMusic", "allowsDogs"}
    missing = already_top_tier - fields
    assert not missing, f"pre-existing top-tier fields dropped: {missing}"
