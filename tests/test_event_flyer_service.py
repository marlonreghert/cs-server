"""Unit coverage for app.services.event_flyer_service.EventFlyerService
(plans/260905_events-serving-projection.md, Phase 1).

Uses hand-rolled fakes for the archive/media stores (so a raised exception's
exact shape is fully controlled) and the real InMemoryRdsVenueStore for
persistence, so `update_event` really does route through the same
`_EVENT_COLUMNS` shape a genuine caller would exercise.
"""
from __future__ import annotations

import asyncio

import pytest

from app.models.venue import Venue
from app.services.event_flyer_service import (
    ALL_OUTCOMES,
    EventFlyerService,
    OUTCOME_ACCESS_DENIED,
    OUTCOME_ARCHIVE_MISSING,
    OUTCOME_COPIED,
    OUTCOME_FAILED,
    OUTCOME_NO_KEY,
    OUTCOME_UNCHANGED,
)
from tests.rds_fake import InMemoryRdsVenueStore


class _FakeArchiveStore:
    def __init__(self):
        self.reads: list[str] = []
        self.missing_keys: set[str] = set()
        self.failing_keys: set[str] = set()
        self.bytes_by_key: dict[str, bytes] = {}

    async def read_image_bytes(self, key: str):
        self.reads.append(key)
        if key in self.missing_keys:
            raise FileNotFoundError(key)
        if key in self.failing_keys:
            raise RuntimeError("transient S3 read error")
        return self.bytes_by_key.get(key, b"default-flyer-bytes"), "image/jpeg"


class _FakeMediaStore:
    def __init__(self):
        self.puts: list[dict] = []
        self.deny = False
        self.fail = False

    def event_flyer_key(self, post_item_id: str, content_hash: str) -> str:
        return f"event-flyers/{post_item_id}/{content_hash[:16]}.jpg"

    def cdn_url(self, key: str) -> str:
        return f"https://media.example/{key}"

    async def put_event_flyer(self, *, post_item_id, content_hash, data, content_type="image/jpeg"):
        if self.deny:
            raise RuntimeError("AccessDenied: not authorized to perform s3:PutObject")
        if self.fail:
            raise RuntimeError("boom")
        key = self.event_flyer_key(post_item_id, content_hash)
        self.puts.append({"post_item_id": post_item_id, "key": key})
        return key, self.cdn_url(key)


def _rds_with_event(event_id: str, **fields) -> InMemoryRdsVenueStore:
    store = InMemoryRdsVenueStore()
    venue_id = "flyer_test_venue"
    store.upsert_venue(Venue(venue_id=venue_id, venue_name="V", venue_lat=-8.0, venue_lng=-34.0))
    base = {
        "event_id": event_id, "venue_id": venue_id, "status": "accepted",
        "title": "Test Event", "source_handle": "h", "source_shortcode": event_id,
    }
    base.update(fields)
    store.insert_event(base)
    return store


def test_no_cover_photo_key_yields_no_key_outcome_and_null_flyer_url():
    rds = _rds_with_event("ev_no_key")
    service = EventFlyerService(archive_store=_FakeArchiveStore(), media_store=_FakeMediaStore(), rds_store=rds)
    result = asyncio.run(service.copy_flyers([rds.get_event("ev_no_key")], max_per_cycle=10))
    assert result["counts"][OUTCOME_NO_KEY] == 1
    assert "ev_no_key" not in result["flyer_urls"]
    assert rds.get_event("ev_no_key")["flyer_url"] is None


def test_successful_copy_persists_url_key_hash_and_byte_size():
    rds = _rds_with_event("ev_copy", cover_photo_key="retrieved/.../cover.jpg")
    archive = _FakeArchiveStore()
    archive.bytes_by_key["retrieved/.../cover.jpg"] = b"real-flyer-bytes"
    media = _FakeMediaStore()
    service = EventFlyerService(archive_store=archive, media_store=media, rds_store=rds)

    result = asyncio.run(service.copy_flyers([rds.get_event("ev_copy")], max_per_cycle=10))

    assert result["counts"][OUTCOME_COPIED] == 1
    assert result["flyer_urls"]["ev_copy"].startswith("https://media.example/event-flyers/ev_copy/")
    row = rds.get_event("ev_copy")
    assert row["flyer_url"] == result["flyer_urls"]["ev_copy"]
    assert row["flyer_s3_key"].startswith("event-flyers/ev_copy/")
    assert row["flyer_content_hash"]  # a real sha256 hexdigest was stored
    assert row["flyer_copied_at"] is not None
    assert row["flyer_byte_size"] == len(b"real-flyer-bytes")
    assert len(media.puts) == 1


def test_no_archived_cover_photo_yields_archive_missing_and_null_url():
    rds = _rds_with_event("ev_missing", cover_photo_key="retrieved/.../gone.jpg")
    archive = _FakeArchiveStore()
    archive.missing_keys.add("retrieved/.../gone.jpg")
    service = EventFlyerService(archive_store=archive, media_store=_FakeMediaStore(), rds_store=rds)

    result = asyncio.run(service.copy_flyers([rds.get_event("ev_missing")], max_per_cycle=10))

    assert result["counts"][OUTCOME_ARCHIVE_MISSING] == 1
    assert "ev_missing" not in result["flyer_urls"]
    assert rds.get_event("ev_missing")["flyer_url"] is None


def test_a_generic_archive_read_failure_yields_failed_outcome():
    rds = _rds_with_event("ev_read_fail", cover_photo_key="retrieved/.../x.jpg")
    archive = _FakeArchiveStore()
    archive.failing_keys.add("retrieved/.../x.jpg")
    service = EventFlyerService(archive_store=archive, media_store=_FakeMediaStore(), rds_store=rds)

    result = asyncio.run(service.copy_flyers([rds.get_event("ev_read_fail")], max_per_cycle=10))
    assert result["counts"][OUTCOME_FAILED] == 1


def test_access_denied_on_the_media_put_is_recorded_loudly_and_never_blocks_projection():
    rds = _rds_with_event("ev_denied", cover_photo_key="retrieved/.../y.jpg")
    media = _FakeMediaStore()
    media.deny = True
    service = EventFlyerService(archive_store=_FakeArchiveStore(), media_store=media, rds_store=rds)

    result = asyncio.run(service.copy_flyers([rds.get_event("ev_denied")], max_per_cycle=10))

    assert result["counts"][OUTCOME_ACCESS_DENIED] == 1
    assert "ev_denied" not in result["flyer_urls"]
    assert rds.get_event("ev_denied")["flyer_url"] is None  # never blocks the caller from proceeding


def test_a_generic_upload_failure_yields_failed_outcome():
    rds = _rds_with_event("ev_upload_fail", cover_photo_key="retrieved/.../z.jpg")
    media = _FakeMediaStore()
    media.fail = True
    service = EventFlyerService(archive_store=_FakeArchiveStore(), media_store=media, rds_store=rds)

    result = asyncio.run(service.copy_flyers([rds.get_event("ev_upload_fail")], max_per_cycle=10))
    assert result["counts"][OUTCOME_FAILED] == 1


def test_matching_stored_hash_skips_reupload_and_recovers_the_same_url():
    """Partial-failure-recovery: an earlier cycle's PUT succeeded and the
    hash got persisted, but flyer_url itself did not (candidacy is gated on
    flyer_url being falsy, so this event is still attempted) — the SAME
    hash on a fresh read must resolve the SAME url with NO re-upload."""
    rds = _rds_with_event(
        "ev_recover", cover_photo_key="retrieved/.../w.jpg",
        flyer_content_hash=None,  # filled below to the real hash of the fixed bytes
    )
    archive = _FakeArchiveStore()
    archive.bytes_by_key["retrieved/.../w.jpg"] = b"stable-bytes"
    import hashlib
    real_hash = hashlib.sha256(b"stable-bytes").hexdigest()
    rds.update_event("ev_recover", {"flyer_content_hash": real_hash})
    media = _FakeMediaStore()
    service = EventFlyerService(archive_store=archive, media_store=media, rds_store=rds)

    result = asyncio.run(service.copy_flyers([rds.get_event("ev_recover")], max_per_cycle=10))

    assert result["counts"][OUTCOME_UNCHANGED] == 1
    assert len(media.puts) == 0  # no re-upload
    assert result["flyer_urls"]["ev_recover"] == media.cdn_url(
        media.event_flyer_key("ev_recover", real_hash)
    )
    assert rds.get_event("ev_recover")["flyer_url"] == result["flyer_urls"]["ev_recover"]


def test_an_event_that_already_has_a_flyer_url_is_never_attempted_again():
    rds = _rds_with_event(
        "ev_done", cover_photo_key="retrieved/.../done.jpg", flyer_url="https://media.example/already",
    )
    archive = _FakeArchiveStore()
    media = _FakeMediaStore()
    service = EventFlyerService(archive_store=archive, media_store=media, rds_store=rds)

    result = asyncio.run(service.copy_flyers([rds.get_event("ev_done")], max_per_cycle=10))

    assert result["attempted"] == 0
    assert sum(result["counts"].values()) == 0
    assert archive.reads == []
    assert media.puts == []


def test_max_per_cycle_bounds_how_many_new_candidates_are_attempted():
    rds = InMemoryRdsVenueStore()
    rds.upsert_venue(Venue(venue_id="v1", venue_name="V", venue_lat=-8.0, venue_lng=-34.0))
    events = []
    for i in range(5):
        eid = f"ev_cap_{i}"
        rds.insert_event({
            "event_id": eid, "venue_id": "v1", "status": "accepted",
            "cover_photo_key": f"retrieved/.../{i}.jpg",
            "source_handle": "h", "source_shortcode": eid,
        })
        events.append(rds.get_event(eid))

    archive = _FakeArchiveStore()
    media = _FakeMediaStore()
    service = EventFlyerService(archive_store=archive, media_store=media, rds_store=rds)

    result = asyncio.run(service.copy_flyers(events, max_per_cycle=2))

    assert result["attempted"] == 2
    assert len(media.puts) == 2
    # Deterministic (sorted by event_id), so exactly the first two IDs won.
    assert set(result["flyer_urls"].keys()) == {"ev_cap_0", "ev_cap_1"}
    # The rest are untouched, ready to be picked up next cycle.
    assert rds.get_event("ev_cap_4")["flyer_url"] is None


def test_all_outcomes_tuple_matches_the_module_constants():
    assert set(ALL_OUTCOMES) == {
        OUTCOME_COPIED, OUTCOME_UNCHANGED, OUTCOME_NO_KEY,
        OUTCOME_ARCHIVE_MISSING, OUTCOME_ACCESS_DENIED, OUTCOME_FAILED,
    }
