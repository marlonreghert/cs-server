"""Behave steps for
tests/bdd/enrichment/address-components-via-place-details.feature.

Reuses the shared RDS layer environment.py builds per scenario
(context.rds_store, context.repository, context.admin_config_service,
context.venue_address_backfill_service, context.google_places_client) — the
same harness address_components_backfill_steps.py exercises. Venue-identity,
raw-text, vocabulary, precedence, and outcome-assertion steps are shared with
that sibling file's step definitions (Behave loads every step module
globally); this file adds only the phrases specific to the Place Details
swap: turning the switch on explicitly, programming
`fetch_address_components` responses (success/empty/failure), the
address-lookup-outcome assertion, the "no Place Details lookup" guard, the
batch-size cost-ceiling fixture, and the cursor-reset scenario.

Google's answer travels through `fetch_address_components` here (Phase 5's
Place Details route, plans/260906_address-components-via-place-details.md)
— the SAME client method the sibling venue-address-components.feature's
`get_place_details` uses for its wire shape (`longText`/`types`), just a
different, cheaper field mask.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from behave import given, then, when  # type: ignore[import-untyped]

from app.api.google_places_client import GoogleAddressLookupError
from app.config import settings
from app.models.venue import Venue
from app.models.vibe_attributes import VibeAttributes
from app.services.venue_address_backfill_service import (
    ADMIN_CONFIG_CURSOR_KEY,
    ADMIN_CONFIG_GEOCODING_ENABLED_KEY,
)


def _component(text: str, *types: str) -> dict:
    return {"longText": text, "types": list(types)}


# ── Given: the free-tier switch, explicitly on ────────────────────────────
@given("the address backfill geocoding switch is on")
def step_given_switch_on(context):
    context.admin_config_service.set(ADMIN_CONFIG_GEOCODING_ENABLED_KEY, True)


# ── Given: programmed Place Details address-lookup responses ─────────────
@given(
    'Google\'s Place Details address lookup returns address components '
    'with sublocality level 1 "{value}"'
)
def step_given_place_details_returns_sublocality(context, value):
    context.google_places_client.fetch_address_components = AsyncMock(
        return_value=[_component(value, "sublocality_level_1", "political")]
    )


@given(
    'Google\'s Place Details address lookup returns no address components '
    'for "{name}"'
)
def step_given_place_details_returns_no_components(context, name):
    context.google_places_client.fetch_address_components = AsyncMock(return_value=None)


@given(
    'Google\'s Place Details address lookup fails with a transport error '
    'for "{name}"'
)
def step_given_place_details_transport_error(context, name):
    context.google_places_client.fetch_address_components = AsyncMock(
        side_effect=GoogleAddressLookupError("simulated transport failure")
    )


# ── Then: the address-lookup outcome, and the spend-gate guard ───────────
@then('the address lookup outcome "{outcome}" is recorded, not "{other}"')
def step_then_address_lookup_outcome(context, outcome, other):
    assert context.acb_last_result["geocoding_outcome"] == outcome, context.acb_last_result
    assert context.acb_last_result["geocoding_outcome"] != other, context.acb_last_result


@then("no Place Details address lookup was made")
def step_then_no_place_details_lookup(context):
    context.google_places_client.fetch_address_components.assert_not_called()


# ── Given/When/Then: the bounded-batch cost-ceiling fixture ───────────────
@given("5 venues are waiting to be backfilled, all with a Google place id")
def step_given_5_venues_all_with_place_id(context):
    # A benign, always-safe response — this scenario cares about HOW MANY
    # times the client is called, not what it returns. Programmed up front
    # so no venue in the batch can fall through to a real HTTP call if the
    # gate is on when process_one reaches it.
    context.google_places_client.fetch_address_components = AsyncMock(
        return_value=[_component("Some Bairro", "sublocality_level_1", "political")]
    )
    context.acb_batch_venue_ids = []
    for i in range(1, 6):
        venue_id = f"acbpd_batch_v{i}"
        context.repository.upsert_venue(
            Venue(
                venue_id=venue_id, venue_name=f"PD Batch Venue {i}",
                venue_address=f"R. Y, {i} - Recife PE 5010{i}-000 Brazil",
                venue_lat=-8.05, venue_lng=-34.88,
            )
        )
        context.repository.set_vibe_attributes(
            VibeAttributes(venue_id=venue_id, google_place_id=f"ChIJ_pd_{venue_id}")
        )
        context.acb_batch_venue_ids.append(venue_id)


@given("the address backfill batch size is configured to {n:d}")
def step_given_batch_size_configured(context, n):
    original = settings.address_backfill_batch_size
    settings.address_backfill_batch_size = n
    context.add_cleanup(setattr, settings, "address_backfill_batch_size", original)


@when("the address backfill runs one trigger with no explicit limit")
def step_when_runs_one_trigger_no_explicit_limit(context):
    context.acb_last_batch_result = asyncio.run(
        context.venue_address_backfill_service.backfill_batch(limit=None)
    )


@then("exactly {n:d} venues are processed")
def step_then_exactly_n_venues_processed(context, n):
    assert context.acb_last_batch_result["processed"] == n, context.acb_last_batch_result


@then("at most {n:d} Place Details address lookups were made")
def step_then_at_most_n_lookups_made(context, n):
    call_count = context.google_places_client.fetch_address_components.await_count
    assert call_count <= n, call_count


# ── Given/When/Then: the cursor-reset re-sweep mechanism ──────────────────
@given("the backfill cursor is parked at the end of the catalog from a completed sweep")
def step_given_cursor_parked_at_end(context):
    context.acbpd_cursor_venue_ids = []
    for i in range(1, 4):
        venue_id = f"acbpd_cursor_v{i}"
        context.repository.upsert_venue(
            Venue(
                venue_id=venue_id, venue_name=f"Cursor Venue {i}",
                venue_address=f"R. Z, {i} - Recife PE 5020{i}-000 Brazil",
                venue_lat=-8.05, venue_lng=-34.88,
            )
        )
        context.acbpd_cursor_venue_ids.append(venue_id)
    # A cursor value that string-sorts AFTER every venue_id created above, so
    # `venue_id > after_id` selects nothing — simulating a sweep that has
    # already reached the end of the catalog.
    context.admin_config_service.set(
        ADMIN_CONFIG_CURSOR_KEY,
        {"last_venue_id": "zzz_end_of_catalog", "processed_total": 3, "written_total": 0},
    )


@when("an operator resets the backfill cursor to the start")
def step_when_operator_resets_cursor(context):
    context.admin_config_service.set(ADMIN_CONFIG_CURSOR_KEY, {"last_venue_id": None})


@when("the address backfill runs a trigger")
def step_when_runs_a_trigger(context):
    context.acb_last_batch_result = asyncio.run(
        context.venue_address_backfill_service.backfill_batch()
    )


@then("venues from the beginning of the catalog are selected again")
def step_then_venues_from_beginning_selected_again(context):
    assert context.acb_last_batch_result["processed"] == len(context.acbpd_cursor_venue_ids), (
        context.acb_last_batch_result
    )
