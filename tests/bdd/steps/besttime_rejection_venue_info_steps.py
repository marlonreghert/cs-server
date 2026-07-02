"""Behave steps for tests/bdd/api/besttime-rejection-venue-info.feature.

Rides the real-BestTime-client harness from besttime_add_response_parse_steps
(httpx.MockTransport at the HTTP boundary). The Givens here program the raw
4xx rejection body — a parseable envelope whose `venue_info` block carries no
`venue_id` (BestTime created nothing) — plus the /venues/filter geo-fallback
reply. The shared When steps drive the production model -> client -> handler
path; shared Thens cover the quota release and the bad-response guard.
"""
from __future__ import annotations

from behave import given, then  # type: ignore[import-untyped]

from besttime_add_response_parse_steps import (  # type: ignore[import-not-found]
    _ADD_VENUE_METRIC,
    _BESTTIME_ERRORS_METRIC,
    _VENUE,
    _metric_delta,
)

_REJECTION_MESSAGE = (
    "The venue could not be found or does not have enough foot traffic data."
)

# The prod 2026-07-02 15:14 shape: HTTP 404, parseable envelope, venue_info
# WITHOUT venue_id (nothing was created), status/message present.
_REJECTION_BODY = {
    "status": "Error",
    "message": _REJECTION_MESSAGE,
    "venue_info": {
        "venue_name": _VENUE["venue_name"],
        "venue_address": _VENUE["venue_address"],
    },
}


def _program_rejection(context) -> None:
    context.besttime_http_body = _REJECTION_BODY
    context.besttime_http_status = 404


@given(
    "BestTime rejects a create with an explanatory message and a venue info "
    "block without a venue id"
)
def step_rejection_with_message_and_idless_info(context):
    _program_rejection(context)


@given("BestTime rejects a create with a venue info block without a venue id")
def step_rejection_with_idless_info(context):
    _program_rejection(context)


@given("no nearby venue matches in the geo fallback")
def step_filter_no_match(context):
    context.besttime_filter_body = {"status": "OK", "venues": [], "venues_n": 0}


@given("a nearby venue matches in the geo fallback")
def step_filter_match(context):
    context.besttime_filter_body = {
        "status": "OK",
        "venues_n": 1,
        "venues": [
            {
                "day_int": 0,
                "day_raw": [10] * 24,
                "venue_id": "ven_geo_fallback_match_001",
                "venue_name": _VENUE["venue_name"],
                "venue_address": _VENUE["venue_address"],
                "venue_lat": _VENUE["venue_lat"],
                "venue_lng": _VENUE["venue_lng"],
                "venue_type": "BAR",
            }
        ],
    }


@then("the add fails as a rejection, not as an unparseable response")
def step_fails_as_rejection(context):
    assert context.response.status_code == 502, (
        f"expected 502, got {context.response.status_code} "
        f"body={context.response.text[:400]}"
    )
    detail = (context.response.json().get("detail") or "").lower()
    assert "unparseable" not in detail, context.response.text
    assert "rejected" in detail, context.response.text
    assert (
        _metric_delta(
            context,
            _ADD_VENUE_METRIC,
            "bad_response",
            {"result": "besttime_bad_response"},
        )
        == 0
    ), "rejection was misclassified as besttime_bad_response"
    assert (
        _metric_delta(
            context,
            _BESTTIME_ERRORS_METRIC,
            "invalid_schema",
            {"endpoint": "/forecasts", "error_type": "invalid_response_schema"},
        )
        == 0
    ), "rejection was misclassified as an invalid response schema"


@then("the error response carries BestTime's message")
def step_error_carries_besttime_message(context):
    body = context.response.json()
    assert body.get("besttime_message") == _REJECTION_MESSAGE, (
        f"besttime_message missing or wrong: {context.response.text[:400]}"
    )


@then("the geo fallback was attempted")
def step_geo_fallback_attempted(context):
    paths = [req["path"] for req in context.besttime_http_requests]
    assert any(path.endswith("/venues/filter") for path in paths), (
        f"no /venues/filter call was made; calls: {paths}"
    )


@then("the add completes as matched via geo fallback")
def step_completes_via_geo_fallback(context):
    assert context.response.status_code == 200, (
        f"expected 200, got {context.response.status_code} "
        f"body={context.response.text[:400]}"
    )
    body = context.response.json()
    assert body.get("status") == "matched_via_geo_fallback", context.response.text
    assert body.get("venue_id") == "ven_geo_fallback_match_001", context.response.text
