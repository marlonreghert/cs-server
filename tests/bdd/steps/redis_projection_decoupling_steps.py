"""Behave steps for tests/bdd/persistence/redis_projection_decoupling.feature.

PASS 1 only: the scheduled projector running alongside the existing write-through,
covering the two correctness fixes B1 (remove venues deprecated in RDS) and B2
(project photos with the remaining TTL / drop aged photos), plus the engagement
carve-out guards (engagement is immediate, never via the projector).

Reuses the harness wired by environment.py (context.repository = write-through,
context.rds_store = fake truth, context.redis_only_dao = Redis-only projection
reader/writer, context.redis_projection_service = the projector). Step phrasings
already defined in rds_system_of_record_steps.py are reused via behave's global
registry; only new phrasings are defined here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from behave import given, when, then  # type: ignore[import-untyped]

from app.config import settings
from app.dao.redis_venue_dao import VENUE_PHOTOS_KEY_FORMAT
from app.handlers.venue_handler import VenueHandler
from app.models import Venue

_LAT, _LNG, _R = -8.05, -34.88, 1.0
_PHOTOS_TABLE = "google_places.photos"


def _venue(vid: str, name: str = "Bar X") -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=name,
        venue_address=f"{vid} address", venue_lat=_LAT, venue_lng=_LNG,
        venue_type="BAR",
    )


def _nearby_ids(context) -> set[str]:
    handler = VenueHandler(context.redis_only_dao)
    return {v.venue_id for v in handler.get_venues_nearby(_LAT, _LNG, _R, verbose=False)}


def _run_projector(context) -> None:
    context.redis_projection_service.rebuild_redis_from_rds()


def _full_photo_ttl_seconds() -> int:
    return settings.photo_cache_ttl_days * 24 * 3600


def _backdate_photo_updated_at(context, vid: str, days: float) -> None:
    """Age the RDS photo row by rewriting its updated_at to `days` ago.

    Uses a tz-aware datetime (the type the real Postgres SELECT yields) so the
    projector's remaining-TTL math is exercised against the production type, not
    only the fake's ISO-string default.
    """
    row = context.rds_store.enrichment[_PHOTOS_TABLE][vid]
    row["updated_at"] = datetime.now(timezone.utc) - timedelta(days=days)


# ── Background ────────────────────────────────────────────────────────────────
@given("the Redis projector is wired")
def step_projector_wired(context):
    assert context.redis_projection_service is not None


# ── B2: photo remaining-TTL / drop aged ───────────────────────────────────────
@given('a venue "{vid}" whose photos were written to RDS some time ago')
def step_photos_aged_in_rds(context, vid):
    context.rds_store.upsert_venue(_venue(vid))
    context.rds_store.upsert_enrichment(
        _PHOTOS_TABLE, vid, {"photos": [{"url": "https://old/1.jpg", "author_name": "A"}]},
        history=False,
    )
    _backdate_photo_updated_at(context, vid, days=1)  # 1d old, within the 5d TTL
    context.vid = vid


@when("the Redis projector runs")
def step_projector_runs(context):
    _run_projector(context)


@then("the projected photo key carries the remaining TTL, not a fresh full TTL")
def step_photo_remaining_ttl(context):
    full = _full_photo_ttl_seconds()
    ttl = context.fake_redis.ttl(VENUE_PHOTOS_KEY_FORMAT.format(context.vid))
    assert ttl and ttl > 0, f"expected a positive TTL on the projected photo key, got {ttl}"
    # 1-day-old photo against a 5-day TTL must read ~4 days, clearly counted down
    # from the full TTL (the as-built projector re-stamps the full TTL → fails).
    assert ttl <= full - 12 * 3600, (
        f"photo TTL {ttl}s was not counted down from the full TTL {full}s "
        f"(projector re-stamped a fresh full TTL)"
    )


@when("the venue photos age past their TTL in RDS")
def step_photos_age_past_ttl(context):
    _backdate_photo_updated_at(context, context.vid, days=settings.photo_cache_ttl_days + 1)


@then("the projector projects the aged photos as absent from serving")
def step_aged_photos_absent(context):
    assert context.redis_only_dao.get_venue_photos(context.vid) is None, (
        "photos aged past their TTL must be projected as absent so stale Google "
        "URLs drop from serving and a refetch is triggered"
    )


# ── shared seeding for engagement + B1 scenarios ──────────────────────────────
@given('a venue "{vid}" exists in RDS and is projected to Redis')
@given('a venue "{vid}" is active in RDS and projected to Redis')
def step_active_venue_seeded(context, vid):
    context.repository.upsert_venue(_venue(vid))  # write-through: RDS + Redis
    context.vid = vid


# ── engagement carve-out: immediate, never via the projector ──────────────────
@then("RDS records the hot-like event first")
def step_rds_hotlike_first(context):
    assert context.rds_store.hot_like_event_count(context.vid) >= 1


@then("Redis reflects the hot-like immediately in the same request, without a projector run")
def step_redis_hotlike_immediate(context):
    # No projector run between the API call and this assertion.
    assert context.fake_redis.sismember(f"hot_likes:v1:{context.vid}", context.uid)


@then("RDS holds the favorite as the system of record")
def step_rds_favorite_sor(context):
    pseudo = context.engagement_service.pseudonymize(context.uid)
    row = context.rds_store.get_favorite(pseudo, context.vid)
    assert row is not None and row["deleted_at"] is None


@then("Redis holds the favorite immediately for the user's next read without a projector run")
def step_redis_favorite_immediate(context):
    assert context.fake_redis.sismember(f"user_favorites:{context.uid}", context.vid)


# ── Pass 2a: pipelines read data inputs from RDS ──────────────────────────────
@given('the photo pipeline has written photos for "{vid}" to RDS only')
def step_photos_rds_only(context, vid):
    # Write to RDS only (NOT via the write-through repository) so Redis has no
    # photo cache for v1 — proving the later stage's read comes from RDS.
    context.rds_store.upsert_venue(_venue(vid))  # FK parent
    context.rds_store.upsert_enrichment(
        _PHOTOS_TABLE, vid, {"photos": [{"url": "https://rds/1.jpg", "author_name": "A"}]},
        history=False,
    )
    context.fake_redis.delete(VENUE_PHOTOS_KEY_FORMAT.format(vid))
    context.vid = vid


@given("the projector has not yet run")
def step_projector_not_run(context):
    pass  # no-op: the scenario simply never runs the projector


@when('the vibe classifier reads the photos for "{vid}"')
def step_classifier_reads_photos(context, vid):
    # The pipeline repository, with RDS reads enabled, must read photo DATA from
    # RDS (cross-stage read-after-write within one cycle, before any projection).
    context.repository.rds_reads = True
    context.read_photos = context.repository.get_venue_photos(vid)


@then("it reads the photos from RDS, not from the unprojected Redis cache")
def step_reads_from_rds(context):
    assert context.read_photos is not None, (
        "pipeline read returned None — it read the empty Redis cache, not RDS"
    )
    assert context.read_photos[0]["url"] == "https://rds/1.jpg"


@then("the classifier can proceed without waiting for projection")
def step_classifier_proceeds(context):
    # Redis was never populated for this venue; the read came from RDS alone.
    assert context.fake_redis.get(VENUE_PHOTOS_KEY_FORMAT.format(context.vid)) is None


# ── Pass 2b: pipelines write only RDS + gating reads RDS ──────────────────────
@given("the pipeline is decoupled to RDS-only")
def step_pipeline_decoupled(context):
    # rds_writes_only requires rds_reads (the container forces this); set both.
    context.repository.rds_reads = True
    context.repository.rds_writes_only = True


@then('Redis has no serving projection for venue "{vid}" yet')
def step_redis_no_projection_yet(context, vid):
    assert context.redis_only_dao.get_venue(vid) is None, (
        f"venue {vid} was projected to Redis — write-through was not dropped"
    )


@then('the venue "{vid}" is not yet returned by nearby serving')
def step_not_yet_served(context, vid):
    assert vid not in _nearby_ids(context)


# photo refetch gating (RDS freshness, not Redis presence)
@given('a venue "{vid}" has fresh photos in RDS but none projected to Redis')
def step_fresh_photos_rds_only(context, vid):
    context.rds_store.upsert_venue(_venue(vid))
    context.rds_store.upsert_enrichment(
        _PHOTOS_TABLE, vid, {"photos": [{"url": "https://rds/1.jpg", "author_name": "A"}]},
        history=False,
    )
    _backdate_photo_updated_at(context, vid, days=0)  # fresh
    context.fake_redis.delete(VENUE_PHOTOS_KEY_FORMAT.format(vid))


@given('a venue "{vid}" has photos in RDS aged past their TTL')
def step_aged_photos_rds_only(context, vid):
    context.rds_store.upsert_venue(_venue(vid))
    context.rds_store.upsert_enrichment(
        _PHOTOS_TABLE, vid, {"photos": [{"url": "https://rds/old.jpg", "author_name": "A"}]},
        history=False,
    )
    _backdate_photo_updated_at(context, vid, days=settings.photo_cache_ttl_days + 1)
    context.fake_redis.delete(VENUE_PHOTOS_KEY_FORMAT.format(vid))


@when("the photo enrichment job lists which venues have fresh photos")
def step_list_fresh_photos(context):
    context.fresh_photo_ids = set(context.repository.list_cached_venue_photos_ids())


@then('"{vid}" counts as fresh from RDS even though Redis has no photo key')
def step_photo_fresh_from_rds(context, vid):
    assert vid in context.fresh_photo_ids, (
        f"{vid} not counted fresh — gating read empty Redis, not RDS freshness"
    )


@then('"{vid}" is excluded because its RDS photos aged past the TTL')
def step_photo_excluded_aged(context, vid):
    assert vid not in context.fresh_photo_ids


# skip-done gating (RDS presence)
@given('a venue "{vid}" has a vibe profile in RDS but none projected to Redis')
def step_vibe_profile_rds_only(context, vid):
    context.rds_store.upsert_venue(_venue(vid))
    context.rds_store.upsert_enrichment(
        "venues.vibe_profile", vid,
        {"venue_id": vid, "top_vibes": ["animado"], "overall_confidence": 0.9},
        history=False,
    )


@when("an enrichment pipeline lists which venues already have a vibe profile")
def step_list_done_vibe(context):
    context.done_vibe_ids = set(context.repository.list_cached_vibe_profile_venue_ids())


@then('"{vid}" counts as done from RDS even though Redis has no vibe-profile key')
def step_vibe_done_from_rds(context, vid):
    assert vid in context.done_vibe_ids, (
        f"{vid} not counted done — gating read empty Redis, not RDS presence"
    )


# instagram status-aware staleness gating
def _seed_instagram(context, vid, status, days_ago):
    context.rds_store.upsert_venue(_venue(vid))
    context.rds_store.upsert_enrichment(
        "instagram.handle", vid,
        {"venue_id": vid, "instagram_handle": ("h" if status != "not_found" else None),
         "status": status, "confidence_score": (1.0 if status == "found" else 0.0)},
        history=False,
    )
    from datetime import datetime, timedelta, timezone
    context.rds_store.enrichment["instagram.handle"][vid]["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    )


@given('a venue "{vid}" was found on instagram in RDS {days:d} days ago')
def step_ig_found(context, vid, days):
    _seed_instagram(context, vid, "found", days)


@given('a venue "{vid}" was marked not_found on instagram in RDS {days:d} days ago')
def step_ig_not_found(context, vid, days):
    _seed_instagram(context, vid, "not_found", days)


@when("the instagram enrichment lists which venues have fresh instagram")
def step_list_fresh_instagram(context):
    context.fresh_ig_ids = set(context.repository.list_cached_instagram_venue_ids())


@then('"{vid}" counts as fresh because found results live 30 days')
def step_ig_fresh_found(context, vid):
    assert vid in context.fresh_ig_ids, (
        f"{vid} (found, 10d old) should be fresh under the 30d window"
    )


@then('"{vid}" is stale because not_found results expire after 7 days')
def step_ig_stale_not_found(context, vid):
    assert vid not in context.fresh_ig_ids, (
        f"{vid} (not_found, 10d old) should be stale under the 7d window"
    )


# ── B1: projector removes venues deprecated in RDS ────────────────────────────
@when('the eligibility sweep deprecates "{vid}" in RDS only')
def step_deprecate_rds_only(context, vid):
    # RDS-only deprecation (NOT through the write-through repository), so Redis
    # still serves the stale active venue until the projector reconciles it.
    context.rds_store.soft_delete_venue(vid, "ineligible_google_type", "eligibility_filter")
    context.vid = vid


@then('the projector removes "{vid}" from the Redis serving set and geo index')
def step_projector_removed(context, vid):
    assert context.redis_only_dao.get_venue(vid) is None, (
        f"venue {vid} deprecated in RDS must be removed from the Redis serving key"
    )
    assert vid not in _nearby_ids(context), f"venue {vid} still present in the geo index"


@then('the venue "{vid}" is no longer returned by nearby serving')
def step_no_longer_served(context, vid):
    assert vid not in _nearby_ids(context)


# ── B1: idempotency + orphan-safety ───────────────────────────────────────────
@given('a venue "{vid}" is present in Redis with no RDS row at all')
def step_orphan_in_redis(context, vid):
    context.redis_only_dao.upsert_venue(_venue(vid))  # Redis only; no RDS row


@given('a venue "{vid}" is deprecated in RDS after being projected to Redis')
def step_projected_then_deprecated(context, vid):
    context.repository.upsert_venue(_venue(vid))  # RDS active + Redis projection
    context.rds_store.soft_delete_venue(vid, "ineligible_google_type", "eligibility_filter")


@when("the Redis projector runs twice")
def step_projector_runs_twice(context):
    _run_projector(context)
    _run_projector(context)


@then('the active venue "{vid}" is still returned by nearby serving after the second run')
def step_active_still_served(context, vid):
    assert vid in _nearby_ids(context)


@then('the venue "{vid}" with no RDS row is left untouched in Redis')
def step_orphan_untouched(context, vid):
    assert context.redis_only_dao.get_venue(vid) is not None, (
        f"orphan {vid} with no RDS row must NOT be pruned by the projector"
    )


@then('the venue "{vid}" deprecated in RDS is removed from Redis')
def step_deprecated_removed(context, vid):
    assert context.redis_only_dao.get_venue(vid) is None
