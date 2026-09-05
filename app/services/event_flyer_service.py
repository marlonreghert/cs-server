"""Copy an accepted event's archived flyer into the app-servable media
bucket. plans/260905_events-serving-projection.md, Phase 1.

The bytes move S3 -> S3 through cs-server, never back through Instagram: the
source object is already paid for and archived under `retrieved/*` in the
data lake (cs-server's writer role can read that one prefix — see
infra/datalake/iam.tf's `ReadArchivedMedia` Sid, exercised in production
today by the admin cover-presign route), so this spends no Apify, Google,
BestTime or OpenAI quota either way. Mirrors the working precedent in
venue_profile_photo_service.py's `_process_venue`: fetch bytes,
content-hash, put to the media bucket, persist the CDN url.

## Candidacy — "lack a current flyer_url", literally

A candidate is an event whose `flyer_url` is currently falsy. An event that
already has one is DONE, permanently: there is no back-fill, no refresh
window, and no re-verification of already-copied bytes on every cycle — the
same "the row's mere existence is the cost gate, not its age" reasoning
migration 0043's own docstring gives for
`instagram.profile_photo`. `max_per_cycle` bounds how many such NEW
candidates one call attempts, so a large first-enable backlog cannot
monopolise a single projection tick; the remainder is picked up on a later
cycle, in a stable (event_id-sorted) order so the same backlog drains in the
same sequence every run.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from app.metrics import EVENT_FLYER_COPY_TOTAL

logger = logging.getLogger(__name__)

OUTCOME_COPIED = "copied"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_NO_KEY = "no_key"
OUTCOME_ARCHIVE_MISSING = "archive_missing"
OUTCOME_ACCESS_DENIED = "access_denied"
OUTCOME_FAILED = "failed"

ALL_OUTCOMES = (
    OUTCOME_COPIED, OUTCOME_UNCHANGED, OUTCOME_NO_KEY,
    OUTCOME_ARCHIVE_MISSING, OUTCOME_ACCESS_DENIED, OUTCOME_FAILED,
)


class EventFlyerService:
    def __init__(self, *, archive_store, media_store, rds_store):
        self.archive_store = archive_store
        self.media_store = media_store
        self.rds_store = rds_store

    async def copy_flyers(self, events: list[dict], *, max_per_cycle: int) -> dict:
        """`events` is this cycle's selected EVENT rows (one dict per
        `event_id` — never one per occurrence: a recurring event's flyer is
        copied once regardless of how many nights it expands into; the
        caller is responsible for that de-duplication before calling this).

        Returns `{"flyer_urls": {event_id: url}, "attempted": int,
        "counts": {outcome: int}}`. `flyer_urls` carries every event this
        call determined a url for — freshly copied, or confirmed unchanged
        — so the SAME cycle's occurrence payloads can carry it immediately,
        rather than waiting a cycle for the RDS round-trip to be re-read.
        """
        summary = {"attempted": 0, "counts": {o: 0 for o in ALL_OUTCOMES}}
        flyer_urls: dict[str, str] = {}
        candidates = sorted(
            (e for e in events if e.get("event_id") and not e.get("flyer_url")),
            key=lambda e: e["event_id"],
        )
        cap = max(max_per_cycle, 0)
        for event in candidates[:cap]:
            event_id = event["event_id"]
            summary["attempted"] += 1
            outcome, flyer_url = await self._copy_one(event)
            summary["counts"][outcome] += 1
            EVENT_FLYER_COPY_TOTAL.labels(outcome=outcome).inc()
            if flyer_url:
                flyer_urls[event_id] = flyer_url
        return {"flyer_urls": flyer_urls, **summary}

    async def _copy_one(self, event: dict) -> tuple[str, Optional[str]]:
        event_id = event["event_id"]
        cover_key = event.get("cover_photo_key")
        if not cover_key:
            return OUTCOME_NO_KEY, None

        try:
            data, content_type = await self.archive_store.read_image_bytes(cover_key)
        except FileNotFoundError:
            logger.warning(
                f"[EventFlyer] {event_id}: archived cover {cover_key!r} not found"
            )
            return OUTCOME_ARCHIVE_MISSING, None
        except Exception as e:
            logger.warning(f"[EventFlyer] {event_id}: archive read failed: {e}")
            return OUTCOME_FAILED, None

        content_hash = hashlib.sha256(data).hexdigest()
        existing_hash = event.get("flyer_content_hash")
        if existing_hash and existing_hash == content_hash:
            # The key is content-addressed, so an already-known matching
            # hash means the object already exists at this key — this is
            # the partial-failure-recovery path (a PUT that succeeded on an
            # earlier cycle whose RDS persist did not finish): recompute the
            # url locally and skip the re-upload entirely.
            key = self.media_store.event_flyer_key(event_id, content_hash)
            flyer_url = self.media_store.cdn_url(key)
            self._persist(event_id, flyer_url, key, content_hash, len(data))
            return OUTCOME_UNCHANGED, flyer_url

        try:
            key, flyer_url = await self.media_store.put_event_flyer(
                post_item_id=event_id, content_hash=content_hash,
                data=data, content_type=content_type,
            )
        except Exception as e:
            if "AccessDenied" in str(e):
                logger.error(
                    f"[EventFlyer] {event_id}: media bucket denied the flyer "
                    "write (access_denied) — has infra/media's terraform "
                    f"apply for event-flyers/* landed and been verified? {e}"
                )
                return OUTCOME_ACCESS_DENIED, None
            logger.error(f"[EventFlyer] {event_id}: upload failed: {e}")
            return OUTCOME_FAILED, None

        self._persist(event_id, flyer_url, key, content_hash, len(data))
        return OUTCOME_COPIED, flyer_url

    def _persist(
        self, event_id: str, flyer_url: str, key: str, content_hash: str, byte_size: int,
    ) -> None:
        from datetime import datetime, timezone

        self.rds_store.update_event(event_id, {
            "flyer_url": flyer_url,
            "flyer_s3_key": key,
            "flyer_content_hash": content_hash,
            "flyer_copied_at": datetime.now(timezone.utc),
            "flyer_byte_size": byte_size,
        })


__all__ = ["EventFlyerService", "ALL_OUTCOMES"]
