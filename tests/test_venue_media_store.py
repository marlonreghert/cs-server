"""Unit coverage for the event-flyer additions to app.dao.venue_media_store
(plans/260905_events-serving-projection.md, Phase 1 pytest list):
`event_flyer_key` shape, idempotence of the hash slice, and the cache
header — mirroring `profile_photo_key`'s own existing behaviour exactly, so
these assert the SAME properties on the new prefix.
"""
from __future__ import annotations

import asyncio

import pytest

from app.dao.venue_media_store import (
    CONTENT_HASH_KEY_LENGTH,
    EVENT_FLYER_ROOT,
    PROFILE_PHOTO_CACHE_CONTROL,
    VenueMediaStore,
    event_flyer_key,
)

_CDN = "https://media.vibesense.example"
_POST_ITEM_ID = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
_FULL_HASH = "b" * 64  # a plausible sha256 hexdigest


class _RecordingS3:
    def __init__(self):
        self.puts: list[dict] = []
        self.fail = False

    def put_object(self, **kwargs):
        if self.fail:
            raise RuntimeError("AccessDenied")
        self.puts.append(dict(kwargs))
        return {}


def test_event_flyer_key_shape():
    key = event_flyer_key(_POST_ITEM_ID, _FULL_HASH)
    assert key == f"{EVENT_FLYER_ROOT}/{_POST_ITEM_ID}/{_FULL_HASH[:CONTENT_HASH_KEY_LENGTH]}.jpg"
    assert key.startswith("event-flyers/")


def test_event_flyer_key_hash_slice_is_idempotent():
    """A full sha256 hexdigest and an already-truncated one produce the
    IDENTICAL key — the same idempotence profile_photo_key already has."""
    truncated = _FULL_HASH[:CONTENT_HASH_KEY_LENGTH]
    assert event_flyer_key(_POST_ITEM_ID, _FULL_HASH) == event_flyer_key(_POST_ITEM_ID, truncated)


def test_event_flyer_key_differs_by_post_item_id_and_by_hash():
    a = event_flyer_key(_POST_ITEM_ID, _FULL_HASH)
    b = event_flyer_key("z" * 32, _FULL_HASH)
    c = event_flyer_key(_POST_ITEM_ID, "c" * 64)
    assert len({a, b, c}) == 3


def test_venue_media_store_event_flyer_key_method_matches_the_module_function():
    store = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=_RecordingS3())
    assert store.event_flyer_key(_POST_ITEM_ID, _FULL_HASH) == event_flyer_key(_POST_ITEM_ID, _FULL_HASH)


def test_put_event_flyer_stores_at_the_content_addressed_key_with_the_immutable_cache_header():
    s3 = _RecordingS3()
    store = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    key, url = asyncio.run(
        store.put_event_flyer(
            post_item_id=_POST_ITEM_ID, content_hash=_FULL_HASH,
            data=b"fake-flyer-bytes", content_type="image/jpeg",
        )
    )
    assert key == event_flyer_key(_POST_ITEM_ID, _FULL_HASH)
    assert url == f"{_CDN}/{key}"
    assert len(s3.puts) == 1
    put = s3.puts[0]
    assert put["Bucket"] == "b"
    assert put["Key"] == key
    assert put["Body"] == b"fake-flyer-bytes"
    assert put["CacheControl"] == PROFILE_PHOTO_CACHE_CONTROL


def test_put_event_flyer_propagates_a_bucket_denial():
    """The caller (EventFlyerService) is what classifies AccessDenied into
    its own outcome label; this store must not swallow it."""
    s3 = _RecordingS3()
    s3.fail = True
    store = VenueMediaStore(bucket="b", region="us-east-1", cdn_base_url=_CDN, s3_client=s3)
    with pytest.raises(RuntimeError, match="AccessDenied"):
        asyncio.run(
            store.put_event_flyer(
                post_item_id=_POST_ITEM_ID, content_hash=_FULL_HASH, data=b"x",
            )
        )
