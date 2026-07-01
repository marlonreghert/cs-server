"""Behave steps for tests/bdd/api/besttime-add-response-parse.feature.

Unlike add_venue_by_address_steps (which program already-parsed models into
the harness's BestTime stub), these scenarios exercise the REAL
BestTimeAPIClient: the stub boundary moves down to HTTP via
httpx.MockTransport, so the response parsing and error classification under
test run production code end-to-end (model -> client -> handler).
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import httpx
from behave import given, then, when  # type: ignore[import-untyped]
from prometheus_client import REGISTRY

from app.api.besttime_client import BestTimeAPIClient
from app.handlers.add_venue_handler import AddVenueByAddressRequest

_DAY_TEXT = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

_VENUE = {
    "venue_name": "Laca Burguer Boa Viagem",
    "venue_address": "Av. Conselheiro Aguiar 123, Recife - PE, Brazil",
    "venue_lat": -8.119,
    "venue_lng": -34.904,
}
_VENUE_ID = "ven_77374d31real0001"

_ANALYSIS_DROPPED_METRIC = "besttime_add_venue_analysis_days_dropped_total"
_ADD_VENUE_METRIC = "add_venue_by_address_total"
_BESTTIME_ERRORS_METRIC = "besttime_api_errors_total"


def _real_day_entry(day_int: int) -> dict:
    """One analysis day in BestTime's REAL POST /forecasts shape: the day
    number lives inside `day_info`; the hourly list sits alongside it (there
    is no top-level `day_int`)."""
    return {
        "day_info": {
            "day_int": day_int,
            "day_max": 100,
            "day_mean": 45,
            "day_rank_max": 5,
            "day_rank_mean": 6,
            "day_text": _DAY_TEXT[day_int],
            "venue_open": 18,
            "venue_closed": 2,
        },
        "day_raw": [min(99, day_int * 3 + hour) for hour in range(24)],
        "busy_hours": [20, 21],
        "quiet_hours": [7],
    }


def _real_success_body(analysis=None) -> dict:
    """The real POST /forecasts success envelope (per the 2026-07-01 prod log)."""
    return {
        "status": "OK",
        "venue_info": {
            "venue_id": _VENUE_ID,
            "venue_name": _VENUE["venue_name"],
            "venue_address": _VENUE["venue_address"],
            "venue_lat": _VENUE["venue_lat"],
            "venue_lon": _VENUE["venue_lng"],
            "venue_timezone": "America/Recife",
            "rating": 4.7,
            "reviews": 351,
            "price_level": 2,
        },
        "analysis": (
            analysis if analysis is not None else [_real_day_entry(d) for d in range(7)]
        ),
    }


class _RecordingEnrichmentService:
    """Stands in for GooglePlacesEnrichmentService; records inline enrichment."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enrich_venue(self, venue_id, google_place_id, force_refresh=False):
        self.calls.append(
            {
                "venue_id": venue_id,
                "google_place_id": google_place_id,
                "force_refresh": force_refresh,
            }
        )


class _StepResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


def _metric(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _snapshot_metrics(context) -> None:
    context.metric_before = {
        "bad_response": _metric(_ADD_VENUE_METRIC, {"result": "besttime_bad_response"}),
        "besttime_error": _metric(_ADD_VENUE_METRIC, {"result": "besttime_error"}),
        "invalid_schema": _metric(
            _BESTTIME_ERRORS_METRIC,
            {"endpoint": "/forecasts", "error_type": "invalid_response_schema"},
        ),
        "analysis_dropped": _metric(_ANALYSIS_DROPPED_METRIC),
    }


def _metric_delta(context, name: str, key: str, labels: dict | None = None) -> float:
    return _metric(name, labels) - context.metric_before[key]


def _created_venue_id(context) -> str:
    body = context.response.json()
    assert "venue_id" in body, (
        f"add returned no venue (status {context.response.status_code}): "
        f"{context.response.text[:300]}"
    )
    return body["venue_id"]


def _install_real_besttime(context) -> None:
    """Swap the harness's programmable BestTime stub for the real client over
    a mocked HTTP transport. The scenario's Given programmed either a raw JSON
    body (context.besttime_http_body) or a transport error
    (context.besttime_http_error) for POST /forecasts."""
    body = getattr(context, "besttime_http_body", None)
    error = getattr(context, "besttime_http_error", None)

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/forecasts/live"):
            # Inline live forecast is best-effort in the handler; reply
            # "no live data" so the add path under test stays deterministic.
            return httpx.Response(
                200, json={"status": "Error", "message": "no live data"}
            )
        if path.endswith("/forecasts"):
            if error is not None:
                raise error
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"status": "Error", "message": "unexpected"})

    client = BestTimeAPIClient(
        base_url="https://besttime.invalid/api/v1",
        api_key_public="test_public",
        api_key_private="test_private",
    )
    # The pooled client from __init__ never connected; replace it with the
    # mock-transport client so no real network is reachable.
    asyncio.run(client.client.aclose())
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    context.add_venue_handler.besttime = client
    context.real_besttime = client


def _wire_inline_enrichment(context) -> None:
    recorder = _RecordingEnrichmentService()
    context.enrichment_recorder = recorder
    context.add_venue_handler.google_places_enrichment_service = recorder
    context.google_places_client.search_place_id = AsyncMock(
        return_value="place_real_add_001"
    )


class _ListLogHandler(logging.Handler):
    def __init__(self, records: list) -> None:
        super().__init__()
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _post_add(context) -> None:
    _install_real_besttime(context)
    _wire_inline_enrichment(context)
    _snapshot_metrics(context)

    request = AddVenueByAddressRequest.model_validate(_VENUE)
    records: list[logging.LogRecord] = []
    handler = _ListLogHandler(records)
    app_logger = logging.getLogger("app")
    app_logger.addHandler(handler)
    try:
        outcome = asyncio.run(context.add_venue_handler.add(request))
    finally:
        app_logger.removeHandler(handler)
    context.captured_logs = records
    context.response = _StepResponse(outcome.status_code, outcome.body)


# ---------------------------------------------------------------------------
# Givens: program the raw HTTP body / transport failure
# ---------------------------------------------------------------------------


@given("BestTime accepts a new venue and replies with its real success payload")
def step_real_success_payload(context):
    context.besttime_http_body = _real_success_body()


@given("each analysis entry nests the day number inside its day info block")
def step_pin_real_analysis_shape(context):
    for entry in context.besttime_http_body["analysis"]:
        assert "day_int" not in entry, entry
        assert "day_int" in entry["day_info"], entry


@given(
    "a BestTime success payload whose analysis mixes parseable and malformed day entries"
)
def step_mixed_analysis_payload(context):
    good = [_real_day_entry(d) for d in (0, 1, 2)]
    bad = [
        # No day number anywhere: cannot be normalized to a WeekRawDay.
        {"day_info": {"day_text": "Someday"}, "day_raw": [1] * 24},
        # Hourly list is not a list of ints.
        {"day_info": {"day_int": 4}, "day_raw": "forecast pending"},
    ]
    context.besttime_http_body = _real_success_body(analysis=good + bad)
    context.expected_cached_days = [0, 1, 2]
    context.expected_uncached_days = [3, 4, 5, 6]
    context.expected_dropped = len(bad)


@given("a BestTime success payload whose analysis cannot be parsed at all")
def step_unparseable_analysis_payload(context):
    context.besttime_http_body = _real_success_body(
        analysis=["forecast-pending", {"nothing": "usable"}]
    )


@given("BestTime replies with a body that has no usable status or venue info")
def step_unparseable_envelope(context):
    context.besttime_http_body = {
        "forecast": "maybe-later",
        "venue_info": "not-an-object",
    }


@given("BestTime cannot be reached at all")
def step_besttime_unreachable(context):
    context.besttime_http_error = httpx.ConnectError("connection refused")


# ---------------------------------------------------------------------------
# Whens
# ---------------------------------------------------------------------------


@when("an operator adds the venue by name and address")
def step_add_by_name_and_address(context):
    _post_add(context)


@when("an operator adds the venue")
def step_add_venue(context):
    _post_add(context)


# ---------------------------------------------------------------------------
# Thens
# ---------------------------------------------------------------------------


@then("the add returns created")
def step_add_created(context):
    assert context.response.status_code == 201, (
        f"expected 201, got {context.response.status_code} "
        f"body={context.response.text[:500]}"
    )
    assert context.response.json().get("status") == "created", context.response.text


@then("the venue is persisted and counted against the monthly ledger")
def step_persisted_and_ledgered(context):
    venue_id = _created_venue_id(context)
    assert context.fake_redis.get(
        f"venues_geo_place_v1:{venue_id}"
    ), f"venue {venue_id} not persisted"
    year_month = context.fixed_year_month
    assert context.fake_redis.sismember(
        f"besttime_touched_v1:{year_month}", venue_id
    ), f"venue {venue_id} not in the {year_month} touched ledger"
    counter = int(context.fake_redis.get(f"venue_add_counter_v1:{year_month}") or 0)
    assert counter == 1, f"expected month counter 1, got {counter}"


@then("the venue is enriched from Google inline")
def step_enriched_inline(context):
    venue_id = _created_venue_id(context)
    calls = context.enrichment_recorder.calls
    assert calls, "inline Google enrichment was never invoked"
    assert calls[0]["venue_id"] == venue_id, calls
    assert calls[0]["force_refresh"] is True, calls


@then("the parseable days are cached as weekly forecast days")
def step_parseable_days_cached(context):
    venue_id = _created_venue_id(context)
    for day in context.expected_cached_days:
        assert context.fake_redis.get(
            f"weekly_forecast_v1:{venue_id}_{day}"
        ), f"day {day} was parseable but not cached"
    for day in context.expected_uncached_days:
        assert not context.fake_redis.get(
            f"weekly_forecast_v1:{venue_id}_{day}"
        ), f"day {day} should not have been cached"


@then("the malformed entries are dropped with a warning")
def step_malformed_dropped_with_warning(context):
    dropped = _metric_delta(context, _ANALYSIS_DROPPED_METRIC, "analysis_dropped")
    assert (
        dropped == context.expected_dropped
    ), f"expected {context.expected_dropped} dropped analysis entries, got {dropped}"
    warnings = [
        r
        for r in context.captured_logs
        if r.levelno == logging.WARNING and "analysis" in r.getMessage().lower()
    ]
    assert warnings, "no WARNING was logged for the dropped analysis entries"


@then("the add still returns created")
def step_add_still_created(context):
    step_add_created(context)


@then("the venue is persisted without cached weekly forecast days")
def step_persisted_without_week_days(context):
    venue_id = _created_venue_id(context)
    assert context.fake_redis.get(
        f"venues_geo_place_v1:{venue_id}"
    ), f"venue {venue_id} not persisted"
    for day in range(7):
        assert not context.fake_redis.get(
            f"weekly_forecast_v1:{venue_id}_{day}"
        ), f"unexpected cached weekly forecast for day {day}"


@then("the add fails with a bad-response error that names an unparseable response")
def step_fails_as_bad_response(context):
    assert context.response.status_code == 502, (
        f"expected 502, got {context.response.status_code} "
        f"body={context.response.text[:500]}"
    )
    detail = (context.response.json().get("detail") or "").lower()
    assert "unparseable" in detail, context.response.text
    assert (
        _metric_delta(
            context,
            _ADD_VENUE_METRIC,
            "bad_response",
            {"result": "besttime_bad_response"},
        )
        == 1
    ), "besttime_bad_response metric was not incremented"
    assert (
        _metric_delta(
            context,
            _BESTTIME_ERRORS_METRIC,
            "invalid_schema",
            {"endpoint": "/forecasts", "error_type": "invalid_response_schema"},
        )
        == 1
    ), "invalid_response_schema metric was not incremented"


@then("the error is not reported as BestTime being unavailable")
def step_not_reported_unavailable(context):
    detail = (context.response.json().get("detail") or "").lower()
    assert "unavailable" not in detail, context.response.text
    assert (
        _metric_delta(
            context, _ADD_VENUE_METRIC, "besttime_error", {"result": "besttime_error"}
        )
        == 0
    ), "besttime_error metric must not move for a parse failure"


@then("the reserved quota slot is released")
def step_quota_slot_released(context):
    year_month = context.fixed_year_month
    counter = int(context.fake_redis.get(f"venue_add_counter_v1:{year_month}") or 0)
    assert counter == 0, f"expected month counter back at 0, got {counter}"


@then("the add fails reporting BestTime is unavailable")
def step_fails_as_unavailable(context):
    assert context.response.status_code == 502, (
        f"expected 502, got {context.response.status_code} "
        f"body={context.response.text[:500]}"
    )
    detail = (context.response.json().get("detail") or "").lower()
    assert "besttime" in detail and "unavailable" in detail, context.response.text
    assert (
        _metric_delta(
            context, _ADD_VENUE_METRIC, "besttime_error", {"result": "besttime_error"}
        )
        == 1
    ), "besttime_error metric was not incremented"
