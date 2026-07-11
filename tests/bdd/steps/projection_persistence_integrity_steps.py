"""Behave steps for tests/bdd/persistence/projection-persistence-integrity.feature.

Covers: per-venue/per-stage exception isolation in the projector (a poisoned
enrichment payload never aborts the run and the reconcile/removal pass still
executes), RDS-deletion propagation to Redis (live forecast, enrichment,
weekly day keys), full per-venue key-family removal on venue deletion, the
idempotent hot-like write-through, and the empty-pseudonymization-key startup
guard.

Reuses the harness wired by environment.py (context.rds_store = fake RDS
truth, context.redis_only_dao = Redis-only projection reader/writer,
context.redis_projection_service = the projector, context.engagement_service).
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo, WeekRawDay
from app.models.instagram import VenueInstagram, VenueInstagramPosts
from app.models.menu import VenueMenuData, VenueMenuPhotos
from app.models.opening_hours import OpeningHours
from app.models.vibe_attributes import VibeAttributes
from app.models.vibe_profile import VenueVibeProfile
from app.models.venue_review import VenueReviews

_LAT, _LNG = -8.05, -34.88


def _venue(vid: str, name: str = "Bar X") -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=name,
        venue_address=f"{vid} address", venue_lat=_LAT, venue_lng=_LNG,
        venue_type="BAR",
    )


def _live_payload(vid: str) -> dict:
    return LiveForecastResponse(
        status="OK", venue_info=VenueInfo(venue_id=vid),
        analysis=Analysis(venue_live_busyness=42, venue_live_busyness_available=True),
    ).model_dump(by_alias=True)


# ── Background ────────────────────────────────────────────────────────────────
@given('servable venues "{vid_a}" and "{vid_b}" exist in RDS')
def step_two_servable_venues(context, vid_a, vid_b):
    context.rds_store.upsert_venue(_venue(vid_a))
    context.rds_store.upsert_venue(_venue(vid_b))
    context.vid_a, context.vid_b = vid_a, vid_b


# ── Scenario: corrupt enrichment payload isolates to its venue ────────────────
@given('"{vid}" has a vibe-profile enrichment row whose payload fails model validation')
def step_corrupt_vibe_profile(context, vid):
    # Missing every VenueVibeProfile field but venue_id would still validate
    # (all other fields default) -- use a wrong TYPE for top_vibes so
    # model_validate genuinely raises, the poisoned-row fixture for this
    # scenario.
    context.rds_store.upsert_enrichment(
        "venues.vibe_profile", vid,
        {"venue_id": vid, "top_vibes": "not-a-list-of-strings"},
        history=False,
    )
    # Canary: a venue deprecated in RDS but stale-present in Redis. Seeded so
    # "the reconcile removal pass must still execute" has something concrete
    # to check -- proof the run reached the removal pass despite venue-a's
    # mid-loop exception.
    canary = _venue("canary-deprecated")
    context.rds_store.upsert_venue(canary)
    context.redis_only_dao.upsert_venue(canary)
    context.rds_store.soft_delete_venue("canary-deprecated", "ineligible_google_type", "eligibility_filter")


@given('"{vid}" has valid enrichment rows')
def step_valid_enrichment_rows(context, vid):
    context.rds_store.upsert_enrichment(
        "google_places.vibe_attributes", vid,
        {"venue_id": vid, "google_primary_type": "bar"}, history=False,
    )
    context.rds_store.upsert_enrichment(
        "venues.vibe_profile", vid,
        {"venue_id": vid, "top_vibes": ["animado"], "overall_confidence": 0.8},
        history=False,
    )


@when("a projection run executes")
def step_projection_run(context):
    context.projection_summary = context.redis_projection_service.rebuild_redis_from_rds()


@then('"{vid}" must be fully projected to Redis')
def step_venue_fully_projected(context, vid):
    assert context.redis_only_dao.get_venue(vid) is not None, f"{vid} venue record missing from Redis"
    assert context.redis_only_dao.get_vibe_attributes(vid) is not None, f"{vid} vibe attributes missing"
    assert context.redis_only_dao.get_venue_vibe_profile(vid) is not None, f"{vid} vibe profile missing"


@then('the run summary must report at least one error naming "{vid}"')
def step_summary_reports_error(context, vid):
    summary = context.projection_summary
    assert summary["errors"] >= 1, f"expected at least one error, got summary={summary}"
    assert vid in summary.get("error_venues", []), (
        f"expected {vid} to be named in summary['error_venues'], got {summary.get('error_venues')}"
    )


@then("the reconcile removal pass must still execute in the same run")
def step_reconcile_still_ran(context):
    assert context.redis_only_dao.get_venue("canary-deprecated") is None, (
        "the deprecated canary venue was not reconciled out of Redis -- the "
        "removal pass did not run in the same cycle as venue-a's exception"
    )


# ── Scenario: RDS-deleted live forecast disappears on the next cycle ─────────
@given('"{vid}" has a live forecast projected in Redis')
def step_live_forecast_projected(context, vid):
    context.rds_store.upsert_live_forecast(vid, _live_payload(vid))
    context.redis_projection_service.rebuild_redis_from_rds()
    assert context.redis_only_dao.get_live_forecast(vid) is not None  # sanity: materialized


@given('the live forecast row for "{vid}" is deleted in RDS')
def step_live_forecast_deleted_in_rds(context, vid):
    context.rds_store.delete_live_forecast(vid)


@then('the Redis live forecast key for "{vid}" must not exist')
def step_redis_live_forecast_absent(context, vid):
    assert context.redis_only_dao.get_live_forecast(vid) is None


@then('"{vid}" must remain served in the Redis geo index')
def step_still_served(context, vid):
    assert context.redis_only_dao.get_venue(vid) is not None


# ── Scenario: soft-deleted enrichment row deletes its Redis key ──────────────
@given('"{vid}" has an Instagram enrichment projected in Redis')
def step_instagram_projected(context, vid):
    context.rds_store.upsert_enrichment(
        "instagram.handle", vid,
        {"venue_id": vid, "instagram_handle": "handle1", "instagram_url": "https://ig/handle1",
         "status": "found", "confidence_score": 0.9},
        history=False,
    )
    # A second enrichment type, seeded so "the other enrichment keys ... must
    # remain present" has something concrete to assert against.
    context.rds_store.upsert_enrichment(
        "google_places.vibe_attributes", vid,
        {"venue_id": vid, "google_primary_type": "bar"}, history=False,
    )
    context.redis_projection_service.rebuild_redis_from_rds()
    assert context.redis_only_dao.get_venue_instagram(vid) is not None
    assert context.redis_only_dao.get_vibe_attributes(vid) is not None


@given('the Instagram enrichment row for "{vid}" is soft-deleted in RDS')
def step_instagram_soft_deleted(context, vid):
    context.rds_store.soft_delete_enrichment("instagram.handle", vid, history=False)


@then('the Redis Instagram key for "{vid}" must not exist')
def step_redis_instagram_absent(context, vid):
    assert context.redis_only_dao.get_venue_instagram(vid) is None


@then('the other enrichment keys for "{vid}" must remain present')
def step_other_enrichment_present(context, vid):
    assert context.redis_only_dao.get_vibe_attributes(vid) is not None


# ── Scenario: soft-deleted weekly day removed without touching other days ────
@given('"{vid}" has weekly forecasts projected for day_int {d1:d} and day_int {d2:d}')
def step_weekly_two_days(context, vid, d1, d2):
    for day in (d1, d2):
        context.rds_store.upsert_enrichment(
            "besttime.weekly_forecast", f"{vid}#{day}",
            {"day_int": day, "day_raw": [1] * 24}, history=False,
        )
    context.redis_projection_service.rebuild_redis_from_rds()
    for day in (d1, d2):
        assert context.redis_only_dao.get_week_raw_forecast(vid, day) is not None


@given('the weekly forecast row for "{vid}" day_int {day:d} is soft-deleted in RDS')
def step_weekly_day_soft_deleted(context, vid, day):
    context.rds_store.soft_delete_enrichment("besttime.weekly_forecast", f"{vid}#{day}", history=False)


@then('the Redis weekly forecast key for "{vid}" day_int {day:d} must not exist')
def step_weekly_key_absent(context, vid, day):
    assert context.redis_only_dao.get_week_raw_forecast(vid, day) is None


@then('the Redis weekly forecast key for "{vid}" day_int {day:d} must remain present')
def step_weekly_key_present(context, vid, day):
    assert context.redis_only_dao.get_week_raw_forecast(vid, day) is not None


# ── Scenario: venue deletion removes every per-venue key family ──────────────
@given(
    '"{vid}" has projected keys for its venue record, live forecast, weekly '
    "forecasts, vibe attributes, photos, fresh photos, IG posts, reviews, "
    "opening hours, and vibe profile"
)
def step_all_families_projected(context, vid):
    dao = context.redis_only_dao
    dao.upsert_venue(_venue(vid))
    dao.set_live_forecast(LiveForecastResponse(
        status="OK", venue_info=VenueInfo(venue_id=vid),
        analysis=Analysis(venue_live_busyness=10, venue_live_busyness_available=True),
    ))
    dao.set_week_raw_forecast(vid, WeekRawDay(day_int=0, day_raw=[1] * 24))
    dao.set_vibe_attributes(VibeAttributes(venue_id=vid, google_primary_type="bar"))
    dao.set_venue_photos(vid, [{"url": "https://p/1.jpg", "author_name": "A"}])
    dao.set_venue_photos_fresh(vid, [{"url": "https://fresh/1.jpg", "author_name": "A"}])
    dao.set_venue_ig_posts(VenueInstagramPosts(venue_id=vid, instagram_handle="h1", posts=[]))
    dao.set_venue_reviews(VenueReviews(venue_id=vid, reviews=[]))
    dao.set_opening_hours(OpeningHours(venue_id=vid, weekday_descriptions=["Seg: 18-02"]))
    dao.set_venue_vibe_profile(
        VenueVibeProfile(venue_id=vid, top_vibes=["animado"], overall_confidence=0.9)
    )
    dao.set_venue_menu_photos(VenueMenuPhotos(venue_id=vid, photos=[]))
    dao.set_venue_menu_data(VenueMenuData(venue_id=vid, sections=[]))
    context.vid = vid


@when('"{vid}" is removed from serving')
def step_venue_removed(context, vid):
    context.delete_result = context.redis_only_dao.delete_venue(vid)


@then('no Redis key for "{vid}" must remain in any per-venue key family')
def step_no_keys_remain(context, vid):
    dao = context.redis_only_dao
    assert dao.get_venue(vid) is None
    assert dao.get_live_forecast(vid) is None
    assert dao.get_week_raw_forecast(vid, 0) is None
    assert dao.get_vibe_attributes(vid) is None
    assert dao.get_venue_photos(vid) is None
    assert dao.get_venue_photos_fresh(vid) is None
    assert dao.get_venue_ig_posts(vid) is None
    assert dao.get_venue_reviews(vid) is None
    assert dao.get_opening_hours(vid) is None
    assert dao.get_venue_vibe_profile(vid) is None
    assert dao.get_venue_menu_photos(vid) is None
    assert dao.get_venue_menu_data(vid) is None


# ── Scenario: retried hot-like write persists exactly one event row ──────────
@given('a hot-like write for user "{uid}" and "{vid}" has committed its RDS event row')
def step_hot_like_committed(context, uid, vid):
    context.rds_store.upsert_venue(_venue(vid))
    context.uid, context.vid = uid, vid
    context.engagement_service.add_hot_like(uid, vid)


@given("the same write failed after the RDS commit and was retried per the router contract")
def step_retry_after_redis_failure(context):
    # engagement_router.py mandates the client retry on a 5xx (e.g. the Redis
    # leg failing after the RDS commit already succeeded); vibes_bot's retry
    # re-sends the exact same request -- exercised by the next When step.
    pass


@when("the retried hot-like write completes")
def step_retry_completes(context):
    context.retry_error = None
    try:
        context.engagement_service.add_hot_like(context.uid, context.vid)
    except Exception as e:  # noqa: BLE001 -- captured for the Then assertion
        context.retry_error = e


@then('exactly one hot-like event row must exist for "{uid}" and "{vid}" in the current business period')
def step_exactly_one_hot_like_row(context, uid, vid):
    assert context.rds_store.hot_like_event_count(vid) == 1, (
        f"expected exactly one hot-like event row for {vid}, "
        f"got {context.rds_store.hot_like_event_count(vid)}"
    )


@then("the retried request must succeed")
def step_retry_succeeds(context):
    assert context.retry_error is None, f"retried hot-like write raised: {context.retry_error}"


# ── Scenario: engagement writes refuse an empty pseudonymization key ─────────
@given("the engagement pseudonymization key is configured empty")
def step_key_configured_empty(context):
    context.pseudonymization_key = ""


@given("engagement persistence is enabled")
def step_engagement_enabled(context):
    pass  # always enabled in this repo; there is no feature flag to gate it


@when("the service starts")
def step_service_starts(context):
    from app.services.engagement_service import EngagementService

    context.startup_error = None
    try:
        EngagementService(
            redis_client=context.fake_redis,
            rds_store=context.rds_store,
            pseudonymization_key=context.pseudonymization_key,
        )
    except Exception as e:  # noqa: BLE001 -- captured for the Then assertion
        context.startup_error = e


@then("startup must fail with a clear error naming the pseudonymization key setting")
def step_startup_fails_clearly(context):
    assert context.startup_error is not None, (
        "expected startup to fail on an empty pseudonymization key, it did not raise"
    )
    assert "ENGAGEMENT_PSEUDONYMIZATION_KEY" in str(context.startup_error), (
        f"error must name the setting, got: {context.startup_error!r}"
    )
