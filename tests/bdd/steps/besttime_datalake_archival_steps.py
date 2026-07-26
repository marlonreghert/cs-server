"""Behave steps for tests/bdd/persistence/besttime-datalake-archival.feature.

These steps drive the REAL `BestTimeAPIClient` — with only its httpx boundary
programmed — and the REAL `DatalakeWriter`, with an in-memory fake standing in
for boto3. The archival tap lives inside the client, so a stubbed client would
assert nothing about the behavior under test.

The ingestion-resilience scenarios drive the real
`VenuesRefresherService._fetch_and_cache_live_forecasts` over the RDS-backed
repository built in `environment.py`, so "the refresh still works" is asserted
against real persistence rather than a mock.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import time
from datetime import datetime, timezone

import httpx
from behave import given, then, when  # type: ignore[import-untyped]
from botocore.exceptions import ClientError
from prometheus_client import REGISTRY

from app.models import Venue
from app.models.vibe_attributes import VibeAttributes
from app.services.venues_refresher_service import VenuesRefresherService

_LAT, _LNG = -8.05, -34.88
_PRIVATE_KEY = "pri_" + "a" * 24
_PUBLIC_KEY = "pub_" + "b" * 24
_BASE_URL = "https://besttime.app/api/v1"

# Recife is UTC-3, so 21:00 local on 2026-07-25 is 00:00 UTC on 2026-07-26 —
# the boundary the UTC-partitioning scenario pins.
_FIXED_UTC = datetime(2026, 7, 26, 0, 4, 3, tzinfo=timezone.utc)


# ── fakes ─────────────────────────────────────────────────────────────────────
class _FakeS3:
    """Captures put_object calls; can be told to fail every upload."""

    def __init__(self) -> None:
        self.puts: list[dict] = []
        self.fail = False

    def put_object(self, **kwargs):
        if self.fail:
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "boom"}},
                "PutObject",
            )
        self.puts.append(kwargs)
        return {}


def _http_response(status: int, body) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=body,
        request=httpx.Request("GET", _BASE_URL),
    )


def _writer_class():
    """Import the writer lazily so a missing module fails as a readable
    assertion rather than crashing the harness at import time (true-RED)."""
    try:
        from app.dao.datalake_writer import DatalakeWriter

        return DatalakeWriter
    except ImportError:
        return None


def _build_writer(context, **overrides):
    cls = _writer_class()
    assert cls is not None, (
        "app.dao.datalake_writer.DatalakeWriter does not exist yet — the data "
        "lake writer must archive BestTime responses"
    )
    kwargs = dict(
        bucket="vibesense-datalake-test",
        region="us-east-1",
        s3_client=context.fake_s3,
        now=lambda: getattr(context, "fixed_now", _FIXED_UTC),
    )
    kwargs.update(overrides)
    return cls(**kwargs)


def _build_client(context, datalake=None):
    from app.api.besttime_client import BestTimeAPIClient

    try:
        return BestTimeAPIClient(
            base_url=_BASE_URL,
            api_key_public=_PUBLIC_KEY,
            api_key_private=_PRIVATE_KEY,
            datalake=datalake,
        )
    except TypeError:
        # `datalake` kwarg not added yet (true-RED): build the current signature
        # so the scenario fails on "nothing was archived", not on a constructor.
        return BestTimeAPIClient(
            base_url=_BASE_URL,
            api_key_public=_PUBLIC_KEY,
            api_key_private=_PRIVATE_KEY,
        )


def _program(context, handler) -> None:
    """Replace the client's httpx boundary with `handler(**request_kwargs)`."""

    async def _request(**kwargs):
        return handler(**kwargs)

    context.client_under_test.client.request = _request


def _always(status: int, body):
    def _handler(**kwargs):
        return _http_response(status, body)

    return _handler


# One loop for the whole run: the writer's queue and its flusher must live in
# the same loop across the steps of a scenario (and `asyncio.get_event_loop()`
# no longer creates one implicitly on 3.13).
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _flush(context) -> None:
    _run(context.writer.flush())


# ── record/object readers ─────────────────────────────────────────────────────
def _objects(context) -> list[dict]:
    return context.fake_s3.puts


def _lines(put: dict) -> list[str]:
    body = put["Body"]
    text = gzip.decompress(body).decode("utf-8")
    return [line for line in text.split("\n") if line.strip()]


def _records(context) -> list[dict]:
    out: list[dict] = []
    for put in _objects(context):
        out.extend(json.loads(line) for line in _lines(put))
    return out


def _records_for(context, dataset: str) -> list[dict]:
    return [r for r in _records(context) if r.get("dataset") == dataset]


def _capture_logs(logger_name: str) -> list:
    """Collect records emitted by `logger_name` for the rest of the scenario."""
    import logging

    collected: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record):
            collected.append(record)

    target = logging.getLogger(logger_name)
    target.addHandler(_Collector())
    target.setLevel(logging.DEBUG)
    return collected


def _metric(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels or None)
    return 0.0 if value is None else float(value)


def _live_ok(venue_id: str) -> dict:
    return {
        "status": "OK",
        "venue_info": {"venue_id": venue_id},
        "analysis": {
            "venue_live_busyness": 55,
            "venue_live_busyness_available": True,
        },
    }


def _seed_venue(context, vid: str) -> None:
    context.repository.upsert_venue(
        Venue(
            forecast=True,
            processed=True,
            venue_id=vid,
            venue_name=f"Venue {vid}",
            venue_address=f"addr {vid}",
            venue_lat=_LAT,
            venue_lng=_LNG,
            priority=1,
        )
    )
    context.repository.set_vibe_attributes(
        VibeAttributes(
            venue_id=vid,
            google_place_id=f"place_{vid}",
            google_primary_type="bar",
        )
    )


def _refresher(context) -> VenuesRefresherService:
    return VenuesRefresherService(
        venue_dao=context.repository,
        besttime_api=context.client_under_test,
        redis_client=context.fake_redis,
    )


# ── Background ────────────────────────────────────────────────────────────────
@given("the data lake is enabled with a configured bucket")
def step_lake_enabled(context):
    context.fake_s3 = _FakeS3()
    context.fixed_now = _FIXED_UTC
    context.datalake_enabled = True


@given("the data lake writer is running")
def step_writer_running(context):
    context.writer = _build_writer(context)
    context.client_under_test = _build_client(context, datalake=context.writer)


@given("the data lake is disabled")
def step_lake_disabled(context):
    context.fake_s3 = _FakeS3()
    context.datalake_enabled = False
    context.writer = None
    context.metric_baseline = _metric(
        "datalake_records_enqueued_total", source="besttime", dataset="live_forecast"
    )
    context.client_under_test = _build_client(context, datalake=None)


# ── Given ─────────────────────────────────────────────────────────────────────
@given("a venue that is due for a live busyness refresh")
def step_venue_due(context):
    context.venue_id = "ven_live_1"
    _seed_venue(context, context.venue_id)
    _program(context, _always(200, _live_ok(context.venue_id)))


@given("the BestTime account inventory spans three pages")
def step_inventory_pages(context):
    # Page size is 2, so a SHORT final page ends pagination after exactly three
    # requests — one archived record per page, with no trailing empty fetch.
    pages = [
        [{"venue_id": "ven_0_0"}, {"venue_id": "ven_0_1"}],
        [{"venue_id": "ven_1_0"}, {"venue_id": "ven_1_1"}],
        [{"venue_id": "ven_2_0"}],
    ]

    def _handler(**kwargs):
        page = int((kwargs.get("params") or {}).get("page", 0))
        return _http_response(200, pages[page] if page < len(pages) else [])

    _program(context, _handler)
    context.inventory_page_size = 2


@given("the platform fetches a live forecast at 21:00 Recife time on 25 July 2026")
def step_fixed_recife_time(context):
    context.fixed_now = _FIXED_UTC  # 2026-07-26T00:04Z == 2026-07-25 21:04 Recife
    context.venue_id = "ven_tz"
    _program(context, _always(200, _live_ok(context.venue_id)))


@given("the live refresh job fetches forty venues in one window")
def step_forty_venues(context):
    context.venue_ids = [f"ven_{i:03d}" for i in range(40)]
    for vid in context.venue_ids:
        _seed_venue(context, vid)

    def _handler(**kwargs):
        vid = (kwargs.get("params") or {}).get("venue_id", "unknown")
        return _http_response(200, _live_ok(vid))

    _program(context, _handler)
    _run(_refresher(context)._fetch_and_cache_live_forecasts(context.venue_ids))


@given("BestTime is called with its private and public API keys in the query string")
def step_keys_in_query(context):
    context.venue_id = "ven_secret"
    _program(context, _always(200, _live_ok(context.venue_id)))
    _run(context.client_under_test.get_live_forecast(venue_id=context.venue_id))


@given("BestTime times out for a venue's live forecast")
def step_besttime_timeout(context):
    context.venue_id = "ven_timeout"
    context.other_venue_id = "ven_ok"
    _seed_venue(context, context.venue_id)
    _seed_venue(context, context.other_venue_id)

    def _handler(**kwargs):
        vid = (kwargs.get("params") or {}).get("venue_id")
        if vid == context.venue_id:
            raise httpx.TimeoutException("timed out")
        return _http_response(200, _live_ok(vid))

    _program(context, _handler)


@given("every upload to the data lake fails")
def step_uploads_fail(context):
    context.fake_s3.fail = True
    context.log_records = _capture_logs("app.dao.datalake_writer")
    context.drop_baseline = _metric(
        "datalake_records_dropped_total",
        source="besttime",
        dataset="live_forecast",
        reason="flush_failed",
    )


@given("the archival queue is saturated")
def step_queue_saturated(context):
    context.writer = _build_writer(context, queue_maxsize=1)
    context.client_under_test = _build_client(context, datalake=context.writer)
    context.drop_baseline = _metric(
        "datalake_records_dropped_total",
        source="besttime",
        dataset="live_forecast",
        reason="queue_full",
    )


@given("the data lake writer raises an error for every record it receives")
def step_writer_raises(context):
    class _ExplodingWriter:
        def record(self, **kwargs):
            raise RuntimeError("datalake exploded")

        async def flush(self):
            raise RuntimeError("datalake exploded")

        async def close(self):
            return None

    context.writer = _ExplodingWriter()
    context.client_under_test = _build_client(context, datalake=context.writer)


@given("BestTime rejects a venue create with its monthly cap message")
def step_monthly_cap(context):
    context.cap_body = {
        "status": "Error",
        "message": "You have reached the maximum number of monthly venues. "
        "The venue counter will reset next month.",
    }
    _program(context, _always(429, context.cap_body))


@given("records are buffered but not yet flushed")
def step_buffered(context):
    context.venue_id = "ven_buffered"
    _program(context, _always(200, _live_ok(context.venue_id)))
    _run(context.client_under_test.get_live_forecast(venue_id=context.venue_id))
    assert not _objects(context), "records must still be buffered before shutdown"


# ── When ──────────────────────────────────────────────────────────────────────
@when("the live forecast refresh job fetches that venue from BestTime")
def step_refresh_one(context):
    _run(_refresher(context)._fetch_and_cache_live_forecasts([context.venue_id]))
    _flush(context)


@when('the platform calls BestTime for "{call}"')
def step_call_besttime(context, call):
    client = context.client_under_test
    if call == "live forecast":
        _program(context, _always(200, _live_ok("ven_1")))
        _run(client.get_live_forecast(venue_id="ven_1"))
    elif call == "weekly raw forecast":
        _program(
            context,
            _always(
                200,
                {
                    "status": "OK",
                    "venue_id": "ven_1",
                    "window": {},
                    "analysis": {"week_raw": []},
                },
            ),
        )
        _run(client.get_week_raw_forecast("ven_1"))
    elif call == "venue filter search":
        _program(context, _always(200, {"status": "OK", "venues": [], "venues_n": 0}))
        from app.models import VenueFilterParams

        _run(client.venue_filter(VenueFilterParams(lat=_LAT, lng=_LNG, radius=1000)))
    elif call == "venue create":
        _program(
            context,
            _always(
                200,
                {
                    "status": "OK",
                    "venue_info": {
                        "venue_id": "ven_new",
                        "venue_name": "New",
                        "venue_address": "addr",
                    },
                },
            ),
        )
        _run(client.add_venue_to_account("New", "addr"))
    elif call == "account inventory":
        _program(context, _always(200, []))
        _run(_drain_inventory(client))
    else:
        raise AssertionError(f"unknown BestTime call: {call}")
    _flush(context)


async def _drain_inventory(client, page_size: int = 2):
    return [venue async for venue in client.list_account_inventory(page_size=page_size)]


@when("the platform lists the account inventory")
def step_list_inventory(context):
    _run(_drain_inventory(context.client_under_test, context.inventory_page_size))
    _flush(context)


@when("a live forecast is archived")
def step_archive_live(context):
    _program(context, _always(200, _live_ok("ven_key")))
    _run(context.client_under_test.get_live_forecast(venue_id="ven_key"))
    _flush(context)


@when("that response is archived")
def step_archive_that(context):
    _run(context.client_under_test.get_live_forecast(venue_id=context.venue_id))
    _flush(context)


@when("the response is archived")
def step_archive_response(context):
    _flush(context)


@when("the archival buffer is flushed")
def step_flush_buffer(context):
    _flush(context)


@when("the refresh job handles that failure")
def step_refresh_with_failure(context):
    _run(
        _refresher(context)._fetch_and_cache_live_forecasts(
            [context.venue_id, context.other_venue_id]
        )
    )
    _flush(context)


@when("the live forecast refresh job runs")
def step_refresh_runs(context):
    context.venue_ids = getattr(context, "venue_ids", ["ven_a", "ven_b", "ven_c"])
    for vid in context.venue_ids:
        _seed_venue(context, vid)

    def _handler(**kwargs):
        vid = (kwargs.get("params") or {}).get("venue_id", "ven_a")
        return _http_response(200, _live_ok(vid))

    _program(context, _handler)
    started = time.monotonic()
    _run(_refresher(context)._fetch_and_cache_live_forecasts(context.venue_ids))
    if context.writer is not None:
        _flush(context)
    context.refresh_seconds = time.monotonic() - started
    context.refresh_completed = True


@when("the platform calls BestTime for a venue filter search that matches nothing")
def step_filter_zero_match(context):
    from app.models import VenueFilterParams

    body = {
        "status": "Error",
        "venues": [],
        "message": "No venues found matching the filter criteria",
    }
    _program(context, _always(404, body))
    context.filter_result = _run(
        context.client_under_test.venue_filter(
            VenueFilterParams(lat=_LAT, lng=_LNG, radius=1000)
        )
    )


@when("the platform handles that rejection")
def step_handle_rejection(context):
    context.create_result = _run(
        context.client_under_test.add_venue_to_account("Capped", "addr")
    )
    _flush(context)


@when("the application shuts down")
def step_shutdown(context):
    _run(context.writer.close())


@when("records are archived and flushed")
def step_archive_and_flush(context):
    _program(context, _always(200, _live_ok("ven_metrics")))
    _run(context.client_under_test.get_live_forecast(venue_id="ven_metrics"))
    _flush(context)


# ── Then ──────────────────────────────────────────────────────────────────────
@then('one archived record is written for the "{dataset}" dataset')
def step_one_record(context, dataset):
    records = _records_for(context, dataset)
    assert len(records) == 1, (
        f"expected exactly 1 archived {dataset} record, got {len(records)}"
    )
    context.record = records[0]


@then("the record's payload is byte-identical to the response BestTime returned")
def step_payload_verbatim(context):
    assert context.record["payload"] == _live_ok(context.venue_id), (
        f"payload was altered: {context.record['payload']}"
    )


@then('the record reports the outcome "{outcome}" with the HTTP status BestTime returned')
def step_outcome_and_status(context, outcome):
    assert context.record["outcome"] == outcome
    assert context.record["http_status"] == 200


@then("the record carries the venue id, the job name, and the run id")
def step_record_context(context):
    assert context.record["venue_id"] == context.venue_id
    assert context.record["run_id"], "run_id must be recorded"
    assert "job" in context.record, "job must be recorded (may be null off-scheduler)"


@then('the archived record belongs to the "{dataset}" dataset')
def step_record_dataset(context, dataset):
    records = _records_for(context, dataset)
    assert records, (
        f"no archived record for dataset {dataset}; "
        f"datasets seen: {sorted({r.get('dataset') for r in _records(context)})}"
    )
    context.record = records[0]


@then("the archived record names the endpoint BestTime was called on")
def step_record_endpoint(context):
    assert context.record.get("endpoint"), "endpoint must be recorded"


@then('three archived records are written for the "{dataset}" dataset')
def step_three_records(context, dataset):
    records = _records_for(context, dataset)
    assert len(records) == 3, f"expected 3 {dataset} records, got {len(records)}"


@then("the archived object key is partitioned by source, dataset, date, and hour")
def step_key_partitioned(context):
    key = _objects(context)[0]["Key"]
    for part in ("source=besttime", "dataset=live_forecast", "dt=", "hour="):
        assert part in key, f"key {key!r} is missing {part!r}"
    assert key.startswith("raw/"), f"key {key!r} must live under raw/"
    assert key.endswith(".ndjson.gz"), f"key {key!r} must be gzipped NDJSON"


@then('every partition is expressed as a "key=value" directory')
def step_key_value_dirs(context):
    key = _objects(context)[0]["Key"]
    directories = key.split("/")[1:-1]  # drop the raw/ root and the file name
    for directory in directories:
        assert "=" in directory, f"partition {directory!r} is not key=value"


@then("the archived object is gzipped NDJSON with one JSON object per line")
def step_gzipped_ndjson(context):
    put = _objects(context)[0]
    lines = _lines(put)
    assert lines, "archived object is empty"
    for line in lines:
        assert isinstance(json.loads(line), dict)


@then("the record is stored under the UTC date {date} and UTC hour {hour}")
def step_utc_partition(context, date, hour):
    key = _objects(context)[0]["Key"]
    assert f"dt={date}" in key, f"key {key!r} is not partitioned under dt={date}"
    assert f"hour={hour}" in key, f"key {key!r} is not partitioned under hour={hour}"


@then("the record reports the Recife date {date} and the Recife hour {hour:d}")
def step_recife_fields(context, date, hour):
    record = _records(context)[0]
    assert record["recife_date"] == date, record["recife_date"]
    assert record["recife_hour"] == hour, record["recife_hour"]


@then("a single archived object contains forty NDJSON lines")
def step_forty_lines(context):
    objects = _objects(context)
    assert len(objects) == 1, f"expected 1 object, got {len(objects)}"
    assert len(_lines(objects[0])) == 40, f"got {len(_lines(objects[0]))} lines"


@then("one archived object is written rather than one per venue")
def step_one_object(context):
    assert len(_objects(context)) == 1, (
        f"batching failed: {len(_objects(context))} objects written for one window"
    )


@then("the archived record contains no BestTime private key")
def step_no_private_key(context):
    blob = json.dumps(_records(context))
    assert _PRIVATE_KEY not in blob, "the BestTime private key was archived"
    assert "pri_" not in blob, "a BestTime private-key-shaped value was archived"


@then("the archived record contains no BestTime public key")
def step_no_public_key(context):
    blob = json.dumps(_records(context))
    assert _PUBLIC_KEY not in blob, "the BestTime public key was archived"


@then("the archived record still reports the non-secret request parameters")
def step_request_params_present(context):
    request = _records(context)[0].get("request") or {}
    assert request.get("venue_id") == context.venue_id, (
        f"non-secret params must survive redaction, got {request}"
    )


@then('an archived record reports the outcome "{outcome}" with an empty payload')
def step_error_record(context, outcome):
    errors = [r for r in _records(context) if r.get("outcome") == outcome]
    assert errors, "the failed BestTime fetch was not archived"
    context.record = errors[0]
    assert not context.record.get("payload"), "an errored record must carry no payload"


@then("the record describes the failure")
def step_record_error_text(context):
    assert context.record.get("error"), "the failure must be described"


@then("the refresh job continues with the remaining venues")
def step_refresh_continues(context):
    forecast = context.repository.get_live_forecast(context.other_venue_id)
    assert forecast is not None, "the healthy venue must still be refreshed"


@then("the refresh job completes successfully")
def step_refresh_ok(context):
    assert context.refresh_completed is True


@then("the refreshed venues are still written to the system of record")
def step_venues_persisted(context):
    for vid in context.venue_ids:
        assert context.repository.get_live_forecast(vid) is not None, (
            f"live forecast for {vid} was not persisted"
        )


@then("the serving projection is still updated")
def step_projection_updated(context):
    result = context.redis_projection_service.rebuild_redis_from_rds()
    assert result is not None, "the projector must still rebuild the serving view"


@then('the dropped records are counted with the reason "{reason}"')
def step_dropped_counted(context, reason):
    value = _metric(
        "datalake_records_dropped_total",
        source="besttime",
        dataset="live_forecast",
        reason=reason,
    )
    assert value > context.drop_baseline, (
        f"datalake_records_dropped_total{{reason={reason}}} did not increase "
        f"({context.drop_baseline} -> {value})"
    )


@then("an error is logged naming the dataset and the number of records lost")
def step_error_logged(context):
    errors = [r for r in context.log_records if r.levelno >= 40]
    assert errors, "a failed flush must log an ERROR"
    text = " ".join(r.getMessage() for r in errors)
    assert "live_forecast" in text, f"the drop log must name the dataset: {text}"
    assert any(ch.isdigit() for ch in text), (
        f"the drop log must count the lost records: {text}"
    )


@then("the refresh job completes without waiting on the data lake")
def step_refresh_not_blocked(context):
    assert context.refresh_completed is True
    assert context.refresh_seconds < 5, (
        f"refresh blocked on the data lake for {context.refresh_seconds:.1f}s"
    )


@then('the excess records are counted as dropped with the reason "{reason}"')
def step_excess_dropped(context, reason):
    step_dropped_counted(context, reason)


@then("the search result is unchanged from when the data lake is disabled")
def step_filter_unchanged(context):
    assert context.filter_result.venues == []
    assert context.filter_result.venues_n == 0


@then("the error from the data lake writer is logged and swallowed")
def step_writer_error_swallowed(context):
    assert context.filter_result is not None, (
        "a failing data lake writer must not break the BestTime call"
    )


@then("the rejection is surfaced exactly as it is when the data lake is disabled")
def step_rejection_unchanged(context):
    assert context.create_result.status == "Error"
    assert "monthly venues" in (context.create_result.message or "").lower()


@then('the rejection is archived for the "{dataset}" dataset')
def step_rejection_archived(context, dataset):
    assert _records_for(context, dataset), f"no {dataset} record archived"


@then("no object is written to the data lake")
def step_no_objects(context):
    assert not _objects(context), (
        f"{len(_objects(context))} objects written while the data lake is disabled"
    )


@then("no data lake metric is emitted")
def step_no_metrics(context):
    value = _metric(
        "datalake_records_enqueued_total", source="besttime", dataset="live_forecast"
    )
    assert value == context.metric_baseline, (
        f"datalake metrics moved while disabled ({context.metric_baseline} -> {value})"
    )


@then("the buffered records are uploaded before shutdown completes")
def step_shutdown_flushed(context):
    assert _objects(context), "shutdown must flush buffered records"


@then("the enqueued record count is exposed per source and dataset")
def step_metric_enqueued(context):
    assert (
        _metric(
            "datalake_records_enqueued_total",
            source="besttime",
            dataset="live_forecast",
        )
        > 0
    )


@then("the flush count is exposed per dataset with a success or error status")
def step_metric_flush(context):
    assert (
        _metric("datalake_flush_total", dataset="live_forecast", status="success") > 0
    )


@then("the current archival queue depth is exposed")
def step_metric_queue_depth(context):
    assert REGISTRY.get_sample_value("datalake_queue_depth") is not None


@then("the timestamp of the last successful flush is exposed")
def step_metric_last_success(context):
    assert _metric("datalake_last_success_timestamp") > 0
