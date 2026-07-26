"""Unit tests for the S3 data lake writer.

Focus: the envelope/key contract a future query engine binds to, and the
failure isolation that keeps ingestion alive when archival breaks.
"""
import gzip
import json
from datetime import datetime, timezone

from botocore.exceptions import ClientError
from prometheus_client import REGISTRY

from app.dao.datalake_writer import (
    SCHEMA_VERSION,
    DatalakeWriter,
    build_envelope,
    object_key,
    set_job_context,
)

# 00:04 UTC on the 26th is 21:04 on the 25th in Recife (UTC-3) — the boundary
# that separates "UTC partition key" from "local analysis column".
UTC_AFTER_RECIFE_MIDNIGHT = datetime(2026, 7, 26, 0, 4, 3, tzinfo=timezone.utc)


class _FakeS3:
    def __init__(self, fail=False):
        self.puts = []
        self.fail = fail

    def put_object(self, **kwargs):
        if self.fail:
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "boom"}},
                "PutObject",
            )
        self.puts.append(kwargs)
        return {}


def _writer(**overrides):
    kwargs = dict(
        bucket="test-lake",
        region="us-east-1",
        s3_client=_FakeS3(),
        now=lambda: UTC_AFTER_RECIFE_MIDNIGHT,
    )
    kwargs.update(overrides)
    return DatalakeWriter(**kwargs)


def _metric(name, **labels):
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else float(value)


def _records(fake_s3):
    out = []
    for put in fake_s3.puts:
        text = gzip.decompress(put["Body"]).decode("utf-8")
        out.extend(json.loads(line) for line in text.splitlines() if line.strip())
    return out


class TestEnvelope:
    def test_carries_the_stable_schema_fields(self):
        envelope = build_envelope(
            dataset="live_forecast",
            ingested_at=UTC_AFTER_RECIFE_MIDNIGHT,
            endpoint="/forecasts/live",
            payload={"status": "OK"},
            http_status=200,
            latency_ms=412,
            venue_id="ven_1",
            job="live_refresh",
            run_id="run-1",
        )
        assert envelope["schema_version"] == SCHEMA_VERSION
        assert envelope["source"] == "besttime"
        assert envelope["dataset"] == "live_forecast"
        assert envelope["endpoint"] == "/forecasts/live"
        assert envelope["outcome"] == "success"
        assert envelope["http_status"] == 200
        assert envelope["latency_ms"] == 412
        assert envelope["venue_id"] == "ven_1"
        assert envelope["job"] == "live_refresh"
        assert envelope["run_id"] == "run-1"
        assert envelope["record_id"]
        assert envelope["ingested_at_utc"] == "2026-07-26T00:04:03Z"

    def test_payload_is_kept_verbatim(self):
        payload = {"status": "OK", "analysis": {"venue_live_busyness": 73}}
        envelope = build_envelope(
            dataset="live_forecast",
            ingested_at=UTC_AFTER_RECIFE_MIDNIGHT,
            payload=payload,
        )
        assert envelope["payload"] == payload

    def test_recife_local_time_rides_alongside_utc(self):
        envelope = build_envelope(
            dataset="live_forecast", ingested_at=UTC_AFTER_RECIFE_MIDNIGHT
        )
        # UTC has already rolled over; Recife has not.
        assert envelope["ingested_at_utc"].startswith("2026-07-26")
        assert envelope["recife_date"] == "2026-07-25"
        assert envelope["recife_hour"] == 21

    def test_errors_are_recorded_as_data(self):
        envelope = build_envelope(
            dataset="live_forecast",
            ingested_at=UTC_AFTER_RECIFE_MIDNIGHT,
            outcome="error",
            error="timeout: boom",
            http_status=None,
        )
        assert envelope["outcome"] == "error"
        assert envelope["error"] == "timeout: boom"
        assert envelope["payload"] is None


class TestObjectKey:
    def test_is_hive_partitioned(self):
        key = object_key(
            dataset="live_forecast",
            dt="2026-07-26",
            hour="00",
            writer_id="abc12345",
            seq=7,
        )
        assert key == (
            "raw/source=besttime/dataset=live_forecast/dt=2026-07-26/hour=00/"
            "part-abc12345-00007.ndjson.gz"
        )

    def test_every_directory_below_the_prefix_is_key_value(self):
        key = object_key(
            dataset="venue_filter",
            dt="2026-07-26",
            hour="13",
            writer_id="abc12345",
            seq=1,
        )
        for directory in key.split("/")[1:-1]:
            assert "=" in directory


class TestFlushing:
    async def test_writes_gzipped_ndjson_one_object_per_line(self):
        writer = _writer()
        for i in range(3):
            writer.record(dataset="live_forecast", payload={"i": i})
        await writer.flush()

        assert len(writer._s3.puts) == 1
        put = writer._s3.puts[0]
        assert put["ContentType"] == "application/gzip"
        lines = gzip.decompress(put["Body"]).decode("utf-8").splitlines()
        assert len(lines) == 3
        assert [json.loads(line)["payload"]["i"] for line in lines] == [0, 1, 2]

    async def test_batches_one_partition_window_into_a_single_object(self):
        writer = _writer()
        for i in range(40):
            writer.record(dataset="live_forecast", payload={"i": i})
        await writer.flush()

        assert len(writer._s3.puts) == 1, "one window must not become 40 objects"
        assert len(_records(writer._s3)) == 40

    async def test_separates_datasets_into_their_own_objects(self):
        writer = _writer()
        writer.record(dataset="live_forecast", payload={})
        writer.record(dataset="venue_filter", payload={})
        await writer.flush()

        keys = [put["Key"] for put in writer._s3.puts]
        assert len(keys) == 2
        assert any("dataset=live_forecast" in k for k in keys)
        assert any("dataset=venue_filter" in k for k in keys)

    async def test_uploads_once_the_byte_threshold_is_reached(self):
        writer = _writer(flush_max_bytes=1)
        writer.record(dataset="live_forecast", payload={"big": "x" * 100})
        writer._drain()
        await writer._upload_buffers(force=False)
        assert writer._s3.puts, "a full buffer must upload without being forced"

    async def test_uploads_once_the_buffer_is_old_enough(self):
        writer = _writer(flush_max_bytes=10**9, flush_max_seconds=0)
        writer.record(dataset="live_forecast", payload={"small": 1})
        writer._drain()
        await writer._upload_buffers(force=False)
        assert writer._s3.puts, "an aged buffer must upload even when small"

    async def test_holds_records_until_a_threshold_is_hit(self):
        writer = _writer(flush_max_bytes=10**9, flush_max_seconds=10**6)
        writer.record(dataset="live_forecast", payload={"small": 1})
        writer._drain()
        await writer._upload_buffers(force=False)
        assert not writer._s3.puts, "small, fresh buffers must keep accumulating"

    async def test_close_flushes_what_is_buffered(self):
        writer = _writer()
        writer.record(dataset="live_forecast", payload={"x": 1})
        await writer.close()
        assert len(_records(writer._s3)) == 1


class TestJobContext:
    async def test_records_the_scheduler_run_that_produced_them(self):
        writer = _writer()
        set_job_context("live_refresh", "run-abc")
        try:
            writer.record(dataset="live_forecast", payload={})
            await writer.flush()
        finally:
            set_job_context(None, None)

        record = _records(writer._s3)[0]
        assert record["job"] == "live_refresh"
        assert record["run_id"] == "run-abc"

    async def test_falls_back_to_the_writer_id_off_scheduler(self):
        writer = _writer()
        set_job_context(None, None)
        writer.record(dataset="live_forecast", payload={})
        await writer.flush()

        record = _records(writer._s3)[0]
        assert record["job"] is None
        assert record["run_id"], "run_id must never be empty"


class TestFailureIsolation:
    """None of these may raise: a broken lake must not break ingestion."""

    async def test_drops_without_blocking_when_the_queue_is_full(self):
        writer = _writer(queue_maxsize=1)
        before = _metric(
            "datalake_records_dropped_total",
            source="besttime",
            dataset="live_forecast",
            reason="queue_full",
        )

        assert writer.record(dataset="live_forecast", payload={"i": 0}) is True
        assert writer.record(dataset="live_forecast", payload={"i": 1}) is False

        after = _metric(
            "datalake_records_dropped_total",
            source="besttime",
            dataset="live_forecast",
            reason="queue_full",
        )
        assert after == before + 1

    async def test_counts_and_swallows_an_unserializable_record(self):
        writer = _writer()
        before = _metric(
            "datalake_records_dropped_total",
            source="besttime",
            dataset="live_forecast",
            reason="serialize_error",
        )

        class _Unserializable:
            def __repr__(self):
                raise ValueError("cannot repr")

        assert writer.record(dataset="live_forecast", payload=_Unserializable()) is False

        after = _metric(
            "datalake_records_dropped_total",
            source="besttime",
            dataset="live_forecast",
            reason="serialize_error",
        )
        assert after == before + 1

    async def test_counts_every_lost_record_when_the_upload_fails(self, caplog):
        writer = _writer(s3_client=_FakeS3(fail=True))
        before = _metric(
            "datalake_records_dropped_total",
            source="besttime",
            dataset="live_forecast",
            reason="flush_failed",
        )

        for i in range(5):
            writer.record(dataset="live_forecast", payload={"i": i})
        await writer.flush()  # must not raise

        after = _metric(
            "datalake_records_dropped_total",
            source="besttime",
            dataset="live_forecast",
            reason="flush_failed",
        )
        assert after == before + 5, "every record in the batch must be counted"

    async def test_logs_the_dataset_and_loss_count_on_a_failed_upload(self, caplog):
        writer = _writer(s3_client=_FakeS3(fail=True))
        for i in range(3):
            writer.record(dataset="live_forecast", payload={"i": i})

        with caplog.at_level("ERROR", logger="app.dao.datalake_writer"):
            await writer.flush()

        assert "live_forecast" in caplog.text
        assert "3 records" in caplog.text

    async def test_a_broken_s3_client_never_propagates(self):
        class _Exploding:
            def put_object(self, **kwargs):
                raise RuntimeError("unexpected boto3 failure")

        writer = _writer(s3_client=_Exploding())
        writer.record(dataset="live_forecast", payload={})
        await writer.flush()  # the assertion is that this does not raise

    async def test_flush_is_safe_with_nothing_buffered(self):
        writer = _writer()
        await writer.flush()
        assert not writer._s3.puts


class TestMetrics:
    async def test_publishes_the_series_grafana_alerts_on(self):
        writer = _writer()
        writer.record(dataset="live_forecast", payload={})
        await writer.flush()

        assert (
            _metric(
                "datalake_records_enqueued_total",
                source="besttime",
                dataset="live_forecast",
            )
            > 0
        )
        assert (
            _metric("datalake_flush_total", dataset="live_forecast", status="success")
            > 0
        )
        assert REGISTRY.get_sample_value("datalake_queue_depth") is not None
        assert _metric("datalake_last_success_timestamp") > 0
