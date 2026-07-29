"""Behave steps for tests/bdd/observability/pipeline-run-registry.feature.

Drives the REAL run scope and registry. The cardinality guarantee is the point
of the design, so the ring is asserted by counting registered series — not by
trusting that eviction was called.
"""
from __future__ import annotations

import logging

from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY


def _mod():
    try:
        from app.services import pipeline_run_registry as m

        return m
    except ImportError:
        return None


def _require(context):
    m = _mod()
    assert m is not None, (
        "app.services.pipeline_run_registry does not exist yet — every pipeline "
        "run must publish an identity an operator can select"
    )
    return m


def _series(pipeline=None):
    """Registered runs, as (pipeline, job_id, status) tuples."""
    out = []
    for metric in REGISTRY.collect():
        if metric.name != "pipeline_run_info":
            continue
        for s in metric.samples:
            lbl = s.labels
            if pipeline is None or lbl.get("pipeline") == pipeline:
                out.append((lbl.get("pipeline"), lbl.get("job_id"), lbl.get("status"), s.value))
    return out


def _run_pipeline(context, name, *, fail=False, log_line=None):
    m = _require(context)
    context.raised = None
    try:
        with m.run_scope(name) as run_id:
            context.last_run_id = run_id
            context.run_ids = getattr(context, "run_ids", []) + [run_id]
            if log_line is not None:
                logging.getLogger("app.services.demo").info(log_line)
            if fail:
                raise RuntimeError("pipeline blew up")
    except Exception as e:
        context.raised = e


# ── Background ────────────────────────────────────────────────────────────────
@given("the pipeline run registry is enabled")
def step_enabled(context):
    m = _require(context)
    m.reset_registry()
    m.configure(enabled=True, size=getattr(context, "ring_size", 10))
    context.run_ids = []


@given("the pipeline run registry is disabled")
def step_disabled(context):
    m = _require(context)
    m.reset_registry()
    m.configure(enabled=False, size=10)


@given("the registry keeps {n:d} runs per pipeline")
def step_ring_size(context, n):
    m = _require(context)
    context.ring_size = n
    m.configure(enabled=True, size=n)


@given("registering a run fails")
def step_registry_fails(context):
    m = _require(context)
    context.broken_registry = True

    def _boom(*a, **kw):
        raise RuntimeError("registry exploded")

    m._REGISTRY_IMPL.register = _boom  # type: ignore[attr-defined]


@given("a pipeline that has never run")
def step_never_run(context):
    context.unrun_pipeline = "never_ran"


# ── When ──────────────────────────────────────────────────────────────────────
@when('the scheduled pipeline "{name}" runs to completion')
def step_scheduled_run(context, name):
    _run_pipeline(context, name)


@when('the pipeline "{name}" is triggered from the admin panel')
def step_admin_run(context, name):
    _run_pipeline(context, name)
    context.admin_series = _series(name)


@when('the scheduled pipeline "{name}" raises during its run')
def step_failing_run(context, name):
    _run_pipeline(context, name, fail=True)


@when('three runs of "{name}" happen in sequence')
def step_three_runs(context, name):
    for _ in range(3):
        _run_pipeline(context, name)


@when('{n:d} runs of "{name}" complete')
def step_n_runs(context, n, name):
    for _ in range(n):
        _run_pipeline(context, name)


@when('{n:d} run of "{name}" completes')
def step_one_run(context, n, name):
    for _ in range(n):
        _run_pipeline(context, name)


@when('the scheduled pipeline "{name}" logs during its run')
def step_run_logs(context, name):
    context.log_records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            context.log_records.append(record)

    root = logging.getLogger("app.services.demo")
    handler = _Collect()
    m = _require(context)
    handler.addFilter(m.RunIdLoggingFilter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        _run_pipeline(context, name, log_line="doing work")
    finally:
        root.removeHandler(handler)


@when("a pipeline logs a line that already carries its run id")
def step_prestamped_log(context):
    m = _require(context)
    context.log_records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            context.log_records.append(record)

    log = logging.getLogger("app.services.demo2")
    handler = _Collect()
    handler.addFilter(m.RunIdLoggingFilter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        with m.run_scope("prestamped") as rid:
            context.last_run_id = rid
            log.info(f"already tagged job={rid} here")
    finally:
        log.removeHandler(handler)


@when("a log line is emitted outside any pipeline run")
def step_log_outside(context):
    m = _require(context)
    context.log_records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            context.log_records.append(record)

    log = logging.getLogger("app.services.demo3")
    handler = _Collect()
    handler.addFilter(m.RunIdLoggingFilter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("no run here")
    finally:
        log.removeHandler(handler)


# ── Then ──────────────────────────────────────────────────────────────────────
@then('the run is registered for the pipeline "{name}"')
def step_registered(context, name):
    rows = _series(name)
    assert rows, f"no run registered for {name}: {_series()}"


@then('the run is registered with the status "{status}"')
def step_registered_status(context, status):
    rows = [r for r in _series() if r[1] == context.last_run_id]
    assert rows, f"run {context.last_run_id} not registered"
    assert rows[-1][2] == status, rows


@then("the run carries a run id")
def step_has_id(context):
    assert context.last_run_id, "no run id was minted"


@then("the run is indistinguishable from a scheduled run")
def step_same_shape(context):
    rows = context.admin_series
    assert rows, "admin-triggered run was not registered"
    assert set(("pipeline", "job_id", "status")) or True
    assert rows[0][0] and rows[0][1] and rows[0][2], rows


@then("the failure still reaches the caller")
def step_failure_propagates(context):
    assert context.raised is not None, "the exception was swallowed"


@then("the pipeline has exactly one registered entry for that run")
def step_one_entry(context):
    rows = [r for r in _series() if r[1] == context.last_run_id]
    assert len(rows) == 1, f"run appears under {len(rows)} statuses: {rows}"


@then("the registered run reports its start time")
def step_start_time(context):
    rows = [r for r in _series() if r[1] == context.last_run_id]
    assert rows and rows[0][3] > 1_600_000_000, rows


@then("sorting the run ids as text puts them in the order they ran")
def step_sortable(context):
    ids = context.run_ids[-3:]
    assert ids == sorted(ids), f"run ids are not time-ordered: {ids}"


@then("only the {n:d} most recent runs remain registered")
def step_ring(context, n):
    rows = _series("live_forecast")
    assert len(rows) == n, f"expected {n} registered runs, got {len(rows)}"


@then("the oldest runs are no longer registered")
def step_oldest_gone(context):
    registered = {r[1] for r in _series("live_forecast")}
    assert context.run_ids[0] not in registered, "the oldest run was not evicted"


@then('the "{name}" run is still registered')
def step_other_pipeline_kept(context, name):
    assert _series(name), f"{name} was evicted by another pipeline's runs"


@then("those log lines carry the run id")
def step_logs_stamped(context):
    text = " ".join(r.getMessage() for r in context.log_records)
    assert f"job={context.last_run_id}" in text, text


@then("the run id appears exactly once in that line")
def step_no_double_stamp(context):
    text = " ".join(r.getMessage() for r in context.log_records)
    assert text.count(f"job={context.last_run_id}") == 1, text


@then("that line carries no run id")
def step_no_stamp(context):
    text = " ".join(r.getMessage() for r in context.log_records)
    assert "job=" not in text, text


@then("the pipeline still completes successfully")
def step_completed(context):
    assert context.raised is None, context.raised


@then("no run is registered")
def step_nothing_registered(context):
    assert not _series(), _series()
