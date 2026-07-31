"""Re-deriving must hand the vision model image BYTES, not a presigned url.

A presigned url signed with the instance role's temporary credentials carries
the entire STS session token in its query string — measured at 1,891 characters
against the real bucket. OpenAI rejects it with `invalid_image_url` even though
the same url returns 200 image/jpeg from inside the VPC, so a re-derive pass
classified nothing while reporting success: 25 venues, 0 photos, 0 failures.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from app.dao.media_archive_store import MediaArchiveStore


class _Body:
    def __init__(self, data): self._d = data
    def read(self): return self._d


class _FakeS3:
    def __init__(self, data=b"\xff\xd8\xffimage", content_type="image/jpeg", raises=False):
        self.data, self.content_type, self.raises = data, content_type, raises
        self.calls = []

    def get_object(self, Bucket=None, Key=None):
        self.calls.append(Key)
        if self.raises:
            raise RuntimeError("access denied")
        return {"Body": _Body(self.data), "ContentType": self.content_type}


def _store(fake):
    s = MediaArchiveStore(bucket="b", region="us-east-1")
    s._s3 = fake
    return s


class TestReadImageDataUri:
    def test_returns_a_data_uri_carrying_the_bytes(self):
        fake = _FakeS3(data=b"hello-bytes")
        uri = asyncio.run(_store(fake).read_image_data_uri("k.jpg"))
        assert uri.startswith("data:image/jpeg;base64,")
        payload = uri.split(",", 1)[1]
        assert base64.b64decode(payload) == b"hello-bytes"

    def test_honours_the_stored_content_type(self):
        uri = asyncio.run(_store(_FakeS3(content_type="image/png")).read_image_data_uri("k"))
        assert uri.startswith("data:image/png;base64,")

    def test_defaults_the_content_type_when_s3_omits_it(self):
        fake = _FakeS3()
        fake.get_object = lambda Bucket=None, Key=None: {"Body": _Body(b"x")}
        uri = asyncio.run(_store(fake).read_image_data_uri("k"))
        assert uri.startswith("data:image/jpeg;base64,")

    def test_carries_no_credentials(self):
        # The whole point: nothing signed, nothing expiring, no session token to
        # blow the url length budget.
        uri = asyncio.run(_store(_FakeS3()).read_image_data_uri("k.jpg"))
        for marker in ("AWSAccessKeyId", "x-amz-security-token", "Signature", "Expires"):
            assert marker not in uri

    def test_one_unreadable_photo_returns_none_rather_than_raising(self):
        # A venue must not lose its other photos to a single bad object.
        assert asyncio.run(_store(_FakeS3(raises=True)).read_image_data_uri("k")) is None


class TestRederiveUsesBytesNotPresign:
    def test_the_rederive_path_never_presigns(self):
        """Guards the regression directly: presign must not be back on this path."""
        import inspect
        from app.services.venue_photo_archive_service import VenuePhotoArchiveService
        src = inspect.getsource(VenuePhotoArchiveService._rederive_venue)
        assert "read_image_data_uri" in src
        assert "media_store.presign(" not in src, (
            "re-derive is presigning again — OpenAI rejects those urls"
        )
