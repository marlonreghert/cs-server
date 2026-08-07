"""Unit tests for the venue photo archive.

The BDD suite covers the observable pipeline behavior. These cover the lower
level pieces that are easy to get subtly wrong: prefix validation, id parsing,
stable photo ids, byte caps, and — most importantly — the ORDERING that makes
the cost guarantee real.
"""
import json

import pytest

from app.dao.media_archive_store import MediaArchiveStore, extension_for
from app.services.venue_photo_archive_service import (
    InvalidArchivePath,
    PhotoTooLarge,
    VenuePhotoArchiveService,
    day_prefix,
    parse_venue_ids,
    photo_id_for,
    validate_override,
)


class _FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None, **kw):
        self.objects[Key] = Body
        return {}

    def list_objects_v2(self, Bucket=None, Prefix="", Delimiter=None, MaxKeys=1000, **kw):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        out = {}
        if Delimiter:
            prefixes = {
                Prefix + k[len(Prefix):].split(Delimiter)[0] + Delimiter
                for k in keys
                if Delimiter in k[len(Prefix):]
            }
            out["CommonPrefixes"] = [{"Prefix": p} for p in sorted(prefixes)]
        if keys[:MaxKeys]:
            out["Contents"] = [{"Key": k} for k in keys[:MaxKeys]]
        return out


class _CountingGoogle:
    def __init__(self, photos=None):
        self.photos = photos or []
        self.calls = 0

    async def get_place_photos(self, place_id, max_photos=5, max_width=800, include_ref=False):
        self.calls += 1
        return list(self.photos)[:max_photos]


class _Downloader:
    def __init__(self, size=32):
        self.size = size

    async def download(self, url, *, timeout=None, max_bytes=None):
        data = b"x" * self.size
        if max_bytes is not None and len(data) > max_bytes:
            raise PhotoTooLarge("too big")
        return data, "image/jpeg"


class _Dao:
    def __init__(self, venues, place_ids=None, instagram_handles=None):
        self._venues = venues
        self._place_ids = place_ids if place_ids is not None else {v: f"place_{v}" for v in venues}
        self._instagram_handles = instagram_handles or {}

    def list_active_venue_ids(self):
        return list(self._venues)

    def get_vibe_attributes(self, venue_id):
        pid = self._place_ids.get(venue_id)
        return type("V", (), {"google_place_id": pid})() if pid else None

    def get_venue_instagram(self, venue_id):
        from types import SimpleNamespace

        handle = self._instagram_handles.get(venue_id)
        if not handle:
            return None
        return SimpleNamespace(instagram_handle=handle, has_instagram=lambda: True)


def _service(**over):
    fake_s3 = _FakeS3()
    kwargs = dict(
        google_places_client=_CountingGoogle([{"url": "u1", "photo_name": "n1"}]),
        venue_dao=_Dao(["ven_a"]),
        media_store=MediaArchiveStore(bucket="b", region="us-east-1", s3_client=fake_s3),
        downloader=_Downloader(),
        today_provider=lambda: "2026-07-26",
    )
    kwargs.update(over)
    svc = VenuePhotoArchiveService(**kwargs)
    svc._fake_s3 = fake_s3
    return svc


class TestVenueIdParsing:
    def test_splits_trims_and_drops_empties(self):
        assert parse_venue_ids(" ven_a , ven_b ,, ") == ["ven_a", "ven_b"]

    def test_dedupes_preserving_operator_order(self):
        assert parse_venue_ids("ven_b, ven_a, ven_b") == ["ven_b", "ven_a"]

    def test_accepts_a_list(self):
        assert parse_venue_ids(["ven_a", " ven_b "]) == ["ven_a", "ven_b"]

    def test_empty_means_whole_catalog(self):
        assert parse_venue_ids("") == []
        assert parse_venue_ids(None) == []


class TestOverrideValidation:
    def test_accepts_a_prefix_under_media(self):
        assert validate_override("media/manual/backfill/") == "media/manual/backfill/"

    def test_adds_the_trailing_slash(self):
        assert validate_override("media/manual") == "media/manual/"

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "raw/source=besttime", "media/../raw/", "../media/", "/etc/passwd"],
    )
    def test_rejects_anything_outside_media(self, bad):
        with pytest.raises(InvalidArchivePath):
            validate_override(bad)

    def test_rejects_traversal_that_only_normalisation_reveals(self):
        """`media/../raw/` passes a naive startswith('media/') check — it must be
        judged on the NORMALISED path."""
        with pytest.raises(InvalidArchivePath):
            validate_override("media/../raw/")


class TestPhotoId:
    def test_is_stable_across_runs_for_the_same_photo(self):
        photo = {"photo_name": "places/X/photos/abc", "url": "https://one"}
        assert photo_id_for(photo) == photo_id_for(dict(photo))

    def test_is_driven_by_the_resource_name_not_the_url(self):
        """Keyless URLs rotate; the resource name does not. Two fetches of the
        same photo must land on the same object key."""
        a = {"photo_name": "places/X/photos/abc", "url": "https://one"}
        b = {"photo_name": "places/X/photos/abc", "url": "https://two"}
        assert photo_id_for(a) == photo_id_for(b)

    def test_falls_back_to_url_without_a_resource_name(self):
        assert photo_id_for({"url": "https://only"})

    def test_differs_between_photos(self):
        assert photo_id_for({"photo_name": "a"}) != photo_id_for({"photo_name": "b"})

    # ── Instagram: post-identity, never the signed url ──────────────────────
    def test_instagram_single_image_id_is_the_shortcode(self):
        assert photo_id_for({"instagram_photo_id": "abc123", "url": "https://x"}) == "abc123"

    def test_instagram_carousel_child_id_carries_its_index(self):
        assert photo_id_for({"instagram_photo_id": "abc123_2", "url": "https://x"}) == "abc123_2"

    def test_instagram_id_is_unhashed_and_traceable_to_the_post(self):
        # Deliberately NOT hashed, unlike the Google path below — the raw
        # shortcode is what lets an id be read straight back to
        # instagram.com/p/<shortcode>.
        assert photo_id_for({"instagram_photo_id": "abc123"}) == "abc123"

    def test_instagram_id_is_stable_when_the_signed_url_rotates(self):
        # The whole point: Instagram's CDN signature changes every scrape, so
        # an id derived from the url would break the skip-before-spend gate.
        a = {"instagram_photo_id": "abc123", "url": "https://cdn.example/x.jpg?sig=v1"}
        b = {"instagram_photo_id": "abc123", "url": "https://cdn.example/x.jpg?sig=v2"}
        assert photo_id_for(a) == photo_id_for(b) == "abc123"

    def test_instagram_id_wins_over_any_google_style_fields(self):
        # A photo carrying both an Instagram id and Google-style fields must
        # dispatch to the Instagram id — the two derivations must never mix.
        photo = {
            "instagram_photo_id": "abc123", "photo_name": "places/X/photos/ref1",
            "url": "https://lh3.googleusercontent.com/token=s0",
        }
        assert photo_id_for(photo) == "abc123"

    def test_google_sources_are_unaffected_by_the_instagram_field(self):
        # A photo with no instagram_photo_id key falls through to the
        # untouched Google derivation.
        photo = {"photo_name": "places/X/photos/ref1", "url": "https://one"}
        assert photo_id_for(photo) == photo_id_for(dict(photo, url="https://two"))


class TestStoreKeys:
    def test_day_prefix_is_hive_partitioned(self):
        assert day_prefix("google_photos", "2026-07-26") == (
            "media/source=google_photos/dt=2026-07-26/"
        )

    @pytest.mark.parametrize(
        "content_type,ext",
        [("image/jpeg", "jpg"), ("image/png", "png"), ("image/webp", "webp"),
         ("image/jpeg; charset=binary", "jpg"), ("application/pdf", "bin"), (None, "bin")],
    )
    def test_extension_follows_content_type(self, content_type, ext):
        assert extension_for(content_type) == ext

    async def test_image_key_layout(self):
        s3 = _FakeS3()
        store = MediaArchiveStore(bucket="b", region="us-east-1", s3_client=s3)
        key = await store.put_image(
            prefix="media/source=google_photos/dt=2026-07-26/",
            venue_id="ven_a",
            photo_id="abc123",
            data=b"x",
            content_type="image/jpeg",
        )
        # Images sit in the venue's media/ subfolder; info/ holds everything
        # else, so a consumer can read one without listing the other.
        assert key == (
            "media/source=google_photos/dt=2026-07-26/venue_id=ven_a/media/abc123.jpg"
        )

    async def test_lists_day_partitions_ascending(self):
        s3 = _FakeS3()
        for day in ("2026-07-24", "2026-07-20", "2026-07-22"):
            s3.objects[f"media/source=google_photos/dt={day}/venue_id=v/a.jpg"] = b"x"
        store = MediaArchiveStore(bucket="b", region="us-east-1", s3_client=s3)
        assert await store.list_day_partitions("google_photos") == [
            "2026-07-20", "2026-07-22", "2026-07-24",
        ]

    async def test_missing_venue_reports_absent(self):
        store = MediaArchiveStore(bucket="b", region="us-east-1", s3_client=_FakeS3())
        assert await store.exists_for_venue("media/x/", "ven_a") is False

    async def test_a_listing_error_re_archives_rather_than_skipping(self):
        """Wrongly skipping loses data silently; wrongly re-fetching only costs
        money and shows up in the metrics. Fail toward the visible one."""
        class _Broken(_FakeS3):
            def list_objects_v2(self, **kw):
                raise RuntimeError("listing blew up")

        store = MediaArchiveStore(bucket="b", region="us-east-1", s3_client=_Broken())
        assert await store.exists_for_venue("media/x/", "ven_a") is False


class TestCostGuarantee:
    """The ordering that makes 'a skipped venue is free' true."""

    async def test_skipped_venue_never_reaches_google(self):
        svc = _service()
        svc._fake_s3.objects[
            "media/source=google_photos/dt=2026-07-26/venue_id=ven_a/old.jpg"
        ] = b"old"

        summary = await svc.run({"venue_ids": "ven_a"})

        assert summary["skipped_existing"] == 1
        assert svc.google_places_client.calls == 0, (
            "the existence check must run BEFORE the billable Google call"
        )

    async def test_overwrite_does_reach_google(self):
        svc = _service()
        svc._fake_s3.objects[
            "media/source=google_photos/dt=2026-07-26/venue_id=ven_a/old.jpg"
        ] = b"old"

        summary = await svc.run({"venue_ids": "ven_a", "overwrite": True})

        assert svc.google_places_client.calls == 1
        assert summary["archived"] == 1

    async def test_venue_without_place_id_never_reaches_google(self):
        svc = _service(venue_dao=_Dao(["ven_a"], place_ids={}))
        summary = await svc.run({"venue_ids": "ven_a"})
        assert summary["no_place_id"] == 1
        assert svc.google_places_client.calls == 0

    async def test_invalid_override_aborts_before_any_spend(self):
        svc = _service()
        with pytest.raises(InvalidArchivePath):
            await svc.run({"path_mode": "override", "path_override": "raw/nope"})
        assert svc.google_places_client.calls == 0


class TestPrefixResolution:
    async def test_append_latest_uses_the_newest_existing_day(self):
        svc = _service()
        for day in ("2026-07-20", "2026-07-24"):
            svc._fake_s3.objects[
                f"media/source=google_photos/dt={day}/venue_id=v/a.jpg"
            ] = b"x"
        prefix = await svc.resolve_prefix("google_photos", {"path_mode": "append_latest"})
        assert prefix == "media/source=google_photos/dt=2026-07-24/"

    async def test_append_latest_falls_back_to_a_new_run_when_empty(self):
        # Superseded by run scoping: with no partition to append to there is
        # nothing to extend, so the fallback opens a fresh versioned run. Asking
        # for "new_day" explicitly still writes a day partition (tested above).
        svc = _service()
        prefix = await svc.resolve_prefix("google_photos", {"path_mode": "append_latest"})
        assert prefix.startswith("retrieved/source=google_photos/year=")
        assert "run_id=" in prefix

    async def test_unknown_mode_is_rejected(self):
        svc = _service()
        with pytest.raises(InvalidArchivePath):
            await svc.resolve_prefix("google_photos", {"path_mode": "sideways"})

    async def test_unsupported_source_is_rejected(self):
        svc = _service()
        with pytest.raises(InvalidArchivePath):
            await svc.run({"sources": ["instagram"]})


class TestFailureIsolation:
    async def test_one_failing_venue_does_not_end_the_run(self):
        class _PartlyBroken(_CountingGoogle):
            async def get_place_photos(self, place_id, **kw):
                self.calls += 1
                if place_id == "place_ven_bad":
                    raise RuntimeError("google down")
                return [{"url": "u", "photo_name": "n"}]

        svc = _service(
            google_places_client=_PartlyBroken(),
            venue_dao=_Dao(["ven_bad", "ven_good"]),
        )
        summary = await svc.run({})

        assert summary["failed"] == 1
        assert summary["archived"] == 1
        assert summary["photos_stored"] == 1

    async def test_an_oversized_photo_is_counted_not_raised(self):
        svc = _service(downloader=_Downloader(size=99), max_photo_bytes=10)
        summary = await svc.run({"venue_ids": "ven_a"})
        assert summary["photo_failures"] == 1
        assert summary["photos_stored"] == 0

    async def test_unknown_ids_are_reported_not_fatal(self):
        svc = _service(venue_dao=_Dao(["ven_a"]))
        summary = await svc.run({"venue_ids": "ven_a, ven_ghost"})
        assert summary["unknown_venue_ids"] == ["ven_ghost"]
        assert summary["considered"] == 1
        assert summary["archived"] == 1


class TestManifest:
    async def test_records_attribution_for_every_stored_photo(self):
        google = _CountingGoogle([
            {"url": "u1", "photo_name": "n1", "author_name": "Ana"},
            {"url": "u2", "photo_name": "n2", "author_name": "Bruno"},
        ])
        svc = _service(google_places_client=google)
        await svc.run({"venue_ids": "ven_a"})

        manifests = [k for k in svc._fake_s3.objects if k.endswith("_manifest.json")]
        assert len(manifests) == 1
        body = json.loads(svc._fake_s3.objects[manifests[0]])
        assert [p["author_name"] for p in body["photos"]] == ["Ana", "Bruno"]
        assert body["venue_id"] == "ven_a"
        assert all(p["photo_id"] and p["content_type"] for p in body["photos"])

    async def test_no_manifest_when_nothing_was_stored(self):
        svc = _service(google_places_client=_CountingGoogle([]))
        await svc.run({"venue_ids": "ven_a"})
        assert not [k for k in svc._fake_s3.objects if k.endswith("_manifest.json")]


class _FakeIgClient:
    """Counts calls — the only thing that matters for the no_handle guarantee."""

    def __init__(self, posts=None):
        self.posts = posts if posts is not None else []
        self.calls: list[str] = []

    async def fetch_recent_posts(self, username, results_limit=10):
        self.calls.append(username)
        return list(self.posts)[:results_limit]


class _RecordingDownloader:
    """Counts calls per url — proves classification and storage share the
    SAME downloaded bytes rather than fetching an Instagram signed url
    twice (once for classify, once to store)."""

    def __init__(self, size=32):
        self.size = size
        self.calls: list[str] = []

    async def download(self, url, *, timeout=None, max_bytes=None):
        self.calls.append(url)
        data = b"\xff\xd8\xff" + b"x" * self.size
        if max_bytes is not None and len(data) > max_bytes:
            raise PhotoTooLarge("too big")
        return data, "image/jpeg"


class _RecordingClassifierClient:
    """Stands in for OpenAIPhotoClassifierClient: records exactly what
    string each photo reference was, so a test can prove no provider url
    (Instagram's signed CDN link) ever reached it."""

    def __init__(self):
        self.received_batches: list[list[str]] = []

    async def classify_photos(self, photo_urls, *, model=None, batch_size=10,
                              with_attributes=True):
        self.received_batches.append(list(photo_urls))
        return [
            {"index": i, "category": "interior", "confidence": 0.9}
            for i in range(len(photo_urls))
        ]


class TestClassifyFromBytesNotUrl:
    """Defect 1 (2026-08-07 RCA): OpenAI cannot fetch Instagram's signed CDN
    urls server-side (400 invalid_image_url) — the live archive path must
    hand the classifier the bytes it already downloaded, not the url."""

    async def test_the_classifier_never_receives_the_instagram_url(self):
        from app.services.photo_classification_service import PhotoClassificationService

        apify = _FakeIgClient(posts=[{
            "caption": "", "likes_count": 0, "comments_count": 0,
            "timestamp": "2026-08-01T00:00:00.000Z", "post_type": "image",
            "shortcode": "sc1", "permalink": "https://instagram.com/p/sc1/",
            "image_urls": [
                "https://instagram.fper12-1.fna.fbcdn.net/v/secret.jpg?sig=TOKEN"
            ],
        }])
        client = _RecordingClassifierClient()
        classifier = PhotoClassificationService(client=client)
        downloader = _RecordingDownloader()
        svc = _service(
            venue_dao=_Dao(["ven_a"], instagram_handles={"ven_a": "somehandle"}),
            apify_instagram_client=apify,
            downloader=downloader,
            photo_classifier=classifier,
        )

        summary = await svc.run({
            "source": "instagram_posts", "venue_ids": "ven_a",
            "max_photos_per_venue": 5,
        })

        assert summary["archived"] == 1
        assert client.received_batches, "the classifier was never called"
        sent = client.received_batches[0]
        assert len(sent) == 1
        assert sent[0].startswith("data:image/jpeg;base64,")
        assert "instagram.fper12-1.fna.fbcdn.net" not in sent[0]
        assert "TOKEN" not in sent[0]
        assert not any(ref.startswith("http") for batch in client.received_batches for ref in batch)

    async def test_the_photo_is_filed_under_its_classified_category(self):
        from app.services.photo_classification_service import PhotoClassificationService

        apify = _FakeIgClient(posts=[{
            "caption": "", "likes_count": 0, "comments_count": 0,
            "timestamp": "2026-08-01T00:00:00.000Z", "post_type": "image",
            "shortcode": "sc1", "permalink": "https://instagram.com/p/sc1/",
            "image_urls": ["https://instagram.fper12-1.fna.fbcdn.net/v/secret.jpg"],
        }])
        classifier = PhotoClassificationService(client=_RecordingClassifierClient())
        svc = _service(
            venue_dao=_Dao(["ven_a"], instagram_handles={"ven_a": "somehandle"}),
            apify_instagram_client=apify,
            downloader=_RecordingDownloader(),
            photo_classifier=classifier,
        )
        await svc.run({
            "source": "instagram_posts", "venue_ids": "ven_a",
            "max_photos_per_venue": 5,
        })

        image_keys = [k for k in svc._fake_s3.objects if k.endswith(".jpg")]
        assert image_keys and all("/media/interior/" in k for k in image_keys)

    async def test_the_download_is_not_repeated_for_storage(self):
        """Classification and storage must share the SAME bytes — a second
        download of an Instagram signed url risks the signature having
        already expired by the time storage gets to it."""
        from app.services.photo_classification_service import PhotoClassificationService

        apify = _FakeIgClient(posts=[{
            "caption": "", "likes_count": 0, "comments_count": 0,
            "timestamp": "2026-08-01T00:00:00.000Z", "post_type": "image",
            "shortcode": "sc1", "permalink": "https://instagram.com/p/sc1/",
            "image_urls": ["https://instagram.fper12-1.fna.fbcdn.net/v/secret.jpg"],
        }])
        downloader = _RecordingDownloader()
        classifier = PhotoClassificationService(client=_RecordingClassifierClient())
        svc = _service(
            venue_dao=_Dao(["ven_a"], instagram_handles={"ven_a": "somehandle"}),
            apify_instagram_client=apify,
            downloader=downloader,
            photo_classifier=classifier,
        )
        await svc.run({
            "source": "instagram_posts", "venue_ids": "ven_a",
            "max_photos_per_venue": 5,
        })

        assert downloader.calls == [
            "https://instagram.fper12-1.fna.fbcdn.net/v/secret.jpg"
        ], f"expected exactly one download, got {downloader.calls}"

    async def test_a_download_failure_before_classification_still_lets_storage_retry(self):
        """A photo whose pre-classification download fails must not be
        silently lost: storage gets its own attempt, exactly as it always
        has when there was no classifier bytes-first step at all."""
        from app.services.photo_classification_service import PhotoClassificationService

        class _FailsThenSucceeds:
            def __init__(self):
                self.calls = 0

            async def download(self, url, *, timeout=None, max_bytes=None):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient network blip")
                return b"\xff\xd8\xff-bytes", "image/jpeg"

        apify = _FakeIgClient(posts=[{
            "caption": "", "likes_count": 0, "comments_count": 0,
            "timestamp": "2026-08-01T00:00:00.000Z", "post_type": "image",
            "shortcode": "sc1", "permalink": "https://instagram.com/p/sc1/",
            "image_urls": ["https://instagram.fper12-1.fna.fbcdn.net/v/secret.jpg"],
        }])
        classifier = PhotoClassificationService(client=_RecordingClassifierClient())
        svc = _service(
            venue_dao=_Dao(["ven_a"], instagram_handles={"ven_a": "somehandle"}),
            apify_instagram_client=apify,
            downloader=_FailsThenSucceeds(),
            photo_classifier=classifier,
        )
        summary = await svc.run({
            "source": "instagram_posts", "venue_ids": "ven_a",
            "max_photos_per_venue": 5,
        })

        # The classify-time download failed (attempt 1); storage's own
        # attempt (attempt 2) still archived the photo, uncategorised.
        assert summary["archived"] == 1
        image_keys = [k for k in svc._fake_s3.objects if k.endswith(".jpg")]
        assert image_keys


class TestInstagramHandleCostsNothingWhenMissing:
    """The handle lookup is a database read that must resolve BEFORE any
    Apify call. Asserted on CALL COUNT, not just the `no_handle` outcome,
    because the ordering — not the label — is what protects the spend."""

    async def test_no_handle_reaches_zero_apify_calls(self):
        apify = _FakeIgClient()
        svc = _service(
            venue_dao=_Dao(["ven_a"]),  # no instagram_handles entry
            apify_instagram_client=apify,
        )
        summary = await svc.run({
            "source": "instagram_posts", "venue_ids": "ven_a",
            "max_photos_per_venue": 5,
        })
        assert summary["no_handle"] == 1
        assert apify.calls == [], "the handle lookup did not happen before the fetch"
        assert summary["archived"] == 0

    async def test_a_confirmed_handle_is_used(self):
        apify = _FakeIgClient(posts=[{
            "caption": "", "likes_count": 0, "comments_count": 0,
            "timestamp": "2026-08-01T00:00:00.000Z", "post_type": "image",
            "shortcode": "sc1", "permalink": "https://instagram.com/p/sc1/",
            "image_urls": ["https://instagram.cdn.example/sc1.jpg"],
        }])
        svc = _service(
            venue_dao=_Dao(["ven_a"], instagram_handles={"ven_a": "somehandle"}),
            apify_instagram_client=apify,
        )
        summary = await svc.run({
            "source": "instagram_posts", "venue_ids": "ven_a",
            "max_photos_per_venue": 5,
        })
        assert summary["no_handle"] == 0
        assert apify.calls == ["somehandle"]
        assert summary["archived"] == 1
