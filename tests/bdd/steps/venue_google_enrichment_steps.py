"""Behave steps for tests/bdd/enrichment/venue-google-enrichment.feature.

Three enrichment paths, all Google-only on enrichment (no BestTime credit spent
enriching):
- Add-time: the add-venue handler enriches inline via an injected enrichment
  service that shares the handler's DAO. Google details / place_id search are
  stubbed; BestTime calls are asserted NOT to grow during the enrichment step
  (the add itself legitimately calls BestTime once).
- Admin-triggered: enrich_all_venues(force_refresh=False) over the RDS-backed
  repository — skips already-enriched (presence-based), makes zero BestTime calls.
- Backfill: enrich_pending_venues() (Google-only price) — pending-only, reuses the
  empty-vibe-attributes no-match marker, idempotent on re-run, zero BestTime calls.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from behave import given, when, then  # type: ignore[import-untyped]

from app.handlers.add_venue_handler import AddVenueByAddressRequest, AddVenueHandler
from app.models import LiveForecastResponse, NewVenueResponse, PriceRange, Venue, VenueInfo, Analysis
from app.models.vibe_attributes import GooglePlacesDetailsResponse
from app.services.google_places_enrichment_service import GooglePlacesEnrichmentService

_LAT, _LNG = -8.05, -34.88


# ── helpers ───────────────────────────────────────────────────────────────────
def _details(
    *,
    primary_type="bar",
    business_status="OPERATIONAL",
    rating=4.6,
    user_rating_count=320,
    price_enum="PRICE_LEVEL_MODERATE",
    price_range=None,
    weekday_descriptions=None,
    reviews=None,
    place_id="places/ChIJenrichtest",
) -> GooglePlacesDetailsResponse:
    return GooglePlacesDetailsResponse(
        place_id=place_id,
        business_status=business_status,
        primary_type=primary_type,
        rating=rating,
        user_rating_count=user_rating_count,
        price_level=price_enum,
        price_range=price_range,
        weekday_descriptions=weekday_descriptions or ["Segunda-feira: 18:00 – 02:00"],
        reviews=reviews or [{
            "author_name": "A. Reviewer",
            "rating": 5,
            "text": "Great spot",
            "relative_time": "2 weeks ago",
        }],
    )


def _add_handler_with_enrichment(context) -> AddVenueHandler:
    """Rebuild the add handler with an enrichment service wired to the SAME DAO so
    inline enrichment writes land where the assertions read (context.venue_dao)."""
    enrichment = GooglePlacesEnrichmentService(
        google_places_client=context.google_places_client,
        venue_dao=context.venue_dao,
    )
    context.add_enrichment_service = enrichment
    return AddVenueHandler(
        venue_dao=context.venue_dao,
        besttime_api=context.besttime,
        budget_service=context.budget_service,
        redis_client=context.fake_redis,
        google_places_client=context.google_places_client,
        google_places_enrichment_service=enrichment,
    )


def _program_besttime_add(context, venue_id, *, price_level=2):
    context.besttime.programmed_add_venue = NewVenueResponse.model_validate({
        "status": "OK",
        "venue_info": {
            "venue_id": venue_id,
            "venue_name": "Beijupirá Olinda",
            "venue_address": "R. do Amparo 116, Olinda - PE",
            "venue_lat": _LAT,
            "venue_lon": _LNG,
            "price_level": price_level,
        },
        "analysis": [],
    })
    context.besttime.programmed_live_forecast = LiveForecastResponse(
        status="Error", venue_info=VenueInfo(venue_id=venue_id), analysis=Analysis()
    )


def _besttime_calls(context) -> int:
    return len(context.besttime.calls)


def _run_add(context, place_id):
    body = {
        "venue_name": "Beijupirá Olinda",
        "venue_address": "R. do Amparo 116, Olinda - PE",
        "venue_lat": _LAT,
        "venue_lng": _LNG,
    }
    if place_id is not None:
        body["place_id"] = place_id
    request = AddVenueByAddressRequest.model_validate(body)
    outcome = asyncio.run(context.add_handler.add(request))
    context.add_outcome = outcome
    context.added_venue_id = outcome.body.get("venue_id")


def _seed_repo_venue(context, vid, name, *, venue_type="BAR", besttime_price=None, enriched=False):
    context.repository.upsert_venue(Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=name,
        venue_address=f"{vid} addr", venue_lat=_LAT, venue_lng=_LNG,
        venue_type=venue_type, besttime_price_level=besttime_price,
    ))
    if enriched:
        from app.models.vibe_attributes import VibeAttributes
        context.repository.set_vibe_attributes(VibeAttributes(
            venue_id=vid, google_place_id=f"places/{vid}", google_primary_type="bar",
        ))
    return vid


# ══════════════════════════════════════════════════════════════════════════════
# Add-time enrichment
# ══════════════════════════════════════════════════════════════════════════════
@given("an operator adds a venue and selects a Google candidate with a place_id")
def step_add_with_place_id(context):
    context.add_handler = _add_handler_with_enrichment(context)
    context.the_place_id = "places/ChIJbeijupira"
    _program_besttime_add(context, "bt-add-1")
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_details(place_id=context.the_place_id)
    )
    context.google_places_client.search_place_id = AsyncMock(return_value=None)


@given("an operator adds a venue with no place_id")
def step_add_no_place_id(context):
    context.add_handler = _add_handler_with_enrichment(context)
    context.resolved_place_id = "places/ChIJresolved"
    _program_besttime_add(context, "bt-add-2")
    context.google_places_client.search_place_id = AsyncMock(
        return_value=context.resolved_place_id
    )
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_details(place_id=context.resolved_place_id)
    )


@given("Google returns no match or the details call fails for an added venue")
def step_add_google_fails(context):
    context.add_handler = _add_handler_with_enrichment(context)
    _program_besttime_add(context, "bt-add-3", price_level=None)  # no BestTime price either
    # No place_id on request, and search finds nothing -> no enrichment.
    context.google_places_client.search_place_id = AsyncMock(return_value=None)
    context.google_places_client.get_place_details = AsyncMock(return_value=None)


@when("the venue is added")
def step_venue_added(context):
    place_id = getattr(context, "the_place_id", None)
    _run_add(context, place_id)


# ── Then: add-time ────────────────────────────────────────────────────────────
@then("the venue is immediately enriched from Google Places")
@then("the venue is enriched from Google Places")
def step_immediately_enriched(context):
    va = context.venue_dao.get_vibe_attributes(context.added_venue_id)
    assert va is not None and va.google_primary_type, (
        f"venue {context.added_venue_id} should have vibe attrs with a primary type, got {va}"
    )


@then("it has a google primary type, opening hours, reviews, business status, rating, and a Google-derived price")
def step_full_enrichment(context):
    vid = context.added_venue_id
    va = context.venue_dao.get_vibe_attributes(vid)
    assert va is not None and va.google_primary_type == "bar", va
    assert context.venue_dao.get_opening_hours(vid) is not None, "opening hours missing"
    assert context.venue_dao.get_venue_reviews(vid) is not None, "reviews missing"
    venue = context.venue_dao.get_venue(vid)
    assert venue.google_business_status == "OPERATIONAL", venue.google_business_status
    assert venue.rating == 4.6, venue.rating
    # Google-derived price: PRICE_LEVEL_MODERATE -> tier 2, source google_enum.
    assert venue.price_level == 2, venue.price_level
    assert venue.price_level_source == "google_enum", venue.price_level_source


@then("no BestTime call is made during enrichment")
def step_no_besttime_during_enrichment(context):
    # The add legitimately calls BestTime (add_venue_to_account + inline live);
    # enrichment must add ZERO further BestTime calls. Only enrichment-specific
    # BestTime methods would signal a violation (there are none — enrichment is
    # Google-only), so assert no method beyond the add's own appears.
    methods = [c.get("method") for c in context.besttime.calls]
    enrichment_besttime = [
        m for m in methods
        if m not in ("add_venue_to_account", "get_live_forecast", "venue_filter")
    ]
    assert enrichment_besttime == [], f"enrichment made BestTime calls: {enrichment_besttime}"
    # Regression guard: with a request place_id, Google Details is fetched exactly
    # ONCE (enrich_venue owns it; _persist_new_venue must not double-fetch).
    if getattr(context, "the_place_id", None):
        assert context.google_places_client.get_place_details.await_count == 1, (
            "get_place_details must be called exactly once per add "
            f"(got {context.google_places_client.get_place_details.await_count})"
        )


@then("a Google place_id is resolved via Google search")
def step_place_id_resolved(context):
    context.google_places_client.search_place_id.assert_awaited()


@then("the resolved place_id is persisted for future re-enrichment")
def step_place_id_persisted(context):
    va = context.venue_dao.get_vibe_attributes(context.added_venue_id)
    assert va is not None and va.google_place_id == context.resolved_place_id, (
        f"expected persisted place_id {context.resolved_place_id}, got {va.google_place_id if va else None}"
    )


@then("the add still succeeds")
def step_add_succeeds(context):
    assert context.add_outcome.status_code in (200, 201), context.add_outcome.status_code
    assert context.added_venue_id, "no venue_id in add outcome"


@then("the venue's Google fields remain empty")
def step_google_fields_empty(context):
    va = context.venue_dao.get_vibe_attributes(context.added_venue_id)
    # Either no vibe attrs row, or one with no primary type (no Google match).
    assert va is None or not va.google_primary_type, va


@then("no BestTime price fallback is applied")
def step_no_besttime_price_fallback(context):
    # This scenario seeded no BestTime price on the add response, so price stays NULL.
    venue = context.venue_dao.get_venue(context.added_venue_id)
    assert venue.price_level is None, f"expected NULL price, got {venue.price_level}"
    assert venue.price_level_source != "besttime", venue.price_level_source


# ══════════════════════════════════════════════════════════════════════════════
# Admin-triggered enrichment (cron stays disabled)
# ══════════════════════════════════════════════════════════════════════════════
@given("the Google enrichment job is triggered from the admin panel")
def step_admin_enrich_setup(context):
    context.enriched_id = _seed_repo_venue(context, "already_enriched", "Enriched Bar", enriched=True)
    context.pending_id = _seed_repo_venue(context, "pending_bar", "Pending Bar")
    context.google_places_client.search_place_id = AsyncMock(return_value="places/ChIJpend")
    context.google_places_client.get_place_details = AsyncMock(return_value=_details())


@when("it processes the catalog without forcing a refresh")
def step_process_catalog(context):
    context.besttime_before = _besttime_calls(context)
    asyncio.run(context.enrichment_service.enrich_all_venues(force_refresh=False))


@then("already-enriched venues are skipped")
def step_enriched_skipped(context):
    # The already-enriched venue keeps its original place_id (not re-fetched).
    va = context.repository.get_vibe_attributes(context.enriched_id)
    assert va is not None and va.google_place_id == f"places/{context.enriched_id}", va


@then("no BestTime call is made")
def step_no_besttime_at_all(context):
    made = _besttime_calls(context) - getattr(context, "besttime_before", 0)
    assert made == 0, f"enrichment made {made} BestTime call(s): {context.besttime.calls}"


# ══════════════════════════════════════════════════════════════════════════════
# Backfill of pending venues
# ══════════════════════════════════════════════════════════════════════════════
@given("a mix of enriched venues and pending venues with no google primary type")
def step_mix_for_backfill(context):
    context.enriched_id = _seed_repo_venue(context, "bf_enriched", "BF Enriched", enriched=True)
    context.pending_id = _seed_repo_venue(context, "bf_pending", "BF Pending")
    context.google_places_client.search_place_id = AsyncMock(return_value="places/ChIJbf")
    context.google_places_client.get_place_details = AsyncMock(return_value=_details())


@given("a pending venue that Google has no match for")
def step_pending_no_match(context):
    context.pending_id = _seed_repo_venue(context, "bf_nomatch", "BF NoMatch")
    context.google_places_client.search_place_id = AsyncMock(return_value=None)
    context.google_places_client.get_place_details = AsyncMock(return_value=None)


@given("a pending venue that carries a stored BestTime price tier but whose Google details carry no price")
def step_pending_besttime_price_no_google_price(context):
    # Seed a pending venue WITH a stored BestTime tier so the assertion is
    # non-vacuous: Google-only must suppress the besttime fallback -> NULL price.
    context.pending_id = _seed_repo_venue(
        context, "bf_price", "BF Price", besttime_price=3
    )
    context.google_places_client.search_place_id = AsyncMock(return_value="places/ChIJprice")
    context.google_places_client.get_place_details = AsyncMock(
        return_value=_details(price_enum=None, price_range=None)
    )


@when("the pending backfill runs")
@when("the pending backfill enriches it")
def step_backfill_runs(context):
    context.besttime_before = _besttime_calls(context)
    context.backfill_summary = asyncio.run(context.enrichment_service.enrich_pending_venues())


@then("only the pending venues are enriched")
def step_only_pending_enriched(context):
    va = context.repository.get_vibe_attributes(context.pending_id)
    assert va is not None and va.google_primary_type == "bar", (
        f"pending venue should now be enriched, got {va}"
    )


@then("already-enriched venues are not reprocessed")
def step_enriched_not_reprocessed(context):
    # The pre-enriched venue keeps its original place_id (backfill skipped it).
    va = context.repository.get_vibe_attributes(context.enriched_id)
    assert va is not None and va.google_place_id == f"places/{context.enriched_id}", va


@then("the venue is marked as attempted")
def step_marked_attempted(context):
    # No-match venue: an empty vibe_attributes row (google_place_id="") is the marker.
    va = context.repository.get_vibe_attributes(context.pending_id)
    assert va is not None, "no-match venue should have an empty marker row"
    assert not va.google_primary_type, va


@then("a second backfill run does not call Google again for that venue")
def step_second_run_skips(context):
    context.google_places_client.search_place_id.reset_mock()
    context.google_places_client.get_place_details.reset_mock()
    asyncio.run(context.enrichment_service.enrich_pending_venues())
    context.google_places_client.search_place_id.assert_not_awaited()
    context.google_places_client.get_place_details.assert_not_awaited()


@then("the venue's price is empty")
def step_backfill_price_empty(context):
    venue = context.repository.get_venue(context.pending_id)
    assert venue.price_level is None, f"expected NULL price, got {venue.price_level}"


@then("its price source is not BestTime")
def step_backfill_price_source_not_besttime(context):
    venue = context.repository.get_venue(context.pending_id)
    assert venue.price_level_source != "besttime", venue.price_level_source
