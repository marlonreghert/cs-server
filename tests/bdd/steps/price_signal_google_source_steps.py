"""Behave steps for tests/bdd/enrichment/price-signal-google-source.feature.

Drives the price-tier derivation end to end over the RDS-backed repository:
- "enriched" runs GooglePlacesEnrichmentService.enrich_venue with a stubbed Google
  details response, then asserts on the column reconstruction (repository.get_venue).
- "created" runs the add-venue-by-address handler with a stubbed Google match.
- "migration applied" simulates the 0013 data step (0 -> NULL) on the fake store
  using the production normalize rule (the real DDL is integration-validated
  post-provisioning, per the plan).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from behave import given, when, then  # type: ignore[import-untyped]

from app.handlers.add_venue_handler import AddVenueByAddressRequest
from app.models import NewVenueResponse, PriceRange, Venue
from app.models.vibe_attributes import GooglePlacesDetailsResponse
from app.services.price_signal import normalize_legacy_price_level

VENUE_ID = "v-price-1"
PLACE_ID = "places/ChIJpricetest"


# ── shared setup helpers ──────────────────────────────────────────────────────
def _init(context) -> None:
    """Set per-scenario price-signal defaults if absent. Idempotent (no guard
    flag) so it is safe to call from every Background/Given step."""
    for attr, default in (
        ("price_enum", None),          # raw Google priceLevel enum string
        ("price_range", None),         # PriceRange or None
        ("primary_type", "restaurant"),
        ("besttime_price", None),      # raw BestTime price tier
    ):
        if not hasattr(context, attr):
            setattr(context, attr, default)


def _details(context) -> GooglePlacesDetailsResponse:
    return GooglePlacesDetailsResponse(
        place_id=PLACE_ID,
        business_status="OPERATIONAL",
        primary_type=getattr(context, "primary_type", "restaurant"),
        price_level=getattr(context, "price_enum", None),
        price_range=getattr(context, "price_range", None),
    )


def _seed_venue(context) -> None:
    """Seed a price-relevant venue carrying a stale legacy `0` tier (the
    pre-migration reality enrichment must correct to NULL when no signal wins)."""
    context.repository.upsert_venue(
        Venue(
            venue_id=VENUE_ID,
            venue_name="Vasto Restaurante Recife",
            venue_address="Av. Boa Viagem 1, Recife - PE",
            venue_lat=-8.12,
            venue_lng=-34.90,
            venue_type="OTHER",
            price_level=0,
            besttime_price_level=getattr(context, "besttime_price", None),
        )
    )


def _enrich(context) -> None:
    _seed_venue(context)
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_details(context)
    )
    asyncio.run(context.enrichment_service.enrich_venue(VENUE_ID, PLACE_ID))
    context.result_venue = context.repository.get_venue(VENUE_ID)


# ── background (descriptive; the behavior is asserted by the scenarios) ────────
@given("enrichment derives the served price tier in the order")
def step_derivation_order(context):
    _init(context)


@given("the served price tier is an integer 1 to 4 or null")
def step_tier_range(context):
    _init(context)


@given("the served price tier is never 0")
def step_tier_never_zero(context):
    _init(context)


# ── given: program the Google price signals ───────────────────────────────────
@given("a price-relevant venue whose Google details carry a priceLevel enum of {enum}")
def step_enum(context, enum):
    _init(context)
    context.price_enum = enum


@given('a price-relevant venue whose Google priceLevel enum is {enum}')
def step_enum_is(context, enum):
    _init(context)
    context.price_enum = enum


@given(
    "a price-relevant venue whose Google details carry both a priceLevel enum of "
    "{enum} and a priceRange of {currency} {pmin:d} to {pmax:d}"
)
def step_enum_and_range(context, enum, currency, pmin, pmax):
    _init(context)
    context.price_enum = enum
    context.price_range = PriceRange(currency=currency, min=pmin, max=pmax)


@given("a price-relevant venue whose Google details carry a priceRange of {currency} {pmin:d} to {pmax:d}")
def step_range(context, currency, pmin, pmax):
    _init(context)
    context.price_range = PriceRange(currency=currency, min=pmin, max=pmax)


@given("a price-relevant venue whose Google priceRange has a startPrice of {currency} {pmin:d} and no endPrice")
def step_range_unbounded(context, currency, pmin):
    _init(context)
    context.price_range = PriceRange(currency=currency, min=pmin, max=None)


@given("the venue has no usable Google priceLevel enum")
def step_no_enum(context):
    _init(context)
    context.price_enum = None


@given("the venue has no usable Google priceRange")
def step_no_range(context):
    _init(context)
    context.price_range = None


@given("a price-relevant venue whose Google details carry neither a priceLevel enum nor a priceRange")
def step_neither(context):
    _init(context)
    context.price_enum = None
    context.price_range = None


@given("a price-relevant venue with no Google price signal")
def step_no_google_signal(context):
    _init(context)
    context.price_enum = None
    context.price_range = None


@given("the venue has no BestTime price")
def step_no_besttime(context):
    _init(context)
    context.besttime_price = None


@given("the venue has a BestTime price tier of {tier:d}")
def step_besttime_tier(context, tier):
    _init(context)
    context.besttime_price = tier


@given("a venue selected by Google primaryType as a non-priceable place such as a mall or park")
def step_non_priceable(context):
    _init(context)
    context.primary_type = "shopping_mall"
    context.price_enum = None
    context.price_range = None


# ── given/when: add-venue-by-address ──────────────────────────────────────────
@given("a venue added by address whose Google match carries a priceRange of {currency} {pmin:d} to {pmax:d} and no priceLevel enum")
def step_add_venue_range(context, currency, pmin, pmax):
    _init(context)
    context.price_range = PriceRange(currency=currency, min=pmin, max=pmax)
    context.price_enum = None


@when("the venue is created")
def step_venue_created(context):
    new_venue_id = "bt-created-1"
    context.besttime.programmed_add_venue = NewVenueResponse.model_validate(
        {
            "status": "OK",
            "venue_info": {
                "venue_id": new_venue_id,
                "venue_name": "Vasto Restaurante Recife",
                "venue_address": "Av. Boa Viagem 1, Recife - PE",
                "venue_lat": -8.12,
                "venue_lon": -34.90,
                "price_level": 0,
            },
            "analysis": [],
        }
    )
    # BestTime live forecast fetched inline after create; program an unavailable one.
    from app.models import LiveForecastResponse, VenueInfo, Analysis

    context.besttime.programmed_live_forecast = LiveForecastResponse(
        status="Error", venue_info=VenueInfo(venue_id=new_venue_id), analysis=Analysis()
    )
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_details(context)
    )
    request = AddVenueByAddressRequest.model_validate(
        {
            "venue_name": "Vasto Restaurante Recife",
            "venue_address": "Av. Boa Viagem 1, Recife - PE",
            "venue_lat": -8.12,
            "venue_lng": -34.90,
            "place_id": PLACE_ID,
        }
    )
    outcome = asyncio.run(context.add_venue_handler.add(request))
    context.add_outcome = outcome
    context.result_venue = context.venue_dao.get_venue(outcome.body.get("venue_id"))


# ── when: enrichment ──────────────────────────────────────────────────────────
@when("the venue is enriched")
def step_venue_enriched(context):
    _enrich(context)


# ── when: migration ───────────────────────────────────────────────────────────
@given("existing venues persisted with price_level values of {values}")
def step_seed_price_levels(context, values):
    _init(context)
    parsed = [int(v.strip()) for v in values.replace("and", ",").split(",") if v.strip()]
    context.seeded_price_levels = {}
    for i, lvl in enumerate(parsed):
        vid = f"legacy-{i}-pl{lvl}"
        context.repository.upsert_venue(
            Venue(
                venue_id=vid,
                venue_name=f"Legacy {lvl}",
                venue_address="Rua Y, Recife",
                venue_lat=-8.05,
                venue_lng=-34.88,
                price_level=lvl,
            )
        )
        context.seeded_price_levels[vid] = lvl


@when("the price-signal migration is applied")
def step_apply_migration(context):
    # Simulate the 0013 data step (UPDATE ... SET price_level=NULL WHERE =0) on the
    # in-memory store using the production normalize rule. The real DDL is
    # integration-validated post-provisioning; this asserts the 0 -> NULL behavior.
    store = context.rds_store
    for vid in list(store.venues.keys()):
        row = store.venues[vid]
        row["price_level"] = normalize_legacy_price_level(row.get("price_level"))


# ── then: assertions ──────────────────────────────────────────────────────────
@then("its served price_level is derived from the enum as tier {tier:d}")
def step_assert_tier(context, tier):
    assert context.result_venue.price_level == tier, (
        f"expected tier {tier}, got {context.result_venue.price_level}"
    )


@then("its served price_level resolves to an expensive tier of 3 or 4")
def step_assert_expensive(context):
    assert context.result_venue.price_level in (3, 4), (
        f"expected tier 3 or 4, got {context.result_venue.price_level}"
    )


@then("its served price_level resolves to an expensive tier of 3 or 4 from the range")
def step_assert_expensive_from_range(context):
    assert context.result_venue.price_level in (3, 4), (
        f"expected tier 3 or 4, got {context.result_venue.price_level}"
    )


@then("its served price_level is tier {tier:d} from BestTime")
def step_assert_tier_besttime(context, tier):
    assert context.result_venue.price_level == tier, (
        f"expected tier {tier}, got {context.result_venue.price_level}"
    )


@then('its price_level_source is recorded as "{source}"')
def step_assert_source(context, source):
    assert context.result_venue.price_level_source == source, (
        f"expected source {source!r}, got {context.result_venue.price_level_source!r}"
    )


@then("its price_level_source is null")
def step_assert_source_null(context):
    assert context.result_venue.price_level_source is None, (
        f"expected source None, got {context.result_venue.price_level_source!r}"
    )


@then("its served price_level is null")
def step_assert_tier_null(context):
    assert context.result_venue.price_level is None, (
        f"expected price_level None, got {context.result_venue.price_level}"
    )


@then("its served price_level is not 0")
def step_assert_not_zero(context):
    assert context.result_venue.price_level != 0, "price_level must never be 0"


@then('its price_range is persisted as currency "{currency}" with min {pmin:d} and max {pmax:d}')
def step_assert_range(context, currency, pmin, pmax):
    pr = context.result_venue.price_range
    assert pr is not None, "expected a persisted price_range"
    assert pr.currency == currency, f"currency {pr.currency!r} != {currency!r}"
    assert pr.min == pmin, f"min {pr.min} != {pmin}"
    assert pr.max == pmax, f"max {pr.max} != {pmax}"


@then('its price_range is persisted as currency "{currency}" with min {pmin:d} and a null max')
def step_assert_range_null_max(context, currency, pmin):
    pr = context.result_venue.price_range
    assert pr is not None, "expected a persisted price_range"
    assert pr.currency == currency, f"currency {pr.currency!r} != {currency!r}"
    assert pr.min == pmin, f"min {pr.min} != {pmin}"
    assert pr.max is None, f"expected null max, got {pr.max}"


@then("its price_range is null")
def step_assert_range_null(context):
    assert context.result_venue.price_range is None, (
        f"expected price_range None, got {context.result_venue.price_range}"
    )


@then("the raw BestTime price is retained in besttime_price_level")
def step_assert_besttime_retained(context):
    assert context.result_venue.besttime_price_level == context.besttime_price, (
        f"expected besttime_price_level {context.besttime_price}, "
        f"got {context.result_venue.besttime_price_level}"
    )


@then("every venue previously at price_level 0 now has price_level null")
def step_assert_zero_to_null(context):
    for vid, lvl in context.seeded_price_levels.items():
        if lvl == 0:
            row = context.rds_store.venues[vid]
            assert row.get("price_level") is None, (
                f"{vid} expected NULL, got {row.get('price_level')}"
            )


@then("venues at price_level 1 through 4 are left unchanged")
def step_assert_one_to_four_unchanged(context):
    for vid, lvl in context.seeded_price_levels.items():
        if lvl != 0:
            row = context.rds_store.venues[vid]
            assert row.get("price_level") == lvl, (
                f"{vid} expected {lvl}, got {row.get('price_level')}"
            )
