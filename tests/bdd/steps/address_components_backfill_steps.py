"""Behave steps for tests/bdd/enrichment/address-components-backfill.feature.

Reuses the shared RDS layer environment.py builds per scenario
(context.rds_store, context.repository, context.admin_config_service,
context.enrichment_service, context.venue_address_backfill_service,
context.google_places_client) — the same harness
venue_address_components_steps.py exercises, so every scenario runs through
the REAL parser/vocabulary/precedence code, never a bespoke shortcut.

Google's answer travels through `fetch_address_components` here
(plans/260906_address-components-via-place-details.md's Place Details
address-components route, which replaced Phase 3's now-dead
Geocoding-by-place_id route), NOT `get_place_details` (the sibling
venue-address-components.feature's own, full-mask Place Details route) —
both return the SAME wire shape (`{"longText", "types"}` per component,
`shortText` present but unused by `map_address_components`); they differ
in field mask and billing tier, not in response shape. The legacy
`geocode_by_place_id` method still exists in
app.api.google_places_client (kept for clean rollback, confirmed
non-functional against this project's GCP key — see its own docstring)
but production code no longer calls it anywhere; see
`step_then_legacy_geocode_by_place_id_never_called` below, which asserts
exactly that.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

from behave import given, then, when  # type: ignore[import-untyped]

from app.metrics import VENUE_ADDRESS_COMPONENTS_TOTAL
from app.models.venue import Venue
from app.models.vibe_attributes import VibeAttributes
from app.services.venue_address_backfill_service import ADMIN_CONFIG_GEOCODING_ENABLED_KEY

_DEFAULT_LAT, _DEFAULT_LNG = -8.0476, -34.8770  # Recife

# Real coordinates for the specific named locations a few scenarios use to
# corroborate (or fail to corroborate) an ambiguous city match.
_NAMED_LOCATIONS = {
    "Boa Vista, Roraima": (2.8235, -60.6758),
    "metropolitan Recife": (-8.0476, -34.8770),
}

# Real coordinates for the specific city names scenarios approve into the
# vocabulary — Recife/São Paulo are already state capitals; listed anyway so
# the Given step needs no special-casing.
_CITY_COORDS = {
    "Niterói": (-22.8832, -43.1034),
    "São Paulo": (-23.5505, -46.6333),
    "Recife": (-8.0476, -34.8770),
    "Boa Vista": (2.8235, -60.6758),  # Roraima's capital
}


def _venue_id_for(context, name: str) -> str:
    """Deterministic per-scenario venue id, memoized by name — mirrors
    events_serving_projection_steps.py's own `_venue_id_for`."""
    if not hasattr(context, "acb_venue_ids"):
        context.acb_venue_ids = {}
    if name not in context.acb_venue_ids:
        context.acb_venue_ids[name] = f"acb_{uuid.uuid4().hex[:12]}"
    context.acb_current_venue = name
    return context.acb_venue_ids[name]


def _ensure_venue(context, name: str, *, lat=_DEFAULT_LAT, lng=_DEFAULT_LNG) -> str:
    """Create the venue if this is the first time `name` is mentioned
    (default raw_text; a later "its raw address text is ..." step
    overwrites it), otherwise a no-op that just tracks `name` as current."""
    venue_id = _venue_id_for(context, name)
    if context.rds_store.get_address(venue_id) is None:
        context.repository.upsert_venue(
            Venue(
                venue_id=venue_id, venue_name=name,
                venue_address="R. Test, 1 - Recife PE 50000-000 Brazil",
                venue_lat=lat, venue_lng=lng,
            )
        )
    return venue_id


def _geocode_component(text: str, *types: str) -> dict:
    """`fetch_address_components`'s wire shape — the same
    `{"longText", "types"}` per-component shape
    venue_address_components_steps.py's sibling `_component` helper builds
    for `get_place_details` (both are Places (New) Details responses, just
    different field masks); name kept for continuity with this file's own
    step text ("Geocoding returns...")."""
    return {"longText": text, "types": list(types)}


# ── Given: venue identity + Google eligibility ────────────────────────────
@given('"{name}" is a venue with a Google place id and no stored address components')
def step_given_venue_with_place_id_no_components(context, name):
    venue_id = _ensure_venue(context, name)
    context.repository.set_vibe_attributes(
        VibeAttributes(venue_id=venue_id, google_place_id=f"ChIJ_{venue_id}")
    )


@given('"{name}" has a Google place id')
def step_given_venue_with_place_id(context, name):
    step_given_venue_with_place_id_no_components(context, name)


@given('"{name}" is a venue with no Google place id')
def step_given_venue_no_place_id(context, name):
    _ensure_venue(context, name)  # no vibe_attributes row -> no place_id


@given('"{name}" is a venue with no Google place id, located near Boa Vista, Roraima')
def step_given_venue_no_place_id_near_boa_vista(context, name):
    lat, lng = _NAMED_LOCATIONS["Boa Vista, Roraima"]
    _ensure_venue(context, name, lat=lat, lng=lng)


@given('"{name}" is a venue with no Google place id, located in metropolitan Recife')
def step_given_venue_no_place_id_metro_recife(context, name):
    lat, lng = _NAMED_LOCATIONS["metropolitan Recife"]
    _ensure_venue(context, name, lat=lat, lng=lng)


@given('"{name}" has no Google place id this run')
def step_given_no_place_id_this_run(context, name):
    _ensure_venue(context, name)  # already true by default; documents intent


@given('its raw address text is "{text}"')
def step_given_raw_address_text(context, text):
    name = context.acb_current_venue
    venue_id = _venue_id_for(context, name)
    existing = context.rds_store.get_address(venue_id)
    lat = existing["lat"] if existing else _DEFAULT_LAT
    lng = existing["lng"] if existing else _DEFAULT_LNG
    context.repository.upsert_venue(
        Venue(venue_id=venue_id, venue_name=name, venue_address=text, venue_lat=lat, venue_lng=lng)
    )


# ── Given: the approved vocabulary ─────────────────────────────────────────
def _add_to_vocabulary(context, name: str, *, ambiguous: bool) -> None:
    lat, lng = _CITY_COORDS[name]
    current = context.admin_config_service.get("address_city_vocabulary") or []
    current = [e for e in current if e.get("name") != name]
    current.append({"name": name, "lat": lat, "lng": lng, "ambiguous": ambiguous})
    context.admin_config_service.set("address_city_vocabulary", current)


@given('"{name}" is in the approved city vocabulary')
def step_given_in_vocabulary(context, name):
    _add_to_vocabulary(context, name, ambiguous=False)


@given('"{name}" is in the approved city vocabulary and flagged ambiguous')
def step_given_in_vocabulary_ambiguous(context, name):
    _add_to_vocabulary(context, name, ambiguous=True)


# ── Given: pre-existing stored values (any provenance) ────────────────────
@given('"{name}" has a stored address neighborhood of "{value}" with source "{source}"')
def step_given_stored_neighborhood_with_source(context, name, value, source):
    venue_id = _ensure_venue(context, name)
    context.repository.update_venue_address_components(venue_id, source=source, neighborhood=value)


# ── Given: programmed Google Place Details address-lookup responses ───────
# Deliberately worded "Geocoding returns..." (not "Google Places returns...")
# to avoid an AmbiguousStep collision with venue_address_components_steps.py's
# identically-parametrized sibling, which programs a DIFFERENT client method
# (get_place_details, not fetch_address_components) — Behave loads every step
# file globally, so two features cannot share a literal step-text pattern
# even when each lives in its own .feature file. The step TEXT is kept as
# "Geocoding returns..." (this feature file predates the Place Details swap
# and is unmodified by this plan, per its own non-goals) even though the
# underlying client call is now fetch_address_components.
@given('Geocoding returns address components with sublocality level 1 "{value}"')
def step_given_geocode_sublocality_level_1(context, value):
    # Google being asked implies the free-tier switch is on for this run —
    # the ONE scenario that keeps it off says so explicitly (see below) and
    # never uses this step.
    context.admin_config_service.set(ADMIN_CONFIG_GEOCODING_ENABLED_KEY, True)
    context.google_places_client.fetch_address_components = AsyncMock(
        return_value=[_geocode_component(value, "sublocality_level_1", "political")]
    )


@given("the address backfill geocoding switch is off")
def step_given_geocoding_switch_off(context):
    context.admin_config_service.set(ADMIN_CONFIG_GEOCODING_ENABLED_KEY, False)
    context.google_places_client.fetch_address_components = AsyncMock(
        side_effect=AssertionError("fetch_address_components must not be called while the switch is off")
    )


# ── Given: the bounded-batch fixture (5 venues, one non-servable) ─────────
@given("5 venues are waiting to be backfilled, a mix of servable and non-servable")
def step_given_5_venues_batch(context):
    context.acb_batch_venue_ids = []
    for i in range(1, 6):
        venue_id = f"acb_batch_v{i}"
        context.repository.upsert_venue(
            Venue(
                venue_id=venue_id, venue_name=f"Batch Venue {i}",
                venue_address=f"R. X, {i} - Recife PE 5000{i}-000 Brazil",
                venue_lat=-8.05, venue_lng=-34.88,
            )
        )
        context.acb_batch_venue_ids.append(venue_id)
    # Non-servable: deprecated. Must still be reached, never silently skipped.
    context.rds_store.soft_delete_venue("acb_batch_v3", reason="test", source="test")


# ── Given: add-time flow ────────────────────────────────────────────────────
@given('a new venue "{name}" is added with raw address text "{raw_text}"')
def step_given_new_venue_added_with_text(context, name, raw_text):
    venue_id = _venue_id_for(context, name)
    context.repository.upsert_venue(
        Venue(
            venue_id=venue_id, venue_name=name, venue_address=raw_text,
            venue_lat=_DEFAULT_LAT, venue_lng=_DEFAULT_LNG,
        )
    )
    # This scenario exercises AddVenueHandler._enrich_from_google directly —
    # environment.py never wires google_places_enrichment_service onto
    # add_venue_handler (no existing scenario needs it live), so this is
    # scoped to just this scenario's own context, touching no shared state.
    context.add_venue_handler.google_places_enrichment_service = context.enrichment_service


@given('Google Places finds no match for "{name}"')
def step_given_no_google_match_for(context, name):
    context.google_places_client.search_place_id = AsyncMock(return_value=None)


# ── When ─────────────────────────────────────────────────────────────────
@when('the address backfill processes "{name}"')
def step_when_processes(context, name):
    venue_id = _venue_id_for(context, name)
    context.acb_last_result = asyncio.run(context.venue_address_backfill_service.process_one(venue_id))


@when('"{name}" finishes its add-time enrichment')
def step_when_finishes_add_time_enrichment(context, name):
    venue_id = _venue_id_for(context, name)
    venue = context.repository.get_venue(venue_id)
    asyncio.run(context.add_venue_handler._enrich_from_google(venue, None))


@when("the address backfill runs with a batch limit of {limit:d}")
def step_when_runs_with_limit(context, limit):
    if not hasattr(context, "acb_batch_results"):
        context.acb_batch_results = []
    result = asyncio.run(context.venue_address_backfill_service.backfill_batch(limit=limit))
    context.acb_batch_results.append(result)
    context.acb_last_batch_result = result


@when("the address backfill runs repeatedly with a batch limit of {limit:d} until none remain")
def step_when_runs_repeatedly(context, limit):
    if not hasattr(context, "acb_batch_results"):
        context.acb_batch_results = []
    while True:
        result = asyncio.run(context.venue_address_backfill_service.backfill_batch(limit=limit))
        context.acb_batch_results.append(result)
        context.acb_last_batch_result = result
        if result["done"]:
            break


@when('the address backfill runs over "{name}" a second time with an unchanged Google response')
def step_when_reruns_unchanged(context, name):
    venue_id = _venue_id_for(context, name)
    context.repository.set_vibe_attributes(
        VibeAttributes(venue_id=venue_id, google_place_id=f"ChIJ_{venue_id}_rerun")
    )
    context.admin_config_service.set(ADMIN_CONFIG_GEOCODING_ENABLED_KEY, True)
    # "Unchanged" from Google's own perspective: this second call has
    # nothing NEW to contribute (an empty component list) — the exact,
    # pre-existing "unchanged" outcome VENUE_ADDRESS_COMPONENTS_TOTAL
    # already tracks (the response answered none of the four fields).
    context.google_places_client.fetch_address_components = AsyncMock(return_value=[])
    context.acb_components_unchanged_before = (
        VENUE_ADDRESS_COMPONENTS_TOTAL.labels(outcome="unchanged")._value.get()
    )
    context.acb_last_result = asyncio.run(context.venue_address_backfill_service.process_one(venue_id))


# ── Then ─────────────────────────────────────────────────────────────────
def _address_for_current_or_named(context, name: str) -> dict:
    venue_id = _venue_id_for(context, name)
    addr = context.rds_store.get_address(venue_id)
    assert addr is not None, f"no address row for {name!r} ({venue_id})"
    return addr


@then('the stored address neighborhood is "{value}" with source "{source}"')
def step_then_neighborhood_is(context, value, source):
    name = context.acb_current_venue
    addr = _address_for_current_or_named(context, name)
    assert addr["neighborhood"] == value, addr
    assert addr["neighborhood_source"] == source, addr


@then('the stored address neighborhood is still "{value}" with source "{source}"')
def step_then_neighborhood_is_still(context, value, source):
    step_then_neighborhood_is(context, value, source)


@then('the stored address city is "{value}" with source "{source}"')
def step_then_city_is(context, value, source):
    name = context.acb_current_venue
    addr = _address_for_current_or_named(context, name)
    assert addr["city"] == value, addr
    assert addr["city_source"] == source, addr


@then("the stored address neighborhood is still unset")
def step_then_neighborhood_still_unset(context):
    name = context.acb_current_venue
    addr = _address_for_current_or_named(context, name)
    assert addr["neighborhood"] is None, addr
    assert addr["neighborhood_source"] is None, addr


@then("the stored address city is still unset")
def step_then_city_still_unset(context):
    name = context.acb_current_venue
    addr = _address_for_current_or_named(context, name)
    assert addr["city"] is None, addr
    assert addr["city_source"] is None, addr


@then("no manual backfill trigger was required")
def step_then_no_manual_trigger(context):
    """The scenario's own WHEN step ("finishes its add-time enrichment")
    never touched venue_address_backfill_service.backfill_batch/
    process_one at all — the assertion IS the absence of that call, which
    the step definitions above structurally guarantee (this Then exists so
    the scenario states its own acceptance criterion explicitly rather
    than leaving it implicit)."""
    assert not hasattr(context, "acb_last_result"), (
        "a manual backfill call was made — the add-time hook should have "
        "been sufficient on its own"
    )


@then("exactly {n:d} venues are processed and the backfill cursor is saved")
def step_then_exactly_n_processed(context, n):
    from app.services.venue_address_backfill_service import ADMIN_CONFIG_CURSOR_KEY

    assert context.acb_last_batch_result["processed"] == n
    cursor = context.admin_config_service.get(ADMIN_CONFIG_CURSOR_KEY)
    assert cursor is not None and cursor.get("last_venue_id") is not None, cursor


@then("all 5 venues have been processed exactly once, servable and non-servable alike")
def step_then_all_5_processed(context):
    total = sum(r["processed"] for r in context.acb_batch_results)
    assert total == 5, context.acb_batch_results
    for venue_id in context.acb_batch_venue_ids:
        addr = context.rds_store.get_address(venue_id)
        assert addr["city"] == "Recife", f"{venue_id} left unresolved: {addr}"


@then('the address components outcome "{outcome}" is recorded')
def step_then_outcome_recorded(context, outcome):
    after = VENUE_ADDRESS_COMPONENTS_TOTAL.labels(outcome=outcome)._value.get()
    before = getattr(context, "acb_components_unchanged_before", None)
    assert before is not None, "no baseline counter value captured by the When step"
    assert after == before + 1, f"expected {outcome} to increment by 1 ({before} -> {after})"


@then("no Geocoding request was made")
def step_then_no_geocoding_request(context):
    context.google_places_client.fetch_address_components.assert_not_called()


@then("the backfill never calls the legacy geocode_by_place_id method")
def step_then_legacy_geocode_by_place_id_never_called(context):
    """Regression guard for correction (C): once `_maybe_fetch_google_address`
    stopped calling `geocode_by_place_id`, a mock assigned directly onto
    that attribute would go vacuously unused. This asserts against the spy
    environment.py wraps onto EVERY scenario's fresh client (not a mock this
    file assigns), so it stays meaningful even if a scenario never touches
    `fetch_address_components` at all."""
    context.google_places_client.geocode_by_place_id.assert_not_called()
