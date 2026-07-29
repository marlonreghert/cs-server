"""Menu extraction sourcing its photos from the retrieval archive.

Extraction never downloads bytes — it presigns S3 keys and hands the urls to a
vision model. These tests pin the parts where that can go quietly wrong: the
wrong run, the wrong category, an info sidecar mistaken for a photo, and a
permission failure that would otherwise look like "this venue has no menu".
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.dao.media_archive_store import MediaArchiveStore
from app.services.menu_extraction_service import (
    MenuExtractionService,
    selectable_menu_photos,
)

SOURCE = "searchapi_gmaps_photos"
ROOT = f"retrieved/source={SOURCE}/year=2026/month=07/day=28/"
OLD_RUN = ROOT + "run_id=01AAAAAAAAAAAAAAAAAAAAAAAA/"
NEW_RUN = ROOT + "run_id=01ZZZZZZZZZZZZZZZZZZZZZZZZ/"   # ULIDs sort chronologically
VENUE = "ven_abc"


class _FakeS3:
    """In-memory S3 with just the surface the store uses."""

    def __init__(self, keys=(), fail_presign=False, deny_list=False):
        self.objects = {k: b"x" for k in keys}
        self.fail_presign = fail_presign
        self.deny_list = deny_list
        self.presigned = []

    def list_objects_v2(self, Bucket=None, Prefix="", Delimiter=None,
                        MaxKeys=1000, **kw):
        if self.deny_list:
            raise RuntimeError("AccessDenied")
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        out = {}
        if Delimiter:
            prefixes = set()
            for k in keys:
                rest = k[len(Prefix):]
                if Delimiter in rest:
                    prefixes.add(Prefix + rest.split(Delimiter)[0] + Delimiter)
            out["CommonPrefixes"] = [{"Prefix": p} for p in sorted(prefixes)]
        if keys:
            out["Contents"] = [{"Key": k} for k in keys[:MaxKeys]]
        return out

    def get_object(self, Bucket=None, Key=None, **kw):
        # Reading the manifest is how extraction learns which photos are
        # legible. A venue with no manifest must still extract, so a miss
        # raises exactly as S3 would rather than returning something empty.
        if Key not in self.objects:
            raise RuntimeError(f"NoSuchKey: {Key}")
        return {"Body": _Body(self.objects[Key])}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        if self.fail_presign:
            raise RuntimeError("AccessDenied: s3:GetObject")
        self.presigned.append((Params["Key"], ExpiresIn))
        return f"https://signed/{Params['Key']}?exp={ExpiresIn}"


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


def _store(keys=(), **kw):
    return MediaArchiveStore(
        bucket="lake", region="us-east-1", s3_client=_FakeS3(keys, **kw)
    )


def _service(store, **kw):
    return MenuExtractionService(
        openai_client=object(), s3_client=object(), venue_dao=object(),
        media_store=store, photo_source="archive", archive_source=SOURCE,
        archive_category="menu", **kw,
    )


def _menu_keys(run, venue=VENUE, n=2):
    return [f"{run}venue_id={venue}/media/menu/p{i}.jpg" for i in range(n)]


# ── choosing the run ──────────────────────────────────────────────────────────
class TestNewestRun:
    def test_it_reads_the_newest_run(self):
        store = _store(_menu_keys(OLD_RUN, n=1) + _menu_keys(NEW_RUN, n=1))
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert len(urls) == 1
        assert NEW_RUN in urls[0]

    def test_an_older_run_is_ignored_even_when_it_has_more_photos(self):
        # Runs are snapshots. Preferring the fuller one would pair a fresh menu
        # with a stale photo and give no sign it happened.
        store = _store(_menu_keys(OLD_RUN, n=9) + _menu_keys(NEW_RUN, n=1))
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert len(urls) == 1 and NEW_RUN in urls[0]

    def test_a_venue_absent_from_the_newest_run_has_no_photos(self):
        store = _store(_menu_keys(OLD_RUN) + _menu_keys(NEW_RUN, venue="ven_other"))
        assert asyncio.run(_service(store)._archive_photo_urls(VENUE)) == (None, None, False)

    def test_no_runs_at_all_is_not_an_error(self):
        assert asyncio.run(_service(_store())._archive_photo_urls(VENUE)) == (None, None, False)

    def test_a_missing_media_store_is_reported_not_crashed(self):
        svc = MenuExtractionService(
            openai_client=object(), s3_client=object(), venue_dao=object(),
            media_store=None, photo_source="archive",
        )
        assert asyncio.run(svc._archive_photo_urls(VENUE)) == (None, None, False)


# ── choosing the photos ───────────────────────────────────────────────────────
class TestPhotoSelection:
    def test_only_the_configured_category_is_read(self):
        store = _store(
            _menu_keys(NEW_RUN)
            + [f"{NEW_RUN}venue_id={VENUE}/media/vibe/v0.jpg",
               f"{NEW_RUN}venue_id={VENUE}/media/all/a0.jpg"]
        )
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert all("/media/menu/" in u for u in urls), urls
        assert len(urls) == 2

    def test_info_sidecars_are_never_treated_as_photos(self):
        # place.json and _manifest.json live under the same venue prefix; a
        # vision model must never be handed one.
        store = _store(_store_keys := _menu_keys(NEW_RUN) + [
            f"{NEW_RUN}venue_id={VENUE}/info/place.json",
            f"{NEW_RUN}venue_id={VENUE}/info/_manifest.json",
        ])
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert all(u.endswith(".jpg?exp=900") or ".jpg?" in u for u in urls)
        assert not any(".json" in u for u in urls)

    def test_another_venue_is_never_included(self):
        store = _store(_menu_keys(NEW_RUN) + _menu_keys(NEW_RUN, venue="ven_zzz"))
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert all(f"venue_id={VENUE}/" in u for u in urls)

    def test_photo_ids_come_from_the_filenames(self):
        store = _store(_menu_keys(NEW_RUN, n=2))
        _, ids, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert ids == ["p0", "p1"]


# ── signing ───────────────────────────────────────────────────────────────────
class TestPresigning:
    def test_urls_are_presigned_and_bounded(self):
        store = _store(_menu_keys(NEW_RUN, n=1))
        asyncio.run(_service(store, presign_seconds=300)._archive_photo_urls(VENUE))
        assert store._s3.presigned[0][1] == 300

    def test_the_model_never_receives_a_raw_bucket_url(self):
        store = _store(_menu_keys(NEW_RUN, n=1))
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert urls[0].startswith("https://signed/")
        assert "s3.amazonaws.com" not in urls[0]

    def test_one_unsignable_photo_does_not_lose_the_others(self):
        store = _store(_menu_keys(NEW_RUN, n=3))
        real = store._s3.generate_presigned_url
        calls = {"n": 0}

        def flaky(op, Params=None, ExpiresIn=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("transient")
            return real(op, Params=Params, ExpiresIn=ExpiresIn)

        store._s3.generate_presigned_url = flaky
        urls, ids, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert len(urls) == 2 and len(ids) == 2

    def test_denied_read_access_yields_no_photos_rather_than_bad_urls(self):
        # The likeliest cause is the IAM grant missing; the service must not
        # hand the model urls that will 403.
        store = _store(_menu_keys(NEW_RUN), fail_presign=True)
        assert asyncio.run(_service(store)._archive_photo_urls(VENUE)) == (None, None, False)

    def test_a_listing_failure_is_survivable(self):
        store = _store(_menu_keys(NEW_RUN), deny_list=True)
        assert asyncio.run(_service(store)._archive_photo_urls(VENUE)) == (None, None, False)


# ── paying only for menus that can be read ────────────────────────────────────
class TestLegibilityGate:
    """`legible` is where the classifier pays for itself: no OCR on a blur.

    The rule is asymmetric on purpose — only what the manifest POSITIVELY marks
    illegible is dropped, so an archive from before the classifier existed
    behaves exactly as it did before.
    """

    def _store_with_manifest(self, entries, keys=None):
        keys = keys if keys is not None else [e["key"] for e in entries]
        store = _store(keys)
        store._s3.objects[f"{NEW_RUN}venue_id={VENUE}/info/_manifest.json"] = (
            json.dumps({"photos": entries}).encode()
        )
        return store

    def test_an_illegible_photo_is_never_sent_to_extraction(self):
        legible, blurred = _menu_keys(NEW_RUN)
        store = self._store_with_manifest([
            {"key": legible, "attributes": {"legible": "yes"}},
            {"key": blurred, "attributes": {"legible": "no"}},
        ])
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert len(urls) == 1 and legible in urls[0]

    def test_a_partly_legible_photo_is_still_worth_reading(self):
        key = _menu_keys(NEW_RUN, n=1)[0]
        store = self._store_with_manifest([
            {"key": key, "attributes": {"legible": "partial"}},
        ])
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert len(urls) == 1

    def test_a_photo_with_no_verdict_is_kept(self):
        # Unknown is not unreadable.
        key = _menu_keys(NEW_RUN, n=1)[0]
        store = self._store_with_manifest([{"key": key}])
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert len(urls) == 1

    def test_a_photo_the_classifier_could_not_judge_is_kept(self):
        # `not_classified` means "asked, could not tell" — which is exactly the
        # case where the extractor deserves its shot at the photo.
        key = _menu_keys(NEW_RUN, n=1)[0]
        store = self._store_with_manifest([
            {"key": key, "attributes": {"legible": "not_classified"}},
        ])
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert len(urls) == 1

    def test_a_photo_the_manifest_never_mentions_is_kept(self):
        listed, unlisted = _menu_keys(NEW_RUN)
        store = self._store_with_manifest(
            [{"key": listed, "attributes": {"legible": "yes"}}],
            keys=[listed, unlisted],
        )
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert len(urls) == 2

    def test_a_run_with_no_manifest_extracts_exactly_as_before(self):
        store = _store(_menu_keys(NEW_RUN))
        urls, _, _ = asyncio.run(_service(store)._archive_photo_urls(VENUE))
        assert len(urls) == 2

    def test_a_venue_whose_every_menu_is_illegible_costs_nothing(self):
        store = self._store_with_manifest([
            {"key": k, "attributes": {"legible": "no"}} for k in _menu_keys(NEW_RUN)
        ])
        assert asyncio.run(
            _service(store)._archive_photo_urls(VENUE)
        ) == (None, None, False)
        assert store._s3.presigned == [], "an illegible photo was signed anyway"

    def test_the_selector_is_a_pure_function_of_the_entries(self):
        entries = [
            {"key": "a", "attributes": {"legible": "yes"}},
            {"key": "b", "attributes": {"legible": "no"}},
            {"key": "c"},
        ]
        assert [e["key"] for e in selectable_menu_photos(entries)] == ["a", "c"]


# ── the seam ──────────────────────────────────────────────────────────────────
class TestPhotoSourceSeam:
    def test_the_redis_path_is_still_selectable(self):
        class _Photos:
            photos = [type("P", (), {"s3_key": "k1", "photo_id": "pid1"})()]
            source = "google_places"  # not one of the pre-filtered sources
            def has_photos(self): return True

        class _Dao:
            def get_venue_menu_photos(self, vid): return _Photos()

        class _S3:
            async def generate_presigned_url(self, key): return f"https://old/{key}"

        svc = MenuExtractionService(
            openai_client=object(), s3_client=_S3(), venue_dao=_Dao(),
            media_store=_store(_menu_keys(NEW_RUN)), photo_source="redis",
        )
        urls, ids, needs_filter = asyncio.run(svc._photo_urls(VENUE))
        assert urls == ["https://old/k1"] and ids == ["pid1"]
        # The Redis path still owes the pre-filter a decision; the archive path
        # does not, because the category and the legibility are already known.
        assert needs_filter is True

    def test_the_archive_path_is_the_default(self):
        svc = MenuExtractionService(
            openai_client=object(), s3_client=object(), venue_dao=object(),
        )
        assert svc.photo_source == "archive"
        assert svc.archive_source == "searchapi_gmaps_photos"
        assert svc.archive_category == "menu"

    def test_both_paths_return_the_same_shape(self):
        store = _store(_menu_keys(NEW_RUN, n=2))
        urls, ids, _ = asyncio.run(_service(store)._photo_urls(VENUE))
        assert isinstance(urls, list) and isinstance(ids, list)
        assert len(urls) == len(ids)


# ── store primitives ──────────────────────────────────────────────────────────
class TestStorePrimitives:
    def test_latest_run_prefix_uses_ulid_ordering(self):
        store = _store(_menu_keys(OLD_RUN) + _menu_keys(NEW_RUN))
        assert asyncio.run(store.latest_run_prefix(SOURCE)) == NEW_RUN

    def test_latest_run_prefix_is_none_when_empty(self):
        assert asyncio.run(_store().latest_run_prefix(SOURCE)) is None

    def test_list_venue_photos_without_a_category_returns_all_media(self):
        store = _store(_menu_keys(NEW_RUN, n=1)
                       + [f"{NEW_RUN}venue_id={VENUE}/media/vibe/v.jpg"])
        keys = asyncio.run(store.list_venue_photos(NEW_RUN, VENUE))
        assert len(keys) == 2

    def test_presign_returns_none_instead_of_raising(self):
        store = _store(_menu_keys(NEW_RUN), fail_presign=True)
        assert asyncio.run(store.presign("any/key")) is None

    @pytest.mark.parametrize("hostile", ["../../raw", "", "menu/../../raw"])
    def test_a_hostile_category_cannot_escape_the_venue_prefix(self, hostile):
        store = _store(_menu_keys(NEW_RUN))
        keys = asyncio.run(store.list_venue_photos(NEW_RUN, VENUE, hostile))
        assert all(k.startswith(f"{NEW_RUN}venue_id={VENUE}/media/") for k in keys)
