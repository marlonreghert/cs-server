"""Deterministic waits for the background job tasks started by
BatchAddService and AddVenueJobService.

Both services expose the same shape: ``start_job()`` schedules the run with
``asyncio.create_task()`` and keeps the handle in a
``_tasks: dict[job_id, asyncio.Task]`` registry, popping it from a done
callback. Tests used to observe completion by counting scheduler yields::

    for _ in range(200):
        task = svc._tasks.get(job_id)
        if task is None:
            break
        await asyncio.sleep(0)
        if task.done():
            break

That is a race, not a wait. ``asyncio.sleep(0)`` yields exactly one scheduling
step and advances no wall clock, while the job's own work suspends on things
that finish on wall-clock time — notably ``BatchAddService._save()``, which is a
``loop.run_in_executor()`` thread-pool round trip *per row*. A fixed yield
budget can therefore expire with the task still running, leaving the job
document on ``"running"`` and failing an assertion that has nothing to do with
the behaviour under test.

It is also inherently interpreter-dependent: CI run 31981689659 passed on
py3.12 and failed on py3.13 at the same commit, because the same work needs a
different number of yield-points on 3.13. Raising the constant only makes the
race rarer.

Awaiting the task removes the guesswork entirely: no yield budget, no
interpreter-version sensitivity, no wall-clock tuning.
"""
from __future__ import annotations

import asyncio

# Bounded on purpose. A genuinely stuck job must fail loudly and quickly rather
# than hang CI until the job-level timeout kills the whole run. This is a
# deadline, not a tuning knob — correct code never comes close to it, so it does
# not need to grow when a job gets slower or a runner gets busier.
DEFAULT_JOB_TIMEOUT_SECONDS = 10.0


async def await_job_task(svc, job_id: str, timeout: float = DEFAULT_JOB_TIMEOUT_SECONDS) -> None:
    """Wait until ``svc``'s background task for ``job_id`` has finished.

    ``asyncio.shield`` keeps the timeout from cancelling the job itself:
    ``wait_for`` cancels whatever it is awaiting when the deadline passes, and
    cancelling the job would destroy the very state the failure message needs to
    report.

    The task's *own* exception is deliberately not re-raised. Both services turn
    a crashed run into an observable job document — ``BatchAddService._on_done``
    records ``status="failed"`` with ``stopped_reason``, and
    ``AddVenueJobService._run_job`` catches internally — and callers assert on
    that document. This also matches what the previous busy-wait did, since it
    only ever inspected ``task.done()`` and never retrieved the exception.
    Propagating here would replace those assertions with a raw traceback. A
    *timeout*, by contrast, is always raised.
    """
    task = svc._tasks.get(job_id)
    if task is None:
        # Already finished — the done callback pops the registry.
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(asyncio.shield(task), return_exceptions=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        job = svc.get_job(job_id) or {}
        raise AssertionError(
            f"background job {job_id} did not finish within {timeout}s "
            f"(status={job.get('status')!r}, "
            f"processed={job.get('processed')}/{job.get('total')})"
        ) from exc


def await_job_task_blocking(
    client, svc, job_id: str, timeout: float = DEFAULT_JOB_TIMEOUT_SECONDS
) -> None:
    """Sync-callable version of :func:`await_job_task`, for behave steps.

    Behave steps run on the plain test thread while the ASGI ``TestClient`` runs
    the app — and therefore the job task — on its own anyio ``BlockingPortal``
    event loop, so a step cannot ``await`` anything directly. ``portal.call``
    hands the wait to that loop and blocks until it returns, which keeps the
    step function synchronous (behave's step contract) while still performing a
    real await instead of a wall-clock poll.
    """
    portal = getattr(client, "portal", None)
    assert portal is not None, (
        "TestClient has no portal — tests/bdd/environment.py must enter the "
        "client as a context manager, or background tasks do not survive "
        "between requests"
    )
    portal.call(await_job_task, svc, job_id, timeout)
