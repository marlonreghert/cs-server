"""Unit tests for GET /admin/jobs/runs/{job_id} resolving a run record
across job-type services, not just the photo archive service it used to be
hardcoded to. See plans/260813_deep-review-corpus.md — the deep review crawl
needs its own run record retrievable by the same endpoint, since its job is
launched as a fire-and-forget background task that returns only a job_id.

A plain SimpleNamespace container + direct coroutine invocation, mirroring
tests/test_admin_venue_inventory.py's style — no FastAPI TestClient needed
for a handler this small.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import importlib

from fastapi import HTTPException
import pytest

# Neither `from app.routers import admin_trigger_router` NOR
# `import app.routers.admin_trigger_router as admin_trigger_router` reach the
# submodule: app/routers/__init__.py does `from app.routers.admin_trigger_
# router import router as admin_trigger_router`, which rebinds the PACKAGE
# ATTRIBUTE `app.routers.admin_trigger_router` to the APIRouter instance —
# and `import a.b.c as x` resolves via that attribute, not via sys.modules.
# importlib.import_module reads sys.modules directly, sidestepping the
# shadowing. Same workaround tests/test_admin_venue_inventory.py already
# established for this exact gotcha.
admin_trigger_router = importlib.import_module("app.routers.admin_trigger_router")


class _RunRecordService:
    def __init__(self, records=None):
        self._records = records or {}

    def get_run_record(self, job_id):
        return self._records.get(job_id)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_finds_a_record_on_the_first_service_in_the_list():
    admin_trigger_router.set_container(SimpleNamespace(
        venue_photo_archive_service=_RunRecordService({"job-1": {"job_id": "job-1", "source": "photo"}}),
        deep_review_crawl_service=None,
    ))
    record = _run(admin_trigger_router.get_job_run("job-1"))
    assert record == {"job_id": "job-1", "source": "photo"}


def test_finds_a_record_on_a_later_service_when_the_first_has_none():
    admin_trigger_router.set_container(SimpleNamespace(
        venue_photo_archive_service=_RunRecordService({}),  # configured, but no such job_id
        deep_review_crawl_service=_RunRecordService({"job-2": {"job_id": "job-2", "outcome": "partial"}}),
    ))
    record = _run(admin_trigger_router.get_job_run("job-2"))
    assert record == {"job_id": "job-2", "outcome": "partial"}


def test_an_unconfigured_service_is_skipped_not_raised():
    """A service that is entirely absent (None) must not stop the lookup
    from reaching the next candidate."""
    admin_trigger_router.set_container(SimpleNamespace(
        venue_photo_archive_service=None,
        deep_review_crawl_service=_RunRecordService({"job-3": {"job_id": "job-3"}}),
    ))
    record = _run(admin_trigger_router.get_job_run("job-3"))
    assert record == {"job_id": "job-3"}


def test_unknown_job_id_across_every_service_is_a_404():
    admin_trigger_router.set_container(SimpleNamespace(
        venue_photo_archive_service=_RunRecordService({}),
        deep_review_crawl_service=_RunRecordService({}),
    ))
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_trigger_router.get_job_run("no-such-job"))
    assert exc_info.value.status_code == 404


def test_both_services_entirely_unconfigured_is_also_a_404_not_503():
    """Behavior change from before this feature: the endpoint used to raise
    503 unconditionally when venue_photo_archive_service alone was absent,
    because it was the only assumed source. With more than one possible
    source, "not configured" and "no record under this id" collapse into
    the same 404 — there is no longer one owning service whose absence is
    exceptional."""
    admin_trigger_router.set_container(SimpleNamespace(
        venue_photo_archive_service=None,
        deep_review_crawl_service=None,
    ))
    with pytest.raises(HTTPException) as exc_info:
        _run(admin_trigger_router.get_job_run("anything"))
    assert exc_info.value.status_code == 404


def test_a_deep_review_budget_stopped_run_is_retrievable_through_the_endpoint():
    """The case plans/260813_deep-review-corpus.md's Desired Behavior #6
    exists for: an operator must be able to fetch exactly which venues a
    budget-stopped run did not reach, not just watch it disappear behind a
    job_id."""
    budget_stopped_record = {
        "job_id": "job-4", "outcome": "budget_stopped", "budget_stopped": True,
        "not_reached_venues": ["v1", "v2", "v3"],
    }
    admin_trigger_router.set_container(SimpleNamespace(
        venue_photo_archive_service=None,
        deep_review_crawl_service=_RunRecordService({"job-4": budget_stopped_record}),
    ))
    record = _run(admin_trigger_router.get_job_run("job-4"))
    assert record["not_reached_venues"] == ["v1", "v2", "v3"]
