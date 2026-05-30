"""Tests for venue_photos cache TTL eviction.

Background: Google rotates photo `name` tokens periodically. Once that happens
the cached /media URLs return 400 INVALID_ARGUMENT and the mobile app shows
nothing. `set_venue_photos` must always write its entries with a finite TTL so
that the daily enrichment cron picks each venue back up after its TTL fires
and replaces the stored URLs with fresh tokens.
"""
import json

import fakeredis
import pytest

from app.config import settings
from app.dao.redis_venue_dao import (
    ADMIN_CONFIG_PHOTOS_TTL_KEY,
    VENUE_PHOTOS_KEY_FORMAT,
    RedisVenueDAO,
)


def assert_ttl_close_to(redis_client, key: str, expected_seconds: int, tolerance: int = 5):
    """fakeredis can tick the TTL down by a second between setex() and ttl();
    assert within a small tolerance instead of demanding exact equality."""
    actual = redis_client.ttl(key)
    assert actual > 0, f"{key} has no TTL (returned {actual})"
    assert abs(actual - expected_seconds) <= tolerance, (
        f"{key} TTL={actual}, expected ~{expected_seconds} (±{tolerance}s)"
    )


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def dao(fake_redis):
    # RedisVenueDAO expects a GeoRedisClient-like object exposing setex/get.
    # fakeredis.FakeRedis satisfies that contract.
    return RedisVenueDAO(client=fake_redis)


PHOTOS = [
    {"url": "https://places.googleapis.com/v1/x/photos/AAA/media?k=1", "author_name": "a"},
    {"url": "https://places.googleapis.com/v1/x/photos/BBB/media?k=1", "author_name": "b"},
]


class TestSetVenuePhotosTTL:
    def test_set_writes_with_default_ttl_when_no_admin_override(self, dao, fake_redis):
        dao.set_venue_photos("ven_1", PHOTOS)
        expected = settings.photo_cache_ttl_days * 24 * 3600
        assert_ttl_close_to(fake_redis, VENUE_PHOTOS_KEY_FORMAT.format("ven_1"), expected)

    def test_set_respects_admin_override(self, dao, fake_redis):
        fake_redis.set(ADMIN_CONFIG_PHOTOS_TTL_KEY, json.dumps(2))
        dao.set_venue_photos("ven_2", PHOTOS)
        assert_ttl_close_to(fake_redis, VENUE_PHOTOS_KEY_FORMAT.format("ven_2"), 2 * 24 * 3600)

    def test_set_falls_back_to_default_on_invalid_admin_override(self, dao, fake_redis):
        fake_redis.set(ADMIN_CONFIG_PHOTOS_TTL_KEY, "not-an-int")
        dao.set_venue_photos("ven_3", PHOTOS)
        expected = settings.photo_cache_ttl_days * 24 * 3600
        assert_ttl_close_to(fake_redis, VENUE_PHOTOS_KEY_FORMAT.format("ven_3"), expected)

    def test_set_falls_back_to_default_on_non_positive_admin_override(self, dao, fake_redis):
        fake_redis.set(ADMIN_CONFIG_PHOTOS_TTL_KEY, json.dumps(0))
        dao.set_venue_photos("ven_4", PHOTOS)
        expected = settings.photo_cache_ttl_days * 24 * 3600
        assert_ttl_close_to(fake_redis, VENUE_PHOTOS_KEY_FORMAT.format("ven_4"), expected)

    def test_get_round_trips_value_set_with_ttl(self, dao):
        dao.set_venue_photos("ven_5", PHOTOS)
        assert dao.get_venue_photos("ven_5") == PHOTOS

    def test_default_ttl_is_5_days(self):
        # If anyone tightens / relaxes the default, the cost analysis ($0/mo at
        # current ~465 venues, well under Google's $200/mo free credit) is
        # invalidated. Update the cost write-up if you change this.
        assert settings.photo_cache_ttl_days == 5
