"""Tests for admin venue inventory response shaping."""
import importlib
from types import SimpleNamespace

from app.models import Venue
from app.models.instagram import VenueInstagram

admin_trigger_router = importlib.import_module("app.routers.admin_trigger_router")
_venue_instagram_item = admin_trigger_router._venue_instagram_item


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

    def get_venue_reviews_deep_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}

    def get_venue_menu_photos_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}

    def get_venue_menu_data_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}

    def get_venue_vibe_profile_bulk(self, venue_ids):
        return {vid: object() for vid in venue_ids if vid == "closed"}


def test_admin_inventory_lists_deprecated_with_cache_flags():
    dao = _InventoryDao()
    admin_trigger_router.set_container(SimpleNamespace(pipeline_repository=dao))

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
    # cache_flags.instagram keeps its exact membership-only meaning: "closed"
    # IS in the fake's ig_map (even though the value is a bare `object()`,
    # not a real VenueInstagram — see get_venue_instagram_bulk above).
    assert item["cache_flags"]["instagram"] is True
    # The projected `instagram` field must degrade to nulls rather than raise
    # or 500 the listing when the stored record isn't a real VenueInstagram.
    assert item["instagram"] == {
        "handle": None,
        "url": None,
        "status": None,
        "confidence": None,
        "source": None,
    }


def test_admin_inventory_reports_no_instagram_object_for_absent_record():
    dao = _InventoryDao()
    admin_trigger_router.set_container(SimpleNamespace(pipeline_repository=dao))

    response = admin_trigger_router.list_venue_inventory(
        status="active", q=None, limit=50, cursor=None,
    )

    item = response["items"][0]
    assert item["venue_id"] == "active"
    assert item["cache_flags"]["instagram"] is False
    assert item["instagram"] is None


# ── _venue_instagram_item: pure projection coverage ─────────────────────────


def test_venue_instagram_item_none_for_absent_record():
    assert _venue_instagram_item(None) is None


def test_venue_instagram_item_found_carries_full_shape():
    record = VenueInstagram(
        venue_id="v1",
        instagram_handle="champagne_recifee",
        instagram_url="https://instagram.com/champagne_recifee",
        confidence_score=0.78,
        status="found",
        source="venue_website",
    )
    assert _venue_instagram_item(record) == {
        "handle": "champagne_recifee",
        "url": "https://instagram.com/champagne_recifee",
        "status": "found",
        "confidence": 0.78,
        "source": "venue_website",
    }


def test_venue_instagram_item_low_confidence():
    record = VenueInstagram(
        venue_id="v2",
        instagram_handle="casaduvidosa",
        instagram_url="https://instagram.com/casaduvidosa",
        confidence_score=0.55,
        status="low_confidence",
        source="google_search",
    )
    item = _venue_instagram_item(record)
    assert item["status"] == "low_confidence"
    assert item["handle"] == "casaduvidosa"


def test_venue_instagram_item_not_found_returns_null_handle_not_none():
    """A `not_found` record must return the object (distinguishable from
    "nobody looked"), just with a null handle."""
    record = VenueInstagram(venue_id="v3", status="not_found")
    item = _venue_instagram_item(record)
    assert item is not None
    assert item["handle"] is None
    assert item["status"] == "not_found"


def test_venue_instagram_item_missing_source_degrades_to_null_source():
    """Records predating the `source` field exist in production; `source`
    must be null, not a raise."""
    record = VenueInstagram(
        venue_id="v4",
        instagram_handle="barantigo",
        instagram_url="https://instagram.com/barantigo",
        confidence_score=0.6,
        status="found",
        source=None,
    )
    item = _venue_instagram_item(record)
    assert item["handle"] == "barantigo"
    assert item["source"] is None


def test_venue_instagram_item_missing_confidence_score_defaults_to_zero():
    """`confidence_score` has a pydantic default of 0.0, so a record missing
    it at parse time already produces 0.0 rather than raising; this pins that
    projected value stays consistent through the projection helper."""
    record = VenueInstagram(venue_id="v5", instagram_handle="x", status="found")
    item = _venue_instagram_item(record)
    assert item["confidence"] == 0.0


def test_venue_instagram_item_malformed_record_degrades_without_raising():
    """A record that isn't even a VenueInstagram instance (e.g. a stray
    object making it into the bulk-read map) must degrade to an all-null
    object rather than raise and take the whole listing down with it."""
    item = _venue_instagram_item(object())
    assert item == {
        "handle": None,
        "url": None,
        "status": None,
        "confidence": None,
        "source": None,
    }
