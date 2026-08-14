"""VenueAddJobStore — the Postgres system-of-record writer/reader for
venue-add job tracking (admin.venue_add_job_run).

Interface-matched to tests.venue_add_job_fake.InMemoryVenueAddJobStore (the
behaviour contract proven by BDD/unit tests) — mirrors app/dao/rds_venue_store.
py's conventions (synchronous SQLAlchemy `create_engine(..., pool_pre_ping=True,
future=True)`, `with self.engine.begin()/.connect()`). Like RdsVenueStore, this
SQL is validated by the post-provisioning smoke test, not by the offline suite
(no local Postgres in CI/dev) — the fake is what BDD/unit tests actually
exercise.

See plans/260814_venue-add-job-rds-tracking.md.

Both AddVenueJobService and BatchAddService share ONE table, discriminated by
`job_type` ("single" | "batch"). Their job dicts have historically had
DIFFERENT shapes when serialized straight to Redis (single-add's dict never
even carries batch-only keys like `summary`/`results`, and — critically —
never carries `finished_at`/`http_status`/`result`/`error` until the job
actually reaches a terminal state; batch's dict carries every one of its own
keys from the moment the job starts, including explicit `None` values). The
plan's hard constraint is that the five job-related HTTP endpoints' response
bodies stay byte-for-byte unchanged, so `shape_job_row` below reconstructs
that exact per-job_type shape (including "this key is simply absent" vs.
"this key is present with value null") on every read — not just "the same
data", but the same JSON shape a caller would see.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

# ── API-shape reconstruction ─────────────────────────────────────────────────
# AddVenueJobService's job dict (see its start_job/_run_job): always has these
# from the moment the job starts...
_SINGLE_ALWAYS = ("job_id", "status", "started_at", "venue_name", "venue_address")
# ...and gains these ONLY once the job actually reaches a terminal state (a
# crash sets finished_at/error; success sets finished_at/http_status/result).
# Omitted entirely (not null) while running — see AddVenueJobService._run_job.
_SINGLE_OPTIONAL = ("finished_at", "http_status", "result", "error")

# BatchAddService's job dict (see its start_job): every one of these keys is
# initialized up front, several to an explicit `None` — always present, even
# while still null.
_BATCH_ALWAYS = (
    "job_id", "label", "status", "total", "processed", "started_at", "finished_at",
    "stopped_reason", "resolve_coords", "summary", "results", "budget_before",
    "budget_after",
)

# Every physical column on admin.venue_add_job_run, for the real SELECT.
_COLUMNS = (
    "job_id", "job_type", "label", "status", "total", "processed", "venue_name",
    "venue_address", "http_status", "result", "error", "resolve_coords", "summary",
    "results", "stopped_reason", "budget_before", "budget_after", "started_at",
    "finished_at",
)

_JSONB_COLUMNS = ("result", "summary", "results", "budget_before", "budget_after")


def shape_job_row(row: dict) -> dict:
    """Reconstruct the exact API-facing job dict for one row — job_type is a
    pure storage-layer discriminator and NEVER appears in the output; a
    single-add row never carries batch-only fields (and vice versa); a
    single-add row additionally omits finished_at/http_status/result/error
    entirely until they are actually set. Shared by VenueAddJobStore (real)
    and tests.venue_add_job_fake.InMemoryVenueAddJobStore so the two can
    never silently drift on this contract. Defaults to the batch shape for
    an unrecognised/missing job_type rather than raising — a read must never
    break a poller."""
    if row.get("job_type") == "single":
        out = {k: row.get(k) for k in _SINGLE_ALWAYS}
        for k in _SINGLE_OPTIONAL:
            if row.get(k) is not None:
                out[k] = row[k]
        return out
    return {k: row.get(k) for k in _BATCH_ALWAYS}


def _epoch_to_dt(value):
    """Application layer always works in epoch floats (time.time(), matching
    what both services have always persisted); the timestamptz columns need a
    real datetime."""
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _dt_to_epoch(value):
    """Inverse of _epoch_to_dt — Postgres yields a datetime; normalize back to
    the epoch float the API response has always carried (byte-for-byte
    contract: started_at/finished_at are JSON numbers, never ISO strings)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    return value.timestamp()


def _row_to_job(row: dict) -> dict:
    row = dict(row)
    row["started_at"] = _dt_to_epoch(row.get("started_at"))
    row["finished_at"] = _dt_to_epoch(row.get("finished_at"))
    return shape_job_row(row)


class VenueAddJobStore:
    def __init__(self, sqlalchemy_url: str):
        self.engine = create_engine(sqlalchemy_url, pool_pre_ping=True, future=True)

    # ── writes ───────────────────────────────────────────────────────────────
    def save(self, job: dict) -> None:
        """Upsert the whole job doc as currently known — both the initial
        insert and every subsequent progress write go through this one
        method (matches how both services have always re-saved their full
        in-memory job dict on every persist, never a partial patch)."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO admin.venue_add_job_run "
                    "(job_id, job_type, label, status, total, processed, venue_name, "
                    " venue_address, http_status, result, error, resolve_coords, "
                    " summary, results, stopped_reason, budget_before, budget_after, "
                    " started_at, finished_at, updated_at) "
                    "VALUES (:job_id, :job_type, :label, :status, :total, :processed, "
                    " :venue_name, :venue_address, :http_status, CAST(:result AS jsonb), "
                    " :error, :resolve_coords, CAST(:summary AS jsonb), "
                    " CAST(:results AS jsonb), :stopped_reason, "
                    " CAST(:budget_before AS jsonb), CAST(:budget_after AS jsonb), "
                    " :started_at, :finished_at, now()) "
                    "ON CONFLICT (job_id) DO UPDATE SET "
                    "job_type=excluded.job_type, label=excluded.label, "
                    "status=excluded.status, total=excluded.total, "
                    "processed=excluded.processed, venue_name=excluded.venue_name, "
                    "venue_address=excluded.venue_address, http_status=excluded.http_status, "
                    "result=excluded.result, error=excluded.error, "
                    "resolve_coords=excluded.resolve_coords, summary=excluded.summary, "
                    "results=excluded.results, stopped_reason=excluded.stopped_reason, "
                    "budget_before=excluded.budget_before, budget_after=excluded.budget_after, "
                    "started_at=excluded.started_at, finished_at=excluded.finished_at, "
                    "updated_at=now()"
                ),
                {
                    "job_id": job["job_id"],
                    "job_type": job.get("job_type"),
                    "label": job.get("label"),
                    "status": job.get("status"),
                    "total": job.get("total", 1),
                    "processed": job.get("processed", 0),
                    "venue_name": job.get("venue_name"),
                    "venue_address": job.get("venue_address"),
                    "http_status": job.get("http_status"),
                    "result": (
                        json.dumps(job["result"]) if job.get("result") is not None else None
                    ),
                    "error": job.get("error"),
                    "resolve_coords": job.get("resolve_coords"),
                    # summary/results are NOT NULL columns — default to the
                    # empty container rather than ever passing a real NULL.
                    "summary": json.dumps(job.get("summary") or {}),
                    "results": json.dumps(job.get("results") or []),
                    "stopped_reason": job.get("stopped_reason"),
                    "budget_before": (
                        json.dumps(job["budget_before"])
                        if job.get("budget_before") is not None else None
                    ),
                    "budget_after": (
                        json.dumps(job["budget_after"])
                        if job.get("budget_after") is not None else None
                    ),
                    "started_at": _epoch_to_dt(job.get("started_at")),
                    "finished_at": _epoch_to_dt(job.get("finished_at")),
                },
            )

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self, job_id: str) -> Optional[dict]:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM admin.venue_add_job_run WHERE job_id=:job_id"
        with self.engine.connect() as conn:
            row = conn.execute(text(sql), {"job_id": job_id}).mappings().first()
            return _row_to_job(dict(row)) if row is not None else None

    def list_recent(self, limit: int, job_type: Optional[str] = None) -> list[dict]:
        sql = f"SELECT {', '.join(_COLUMNS)} FROM admin.venue_add_job_run"
        params: dict = {"limit": limit}
        if job_type is not None:
            sql += " WHERE job_type=:job_type"
            params["job_type"] = job_type
        sql += " ORDER BY started_at DESC LIMIT :limit"
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings()
            return [_row_to_job(dict(r)) for r in rows]

    # ── boot-time reconciliation ─────────────────────────────────────────────
    def reconcile_orphaned(self, reason: str) -> int:
        """Flip every row still `status='running'` to `'interrupted'` — a
        pure status-metadata UPDATE, never a resumed/re-run add. Called once
        at startup (main.py:startup_essential); deterministically correct in
        this single-process deployment (app/services/job_lock.py's own
        documented one-event-loop assumption): any row still "running" when
        this runs cannot belong to the process now starting. Returns the
        number of rows fixed, for the startup INFO log line."""
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE admin.venue_add_job_run SET status='interrupted', "
                    "stopped_reason=:reason, finished_at=now(), updated_at=now() "
                    "WHERE status='running'"
                ),
                {"reason": reason},
            )
            return result.rowcount
