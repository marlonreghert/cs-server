"""Unit tests for app/services/crawl_schedule_sync.py — the §D "registry is
re-read and jobs re-registered when a target is written" reconciliation.

Uses a REAL (unstarted) `AsyncIOScheduler`: `add_job`/`get_jobs`/`remove_job`
work without `.start()`, so this exercises the actual APScheduler behavior
`CrawlScheduleSync` depends on rather than a hand-rolled scheduler fake.
"""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.dao.venue_repository import VenueRepository
from app.services import job_lock
from app.services.crawl_schedule_sync import CrawlScheduleSync, job_id_for
from app.services.instagram_crawl_service import (
    CrawlServiceConfig,
    ScheduledInstagramCrawlService,
    lock_name_for,
)
from tests.rds_fake import InMemoryRdsVenueStore


def setup_function(_):
    """`job_lock` is a module-level registry — persists across tests
    otherwise, same guard tests/test_job_lock.py and tests/test_instagram_
    crawl_service.py already need for the SAME module."""
    job_lock._running.clear()


def _dao():
    return VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())


class _StubCrawlService:
    def __init__(self):
        self.run_calls: list[list[str]] = []

    async def run_due_targets(self, handles):
        self.run_calls.append(list(handles))
        return {"targets": [], "stopped_early": False}


def test_sync_once_registers_a_job_per_enabled_target():
    dao = _dao()
    dao.upsert_crawl_target("a", {"kind": "venue", "cron": "0 22 * * *"})
    dao.upsert_crawl_target("b", {"kind": "venue", "cron": "0 6 1 * *"})
    scheduler = AsyncIOScheduler()
    sync = CrawlScheduleSync(venue_dao=dao, crawl_service=_StubCrawlService(), scheduler=scheduler)

    sync.sync_once()

    ids = {job.id for job in scheduler.get_jobs()}
    assert job_id_for("a") in ids
    assert job_id_for("b") in ids


def test_sync_once_never_registers_a_disabled_target():
    dao = _dao()
    dao.upsert_crawl_target("enabled_one", {"kind": "venue", "cron": "0 22 * * *"})
    dao.upsert_crawl_target("disabled_one", {"kind": "venue", "cron": "0 22 * * *", "enabled": False})
    scheduler = AsyncIOScheduler()
    sync = CrawlScheduleSync(venue_dao=dao, crawl_service=_StubCrawlService(), scheduler=scheduler)

    sync.sync_once()

    ids = {job.id for job in scheduler.get_jobs()}
    assert job_id_for("enabled_one") in ids
    assert job_id_for("disabled_one") not in ids


def test_sync_once_removes_a_job_whose_target_was_disabled_since_the_last_sync():
    dao = _dao()
    dao.upsert_crawl_target("flippy", {"kind": "venue", "cron": "0 22 * * *"})
    scheduler = AsyncIOScheduler()
    sync = CrawlScheduleSync(venue_dao=dao, crawl_service=_StubCrawlService(), scheduler=scheduler)
    sync.sync_once()
    assert job_id_for("flippy") in {job.id for job in scheduler.get_jobs()}

    dao.update_crawl_target("flippy", {"enabled": False})
    sync.sync_once()

    assert job_id_for("flippy") not in {job.id for job in scheduler.get_jobs()}


def test_sync_once_removes_a_job_whose_target_was_deleted():
    dao = _dao()
    dao.upsert_crawl_target("gone", {"kind": "venue", "cron": "0 22 * * *"})
    scheduler = AsyncIOScheduler()
    sync = CrawlScheduleSync(venue_dao=dao, crawl_service=_StubCrawlService(), scheduler=scheduler)
    sync.sync_once()
    assert job_id_for("gone") in {job.id for job in scheduler.get_jobs()}

    dao.delete_crawl_target("gone")
    sync.sync_once()

    assert job_id_for("gone") not in {job.id for job in scheduler.get_jobs()}


def test_sync_once_leaves_the_scheduler_untouched_for_an_unparseable_cron():
    dao = _dao()
    dao.upsert_crawl_target("badcron", {"kind": "venue", "cron": "not a crontab"})
    scheduler = AsyncIOScheduler()
    sync = CrawlScheduleSync(venue_dao=dao, crawl_service=_StubCrawlService(), scheduler=scheduler)

    # Must not raise — a defensive backstop, never the primary guard
    # (validation happens at admin-router write time).
    sync.sync_once()

    assert job_id_for("badcron") not in {job.id for job in scheduler.get_jobs()}


# ── cross-mechanism lock: a scheduled fire and an admin run-now of the SAME
# handle can never overlap ───────────────────────────────────────────────────
# #164's `run_crawl_target_now` called `run_target` directly, taking no lock
# at all, so it could race a scheduled fire of the same handle — two Apify
# bills, two cursor writes, last-write-wins on a field whose whole purpose is
# to never move backwards. This uses the REAL `ScheduledInstagramCrawlService`
# (not a stub) on BOTH sides — the scheduled job (via `CrawlScheduleSync`) and
# the admin run-now path (`service.start_run`) — because the guarantee being
# tested is that they share the exact same lock, which a stub could not prove.
class _GatedApifyClient:
    def __init__(self, gate: asyncio.Event):
        self._gate = gate
        self.calls = 0

    async def fetch_recent_posts(
        self, handle, results_limit=10, *, only_posts_newer_than=None, results_type="posts",
    ):
        self.calls += 1
        await self._gate.wait()
        return []


class _FakeBudgetDao:
    def __init__(self):
        self._counts: dict[str, int] = {}

    def current_year_month_utc(self, now=None):
        return "2026-08"

    def get_month_count(self, year_month):
        return self._counts.get(year_month, 0)

    def increment_month(self, year_month, n):
        self._counts[year_month] = self._counts.get(year_month, 0) + n
        return self._counts[year_month]


async def test_a_scheduled_fire_and_a_run_now_of_the_same_handle_cannot_overlap():
    dao = _dao()
    dao.upsert_crawl_target("racyhandle", {"kind": "venue", "cron": "0 22 * * *"})
    gate = asyncio.Event()
    apify = _GatedApifyClient(gate)
    service = ScheduledInstagramCrawlService(
        venue_dao=dao, apify_client=apify, budget_dao=_FakeBudgetDao(),
        config=CrawlServiceConfig(),
    )
    scheduler = AsyncIOScheduler()
    sync = CrawlScheduleSync(venue_dao=dao, crawl_service=service, scheduler=scheduler)
    sync.sync_once()

    # Simulate the scheduled job firing (exactly what APScheduler would call).
    job = scheduler.get_job(job_id_for("racyhandle"))
    scheduled_task = asyncio.create_task(job.func())
    await asyncio.sleep(0)  # let it acquire the lock and block on the gate

    assert job_lock.is_running(lock_name_for("racyhandle")) is True
    assert apify.calls == 1

    # An admin run-now for the SAME handle while the scheduled fire holds
    # the lock must be refused, not queued, not raced.
    result = await service.start_run("racyhandle")
    assert result == {"started": False, "reason": "already_running"}
    assert apify.calls == 1, "run-now must never reach the actor while the scheduled fire holds the lock"

    gate.set()
    await scheduled_task
    assert job_lock.is_running(lock_name_for("racyhandle")) is False

    # And now that the scheduled fire released the lock, run-now works —
    # `gate` is already set, so this second crawl completes immediately.
    result2 = await service.start_run("racyhandle")
    assert result2 == {"started": True}
    await asyncio.sleep(0)
    assert job_lock.is_running(lock_name_for("racyhandle")) is False
