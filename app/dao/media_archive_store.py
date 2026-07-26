"""S3 store for archived venue media (binary images + their manifests).

Sibling of `datalake_writer`, and deliberately a separate class: the lake writer
batches small JSON records into gzipped NDJSON, while this stores individual
binary objects addressed by venue. What they share is the credential story —
both build their boto3 client through `_build_s3_client`, so neither needs a
static access key and both inherit the bounded timeouts.

Layout (same Hive-style `key=value` convention as `raw/`, so `media/` is
discoverable by the same query tooling):

    media/source=<source>/dt=<YYYY-MM-DD>/venue_id=<venue_id>/<photo_id>.jpg
    media/source=<source>/dt=<YYYY-MM-DD>/venue_id=<venue_id>/_manifest.json

The IAM policy backing this grants PutObject and a prefix-scoped ListBucket, but
NOT GetObject: the pipeline may see that an object exists and add new ones, and
can never read archived content back.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from app.dao.datalake_writer import _build_s3_client

logger = logging.getLogger(__name__)

MEDIA_ROOT = "media"
MANIFEST_NAME = "_manifest.json"

# Content type -> file extension. Anything unrecognised is stored as .bin rather
# than guessed, so a surprising type is visible in the key instead of silently
# mislabelled as a jpeg.
_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def extension_for(content_type: Optional[str]) -> str:
    return _EXTENSIONS.get((content_type or "").split(";")[0].strip().lower(), "bin")


class MediaArchiveStore:
    """Binary media persistence for the archive pipeline."""

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        s3_client: Any = None,
    ):
        self.bucket = bucket
        self.region = region
        self._s3 = s3_client if s3_client is not None else _build_s3_client(
            region=region,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )

    # ── reads (metadata only — ListBucket, never GetObject) ──────────────────
    async def list_day_partitions(self, source: str) -> list[str]:
        """Day values (`YYYY-MM-DD`) already present for a source, ascending.

        ISO dates sort lexicographically, so the caller can take the last entry
        as "latest" without parsing.
        """
        prefix = f"{MEDIA_ROOT}/source={source}/"
        try:
            response = await asyncio.to_thread(
                self._s3.list_objects_v2,
                Bucket=self.bucket,
                Prefix=prefix,
                Delimiter="/",
            )
        except Exception as e:
            logger.error(f"[MediaArchiveStore] listing day partitions failed: {e}")
            return []
        days = []
        for entry in response.get("CommonPrefixes", []) or []:
            tail = (entry.get("Prefix") or "")[len(prefix):].strip("/")
            if tail.startswith("dt="):
                days.append(tail[len("dt="):])
        return sorted(days)

    async def exists_for_venue(self, prefix: str, venue_id: str) -> bool:
        """True when anything is already stored for this venue under `prefix`.

        This is the cost gate: the caller uses it to skip a venue BEFORE paying
        Google. A listing error returns False (re-archive) rather than True,
        because wrongly skipping loses data silently while wrongly re-fetching
        only costs money and is visible in the metrics.
        """
        try:
            response = await asyncio.to_thread(
                self._s3.list_objects_v2,
                Bucket=self.bucket,
                Prefix=f"{prefix}venue_id={venue_id}/",
                MaxKeys=1,
            )
        except Exception as e:
            logger.error(
                f"[MediaArchiveStore] existence check failed for {venue_id}: {e}"
            )
            return False
        return bool(response.get("Contents"))

    # ── writes ───────────────────────────────────────────────────────────────
    async def put_image(
        self,
        *,
        prefix: str,
        venue_id: str,
        photo_id: str,
        data: bytes,
        content_type: str,
    ) -> str:
        key = (
            f"{prefix}venue_id={venue_id}/{photo_id}.{extension_for(content_type)}"
        )
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )
        return key

    async def put_manifest(self, *, prefix: str, venue_id: str, manifest: dict) -> str:
        """Store the per-venue manifest.

        Google requires author attribution to be displayed with its photos, so an
        image archived without its attribution is unusable. The manifest is what
        keeps the archive legitimate, not a nicety.
        """
        key = f"{prefix}venue_id={venue_id}/{MANIFEST_NAME}"
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        return key
