"""Unit tests for app/services/event_source_media.py
(plans/260813_event-source-media.md).

Covers the internal edge cases the BDD feature
(tests/bdd/api/event-source-media.feature) does not assert on directly:
deriving a run's manifest address from a stored key (both the venue_id= and
promoter= partition spellings), matching manifest entries to a source by
shortcode across suffixed/unsuffixed archived filenames, the
read-by-the-extractor flag's exactly-one/none behavior, and the
one-manifest-read-per-distinct-run cache `resolve_event_media` relies on.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from app.services.event_source_media import (
    RunPartition,
    derive_run_partition,
    entries_for_shortcode,
    images_from_entries,
    resolve_event_media,
    sort_sources_by_published,
)

RUN_PREFIX = "retrieved/source=instagram_posts/year=2026/month=08/day=07/run_id=01J000000000000TESTRUN/"
VENUE_ID = "venue-1"
PROMOTER_HANDLE = "some_promoter"


# ── derive_run_partition ─────────────────────────────────────────────────────
class TestDeriveRunPartition:
    def test_venue_id_partitioned_key(self):
        key = f"{RUN_PREFIX}venue_id={VENUE_ID}/media/flyer/Dbs1FdsEWr7_1.jpg"
        assert derive_run_partition(key) == RunPartition(
            prefix=RUN_PREFIX, kind="venue_id", value=VENUE_ID,
        )

    def test_promoter_partitioned_key(self):
        key = f"{RUN_PREFIX}promoter={PROMOTER_HANDLE}/media/DbtSQngKcPm.jpg"
        assert derive_run_partition(key) == RunPartition(
            prefix=RUN_PREFIX, kind="promoter", value=PROMOTER_HANDLE,
        )

    def test_none_key_returns_none(self):
        assert derive_run_partition(None) is None

    def test_empty_key_returns_none(self):
        assert derive_run_partition("") is None

    def test_key_with_no_run_id_segment_returns_none(self):
        # A legacy/malformed key must degrade to the fallback path, never
        # raise or match something it shouldn't.
        assert derive_run_partition("media/source=instagram/dt=2026-08-07/venue-1/cover.jpg") is None


# ── entries_for_shortcode ─────────────────────────────────────────────────────
class TestEntriesForShortcode:
    def test_matches_suffixed_and_unsuffixed_filenames_by_the_shortcode_field(self):
        # The archived FILENAME carries the _1/_2 ordinal (or none at all
        # for a single-image post); the manifest's own `shortcode` field
        # never does. Matching must key off that field, never off parsing
        # the filename — this is the guarantee the plan's own pytest list
        # names explicitly.
        manifest = {"photos": [
            {"shortcode": "Dbs1FdsEWr7", "key": "run/Dbs1FdsEWr7_1.jpg", "category": "flyer"},
            {"shortcode": "Dbs1FdsEWr7", "key": "run/Dbs1FdsEWr7_2.jpg", "category": "other"},
            {"shortcode": "DbtSQngKcPm", "key": "run/DbtSQngKcPm.jpg", "category": "flyer"},
        ]}
        assert {e["key"] for e in entries_for_shortcode(manifest, "Dbs1FdsEWr7")} == {
            "run/Dbs1FdsEWr7_1.jpg", "run/Dbs1FdsEWr7_2.jpg",
        }
        assert {e["key"] for e in entries_for_shortcode(manifest, "DbtSQngKcPm")} == {
            "run/DbtSQngKcPm.jpg",
        }

    def test_no_entries_for_an_unknown_shortcode(self):
        manifest = {"photos": [{"shortcode": "abc", "key": "k.jpg"}]}
        assert entries_for_shortcode(manifest, "does-not-exist") == []

    def test_none_manifest_returns_empty(self):
        assert entries_for_shortcode(None, "abc") == []

    def test_none_shortcode_returns_empty(self):
        assert entries_for_shortcode({"photos": [{"shortcode": "abc", "key": "k.jpg"}]}, None) == []

    def test_entries_missing_a_key_are_skipped(self):
        manifest = {"photos": [{"shortcode": "abc", "category": "flyer"}]}
        assert entries_for_shortcode(manifest, "abc") == []

    def test_missing_photos_list_returns_empty(self):
        assert entries_for_shortcode({}, "abc") == []


# ── images_from_entries: the read-by-the-extractor flag ─────────────────────
class TestImagesFromEntriesReadFlag:
    def test_exactly_one_image_marked_read_when_the_cover_key_is_present(self):
        entries = [
            {"key": "k1.jpg", "category": "flyer", "classification_confidence": 0.9},
            {"key": "k2.jpg", "category": "flyer", "classification_confidence": 0.7},
        ]
        images = images_from_entries(entries, read_key="k2.jpg")
        read_flags = [img["read_by_extractor"] for img in images]
        assert read_flags == [False, True]
        assert sum(read_flags) == 1

    def test_no_image_marked_read_when_the_cover_key_is_none(self):
        entries = [
            {"key": "k1.jpg", "category": "flyer", "classification_confidence": 0.9},
            {"key": "k2.jpg", "category": "flyer", "classification_confidence": 0.7},
        ]
        images = images_from_entries(entries, read_key=None)
        assert all(img["read_by_extractor"] is False for img in images)

    def test_no_image_marked_read_when_the_cover_key_matches_nothing_in_the_manifest(self):
        # A stale/rotated manifest that no longer lists the extracted key —
        # must not crash, and must not falsely mark something else as read.
        entries = [{"key": "k1.jpg", "category": "flyer", "classification_confidence": 0.9}]
        images = images_from_entries(entries, read_key="some-other-key.jpg")
        assert images[0]["read_by_extractor"] is False

    def test_category_and_confidence_are_carried_through_unchanged(self):
        entries = [{"key": "k1.jpg", "category": "flyer", "classification_confidence": 0.42}]
        images = images_from_entries(entries, read_key=None)
        assert images[0]["category"] == "flyer"
        assert images[0]["confidence"] == 0.42


# ── sort_sources_by_published ────────────────────────────────────────────────
class TestSortSourcesByPublished:
    def test_oldest_published_first(self):
        early = {"source_shortcode": "early", "source_uploaded_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        late = {"source_shortcode": "late", "source_uploaded_at": datetime(2026, 6, 1, tzinfo=timezone.utc)}
        ordered = sort_sources_by_published([late, early])
        assert [s["source_shortcode"] for s in ordered] == ["early", "late"]

    def test_unknown_publish_time_sorts_after_every_known_one(self):
        unknown = {"source_shortcode": "unknown", "source_uploaded_at": None}
        known = {"source_shortcode": "known", "source_uploaded_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        ordered = sort_sources_by_published([unknown, known])
        assert [s["source_shortcode"] for s in ordered] == ["known", "unknown"]

    def test_sort_is_stable_for_ties_and_multiple_unknowns(self):
        a = {"source_shortcode": "a", "source_uploaded_at": None}
        b = {"source_shortcode": "b", "source_uploaded_at": None}
        ordered = sort_sources_by_published([a, b])
        assert [s["source_shortcode"] for s in ordered] == ["a", "b"]


# ── resolve_event_media: manifest caching + fallback + sign-failure ─────────
class _FakeMediaStore:
    """A media store stand-in keyed exactly like the real
    `MediaArchiveStore` — `(prefix, venue_id_or_handle)` — so cache-hit
    behavior is directly observable via `manifest_calls`."""

    def __init__(self, manifests: Optional[dict[tuple[str, str], Optional[dict]]] = None):
        self._manifests = manifests or {}
        self.manifest_calls: list[tuple[str, str]] = []
        self.presign_calls: list[str] = []
        self._unsignable: set[str] = set()

    def fail_presign_for(self, key: str) -> None:
        self._unsignable.add(key)

    async def read_manifest(self, prefix, venue_id):
        self.manifest_calls.append((prefix, venue_id))
        return self._manifests.get((prefix, venue_id))

    async def read_promoter_manifest(self, prefix, handle):
        self.manifest_calls.append((prefix, handle))
        return self._manifests.get((prefix, handle))

    async def presign(self, key, expires_in=900):
        self.presign_calls.append(key)
        if key in self._unsignable:
            return None
        return f"https://signed.example.com/{key}?expires_in={expires_in}"


def _venue_source(shortcode: str, key: str, uploaded_at=None) -> dict:
    return {
        "source_shortcode": shortcode, "source_handle": "handle",
        "source_permalink": f"https://instagram.com/p/{shortcode}/",
        "source_media_type": "Sidecar", "source_uploaded_at": uploaded_at,
        "cover_photo_key": key,
    }


class TestResolveEventMediaManifestCaching:
    def test_one_manifest_read_per_distinct_run_when_sources_share_a_run(self):
        manifest = {"photos": [
            {"shortcode": "s1", "key": f"{RUN_PREFIX}venue_id={VENUE_ID}/media/flyer/s1.jpg",
             "category": "flyer", "classification_confidence": 0.9},
            {"shortcode": "s2", "key": f"{RUN_PREFIX}venue_id={VENUE_ID}/media/flyer/s2.jpg",
             "category": "flyer", "classification_confidence": 0.8},
        ]}
        store = _FakeMediaStore({(RUN_PREFIX, VENUE_ID): manifest})
        sources = [
            _venue_source("s1", f"{RUN_PREFIX}venue_id={VENUE_ID}/media/flyer/s1.jpg"),
            _venue_source("s2", f"{RUN_PREFIX}venue_id={VENUE_ID}/media/flyer/s2.jpg"),
        ]
        resolved = asyncio.run(resolve_event_media(store, sources, presign_expires_in=900))
        assert store.manifest_calls == [(RUN_PREFIX, VENUE_ID)]
        assert sum(len(s["images"]) for s in resolved) == 2
        assert all(not s["used_fallback"] for s in resolved)

    def test_different_runs_are_read_independently(self):
        prefix_a = RUN_PREFIX
        prefix_b = "retrieved/source=instagram_posts/year=2026/month=08/day=01/run_id=01J000000000000OTHERRUN/"
        manifest_a = {"photos": [{
            "shortcode": "s1", "key": f"{prefix_a}venue_id={VENUE_ID}/media/flyer/s1.jpg",
            "category": "flyer", "classification_confidence": 0.9,
        }]}
        manifest_b = {"photos": [{
            "shortcode": "s2", "key": f"{prefix_b}venue_id={VENUE_ID}/media/flyer/s2.jpg",
            "category": "flyer", "classification_confidence": 0.8,
        }]}
        store = _FakeMediaStore({
            (prefix_a, VENUE_ID): manifest_a, (prefix_b, VENUE_ID): manifest_b,
        })
        sources = [
            _venue_source("s1", f"{prefix_a}venue_id={VENUE_ID}/media/flyer/s1.jpg"),
            _venue_source("s2", f"{prefix_b}venue_id={VENUE_ID}/media/flyer/s2.jpg"),
        ]
        resolved = asyncio.run(resolve_event_media(store, sources, presign_expires_in=900))
        assert sorted(store.manifest_calls) == sorted([(prefix_a, VENUE_ID), (prefix_b, VENUE_ID)])
        assert sum(len(s["images"]) for s in resolved) == 2


class TestResolveEventMediaFallback:
    def test_unreadable_manifest_falls_back_to_the_stored_cover_key_alone(self):
        cover_key = f"{RUN_PREFIX}venue_id={VENUE_ID}/media/flyer/s1.jpg"
        store = _FakeMediaStore({(RUN_PREFIX, VENUE_ID): None})  # unreadable
        sources = [_venue_source("s1", cover_key)]
        resolved = asyncio.run(resolve_event_media(store, sources, presign_expires_in=900))
        assert len(resolved) == 1
        assert resolved[0]["used_fallback"] is True
        assert [img["url"] for img in resolved[0]["images"]] == [
            f"https://signed.example.com/{cover_key}?expires_in=900",
        ]
        assert resolved[0]["images"][0]["read_by_extractor"] is True

    def test_no_cover_key_yields_zero_images_without_reading_any_manifest(self):
        store = _FakeMediaStore()
        sources = [_venue_source("s1", None)]
        resolved = asyncio.run(resolve_event_media(store, sources, presign_expires_in=900))
        assert resolved[0]["images"] == []
        assert store.manifest_calls == []

    def test_malformed_key_falls_back_without_raising(self):
        store = _FakeMediaStore()
        sources = [_venue_source("s1", "not-a-real-archive-key.jpg")]
        resolved = asyncio.run(resolve_event_media(store, sources, presign_expires_in=900))
        assert resolved[0]["used_fallback"] is True
        assert len(resolved[0]["images"]) == 1


class TestResolveEventMediaSignFailure:
    def test_an_unsignable_image_is_omitted_and_the_rest_still_return(self):
        key_ok = f"{RUN_PREFIX}venue_id={VENUE_ID}/media/flyer/s1_1.jpg"
        key_bad = f"{RUN_PREFIX}venue_id={VENUE_ID}/media/s1_2.jpg"
        manifest = {"photos": [
            {"shortcode": "s1", "key": key_ok, "category": "flyer", "classification_confidence": 0.9},
            {"shortcode": "s1", "key": key_bad, "category": "other", "classification_confidence": 0.3},
        ]}
        store = _FakeMediaStore({(RUN_PREFIX, VENUE_ID): manifest})
        store.fail_presign_for(key_bad)
        sources = [_venue_source("s1", key_ok)]
        resolved = asyncio.run(resolve_event_media(store, sources, presign_expires_in=900))
        urls = [img["url"] for img in resolved[0]["images"]]
        assert urls == [f"https://signed.example.com/{key_ok}?expires_in=900"]
