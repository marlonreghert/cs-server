"""Unit tests for plans/260710_projector-and-serving-bulk-reads.md (P1-P5).

BDD (tests/bdd/persistence/projector-and-serving-bulk-reads.feature) covers the
end-to-end projection/nearby equivalence and bounded-query/round-trip behavior.
This file covers the lower-level pieces the BDD scenarios don't reach directly:

- P2: RedisVenueDAO's bulk MGET getters — parse parity with the single-item
  getters, and per-item error tolerance (one corrupt JSON skips only that
  item, never the whole request).
- P3: update_data_quality_metrics — same gauge values as the old per-venue
  loop for mixed live/weekly presence, computed via exactly 2 bulk (P2) DAO
  calls (not one GET per venue).
- P4: the converted admin/engagement handlers are plain functions (locks the
  threadpool-eligibility property the BDD event-loop scenario exercises
  behaviorally).
- P5: GeoRedisClient.keys() never issues KEYS (SCAN only), with the same
  return contract (a de-duplicated list of matching key strings) as KEYS.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect

import fakeredis
import pytest

from app.dao.redis_venue_dao import RedisVenueDAO
from app.db.geo_redis_client import GeoRedisClient
from app.models import Analysis, LiveForecastResponse, VenueInfo, WeekRawDay
from app.models.opening_hours import OpeningHours
from app.models.vibe_attributes import VibeAttributes
from app.services.venues_refresher_service import VenuesRefresherService

# app/routers/__init__.py re-exports the router INSTANCES under these same
# names (`from app.routers.admin_trigger_router import router as
# admin_trigger_router`), shadowing the submodules on the `app.routers`
# package namespace — importlib.import_module goes straight to sys.modules
# and sidesteps that, matching the pattern other router-module tests use.
admin_trigger_router = importlib.import_module("app.routers.admin_trigger_router")
engagement_router = importlib.import_module("app.routers.engagement_router")


def _dao() -> RedisVenueDAO:
    return RedisVenueDAO(GeoRedisClient(fakeredis.FakeRedis(decode_responses=True)))


# ── P2: bulk MGET getters ───────────────────────────────────────────────────
class TestBulkRedisGetters:
    def test_live_forecasts_bulk_matches_single_getter(self):
        dao = _dao()
        lf = LiveForecastResponse(
            status="OK", venue_info=VenueInfo(venue_id="v1"),
            analysis=Analysis(venue_live_busyness=42, venue_live_busyness_available=True),
        )
        dao.set_live_forecast(lf)

        bulk = dao.get_live_forecasts_bulk(["v1", "v2"])

        assert set(bulk) == {"v1"}  # v2 was never set -> absent, not KeyError/None entry
        assert bulk["v1"].model_dump(mode="json") == dao.get_live_forecast("v1").model_dump(mode="json")

    def test_week_raw_forecasts_bulk_matches_single_getter(self):
        dao = _dao()
        dao.set_week_raw_forecast("v1", WeekRawDay(day_int=3, day_raw=[10] * 24))
        dao.set_week_raw_forecast("v2", WeekRawDay(day_int=3, day_raw=[20] * 24))

        bulk = dao.get_week_raw_forecasts_bulk(["v1", "v2", "v3"], 3)

        assert set(bulk) == {"v1", "v2"}
        assert bulk["v1"].day_raw == [10] * 24
        assert bulk["v2"].day_raw == [20] * 24

    def test_week_raw_forecasts_bulk_is_day_scoped(self):
        """A venue's day-0 forecast must not leak into a day-3 bulk fetch."""
        dao = _dao()
        dao.set_week_raw_forecast("v1", WeekRawDay(day_int=0, day_raw=[1] * 24))

        assert dao.get_week_raw_forecasts_bulk(["v1"], 3) == {}
        assert set(dao.get_week_raw_forecasts_bulk(["v1"], 0)) == {"v1"}

    def test_vibe_attributes_bulk_matches_single_getter(self):
        dao = _dao()
        dao.set_vibe_attributes(VibeAttributes(venue_id="v1", google_primary_type="bar"))

        bulk = dao.get_vibe_attributes_bulk(["v1", "v2"])

        assert set(bulk) == {"v1"}
        assert bulk["v1"].google_primary_type == "bar"

    def test_opening_hours_bulk_matches_single_getter(self):
        dao = _dao()
        dao.set_opening_hours(OpeningHours(venue_id="v1", weekday_descriptions=["Seg: 18-02"]))

        bulk = dao.get_opening_hours_bulk(["v1", "v2"])

        assert set(bulk) == {"v1"}
        assert bulk["v1"].weekday_descriptions == ["Seg: 18-02"]

    def test_venue_photos_bulk_normalizes_legacy_format(self):
        """The bulk photos getter must apply the same legacy bare-URL-string-
        list normalization as get_venue_photos."""
        dao = _dao()
        dao.set_venue_photos("v1", [{"url": "https://p/1.jpg", "author_name": "A"}])
        # Legacy format: a bare list of URL strings (pre-dates the {url,
        # author_name} dict shape).
        dao.client.set("venue_photos_v1:v2", '["https://legacy/1.jpg"]')

        bulk = dao.get_venue_photos_bulk(["v1", "v2", "v3"])

        assert bulk["v1"] == [{"url": "https://p/1.jpg", "author_name": "A"}]
        assert bulk["v2"] == [{"url": "https://legacy/1.jpg", "author_name": None}]
        assert "v3" not in bulk

    def test_bulk_getter_per_item_error_tolerance(self):
        """One corrupt JSON value must be skipped, not fail the whole MGET
        request (mirrors the single getter's per-key try/except)."""
        dao = _dao()
        dao.set_vibe_attributes(VibeAttributes(venue_id="v1", google_primary_type="bar"))
        dao.client.set("vibe_attributes_v1:v2", "{not valid json")

        bulk = dao.get_vibe_attributes_bulk(["v1", "v2"])

        assert set(bulk) == {"v1"}

    def test_bulk_getter_empty_input_short_circuits(self):
        dao = _dao()
        assert dao.get_live_forecasts_bulk([]) == {}
        assert dao.get_vibe_attributes_bulk([]) == {}


# ── P3: update_data_quality_metrics via 2 bulk calls ────────────────────────
class TestDataQualityMetricsBulk:
    def test_same_gauge_values_as_per_venue_loop_would_produce(self):
        from app.metrics import VENUES_WITH_LIVE_FORECAST, VENUES_WITH_WEEKLY_FORECAST
        from app.models import Venue

        dao = _dao()
        venues = [
            Venue(venue_id=f"v{i}", venue_name=f"Bar {i}", venue_address="a",
                  venue_lat=-8.0, venue_lng=-34.9, venue_type="BAR")
            for i in range(4)
        ]
        for v in venues:
            dao.upsert_venue(v)

        # v0: live only. v1: weekly (Monday) only. v2: both. v3: neither.
        dao.set_live_forecast(LiveForecastResponse(
            status="OK", venue_info=VenueInfo(venue_id="v0"),
            analysis=Analysis(venue_live_busyness=1, venue_live_busyness_available=True)))
        dao.set_week_raw_forecast("v1", WeekRawDay(day_int=0, day_raw=[1] * 24))
        dao.set_live_forecast(LiveForecastResponse(
            status="OK", venue_info=VenueInfo(venue_id="v2"),
            analysis=Analysis(venue_live_busyness=1, venue_live_busyness_available=True)))
        dao.set_week_raw_forecast("v2", WeekRawDay(day_int=0, day_raw=[1] * 24))
        # v2 also has a Tuesday (day 1) forecast — must NOT count for the
        # Monday-only gauge (distinct from P4's cache-flags "any day" semantic).
        dao.set_week_raw_forecast("v2", WeekRawDay(day_int=1, day_raw=[1] * 24))
        # v3: weekly present only on a non-Monday day -> must not count.
        dao.set_week_raw_forecast("v3", WeekRawDay(day_int=2, day_raw=[1] * 24))

        service = VenuesRefresherService(dao, besttime_api=object())
        service.update_data_quality_metrics()

        assert VENUES_WITH_LIVE_FORECAST._value.get() == 2  # v0, v2
        assert VENUES_WITH_WEEKLY_FORECAST._value.get() == 2  # v1, v2 (Monday only)

    def test_computed_via_exactly_two_bulk_calls(self, monkeypatch):
        from app.models import Venue

        dao = _dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="Bar", venue_address="a",
                                venue_lat=-8.0, venue_lng=-34.9, venue_type="BAR"))

        calls = {"live": 0, "weekly": 0}
        orig_live = dao.get_live_forecasts_bulk
        orig_weekly = dao.get_week_raw_forecasts_bulk

        def counted_live(ids):
            calls["live"] += 1
            return orig_live(ids)

        def counted_weekly(ids, day_int):
            calls["weekly"] += 1
            return orig_weekly(ids, day_int)

        monkeypatch.setattr(dao, "get_live_forecasts_bulk", counted_live)
        monkeypatch.setattr(dao, "get_week_raw_forecasts_bulk", counted_weekly)

        service = VenuesRefresherService(dao, besttime_api=object())
        service.update_data_quality_metrics()

        assert calls == {"live": 1, "weekly": 1}  # 2 bulk queries total


# ── P4: converted handlers are plain functions ──────────────────────────────
class TestHandlersAreThreadpoolEligible:
    @pytest.mark.parametrize("fn_name", ["list_venue_inventory", "venue_type_breakdown"])
    def test_admin_handler_is_plain_def(self, fn_name):
        fn = getattr(admin_trigger_router, fn_name)
        assert not asyncio.iscoroutinefunction(fn), (
            f"{fn_name} must be a plain `def` so FastAPI runs it in the "
            f"threadpool instead of blocking the event loop"
        )
        assert not inspect.iscoroutinefunction(fn)

    @pytest.mark.parametrize(
        "fn_name",
        ["add_favorite", "remove_favorite", "add_hot_like", "record_session", "remove_hot_like"],
    )
    def test_engagement_handler_is_plain_def(self, fn_name):
        fn = getattr(engagement_router, fn_name)
        assert not asyncio.iscoroutinefunction(fn), (
            f"{fn_name} must be a plain `def` so FastAPI runs it in the "
            f"threadpool instead of blocking the event loop"
        )


# ── P5: GeoRedisClient.keys uses SCAN, not KEYS ─────────────────────────────
class TestGeoRedisClientScan:
    def test_keys_result_matches_keys_command_contract(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set("venue_photos_v1:a", "x")
        fake.set("venue_photos_v1:b", "y")
        fake.set("other_key", "z")
        client = GeoRedisClient(fake)

        result = client.keys("venue_photos_v1:*")

        assert sorted(result) == sorted(fake.keys("venue_photos_v1:*"))
        assert "other_key" not in result

    def test_keys_never_issues_the_keys_command(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.set("venue_photos_v1:a", "x")

        real_keys = fake.keys

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("GeoRedisClient.keys must not issue KEYS (SCAN only)")

        fake.keys = _fail_if_called
        try:
            client = GeoRedisClient(fake)
            result = client.keys("venue_photos_v1:*")
        finally:
            fake.keys = real_keys

        assert result == ["venue_photos_v1:a"]

    def test_keys_empty_pattern_match_returns_empty_list(self):
        fake = fakeredis.FakeRedis(decode_responses=True)
        client = GeoRedisClient(fake)
        assert client.keys("nonexistent_pattern_*") == []


# ── P5: list_all_venues / count_venues_with_instagram use MGET ─────────────
class TestBulkListAndCount:
    def test_list_all_venues_mgets_scanned_keys(self):
        from app.models import Venue

        dao = _dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="Bar", venue_address="a",
                                venue_lat=-8.0, venue_lng=-34.9, venue_type="BAR"))
        dao.upsert_venue(Venue(venue_id="v2", venue_name="Bar2", venue_address="a",
                                venue_lat=-8.01, venue_lng=-34.91, venue_type="BAR"))

        venues = dao.list_all_venues()

        assert {v.venue_id for v in venues} == {"v1", "v2"}

    def test_list_all_venues_skips_one_corrupt_entry(self):
        from app.models import Venue

        dao = _dao()
        dao.upsert_venue(Venue(venue_id="v1", venue_name="Bar", venue_address="a",
                                venue_lat=-8.0, venue_lng=-34.9, venue_type="BAR"))
        dao.client.client.geoadd("venues_geo_v1", (-34.9, -8.0, "venues_geo_place_v1:v2"))
        dao.client.set("venues_geo_place_v1:v2", "{not valid json")

        venues = dao.list_all_venues()

        assert {v.venue_id for v in venues} == {"v1"}

    def test_count_venues_with_instagram_matches_manual_count(self):
        from app.models.instagram import VenueInstagram

        dao = _dao()
        dao.set_venue_instagram(VenueInstagram(
            venue_id="v1", instagram_handle="h1", instagram_url="https://ig/h1",
            status="found", confidence_score=0.9,
        ))
        dao.set_venue_instagram(VenueInstagram(
            venue_id="v2", instagram_handle=None, instagram_url=None,
            status="not_found", confidence_score=0.0,
        ))

        assert dao.count_venues_with_instagram() == 1
