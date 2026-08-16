"""Archive each venue's Instagram profile picture and make it app-servable.

See plans/260816_instagram-profile-photo-hero.md.

For every servable venue holding a confirmed Instagram handle: scrape the
profile through Apify, download the picture, content-address it, upload it to
the media bucket, and persist the resulting CloudFront URL to
`instagram.profile_photo`. The off-loop projector then mirrors that row into
`venue_profile_photo_v1:{venue_id}` so vibes_bot can put a photo on a list card
with no serve-time Google call at all.

## The ordering that carries the cost guarantee (do not rearrange)

Inherited verbatim from `VenuePhotoArchiveService`, whose module docstring
states it as a rule the archive pipeline must not violate:

1. Configuration is validated BEFORE any billed call, so a misconfigured run
   costs nothing.
2. **The already-stored freshness check runs BEFORE the paid scrape.** A skip
   that happens after the fetch has already spent the money it was meant to
   save.

Here that means the freshness gate runs during SELECTION — one bulk RDS read,
zero provider calls — and only the venues that survive it are ever handed to
Apify. `venue_profile_photo_apify_calls_total` is the proof: a venue skipped by
the gate cannot move that counter, which is why the acceptance criterion is
written against the counter and not against a log line.

The per-run cap is applied to the survivors of that gate, not to the raw
servable list. Capping earlier would let a catalog of already-fresh venues
consume the whole run budget and refresh nothing.

## What is never a failure

A venue with no handle, a profile with no picture, or a failed fetch produces
an ABSENCE — no row, no key, no error. Nothing partial is ever persisted: the
RDS row is written only after the S3 upload has returned, so a Redis key can
never point at an object that does not exist.

Failure isolation is per venue. One bad venue never aborts a run that is paying
for the others. The single exception is an exhausted Apify balance, which stops
the run deliberately — continuing would only produce more 402s.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.api.apify_instagram_client import ApifyCreditExhaustedError
from app.config import settings
from app.metrics import (
    VENUE_PROFILE_PHOTO_APIFY_CALLS_TOTAL,
    VENUE_PROFILE_PHOTO_BYTES_STORED_TOTAL,
    VENUE_PROFILE_PHOTO_ESTIMATED_COST_USD,
    VENUE_PROFILE_PHOTO_RUNS_TOTAL,
    VENUE_PROFILE_PHOTO_RUN_DURATION_SECONDS,
    VENUE_PROFILE_PHOTO_VENUES_TOTAL,
)
from app.models.instagram import VenueInstagramProfilePhoto
from app.services.pipeline_run_registry import new_run_id

logger = logging.getLogger(__name__)

PROFILE_PHOTO_TABLE = "instagram.profile_photo"

# Outcome buckets. Every venue the run touches lands in exactly one, and the
# set is closed — an outcome label that never appears in Prometheus is itself
# the diagnostic that its code path never ran.
OUTCOME_STORED = "stored"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_SKIPPED_FRESH = "skipped_fresh"
OUTCOME_NO_HANDLE = "no_handle"
OUTCOME_NO_PIC = "no_pic"
OUTCOME_FETCH_FAILED = "fetch_failed"
OUTCOME_DOWNLOAD_FAILED = "download_failed"
OUTCOME_UPLOAD_FAILED = "upload_failed"
OUTCOME_CREDIT_EXHAUSTED = "credit_exhausted"

# What we are willing to store and serve. A datacenter IP asking Instagram for
# an image very often gets an HTML login wall with HTTP 200, so a content-type
# allowlist is a correctness check, not paranoia: without it the "photo" on a
# venue card would be a few KB of markup.
#
# PNG and WebP are admitted even though the key's extension is fixed at `.jpg`
# by the cross-repo contract. The object's Content-Type is what a browser and
# React Native actually honour, so such a photo renders correctly; rejecting it
# would throw away a scrape that was already paid for over a cosmetic mismatch.
ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp"}
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_dt(value) -> Optional[datetime]:
    """Coerce an `updated_at` to an aware datetime. Real Postgres yields a
    tz-aware datetime; the in-memory fake and JSON yield an ISO string. A naive
    timestamp is read as UTC."""
    if value is None:
        return None
    ts = value
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


async def download_image(url: str, *, max_bytes: Optional[int] = None) -> tuple[bytes, str]:
    """Download an image, refusing to buffer more than `max_bytes` (+1).

    Streamed, and stopped one byte past the cap rather than read to completion:
    the cap exists to bound memory as well as to reject oversized files, and a
    caller that reads the whole body before checking its length has already
    lost the first half of that. The extra byte is what lets the SERVICE make
    the reject/accept decision — it can see the body exceeded the limit without
    this function deciding policy on its own.
    """
    limit = max_bytes or settings.instagram_profile_photo_max_bytes
    timeout = settings.instagram_profile_photo_download_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    break
            return b"".join(chunks), content_type


class VenueProfilePhotoService:
    def __init__(
        self,
        repo,
        apify_client,
        media_store,
        image_fetcher=None,
        now_fn=None,
        run_record_store=None,
    ):
        self.repo = repo  # VenueRepository: RDS system of record + serving DAO
        self.rds_store = getattr(repo, "rds_store", None)
        self.apify_client = apify_client
        self.media_store = media_store
        self._fetch_image = image_fetcher or download_image
        self._now = now_fn or _now
        # Same pattern as VenuePhotoArchiveService/DeepReviewCrawlService: an
        # in-memory dict by default (one process, no worker fan-out),
        # injectable so a future shared store can back all three.
        self._records = run_record_store if run_record_store is not None else {}

    # ── run records (GET /admin/jobs/runs/{job_id}) ──────────────────────────
    def get_run_record(self, job_id: str) -> Optional[dict]:
        try:
            return self._records.get(job_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[ProfilePhoto] run record read failed: {e}")
            return None

    def _save_run_record(self, job_id: str, record: dict) -> None:
        # A lost record must never fail a run that already spent money.
        try:
            self._records[job_id] = record
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[ProfilePhoto] run record write failed: {e}")

    # ── the run ─────────────────────────────────────────────────────────────
    async def run(self, config: Optional[dict] = None) -> dict:
        # `config` is accepted (the JOB_REGISTRY runner signature passes the
        # admin dialog's options through) and deliberately ignored: this job has
        # no per-run knobs. Every bound that matters — the enable flag, the
        # refresh window, the venue cap, the byte cap — is a setting, so an
        # operator cannot widen a spend limit from a trigger dialog.
        del config
        started = time.perf_counter()
        job_id = new_run_id()
        summary: dict = {
            "job_id": job_id,
            "status": "success",
            "stopped_reason": None,
            "venues_servable": 0,
            "venues_selected": 0,
            "apify_calls": 0,
            "bytes_stored": 0,
            "estimated_cost_usd": 0.0,
            "counts": {},
            "outcomes": {},
        }

        def record(venue_id: str, outcome: str) -> None:
            summary["outcomes"][venue_id] = outcome
            summary["counts"][outcome] = summary["counts"].get(outcome, 0) + 1
            VENUE_PROFILE_PHOTO_VENUES_TOTAL.labels(outcome=outcome).inc()

        # ── gate 1: configuration, BEFORE anything can be billed ────────────
        if not settings.instagram_profile_photo_enabled:
            # Inert, not broken: no scrape, no upload, no row. The flag ships
            # false and prod turns it on only after infra/media/ is applied and
            # verified, because a bucket-policy gap fails the write AFTER the
            # scrape has been paid for.
            return self._finish(summary, "disabled", started, job_id)

        if self.media_store is None or not settings.media_cdn_base_url:
            logger.warning(
                "[ProfilePhoto] not configured (media bucket / CDN base URL "
                "missing); no venue was scraped"
            )
            return self._finish(summary, "not_configured", started, job_id)

        if self.rds_store is None:  # pragma: no cover - defensive
            logger.error("[ProfilePhoto] no RDS store on the repository")
            return self._finish(summary, "error", started, job_id)

        # ── selection: free reads only ─────────────────────────────────────
        try:
            servable_ids = list(self.rds_store.list_servable_venue_ids())
            handles = self.rds_store.list_instagram_handles()
            existing = self.rds_store.get_enrichment_bulk(
                PROFILE_PHOTO_TABLE, servable_ids
            )
        except Exception as e:
            logger.error(f"[ProfilePhoto] selection failed; nothing spent: {e}")
            return self._finish(summary, "error", started, job_id)

        summary["venues_servable"] = len(servable_ids)
        refresh_cutoff = self._now() - timedelta(
            days=settings.instagram_profile_photo_refresh_days
        )

        due: list[tuple[str, str]] = []
        for venue_id in servable_ids:
            handle = handles.get(venue_id)
            if not handle:
                # Never fetched, never billed — the venue simply has nothing to
                # scrape. Handle discovery is a different pipeline's job.
                record(venue_id, OUTCOME_NO_HANDLE)
                continue
            row = existing.get(venue_id)
            if row is not None and self._is_fresh(row, refresh_cutoff):
                record(venue_id, OUTCOME_SKIPPED_FRESH)
                continue
            due.append((venue_id, handle))

        cap = settings.instagram_profile_photo_max_venues_per_run
        selected = due[:cap] if cap and cap > 0 else due
        summary["venues_selected"] = len(selected)
        logger.info(
            f"[ProfilePhoto] job={job_id} servable={len(servable_ids)} "
            f"due={len(due)} selected={len(selected)} "
            f"skipped_fresh={summary['counts'].get(OUTCOME_SKIPPED_FRESH, 0)} "
            f"no_handle={summary['counts'].get(OUTCOME_NO_HANDLE, 0)}"
        )

        # ── spend ──────────────────────────────────────────────────────────
        for venue_id, handle in selected:
            try:
                outcome = await self._process_venue(
                    venue_id, handle, existing.get(venue_id), summary
                )
            except ApifyCreditExhaustedError:
                # Stop the run. Every further venue would buy another 402.
                record(venue_id, OUTCOME_CREDIT_EXHAUSTED)
                summary["stopped_reason"] = OUTCOME_CREDIT_EXHAUSTED
                logger.error(
                    f"[ProfilePhoto] job={job_id} stopped at {venue_id}: "
                    "Apify credit exhausted"
                )
                break
            except Exception as e:
                # The unforeseen-error net. Every realistic failure is already
                # bucketed inside _process_venue; anything reaching here is a
                # bug, so the LOG (with the venue named) is the diagnostic and
                # the label only keeps the run summary total-preserving.
                logger.warning(
                    f"[ProfilePhoto] venue {venue_id} (@{handle}) failed "
                    f"unexpectedly: {e}"
                )
                outcome = OUTCOME_FETCH_FAILED
            record(venue_id, outcome)

        failed = sum(
            summary["counts"].get(o, 0)
            for o in (
                OUTCOME_FETCH_FAILED, OUTCOME_DOWNLOAD_FAILED, OUTCOME_UPLOAD_FAILED,
            )
        )
        if summary["stopped_reason"] == OUTCOME_CREDIT_EXHAUSTED:
            status = OUTCOME_CREDIT_EXHAUSTED
        elif failed:
            status = "partial"
        else:
            status = "success"
        return self._finish(summary, status, started, job_id)

    # ── one venue ───────────────────────────────────────────────────────────
    async def _process_venue(
        self, venue_id: str, handle: str, existing_row: Optional[dict], summary: dict
    ) -> str:
        result = await self.apify_client.fetch_profile(handle)
        # Counted only on a RETURN: a 402 never ran the actor, so it is not a
        # billed call and must not appear in the cost figure. A returned
        # dataset — even an empty or error one — did run.
        summary["apify_calls"] += 1
        VENUE_PROFILE_PHOTO_APIFY_CALLS_TOTAL.inc()
        cost = settings.apify_instagram_profile_cost_usd
        summary["estimated_cost_usd"] = round(summary["estimated_cost_usd"] + cost, 6)
        VENUE_PROFILE_PHOTO_ESTIMATED_COST_USD.inc(cost)

        if getattr(result, "error_code", None):
            logger.warning(
                f"[ProfilePhoto] {venue_id} (@{handle}) scrape failed: "
                f"{result.error_code}"
            )
            return OUTCOME_FETCH_FAILED

        pic_url = getattr(result, "profile_pic_url", None)
        if not pic_url:
            # An absence, not an error: this profile has no picture to store.
            logger.info(f"[ProfilePhoto] {venue_id} (@{handle}) has no profile picture")
            return OUTCOME_NO_PIC

        max_bytes = settings.instagram_profile_photo_max_bytes
        try:
            data, content_type = await self._fetch_image(pic_url, max_bytes=max_bytes)
        except Exception as e:
            logger.warning(f"[ProfilePhoto] {venue_id} download failed: {e}")
            return OUTCOME_DOWNLOAD_FAILED

        normalized_type = (content_type or "").split(";")[0].strip().lower()
        if normalized_type not in ALLOWED_IMAGE_CONTENT_TYPES:
            logger.warning(
                f"[ProfilePhoto] {venue_id} discarded: content-type "
                f"{normalized_type!r} is not an allowed image type"
            )
            return OUTCOME_DOWNLOAD_FAILED
        if not data or len(data) > max_bytes:
            logger.warning(
                f"[ProfilePhoto] {venue_id} discarded: {len(data)} bytes "
                f"exceeds the {max_bytes}-byte cap"
            )
            return OUTCOME_DOWNLOAD_FAILED

        content_hash = hashlib.sha256(data).hexdigest()
        key = self.media_store.profile_photo_key(venue_id, content_hash)
        photo_url = self.media_store.cdn_url(key)

        unchanged = (
            existing_row is not None
            and (existing_row.get("payload") or {}).get("content_hash") == content_hash
        )
        if unchanged:
            # Identical bytes -> identical key -> identical URL. Re-uploading
            # would replace an object with a byte-identical copy and buy
            # nothing; the row IS re-asserted, and that is deliberate — it
            # restarts the freshness clock so an unchanged avatar is not
            # re-scraped on every single cycle.
            self._persist(venue_id, handle, photo_url, key, content_hash,
                          normalized_type, len(data))
            return OUTCOME_UNCHANGED

        try:
            key, photo_url = await self.media_store.put_profile_photo(
                venue_id=venue_id,
                content_hash=content_hash,
                data=data,
                content_type=normalized_type,
            )
        except Exception as e:
            logger.error(f"[ProfilePhoto] {venue_id} upload failed: {e}")
            return OUTCOME_UPLOAD_FAILED

        summary["bytes_stored"] += len(data)
        VENUE_PROFILE_PHOTO_BYTES_STORED_TOTAL.inc(len(data))
        # Written only AFTER the upload returned: a row is a promise that the
        # object exists, and the projector turns that row into a URL the app
        # will render.
        self._persist(venue_id, handle, photo_url, key, content_hash,
                      normalized_type, len(data))
        return OUTCOME_STORED

    # ── helpers ─────────────────────────────────────────────────────────────
    def _is_fresh(self, row: dict, cutoff: datetime) -> bool:
        """A row younger than the refresh window, and not soft-deleted.

        The `deleted_at` check is belt-and-braces: `get_enrichment_bulk`
        already excludes soft-deleted rows on both the real store and the
        fake. It is kept because reading it the other way round — treating a
        withdrawn hero as "fresh" — would freeze that venue out of the
        pipeline permanently, and that is too quiet a failure to leave to one
        layer.

        An unparseable/absent `updated_at` is treated as NOT fresh: erring
        towards a re-scrape costs one Apify unit, while erring the other way
        would freeze a venue's hero forever.
        """
        if row.get("deleted_at") is not None:
            return False
        updated_at = _coerce_dt(row.get("updated_at"))
        if updated_at is None:
            return False
        return updated_at > cutoff

    def _persist(
        self, venue_id: str, handle: str, photo_url: str, key: str,
        content_hash: str, content_type: str, byte_size: int,
    ) -> None:
        self.repo.set_venue_profile_photo(
            VenueInstagramProfilePhoto(
                venue_id=venue_id,
                instagram_handle=handle,
                photo_url=photo_url,
                s3_key=key,
                content_hash=content_hash,
                content_type=content_type,
                byte_size=byte_size,
                fetched_at=self._now(),
            )
        )

    def _finish(self, summary: dict, status: str, started: float, job_id: str) -> dict:
        duration = time.perf_counter() - started
        summary["status"] = status
        summary["duration_seconds"] = round(duration, 3)
        VENUE_PROFILE_PHOTO_RUN_DURATION_SECONDS.observe(duration)
        VENUE_PROFILE_PHOTO_RUNS_TOTAL.labels(result=status).inc()
        self._save_run_record(job_id, summary)
        logger.info(f"[ProfilePhoto] job={job_id} {status}: {summary['counts']}")
        return summary
