"""Unit tests for the RDS write-through repository + projection (via fake store)."""
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from app.config import settings

from app.db.geo_redis_client import GeoRedisClient
from app.dao.redis_venue_dao import RedisVenueDAO
from app.dao.venue_repository import VenueRepository
from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo, WeekRawDay
from app.models.vibe_attributes import VibeAttributes
from app.models.opening_hours import OpeningHours
from app.models.instagram import InstagramPost, VenueInstagram, VenueInstagramPosts
from app.models.menu import MenuItem, MenuPhoto, MenuSection, VenueMenuData, VenueMenuPhotos
from app.models.venue_review import VenueReview, VenueReviews
from app.models.vibe_profile import VenueVibeProfile
from app.services.redis_projection_service import RedisProjectionService
from app.services.engagement_service import EngagementService
from tests.rds_fake import InMemoryRdsVenueStore, RdsUnavailable

_VA = "google_places.vibe_attributes"


def _geo():
    return GeoRedisClient(fakeredis.FakeRedis(decode_responses=True))


def _venue(vid="v1", name="Bar X"):
    return Venue(venue_id=vid, venue_name=name, venue_address="a",
                 venue_lat=-8.05, venue_lng=-34.88, venue_type="BAR")


class TestFlagOffParity:
    def test_repository_without_store_behaves_as_dao(self):
        repo = VenueRepository(_geo(), rds_store=None)  # rds_enabled=false
        repo.upsert_venue(_venue())
        repo.set_vibe_attributes(VibeAttributes(venue_id="v1", google_primary_type="bar"))
        # Reads (inherited) work; no exception, no RDS dependency.
        assert repo.get_venue("v1") is not None
        assert repo.get_vibe_attributes("v1").google_primary_type == "bar"


class TestWriteThrough:
    def test_writes_rds_then_redis(self):
        store = InMemoryRdsVenueStore()
        repo = VenueRepository(_geo(), rds_store=store)
        repo.upsert_venue(_venue())
        assert store.get_venue("v1") is not None          # truth
        assert repo.get_venue("v1") is not None            # projection

    def test_live_forecast_persisted_to_rds(self):
        store = InMemoryRdsVenueStore()
        repo = VenueRepository(_geo(), rds_store=store)
        repo.upsert_venue(_venue())
        repo.set_live_forecast(LiveForecastResponse(
            status="OK", venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(venue_live_busyness=42, venue_live_busyness_available=True)))
        assert store.get_live_forecast("v1") is not None

    def test_rds_outage_does_not_corrupt_redis_projection(self):
        store = InMemoryRdsVenueStore()
        repo = VenueRepository(_geo(), rds_store=store)
        repo.upsert_venue(_venue(name="Original"))
        store.set_unavailable(True)
        with pytest.raises(RdsUnavailable):
            repo.upsert_venue(_venue(name="Renamed"))
        # RDS-first: projection never updated, original name intact.
        assert repo.get_venue("v1").venue_name == "Original"


class TestNeverDelete:
    def test_delete_soft_deletes_rds_with_history(self):
        store = InMemoryRdsVenueStore()
        repo = VenueRepository(_geo(), rds_store=store)
        repo.upsert_venue(_venue())
        repo.set_vibe_attributes(VibeAttributes(venue_id="v1", google_primary_type="bar"))
        repo.delete_vibe_attributes("v1")
        rec = store.get_enrichment(_VA, "v1")
        assert rec is not None and rec["deleted_at"] is not None  # soft, not gone
        assert store.history_count(_VA, "v1") >= 1                # recoverable
        assert repo.get_vibe_attributes("v1") is None             # Redis cache dropped

    def test_photos_excluded_from_history(self):
        store = InMemoryRdsVenueStore()
        repo = VenueRepository(_geo(), rds_store=store)
        repo.upsert_venue(_venue())
        repo.set_venue_photos("v1", [{"url": "u", "author_name": "a"}])
        assert store.get_enrichment("google_places.photos", "v1") is not None
        assert store.history_count("google_places.photos", "v1") == 0


class TestProjectionService:
    def test_backfill_is_idempotent_and_venue_first(self):
        store = InMemoryRdsVenueStore()
        geo = _geo()
        redis_only = RedisVenueDAO(geo)
        repo = VenueRepository(geo, rds_store=store)
        # pre-RDS Redis-only state
        redis_only.upsert_venue(_venue())
        redis_only.set_vibe_attributes(VibeAttributes(venue_id="v1", google_primary_type="bar"))
        svc = RedisProjectionService(repo, redis_only, store)
        svc.backfill_rds_from_redis()
        svc.backfill_rds_from_redis()  # idempotent re-run
        assert store.get_venue("v1") is not None
        assert store.get_enrichment(_VA, "v1") is not None

    def test_rebuild_restores_geo_and_live(self):
        store = InMemoryRdsVenueStore()
        geo = _geo()
        redis_only = RedisVenueDAO(geo)
        repo = VenueRepository(geo, rds_store=store)
        repo.upsert_venue(_venue())
        repo.set_live_forecast(LiveForecastResponse(
            status="OK", venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(venue_live_busyness=42, venue_live_busyness_available=True)))
        geo.client.flushall()  # lose Redis
        assert redis_only.get_venue("v1") is None
        RedisProjectionService(repo, redis_only, store).rebuild_redis_from_rds()
        # geo index + json restored, nearby finds it, live restored
        assert redis_only.get_venue("v1") is not None
        assert {v.venue_id for v in redis_only.get_nearby_venues(-8.05, -34.88, 1.0)} == {"v1"}
        assert redis_only.get_live_forecast("v1") is not None


class TestPass2aReadParity:
    """RDS-read path (rds_reads=True) must return models field-equal to the
    Redis-read path. This is what catches reconstruction drift once pipelines
    read RDS instead of the Redis projection."""

    def _seed_full(self, repo):
        # write-through populates BOTH RDS and Redis from the same objects
        repo.upsert_venue(_venue())
        repo.set_vibe_attributes(
            VibeAttributes(venue_id="v1", google_place_id="p", google_primary_type="bar"))
        repo.set_opening_hours(OpeningHours(venue_id="v1", weekday_descriptions=["Seg: 18-02"]))
        repo.set_venue_reviews(VenueReviews(venue_id="v1", reviews=[
            VenueReview(author_name="A", rating=5, text="ok", relative_time="today")]))
        repo.set_venue_instagram(VenueInstagram(
            venue_id="v1", instagram_handle="h", instagram_url="https://ig/h",
            status="found", confidence_score=1.0))
        repo.set_venue_ig_posts(VenueInstagramPosts(
            venue_id="v1", instagram_handle="h", posts=[InstagramPost(caption="hi")]))
        repo.set_venue_menu_photos(VenueMenuPhotos(venue_id="v1", photos=[
            MenuPhoto(photo_id="p1", s3_url="https://s3/m.jpg", s3_key="m.jpg")]))
        repo.set_venue_menu_data(VenueMenuData(venue_id="v1", sections=[
            MenuSection(name="Drinks", items=[MenuItem(name="Beer", prices=[{"price": 12}])])]))
        repo.set_venue_vibe_profile(VenueVibeProfile(
            venue_id="v1", top_vibes=["animado"], overall_confidence=0.9))
        repo.set_venue_photos("v1", [{"url": "https://p/1.jpg", "author_name": "A"}])
        repo.set_week_raw_forecast("v1", WeekRawDay(day_int=0, day_raw=[50] * 24))
        repo.set_live_forecast(LiveForecastResponse(
            status="OK", venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(venue_live_busyness=42, venue_live_busyness_available=True)))

    def test_rds_reads_match_redis_reads(self):
        store = InMemoryRdsVenueStore()
        geo = _geo()
        redis_only = RedisVenueDAO(geo)
        write_through = VenueRepository(geo, rds_store=store, rds_reads=False)
        self._seed_full(write_through)
        rds_reader = VenueRepository(geo, rds_store=store, rds_reads=True)

        def _eq(getter, *args):
            r = getattr(redis_only, getter)(*args)
            d = getattr(rds_reader, getter)(*args)
            assert r is not None and d is not None, f"{getter} returned None"
            assert (d.model_dump(by_alias=True, mode="json")
                    == r.model_dump(by_alias=True, mode="json")), getter

        for getter in (
            "get_venue", "get_vibe_attributes", "get_opening_hours", "get_venue_reviews",
            "get_venue_instagram", "get_venue_ig_posts", "get_venue_menu_photos",
            "get_venue_menu_data", "get_venue_vibe_profile", "get_live_forecast",
        ):
            _eq(getter, "v1")
        _eq("get_week_raw_forecast", "v1", 0)

        # photos are plain dicts (no model)
        assert rds_reader.get_venue_photos("v1") == redis_only.get_venue_photos("v1")
        # collection reads
        assert set(rds_reader.list_active_venue_ids()) == set(redis_only.list_active_venue_ids()) == {"v1"}
        assert {v.venue_id for v in rds_reader.list_all_venues()} == {"v1"}

    def test_flag_off_reads_redis(self):
        # rds_reads=False must read Redis even when RDS holds a different value.
        store = InMemoryRdsVenueStore()
        geo = _geo()
        repo = VenueRepository(geo, rds_store=store, rds_reads=False)
        repo.upsert_venue(_venue(name="Redis Name"))
        store.venues["v1"]["payload"]["venue_name"] = "RDS Only Name"  # diverge RDS
        assert repo.get_venue("v1").venue_name == "Redis Name"  # read Redis, not RDS

    def test_write_guard_holds_when_reads_are_rds(self):
        # With rds_reads=True the write path's internal self.get_venue reads RDS;
        # an active re-add of a deprecated venue must still not resurrect it.
        store = InMemoryRdsVenueStore()
        geo = _geo()
        repo = VenueRepository(geo, rds_store=store, rds_reads=True)
        repo.upsert_venue(_venue())
        repo.soft_delete_venue("v1", "ineligible_google_type", "eligibility_filter")
        repo.upsert_venue(_venue(name="Re-added Active"))  # active re-add
        assert store.get_venue("v1")["lifecycle_status"] == "deprecated"
        assert "v1" not in store.list_active_venue_ids()

    def test_serving_dao_never_reads_rds(self):
        # A plain RedisVenueDAO (serving_dao) is unaffected by RDS — reads Redis.
        store = InMemoryRdsVenueStore()
        geo = _geo()
        serving = RedisVenueDAO(geo)
        VenueRepository(geo, rds_store=store).upsert_venue(_venue())
        assert serving.get_venue("v1") is not None
        assert not hasattr(serving, "rds_store")


class TestPass2bWritesOnly:
    """rds_writes_only: writes persist ONLY to RDS (projector is sole Redis writer)."""

    def _repo(self, geo, store):
        return VenueRepository(geo, rds_store=store, rds_reads=True, rds_writes_only=True)

    def test_upsert_writes_rds_not_redis(self):
        store, geo, redis_only = InMemoryRdsVenueStore(), _geo(), None
        redis_only = RedisVenueDAO(geo)
        repo = self._repo(geo, store)
        repo.upsert_venue(_venue())
        assert store.get_venue("v1") is not None       # RDS truth written
        assert redis_only.get_venue("v1") is None       # Redis NOT written
        assert repo.get_venue("v1") is not None          # reads come from RDS

    def test_enrichment_writes_rds_not_redis(self):
        store, geo = InMemoryRdsVenueStore(), _geo()
        redis_only = RedisVenueDAO(geo)
        repo = self._repo(geo, store)
        repo.upsert_venue(_venue())
        repo.set_vibe_attributes(VibeAttributes(venue_id="v1", google_primary_type="bar"))
        repo.set_venue_photos("v1", [{"url": "u", "author_name": "a"}])
        assert store.get_enrichment(_VA, "v1") is not None
        assert redis_only.get_vibe_attributes("v1") is None
        assert redis_only.get_venue_photos("v1") is None

    def test_soft_delete_enrichment_no_redis_write(self):
        store, geo = InMemoryRdsVenueStore(), _geo()
        redis_only = RedisVenueDAO(geo)
        # seed the Redis copy so we can prove the delete does NOT touch it
        redis_only.set_vibe_attributes(VibeAttributes(venue_id="v1", google_primary_type="bar"))
        repo = self._repo(geo, store)
        repo.upsert_venue(_venue())
        repo.set_vibe_attributes(VibeAttributes(venue_id="v1", google_primary_type="bar"))
        repo.delete_vibe_attributes("v1")
        assert store.get_enrichment(_VA, "v1")["deleted_at"] is not None  # RDS soft-delete
        assert redis_only.get_vibe_attributes("v1") is not None           # Redis untouched

    def test_delete_live_forecast_routes_to_rds(self):
        store, geo = InMemoryRdsVenueStore(), _geo()
        repo = self._repo(geo, store)
        repo.upsert_venue(_venue())
        repo.set_live_forecast(LiveForecastResponse(
            status="OK", venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(venue_live_busyness=1, venue_live_busyness_available=True)))
        assert store.get_live_forecast("v1") is not None
        repo.delete_live_forecast("v1")
        assert store.get_live_forecast("v1") is None  # section-E gap closed

    def test_flag_off_still_write_through(self):
        store, geo = InMemoryRdsVenueStore(), _geo()
        redis_only = RedisVenueDAO(geo)
        repo = VenueRepository(geo, rds_store=store, rds_reads=False, rds_writes_only=False)
        repo.upsert_venue(_venue())
        assert store.get_venue("v1") is not None and redis_only.get_venue("v1") is not None

    def test_set_google_business_status_routes_to_rds(self):
        # Section E: set_google_business_status routes through the overridden
        # get_venue + upsert_venue, so it persists to RDS and (writes-only) never
        # escapes to Redis.
        store, geo = InMemoryRdsVenueStore(), _geo()
        redis_only = RedisVenueDAO(geo)
        repo = self._repo(geo, store)
        repo.upsert_venue(_venue())
        repo.set_google_business_status("v1", "CLOSED_TEMPORARILY")
        assert store.get_venue("v1")["payload"]["google_business_status"] == "CLOSED_TEMPORARILY"
        assert redis_only.get_venue("v1") is None


class TestPass2bGating:
    """rds_writes_only: cache-freshness gating reads RDS (status-aware staleness)."""

    def _repo(self, geo, store):
        return VenueRepository(geo, rds_store=store, rds_reads=True, rds_writes_only=True)

    def _age(self, store, table, vid, days):
        store.enrichment[table][vid]["updated_at"] = (
            datetime.now(timezone.utc) - timedelta(days=days)
        )

    def test_photo_gating_uses_rds_freshness(self):
        store, geo = InMemoryRdsVenueStore(), _geo()
        repo = self._repo(geo, store)
        for vid in ("v1", "v2"):
            store.upsert_venue(_venue(vid))
            store.upsert_enrichment("google_places.photos", vid,
                                    {"photos": [{"url": "u"}]}, history=False)
        self._age(store, "google_places.photos", "v1", 0)                          # fresh
        self._age(store, "google_places.photos", "v2", settings.photo_cache_ttl_days + 1)  # aged
        fresh = set(repo.list_cached_venue_photos_ids())
        assert "v1" in fresh and "v2" not in fresh

    def test_vibe_profile_gating_is_presence(self):
        store, geo = InMemoryRdsVenueStore(), _geo()
        repo = self._repo(geo, store)
        store.upsert_venue(_venue())
        store.upsert_enrichment("venues.vibe_profile", "v1",
                                {"venue_id": "v1", "top_vibes": [], "overall_confidence": 0.5},
                                history=False)
        assert "v1" in set(repo.list_cached_vibe_profile_venue_ids())

    def test_instagram_gating_status_aware(self):
        store, geo = InMemoryRdsVenueStore(), _geo()
        repo = self._repo(geo, store)
        for vid, status in (("v1", "found"), ("v2", "not_found")):
            store.upsert_venue(_venue(vid))
            store.upsert_enrichment("instagram.handle", vid,
                                    {"venue_id": vid, "status": status}, history=False)
            self._age(store, "instagram.handle", vid, 10)  # both 10 days old
        fresh = set(repo.list_cached_instagram_venue_ids())
        assert "v1" in fresh       # found: fresh within 30d
        assert "v2" not in fresh   # not_found: stale past 7d

    def test_flag_off_gating_reads_redis(self):
        store, geo = InMemoryRdsVenueStore(), _geo()
        repo = VenueRepository(geo, rds_store=store, rds_reads=True, rds_writes_only=False)
        repo.set_venue_photos("v1", [{"url": "u"}])  # write-through projects Redis
        assert "v1" in set(repo.list_cached_venue_photos_ids())  # from Redis SCAN

    def test_instagram_fresh_set_flag_off_matches_redis_presence(self):
        # The dual-purpose split must be behavior-identical flag-off: a venue with
        # a non-expired Redis instagram key is exactly the get_venue_instagram set.
        store, geo = InMemoryRdsVenueStore(), _geo()
        repo = VenueRepository(geo, rds_store=store, rds_writes_only=False)
        repo.upsert_venue(_venue())
        repo.set_venue_instagram(VenueInstagram(
            venue_id="v1", instagram_handle="h", status="found", confidence_score=1.0))
        fresh = set(repo.list_cached_instagram_venue_ids())
        present_as_data = repo.get_venue_instagram("v1") is not None
        assert ("v1" in fresh) and present_as_data


class TestEngagementPseudonymization:
    def test_user_id_pseudonymized_and_favorite_roundtrip(self):
        store = InMemoryRdsVenueStore()
        svc = EngagementService(
            fakeredis.FakeRedis(decode_responses=True), rds_store=store,
            pseudonymization_key="k")
        svc.add_favorite("user-123", "v1")
        assert not store.contains_raw_value("user-123")     # raw id never stored
        pseudo = svc.pseudonymize("user-123")
        assert store.get_favorite(pseudo, "v1")["deleted_at"] is None
        svc.remove_favorite("user-123", "v1")
        assert store.get_favorite(pseudo, "v1")["deleted_at"] is not None


class TestEngagementRedisContract:
    """Projection keys MUST match what vibes_bot reads (silent-mismatch guard)."""

    def _svc(self):
        self.fake = __import__("fakeredis").FakeRedis(decode_responses=True)
        return EngagementService(self.fake, rds_store=InMemoryRdsVenueStore(),
                                 pseudonymization_key="k")

    def test_favorite_key_matches_vibes_bot(self):
        svc = self._svc()
        svc.add_favorite("u1", "v1")
        assert self.fake.sismember("user_favorites:u1", "v1")  # vibes_bot read key

    def test_hot_like_key_is_versioned_and_ttl_honored(self):
        svc = self._svc()
        svc.add_hot_like("u1", "v1", ttl_seconds=120)
        assert self.fake.sismember("hot_likes:v1:v1", "u1")    # hot_likes:v1:{venue}
        assert not self.fake.exists("hot_likes:v1")            # not the unversioned key
        assert 0 < self.fake.ttl("hot_likes:v1:v1") <= 120     # client ttl_seconds applied

    def test_remove_hot_like_srem(self):
        svc = self._svc()
        svc.add_hot_like("u1", "v1")
        svc.remove_hot_like("u1", "v1")
        assert not self.fake.sismember("hot_likes:v1:v1", "u1")
