"""S3 store for archived venue media (binary images + their manifests).

Sibling of `datalake_writer`, and deliberately a separate class: the lake writer
batches small JSON records into gzipped NDJSON, while this stores individual
binary objects addressed by venue. What they share is the credential story —
both build their boto3 client through `_build_s3_client`, so neither needs a
static access key and both inherit the bounded timeouts.

Layout (same Hive-style `key=value` convention as `raw/`, so `media/` is
discoverable by the same query tooling):

    retrieved/source=<s>/year=/month=/day=/run_id=<ulid>/venue_id=<v>/media/<category>/<photo_id>.jpg
    retrieved/source=<s>/year=/month=/day=/run_id=<ulid>/venue_id=<v>/info/place.json
    retrieved/source=<s>/year=/month=/day=/run_id=<ulid>/venue_id=<v>/info/_manifest.json

Media and info are split per venue so a consumer can read one without listing
the other: images are large and binary, everything else is a few KB of JSON.
`media/` is the superseded root — its partitions stay readable where they are.

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

MEDIA_ROOT = "media"          # superseded root, still listable
RETRIEVED_ROOT = "retrieved"  # current root
MEDIA_DIR = "media"           # per-venue subfolder for images
INFO_DIR = "info"             # per-venue subfolder for everything else
PLACE_INFO_NAME = "place.json"
MANIFEST_NAME = "_manifest.json"
LATEST_MARKER_NAME = "_latest.json"

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


_CATEGORY_SAFE = "abcdefghijklmnopqrstuvwxyz0123456789_-"


def _safe_category(category: Optional[str]) -> str:
    """Normalise a category into a key-safe folder name.

    Categories can come from a scrape, so they are untrusted input that ends up
    in an object key: anything unexpected collapses to `uncategorised` rather
    than producing a key with a slash or a space in it.
    """
    text = "".join(
        c if c in _CATEGORY_SAFE else "_"
        for c in str(category or "").strip().lower().replace(" ", "_")
    ).strip("_")
    return text or "uncategorised"


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

    async def list_run_prefixes(self, source: str) -> list[str]:
        """Every run prefix stored for a source, ascending — latest last.

        Walks the `year=/month=/day=/run_id=` levels with delimiter listings.
        Every level is zero-padded and the run id is itself time-ordered, so
        lexicographic order IS chronological order and the caller can take the
        last entry without parsing a date.

        Listing is the ONLY way this class can find the latest run: the writer
        role holds ListBucket but not GetObject, so reading a pointer object back
        is not an option (see the module docstring).
        """
        prefix = f"{RETRIEVED_ROOT}/source={source}/"
        level = [prefix]
        # year= -> month= -> day= -> run_id=
        for _ in range(4):
            children: list[str] = []
            for parent in level:
                children.extend(await self._list_child_prefixes(parent))
            if not children:
                return []
            level = sorted(children)
        return level

    async def _list_child_prefixes(self, prefix: str) -> list[str]:
        """Immediate `key=value/` children of a prefix, via a delimiter listing."""
        out: list[str] = []
        token: Optional[str] = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self.bucket, "Prefix": prefix, "Delimiter": "/",
            }
            if token:
                kwargs["ContinuationToken"] = token
            try:
                response = await asyncio.to_thread(self._s3.list_objects_v2, **kwargs)
            except Exception as e:
                logger.error(f"[MediaArchiveStore] listing {prefix!r} failed: {e}")
                return []
            for entry in response.get("CommonPrefixes", []) or []:
                child = entry.get("Prefix") or ""
                tail = child[len(prefix):].strip("/")
                if "=" in tail:  # skip stray non-partition keys
                    out.append(child)
            token = response.get("NextContinuationToken")
            if not response.get("IsTruncated") or not token:
                return out

    async def put_latest_marker(self, source: str, marker: dict) -> str:
        """Record where the most recent completed run for this source landed.

        Informational only — the pipeline resolves "latest" by listing, because
        it cannot read objects back. This exists for the analytics roles that
        can, so "where is the newest dump?" is answerable without a bucket walk.
        """
        key = f"{RETRIEVED_ROOT}/source={source}/{LATEST_MARKER_NAME}"
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(marker, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        return key

    async def latest_run_prefix(self, source: str) -> Optional[str]:
        """The newest run for a source, or None.

        Always the NEWEST, never "any run that has photos": a run is a snapshot,
        and blending runs would pair a fresh menu with a stale one and give no
        sign it had happened.
        """
        prefixes = await self.list_run_prefixes(source)
        return prefixes[-1] if prefixes else None

    async def list_venue_photos(
        self, prefix: str, venue_id: str, category: Optional[str] = None
    ) -> list[str]:
        """Image keys for one venue under a run, optionally one category.

        Only images: the `info/` sidecars (`place.json`, `_manifest.json`) live
        under the same venue prefix and must never be handed to a vision model
        as though they were photos.
        """
        folder = f"{MEDIA_DIR}/{_safe_category(category)}" if category else MEDIA_DIR
        search = f"{prefix}venue_id={venue_id}/{folder}/"
        keys: list[str] = []
        token: Optional[str] = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": search}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                response = await asyncio.to_thread(self._s3.list_objects_v2, **kwargs)
            except Exception as e:
                logger.error(
                    f"[MediaArchiveStore] listing photos for {venue_id} failed: {e}"
                )
                return []
            for entry in response.get("Contents", []) or []:
                key = entry.get("Key") or ""
                if key.rsplit(".", 1)[-1].lower() in _EXTENSIONS.values():
                    keys.append(key)
            token = response.get("NextContinuationToken")
            if not response.get("IsTruncated") or not token:
                return sorted(keys)

    async def list_run_venue_ids(self, prefix: str) -> list[str]:
        """Venue ids archived under a run, ascending."""
        out: list[str] = []
        for child in await self._list_child_prefixes(prefix):
            tail = child[len(prefix):].strip("/")
            if tail.startswith("venue_id="):
                out.append(tail[len("venue_id="):])
        return sorted(out)

    async def read_manifest(self, prefix: str, venue_id: str) -> Optional[dict]:
        """The stored manifest for one venue, or None.

        The one read that needs `s3:GetObject` — scoped to `retrieved/*` for
        exactly this, so an archived run's labels can be revised without
        re-fetching the photos from a provider. Returns None rather than
        raising: a venue with an unreadable manifest must not end a pass over
        the rest of the run.
        """
        key = f"{prefix}venue_id={venue_id}/{INFO_DIR}/{MANIFEST_NAME}"
        try:
            response = await asyncio.to_thread(
                self._s3.get_object, Bucket=self.bucket, Key=key
            )
            return json.loads(response["Body"].read())
        except Exception as e:
            logger.warning(f"[MediaArchiveStore] manifest read failed for {key}: {e}")
            return None

    async def presign(self, key: str, expires_in: int = 900) -> Optional[str]:
        """A time-limited GET url for one archived object.

        Needed because the vision model fetches the image itself, so the bucket
        stays private and the url carries the grant. Short-lived on purpose: it
        is handed to a third party.

        Returns None rather than raising — one unsignable photo must not lose
        the venue's other photos.
        """
        try:
            return await asyncio.to_thread(
                self._s3.generate_presigned_url,
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except Exception as e:
            logger.warning(f"[MediaArchiveStore] presign failed for {key}: {e}")
            return None

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
        category: Optional[str] = None,
    ) -> str:
        """Store one image, filed under its category when it has one.

        The category is a plain directory rather than a `key=value` partition:
        it is a container for browsing, not something the lake tooling should
        treat as a partition column.
        """
        folder = f"{MEDIA_DIR}/{_safe_category(category)}" if category else MEDIA_DIR
        key = (
            f"{prefix}venue_id={venue_id}/{folder}/"
            f"{photo_id}.{extension_for(content_type)}"
        )
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
        )
        return key

    async def put_info(self, *, prefix: str, venue_id: str, info: dict) -> str:
        """Store everything the source returned that is NOT an image.

        Written even when a venue yields no photos: the place data is the
        cheaper half of what was already paid for, and discarding it because
        the images failed would throw away the part that still succeeded.
        """
        key = f"{prefix}venue_id={venue_id}/{INFO_DIR}/{PLACE_INFO_NAME}"
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(info, ensure_ascii=False, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        return key

    async def put_manifest(self, *, prefix: str, venue_id: str, manifest: dict) -> str:
        """Store the per-venue manifest.

        Google requires author attribution to be displayed with its photos, so an
        image archived without its attribution is unusable. The manifest is what
        keeps the archive legitimate, not a nicety.
        """
        key = f"{prefix}venue_id={venue_id}/{INFO_DIR}/{MANIFEST_NAME}"
        await asyncio.to_thread(
            self._s3.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        return key
