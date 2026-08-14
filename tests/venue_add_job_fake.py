"""In-memory fake for app.dao.venue_add_job_store.VenueAddJobStore — the
admin.venue_add_job_run behaviour contract, without a live Postgres. Mirrors
tests.rds_fake.InMemoryRdsVenueStore's fake-store convention (plain dict
storage instead of real SQL) so BDD/unit coverage never needs a live RDS,
per this repo's established testing convention for RDS-backed stores.

See plans/260814_venue-add-job-rds-tracking.md.
"""
from __future__ import annotations

import time
from typing import Optional

from app.dao.venue_add_job_store import shape_job_row


class InMemoryVenueAddJobStore:
    """Plain dict storage keyed by job_id. `save()` is an upsert (merges onto
    whatever is already stored, matching the real store's `ON CONFLICT
    (job_id) DO UPDATE`) — both services always pass the FULL job dict as
    currently known on every save, never a partial patch, so a plain
    dict-merge round-trips correctly either way.

    `shape_job_row` (shared with the real VenueAddJobStore) is what
    guarantees the two stores can never silently drift on the byte-for-byte
    API-shape contract (plans/260814_venue-add-job-rds-tracking.md) — job_type
    is stripped from every read, and a "single" row never carries batch-only
    fields (and vice versa).
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        # One-shot outage simulation for the "a persistence failure during a
        # run does not crash the job" BDD scenario: makes the next N save()
        # calls raise, then resumes succeeding. A sustained on/off toggle
        # (tests.rds_fake.InMemoryRdsVenueStore's set_unavailable/_guard
        # pattern) doesn't fit here — the scenario needs exactly one write to
        # fail while the rest of the run keeps succeeding, not a sustained
        # outage window.
        self._fail_next_saves = 0

    # ── test controls ────────────────────────────────────────────────────────
    def fail_next_save(self, times: int = 1) -> None:
        self._fail_next_saves += times

    # ── writes ───────────────────────────────────────────────────────────────
    def save(self, job: dict) -> None:
        if self._fail_next_saves > 0:
            self._fail_next_saves -= 1
            raise RuntimeError("venue_add_job_store unavailable (fake outage)")
        job_id = job["job_id"]
        row = dict(self.rows.get(job_id) or {})
        row.update(job)
        self.rows[job_id] = row

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self, job_id: str) -> Optional[dict]:
        row = self.rows.get(job_id)
        return shape_job_row(row) if row is not None else None

    def list_recent(self, limit: int, job_type: Optional[str] = None) -> list[dict]:
        rows = list(self.rows.values())
        if job_type is not None:
            rows = [r for r in rows if r.get("job_type") == job_type]
        rows.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return [shape_job_row(r) for r in rows[:limit]]

    def reconcile_orphaned(self, reason: str) -> int:
        """Flip every row still `status == "running"` to `"interrupted"` —
        deterministically correct in this single-process deployment
        (job_lock.py's own one-event-loop assumption): any row still
        "running" when this runs cannot belong to the process now starting.
        A pure status-metadata update; never touches AddVenueHandler, never
        re-runs anything."""
        fixed = 0
        for row in self.rows.values():
            if row.get("status") == "running":
                row["status"] = "interrupted"
                row["stopped_reason"] = reason
                row["finished_at"] = time.time()
                fixed += 1
        return fixed
