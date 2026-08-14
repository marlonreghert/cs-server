"""Unit tests for the deep-review-corpus projection slice: RedisVenueDAO.
set_venue_reviews_deep bounds what reaches Redis to the newest
`settings.reviews_deep_projection_max`, and RedisProjectionService's
_REBUILD_MODELS entry re-asserts / propagates deletion for it exactly like
every other enrichment family. Mirrors tests/test_redis_projection.py's
fixture shape (fakeredis + InMemoryRdsVenueStore, no Postgres/Redis needed).
See plans/260813_deep-review-corpus.md.
"""
from datetime import datetime, timedelta, timezone

import fakeredis

from app.config import settings
from app.db.geo_redis_client import GeoRedisClient
from app.dao.redis_venue_dao import RedisVenueDAO
from app.models import Venue
from app.models.venue_review import VenueReview, VenueReviewsDeep
from app.services.redis_projection_service import RedisProjectionService
from tests.rds_fake import InMemoryRdsVenueStore

_TABLE = "venues.reviews_deep"
_LAT, _LNG = -8.05, -34.88


def _venue(vid="v1"):
    return Venue(venue_id=vid, venue_name="Bar X", venue_address="a",
                 venue_lat=_LAT, venue_lng=_LNG, venue_type="BAR")


def _setup():
    fake = fakeredis.FakeRedis(decode_responses=True)
    geo = GeoRedisClient(fake)
    redis_only = RedisVenueDAO(geo)
    store = InMemoryRdsVenueStore()
    svc = RedisProjectionService(redis_only, store)
    return fake, redis_only, store, svc


def _deep(vid, n, base=None):
    base = base or datetime.now(timezone.utc)
    reviews = [
        VenueReview(
            author_name=f"A{i}", rating=5, text=f"t{i}", relative_time="",
            publish_time=(base - timedelta(hours=i)).isoformat(),
            review_id=f"{vid}-r{i}", source="apify_gmaps",
        )
        for i in range(n)
    ]
    times = [r.publish_time for r in reviews]
    return VenueReviewsDeep(
        venue_id=vid, reviews=reviews, window_days=180, fetched_at=base.isoformat(),
        oldest_publish_time=min(times), newest_publish_time=max(times), truncated=False,
    )


class TestRedisVenueDAOProjectionSliceCap:
    def test_set_bounds_to_configured_projection_max(self, monkeypatch):
        fake = fakeredis.FakeRedis(decode_responses=True)
        redis_only = RedisVenueDAO(GeoRedisClient(fake))
        monkeypatch.setattr(settings, "reviews_deep_projection_max", 5)
        redis_only.set_venue_reviews_deep(_deep("v1", 50))
        projected = redis_only.get_venue_reviews_deep("v1")
        assert len(projected.reviews) == 5

    def test_slice_keeps_the_newest_first(self, monkeypatch):
        fake = fakeredis.FakeRedis(decode_responses=True)
        redis_only = RedisVenueDAO(GeoRedisClient(fake))
        monkeypatch.setattr(settings, "reviews_deep_projection_max", 3)
        deep = _deep("v1", 10)
        redis_only.set_venue_reviews_deep(deep)
        projected = redis_only.get_venue_reviews_deep("v1")
        expected_ids = {r.review_id for r in sorted(deep.reviews, key=lambda r: r.publish_time, reverse=True)[:3]}
        assert {r.review_id for r in projected.reviews} == expected_ids

    def test_fewer_reviews_than_the_cap_are_all_kept(self, monkeypatch):
        fake = fakeredis.FakeRedis(decode_responses=True)
        redis_only = RedisVenueDAO(GeoRedisClient(fake))
        monkeypatch.setattr(settings, "reviews_deep_projection_max", 40)
        redis_only.set_venue_reviews_deep(_deep("v1", 7))
        projected = redis_only.get_venue_reviews_deep("v1")
        assert len(projected.reviews) == 7

    def test_returns_bytes_written_for_projector_observability(self, monkeypatch):
        fake = fakeredis.FakeRedis(decode_responses=True)
        redis_only = RedisVenueDAO(GeoRedisClient(fake))
        monkeypatch.setattr(settings, "reviews_deep_projection_max", 40)
        written = redis_only.set_venue_reviews_deep(_deep("v1", 5))
        assert isinstance(written, int) and written > 0


class TestRedisProjectionServiceReviewsDeep:
    def test_rebuild_projects_bounded_slice_rds_keeps_full(self, monkeypatch):
        fake, redis_only, store, svc = _setup()
        monkeypatch.setattr(settings, "reviews_deep_projection_max", 4)
        store.upsert_venue(_venue("v1"))
        deep = _deep("v1", 20)
        store.upsert_enrichment(_TABLE, "v1", deep.model_dump(mode="json"), history=True)

        summary = svc.rebuild_redis_from_rds()

        assert summary["errors"] == 0
        projected = redis_only.get_venue_reviews_deep("v1")
        assert len(projected.reviews) == 4
        full = store.get_enrichment(_TABLE, "v1")
        assert len(full["payload"]["reviews"]) == 20

    def test_soft_deleted_row_is_absent_after_rebuild(self, monkeypatch):
        fake, redis_only, store, svc = _setup()
        monkeypatch.setattr(settings, "reviews_deep_projection_max", 40)
        store.upsert_venue(_venue("v1"))
        deep = _deep("v1", 3)
        store.upsert_enrichment(_TABLE, "v1", deep.model_dump(mode="json"), history=True)
        svc.rebuild_redis_from_rds()
        assert redis_only.get_venue_reviews_deep("v1") is not None

        store.soft_delete_enrichment(_TABLE, "v1", history=True)
        svc.rebuild_redis_from_rds()
        assert redis_only.get_venue_reviews_deep("v1") is None

    def test_a_venue_never_enriched_with_deep_reviews_is_unaffected(self, monkeypatch):
        """No `venues.reviews_deep` row at all must not error or write
        anything — the common case for the ~1437 venues that have never had
        a deep crawl run against them."""
        fake, redis_only, store, svc = _setup()
        store.upsert_venue(_venue("v1"))
        summary = svc.rebuild_redis_from_rds()
        assert summary["errors"] == 0
        assert redis_only.get_venue_reviews_deep("v1") is None
