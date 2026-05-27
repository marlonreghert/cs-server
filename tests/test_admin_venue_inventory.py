"""Tests for admin venue inventory response shaping."""
import importlib
from types import SimpleNamespace

import pytest

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

    def get_live_forecast(self, venue_id):
        return object() if venue_id == "closed" else None

    def get_week_raw_forecast(self, venue_id, day_int):
        return object() if venue_id == "closed" and day_int == 0 else None

    def get_vibe_attributes(self, venue_id):
        return object() if venue_id == "closed" else None

    def get_venue_photos(self, venue_id):
        return [{"url": "x"}] if venue_id == "closed" else None

    def get_opening_hours(self, venue_id):
        return object() if venue_id == "closed" else None

    def get_venue_instagram(self, venue_id):
        return object() if venue_id == "closed" else None

    def get_venue_reviews(self, venue_id):
        return object() if venue_id == "closed" else None

    def get_venue_menu_photos(self, venue_id):
        return object() if venue_id == "closed" else None

    def get_venue_menu_data(self, venue_id):
        return object() if venue_id == "closed" else None

    def get_venue_vibe_profile(self, venue_id):
        return object() if venue_id == "closed" else None


@pytest.mark.asyncio
async def test_admin_inventory_lists_deprecated_with_cache_flags():
    dao = _InventoryDao()
    admin_trigger_router.set_container(SimpleNamespace(venue_dao=dao))

    response = await admin_trigger_router.list_venue_inventory(
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
