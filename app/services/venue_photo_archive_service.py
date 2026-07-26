"""Archive venue photos to the dated S3 media prefix.

An admin-triggered pipeline that downloads every available Google Places photo
for a chosen set of venues and stores the image bytes under a day-partitioned
`media/` prefix.

The ordering inside `_archive_venue` is the point of the whole design: the
already-archived check runs BEFORE the Google call, because Google bills per
photo request. A skip that happens after the fetch has already spent the money
it was meant to save.

Failure isolation is per venue and per photo — one bad venue, or one bad photo
within a venue, never aborts a run that may be paying for thousands of others.
"""
from __future__ import annotations

import hashlib
import logging
import posixpath
import time
from typing import Any, Iterable, Optional

import httpx

from app.dao.media_archive_store import MEDIA_ROOT
from app.metrics import (
    MEDIA_ARCHIVE_BYTES_STORED_TOTAL,
    MEDIA_ARCHIVE_LAST_SUCCESS_TIMESTAMP,
    MEDIA_ARCHIVE_PHOTOS_STORED_TOTAL,
    MEDIA_ARCHIVE_PHOTO_FAILURES_TOTAL,
    MEDIA_ARCHIVE_RUNS_TOTAL,
    MEDIA_ARCHIVE_RUN_DURATION_SECONDS,
    MEDIA_ARCHIVE_VENUES_TOTAL,
)
from app.utils.recife_time import recife_today

logger = logging.getLogger(__name__)

SOURCE_GOOGLE_PHOTOS = "google_photos"
SUPPORTED_SOURCES = (SOURCE_GOOGLE_PHOTOS,)

PATH_MODE_NEW_DAY = "new_day"
PATH_MODE_APPEND_LATEST = "append_latest"
PATH_MODE_OVERRIDE = "override"
PATH_MODES = (PATH_MODE_NEW_DAY, PATH_MODE_APPEND_LATEST, PATH_MODE_OVERRIDE)


class InvalidArchivePath(Exception):
    """The requested override prefix is unusable.

    Raised BEFORE any Google call: the writer's IAM policy only covers `media/*`,
    so a prefix outside it would fail every put — after the photos had already
    been paid for.
    """


class PhotoTooLarge(Exception):
    """A single image exceeded the per-photo byte cap and was not stored."""


def parse_venue_ids(raw: Any) -> list[str]:
    """Parse the comma-separated venue id field.

    Trims, drops empties, and de-duplicates while preserving the operator's
    order, so a hand-pasted list behaves predictably. A list is accepted too, for
    callers that already have one.
    """
    if raw is None:
        return []
    items: Iterable[str]
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        vid = str(item).strip()
        if vid and vid not in seen:
            seen.add(vid)
            out.append(vid)
    return out


def day_prefix(source: str, day: str) -> str:
    return f"{MEDIA_ROOT}/source={source}/dt={day}/"


def validate_override(prefix: Any) -> str:
    """Normalise and bounds-check an operator-supplied prefix.

    Must be non-empty and must resolve inside `media/`. Traversal is rejected on
    the NORMALISED path, so `media/../raw/` is caught rather than passing a naive
    prefix check.
    """
    text = str(prefix or "").strip()
    if not text:
        raise InvalidArchivePath("path_override is required when path_mode is 'override'")
    normalised = posixpath.normpath(text.lstrip("/"))
    if normalised in (".", "/") or not (
        normalised == MEDIA_ROOT or normalised.startswith(f"{MEDIA_ROOT}/")
    ):
        raise InvalidArchivePath(
            f"path_override must stay under '{MEDIA_ROOT}/': {prefix!r}"
        )
    return normalised.rstrip("/") + "/"


def photo_id_for(photo: dict) -> str:
    """Stable id for a photo.

    Derived from Google's photo resource name when available so the same photo
    keeps the same id across days and runs; falls back to the URL. Hashed to a
    fixed length because the raw resource name is long and not key-safe.
    """
    seed = photo.get("photo_name") or photo.get("url") or ""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


class HttpPhotoDownloader:
    """Downloads image bytes, bounded by a timeout and a byte cap.

    Reads the body in chunks and aborts as soon as the cap is exceeded, so a
    pathological image cannot consume memory or stall a run that has thousands of
    venues left to process.
    """

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient(follow_redirects=True)

    async def download(
        self, url: str, *, timeout: float = 15.0, max_bytes: Optional[int] = None
    ) -> tuple[bytes, str]:
        async with self._client.stream("GET", url, timeout=timeout) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "image/jpeg")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise PhotoTooLarge(f"{url} exceeds {max_bytes} bytes")
                chunks.append(chunk)
        return b"".join(chunks), content_type

    async def close(self) -> None:
        await self._client.aclose()


class VenuePhotoArchiveService:
    """Downloads venue photos into the dated media archive."""

    def __init__(
        self,
        *,
        google_places_client,
        venue_dao,
        media_store,
        downloader=None,
        max_photos_per_venue: int = 10,
        photo_timeout_seconds: float = 15.0,
        max_photo_bytes: int = 10 * 1024 * 1024,
        today_provider=None,
    ):
        self.google_places_client = google_places_client
        self.venue_dao = venue_dao
        self.media_store = media_store
        self.downloader = downloader or HttpPhotoDownloader()
        self.max_photos_per_venue = max_photos_per_venue
        self.photo_timeout_seconds = photo_timeout_seconds
        self.max_photo_bytes = max_photo_bytes
        self._today = today_provider or (lambda: recife_today().isoformat())

    # ── target prefix ────────────────────────────────────────────────────────
    async def resolve_prefix(self, source: str, config: dict) -> str:
        mode = str(config.get("path_mode") or PATH_MODE_NEW_DAY).strip()
        if mode not in PATH_MODES:
            raise InvalidArchivePath(
                f"unknown path_mode {mode!r}; expected one of {list(PATH_MODES)}"
            )
        if mode == PATH_MODE_OVERRIDE:
            return validate_override(config.get("path_override"))
        if mode == PATH_MODE_APPEND_LATEST:
            days = await self.media_store.list_day_partitions(source)
            if days:
                return day_prefix(source, days[-1])
            # Defined fallback rather than an error: the first-ever run has no
            # partition to append to, and failing it would be surprising.
            logger.info(
                "[VenuePhotoArchive] append_latest found no existing partition; "
                "falling back to today"
            )
        return day_prefix(source, self._today())

    # ── venue selection ──────────────────────────────────────────────────────
    def _select_venues(self, config: dict) -> tuple[list[str], list[str]]:
        """Return (venue_ids_to_process, unknown_ids)."""
        requested = parse_venue_ids(config.get("venue_ids"))
        try:
            catalog = list(self.venue_dao.list_active_venue_ids() or [])
        except Exception as e:
            logger.error(f"[VenuePhotoArchive] listing active venues failed: {e}")
            catalog = []
        if not requested:
            return catalog, []
        known = set(catalog)
        # An unknown id is reported, never fatal: one typo in a 40-id paste must
        # not throw away the whole run.
        selected = [vid for vid in requested if vid in known]
        unknown = [vid for vid in requested if vid not in known]
        return selected, unknown

    def _place_id_for(self, venue_id: str) -> Optional[str]:
        try:
            vibe = self.venue_dao.get_vibe_attributes(venue_id)
        except Exception as e:
            logger.warning(
                f"[VenuePhotoArchive] vibe-attrs read failed for {venue_id}: {e}"
            )
            return None
        return getattr(vibe, "google_place_id", None) if vibe else None

    # ── run ──────────────────────────────────────────────────────────────────
    async def run(self, config: Optional[dict] = None) -> dict:
        config = dict(config or {})
        sources = config.get("sources") or [SOURCE_GOOGLE_PHOTOS]
        source = str(sources[0])
        if source not in SUPPORTED_SOURCES:
            raise InvalidArchivePath(
                f"unsupported source {source!r}; expected one of {list(SUPPORTED_SOURCES)}"
            )

        started = time.perf_counter()
        # Resolve the prefix FIRST: an invalid override must abort before a
        # single billable Google call is made.
        prefix = await self.resolve_prefix(source, config)
        overwrite = bool(config.get("overwrite"))
        selected, unknown = self._select_venues(config)

        summary = {
            "source": source,
            "prefix": prefix,
            "overwrite": overwrite,
            "considered": len(selected),
            "archived": 0,
            "skipped_existing": 0,
            "no_place_id": 0,
            "failed": 0,
            "photos_stored": 0,
            "photo_failures": 0,
            "bytes_stored": 0,
            "unknown_venue_ids": unknown,
        }
        if unknown:
            logger.warning(
                f"[VenuePhotoArchive] {len(unknown)} unknown venue id(s) ignored: "
                f"{unknown[:10]}"
            )

        logger.info(
            f"[VenuePhotoArchive] starting: source={source} prefix={prefix} "
            f"venues={len(selected)} overwrite={overwrite}"
        )

        for venue_id in selected:
            try:
                await self._archive_venue(venue_id, source, prefix, overwrite, summary)
            except Exception as e:  # noqa: BLE001 — one venue must not end the run
                summary["failed"] += 1
                MEDIA_ARCHIVE_VENUES_TOTAL.labels(source=source, result="failed").inc()
                logger.error(f"[VenuePhotoArchive] venue {venue_id} failed: {e}")

        duration = time.perf_counter() - started
        MEDIA_ARCHIVE_RUN_DURATION_SECONDS.labels(source=source).observe(duration)
        MEDIA_ARCHIVE_RUNS_TOTAL.labels(source=source, status="success").inc()
        MEDIA_ARCHIVE_LAST_SUCCESS_TIMESTAMP.set_to_current_time()
        summary["duration_seconds"] = round(duration, 2)

        logger.info(
            f"[VenuePhotoArchive] done in {duration:.1f}s: "
            f"considered={summary['considered']} archived={summary['archived']} "
            f"skipped={summary['skipped_existing']} no_place_id={summary['no_place_id']} "
            f"failed={summary['failed']} photos={summary['photos_stored']}"
        )
        return summary

    async def _archive_venue(
        self, venue_id: str, source: str, prefix: str, overwrite: bool, summary: dict
    ) -> None:
        # 1. Skip BEFORE spending. This ordering is the cost guarantee.
        if not overwrite and await self.media_store.exists_for_venue(prefix, venue_id):
            summary["skipped_existing"] += 1
            MEDIA_ARCHIVE_VENUES_TOTAL.labels(
                source=source, result="skipped_existing"
            ).inc()
            logger.debug(f"[VenuePhotoArchive] {venue_id} already archived; skipping")
            return

        # 2. A venue with no place id can never be fetched — not a failure.
        place_id = self._place_id_for(venue_id)
        if not place_id:
            summary["no_place_id"] += 1
            MEDIA_ARCHIVE_VENUES_TOTAL.labels(source=source, result="no_place_id").inc()
            return

        # 3. Now, and only now, spend.
        try:
            photos = await self.google_places_client.get_place_photos(
                place_id,
                max_photos=self.max_photos_per_venue,
                include_ref=True,
            )
        except Exception as e:
            summary["failed"] += 1
            MEDIA_ARCHIVE_VENUES_TOTAL.labels(source=source, result="google_error").inc()
            logger.error(f"[VenuePhotoArchive] Google fetch failed for {venue_id}: {e}")
            return

        entries = []
        for photo in photos or []:
            entry = await self._store_photo(venue_id, source, prefix, photo, summary)
            if entry is not None:
                entries.append(entry)

        if entries:
            try:
                await self.media_store.put_manifest(
                    prefix=prefix,
                    venue_id=venue_id,
                    manifest={
                        "venue_id": venue_id,
                        "source": source,
                        "google_place_id": place_id,
                        "photos": entries,
                    },
                )
            except Exception as e:
                # The images are already stored; losing the manifest costs the
                # attribution, so it is loud but does not fail the venue.
                logger.error(
                    f"[VenuePhotoArchive] manifest write failed for {venue_id}: {e}"
                )

        summary["archived"] += 1
        MEDIA_ARCHIVE_VENUES_TOTAL.labels(source=source, result="archived").inc()

    async def _store_photo(
        self, venue_id: str, source: str, prefix: str, photo: dict, summary: dict
    ) -> Optional[dict]:
        url = photo.get("url")
        if not url:
            return None
        photo_id = photo_id_for(photo)
        try:
            data, content_type = await self.downloader.download(
                url,
                timeout=self.photo_timeout_seconds,
                max_bytes=self.max_photo_bytes,
            )
        except PhotoTooLarge as e:
            summary["photo_failures"] += 1
            MEDIA_ARCHIVE_PHOTO_FAILURES_TOTAL.labels(
                source=source, reason="too_large"
            ).inc()
            logger.warning(f"[VenuePhotoArchive] {venue_id} photo too large: {e}")
            return None
        except Exception as e:
            summary["photo_failures"] += 1
            MEDIA_ARCHIVE_PHOTO_FAILURES_TOTAL.labels(
                source=source, reason="download_error"
            ).inc()
            logger.warning(f"[VenuePhotoArchive] {venue_id} photo download failed: {e}")
            return None

        try:
            key = await self.media_store.put_image(
                prefix=prefix,
                venue_id=venue_id,
                photo_id=photo_id,
                data=data,
                content_type=content_type,
            )
        except Exception as e:
            summary["photo_failures"] += 1
            MEDIA_ARCHIVE_PHOTO_FAILURES_TOTAL.labels(
                source=source, reason="store_error"
            ).inc()
            logger.warning(f"[VenuePhotoArchive] {venue_id} photo store failed: {e}")
            return None

        summary["photos_stored"] += 1
        summary["bytes_stored"] += len(data)
        MEDIA_ARCHIVE_PHOTOS_STORED_TOTAL.labels(source=source).inc()
        MEDIA_ARCHIVE_BYTES_STORED_TOTAL.labels(source=source).inc(len(data))
        return {
            "photo_id": photo_id,
            "key": key,
            "content_type": content_type,
            "bytes": len(data),
            "author_name": photo.get("author_name"),
            "source_url": url,
            "photo_name": photo.get("photo_name"),
        }
