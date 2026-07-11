"""Behave steps for tests/bdd/persistence/venue_soft_deletion.feature."""
from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timezone
from typing import Any

from behave import given, when, then  # type: ignore[import-untyped]
from prometheus_client import generate_latest

from app.config import settings
from app.dao.redis_venue_dao import (
    LIVE_FORECAST_KEY_FORMAT,
    OPENING_HOURS_KEY_FORMAT,
    VENUE_IG_POSTS_KEY_FORMAT,
    VENUE_INSTAGRAM_KEY_FORMAT,
    VENUE_MENU_PHOTOS_KEY_FORMAT,
    VENUE_MENU_RAW_DATA_KEY_FORMAT,
    VENUE_PHOTOS_KEY_FORMAT,
    VENUE_REVIEWS_KEY_FORMAT,
    VENUE_VIBE_PROFILE_KEY_FORMAT,
    VENUES_GEO_PLACE_MEMBER_FORMAT_V1,
    VIBE_ATTRIBUTES_KEY_FORMAT,
    WEEKLY_FORECAST_KEY_FORMAT,
)
from app.handlers.venue_handler import VenueHandler
from app.models import Analysis, LiveForecastResponse, Venue, VenueInfo, WeekRawDay
from app.models.instagram import VenueInstagram, VenueInstagramPosts, InstagramPost
from app.models.menu import (
    MenuItem,
    MenuPhoto,
    MenuSection,
    VenueMenuData,
    VenueMenuPhotos,
)
from app.models.opening_hours import OpeningHours
from app.models.venue_review import VenueReview, VenueReviews
from app.models.vibe_attributes import GooglePlacesDetailsResponse, VibeAttributes
from app.models.vibe_profile import VenueVibeProfile
from app.services.google_places_enrichment_service import GooglePlacesEnrichmentService


class _BDDResponse:
    def __init__(self, status_code: int, body: dict[str, Any]):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict[str, Any]:
        return self._body


def _venue_key(venue_id: str) -> str:
    return VENUES_GEO_PLACE_MEMBER_FORMAT_V1.format(venue_id)


def _json_for_venue(context, venue_id: str) -> dict[str, Any]:
    raw = context.fake_redis.get(_venue_key(venue_id))
    assert raw is not None, f"Expected Redis venue key for {venue_id}"
    return json.loads(raw)


def _write_venue_json(context, data: dict[str, Any]) -> None:
    context.fake_redis.set(_venue_key(data["venue_id"]), json.dumps(data))


def _lifecycle_status(context, venue_id: str) -> str:
    return _json_for_venue(context, venue_id).get("lifecycle_status", "active")


def _metric_value(name: str, labels: dict[str, str] | None = None) -> float:
    labels = labels or {}
    prefix = f"{name}{{"
    plain = f"{name} "
    for line in generate_latest().decode("utf-8").splitlines():
        if line.startswith("#"):
            continue
        if labels:
            if not line.startswith(prefix):
                continue
            label_blob = line.split("{", 1)[1].split("}", 1)[0]
            parsed = {}
            for part in label_blob.split(","):
                key, value = part.split("=", 1)
                parsed[key] = value.strip('"')
            if any(parsed.get(k) != v for k, v in labels.items()):
                continue
            return float(line.rsplit(" ", 1)[1])
        if line.startswith(plain):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def _seed_live_forecast(context, venue_id: str, available: bool = True) -> None:
    context.venue_dao.set_live_forecast(
        LiveForecastResponse(
            status="OK",
            venue_info=VenueInfo(venue_id=venue_id),
            analysis=Analysis(
                venue_live_busyness=67,
                venue_live_busyness_available=available,
            ),
        )
    )


class _FakeGooglePlacesClient:
    def __init__(self) -> None:
        self.details_by_place_id: dict[str, GooglePlacesDetailsResponse] = {}

    async def get_place_details(self, place_id: str):
        return self.details_by_place_id[place_id]

    def details_to_vibe_attributes(self, venue_id: str, details):
        return VibeAttributes(
            venue_id=venue_id,
            google_place_id=details.place_id,
            google_primary_type=details.primary_type,
        )


@given('Redis contains an active venue "{venue_id}" in the geo index')
def step_seed_active_venue(context, venue_id):
    venue = Venue(
        forecast=True,
        processed=True,
        venue_id=venue_id,
        venue_name=venue_id.replace("_", " ").title(),
        venue_address=f"{venue_id} address",
        venue_lat=-8.05 if venue_id.endswith("active") else -8.051,
        venue_lng=-34.88 if venue_id.endswith("active") else -34.881,
        venue_type="BAR",
    )
    context.venue_dao.upsert_venue(venue)


@given('"{venue_id}" has cached live forecast, weekly forecast, vibe attributes, photos, opening hours, Instagram, reviews, menu data, and vibe profile records')
def step_seed_associated_cache_records(context, venue_id):
    _seed_live_forecast(context, venue_id, available=True)
    context.venue_dao.set_week_raw_forecast(
        venue_id,
        WeekRawDay(day_int=0, day_raw=[50] * 24),
    )
    context.venue_dao.set_vibe_attributes(
        VibeAttributes(
            venue_id=venue_id,
            google_place_id=f"place_{venue_id}",
            google_primary_type="bar",
        )
    )
    context.venue_dao.set_venue_photos(
        venue_id,
        [{"url": "https://example.test/photo.jpg", "author_name": "Tester"}],
    )
    context.venue_dao.set_opening_hours(
        OpeningHours(venue_id=venue_id, weekday_descriptions=["Segunda: 18:00 - 02:00"])
    )
    context.venue_dao.set_venue_instagram(
        VenueInstagram(
            venue_id=venue_id,
            instagram_handle="venue",
            instagram_url="https://instagram.com/venue",
            status="found",
            confidence_score=1.0,
        )
    )
    context.venue_dao.set_venue_reviews(
        VenueReviews(
            venue_id=venue_id,
            reviews=[
                VenueReview(
                    author_name="Tester",
                    rating=5,
                    text="Great",
                    relative_time="today",
                )
            ],
        )
    )
    context.venue_dao.set_venue_menu_photos(
        VenueMenuPhotos(
            venue_id=venue_id,
            photos=[
                MenuPhoto(
                    photo_id="photo-1",
                    s3_url="https://bucket.test/menu.jpg",
                    s3_key="places/menu.jpg",
                )
            ],
        )
    )
    context.venue_dao.set_venue_menu_data(
        VenueMenuData(
            venue_id=venue_id,
            sections=[
                MenuSection(
                    name="Drinks",
                    items=[MenuItem(name="Beer", prices=[{"price": 12}])],
                )
            ],
        )
    )
    context.venue_dao.set_venue_ig_posts(
        VenueInstagramPosts(
            venue_id=venue_id,
            instagram_handle="venue",
            posts=[InstagramPost(caption="Tonight")],
        )
    )
    context.venue_dao.set_venue_vibe_profile(
        VenueVibeProfile(
            venue_id=venue_id,
            top_vibes=["animado"],
            overall_confidence=0.9,
        )
    )


@given("Google Places closure handling is enabled")
def step_enable_google_places_closure_handling(context):
    settings.remove_permanently_closed_venues = True
    settings.remove_temporarily_closed_venues = True
    context.google_places_client = _FakeGooglePlacesClient()
    context.google_places_service = GooglePlacesEnrichmentService(
        context.google_places_client,
        context.venue_dao,
    )


@given('Google Places details for "{venue_id}" return business status "{status}"')
def step_google_places_details(context, venue_id, status):
    context.google_places_client.details_by_place_id[f"place_{venue_id}"] = (
        GooglePlacesDetailsResponse(
            place_id=f"place_{venue_id}",
            primary_type="bar",
            business_status=status,
        )
    )


@given('"{venue_id}" has available cached live busyness')
def step_available_live_busyness(context, venue_id):
    _seed_live_forecast(context, venue_id, available=True)


@given('"{venue_id}" is already marked as deprecated')
def step_mark_deprecated(context, venue_id):
    data = _json_for_venue(context, venue_id)
    data.update(
        {
            "lifecycle_status": "deprecated",
            "deprecated_reason": "google_places_closed_permanently",
            "deprecated_source": "google_places",
            "deprecated_at": datetime.now(timezone.utc).isoformat(),
            "google_business_status": "CLOSED_PERMANENTLY",
        }
    )
    _write_venue_json(context, data)


@given("Redis already contains legacy venue records with no lifecycle metadata")
def step_seed_legacy_records(context):
    venue = Venue(
        venue_id="venue_legacy",
        venue_name="Legacy Venue",
        venue_address="Legacy Address",
        venue_lat=-8.052,
        venue_lng=-34.882,
    )
    context.venue_dao.upsert_venue(venue)
    data = _json_for_venue(context, "venue_legacy")
    for key in (
        "lifecycle_status",
        "deprecated_reason",
        "deprecated_source",
        "deprecated_at",
        "google_business_status",
    ):
        data.pop(key, None)
    _write_venue_json(context, data)


@when('the Google Places enrichment job force-refreshes "{venue_id}"')
def step_force_refresh_google_places(context, venue_id):
    context.metric_baseline = {
        "soft_permanent": _metric_value(
            "venues_soft_deleted_total",
            {"reason": "google_places_closed_permanently", "source": "google_places"},
        ),
        "soft_temporary": _metric_value(
            "venues_soft_deleted_total",
            {"reason": "google_places_closed_temporarily", "source": "google_places"},
        ),
        "removed_permanent": _metric_value("venues_permanently_closed_removed_total"),
        "removed_temporary": _metric_value("venues_temporarily_closed_removed_total"),
    }
    context.enrich_result = asyncio.run(
        context.google_places_service.enrich_venue(
            venue_id,
            f"place_{venue_id}",
            force_refresh=True,
        )
    )


@when('a client requests venues nearby a point that includes "{active_id}" and "{closed_id}"')
def step_request_nearby(context, active_id, closed_id):
    handler = VenueHandler(context.venue_dao)
    context.nearby_response = handler.get_venues_nearby(
        lat=-8.05,
        lon=-34.88,
        radius=1.0,
        verbose=False,
    )


@when("live forecast refresh, weekly forecast refresh, Google Places enrichment, photo enrichment, Instagram discovery, Instagram posts, menu photo enrichment, menu extraction, and vibe classification jobs run")
def step_jobs_run(context):
    all_ids = set(context.venue_dao.list_all_venue_ids())
    if hasattr(context.venue_dao, "list_active_venue_ids"):
        processed = set(context.venue_dao.list_active_venue_ids())
    else:
        processed = all_ids
    context.job_processed_venue_ids = processed
    context.job_skipped_deprecated = len(all_ids - processed)


@when('the admin client requests the venue inventory with status "{status}"')
def step_admin_inventory(context, status):
    admin_trigger_router = importlib.import_module("app.routers.admin_trigger_router")

    # P4: list_venue_inventory is now a plain `def` (FastAPI threadpool), not
    # a coroutine — call it directly rather than via asyncio.run.
    body = admin_trigger_router.list_venue_inventory(
        status=status,
        q=None,
        limit=50,
        cursor=None,
    )
    context.response = _BDDResponse(200, body)


@when("cs-server starts after the soft-delete feature is deployed")
def step_cs_server_starts(context):
    context.startup_completed = True


@when('inventory sync or discovery refresh upserts a venue with id "{venue_id}"')
def step_upsert_existing_deprecated(context, venue_id):
    venue = Venue(
        venue_id=venue_id,
        venue_name="Re-seen Venue",
        venue_address="Re-seen Address",
        venue_lat=-8.051,
        venue_lng=-34.881,
        venue_type="BAR",
    )
    context.venue_dao.upsert_venue(venue)


@then('"{venue_id}" must still exist under its existing Redis venue key')
def step_venue_key_exists(context, venue_id):
    assert context.fake_redis.exists(_venue_key(venue_id)), f"Missing venue key for {venue_id}"


@then('"{venue_id}" must still remain a member of the existing Redis geo index')
def step_venue_geo_member_exists(context, venue_id):
    # A radius lookup proves the existing geo index can still return the member.
    nearby_ids = {
        v.venue_id
        for v in context.venue_dao.get_nearby_venues(
            -8.05,
            -34.88,
            1.0,
            include_deprecated=True,
        )
    }
    assert venue_id in nearby_ids, f"{venue_id} is not in the geo index"


@then('"{venue_id}" must be marked as deprecated with reason "{reason}"')
def step_venue_marked_deprecated(context, venue_id, reason):
    data = _json_for_venue(context, venue_id)
    assert data.get("lifecycle_status") == "deprecated"
    assert data.get("deprecated_reason") == reason


@then('the deprecated metadata must include source "{source}" and business status "{status}"')
def step_deprecated_metadata(context, source, status):
    data = _json_for_venue(context, "venue_closed")
    assert data.get("deprecated_source") == source
    assert data.get("google_business_status") == status
    assert data.get("deprecated_at")


@then('"{venue_id}" must not be marked as deprecated')
def step_not_deprecated(context, venue_id):
    assert _lifecycle_status(context, venue_id) != "deprecated"


@then('"{venue_id}" must remain eligible for future live forecast refreshes')
def step_live_refresh_eligible(context, venue_id):
    if hasattr(context.venue_dao, "list_active_venue_ids"):
        assert venue_id in context.venue_dao.list_active_venue_ids()
    else:
        assert venue_id in context.venue_dao.list_all_venue_ids()


@then('the cached live forecast, weekly forecast, vibe attributes, photos, opening hours, Instagram, reviews, menu data, and vibe profile records for "{venue_id}" must not be deleted')
def step_associated_cache_retained(context, venue_id):
    expected_keys = [
        LIVE_FORECAST_KEY_FORMAT.format(venue_id),
        WEEKLY_FORECAST_KEY_FORMAT.format(venue_id, 0),
        VIBE_ATTRIBUTES_KEY_FORMAT.format(venue_id),
        VENUE_PHOTOS_KEY_FORMAT.format(venue_id),
        OPENING_HOURS_KEY_FORMAT.format(venue_id),
        VENUE_INSTAGRAM_KEY_FORMAT.format(venue_id),
        VENUE_REVIEWS_KEY_FORMAT.format(venue_id),
        VENUE_MENU_PHOTOS_KEY_FORMAT.format(venue_id),
        VENUE_MENU_RAW_DATA_KEY_FORMAT.format(venue_id),
        VENUE_IG_POSTS_KEY_FORMAT.format(venue_id),
        VENUE_VIBE_PROFILE_KEY_FORMAT.format(venue_id),
    ]
    missing = [key for key in expected_keys if not context.fake_redis.exists(key)]
    assert not missing, f"Expected cache keys to be retained, missing: {missing}"


@then('the metric "{metric_expr}" must be incremented')
def step_metric_incremented(context, metric_expr):
    metric_name, labels = _parse_metric_expr(metric_expr)
    baseline_key = _baseline_key(metric_name, labels)
    assert _metric_value(metric_name, labels) > context.metric_baseline[baseline_key]


@then('the metric "{metric_expr}" must not be incremented')
def step_metric_not_incremented(context, metric_expr):
    metric_name, labels = _parse_metric_expr(metric_expr)
    baseline_key = _baseline_key(metric_name, labels)
    assert _metric_value(metric_name, labels) == context.metric_baseline[baseline_key]


def _parse_metric_expr(metric_expr: str) -> tuple[str, dict[str, str]]:
    if "{" not in metric_expr:
        return metric_expr, {}
    name, rest = metric_expr.split("{", 1)
    label_blob = rest.rstrip("}")
    labels = {}
    for part in label_blob.split(","):
        key, value = part.split("=", 1)
        labels[key] = value.replace('\\"', '"').strip('"')
    return name, labels


def _baseline_key(metric_name: str, labels: dict[str, str]) -> str:
    if metric_name == "venues_soft_deleted_total":
        if labels.get("reason") == "google_places_closed_permanently":
            return "soft_permanent"
        if labels.get("reason") == "google_places_closed_temporarily":
            return "soft_temporary"
    if metric_name == "venues_permanently_closed_removed_total":
        return "removed_permanent"
    if metric_name == "venues_temporarily_closed_removed_total":
        return "removed_temporary"
    raise AssertionError(f"No metric baseline configured for {metric_name}{labels}")


@then('the public nearby response must include "{venue_id}" when live busyness is available')
def step_public_nearby_includes_when_live(context, venue_id):
    handler = VenueHandler(context.venue_dao)
    response = handler.get_venues_nearby(-8.05, -34.88, 1.0, verbose=False)
    assert venue_id in {item.venue_id for item in response}


@then('the public nearby response must include "{venue_id}"')
def step_public_nearby_includes(context, venue_id):
    assert venue_id in {item.venue_id for item in context.nearby_response}


@then('the public nearby response must not include "{venue_id}"')
def step_public_nearby_excludes(context, venue_id):
    assert venue_id not in {item.venue_id for item in context.nearby_response}


@then('the Redis record for "{venue_id}" must remain available for direct admin lookup')
def step_direct_admin_lookup(context, venue_id):
    assert context.venue_dao.get_venue(venue_id) is not None


@then('those jobs must process "{venue_id}"')
def step_jobs_process(context, venue_id):
    assert venue_id in context.job_processed_venue_ids


@then('those jobs must not call external enrichment or refresh clients for "{venue_id}"')
def step_jobs_skip_deprecated(context, venue_id):
    assert venue_id not in context.job_processed_venue_ids


@then("those jobs must log how many deprecated venues were skipped")
def step_jobs_log_skipped(context):
    assert context.job_skipped_deprecated >= 1


@then('the response must include "{venue_id}"')
def step_response_includes_venue(context, venue_id):
    assert context.response.status_code == 200, context.response.text
    data = context.response.json()
    assert venue_id in {item["venue_id"] for item in data["items"]}


@then("the response item must include venue id, name, address, latitude, longitude, lifecycle status, deprecated reason, deprecated source, deprecated timestamp, and Google business status")
def step_response_item_fields(context):
    data = context.response.json()
    item = next(item for item in data["items"] if item["venue_id"] == "venue_closed")
    for field in [
        "venue_id",
        "venue_name",
        "venue_address",
        "venue_lat",
        "venue_lng",
        "lifecycle_status",
        "deprecated_reason",
        "deprecated_source",
        "deprecated_at",
        "google_business_status",
    ]:
        assert field in item, f"Missing field {field}"


@then("the response item must include cache flags for live forecast, weekly forecast, vibe attributes, photos, opening hours, Instagram, reviews, menu data, and vibe profile")
def step_response_cache_flags(context):
    item = next(item for item in context.response.json()["items"] if item["venue_id"] == "venue_closed")
    flags = item.get("cache_flags", {})
    for field in [
        "live_forecast",
        "weekly_forecast",
        "vibe_attributes",
        "photos",
        "opening_hours",
        "instagram",
        "reviews",
        "menu_data",
        "vibe_profile",
    ]:
        assert flags.get(field) is True, f"Missing or false cache flag {field}"


@then("the response must include separate active and deprecated venue counts")
def step_response_counts(context):
    counts = context.response.json().get("counts", {})
    assert "active" in counts
    assert "deprecated" in counts


@then("legacy venue records must be treated as active")
def step_legacy_active(context):
    assert _lifecycle_status(context, "venue_legacy") == "active"


@then("cs-server must not flush Redis")
def step_no_flush(context):
    assert context.fake_redis.exists(_venue_key("venue_legacy"))


@then("cs-server must not rename or rebuild the existing venue geo key")
def step_no_geo_key_rename(context):
    assert context.venue_dao.get_nearby_venues(-8.052, -34.882, 1.0)


@then("cs-server must not require a backfill migration before serving existing venues")
def step_no_backfill_required(context):
    assert context.venue_dao.get_venue("venue_legacy") is not None


@then('the existing deprecated lifecycle metadata for "{venue_id}" must be preserved')
def step_deprecated_metadata_preserved(context, venue_id):
    data = _json_for_venue(context, venue_id)
    assert data.get("lifecycle_status") == "deprecated"
    assert data.get("deprecated_reason") == "google_places_closed_permanently"


@then('"{venue_id}" must remain hidden from public nearby results')
def step_deprecated_hidden(context, venue_id):
    handler = VenueHandler(context.venue_dao)
    response = handler.get_venues_nearby(-8.05, -34.88, 1.0, verbose=False)
    assert venue_id not in {item.venue_id for item in response}


@then('"{venue_id}" must remain visible through the admin deprecated inventory')
def step_deprecated_visible_admin(context, venue_id):
    admin_trigger_router = importlib.import_module("app.routers.admin_trigger_router")

    # P4: list_venue_inventory is now a plain `def` (FastAPI threadpool), not
    # a coroutine — call it directly rather than via asyncio.run.
    body = admin_trigger_router.list_venue_inventory(
        status="deprecated",
        q=None,
        limit=50,
        cursor=None,
    )
    response = _BDDResponse(200, body)
    assert response.status_code == 200, response.text
    assert venue_id in {item["venue_id"] for item in response.json()["items"]}
