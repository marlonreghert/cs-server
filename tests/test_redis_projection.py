"""Unit tests for the decoupling Pass-1 projector fixes (B1 + B2) and helpers.

B1: the projector removes venues deprecated in RDS from the Redis serving set +
geo index, but leaves orphans (no RDS row at all) untouched.
B2: the projector projects photos with the REMAINING TTL (full - age) and drops
photos aged past the TTL, instead of re-stamping a fresh full TTL.

All against the in-memory fake store + fakeredis (no Postgres / Redis needed).
"""
from datetime import datetime, timedelta, timezone

import fakeredis

from app.config import settings
from app.db.geo_redis_client import GeoRedisClient
from app.dao.redis_venue_dao import RedisVenueDAO, VENUE_PHOTOS_KEY_FORMAT
from app.models import Venue
from app.services.redis_projection_service import RedisProjectionService, _age_seconds
from tests.rds_fake import InMemoryRdsVenueStore

_PHOTOS = "google_places.photos"
_LAT, _LNG = -8.05, -34.88


def _venue(vid="v1", name="Bar X"):
    return Venue(venue_id=vid, venue_name=name, venue_address="a",
                 venue_lat=_LAT, venue_lng=_LNG, venue_type="BAR")


def _setup():
    fake = fakeredis.FakeRedis(decode_responses=True)
    geo = GeoRedisClient(fake)
    redis_only = RedisVenueDAO(geo)
    store = InMemoryRdsVenueStore()
    svc = RedisProjectionService(redis_only, store)
    return fake, redis_only, store, svc


def _seed_photos(store, vid, age_days):
    store.upsert_venue(_venue(vid))
    store.upsert_enrichment(
        _PHOTOS, vid, {"photos": [{"url": "https://old/1.jpg", "author_name": "A"}]},
        history=False,
    )
    # tz-aware datetime = the type the real Postgres SELECT yields
    store.enrichment[_PHOTOS][vid]["updated_at"] = (
        datetime.now(timezone.utc) - timedelta(days=age_days)
    )


# ── B1: deprecation removal + orphan-safety ───────────────────────────────────
class TestB1DeprecationRemoval:
    def test_removes_deprecated_keeps_active_and_orphan(self):
        fake, redis_only, store, svc = _setup()
        store.upsert_venue(_venue("v1"))                  # active in RDS
        redis_only.upsert_venue(_venue("vdep"))           # stale-active in Redis
        store.upsert_venue(_venue("vdep"))
        store.soft_delete_venue("vdep", "ineligible", "eligibility_filter")  # RDS-only
        redis_only.upsert_venue(_venue("orphan"))         # Redis-only, no RDS row

        svc.rebuild_redis_from_rds()

        assert redis_only.get_venue("v1") is not None      # active projected
        assert redis_only.get_venue("vdep") is None        # deprecated removed
        assert redis_only.get_venue("orphan") is not None  # orphan untouched

    def test_removed_count_in_summary(self):
        fake, redis_only, store, svc = _setup()
        redis_only.upsert_venue(_venue("vdep"))
        store.upsert_venue(_venue("vdep"))
        store.soft_delete_venue("vdep", "r", "s")
        summary = svc.rebuild_redis_from_rds()
        assert summary["removed"] == 1

    def test_deprecated_removed_from_geo_index(self):
        fake, redis_only, store, svc = _setup()
        store.upsert_venue(_venue("v1"))
        redis_only.upsert_venue(_venue("vdep"))
        store.upsert_venue(_venue("vdep"))
        store.soft_delete_venue("vdep", "r", "s")
        svc.rebuild_redis_from_rds()
        nearby = {v.venue_id for v in redis_only.get_nearby_venues(_LAT, _LNG, 1.0)}
        assert nearby == {"v1"}

    def test_idempotent_across_two_runs(self):
        fake, redis_only, store, svc = _setup()
        store.upsert_venue(_venue("v1"))
        svc.rebuild_redis_from_rds()
        svc.rebuild_redis_from_rds()
        assert redis_only.get_venue("v1") is not None
        assert {v.venue_id for v in redis_only.get_nearby_venues(_LAT, _LNG, 1.0)} == {"v1"}


# ── serving-view reconcile: active-but-ineligible removed, both directions ─────
class TestServingViewReconcile:
    def test_active_but_ineligible_is_not_projected_and_removed(self):
        # A blocked-google-type venue is active in RDS but NOT in the serving view;
        # the projector must not project it and must remove a stale Redis copy.
        fake, redis_only, store, svc = _setup()
        store.upsert_venue(_venue("bar"))                  # eligible
        store.upsert_venue(_venue("market", "Market"))     # active but ineligible
        store.upsert_enrichment(
            "google_places.vibe_attributes", "market",
            {"venue_id": "market", "google_primary_type": "supermarket"},
            history=False, promoted={"google_primary_type": "supermarket"},
        )
        redis_only.upsert_venue(_venue("market", "Market"))  # stale-active in Redis

        svc.rebuild_redis_from_rds()

        assert redis_only.get_venue("bar") is not None
        assert redis_only.get_venue("market") is None       # reconciled out (no lifecycle change)
        assert store.get_venue("market")["lifecycle_status"] == "active"  # still active in RDS

    def test_blocklist_edit_is_reversible_across_runs(self):
        fake, redis_only, store, svc = _setup()
        store.upsert_venue(_venue("v1"))
        store.upsert_enrichment(
            "google_places.vibe_attributes", "v1",
            {"venue_id": "v1", "google_primary_type": "arcade"},
            history=False, promoted={"google_primary_type": "arcade"},
        )
        svc.rebuild_redis_from_rds()
        assert redis_only.get_venue("v1") is not None       # arcade allowed by default

        store.add_eligibility_rule("blocked_google_type", "arcade")
        svc.rebuild_redis_from_rds()
        assert redis_only.get_venue("v1") is None           # blocked -> left serving

        store.remove_eligibility_rule("blocked_google_type", "arcade")
        svc.rebuild_redis_from_rds()
        assert redis_only.get_venue("v1") is not None       # unblocked -> back in serving

    def test_failed_view_read_aborts_without_blanket_delete(self):
        fake, redis_only, store, svc = _setup()
        store.upsert_venue(_venue("v1"))
        svc.rebuild_redis_from_rds()
        assert redis_only.get_venue("v1") is not None
        store.set_unavailable(True)                          # serving view read raises
        summary = svc.rebuild_redis_from_rds()
        assert summary["errors"] == 1
        assert redis_only.get_venue("v1") is not None       # serving left intact


# ── B2: photo remaining-TTL / drop aged ───────────────────────────────────────
class TestB2PhotoTTL:
    def test_projects_remaining_ttl_not_full(self):
        fake, redis_only, store, svc = _setup()
        _seed_photos(store, "v1", age_days=1)
        svc.rebuild_redis_from_rds()
        full = settings.photo_cache_ttl_days * 24 * 3600
        ttl = fake.ttl(VENUE_PHOTOS_KEY_FORMAT.format("v1"))
        assert ttl is not None and 0 < ttl < full           # counted down, not full
        # ~1 day aged against the 5-day TTL → ~4 days remaining (±5 min slack)
        expected = full - 24 * 3600
        assert expected - 300 <= ttl <= expected + 300

    def test_drops_photos_aged_past_ttl(self):
        fake, redis_only, store, svc = _setup()
        _seed_photos(store, "v1", age_days=settings.photo_cache_ttl_days + 1)
        svc.rebuild_redis_from_rds()
        assert redis_only.get_venue_photos("v1") is None    # projected absent

    def test_fresh_photos_get_near_full_ttl(self):
        fake, redis_only, store, svc = _setup()
        _seed_photos(store, "v1", age_days=0)
        svc.rebuild_redis_from_rds()
        full = settings.photo_cache_ttl_days * 24 * 3600
        ttl = fake.ttl(VENUE_PHOTOS_KEY_FORMAT.format("v1"))
        assert full - 300 <= ttl <= full


# ── _age_seconds coercion (the silent real-vs-fake type trap) ─────────────────
class TestAgeSeconds:
    def test_none_and_garbage_return_none(self):
        assert _age_seconds(None) is None
        assert _age_seconds("not-a-timestamp") is None

    def test_tz_aware_datetime(self):
        ts = datetime.now(timezone.utc) - timedelta(seconds=100)
        assert abs(_age_seconds(ts) - 100) < 5

    def test_iso_string(self):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        assert abs(_age_seconds(ts) - 100) < 5

    def test_naive_datetime_treated_as_utc(self):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=100)).replace(tzinfo=None)
        assert abs(_age_seconds(ts) - 100) < 5


# ── set_venue_photos ttl_seconds parameter ────────────────────────────────────
class TestSetVenuePhotosTTL:
    def test_explicit_ttl_applied(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        dao = RedisVenueDAO(GeoRedisClient(fake))
        dao.set_venue_photos("v1", [{"url": "u", "author_name": "a"}], ttl_seconds=123)
        assert 0 < fake.ttl(VENUE_PHOTOS_KEY_FORMAT.format("v1")) <= 123

    def test_default_ttl_is_full(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        dao = RedisVenueDAO(GeoRedisClient(fake))
        dao.set_venue_photos("v1", [{"url": "u", "author_name": "a"}])
        full = settings.photo_cache_ttl_days * 24 * 3600
        assert full - 5 <= fake.ttl(VENUE_PHOTOS_KEY_FORMAT.format("v1")) <= full
