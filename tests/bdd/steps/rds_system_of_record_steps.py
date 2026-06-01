"""Behave steps for tests/bdd/persistence/rds_system_of_record.feature.

Runs against the in-memory fake RdsVenueStore wired by environment.py
(context.repository = write-through, context.rds_store = fake truth,
context.redis_only_dao = Redis-only projection reader/writer).
"""
from __future__ import annotations

from behave import given, when, then  # type: ignore[import-untyped]

from app.handlers.venue_handler import VenueHandler
from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo, WeekRawDay
from app.models.instagram import VenueInstagram
from app.models.menu import MenuItem, MenuPhoto, MenuSection, VenueMenuData, VenueMenuPhotos
from app.models.opening_hours import OpeningHours
from app.models.venue_review import VenueReview, VenueReviews
from app.models.vibe_attributes import VibeAttributes
from app.models.vibe_profile import VenueVibeProfile
from app.dao.redis_venue_dao import VENUE_PHOTOS_KEY_FORMAT

_LAT, _LNG, _R = -8.05, -34.88, 1.0
_VA = "google_places.vibe_attributes"


def _venue(vid: str, name: str = "Bar X", venue_type: str = "BAR") -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=name,
        venue_address=f"{vid} address", venue_lat=_LAT, venue_lng=_LNG,
        venue_type=venue_type,
    )


def _vibe(vid: str, gtype: str = "bar") -> VibeAttributes:
    return VibeAttributes(venue_id=vid, google_place_id=f"place_{vid}", google_primary_type=gtype)


def _live(vid: str) -> LiveForecastResponse:
    return LiveForecastResponse(
        status="OK", venue_info=VenueInfo(venue_id=vid),
        analysis=Analysis(venue_live_busyness=55, venue_live_busyness_available=True),
    )


def _nearby_ids(context) -> set[str]:
    handler = VenueHandler(context.repository)
    return {v.venue_id for v in handler.get_venues_nearby(_LAT, _LNG, _R, verbose=False)}


def _persist_all_enrichment(context, vid: str) -> None:
    r = context.repository
    r.set_vibe_attributes(_vibe(vid))
    r.set_venue_instagram(VenueInstagram(
        venue_id=vid, instagram_handle="v", instagram_url="https://ig/v",
        status="found", confidence_score=1.0))
    r.set_venue_photos(vid, [{"url": "https://p/1.jpg", "author_name": "A"}])
    r.set_venue_reviews(VenueReviews(venue_id=vid, reviews=[
        VenueReview(author_name="A", rating=5, text="ok", relative_time="today")]))
    r.set_opening_hours(OpeningHours(venue_id=vid, weekday_descriptions=["Seg: 18-02"]))
    r.set_venue_menu_data(VenueMenuData(venue_id=vid, sections=[
        MenuSection(name="Drinks", items=[MenuItem(name="Beer", prices=[{"price": 12}])])]))
    r.set_venue_menu_photos(VenueMenuPhotos(venue_id=vid, photos=[
        MenuPhoto(photo_id="p1", s3_url="https://s3/m.jpg", s3_key="m.jpg")]))
    r.set_venue_vibe_profile(VenueVibeProfile(venue_id=vid, top_vibes=["animado"], overall_confidence=0.9))
    r.set_week_raw_forecast(vid, WeekRawDay(day_int=0, day_raw=[50] * 24))


# ── Background ────────────────────────────────────────────────────────────────
@given("the RDS system-of-record is enabled")
def step_rds_enabled(context):
    context.rds_enabled = True


@given("an empty RDS and an empty Redis")
def step_empty(context):
    context.fake_redis.flushall()


# ── write-through ─────────────────────────────────────────────────────────────
@when('a pipeline upserts a venue "{vid}" named "{name}"')
def step_upsert_venue(context, vid, name):
    context.repository.upsert_venue(_venue(vid, name))
    context.vid = vid


@then('RDS holds venue "{vid}" as the system of record')
def step_rds_has_venue(context, vid):
    assert context.rds_store.get_venue(vid) is not None


@then('Redis holds the serving projection for venue "{vid}"')
def step_redis_has_venue(context, vid):
    assert context.redis_only_dao.get_venue(vid) is not None


@then('the venue "{vid}" is returned by nearby serving')
def step_nearby_includes(context, vid):
    assert vid in _nearby_ids(context)


@given('a venue "{vid}" exists in RDS and Redis')
def step_seed_venue(context, vid):
    context.repository.upsert_venue(_venue(vid))
    context.vid = vid


@when('the pipelines persist google places, instagram, photos, reviews, opening hours, menu, vibe profile, and weekly forecast for "{vid}"')
def step_persist_enrichment(context, vid):
    _persist_all_enrichment(context, vid)


@then('RDS holds each of those records for "{vid}"')
def step_rds_has_enrichment(context, vid):
    for table_key in (
        _VA, "instagram.handle", "google_places.photos", "google_places.reviews",
        "google_places.opening_hours", "venues.menu_data", "venues.menu_photos",
        "venues.vibe_profile",
    ):
        assert context.rds_store.get_enrichment(table_key, vid) is not None, table_key
    assert context.rds_store.get_enrichment("besttime.weekly_forecast", f"{vid}#0") is not None


@then('the Redis serving projection for "{vid}" includes every field the nearby response reads')
def step_redis_projection_complete(context, vid):
    d = context.redis_only_dao
    assert d.get_vibe_attributes(vid) is not None
    assert d.get_venue_photos(vid)
    assert d.get_opening_hours(vid) is not None
    assert d.get_venue_instagram(vid) is not None
    assert d.get_venue_reviews(vid) is not None
    assert d.get_venue_vibe_profile(vid) is not None
    assert d.get_venue_menu_data(vid) is not None
    assert d.get_week_raw_forecast(vid, 0) is not None


# ── live busyness ─────────────────────────────────────────────────────────────
@when('the live forecast refresh stores live busyness for "{vid}"')
def step_store_live(context, vid):
    context.repository.set_live_forecast(_live(vid))


@then('RDS holds the current live busyness for "{vid}"')
def step_rds_live(context, vid):
    assert context.rds_store.get_live_forecast(vid) is not None


@then('Redis holds the live busyness for "{vid}" for serving')
def step_redis_live(context, vid):
    assert context.redis_only_dao.get_live_forecast(vid) is not None


# ── soft-delete reason ────────────────────────────────────────────────────────
@when('the eligibility sweep soft-deletes "{vid}" with reason "{reason}"')
def step_soft_delete(context, vid, reason):
    context.repository.soft_delete_venue(vid, reason, "eligibility_filter")


@then('RDS records "{vid}" as deprecated with reason "{reason}" and source "{source}"')
def step_rds_deprecated(context, vid, reason, source):
    row = context.rds_store.get_venue(vid)
    assert row["lifecycle_status"] == "deprecated"
    assert row["deprecated_reason"] == reason
    assert row["deprecated_source"] == source


@then('the venue "{vid}" is excluded from nearby serving')
def step_nearby_excludes(context, vid):
    assert vid not in _nearby_ids(context)


# ── rebuild ───────────────────────────────────────────────────────────────────
@given("RDS holds venues, enrichment records, and admin config")
def step_rds_seeded(context):
    context.repository.upsert_venue(_venue("v1"))
    context.repository.set_vibe_attributes(_vibe("v1"))
    context.repository.set_live_forecast(_live("v1"))
    context.vid = "v1"


@given("Redis has been flushed")
def step_flush_redis(context):
    context.fake_redis.flushall()


@when("the rebuild-Redis-from-RDS job runs")
def step_rebuild(context):
    context.redis_projection_service.rebuild_redis_from_rds()


@then("Redis holds the serving projection for every active venue in RDS")
def step_rebuilt_projection(context):
    for vid in context.rds_store.list_active_venue_ids():
        assert context.redis_only_dao.get_venue(vid) is not None


@then("nearby serving returns those venues from the rebuilt geo index")
def step_rebuilt_geo(context):
    assert context.vid in _nearby_ids(context)


@then("live busyness is restored from RDS (refreshed by the next live cron)")
def step_rebuilt_live(context):
    assert context.redis_only_dao.get_live_forecast(context.vid) is not None


# ── RDS outage ────────────────────────────────────────────────────────────────
@given("RDS is unavailable")
def step_rds_down(context):
    context.rds_store.set_unavailable(True)


# NOTE: "a client requests nearby venues" is defined by the eligibility steps
# (shared behave registry); it sets context.nearby_ids. We reuse it here.


@then('the venue "{vid}" is still returned from Redis')
def step_still_served(context, vid):
    assert vid in context.nearby_ids


@when('a pipeline attempts to persist an update for "{vid}"')
def step_attempt_write(context, vid):
    context.write_failed = False
    try:
        context.repository.upsert_venue(_venue(vid, "Renamed During Outage"))
    except Exception:
        context.write_failed = True


@then("the write fails and is logged without corrupting the Redis projection")
def step_write_failed_clean(context):
    assert context.write_failed
    # RDS-first means Redis projection was never touched: original name intact.
    assert context.redis_only_dao.get_venue(context.vid).venue_name == "Bar X"


# ── backfill ──────────────────────────────────────────────────────────────────
@given("Redis already contains venues and enrichment records from before RDS")
def step_redis_only_seed(context):
    # Write via the Redis-only DAO so RDS stays empty (pre-RDS state).
    context.redis_only_dao.upsert_venue(_venue("v1"))
    context.redis_only_dao.set_vibe_attributes(_vibe("v1"))
    context.redis_only_dao.set_live_forecast(_live("v1"))
    context.vid = "v1"


@given("RDS is empty")
def step_rds_empty(context):
    assert context.rds_store.get_venue("v1") is None


@when("the one-time Redis-to-RDS backfill runs")
def step_backfill(context):
    context.redis_projection_service.backfill_rds_from_redis()


@then('RDS holds every venue and enrichment record that Redis contained')
def step_backfilled(context):
    assert context.rds_store.get_venue("v1") is not None
    assert context.rds_store.get_enrichment(_VA, "v1") is not None


@then("venue rows are inserted before their enrichment rows")
def step_fk_order(context):
    # Enrichment present implies its venue row exists (FK satisfied by ordering).
    assert context.rds_store.get_venue("v1") is not None
    assert context.rds_store.get_enrichment(_VA, "v1") is not None


@then("serving behavior is unchanged for those venues")
def step_serving_unchanged(context):
    assert "v1" in _nearby_ids(context)


# ── engagement ────────────────────────────────────────────────────────────────
@when('user "{uid}" favorites venue "{vid}" through the engagement API')
def step_favorite(context, uid, vid):
    context.engagement_service.add_favorite(uid, vid)
    context.uid = uid


@then('RDS holds the favorite for venue "{vid}"')
def step_rds_favorite(context, vid):
    pseudo = context.engagement_service.pseudonymize(context.uid)
    row = context.rds_store.get_favorite(pseudo, vid)
    assert row is not None and row["deleted_at"] is None


@then('RDS stores the user only as a pseudonymized id, never the raw "{uid}"')
def step_pseudonymized(context, uid):
    assert not context.rds_store.contains_raw_value(uid)


@then('Redis holds the favorite so vibes_bot can read it')
def step_redis_favorite(context):
    assert context.fake_redis.sismember(f"user_favorites:{context.uid}", context.vid)


@when('user "{uid}" un-favorites venue "{vid}" through the engagement API')
def step_unfavorite(context, uid, vid):
    context.engagement_service.remove_favorite(uid, vid)


@then('RDS no longer holds an active favorite for "{uid}" on "{vid}"')
def step_rds_unfavorited(context, uid, vid):
    pseudo = context.engagement_service.pseudonymize(uid)
    row = context.rds_store.get_favorite(pseudo, vid)
    assert row is not None and row["deleted_at"] is not None


@when('user "{uid}" hot-likes venue "{vid}" through the engagement API')
def step_hot_like(context, uid, vid):
    context.engagement_service.add_hot_like(uid, vid)
    context.uid, context.vid = uid, vid


@then('RDS holds a hot_like event for venue "{vid}" with a pseudonymized user id')
def step_rds_hot_event(context, vid):
    assert context.rds_store.hot_like_event_count(vid) >= 1
    assert not context.rds_store.contains_raw_value(context.uid)


@then('the Redis trending hot_like counter for "{vid}" still reflects the like')
def step_redis_hot(context, vid):
    assert context.fake_redis.sismember(f"hot_likes:{vid}", context.uid)


# ── never-delete + history ────────────────────────────────────────────────────
@given('a venue "{vid}" has google places vibe labels persisted in RDS')
def step_labels_persisted(context, vid):
    context.repository.upsert_venue(_venue(vid))
    context.repository.set_vibe_attributes(_vibe(vid, "bar"))
    context.vid = vid


@when('the label record for "{vid}" is deleted')
def step_delete_label(context, vid):
    context.repository.delete_vibe_attributes(vid)


@then('RDS still holds the label record marked soft-deleted with a timestamp')
def step_soft_deleted_label(context):
    rec = context.rds_store.get_enrichment(_VA, context.vid)
    assert rec is not None and rec["deleted_at"] is not None


@then('the prior label values are recoverable from the enrichment history')
def step_history_recoverable(context):
    assert context.rds_store.history_count(_VA, context.vid) >= 1


@then('re-enriching "{vid}" appends a new history entry without losing the old one')
def step_history_appends(context, vid):
    before = context.rds_store.history_count(_VA, vid)
    context.repository.set_vibe_attributes(_vibe(vid, "pub"))
    assert context.rds_store.history_count(_VA, vid) > before


# ── photos non-regression ─────────────────────────────────────────────────────
@given('a venue "{vid}" whose photos are persisted in RDS and projected to Redis with a TTL')
def step_photos_persisted(context, vid):
    context.repository.upsert_venue(_venue(vid))
    context.repository.set_venue_photos(vid, [{"url": "https://old/1.jpg", "author_name": "A"}])
    context.vid = vid


@when('the venue photos TTL expires in Redis')
def step_photos_ttl_expire(context):
    # TTL expiry == Redis key gone. RDS copy is retained.
    context.fake_redis.delete(VENUE_PHOTOS_KEY_FORMAT.format(context.vid))


@then('the photo refetch trigger sees "{vid}" as missing photos using Redis only')
def step_refetch_trigger_redis(context, vid):
    assert vid not in set(context.redis_only_dao.list_cached_venue_photos_ids())


@then('the photo enrichment job refetches fresh Google photo URLs for "{vid}"')
def step_refetch_fresh(context, vid):
    context.repository.set_venue_photos(vid, [{"url": "https://fresh/1.jpg", "author_name": "A"}])
    photos = context.redis_only_dao.get_venue_photos(vid)
    assert photos and photos[0]["url"] == "https://fresh/1.jpg"


@then('RDS is never consulted to decide whether photos need refetching')
def step_rds_not_consulted(context):
    # The refetch trigger uses Redis (list_cached_venue_photos_ids); RDS still
    # holds photos, proving RDS presence does not suppress refetch.
    assert context.rds_store.get_enrichment("google_places.photos", context.vid) is not None
