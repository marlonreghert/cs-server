"""S3 store for APP-SERVABLE venue media (currently: Instagram profile photos).

Deliberately a separate class from `MediaArchiveStore`, and the reason is a
compliance boundary rather than a style preference. `MediaArchiveStore` writes
the run-scoped `retrieved/` layout in the data lake, which is
**internal-use-only by design** (docs/venue-retrieval-storage.md §8): its
bucket blocks public access and the writer role is deliberately denied
`s3:GetObject`, so nothing there can ever be served to a user. This store
writes to a *different* bucket whose whole purpose is to be read by end users
through CloudFront. Sharing one class between the two would put a
publicly-served prefix one typo away from the internal-only one.

Layout::

    venue-profile-photos/<venue_id>/<sha256[:16]>.jpg

The key is **content-addressed**, and that is what makes the cache headers
safe. Because the bytes determine the key, an unchanged photo re-resolves to
the identical key and the identical URL — nothing is re-uploaded and no CDN or
device cache is invalidated for free — while changed bytes land on a *new* key
that no cache has ever seen. That is precisely the condition under which
`public, max-age=31536000, immutable` is correct rather than reckless: the
object at a given key is never rewritten, so a client may keep it forever.

The bucket stays private. Public reach comes from a CloudFront distribution
with an Origin Access Control (infra/media/), never from an object ACL, so
`cdn_base_url` — not an S3 URL — is the only address that works from a
browser or the app.

Credentials come from `_build_s3_client` (shared with the data lake writer):
the default credential chain resolves the EC2 instance role in production, so
there is no long-lived key to store, inject or rotate.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.dao.datalake_writer import _build_s3_client

logger = logging.getLogger(__name__)

# Every object this store writes lives under this one prefix, and the IAM
# policy in infra/media/ grants PutObject on exactly this prefix and nothing
# else. Widening it means a terraform apply FIRST — a write outside the policy
# fails after the Apify scrape has already been paid for.
PROFILE_PHOTO_ROOT = "venue-profile-photos"

# The number of leading sha256 hex characters that appear in the key. 16 hex
# chars is 64 bits: at catalog scale (thousands of objects) a collision is not
# a practical concern, and the full digest is kept in the RDS row anyway, which
# is what the unchanged-photo comparison actually uses.
CONTENT_HASH_KEY_LENGTH = 16

# One year, and `immutable` so a conditional revalidation request is never even
# sent. Safe only because the key is content-addressed — see the module
# docstring. Changing this string changes what every already-cached client
# does, so it lives here, in one place, and the BDD asserts on it.
PROFILE_PHOTO_CACHE_CONTROL = "public, max-age=31536000, immutable"


def profile_photo_key(venue_id: str, content_hash: str) -> str:
    """The content-addressed key for one venue's profile photo.

    `content_hash` may be the full sha256 hexdigest or an already-truncated
    one: the slice is idempotent, so both produce the identical key. The
    extension is always `.jpg` — that is fixed by the cross-repo contract two
    other repos are being built against. The object's real media type travels
    in its `Content-Type` header, which is what browsers and React Native
    actually honour, so a PNG or WebP avatar stored here still renders
    correctly.
    """
    return (
        f"{PROFILE_PHOTO_ROOT}/{venue_id}/"
        f"{content_hash[:CONTENT_HASH_KEY_LENGTH]}.jpg"
    )


class VenueMediaStore:
    """Binary persistence for media the app is allowed to display."""

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        cdn_base_url: str,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        s3_client: Any = None,
    ):
        self.bucket = bucket
        self.region = region
        # Trailing slashes are stripped once, here, so no caller has to guess
        # whether it should add one (and produce `https://host//key`).
        self.cdn_base_url = (cdn_base_url or "").rstrip("/")
        self._s3 = s3_client if s3_client is not None else _build_s3_client(
            region=region,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )

    def cdn_url(self, key: str) -> str:
        """The public URL for a stored key. The S3 URL is deliberately never
        constructed anywhere: the bucket is private, so an S3 URL would be a
        403 that looks like a working link."""
        return f"{self.cdn_base_url}/{key.lstrip('/')}"

    def profile_photo_key(self, venue_id: str, content_hash: str) -> str:
        return profile_photo_key(venue_id, content_hash)

    async def put_profile_photo(
        self,
        *,
        venue_id: str,
        content_hash: str,
        data: bytes,
        content_type: str = "image/jpeg",
    ) -> tuple[str, str]:
        """Store one profile photo and return `(key, cdn_url)`.

        Raises whatever boto3 raises. The caller must treat that as
        `upload_failed` for this venue only and keep going — a bucket-policy
        gap must not abort a run that is paying for every other venue in it.
        """
        key = profile_photo_key(venue_id, content_hash)
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type or "image/jpeg",
            CacheControl=PROFILE_PHOTO_CACHE_CONTROL,
        )
        logger.debug(
            f"[VenueMediaStore] stored {key} ({len(data)} bytes, {content_type})"
        )
        return key, self.cdn_url(key)
