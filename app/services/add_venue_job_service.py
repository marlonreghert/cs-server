"""Single-venue add as a pollable background job.

Sibling to app/services/batch_add_service.py, not layered on it — the batch
service's single-flight lock and multi-row _classify() summary shape don't
fit a single add's concurrency model or its callers' expected response
shape. Wraps the existing, UNMODIFIED AddVenueHandler.add() in an
asyncio.Task and persists a pollable job doc to the shared RDS job store
(admin.venue_add_job_run — plans/260814_venue-add-job-rds-tracking.md), so
POST /venues/by-address's full BestTime-create + Google-enrichment +
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
import logging
import time
import uuid
from typing import Optional

from app.handlers.add_venue_handler import AddVenueByAddressRequest, AddVenueOutcome
from app.metrics import VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL
from app.services.add_venue_outcome_classify import classify

logger = logging.getLogger(__name__)

# Storage-layer discriminator on the shared admin.venue_add_job_run table
# (plans/260814_venue-add-job-rds-tracking.md) — never surfaced in this
# service's own API response; app.dao.venue_add_job_store.shape_job_row
# strips it back out on every read.
JOB_TYPE = "single"

# Response-shaping cap for list_recent() — unrelated to storage now that the
# backing table is unbounded (no TTL, no capped index): this purely bounds
# how many rows a single GET /venues/add-jobs/recent response can return,
# regardless of the caller's own `limit` query param.
ADD_VENUE_RECENT_JOBS_CAP = 50


class AddVenueJobService:
    def __init__(self, handler, job_store) -> None:
        self.handler = handler
        self.job_store = job_store
        # Keep task refs so background jobs are not garbage-collected.
        self._tasks: dict[str, asyncio.Task] = {}

    # ── persistence ──────────────────────────────────────────────────────────
    def _save(self, job: dict) -> None:
        """Blocking write to the shared RDS job store (admin.venue_add_job_
        run) — best-effort, matching the pre-RDS Redis _save's own
        except-and-continue shape: a persistence hiccup must not crash the
        run itself. Called directly (sync) from start_job() (not async — the
        POST route calls it without awaiting) and from _run_job()'s
        completion/crash paths; a single-add job has no per-row hot loop to
        off-load the way BatchAddService's _save does.

        Always re-stamps job_type right before the write: _run_job's
        completion/crash paths both re-fetch via self.get_job(job_id), and
        get_job() (via the store's shape_job_row) strips job_type on every
        read — it must never appear in an HTTP response. Re-saving that
        stripped dict as-is would silently NULL the column on the real table
        (an explicit-column upsert, not a partial patch) after the very
        first write."""
        job["job_type"] = JOB_TYPE
        try:
            self.job_store.save(job)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[AddVenueJobService] job persist failed: {e}")
            VENUE_ADD_JOB_PERSIST_FAILURES_TOTAL.labels(job_type=JOB_TYPE).inc()

    def get_job(self, job_id: str) -> Optional[dict]:
        return self.job_store.get(job_id)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start_job(self, request: AddVenueByAddressRequest) -> dict:
        """Mint a job id, persist the initial running doc, and launch the
        background task. Returns immediately — never awaits handler.add()."""
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "job_type": JOB_TYPE,
            "status": "running",
            "started_at": time.time(),
            # Captured up front purely so the recent-jobs list is
            # self-describing without a second lookup.
            "venue_name": request.venue_name,
            "venue_address": request.venue_address,
        }
        self._save(job)
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
        `outcome` label via the shared classification vocabulary.

        `ORDER BY started_at DESC LIMIT` on the durable admin.venue_add_job_
        run table replaces the old admin:add_venue_job_recent_v1 Redis LIST
        outright — no separate capped index structure to maintain, and (RDS
        having no TTL) nothing here ever "ages out" the way a Redis-backed
        job used to."""
        effective_limit = max(0, min(limit, ADD_VENUE_RECENT_JOBS_CAP))
        if effective_limit == 0:
            return []
        jobs = []
        for job in self.job_store.list_recent(effective_limit, job_type=JOB_TYPE):
            if job.get("status") == "done":
                outcome = AddVenueOutcome(
                    status_code=job.get("http_status"), body=job.get("result") or {}
                )
                job = {**job, "outcome": classify(outcome)["outcome"]}
            jobs.append(job)
        return jobs
