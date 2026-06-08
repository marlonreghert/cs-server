"""Unit tests for the schema-normalization equivalence harness."""
import fakeredis

from app.dao.redis_venue_dao import RedisVenueDAO
from app.db.geo_redis_client import GeoRedisClient
from app.models import Venue
from app.models.venue import FootTrafficForecast
from app.services.equivalence_verify import (
    canonical_venue,
    redis_vs_rds_serving_diff,
    venue_diff_fields,
)
from app.services.redis_projection_service import RedisProjectionService
from tests.rds_fake import InMemoryRdsVenueStore


def _fresh_dao() -> RedisVenueDAO:
    return RedisVenueDAO(GeoRedisClient(fakeredis.FakeRedis(decode_responses=True)))


def _full_venue(vid="v1") -> Venue:
    return Venue(
        forecast=True, processed=True, venue_id=vid, venue_name=f"Bar {vid}",
        venue_address=f"{vid} Rua X, 100", venue_lat=-8.05, venue_lng=-34.88,
        venue_type="BAR", price_level=2, rating=4.5, reviews=321, priority=4,
        venue_dwell_time_min=30, venue_dwell_time_max=90,
        venue_foot_traffic_forecast=[FootTrafficForecast(day_int=0, day_raw=[10] * 24)],
    )


def test_canonical_venue_is_stable_and_float_tolerant():
    a = _full_venue()
    b = _full_venue()
    b.venue_lat = a.venue_lat + 1e-12  # below the rounding precision
    assert canonical_venue(a) == canonical_venue(b)


def test_venue_diff_fields_reports_changed_field_only():
    a = _full_venue()
    b = _full_venue()
    b.venue_name = "Different"
    assert venue_diff_fields(a, b) == ["venue_name"]
    assert venue_diff_fields(a, _full_venue()) == []


def test_redis_vs_rds_serving_diff_passes_after_projection():
    store = InMemoryRdsVenueStore()
    store.upsert_venue(_full_venue("a"))
    store.upsert_venue(_full_venue("b"))
    dao = _fresh_dao()
    RedisProjectionService(dao, store).rebuild_redis_from_rds()  # v2 projection
    result = redis_vs_rds_serving_diff(store, dao)
    assert result.checked == 2
    assert result.passing


def test_redis_vs_rds_serving_diff_flags_unprojected_venue():
    store = InMemoryRdsVenueStore()
    store.upsert_venue(_full_venue("a"))
    result = redis_vs_rds_serving_diff(store, _fresh_dao())  # nothing projected
    assert not result.passing
    assert result.mismatches == [{"venue_id": "a", "reason": "missing_in_redis"}]
