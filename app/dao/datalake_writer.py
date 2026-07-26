"""S3 data-lake writer for raw external-API responses.

Every response we get from BestTime is archived verbatim so the history of what
BestTime told us stays queryable long after the refresh cycle that fetched it.
Objects are Hive-partitioned gzipped NDJSON:

    raw/source=<source>/dataset=<dataset>/dt=<YYYY-MM-DD>/hour=<HH>/part-<id>-<seq>.ndjson.gz

`dt`/`hour` are UTC — partition keys must be unambiguous and monotonic at write
time — and the Recife-local date/hour ride inside each record instead, so
local-time analysis stays a column filter.

THIS PATH MUST NEVER BREAK INGESTION. `record()` is synchronous, never awaits,
and never raises: it drops onto a bounded queue and returns. A background task
batches by partition and uploads. Every failure mode (queue full, serialization
error, S3 error/timeout, unexpected bug) is swallowed, logged, and counted in a
Prometheus metric so Grafana sees it — a refresh job must never fail because an
archive write did.

Batching is also a cost decision: at a 5-minute live-refresh cadence one object
per API response would be ~50k objects/day, versus ~100-300 when a partition
window is rolled into a single object.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from app.metrics import (
    DATALAKE_FLUSH_BYTES_TOTAL,
    DATALAKE_FLUSH_DURATION_SECONDS,
    DATALAKE_FLUSH_TOTAL,
    DATALAKE_LAST_SUCCESS_TIMESTAMP,
    DATALAKE_QUEUE_DEPTH,
    DATALAKE_RECORDS_DROPPED_TOTAL,
    DATALAKE_RECORDS_ENQUEUED_TOTAL,
)
from app.utils.recife_time import RECIFE_TZ

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_SOURCE = "besttime"
DEFAULT_PREFIX = "raw"

# Query params whose VALUE is a credential — dropped entirely rather than masked,
# so no key-shaped substring (`pri_`, `AIza`) ever reaches the lake.
_SECRET_PARAM_NAMES = frozenset(
    {
        "api_key",
        "api_key_private",
        "api_key_public",
        "apikey",
        "access_token",
        "key",
        "password",
        "secret",
        "token",
    }
)

# Value-shaped secrets, masked wherever they appear (mirrors app/log_redaction.py,
# but replaces the whole value — the lake keeps no partial key material).
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bpri_[0-9a-fA-F]{16,}\b"),
    re.compile(r"\bpub_[0-9a-fA-F]{16,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
)

_REDACTED = "***REDACTED***"

# Job identity for the record envelope. The scheduler's shared job skeleton
# (main.py) sets this per run; direct/ad-hoc calls fall back to the writer's own
# process id so `run_id` is never empty.
_job_context: ContextVar[tuple[Optional[str], Optional[str]]] = ContextVar(
    "datalake_job_context", default=(None, None)
)


def set_job_context(job: Optional[str], run_id: Optional[str]) -> None:
    """Name the job (and its run) that subsequent archived records belong to."""
    _job_context.set((job, run_id))


def current_job_context() -> tuple[Optional[str], Optional[str]]:
    return _job_context.get()


def redact_params(params: Optional[Mapping[str, Any]]) -> dict:
    """Strip credentials from a request's query params.

    Secret-named params are dropped outright; any remaining value that still
    looks like a key is replaced wholesale. Non-secret params survive — knowing
    which venue_id or lat/lng produced a payload is most of the analytical value.
    """
    if not params:
        return {}
    clean: dict[str, Any] = {}
    for name, value in params.items():
        if str(name).lower() in _SECRET_PARAM_NAMES:
            continue
        text = str(value)
        if any(pattern.search(text) for pattern in _SECRET_VALUE_PATTERNS):
            clean[name] = _REDACTED
            continue
        clean[name] = value
    return clean


def build_envelope(
    *,
    dataset: str,
    ingested_at: datetime,
    source: str = DEFAULT_SOURCE,
    endpoint: Optional[str] = None,
    payload: Any = None,
    outcome: str = "success",
    http_status: Optional[int] = None,
    latency_ms: Optional[int] = None,
    venue_id: Optional[str] = None,
    request_params: Optional[Mapping[str, Any]] = None,
    error: Optional[str] = None,
    job: Optional[str] = None,
    run_id: Optional[str] = None,
) -> dict:
    """Build the one envelope shape every dataset shares.

    Stable across datasets so a single table definition holds forever, with the
    verbatim `payload` carrying whatever we did not think to extract today.
    """
    local = ingested_at.astimezone(RECIFE_TZ)
    return {
        "record_id": str(uuid.uuid4()),
        "schema_version": SCHEMA_VERSION,
        "ingested_at_utc": ingested_at.isoformat().replace("+00:00", "Z"),
        "recife_date": local.strftime("%Y-%m-%d"),
        "recife_hour": local.hour,
        "source": source,
        "dataset": dataset,
        "endpoint": endpoint,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "outcome": outcome,
        "run_id": run_id,
        "job": job,
        "venue_id": venue_id,
        "request": redact_params(request_params),
        "payload": payload,
        "error": error,
    }


def object_key(
    *,
    dataset: str,
    dt: str,
    hour: str,
    writer_id: str,
    seq: int,
    source: str = DEFAULT_SOURCE,
    prefix: str = DEFAULT_PREFIX,
) -> str:
    """Hive-style key: every directory below the prefix is `key=value`, so Glue,
    Athena, Spark, Trino, and DuckDB all discover partitions with no config."""
    return (
        f"{prefix}/source={source}/dataset={dataset}/dt={dt}/hour={hour}/"
        f"part-{writer_id}-{seq:05d}.ndjson.gz"
    )


class DatalakeWriter:
    """Buffers archived records and uploads them as gzipped NDJSON objects.

    Failure isolation is the whole design: nothing here may propagate to a
    caller. See the module docstring.
    """

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        source: str = DEFAULT_SOURCE,
        prefix: str = DEFAULT_PREFIX,
        queue_maxsize: int = 10000,
        flush_max_bytes: int = 262144,
        flush_max_seconds: int = 900,
        shutdown_flush_seconds: int = 10,
        s3_client: Any = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        now=None,
    ):
        self.bucket = bucket
        self.region = region
        self.source = source
        self.prefix = prefix
        self.flush_max_bytes = flush_max_bytes
        self.flush_max_seconds = flush_max_seconds
        self.shutdown_flush_seconds = shutdown_flush_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._writer_id = uuid.uuid4().hex[:8]
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
        # (dataset, dt, hour) -> {"lines": [...], "bytes": int, "opened": float}
        self._buffers: dict[tuple[str, str, str], dict] = {}
        self._seq = 0
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._s3 = s3_client if s3_client is not None else _build_s3_client(
            region=region,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )

    # ── enqueue ──────────────────────────────────────────────────────────────
    def record(
        self,
        *,
        dataset: str,
        endpoint: Optional[str] = None,
        payload: Any = None,
        outcome: str = "success",
        http_status: Optional[int] = None,
        latency_ms: Optional[int] = None,
        venue_id: Optional[str] = None,
        request_params: Optional[Mapping[str, Any]] = None,
        error: Optional[str] = None,
    ) -> bool:
        """Queue one record. Synchronous, non-blocking, and never raises."""
        try:
            job, run_id = current_job_context()
            ingested_at = self._now()
            envelope = build_envelope(
                dataset=dataset,
                ingested_at=ingested_at,
                source=self.source,
                endpoint=endpoint,
                payload=payload,
                outcome=outcome,
                http_status=http_status,
                latency_ms=latency_ms,
                venue_id=venue_id,
                request_params=request_params,
                error=error,
                job=job,
                run_id=run_id or self._writer_id,
            )
            line = json.dumps(envelope, ensure_ascii=False, default=str)
        except Exception as e:
            DATALAKE_RECORDS_DROPPED_TOTAL.labels(
                source=self.source, dataset=dataset, reason="serialize_error"
            ).inc()
            logger.error(
                f"[DatalakeWriter] could not serialize a {dataset} record: {e}"
            )
            return False

        try:
            partition = (
                dataset,
                ingested_at.strftime("%Y-%m-%d"),
                ingested_at.strftime("%H"),
            )
            self._queue.put_nowait((partition, line))
        except asyncio.QueueFull:
            DATALAKE_RECORDS_DROPPED_TOTAL.labels(
                source=self.source, dataset=dataset, reason="queue_full"
            ).inc()
            return False
        except Exception as e:
            DATALAKE_RECORDS_DROPPED_TOTAL.labels(
                source=self.source, dataset=dataset, reason="unexpected"
            ).inc()
            logger.error(f"[DatalakeWriter] could not enqueue a {dataset} record: {e}")
            return False

        DATALAKE_RECORDS_ENQUEUED_TOTAL.labels(
            source=self.source, dataset=dataset
        ).inc()
        DATALAKE_QUEUE_DEPTH.set(self._queue.qsize())
        return True

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Start the background flusher. Safe to call twice."""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run())
            logger.info(
                f"[DatalakeWriter] started (bucket={self.bucket}, "
                f"flush>={self.flush_max_bytes}B or {self.flush_max_seconds}s)"
            )

    async def close(self) -> None:
        """Stop the flusher and drain what is buffered, within a bounded wait."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        try:
            await asyncio.wait_for(self.flush(), timeout=self.shutdown_flush_seconds)
        except asyncio.TimeoutError:
            logger.error(
                "[DatalakeWriter] shutdown flush timed out; buffered records lost"
            )
        except Exception as e:
            logger.error(f"[DatalakeWriter] shutdown flush failed: {e}")

    async def flush(self) -> None:
        """Drain the queue and upload every buffered partition immediately."""
        try:
            self._drain()
            await self._upload_buffers(force=True)
        except Exception as e:
            logger.error(f"[DatalakeWriter] flush failed: {e}")

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._await_record(timeout=1.0)
                self._drain()
                await self._upload_buffers(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the flusher die
                logger.error(f"[DatalakeWriter] flusher loop error: {e}")
                await asyncio.sleep(1.0)

    async def _await_record(self, timeout: float) -> None:
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return
        self._buffer(item)

    def _drain(self) -> None:
        while True:
            try:
                self._buffer(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        DATALAKE_QUEUE_DEPTH.set(self._queue.qsize())

    def _buffer(self, item) -> None:
        partition, line = item
        buffer = self._buffers.get(partition)
        if buffer is None:
            buffer = {"lines": [], "bytes": 0, "opened": time.monotonic()}
            self._buffers[partition] = buffer
        buffer["lines"].append(line)
        buffer["bytes"] += len(line) + 1

    # ── upload ───────────────────────────────────────────────────────────────
    async def _upload_buffers(self, *, force: bool) -> None:
        for partition in list(self._buffers):
            buffer = self._buffers[partition]
            if not buffer["lines"]:
                continue
            aged = (time.monotonic() - buffer["opened"]) >= self.flush_max_seconds
            if not force and buffer["bytes"] < self.flush_max_bytes and not aged:
                continue
            del self._buffers[partition]
            await self._upload(partition, buffer["lines"])

    async def _upload(self, partition, lines: list[str]) -> None:
        dataset, dt, hour = partition
        self._seq += 1
        key = object_key(
            dataset=dataset,
            dt=dt,
            hour=hour,
            writer_id=self._writer_id,
            seq=self._seq,
            source=self.source,
            prefix=self.prefix,
        )
        body = gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))
        started = time.perf_counter()
        try:
            await asyncio.to_thread(
                self._s3.put_object,
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/gzip",
            )
        except Exception as e:
            DATALAKE_FLUSH_TOTAL.labels(dataset=dataset, status="error").inc()
            DATALAKE_RECORDS_DROPPED_TOTAL.labels(
                source=self.source, dataset=dataset, reason="flush_failed"
            ).inc(len(lines))
            logger.error(
                f"[DatalakeWriter] upload failed for dataset={dataset} dt={dt} "
                f"hour={hour}: dropping {len(lines)} records: {e}"
            )
            return

        DATALAKE_FLUSH_DURATION_SECONDS.labels(dataset=dataset).observe(
            time.perf_counter() - started
        )
        DATALAKE_FLUSH_TOTAL.labels(dataset=dataset, status="success").inc()
        DATALAKE_FLUSH_BYTES_TOTAL.labels(dataset=dataset).inc(len(body))
        DATALAKE_LAST_SUCCESS_TIMESTAMP.set_to_current_time()
        logger.debug(
            f"[DatalakeWriter] archived {len(lines)} {dataset} records "
            f"({len(body)} bytes) to s3://{self.bucket}/{key}"
        )


def _build_s3_client(
    *,
    region: str,
    access_key_id: Optional[str],
    secret_access_key: Optional[str],
):
    """Build the boto3 client.

    With no explicit keys configured the DEFAULT CREDENTIAL CHAIN resolves — the
    EC2 instance role in production (short-lived, auto-rotating, nothing stored)
    and the local SSO profile in development. Explicit keys stay available as a
    local-dev escape hatch only.
    """
    import boto3
    from botocore.config import Config

    config = Config(
        connect_timeout=5,
        read_timeout=5,
        retries={"max_attempts": 2, "mode": "standard"},
    )
    if access_key_id and secret_access_key:
        return boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=config,
        )
    return boto3.client("s3", region_name=region, config=config)
