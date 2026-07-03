"""Behave steps for tests/bdd/api/add-venue-no-live-rate-limit.feature.

Rides the real-BestTime-client harness from besttime_add_response_parse_steps
(httpx.MockTransport at the HTTP boundary, every request path recorded in
context.besttime_http_requests). Pins two credit-safety behaviors of the add
flow:

  1. No request to GET /forecasts/live is ever made while adding — live
     busyness spends BestTime credits and belongs to the live pipeline only.
  2. An HTTP 429 (rate limit) on the POST /forecasts create is retried by the
     client (bounded, Retry-After-aware) instead of failing the add or being
     laundered through the geo-fallback path as a venue rejection.
"""
from __future__ import annotations

from behave import given, then  # type: ignore[import-untyped]

from besttime_add_response_parse_steps import (  # type: ignore[import-not-found]
    _real_success_body,
)

_RATE_LIMIT_BODY = {"status": "Error", "message": "Too many requests."}


@given("BestTime rate-limits the create once and then accepts it")
def step_rate_limit_once_then_success(context):
    # Retry-After: 0 keeps the scenario wall-clock free — the client honors
    # the header, so the retry happens immediately.
    context.besttime_http_sequence = [
        (429, _RATE_LIMIT_BODY, {"Retry-After": "0"}),
        (200, _real_success_body(), None),
    ]


@then("no request was made to the live-busyness endpoint")
def step_no_live_request(context):
    live_calls = [
        r for r in context.besttime_http_requests
        if r["path"].endswith("/forecasts/live")
    ]
    assert not live_calls, (
        f"add flow fetched live busyness (spends credits): {live_calls}"
    )


@then("the create endpoint was called twice")
def step_create_called_twice(context):
    creates = [
        r for r in context.besttime_http_requests
        if r["path"].endswith("/forecasts")
        and not r["path"].endswith("/forecasts/live")
    ]
    assert len(creates) == 2, (
        f"expected the 429 to be retried exactly once (2 creates), "
        f"saw {len(creates)}: {creates}"
    )
