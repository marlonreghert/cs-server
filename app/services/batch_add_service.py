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
import json
import logging
import time
import uuid
from typing import Optional

from app.handlers.add_venue_handler import (
    AddVenueByAddressRequest,
    AddVenueOutcome,
)
from app.models.batch_add import BatchAddRequest, BatchAddRow

logger = logging.getLogger(__name__)

JOB_KEY_FMT = "admin:batch_add_job:{job_id}"
JOB_TTL_SECONDS = 7 * 24 * 3600

# Outcomes that end the job early — "stop spending" states.
_STOP_OUTCOMES = {"quota_exhausted", "besttime_monthly_cap", "besttime_bad_response"}


def _classify(outcome: AddVenueOutcome) -> dict:
    """Map an AddVenueOutcome to a batch row result dict."""
    code = outcome.status_code
    body = outcome.body or {}
    status = body.get("status")
    res = {
        "http": code,
        "venue_id": body.get("venue_id"),
        "detail": None,
        "newly_linked": None,
        "match_reason": None,
    }
    if code == 201:
        res["outcome"] = (
            "created_recovered_timeout"
            if body.get("recovered_from_timeout")
            else "created"
        )
    elif code == 200 and status == "already_exists":
        res["outcome"] = "already_exists"
    elif code == 200 and status == "matched_via_geo_fallback":
        res["outcome"] = "geo_linked"
        res["newly_linked"] = body.get("newly_linked")
        res["match_reason"] = body.get("match_reason")
        res["venue_id"] = body.get("venue_id")
    elif code == 429:
        # Internal quota vs BestTime's own venue cap.
        res["outcome"] = (
            "besttime_monthly_cap"
            if "cap" in str(body.get("detail", "")).lower()
            else "quota_exhausted"
        )
        res["detail"] = str(body.get("detail"))
    elif code == 502:
        detail = str(body.get("detail", ""))
        if "unparseable" in detail.lower():
            res["outcome"] = "besttime_bad_response"
        elif "candidates_seen" in body or "rejected the address" in detail:
            res["outcome"] = "besttime_rejected_no_geo_match"
            res["detail"] = body.get("besttime_message") or detail
        else:
            res["outcome"] = "besttime_error"
            res["detail"] = detail
    else:
        res["outcome"] = f"http_{code}"
        res["detail"] = str(body)[:300]
    return res


class BatchAddService:
    def __init__(
        self,
        handler,
        redis_client,
        google_client=None,
        budget_service=None,
    ) -> None:
        self.handler = handler
        self.redis = redis_client
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
    def _save(self, job: dict) -> None:
        try:
            self.redis.setex(
                JOB_KEY_FMT.format(job_id=job["job_id"]),
                JOB_TTL_SECONDS,
                json.dumps(job, ensure_ascii=False),
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[BatchAddService] job persist failed: {e}")

    def get_job(self, job_id: str) -> Optional[dict]:
        raw = self.redis.get(JOB_KEY_FMT.format(job_id=job_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start_job(self, request: BatchAddRequest) -> dict:
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
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
        self._save(job)
        task = asyncio.create_task(self._run_job(job, list(request.venues)))
        self._tasks[job_id] = task
        task.add_done_callback(lambda t: self._on_done(job_id, t))
        return {"job_id": job_id, "total": job["total"], "status": "running"}

    def _on_done(self, job_id: str, task: asyncio.Task) -> None:
        self._tasks.pop(job_id, None)
        exc = task.exception() if not task.cancelled() else None
        if exc is not None:
            logger.error(f"[BatchAddService] job {job_id} crashed: {exc!r}")
            job = self.get_job(job_id)
            if job and job.get("status") == "running":
                job["status"] = "failed"
                job["stopped_reason"] = f"{type(exc).__name__}: {exc}"[:300]
                job["finished_at"] = time.time()
                self._save(job)

    async def _resolve_coords(
        self, row: BatchAddRow
    ) -> tuple[Optional[str], Optional[float], Optional[float]]:
        if row.venue_lat is not None and row.venue_lng is not None:
            return row.place_id, row.venue_lat, row.venue_lng
        if self.google is None:
            return row.place_id, None, None
        return await self.google.resolve_coordinates(
            row.venue_name,
            row.venue_address,
            place_id=row.place_id,
            lat_bias=row.bias_lat,
            lng_bias=row.bias_lng,
        )

    async def _run_job(self, job: dict, rows: list[BatchAddRow]) -> None:
        summary: dict[str, int] = {}
        for idx, row in enumerate(rows):
            t0 = time.perf_counter()
            result = {"index": idx, "venue_name": row.venue_name,
                      "venue_address": row.venue_address}
            try:
                place_id, lat, lng = await self._resolve_coords(row)
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
                    outcome = await self.handler.add(req)
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
            self._save(job)

            if result["outcome"] in _STOP_OUTCOMES:
                job["status"] = "stopped"
                job["stopped_reason"] = (
                    f"row {idx} '{row.venue_name}' -> {result['outcome']}"
                )
                job["finished_at"] = time.time()
                job["budget_after"] = self._budget_snapshot()
                self._save(job)
                logger.warning(
                    f"[BatchAddService] job {job['job_id']} stopped: "
                    f"{job['stopped_reason']}"
                )
                return

        job["status"] = "done"
        job["finished_at"] = time.time()
        job["budget_after"] = self._budget_snapshot()
        self._save(job)
        logger.info(
            f"[BatchAddService] job {job['job_id']} done: {summary}"
        )
