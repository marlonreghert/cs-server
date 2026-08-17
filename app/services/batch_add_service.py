"""Server-side batch venue-add: run a whole curated list through the proven
single-add path (AddVenueHandler.add) in one background job, persisting a
structured, pollable summary.

Design notes in plans/260705_batch-add-venues-endpoint.md. The key property:
each row reuses the exact single-add flow, so every existing guarantee holds
(no live/forecast retrieval at add time, deterministic venue_id, monthly-slot
reserve/release, geo-fallback + 24h undo, Google enrichment, and the PR #71
venue-search rate limiter that paces BestTime). No artificial sleeps here —
pacing is the client limiter's job.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Optional

from app.handlers.add_venue_handler import AddVenueByAddressRequest
from app.metrics import VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL
from app.models.batch_add import BatchAddRequest, BatchAddRow
from app.services import job_lock
from app.services.add_venue_outcome_classify import classify as _classify

logger = logging.getLogger(__name__)

# Storage-layer discriminator on the shared admin.venue_add_job_run table
# (plans/260814_venue-add-job-rds-tracking.md) — never surfaced in this
# service's own API response; app.dao.venue_add_job_store.shape_job_row
# strips it back out on every read.
JOB_TYPE = "batch"

# Single-flight lock name for batch-add (shares app/services/job_lock with the
# scheduler+admin guard). Two concurrent batch jobs would interleave their
# reserve→create sequences and race the same paid-add pacing/budget; only one
# batch runs at a time.
BATCH_ADD_LOCK = "batch_add"

# Outcomes that end the job early — "stop spending" states.
_STOP_OUTCOMES = {"quota_exhausted", "besttime_monthly_cap", "besttime_bad_response"}

# Coordinate resolution (Google) pacing + retry. A large batch that skips
# unresolved rows instantly would blaze through hundreds of Google calls per
# second the moment resolution starts failing, deepening a per-minute quota
# spike into a cascade that skips the rest of the list (prod 2026-07-05: 314
# rows skipped after a QPM spike ~row 120). Pace every Google-touching row and
# retry a transient miss with backoff so a momentary 429 recovers.
_GOOGLE_PACE_SECONDS = 0.3
_COORD_RETRY_BACKOFFS = (2.0, 5.0)


class BatchAddService:
    def __init__(
        self,
        handler,
        job_store,
        google_client=None,
        budget_service=None,
    ) -> None:
        self.handler = handler
        self.job_store = job_store
        self.google = google_client
        self.budget = budget_service
        # Keep task refs so background jobs are not garbage-collected.
        self._tasks: dict[str, asyncio.Task] = {}

    # ── budget snapshot ──────────────────────────────────────────────────────
    def _budget_snapshot(self) -> Optional[dict]:
        if self.budget is None:
            return None
        try:
            snap = self.budget.get_snapshot()
        except Exception:  # noqa: BLE001
            return None
        if snap is None:
            return None
        return {
            "year_month": getattr(snap, "year_month", None),
            "month_counter": getattr(snap, "month_counter", None),
            "quota": getattr(snap, "quota", None),
        }

    # ── persistence ──────────────────────────────────────────────────────────
    def _persist(self, job: dict) -> None:
        """Blocking write to the shared RDS job store (admin.venue_add_job_
        run) — best-effort, matching the pre-RDS Redis _save's own
        except-and-continue shape: a persistence hiccup must not crash the
        run itself. Now a materially bigger deal than a missed Redis write
        (RDS is the ONLY copy), so a sustained outage is visible via the
        dedicated counter instead of silently degrading to "job ran, nothing
        got recorded". Called directly (sync) from start_job()'s one initial
        save and from _on_done()'s crash-path save — both are plain
        synchronous call sites (start_job is not async, and a
        done_callback cannot await) — and wrapped in run_in_executor by
        _save() below for _run_job's per-row hot loop.

        Always re-stamps job_type right before the write: _on_done's
        crash-path job comes from self.get_job(job_id), and get_job() (via
        the store's shape_job_row) strips job_type on every read — it must
        never appear in an HTTP response. Re-saving that stripped dict
        as-is would silently NULL the column on the real table (an
        explicit-column upsert, not a partial patch) after the very first
        write."""
        job["job_type"] = JOB_TYPE
        try:
            self.job_store.save(job)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[BatchAddService] job persist failed: {e}")
            VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL.labels(job_type=JOB_TYPE).inc()

    async def _save(self, job: dict) -> None:
        """Off-loop write for _run_job's per-row hot path — a blocking
        SQLAlchemy call must never stall the event loop on every processed
        row (mirrors main.py:startup_essential's rehydrate_mirror
        run_in_executor pattern)."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._persist, job)

    def get_job(self, job_id: str) -> Optional[dict]:
        return self.job_store.get(job_id)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start_job(self, request: BatchAddRequest) -> dict:
        # Single-flight: refuse to start a second batch while one is running.
        # Acquired synchronously (no await before create_task) so a racing
        # request cannot slip in between the check and the task launch.
        if not job_lock.try_acquire(BATCH_ADD_LOCK):
            logger.warning(
                "[BatchAddService] batch add refused: another batch job is already running"
            )
            return {"status": "already_running", "total": len(request.venues)}
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "job_type": JOB_TYPE,
            "label": request.label,
            "status": "running",
            "total": len(request.venues),
            "processed": 0,
            "started_at": time.time(),
            "finished_at": None,
            "stopped_reason": None,
            "resolve_coords": request.resolve_coords,
            "summary": {},
            "results": [],
            "budget_before": self._budget_snapshot(),
            "budget_after": None,
        }
        self._persist(job)
        try:
            task = asyncio.create_task(self._run_job(job, list(request.venues)))
        except BaseException:
            # Launch failed — never leave the single-flight lock stuck.
            job_lock.release(BATCH_ADD_LOCK)
            raise
        self._tasks[job_id] = task
        task.add_done_callback(lambda t: self._on_done(job_id, t))
        return {"job_id": job_id, "total": job["total"], "status": "running"}

    def _on_done(self, job_id: str, task: asyncio.Task) -> None:
        self._tasks.pop(job_id, None)
        job_lock.release(BATCH_ADD_LOCK)  # release single-flight when the job ends
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.error(f"[BatchAddService] job {job_id} crashed: {exc!r}")
            job = self.get_job(job_id)
            if job and job.get("status") == "running":
                job["status"] = "failed"
                job["stopped_reason"] = f"{type(exc).__name__}: {exc}"[:300]
                job["finished_at"] = time.time()
                self._persist(job)

    def _already_active_id(self, name: str, address: str) -> Optional[str]:
        """Cheap address-hash store check (handler step 1) — no coords, no
        Google, no BestTime. Lets a re-run skip coord resolution entirely for
        rows already added, which both avoids wasted Google quota and removes
        the cascade trigger. Best-effort: any error falls through to the full
        flow."""
        try:
            vid = self.handler._lookup_cached_venue_id(name, address)
            if not vid:
                return None
            venue = self.handler.venue_dao.get_venue(vid)
            if venue is not None and venue.is_active():
                return vid
        except Exception:  # noqa: BLE001
            return None
        return None

    async def _resolve_coords(
        self, row: BatchAddRow
    ) -> tuple[Optional[str], Optional[float], Optional[float], bool]:
        """Resolve (place_id, lat, lng, coordinates_trusted) for one batch
        row. coordinates_trusted mirrors AddVenueHandler.add()'s trust gate
        (plans/260816_venue-address-cache-integrity.md): True when the
        coordinate is caller-supplied lat/lng, or resolved via a
        caller-supplied place_id (row.place_id already set before
        resolution); False when it had to come from a bare Text Search (see
        GooglePlacesAPIClient.search_place_id) with no caller anchor — the
        exact gap that let a submission permanently mis-link to an unrelated
        existing venue. bias_lat/bias_lng only steer Google's own relevance
        ranking; they are not a caller-verified location, so a
        Text-Search-resolved coordinate stays untrusted even when biased.
        """
        if row.venue_lat is not None and row.venue_lng is not None:
            return row.place_id, row.venue_lat, row.venue_lng, True
        if self.google is None:
            return row.place_id, None, None, True
        # Pace + retry: a transient Google QPM 429 returns None; back off and
        # retry rather than skip (which would speed the loop into a cascade).
        attempts = (0.0,) + _COORD_RETRY_BACKOFFS
        pid = row.place_id
        trusted = pid is not None
        for i, backoff in enumerate(attempts):
            if backoff:
                await asyncio.sleep(backoff)
            pid, lat, lng = await self.google.resolve_coordinates(
                row.venue_name,
                row.venue_address,
                place_id=pid,
                lat_bias=row.bias_lat,
                lng_bias=row.bias_lng,
            )
            if lat is not None and lng is not None:
                return pid, lat, lng, trusted
        return pid, None, None, trusted

    async def _run_job(self, job: dict, rows: list[BatchAddRow]) -> None:
        summary: dict[str, int] = {}
        for idx, row in enumerate(rows):
            t0 = time.perf_counter()
            result = {"index": idx, "venue_name": row.venue_name,
                      "venue_address": row.venue_address}
            used_google = False
            try:
                # Re-run fast-path: already-active by address hash → record
                # already_exists without any Google/BestTime call.
                pre_id = self._already_active_id(
                    row.venue_name, row.venue_address
                )
                if pre_id is not None:
                    result.update(outcome="already_exists", http=200,
                                  venue_id=pre_id, detail="address-hash hit")
                else:
                    used_google = (
                        self.google is not None
                        and (row.venue_lat is None or row.venue_lng is None)
                    )
                    place_id, lat, lng, coordinates_trusted = await self._resolve_coords(row)
                    if lat is None or lng is None:
                        result.update(outcome="skipped_unresolved_coords",
                                      http=None, venue_id=None,
                                      detail=f"place_id={place_id}")
                    else:
                        req = AddVenueByAddressRequest(
                            venue_name=row.venue_name,
                            venue_address=row.venue_address,
                            venue_lat=lat,
                            venue_lng=lng,
                            place_id=place_id,
                        )
                        outcome = await self.handler.add(
                            req, coordinates_trusted=coordinates_trusted
                        )
                        result.update(_classify(outcome))
            except Exception as e:  # noqa: BLE001
                logger.error(
                    f"[BatchAddService] row {idx} '{row.venue_name}' "
                    f"exception: {type(e).__name__}: {e}"
                )
                result.update(outcome="runner_exception", http=None,
                              venue_id=None,
                              detail=f"{type(e).__name__}: {e}"[:300])

            result["secs"] = round(time.perf_counter() - t0, 1)
            summary[result["outcome"]] = summary.get(result["outcome"], 0) + 1
            job["results"].append(result)
            job["processed"] = idx + 1
            job["summary"] = summary
            await self._save(job)

            # Steady pace on Google-touching rows: keeps the resolve rate under
            # the Places QPM quota so a burst never trips the cascade in the
            # first place (retry backoff above recovers if one slips through).
            if used_google and idx < len(rows) - 1:
                await asyncio.sleep(_GOOGLE_PACE_SECONDS)

            if result["outcome"] in _STOP_OUTCOMES:
                job["status"] = "stopped"
                job["stopped_reason"] = (
                    f"row {idx} '{row.venue_name}' -> {result['outcome']}"
                )
                job["finished_at"] = time.time()
                job["budget_after"] = self._budget_snapshot()
                await self._save(job)
                logger.warning(
                    f"[BatchAddService] job {job['job_id']} stopped: "
                    f"{job['stopped_reason']}"
                )
                return

        job["status"] = "done"
        job["finished_at"] = time.time()
        job["budget_after"] = self._budget_snapshot()
        await self._save(job)
        logger.info(
            f"[BatchAddService] job {job['job_id']} done: {summary}"
        )
