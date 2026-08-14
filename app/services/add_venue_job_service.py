"""Single-venue add as a pollable background job.

Sibling to app/services/batch_add_service.py, not layered on it — the batch
service's single-flight lock and multi-row _classify() summary shape don't
fit a single add's concurrency model or its callers' expected response
shape. Wraps the existing, UNMODIFIED AddVenueHandler.add() in an
asyncio.Task and persists a pollable job doc to Redis, so POST
/venues/by-address's full BestTime-create + Google-enrichment +
Instagram-discovery duration no longer has to be held open on one HTTP
request. See plans/260813_add-venue-async-job.md.

No new single-flight lock: the per-venue VENUE_ADD_LOCK_KEY_V1 guard already
inside AddVenueHandler.add() is what prevents a real double-spend (it is
keyed per-venue, not per-caller, so it already covers concurrent calls from
this service, from BatchAddService, or from the synchronous route). This
service must never block one single-add job against another single-add job,
or against a running batch-add job — that would reintroduce the exact
regression the plan calls out: an operator adding several different venues
back-to-back from search results is the normal admin workflow.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from app.handlers.add_venue_handler import AddVenueByAddressRequest, AddVenueOutcome
from app.services.add_venue_outcome_classify import classify

logger = logging.getLogger(__name__)

JOB_KEY_FMT = "admin:add_venue_job:{job_id}"
# 24h — long enough to survive a refreshed admin tab, short enough that this
# doesn't need batch's 7-day campaign-review window.
JOB_TTL_SECONDS = 24 * 3600

RECENT_JOBS_KEY = "admin:add_venue_job_recent_v1"
ADD_VENUE_RECENT_JOBS_CAP = 50


class AddVenueJobService:
    def __init__(self, handler, redis_client) -> None:
        self.handler = handler
        self.redis = redis_client
        # Keep task refs so background jobs are not garbage-collected.
        self._tasks: dict[str, asyncio.Task] = {}

    # ── persistence ──────────────────────────────────────────────────────────
    def _save(self, job: dict) -> None:
        try:
            self.redis.setex(
                JOB_KEY_FMT.format(job_id=job["job_id"]),
                JOB_TTL_SECONDS,
                json.dumps(job, ensure_ascii=False),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[AddVenueJobService] job persist failed: {e}")

    def get_job(self, job_id: str) -> Optional[dict]:
        raw = self.redis.get(JOB_KEY_FMT.format(job_id=job_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    def _push_recent(self, job_id: str) -> None:
        try:
            self.redis.lpush(RECENT_JOBS_KEY, job_id)
            self.redis.ltrim(RECENT_JOBS_KEY, 0, ADD_VENUE_RECENT_JOBS_CAP - 1)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[AddVenueJobService] recent-jobs index update failed: {e}")

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start_job(self, request: AddVenueByAddressRequest) -> dict:
        """Mint a job id, persist the initial running doc, index it onto the
        recent-jobs list, and launch the background task. Returns immediately
        — never awaits handler.add()."""
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "status": "running",
            "started_at": time.time(),
            # Captured up front purely so the recent-jobs list is
            # self-describing without a second lookup.
            "venue_name": request.venue_name,
            "venue_address": request.venue_address,
        }
        self._save(job)
        self._push_recent(job_id)
        task = asyncio.create_task(self._run_job(job_id, request))
        self._tasks[job_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(job_id, None))
        return {"job_id": job_id, "status": "running"}

    async def _run_job(self, job_id: str, request: AddVenueByAddressRequest) -> None:
        try:
            outcome: AddVenueOutcome = await self.handler.add(request)
        except Exception as e:  # noqa: BLE001
            # A crashed runner must never fail silently and must never leave
            # a poller hanging forever (CLAUDE.md: background jobs must log
            # failures with enough context to troubleshoot).
            logger.error(
                f"[AddVenueJobService] job {job_id} crashed for "
                f"{request.venue_name!r} / {request.venue_address!r}: "
                f"{type(e).__name__}: {e}"
            )
            job = self.get_job(job_id) or {
                "job_id": job_id,
                "venue_name": request.venue_name,
                "venue_address": request.venue_address,
            }
            job["status"] = "failed"
            job["finished_at"] = time.time()
            job["error"] = f"{type(e).__name__}: {e}"[:300]
            self._save(job)
            return

        job = self.get_job(job_id) or {
            "job_id": job_id,
            "venue_name": request.venue_name,
            "venue_address": request.venue_address,
        }
        job["status"] = "done"
        job["finished_at"] = time.time()
        job["http_status"] = outcome.status_code
        job["result"] = outcome.body
        self._save(job)

    # ── monitoring ───────────────────────────────────────────────────────────
    def list_recent(self, limit: int = 20) -> list[dict]:
        """Newest-first recent jobs, capped at ADD_VENUE_RECENT_JOBS_CAP
        regardless of `limit`. Each "done" job is annotated with a short
        `outcome` label via the shared classification vocabulary. A job id
        that has aged out past its 24h TTL (or been trimmed) is silently
        dropped — expected and harmless for a best-effort recency list."""
        effective_limit = max(0, min(limit, ADD_VENUE_RECENT_JOBS_CAP))
        if effective_limit == 0:
            return []
        try:
            ids = self.redis.lrange(RECENT_JOBS_KEY, 0, effective_limit - 1)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[AddVenueJobService] recent-jobs list read failed: {e}")
            return []
        jobs = []
        for raw_id in ids:
            job_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else raw_id
            job = self.get_job(job_id)
            if job is None:
                continue
            if job.get("status") == "done":
                outcome = AddVenueOutcome(
                    status_code=job.get("http_status"), body=job.get("result") or {}
                )
                job = {**job, "outcome": classify(outcome)["outcome"]}
            jobs.append(job)
        return jobs
