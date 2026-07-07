"""Behave steps for tests/bdd/enrichment/price-tier-range-first.feature.

Reuses the RDS-backed harness from price_signal_google_source_steps (shared
`context`): "the venue is enriched" runs GooglePlacesEnrichmentService and sets
`context.result_venue`. The backfill steps simulate the data re-derivation on the
fake store using the production `derive_price_signal` rule — the same pattern the
existing migration step uses (the real Alembic/DDL is integration-validated).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from behave import given, when, then  # type: ignore[import-untyped]

from app.config import settings
from app.models import PriceRange, Venue
from app.models.vibe_attributes import GooglePlacesDetailsResponse
from app.services.price_signal import (
    bucket_price_range,
    derive_price_signal,
    price_level_from_enum,
)


def _thresholds():
    return settings.price_range_tier_thresholds


def _seed(context, venue_id, **price) -> None:
    context.repository.upsert_venue(
        Venue(
            venue_id=venue_id,
            venue_name=f"Venue {venue_id}",
            venue_address="Rua Teste, Recife - PE",
            venue_lat=-8.05,
            venue_lng=-34.88,
            venue_type="OTHER",
            **price,
        )
    )


def _enrich(context, venue_id, place_id, enum, price_range):
    _seed(context, venue_id, price_level=0)
    context.google_places_client.get_place_details = AsyncMock(
        return_value=GooglePlacesDetailsResponse(
            place_id=place_id,
            business_status="OPERATIONAL",
            primary_type="restaurant",
            price_level=enum,
            price_range=price_range,
        )
    )
    asyncio.run(context.enrichment_service.enrich_venue(venue_id, place_id))
    return context.repository.get_venue(venue_id)


def _apply_backfill(context) -> None:
    """Re-derive (price_level, source) for every stored venue from its raw signals
    via the production rule — the backfill's behavioural contract."""
    for vid in list(context.rds_store.venues.keys()):
        v = context.repository.get_venue(vid)
        if v is None:
            continue
        sig = derive_price_signal(
            v.google_price_level, v.price_range, v.besttime_price_level
        )
        v.price_level = sig.price_level
        v.price_level_source = sig.source
        context.repository.upsert_venue(v)


# ── enrichment-driven thens ───────────────────────────────────────────────────
@then("its served price_level is derived from the range, not from the enum")
def step_from_range_not_enum(context):
    v = context.result_venue
    expected = bucket_price_range(context.price_range, _thresholds())
    assert v.price_level == expected, f"expected range tier {expected}, got {v.price_level}"
    assert v.price_level_source == "google_range", (
        f"expected source google_range, got {v.price_level_source!r}"
    )


@then("its served price_level is derived from the enum")
def step_from_enum(context):
    v = context.result_venue
    assert v.price_level == price_level_from_enum(context.price_enum)
    assert v.price_level_source == "google_enum", (
        f"expected source google_enum, got {v.price_level_source!r}"
    )


@then("its served price_level is derived from the range lower bound")
def step_from_range_lower(context):
    v = context.result_venue
    expected = bucket_price_range(context.price_range, _thresholds())
    assert v.price_level == expected, f"expected {expected}, got {v.price_level}"
    assert v.price_level_source == "google_range"


# ── two-venue comparison ──────────────────────────────────────────────────────
@given("a price-relevant venue with a priceRange of {currency} {pmin:d} to {pmax:d} and no priceLevel enum")
def step_multi_range(context, currency, pmin, pmax):
    if not hasattr(context, "multi_ranges"):
        context.multi_ranges = []
    context.multi_ranges.append((currency, pmin, pmax))


@when("both venues are enriched")
def step_enrich_both(context):
    context.multi_results = {}
    for i, (cur, pmin, pmax) in enumerate(context.multi_ranges):
        res = _enrich(
            context, f"multi-{i}", f"places/multi{i}", None,
            PriceRange(currency=cur, min=pmin, max=pmax),
        )
        context.multi_results[(pmin, pmax)] = res


@then("the venue priced {currency} {pmin1:d} to {pmax1:d} has a strictly lower served price_level than the venue priced {currency2} {pmin2:d} to {pmax2:d}")
def step_compare(context, currency, pmin1, pmax1, currency2, pmin2, pmax2):
    a = context.multi_results[(pmin1, pmax1)].price_level
    b = context.multi_results[(pmin2, pmax2)].price_level
    assert a is not None and b is not None, f"levels: {a}, {b}"
    assert a < b, f"expected {pmin1}-{pmax1} tier {a} strictly < {pmin2}-{pmax2} tier {b}"


@then('both venues have a price_level_source of "{source}"')
def step_both_source(context, source):
    for r in context.multi_results.values():
        assert r.price_level_source == source, f"got {r.price_level_source!r}"


# ── backfill ──────────────────────────────────────────────────────────────────
@given('existing venues each stored with a priceLevel enum of {enum}, a priceRange of {currency} {pmin:d} to {pmax:d}, and a price_level_source of "{source}"')
def step_seed_backfill(context, enum, currency, pmin, pmax, source):
    context.backfill_ids = []
    for i in range(3):
        vid = f"backfill-{i}"
        _seed(
            context, vid,
            google_price_level=enum,
            price_range=PriceRange(currency=currency, min=pmin, max=pmax),
            price_level=price_level_from_enum(enum),
            price_level_source=source,
        )
        context.backfill_ids.append(vid)


@given("an existing venue with no priceLevel enum, no priceRange, and no BestTime price")
def step_seed_signalless(context):
    context.backfill_ids = ["signalless-0"]
    _seed(
        context, "signalless-0",
        google_price_level=None, price_range=None, besttime_price_level=None,
        price_level=None, price_level_source=None,
    )


@when("the price-tier backfill is applied")
def step_backfill(context):
    _apply_backfill(context)


@given("the price-tier backfill has already been applied")
def step_backfill_already(context):
    context.backfill_ids = []
    for i, (enum, lo, hi) in enumerate(
        (("PRICE_LEVEL_MODERATE", 80, 160), ("PRICE_LEVEL_INEXPENSIVE", 40, 120))
    ):
        vid = f"idem-{i}"
        _seed(
            context, vid,
            google_price_level=enum,
            price_range=PriceRange(currency="BRL", min=lo, max=hi),
            price_level=price_level_from_enum(enum),
            price_level_source="google_enum",
        )
        context.backfill_ids.append(vid)
    _apply_backfill(context)
    context.backfill_snapshot = {
        vid: (
            context.repository.get_venue(vid).price_level,
            context.repository.get_venue(vid).price_level_source,
        )
        for vid in context.backfill_ids
    }


@when("the price-tier backfill is applied again")
def step_backfill_again(context):
    _apply_backfill(context)


@then("each such venue's price_level is recomputed from the range")
def step_backfill_from_range(context):
    for vid in context.backfill_ids:
        v = context.repository.get_venue(vid)
        expected = bucket_price_range(v.price_range, _thresholds())
        assert v.price_level == expected, f"{vid}: {v.price_level} != range {expected}"


@then('each such venue\'s price_level_source becomes "{source}"')
def step_backfill_source(context, source):
    for vid in context.backfill_ids:
        v = context.repository.get_venue(vid)
        assert v.price_level_source == source, f"{vid}: {v.price_level_source!r}"


@then("no venue's price_level or price_level_source changes")
def step_backfill_idempotent(context):
    for vid in context.backfill_ids:
        v = context.repository.get_venue(vid)
        got = (v.price_level, v.price_level_source)
        assert got == context.backfill_snapshot[vid], (
            f"{vid} changed: {got} != {context.backfill_snapshot[vid]}"
        )


@then("its served price_level remains null")
def step_remains_null(context):
    v = context.repository.get_venue(context.backfill_ids[0])
    assert v.price_level is None, f"got {v.price_level}"


@then("its price_level_source remains null")
def step_source_remains_null(context):
    v = context.repository.get_venue(context.backfill_ids[0])
    assert v.price_level_source is None, f"got {v.price_level_source!r}"
