"""Tests for admin venue inventory response shaping."""
import importlib
from types import SimpleNamespace

from app.models import Venue

admin_trigger_router = importlib.import_module("app.routers.admin_trigger_router")


class _InventoryDao:
    def __init__(self):
        self.active = Venue(
            venue_id="active",
            venue_name="Active Venue",
            venue_address="Active Address",
            venue_lat=-8.0,
            venue_lng=-34.9,
        )
        self.deprecated = Venue(
            venue_id="closed",
            venue_name="Closed Venue",
            venue_address="Closed Address",
            venue_lat=-8.01,
            venue_lng=-34.91,
            lifecycle_status="deprecated",
            deprecated_reason="google_places_closed_permanently",
            deprecated_source="google_places",
            google_business_status="CLOSED_PERMANENTLY",
        )

    def list_all_venues(self):
        return [self.active, self.deprecated]

    # Bulk (P4) methods: list_venue_inventory computes cache flags from a
    # single bulk presence lookup per key family for the whole page, so the
    # test double implements the bulk shape directly (dict keyed by the ids
    # that are "present"), matching RedisVenueDAO's real bulk getters.
    def get_live_forecasts_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}

    def get_week_raw_forecasts_bulk(self, venue_ids, day_int):
        return {vid: object() for vid in venue_ids if vid == "closed" and day_int == 0}

    def get_vibe_attributes_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}

    def get_venue_photos_bulk(self, venue_ids):
        return {vid: [{"url": "x"}] for vid in venue_ids if vid == "closed"}

    def get_opening_hours_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}

    def get_venue_instagram_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}

    def get_venue_reviews_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}

    def get_venue_menu_photos_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}

    def get_venue_menu_data_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}

    def get_venue_vibe_profile_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}


def test_admin_inventory_lists_deprecated_with_cache_flags():
    dao = _InventoryDao()
    admin_trigger_router.set_container(SimpleNamespace(venue_dao=dao))

    # P4: list_venue_inventory is now a plain `def` (FastAPI threadpool), not
    # a coroutine — call it directly rather than awaiting it.
    response = admin_trigger_router.list_venue_inventory(
        status="deprecated",
        q=None,
        limit=50,
        cursor=None,
    )

    assert response["counts"]["active"] == 1
    assert response["counts"]["deprecated"] == 1
    assert response["next_cursor"] is None
    assert len(response["items"]) == 1
    item = response["items"][0]
    assert item["venue_id"] == "closed"
    assert item["lifecycle_status"] == "deprecated"
    assert item["deprecated_reason"] == "google_places_closed_permanently"
    assert item["google_business_status"] == "CLOSED_PERMANENTLY"
    assert item["cache_flags"]["live_forecast"] is True
    assert item["cache_flags"]["weekly_forecast"] is True
    assert item["cache_flags"]["menu_data"] is True
