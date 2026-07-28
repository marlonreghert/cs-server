"""Nothing a provider tells us about a photo is thrown away.

The asymmetry that motivates this: a field costs bytes in a JSON sidecar, but
recovering it costs a billed request — and some of it cannot be recovered at
all, because Google rotates photo tokens and an attribution uri or an authorship
signal is simply gone once the photo is re-fetched under a new token.

Two fields carry obligations rather than convenience:
  * `attributions` — Google's terms require showing attribution with a photo, so
    keeping only a display name makes the archive unusable compliantly.
  * `authorship`   — who took the photo, kept separate from `category` so a
    classifier assigning "menu" cannot destroy "the owner took this".
"""
from __future__ import annotations

import pytest

from app.api.apify_gmaps_extractor_client import ApifyGMapsExtractorClient


class TestApifyFidelity:
    def _photos(self, place):
        return ApifyGMapsExtractorClient(api_token="t")._archive_photos([place], 10)

    def test_authorship_is_separate_from_category(self):
        photos = self._photos({
            "title": "Tasquinha do Tio",
            "images": [{"imageUrl": "u1", "authorName": "Tasquinha do Tio"}],
        })
        p = photos[0]
        assert p["authorship"] == "by_owner"
        # `category` is where it is filed and may be reassigned later; the fact
        # of who took it must survive that.
        assert p["category"] == "by_owner"
        p["category"] = "menu"
        assert p["authorship"] == "by_owner"

    def test_a_visitor_photo_is_marked_as_such(self):
        photos = self._photos({
            "title": "Tasquinha do Tio",
            "images": [{"imageUrl": "u1", "authorName": "Maria das Gracas"}],
        })
        assert photos[0]["authorship"] == "by_visitor"

    def test_a_bare_url_claims_unknown_authorship_rather_than_guessing(self):
        photos = self._photos({"title": "X", "imageUrls": ["u1"]})
        assert photos[0]["authorship"] == "unknown"

    def test_the_author_profile_link_is_kept(self):
        photos = self._photos({
            "title": "X",
            "images": [{"imageUrl": "u1", "authorName": "A",
                        "authorUrl": "https://maps.google.com/contrib/123"}],
        })
        assert photos[0]["author_uri"] == "https://maps.google.com/contrib/123"

    def test_the_raw_provider_object_is_retained(self):
        image = {"imageUrl": "u1", "authorName": "A", "uploadedAt": "2020-01-01",
                 "somethingNew": {"we": "do not model this yet"}}
        photos = self._photos({"title": "X", "images": [image]})
        # A field we do not use today is still here when we want it.
        assert photos[0]["raw"]["somethingNew"] == {"we": "do not model this yet"}

    def test_an_unmodelled_field_survives_without_code_changes(self):
        photos = self._photos({
            "title": "X",
            "images": [{"imageUrl": "u1", "futureField": 42}],
        })
        assert photos[0]["raw"]["futureField"] == 42


class TestManifestEntryFidelity:
    """What actually lands in info/_manifest.json."""

    def _entry(self, photo, **over):
        import asyncio

        from app.services.venue_photo_archive_service import VenuePhotoArchiveService

        class _Store:
            async def put_image(self, **k): return "some/key.jpg"

        class _Dl:
            async def download(self, url, timeout=None, max_bytes=None):
                return b"xx", "image/jpeg"

        svc = VenuePhotoArchiveService(
            google_places_client=None, venue_dao=None,
            media_store=_Store(), downloader=_Dl(),
        )
        summary = {"photos_stored": 0, "bytes_stored": 0, "photo_failures": 0}
        return asyncio.run(
            svc._store_photo("v1", "google_photos", "p/", photo, summary)
        )

    def test_every_known_field_reaches_the_manifest(self):
        entry = self._entry({
            "url": "https://x/1.jpg",
            "author_name": "Jane", "author_uri": "https://maps/contrib/1",
            "author_photo_uri": "https://avatar/1",
            "attributions": [{"displayName": "Jane", "uri": "https://maps/contrib/1"}],
            "authorship": "by_visitor", "category": "menu",
            "uploaded_at": "2020-01-01T00:00:00Z",
            "width_px": 4032, "height_px": 3024,
            "photo_name": "places/X/photos/ref1",
            "raw": {"anything": "at all"},
        })
        for field in ("author_name", "author_uri", "author_photo_uri",
                      "attributions", "authorship", "category", "uploaded_at",
                      "width_px", "height_px", "photo_name", "raw"):
            assert field in entry, f"{field} was dropped on the way to the manifest"

    def test_attributions_are_kept_whole_not_just_the_first_name(self):
        # The uri is the half that makes attribution displayable.
        attributions = [
            {"displayName": "Jane", "uri": "https://maps/contrib/1"},
            {"displayName": "Bob", "uri": "https://maps/contrib/2"},
        ]
        entry = self._entry({"url": "https://x/1.jpg", "attributions": attributions})
        assert entry["attributions"] == attributions
        assert entry["attributions"][0]["uri"]

    def test_absent_fields_are_omitted_rather_than_stored_as_null(self):
        entry = self._entry({"url": "https://x/1.jpg"})
        assert "author_uri" not in entry
        assert entry["key"] and entry["photo_id"]

    def test_storage_facts_are_always_present(self):
        entry = self._entry({"url": "https://x/1.jpg"})
        for field in ("photo_id", "key", "content_type", "bytes", "source_url"):
            assert field in entry


class TestSummaryKeepsAuthorship:
    def _summary(self, entries):
        from app.services.venue_photo_archive_service import _media_summary
        return _media_summary(entries)

    def test_authorship_is_counted_separately_from_category(self):
        s = self._summary([
            {"category": "menu", "authorship": "by_owner"},
            {"category": "menu", "authorship": "by_visitor"},
            {"category": "vibe", "authorship": "by_visitor"},
        ])
        # Once a classifier owns `category`, this is the only remaining record
        # of who took the photos.
        assert s["by_authorship"] == {"by_owner": 1, "by_visitor": 2}
        assert s["by_category"] == {"menu": 2, "vibe": 1}

    def test_missing_authorship_counts_as_unknown(self):
        s = self._summary([{"category": "menu"}])
        assert s["by_authorship"] == {"unknown": 1}


class TestGoogleFieldMask:
    def test_dimensions_are_requested_since_they_are_free_in_the_same_call(self):
        from app.api.google_places_client import PHOTOS_FIELDS_MASK
        assert "photos.widthPx" in PHOTOS_FIELDS_MASK
        assert "photos.heightPx" in PHOTOS_FIELDS_MASK
        assert "photos.authorAttributions" in PHOTOS_FIELDS_MASK
