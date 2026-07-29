"""Run identity for every pipeline: a selectable id, in metrics and in logs.

An operator watching an enrichment can see *that* it is running but not scope
anything to *this* run. This gives every scheduled and admin-triggered run a
time-ordered id, stamps it into the run's log lines, and publishes it as a
metric Grafana can turn into a dropdown.

WHY THE ID IS NOT A LOKI LABEL. Loki here is 2.9.8 on schema v11, where
structured metadata — the feature built for high-cardinality fields exactly like
this — does not exist. A per-run label would create one stream per run;
redis_projection alone runs every 2 minutes, ~720 streams a day, forever. So
selection and filtering deliberately use different stores: SELECTION from this
Prometheus metric, whose cardinality is bounded BY CONSTRUCTION by the ring
below, and FILTERING from Loki by plain substring on `job=<id>`, which needs no
label at all and already works.

Everything here is instrumentation. It must never fail a pipeline.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Optional

from app.metrics import PIPELINE_RUN_INFO

logger = logging.getLogger(__name__)

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"

DEFAULT_RING_SIZE = 10

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# (pipeline, job_id) for the run this task is inside of.
_current: ContextVar[tuple[Optional[str], Optional[str]]] = ContextVar(
    "pipeline_run", default=(None, None)
)


_id_lock = threading.Lock()
_last_ms = 0
_last_rand = 0
_RAND_BITS = 80
_RAND_MAX = (1 << _RAND_BITS) - 1


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_CROCKFORD[rem])
    return "".join(reversed(out))


def new_run_id(when: Optional[datetime] = None) -> str:
    """A MONOTONIC ULID: 48-bit millisecond timestamp, then 80 bits.

    Time-ordered ON PURPOSE — a lexicographic sort of run ids is chronological,
    which is what lets "newest run" be answered by sorting rather than by
    parsing or by an extra timestamp field.

    Monotonic within a millisecond, per the ULID spec's monotonicity rule: two
    runs starting in the same millisecond would otherwise differ only in their
    RANDOM component and sort arbitrarily, breaking the ordering the rest of the
    design leans on. Same millisecond -> increment the previous randomness
    instead of drawing fresh.
    """
    moment = when or datetime.now(timezone.utc)
    ms = int(moment.timestamp() * 1000)
    global _last_ms, _last_rand
    with _id_lock:
        if ms == _last_ms:
            _last_rand = min(_last_rand + 1, _RAND_MAX)
        else:
            _last_ms = ms
            _last_rand = int.from_bytes(os.urandom(10), "big")
        rand = _last_rand
    return _encode(ms, 10) + _encode(rand, 16)


def current_run() -> tuple[Optional[str], Optional[str]]:
    """(pipeline, job_id) for the enclosing run, or (None, None)."""
    return _current.get()


class _BoundedRunRegistry:
    """Keeps the last N runs per pipeline registered, and no more.

    The eviction is the entire cardinality argument. Without it, `job_id` as a
    Prometheus label grows without bound exactly like the Loki label this design
    rejects.
    """

    def __init__(self, size: int = DEFAULT_RING_SIZE):
        self.size = size
        self._lock = threading.Lock()
        # pipeline -> [(job_id, status, started)] oldest first
        self._runs: dict[str, list[list]] = {}

    def register(self, pipeline: str, job_id: str, status: str, started: float) -> None:
        with self._lock:
            runs = self._runs.setdefault(pipeline, [])
            for entry in runs:
                if entry[0] == job_id:
                    # A run must appear under ONE status: drop the old label set
                    # before publishing the new one.
                    PIPELINE_RUN_INFO.remove(pipeline, job_id, entry[1])
                    entry[1] = status
                    PIPELINE_RUN_INFO.labels(
                        pipeline=pipeline, job_id=job_id, status=status
                    ).set(entry[2])
                    return
            runs.append([job_id, status, started])
            PIPELINE_RUN_INFO.labels(
                pipeline=pipeline, job_id=job_id, status=status
            ).set(started)
            while len(runs) > self.size:
                old_id, old_status, _ = runs.pop(0)
                PIPELINE_RUN_INFO.remove(pipeline, old_id, old_status)

    def clear(self) -> None:
        with self._lock:
            for pipeline, runs in self._runs.items():
                for job_id, status, _ in runs:
                    try:
                        PIPELINE_RUN_INFO.remove(pipeline, job_id, status)
                    except Exception:
                        pass
            self._runs.clear()


_REGISTRY_IMPL = _BoundedRunRegistry()
_ENABLED = True


def configure(*, enabled: bool = True, size: int = DEFAULT_RING_SIZE) -> None:
    global _ENABLED
    _ENABLED = enabled
    _REGISTRY_IMPL.size = size


def reset_registry() -> None:
    """Drop every registered run. For tests and for a clean restart."""
    _REGISTRY_IMPL.clear()


def _register(pipeline: str, job_id: str, status: str, started: float) -> None:
    """Best-effort. A bookkeeping failure must never fail a pipeline."""
    if not _ENABLED:
        return
    try:
        _REGISTRY_IMPL.register(pipeline, job_id, status, started)
    except Exception as e:
        logger.warning(f"[PipelineRunRegistry] could not register {pipeline}: {e}")


@contextmanager
def run_scope(pipeline: str, job_id: Optional[str] = None):
    """Mint a run id, publish it, and record the terminal status on the way out.

    Yields the run id. The pipeline's exception is re-raised unchanged — the
    registry observes runs, it does not change their outcome.
    """
    run_id = job_id or new_run_id()
    started = time.time()
    token = _current.set((pipeline, run_id))
    _register(pipeline, run_id, STATUS_RUNNING, started)
    try:
        yield run_id
    except BaseException:
        _register(pipeline, run_id, STATUS_ERROR, started)
        raise
    else:
        _register(pipeline, run_id, STATUS_SUCCESS, started)
    finally:
        _current.reset(token)


class RunIdLoggingFilter(logging.Filter):
    """Stamp `job=<id>` onto log lines emitted inside a run.

    Gives every existing pipeline log correlation without touching a single log
    statement. A line that already carries its own id — the photo archive writes
    one — is left alone rather than stamped twice.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _, job_id = current_run()
            if not job_id:
                return True
            message = record.getMessage()
            if f"job={job_id}" in message:
                return True
            record.msg = f"{message} job={job_id}"
            record.args = ()
        except Exception:  # pragma: no cover - logging must never raise
            pass
        return True


def install_run_id_logging(logger_obj: Optional[logging.Logger] = None) -> None:
    """Attach the filter to every handler of `logger_obj` (root by default).
    Idempotent — a handler is never given two."""
    target = logger_obj if logger_obj is not None else logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, RunIdLoggingFilter) for f in handler.filters):
            handler.addFilter(RunIdLoggingFilter())
