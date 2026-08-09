"""Unit tests for app/services/crawl_schedule_sync.py — the §D "registry is
re-read and jobs re-registered when a target is written" reconciliation.

Uses a REAL (unstarted) `AsyncIOScheduler`: `add_job`/`get_jobs`/`remove_job`
work without `.start()`, so this exercises the actual APScheduler behavior
`CrawlScheduleSync` depends on rather than a hand-rolled scheduler fake.
"""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.dao.venue_repository import VenueRepository
from app.services.crawl_schedule_sync import CrawlScheduleSync, job_id_for
from tests.rds_fake import InMemoryRdsVenueStore


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

    dao.upsert_crawl_target("flippy", {"enabled": False})
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
