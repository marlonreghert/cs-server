"""Unit tests for the schema-normalization equivalence harness."""
import fakeredis

from app.dao.redis_venue_dao import RedisVenueDAO
from app.db.geo_redis_client import GeoRedisClient
from app.models import Venue
from app.models.venue import FootTrafficForecast
from app.services.equivalence_verify import (
    canonical_venue,
    rds_venue_golden_diff,
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


def test_golden_diff_passes_when_columns_match_payload():
    store = InMemoryRdsVenueStore()
    store.upsert_venue(_full_venue("a"))
    store.upsert_venue(_full_venue("b"))
    result = rds_venue_golden_diff(store)
    assert result.checked == 2
    assert result.passing
    assert result.mismatch_count == 0


def test_golden_diff_detects_drift_and_leaks_no_values():
    store = InMemoryRdsVenueStore()
    store.upsert_venue(_full_venue("vd"))
    # Perturb only the retained v1 payload so it diverges from the v2 columns.
    store.venues["vd"]["payload"]["venue_name"] = "SECRET DRIFT"
    result = rds_venue_golden_diff(store)
    assert not result.passing
    [mismatch] = result.mismatches
    assert mismatch == {"venue_id": "vd", "fields": ["venue_name"]}
    assert "SECRET DRIFT" not in str(result.mismatches)


def test_golden_diff_ignores_column_authoritative_drift():
    # priority + lifecycle are column-managed; a stale payload value for them is
    # expected (not data loss) and must NOT fail the gate. Confirmed against prod.
    store = InMemoryRdsVenueStore()
    store.upsert_venue(_full_venue("p"))
    payload = store.venues["p"]["payload"]
    payload["priority"] = 0                 # column has 4; payload stale
    payload["lifecycle_status"] = "active"  # column may say deprecated
    payload["deprecated_reason"] = "stale"
    assert rds_venue_golden_diff(store).passing


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
