"""Behave steps for tests/bdd/enrichment/enrichment-and-budget-hardening.feature.

Covers: the business-status recheck for already-enriched venues (closure
detection without full re-enrichment), the search_place_id no-match-vs-error
split (a transport/quota failure must never poison a venue with the empty
marker), Instagram delete-only-on-definitive-404, the shared scheduler+admin
job concurrency lock, geo-link provenance + month-aware undo, the manual-add
concurrency lock + batch single-flight, the timeout-recovery containment
guard, and photo category tags on the fresh-photos resolve endpoint.

Reuses the harness wired by environment.py (context.repository = RDS-backed
venue DAO, context.rds_store = fake RDS truth, context.add_venue_handler,
context.container, context.client = FastAPI TestClient).
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock

import httpx
import respx
from behave import given, when, then  # type: ignore[import-untyped]

from app.api.google_places_client import GooglePlacesSearchError
from app.dao.venue_budget_dao import VENUE_ADD_COUNTER_KEY_V1
from app.handlers.add_venue_handler import AddVenueByAddressRequest
from app.metrics import VIBE_ATTRIBUTES_FETCH_RESULTS
from app.models import Venue
from app.models.instagram import VenueInstagram
from app.models.new_venue import NewVenueResponse
from app.models.vibe_attributes import GooglePlacesDetailsResponse, VibeAttributes
from app.models.vibe_profile import EvidencePhoto, VenueVibeProfile
from tests.bdd.steps.on_demand_venue_photos_steps import (
    _install_google_transport,
    _override_setting,
)

_LAT, _LNG = -8.05, -34.88


def _venue(vid: str, name: str = "Bar X") -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=name,
        venue_address=f"{vid} address", venue_lat=_LAT, venue_lng=_LNG,
        venue_type="BAR",
    )


# ── Scenario: Fresh photos carry the classifier's category tags ──────────────
@given('"{vid}" has a vibe profile with categorized evidence photos')
def step_vibe_profile_with_evidence(context, vid):
    context.venue_id = vid
    context.repository.set_vibe_attributes(
        VibeAttributes(venue_id=vid, google_place_id="places/CJcategorized")
    )
    context.repository.set_venue_vibe_profile(VenueVibeProfile(
        venue_id=vid, top_vibes=["animado"], overall_confidence=0.85,
        evidence_photos=[
            EvidencePhoto(
                photo_url="https://lh3.googleusercontent.com/places_CJcategorized_photos_p0=w800",
                photo_type="interior",
            ),
        ],
    ))
    # p0's resolved URL matches the evidence above; p1's does not -- proving a
    # partial match (some photos categorized, some not) is safe, not an error.
    _install_google_transport(context, [
        {"name": "places/CJcategorized/photos/p0", "author": "Ana"},
        {"name": "places/CJcategorized/photos/p1", "author": None},
    ])


@when('the fresh photos for "{vid}" are projected and resolved')
def step_resolve_fresh_photos(context, vid):
    context.resolve_response = context.client.post(f"/internal/venues/{vid}/photos/resolve")


@then("each resolved photo with a known category must carry that category")
def step_check_categories(context):
    assert context.resolve_response.status_code == 200, context.resolve_response.text
    photos = context.resolve_response.json()["venue_photos"]
    assert len(photos) == 2
    matched = next(p for p in photos if p["url"].endswith("p0=w800"))
    assert matched["category"] == "Ambiente", matched
    unmatched = next(p for p in photos if p["url"].endswith("p1=w800"))
    assert unmatched.get("category") is None, unmatched


# ── Scenarios: Instagram validation keeps/deletes on ambiguous/definitive ────
@given('"{vid}" has a cached Instagram handle')
def step_has_cached_instagram_handle(context, vid):
    context.venue_id = vid
    context.instagram_handle = f"handle_{vid}".replace("-", "_")
    context.repository.upsert_venue(_venue(vid))
    context.repository.set_venue_instagram(VenueInstagram(
        venue_id=vid, instagram_handle=context.instagram_handle,
        instagram_url=f"https://instagram.com/{context.instagram_handle}",
        status="found", confidence_score=0.9,
    ))


@given("the Instagram profile check returns a rate-limit response")
def step_ig_check_rate_limited(context):
    context.instagram_http_status = 429


@given("the Instagram profile check returns a definitive not-found")
def step_ig_check_not_found(context):
    context.instagram_http_status = 404


@when("the Instagram validation sweep runs")
def step_run_instagram_validation_sweep(context):
    with respx.mock:
        respx.head(url__regex=r"https://www\.instagram\.com/.*/?$").mock(
            return_value=httpx.Response(context.instagram_http_status)
        )
        context.instagram_removed = asyncio.run(
            context.enrichment_service.validate_cached_instagram_handles()
        )


@then('the handle for "{vid}" must be kept')
def step_handle_kept(context, vid):
    ig = context.repository.get_venue_instagram(vid)
    assert ig is not None and ig.has_instagram(), (
        f"expected {vid}'s Instagram handle to survive an ambiguous check"
    )


@then('the handle for "{vid}" must be soft-deleted')
def step_handle_deleted(context, vid):
    ig = context.repository.get_venue_instagram(vid)
    assert ig is None or not ig.has_instagram(), (
        f"expected {vid}'s Instagram handle to be removed after a definitive 404"
    )


# ── Scenarios: search_place_id no-match vs error split ────────────────────────
@given('"{vid}" has no vibe attributes cached')
def step_no_vibe_attributes_cached(context, vid):
    context.venue_id = vid
    context.repository.upsert_venue(_venue(vid))
    assert context.repository.get_vibe_attributes(vid) is None


@given("the Google place search fails with a rate-limit error")
def step_google_search_rate_limited(context):
    context.google_places_client.search_place_id = AsyncMock(
        side_effect=GooglePlacesSearchError("rate limited (429)")
    )


@given('the Google place search returns no results for "{vid}"')
def step_google_search_no_results(context, vid):
    context.google_places_client.search_place_id = AsyncMock(return_value=None)


@when('the enrichment job processes "{vid}"')
def step_enrichment_job_processes(context, vid):
    context._skipped_error_before = VIBE_ATTRIBUTES_FETCH_RESULTS.labels(
        result="skipped_error"
    )._value.get()
    context.enrich_summary = asyncio.run(
        context.enrichment_service.enrich_all_venues(force_refresh=False)
    )


@then('no empty vibe-attributes marker must be written for "{vid}"')
def step_no_empty_marker(context, vid):
    assert context.repository.get_vibe_attributes(vid) is None, (
        f"a transport error must never write the poison marker for {vid}"
    )


@then("the run must record the venue as skipped due to error")
def step_run_records_skipped_error(context):
    after = VIBE_ATTRIBUTES_FETCH_RESULTS.labels(result="skipped_error")._value.get()
    assert after == context._skipped_error_before + 1, (
        f"expected the skipped_error metric to increment by 1, before="
        f"{context._skipped_error_before} after={after}"
    )


@then('the next enrichment run must process "{vid}" again')
def step_next_run_reprocesses(context, vid):
    # Google now succeeds; a genuinely-retried venue gets enriched this time
    # (proving no poison marker short-circuited it on the prior run).
    context.google_places_client.search_place_id = AsyncMock(
        return_value="places/CJretry"
    )
    context.google_places_client.get_place_details = AsyncMock(return_value=None)
    asyncio.run(context.enrichment_service.enrich_all_venues(force_refresh=False))
    context.google_places_client.search_place_id.assert_awaited()


@then('an empty vibe-attributes marker must be written for "{vid}"')
def step_empty_marker_written(context, vid):
    attrs = context.repository.get_vibe_attributes(vid)
    assert attrs is not None and attrs.google_place_id == "", (
        f"expected the empty no-match marker for {vid}, got {attrs}"
    )


# ── Scenario: the nightly recheck deprecates a permanently closed venue ──────
@given('"{vid}" was fully enriched in a previous run')
def step_fully_enriched_previously(context, vid):
    context.venue_id = vid
    context.repository.upsert_venue(_venue(vid))
    context.repository.set_vibe_attributes(VibeAttributes(
        venue_id=vid, google_place_id="places/CJrecheck", google_primary_type="bar",
    ))


@given('Google now reports business status "{status}" for "{vid}"')
def step_google_reports_status(context, status, vid):
    async def _fake_get_place_details(place_id, fields_mask=None):
        # The recheck must request the cheap, status-only field mask -- never
        # the full VIBE_FIELDS_MASK (which would re-run full enrichment).
        assert fields_mask == "businessStatus", (
            f"recheck must request fields_mask='businessStatus', got {fields_mask!r}"
        )
        return GooglePlacesDetailsResponse(place_id=place_id, business_status=status)

    context.google_places_client.get_place_details = _fake_get_place_details
    # An already-enriched venue's recheck must never re-run place search.
    context.google_places_client.search_place_id = AsyncMock(
        side_effect=AssertionError("search_place_id must not run during a status-only recheck")
    )


@given("the business-status recheck flag is enabled")
def step_recheck_flag_enabled(context):
    _override_setting(context, "business_status_recheck_enabled", True)


@when("the nightly Google enrichment job runs")
def step_nightly_job_runs(context):
    context.enrich_result = asyncio.run(
        context.enrichment_service.enrich_all_venues(force_refresh=False)
    )


@then('"{vid}" must be deprecated through the permanently-closed path')
def step_deprecated_via_permanent(context, vid):
    row = context.rds_store.get_venue(vid)
    assert row is not None
    assert row["lifecycle_status"] == "deprecated", row
    assert row["deprecated_source"] == "google_places", row
    assert row["deprecated_reason"] == "google_places_closed_permanently", row


@then('no full vibe enrichment must be performed for "{vid}"')
def step_no_full_enrichment(context, vid):
    # The originally-cached vibe attributes must be untouched: the recheck's
    # fields_mask assertion above already proves no full Details call was
    # made, and this proves set_vibe_attributes was never re-invoked either.
    attrs = context.repository.get_vibe_attributes(vid)
    assert attrs is not None and attrs.google_primary_type == "bar", attrs


@then('"{vid}" must not appear in the next live refresh selection')
def step_not_in_live_refresh_selection(context, vid):
    ids = context.repository.list_servable_venue_ids()
    assert vid not in ids, f"{vid} still in the serving view after deprecation"


# ── Scenario: an admin trigger cannot double a scheduled paid refresh ────────
@given("the scheduled live forecast refresh is mid-run")
def step_scheduled_live_forecast_mid_run(context):
    from app.services import job_lock

    assert job_lock.try_acquire(job_lock.LIVE_FORECAST), (
        "test setup bug: live_forecast lock was already held"
    )
    # Guarantee release even if a later assertion fails, so this scenario can
    # never leak lock state into another scenario/test in the same process.
    context.add_cleanup(job_lock.release, job_lock.LIVE_FORECAST)


@when("an operator triggers the live forecast job via the admin endpoint")
def step_admin_triggers_live_forecast(context):
    context.trigger_response = context.client.post("/admin/trigger/live_forecast")


@then("the trigger must be refused as already running")
def step_trigger_refused(context):
    assert context.trigger_response.status_code == 200, context.trigger_response.text
    body = context.trigger_response.json()
    assert body["status"] == "already_running", body


@then("no additional BestTime calls must be spent by the trigger")
def step_no_besttime_calls(context):
    assert context.besttime.calls == [], context.besttime.calls


# ── Scenarios: geo-link provenance + month-aware undo ────────────────────────
_GEO_UNDO_NAME = "Bar do Undo"
_GEO_UNDO_ADDRESS = "Rua do Teste, 1 - Recife - PE"


def _counter_key(year_month: str) -> str:
    return VENUE_ADD_COUNTER_KEY_V1.format(year_month=year_month)


def _seed_geo_linked_venue(context, *, geo_linked: bool, recorded_month=None, venue_id="ven_undo"):
    """Seed the RDS system of record with a venue in the post-add state,
    optionally carrying geo-link provenance, plus the address-hash cache
    entry. Mirrors what AddVenueHandler._geo_fallback / the normal create
    path persist."""
    context.repository.upsert_venue(Venue(
        forecast=True, processed=True, venue_id=venue_id,
        venue_name=_GEO_UNDO_NAME, venue_address=_GEO_UNDO_ADDRESS,
        venue_lat=_LAT, venue_lng=_LNG, venue_type="BAR",
        geo_linked=geo_linked,
        geo_linked_year_month=recorded_month if geo_linked else None,
    ))
    context.geo_undo_venue_id = venue_id


@given("a venue was geo-linked last month consuming last month's budget slot")
def step_geo_linked_last_month(context):
    # The harness pins "this month" to 2026-05; last month is 2026-04.
    context.this_month, context.last_month = "2026-05", "2026-04"
    context.fake_redis.set(_counter_key(context.last_month), 1)   # last month's consumed slot
    context.fake_redis.set(_counter_key(context.this_month), 4)   # this month's unrelated adds
    _seed_geo_linked_venue(context, geo_linked=True, recorded_month=context.last_month)


@when("the operator undoes the geo-link this month")
def step_operator_undoes_geo_link(context):
    context.undo_response = context.client.post(
        "/admin/venues/geo-link/undo", json={"venue_id": context.geo_undo_venue_id}
    )


@then("last month's counter must be decremented")
def step_last_month_decremented(context):
    assert context.undo_response.status_code == 200, context.undo_response.text
    assert context.undo_response.json()["status"] == "undone", context.undo_response.json()
    assert int(context.fake_redis.get(_counter_key(context.last_month))) == 0


@then("this month's counter must be unchanged")
def step_this_month_unchanged(context):
    assert int(context.fake_redis.get(_counter_key(context.this_month))) == 4


@given("a venue was created through the normal paid add path within 24 hours")
def step_venue_created_normal_path(context):
    context.this_month = "2026-05"
    context.fake_redis.set(_counter_key(context.this_month), 4)
    # geo_linked=False mirrors a direct POST /forecasts create (never geo-linked).
    _seed_geo_linked_venue(context, geo_linked=False)


@when("the operator requests a geo-link undo for that venue")
def step_operator_requests_undo(context):
    context.undo_response = context.client.post(
        "/admin/venues/geo-link/undo", json={"venue_id": context.geo_undo_venue_id}
    )


@then("the undo must be rejected")
def step_undo_rejected(context):
    assert context.undo_response.status_code == 409, context.undo_response.text
    body = context.undo_response.json()
    assert "not created via geo-link fallback" in body["detail"], body


@then("no budget counter must change")
def step_no_budget_counter_change(context):
    assert int(context.fake_redis.get(_counter_key(context.this_month))) == 4
    # The venue is still active — the undo never soft-deleted it.
    row = context.rds_store.get_venue(context.geo_undo_venue_id)
    assert row["lifecycle_status"] == "active", row


# ── Scenario: concurrent duplicate manual adds spend exactly one create ──────
_CONCURRENT_NAME = "Bar Concorrente"
_CONCURRENT_ADDRESS = "Rua Dupla, 1 - Recife - PE"
_CONCURRENT_VENUE_ID = "ven_concurrent_1"


@given("two identical add requests for the same name and address arrive concurrently")
def step_two_concurrent_adds(context):
    ok = NewVenueResponse.model_validate({
        "status": "OK",
        "venue_info": {
            "venue_id": _CONCURRENT_VENUE_ID,
            "venue_name": _CONCURRENT_NAME,
            "venue_address": _CONCURRENT_ADDRESS,
            "venue_lat": _LAT,
            "venue_lon": _LNG,
        },
        "analysis": [],
    })
    # Insert a real await inside the create so the winner holds the single-flight
    # lock across a yield point; the loser then runs mid-flight, hits the lock,
    # and resolves via the cache the winner publishes — genuinely exercising the
    # lock rather than relying on the winner running to completion first.
    calls = context.besttime.calls

    async def _slow_create(venue_name, venue_address):
        calls.append({"method": "add_venue_to_account",
                      "venue_name": venue_name, "venue_address": venue_address})
        await asyncio.sleep(0.05)
        return ok

    context.besttime.add_venue_to_account = _slow_create
    context.concurrent_request = AddVenueByAddressRequest(
        venue_name=_CONCURRENT_NAME, venue_address=_CONCURRENT_ADDRESS,
        venue_lat=_LAT, venue_lng=_LNG,
    )


@when("both requests are processed")
def step_process_both_concurrent(context):
    handler = context.add_venue_handler

    async def _run_both():
        return await asyncio.gather(
            handler.add(context.concurrent_request),
            handler.add(context.concurrent_request),
        )

    context.concurrent_outcomes = asyncio.run(_run_both())


@then("exactly one budget slot must be reserved")
def step_one_budget_slot(context):
    counter = context.fake_redis.get(_counter_key("2026-05"))
    assert int(counter) == 1, f"expected exactly one reserved slot, counter={counter}"


@then("exactly one paid BestTime create must be issued")
def step_one_create(context):
    creates = [c for c in context.besttime.calls if c.get("method") == "add_venue_to_account"]
    assert len(creates) == 1, f"expected exactly one BestTime create, got {creates}"


@then("both requests must resolve to the same venue")
def step_both_same_venue(context):
    ids = {o.body.get("venue_id") for o in context.concurrent_outcomes}
    assert ids == {_CONCURRENT_VENUE_ID}, (
        f"both adds must resolve to {_CONCURRENT_VENUE_ID}, got {ids}"
    )
    codes = sorted(o.status_code for o in context.concurrent_outcomes)
    # One create (201) + one loser resolving to the same venue (200).
    assert codes == [200, 201], f"expected [200, 201], got {codes}"


# ── Scenario: timeout recovery refuses short-name containment matches ────────
_TIMEOUT_SHORT_NAME = "Vila"  # folds to 4 chars, below MIN_CONTAINMENT_MATCH_LEN
_TIMEOUT_ADDRESS = "Rua das Flores 123, Recife - PE"
_TIMEOUT_UNRELATED_ID = "ven_unrelated_containment"


@given("a paid create timed out for a venue whose folded name has 4 characters")
def step_create_timed_out_short_name(context):
    context.besttime.programmed_add_venue = httpx.TimeoutException("simulated timeout")
    context.add_venue_handler.timeout_recovery_grace_seconds = 0.0
    context.timeout_request = AddVenueByAddressRequest(
        venue_name=_TIMEOUT_SHORT_NAME, venue_address=_TIMEOUT_ADDRESS,
        venue_lat=_LAT, venue_lng=_LNG,
    )


@given(
    "the account inventory contains an unrelated venue whose folded name "
    "contains those 4 characters"
)
def step_inventory_has_unrelated_containment(context):
    context.besttime.programmed_inventory_pages = [[
        {
            "venue_id": _TIMEOUT_UNRELATED_ID,
            "venue_name": "Vila Madalena Bar",  # folded contains "vila"
            "venue_address": "Av. Outra, 500 - Sao Paulo - SP",
            "venue_lat": -23.5, "venue_lng": -46.6, "venue_forecasted": True,
        }
    ]]


@when("timeout recovery scans the account inventory")
def step_timeout_recovery_scans(context):
    context.timeout_outcome = asyncio.run(
        context.add_venue_handler.add(context.timeout_request)
    )


@then("the unrelated venue must not be linked")
def step_unrelated_not_linked(context):
    outcome = context.timeout_outcome
    # A 4-char folded name below the containment guard finds no confirmed venue,
    # so the timed-out create is reported unconfirmed (502) — never a 201 with
    # the wrong venue_id.
    assert outcome.status_code == 502, (outcome.status_code, outcome.body)
    assert outcome.body.get("venue_id") != _TIMEOUT_UNRELATED_ID
    assert context.add_venue_handler.venue_dao.get_venue(_TIMEOUT_UNRELATED_ID) is None


@then("the address cache must not be poisoned with the wrong venue id")
def step_address_cache_not_poisoned(context):
    cached = context.add_venue_handler._lookup_cached_venue_id(
        _TIMEOUT_SHORT_NAME, _TIMEOUT_ADDRESS
    )
    assert cached != _TIMEOUT_UNRELATED_ID, (
        f"address cache poisoned with the wrong venue id: {cached}"
    )
