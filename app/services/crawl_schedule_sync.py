"""Keeps the running `AsyncIOScheduler`'s per-target crawl jobs in sync with
`events.crawl_target`. See plans/260809_scheduled-incremental-instagram-
crawl.md §D:

> Schedule changes take effect without a restart: the registry is re-read
> and jobs re-registered when a target is written, the same way main.py:420's
> comment describes for existing jobs.

`RefreshIntervalWatcher` (app/services/refresh_interval_watch.py) is the
precedent this mirrors, but it reschedules ONE fixed, already-registered job.
There is no fixed set of crawl-target jobs to reschedule — the set itself
changes as targets are created, deleted, enabled, or disabled — so this class
does something `RefreshIntervalWatcher` never has to: diff the WANTED set
(every enabled `crawl_target` row) against the scheduler's own EXISTING
`crawl_target:*` jobs, adding/updating/removing to match.

One APScheduler job per HANDLE, id `crawl_target:<handle>` — not one per
stream. §E's reels run is a SECOND Apify call made inside the same job
firing (`ScheduledInstagramCrawlService.run_target` handles both streams),
never a second cron trigger; the plan's own words are "each target carries
A crontab string" (singular).

`build_cron_trigger(cron, timezone=target's own timezone)` (app/services/
instagram_crawl_service.py — NOT `CronTrigger.from_crontab`, whose own
day-of-week field is a trap; see that function's module-level comment) is
where §D's two-timezone rule actually takes effect: the TRIGGER fires in the
target's local timezone (`America/Recife` by default — "Friday night" means
Friday night in Recife), while everything the fired job computes
(`onlyPostsNewerThan`, the stored cursor) is UTC, computed inside
`ScheduledInstagramCrawlService` at fire time, never here.
"""
from __future__ import annotations

import logging
import time

from app.metrics import (
    BACKGROUND_JOB_DURATION_SECONDS,
    BACKGROUND_JOB_LAST_RUN_TIMESTAMP,
    BACKGROUND_JOB_RUNS_TOTAL,
    JOB_LOCK_REJECTED_TOTAL,
)
from app.services import job_lock
from app.services.instagram_crawl_service import build_cron_trigger

logger = logging.getLogger(__name__)

JOB_ID_PREFIX = "crawl_target:"
SYNC_JOB_NAME = "crawl_schedule_sync"
CRAWL_JOB_NAME = "crawl_target_run"
DEFAULT_TIMEZONE = "America/Recife"


def job_id_for(handle: str) -> str:
    return f"{JOB_ID_PREFIX}{handle}"


def lock_name_for(handle: str) -> str:
    """Per-handle lock namespace, distinct from `job_lock.LOCKED_JOB_NAMES`'s
    fixed 4 names (those cover a different, static set of jobs). Guarantees
    §D's "two crawls of the same handle can never overlap" while leaving
    different handles free to run concurrently."""
    return f"crawl_target:{handle}"


class CrawlScheduleSync:
    """`venue_dao` needs only `list_crawl_targets(enabled=True)`.
    `crawl_service` needs only `run_due_targets([handle])`. `scheduler`
    needs `.get_jobs()`, `.add_job(...)`, `.remove_job(job_id)` — the same
    `AsyncIOScheduler` main.py already builds; this registers INTO it,
    never a second one."""

    def __init__(self, *, venue_dao, crawl_service, scheduler):
        self._venue_dao = venue_dao
        self._crawl_service = crawl_service
        self._scheduler = scheduler

    def _make_job_func(self, handle: str):
        crawl_service = self._crawl_service

        async def _run_one_target():
            lock_name = lock_name_for(handle)
            if not job_lock.try_acquire(lock_name):
                logger.warning(
                    f"[{CRAWL_JOB_NAME}] {handle} skipped: a crawl of this "
                    "handle is already running"
                )
                JOB_LOCK_REJECTED_TOTAL.labels(job_name=lock_name, source="scheduler").inc()
                return
            start_time = time.perf_counter()
            try:
                logger.info(f"[{CRAWL_JOB_NAME}] running scheduled crawl for {handle}")
                await crawl_service.run_due_targets([handle])
                duration = time.perf_counter() - start_time
                BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=CRAWL_JOB_NAME).observe(duration)
                BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=CRAWL_JOB_NAME, status="success").inc()
                BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=CRAWL_JOB_NAME).set_to_current_time()
            except Exception as e:
                duration = time.perf_counter() - start_time
                BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=CRAWL_JOB_NAME).observe(duration)
                BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=CRAWL_JOB_NAME, status="error").inc()
                logger.error(f"[{CRAWL_JOB_NAME}] {handle} failed: {e}")
            finally:
                job_lock.release(lock_name)

        return _run_one_target

    def sync_once(self) -> None:
        """Reconcile the scheduler's `crawl_target:*` jobs to match every
        currently-enabled target. A target whose `cron` fails to parse is
        logged and skipped (its previous registration, if any, is left
        alone) rather than aborting the whole sync — crontab validation is
        the admin router's job at WRITE time; this is a defensive backstop,
        never the primary guard."""
        try:
            targets = self._venue_dao.list_crawl_targets(enabled=True) or []
        except Exception as e:
            logger.error(f"[{SYNC_JOB_NAME}] failed to list crawl targets: {e}")
            return

        wanted: dict[str, dict] = {}
        for target in targets:
            handle = target.get("handle")
            cron = target.get("cron")
            if not handle or not cron:
                continue
            wanted[job_id_for(handle)] = target

        existing_ids = {
            job.id for job in self._scheduler.get_jobs() if job.id.startswith(JOB_ID_PREFIX)
        }

        for job_id in existing_ids - set(wanted):
            self._scheduler.remove_job(job_id)
            logger.info(f"[{SYNC_JOB_NAME}] removed {job_id} (disabled or deleted)")

        registered = 0
        for job_id, target in wanted.items():
            handle = target["handle"]
            tz_name = target.get("timezone") or DEFAULT_TIMEZONE
            try:
                trigger = build_cron_trigger(target["cron"], timezone=tz_name)
            except Exception as e:
                logger.error(
                    f"[{SYNC_JOB_NAME}] {handle}: invalid cron "
                    f"{target.get('cron')!r} ({e}); leaving its existing "
                    "schedule (if any) untouched"
                )
                continue
            self._scheduler.add_job(
                self._make_job_func(handle), trigger=trigger, id=job_id,
                name=f"Crawl target: {handle}", replace_existing=True,
            )
            registered += 1
        logger.info(f"[{SYNC_JOB_NAME}] synced {registered} enabled crawl target job(s)")

    async def run(self) -> None:
        """Periodic entry point with the standard background-job metrics,
        mirroring `RefreshIntervalWatcher.run`."""
        start_time = time.perf_counter()
        try:
            self.sync_once()
            duration = time.perf_counter() - start_time
            BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=SYNC_JOB_NAME).observe(duration)
            BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=SYNC_JOB_NAME, status="success").inc()
            BACKGROUND_JOB_LAST_RUN_TIMESTAMP.labels(job_name=SYNC_JOB_NAME).set_to_current_time()
        except Exception as e:
            duration = time.perf_counter() - start_time
            BACKGROUND_JOB_DURATION_SECONDS.labels(job_name=SYNC_JOB_NAME).observe(duration)
            BACKGROUND_JOB_RUNS_TOTAL.labels(job_name=SYNC_JOB_NAME, status="error").inc()
            logger.error(f"[{SYNC_JOB_NAME}] sync cycle failed: {e}")
