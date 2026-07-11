"""Unit tests for the shared scheduler+admin job concurrency guard
(app/services/job_lock.py).

The scheduler (main.py make_job's lock_name) and the admin trigger endpoint
(app/routers/admin_trigger_router.py trigger_job) both check/acquire the same
named lock for live_forecast, weekly_forecast, google_places, and
rebuild_redis so a paid-refresh job can never run twice concurrently across
the two trigger paths.
"""
import importlib
from unittest.mock import MagicMock

import pytest

from app.services import job_lock

# app/routers/__init__.py re-exports the router INSTANCE under this same name
# (`from app.routers.admin_trigger_router import router as admin_trigger_router`),
# shadowing the submodule on the `app.routers` package namespace — importlib
# goes straight to sys.modules and sidesteps that (same pattern as
# tests/test_projector_and_serving_bulk_reads.py).
admin_trigger_router = importlib.import_module("app.routers.admin_trigger_router")


def setup_function(_):
    """Every job_lock test starts from a clean module-level registry —
    module state persists across tests otherwise (no per-test instance)."""
    job_lock._running.clear()
    admin_trigger_router._running_jobs.clear()


def test_try_acquire_succeeds_when_free():
    assert job_lock.try_acquire("live_forecast") is True
    assert job_lock.is_running("live_forecast") is True


def test_try_acquire_fails_when_already_held():
    assert job_lock.try_acquire("live_forecast") is True
    assert job_lock.try_acquire("live_forecast") is False


def test_release_frees_the_lock():
    job_lock.try_acquire("live_forecast")
    job_lock.release("live_forecast")
    assert job_lock.is_running("live_forecast") is False
    assert job_lock.try_acquire("live_forecast") is True  # re-acquirable


def test_release_is_idempotent_on_an_unheld_name():
    job_lock.release("never_acquired")  # must not raise


def test_locks_are_independent_per_job_name():
    assert job_lock.try_acquire("live_forecast") is True
    assert job_lock.try_acquire("weekly_forecast") is True  # unaffected
    assert job_lock.is_running("live_forecast") is True
    assert job_lock.is_running("weekly_forecast") is True


def test_locked_job_names_covers_the_four_paid_refresh_jobs():
    assert job_lock.LOCKED_JOB_NAMES == {
        "live_forecast", "weekly_forecast", "google_places", "rebuild_redis",
    }


# ── admin_trigger_router.trigger_job integration ─────────────────────────────
@pytest.fixture(autouse=True)
def _container():
    container = MagicMock()
    admin_trigger_router.set_container(container)
    yield container
    admin_trigger_router.set_container(None)


@pytest.mark.asyncio
async def test_admin_trigger_refused_when_scheduler_holds_the_lock():
    job_lock.try_acquire("live_forecast")  # simulates the scheduler mid-run

    result = await admin_trigger_router.trigger_job("live_forecast", config=None)

    assert result.status == "already_running"
    assert "scheduled run in progress" in result.message
    # No admin task was registered -- the trigger never started anything.
    assert "live_forecast" not in admin_trigger_router._running_jobs


@pytest.mark.asyncio
async def test_admin_trigger_acquires_the_lock_when_free():
    assert job_lock.is_running("live_forecast") is False

    result = await admin_trigger_router.trigger_job("live_forecast", config=None)

    assert result.status == "started"
    assert job_lock.is_running("live_forecast") is True


@pytest.mark.asyncio
async def test_unlocked_jobs_are_unaffected_by_the_guard():
    """A job outside LOCKED_JOB_NAMES (e.g. instagram_validate) never touches
    job_lock at all -- the guard is scoped to the 4 paid-refresh jobs."""
    result = await admin_trigger_router.trigger_job("instagram_validate", config=None)
    assert result.status == "started"
    assert job_lock.is_running("instagram_validate") is False
