"""Shared per-job concurrency guard covering BOTH the APScheduler wrappers
(main.py) and admin-triggered runs (app/routers/admin_trigger_router.py).

`admin_trigger_router._running_jobs` only dedupes admin-triggered tasks against
each other, and APScheduler's `max_instances=1` only guards a scheduled job
against its own re-entrancy — neither stops an admin trigger from racing a
scheduled run of the SAME job (or vice versa), doubling the paid BestTime/
Google calls for that cycle. This module is the single shared lock namespace
both call sites check before starting `live_forecast`, `weekly_forecast`,
`rebuild_redis`, and `google_places`.

In-process only: the scheduler and the FastAPI admin routes share one event
loop/process in this deployment (see main.py / docker-compose.yml — one
`cs-server` container, no worker-process fan-out), so a plain module-level set
is sufficient; no cross-process/Redis lock is needed. `try_acquire`/`release`
are synchronous with no `await` between a caller's check and acquire, so there
is no race window within this process.
"""
from __future__ import annotations

# The 4 jobs the plan names as requiring the shared guard (paid BestTime/Google
# calls): the admin_trigger_router.JOB_REGISTRY key strings are canonical — the
# scheduler side (main.py) passes the SAME strings as `lock_name` to make_job.
LIVE_FORECAST = "live_forecast"
WEEKLY_FORECAST = "weekly_forecast"
GOOGLE_PLACES = "google_places"
REBUILD_REDIS = "rebuild_redis"
LOCKED_JOB_NAMES = frozenset({LIVE_FORECAST, WEEKLY_FORECAST, GOOGLE_PLACES, REBUILD_REDIS})

_running: set[str] = set()


def is_running(job_name: str) -> bool:
    """True if `job_name` is currently held (by either the scheduler or an
    admin trigger)."""
    return job_name in _running


def try_acquire(job_name: str) -> bool:
    """Attempt to mark `job_name` as running.

    Returns True when acquired (the caller now owns the lock and MUST call
    `release(job_name)` when done, e.g. via try/finally), False when another
    run already holds it.
    """
    if job_name in _running:
        return False
    _running.add(job_name)
    return True


def release(job_name: str) -> None:
    """Release `job_name`. Idempotent — releasing a name that is not held is
    a no-op, so a defensive double-release never raises."""
    _running.discard(job_name)
