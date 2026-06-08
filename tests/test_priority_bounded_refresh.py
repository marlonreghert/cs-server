"""Unit tests for priority-bounded BestTime refresh selection + identical set.

Covers the RDS selection contract via the in-memory fake (the proven behaviour
contract for the SQLAlchemy store) and the guarantee that live + weekly refresh
request the identical priority-bounded venue set.
"""
import fakeredis
import pytest

from app.dao.redis_venue_dao import RedisVenueDAO  # noqa: F401 (kept for parity)
from app.dao.venue_budget_dao import VenueBudgetDao
from app.dao.venue_repository import VenueRepository
from app.db.geo_redis_client import GeoRedisClient
from app.handlers.add_venue_handler import (
    DEFAULT_FALLBACK_RADIUS_M,
    MAX_FALLBACK_RADIUS_M,
    AddVenueByAddressRequest,
)
from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo
from app.services.venue_budget_service import VenueBudgetService
from app.services.venues_refresher_service import VenuesRefresherService
from tests.rds_fake import InMemoryRdsVenueStore


def _venue(vid, priority=5, reviews=None, rating=None):
    return Venue(
        forecast=True,
        processed=True,
        venue_id=vid,
        venue_name=f"Venue {vid}",
        venue_address=f"addr {vid}",
        venue_lat=-8.05,
        venue_lng=-34.88,
        priority=priority,
        reviews=reviews,
        rating=rating,
    )


class TestPrioritySelection:
    def _store_with(self, venues):
        store = InMemoryRdsVenueStore()
        for v in venues:
            store.upsert_venue(v)
        return store

    def test_orders_by_priority_then_reviews_then_rating(self):
        store = self._store_with([
            _venue("low_prio", priority=5, reviews=9999, rating=5.0),
            _venue("p0_few", priority=0, reviews=10, rating=4.0),
            _venue("p0_many", priority=0, reviews=900, rating=4.0),
            _venue("p1", priority=1, reviews=500, rating=4.0),
        ])
        assert store.list_active_venue_ids_by_priority(10) == [
            "p0_many", "p0_few", "p1", "low_prio",
        ]

    def test_rating_breaks_reviews_tie(self):
        store = self._store_with([
            _venue("a", priority=0, reviews=100, rating=4.1),
            _venue("b", priority=0, reviews=100, rating=4.9),
        ])
        assert store.list_active_venue_ids_by_priority(10) == ["b", "a"]

    def test_null_reviews_and_rating_sort_last(self):
        store = self._store_with([
            _venue("has_reviews", priority=0, reviews=1, rating=None),
            _venue("no_signal", priority=0, reviews=None, rating=None),
        ])
        assert store.list_active_venue_ids_by_priority(10) == [
            "has_reviews", "no_signal",
        ]

    def test_limit_is_respected(self):
        store = self._store_with([
            _venue("a", priority=0, reviews=3),
            _venue("b", priority=0, reviews=2),
            _venue("c", priority=0, reviews=1),
        ])
        assert store.list_active_venue_ids_by_priority(2) == ["a", "b"]

    def test_non_positive_limit_selects_nothing(self):
        store = self._store_with([_venue("a", priority=0)])
        assert store.list_active_venue_ids_by_priority(0) == []
        assert store.list_active_venue_ids_by_priority(-5) == []

    def test_deprecated_venues_excluded(self):
        store = self._store_with([
            _venue("active", priority=0, reviews=1),
            _venue("dead", priority=0, reviews=999),
        ])
        store.soft_delete_venue("dead", "ineligible", "test")
        assert store.list_active_venue_ids_by_priority(10) == ["active"]

    def test_repository_delegates_to_store(self):
        store = self._store_with([
            _venue("a", priority=1, reviews=1),
            _venue("b", priority=0, reviews=1),
        ])
        repo = VenueRepository(
            GeoRedisClient(fakeredis.FakeRedis(decode_responses=True)),
            rds_store=store,
        )
        assert repo.list_active_venue_ids_by_priority(10) == ["b", "a"]

    def test_stable_across_repeated_calls(self):
        store = self._store_with([
            _venue("a", priority=0, reviews=5),
            _venue("b", priority=0, reviews=5, rating=4.5),
            _venue("c", priority=0, reviews=5, rating=4.5),
        ])
        first = store.list_active_venue_ids_by_priority(10)
        assert first == store.list_active_venue_ids_by_priority(10)


class TestPriorityPersistence:
    """Manual RDS priority edits must survive a re-upsert. Priority is managed
    only by direct SQL (one-time tiering + manual edits); a default-constructed
    re-upsert (e.g. discovery re-finding a venue) must never reset it."""

    def test_reupsert_preserves_existing_priority(self):
        store = InMemoryRdsVenueStore()
        store.upsert_venue(_venue("a", priority=0))
        store.upsert_venue(_venue("b", priority=1))
        # Re-find via discovery: a fresh Venue defaults priority to 5.
        store.upsert_venue(_venue("a", priority=5))
        # 'a' must still be P0 (would fall below 'b' if it were reset to P5).
        assert store.list_active_venue_ids_by_priority(2) == ["a", "b"]
        assert store.get_venue("a")["priority"] == 0

    def test_new_venue_keeps_its_priority(self):
        store = InMemoryRdsVenueStore()
        store.upsert_venue(_venue("new", priority=2))
        assert store.get_venue("new")["priority"] == 2

    def test_repository_reupsert_preserves_priority(self):
        store = InMemoryRdsVenueStore()
        repo = VenueRepository(
            GeoRedisClient(fakeredis.FakeRedis(decode_responses=True)),
            rds_store=store,
        )
        repo.upsert_venue(_venue("a", priority=0))
        repo.upsert_venue(_venue("b", priority=1))
        repo.upsert_venue(_venue("a", priority=5))
        assert repo.list_active_venue_ids_by_priority(2) == ["a", "b"]


class _RecordingBesttime:
    def __init__(self):
        self.live_calls = []
        self.weekly_calls = []

    async def get_live_forecast(self, venue_id=None, **_):
        self.live_calls.append(venue_id)
        return LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id=venue_id),
            analysis=Analysis(venue_live_busyness=10, venue_live_busyness_available=True),
        )

    async def get_week_raw_forecast(self, venue_id):
        self.weekly_calls.append(venue_id)

        class _Resp:
            status = "OK"

            class analysis:
                week_raw = []

        return _Resp()


@pytest.mark.asyncio
async def test_live_and_weekly_request_identical_set():
    fake = fakeredis.FakeRedis(decode_responses=True)
    fake.set(
        "admin_config:venue_monthly_budget",
        '{"monthly_quota": 500, "manual_reserve": 498}',  # X = 2
    )
    store = InMemoryRdsVenueStore()
    for i in range(10):
        store.upsert_venue(_venue(f"v{i}", priority=i % 3, reviews=1000 - i, rating=4.0))
    repo = VenueRepository(GeoRedisClient(fake), rds_store=store)
    budget = VenueBudgetService(
        redis_client=fake,
        budget_dao=VenueBudgetDao(fake),
        year_month_provider=lambda: "2026-05",
    )
    besttime = _RecordingBesttime()
    refresher = VenuesRefresherService(
        venue_dao=repo, besttime_api=besttime, redis_client=fake
    )
    refresher.set_budget_service(budget)

    await refresher.refresh_live_forecasts_for_all_venues()
    await refresher.refresh_weekly_forecasts_for_all_venues()

    # Exactly X=2 distinct venues, and the two jobs touch the identical set.
    assert len(set(besttime.live_calls)) == 2
    assert set(besttime.live_calls) == set(besttime.weekly_calls)
    # Weekly re-reading the same venues adds no new unique against the cap.
    assert budget.unique_touched_count() == 2


class TestAddVenueRadiusConstants:
    def test_fallback_radius_is_50m(self):
        assert DEFAULT_FALLBACK_RADIUS_M == 50
        assert MAX_FALLBACK_RADIUS_M == 50

    def test_request_rejects_radius_above_50(self):
        with pytest.raises(ValueError):
            AddVenueByAddressRequest(
                venue_name="X",
                venue_address="Y",
                venue_lat=-8.0,
                venue_lng=-34.9,
                fallback_radius_meters=200,
            )

    def test_request_accepts_radius_at_50(self):
        req = AddVenueByAddressRequest(
            venue_name="X",
            venue_address="Y",
            venue_lat=-8.0,
            venue_lng=-34.9,
            fallback_radius_meters=50,
        )
        assert req.fallback_radius_meters == 50
