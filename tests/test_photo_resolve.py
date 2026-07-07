"""Unit tests for on-demand venue photo resolution (fresh keyless CDN URLs).

Covers the critical internal logic behind
POST /internal/venues/{id}/photos/resolve:

- GooglePlacesAPIClient.get_place_photos: calls the media endpoint with
  skipHttpRedirect=true, returns the KEYLESS photoUri, keeps the API key in the
  X-Goog-Api-Key header (never the URL), caps at max_photos, skips a photo whose
  media call fails, and PROPAGATES a hard Place Details error.
- RedisVenueDAO fresh-cache: TTL resolver precedence/guard-rails and
  set/get round-trip + isolation from the legacy venue_photos_v1 key.
- PhotoEnrichmentService.resolve_and_cache_fresh_photos branches: happy path,
  no google_place_id, zero photos, and exception (never cached).
"""
import json

import fakeredis
import httpx
import pytest

from app.api.google_places_client import GooglePlacesAPIClient
from app.config import settings
from app.db.geo_redis_client import GeoRedisClient
from app.dao.redis_venue_dao import (
    RedisVenueDAO,
    VENUE_PHOTOS_FRESH_KEY_FORMAT,
    VENUE_PHOTOS_KEY_FORMAT,
    ADMIN_CONFIG_FRESH_PHOTOS_TTL_KEY,
)
from app.models.vibe_attributes import VibeAttributes
from app.services.photo_enrichment_service import PhotoEnrichmentService


# ── fixtures / helpers ────────────────────────────────────────────────────────
@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def dao(fake_redis):
    return RedisVenueDAO(GeoRedisClient(fake_redis))


def _client_with_handler(handler):
    client = GooglePlacesAPIClient(api_key="unit-key")
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return client


def _details_photos(n, *, author_at=None):
    """A Place Details 'photos' payload with n photos; author_at maps index->name."""
    photos = []
    for i in range(n):
        entry = {"name": f"places/PID/photos/p{i}"}
        if author_at and i in author_at:
            entry["authorAttributions"] = [{"displayName": author_at[i]}]
        photos.append(entry)
    return photos


class _FakeGoogle:
    """Minimal async stand-in for GooglePlacesAPIClient in service tests."""

    def __init__(self, photos=None, error=None):
        self._photos = photos or []
        self._error = error
        self.calls = 0
        self.last_kwargs = None

    async def get_place_photos(self, place_id, max_photos=5, max_width=800):
        self.calls += 1
        self.last_kwargs = {"place_id": place_id, "max_photos": max_photos, "max_width": max_width}
        if self._error is not None:
            raise self._error
        return list(self._photos)[:max_photos]


# ══════════════════════════════════════════════════════════════════════════════
# GooglePlacesAPIClient.get_place_photos — keyless mechanism
# ══════════════════════════════════════════════════════════════════════════════
async def test_get_place_photos_returns_keyless_uris_with_header_auth():
    seen = {"media_calls": 0, "details_has_key": None}

    def handler(request: httpx.Request) -> httpx.Response:
        # API key must be header-only on every request.
        assert request.headers.get("X-Goog-Api-Key") == "unit-key"
        assert "key" not in request.url.params
        if request.url.path.endswith("/media"):
            seen["media_calls"] += 1
            assert request.url.params.get("skipHttpRedirect") == "true"
            assert request.url.params.get("maxWidthPx") == "800"
            name = request.url.path[len("/v1/"):-len("/media")]
            return httpx.Response(200, json={"name": name, "photoUri": f"https://lh3.googleusercontent.com/{name.replace('/', '_')}"})
        return httpx.Response(200, json={"photos": _details_photos(3, author_at={0: "Ana"})})

    client = _client_with_handler(handler)
    result = await client.get_place_photos("places/PID", max_photos=5, max_width=800)

    assert len(result) == 3
    assert seen["media_calls"] == 3
    for item in result:
        assert item["url"].startswith("https://lh3.googleusercontent.com/")
        assert "key=" not in item["url"]
        assert "places.googleapis.com" not in item["url"]
    # First author attribution preserved; missing attribution -> None.
    assert result[0]["author_name"] == "Ana"
    assert result[1]["author_name"] is None


async def test_get_place_photos_caps_at_max_photos():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            name = request.url.path[len("/v1/"):-len("/media")]
            return httpx.Response(200, json={"photoUri": f"https://lh3.googleusercontent.com/{name.replace('/', '_')}"})
        return httpx.Response(200, json={"photos": _details_photos(8)})

    client = _client_with_handler(handler)
    result = await client.get_place_photos("places/PID", max_photos=5)
    assert len(result) == 5


async def test_get_place_photos_skips_photo_when_media_call_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            # The middle photo's media call fails; the venue must not fail.
            if request.url.path.endswith("p1/media"):
                return httpx.Response(500, json={"error": "boom"})
            name = request.url.path[len("/v1/"):-len("/media")]
            return httpx.Response(200, json={"photoUri": f"https://lh3.googleusercontent.com/{name.replace('/', '_')}"})
        return httpx.Response(200, json={"photos": _details_photos(3)})

    client = _client_with_handler(handler)
    result = await client.get_place_photos("places/PID", max_photos=5)
    assert len(result) == 2  # p1 skipped, p0 + p2 kept
    assert all("googleusercontent.com" in it["url"] for it in result)


async def test_get_place_photos_raises_on_place_details_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = _client_with_handler(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_place_photos("places/PID")


# ══════════════════════════════════════════════════════════════════════════════
# RedisVenueDAO — fresh TTL resolver + set/get round-trip + key isolation
# ══════════════════════════════════════════════════════════════════════════════
class TestFreshTtlResolver:
    def test_default_when_no_admin_override(self, dao):
        expected = settings.photo_fresh_cache_ttl_hours * 3600
        assert dao._resolve_fresh_photos_cache_ttl_seconds() == expected

    def test_admin_override_wins(self, dao, fake_redis):
        fake_redis.set(ADMIN_CONFIG_FRESH_PHOTOS_TTL_KEY, json.dumps(3))
        assert dao._resolve_fresh_photos_cache_ttl_seconds() == 3 * 3600

    def test_falls_back_on_invalid_override(self, dao, fake_redis):
        fake_redis.set(ADMIN_CONFIG_FRESH_PHOTOS_TTL_KEY, json.dumps("abc"))
        assert (
            dao._resolve_fresh_photos_cache_ttl_seconds()
            == settings.photo_fresh_cache_ttl_hours * 3600
        )

    def test_falls_back_on_non_positive_override(self, dao, fake_redis):
        fake_redis.set(ADMIN_CONFIG_FRESH_PHOTOS_TTL_KEY, json.dumps(0))
        assert (
            dao._resolve_fresh_photos_cache_ttl_seconds()
            == settings.photo_fresh_cache_ttl_hours * 3600
        )


class TestFreshCacheRoundTrip:
    PHOTOS = [{"url": "https://lh3.googleusercontent.com/x=w800", "author_name": "A"}]

    def test_set_get_round_trip(self, dao):
        dao.set_venue_photos_fresh("v1", self.PHOTOS)
        assert dao.get_venue_photos_fresh("v1") == self.PHOTOS

    def test_set_applies_finite_positive_ttl(self, dao, fake_redis):
        dao.set_venue_photos_fresh("v1", self.PHOTOS)
        ttl = fake_redis.ttl(VENUE_PHOTOS_FRESH_KEY_FORMAT.format("v1"))
        assert 0 < ttl <= settings.photo_fresh_cache_ttl_hours * 3600

    def test_empty_list_is_a_valid_cached_value(self, dao):
        dao.set_venue_photos_fresh("v1", [])
        assert dao.get_venue_photos_fresh("v1") == []

    def test_fresh_write_does_not_touch_legacy_key(self, dao, fake_redis):
        dao.set_venue_photos_fresh("v1", self.PHOTOS)
        assert fake_redis.get(VENUE_PHOTOS_KEY_FORMAT.format("v1")) is None
        assert dao.get_venue_photos("v1") is None

    def test_legacy_write_does_not_touch_fresh_key(self, dao, fake_redis):
        dao.set_venue_photos("v1", self.PHOTOS)
        assert fake_redis.get(VENUE_PHOTOS_FRESH_KEY_FORMAT.format("v1")) is None
        assert dao.get_venue_photos_fresh("v1") is None

    def test_get_returns_none_when_absent(self, dao):
        assert dao.get_venue_photos_fresh("missing") is None


# ══════════════════════════════════════════════════════════════════════════════
# PhotoEnrichmentService.resolve_and_cache_fresh_photos — branches
# ══════════════════════════════════════════════════════════════════════════════
def _service(dao, google):
    return PhotoEnrichmentService(google_places_client=google, venue_dao=dao)


def _seed_place_id(dao, venue_id, place_id="places/PID"):
    dao.set_vibe_attributes(VibeAttributes(venue_id=venue_id, google_place_id=place_id))


async def test_resolve_happy_path_caches_and_returns(dao):
    _seed_place_id(dao, "v1")
    photos = [
        {"url": "https://lh3.googleusercontent.com/a", "author_name": "Ana"},
        {"url": "https://lh3.googleusercontent.com/b", "author_name": None},
    ]
    google = _FakeGoogle(photos=photos)
    service = _service(dao, google)

    result = await service.resolve_and_cache_fresh_photos("v1")

    assert result == photos
    assert dao.get_venue_photos_fresh("v1") == photos
    # Resolution requests photos_per_venue at maxWidthPx=800.
    assert google.last_kwargs["max_photos"] == settings.photos_per_venue
    assert google.last_kwargs["max_width"] == 800


async def test_resolve_no_place_id_caches_empty_and_skips_google(dao):
    google = _FakeGoogle(photos=[{"url": "u", "author_name": None}])
    service = _service(dao, google)

    result = await service.resolve_and_cache_fresh_photos("no_pid")

    assert result == []
    assert google.calls == 0  # Google never hit without a place id
    assert dao.get_venue_photos_fresh("no_pid") == []  # deterministic empty cached


async def test_resolve_zero_photos_caches_empty_list(dao):
    _seed_place_id(dao, "v1")
    google = _FakeGoogle(photos=[])
    service = _service(dao, google)

    result = await service.resolve_and_cache_fresh_photos("v1")

    assert result == []
    assert dao.get_venue_photos_fresh("v1") == []


async def test_resolve_exception_returns_empty_without_caching(dao):
    _seed_place_id(dao, "v1")
    google = _FakeGoogle(error=httpx.HTTPStatusError("boom", request=None, response=None))
    service = _service(dao, google)

    result = await service.resolve_and_cache_fresh_photos("v1")

    assert result == []
    # Never cache on exception — a later retry must be able to succeed.
    assert dao.get_venue_photos_fresh("v1") is None


async def test_resolve_uses_serving_dao_fallback_for_place_id(fake_redis):
    # Primary DAO (system of record) has no vibe attributes; the Redis fallback
    # carries the google_place_id and drives resolution.
    primary = RedisVenueDAO(GeoRedisClient(fakeredis.FakeRedis(decode_responses=True)))
    serving = RedisVenueDAO(GeoRedisClient(fake_redis))
    _seed_place_id(serving, "v1", place_id="places/FALLBACK")
    google = _FakeGoogle(photos=[{"url": "https://lh3.googleusercontent.com/z", "author_name": None}])
    service = PhotoEnrichmentService(
        google_places_client=google, venue_dao=primary, serving_dao=serving
    )

    result = await service.resolve_and_cache_fresh_photos("v1")

    assert len(result) == 1
    assert google.last_kwargs["place_id"] == "places/FALLBACK"
    # Fresh cache is written through the primary (system-of-record) DAO's Redis.
    assert primary.get_venue_photos_fresh("v1") == result
