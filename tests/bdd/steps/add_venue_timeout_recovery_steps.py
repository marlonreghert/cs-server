"""Behave steps for tests/bdd/api/add-venue-timeout-recovery.feature.

Rides the real-BestTime-client harness from besttime_add_response_parse_steps
(httpx.MockTransport at the HTTP boundary): the Givens here program the raw
HTTP responses via the context.besttime_http_* / besttime_inventory_*
attributes, and the shared "an operator adds the venue" When steps drive the
production client + handler end-to-end. Shared Thens (ledger, inline
enrichment, quota release) are reused from that module too.
"""
from __future__ import annotations

import httpx
from behave import given, then, when  # type: ignore[import-untyped]

from app.config import Settings

# BestTime's normalized rendering of the venue that the shared harness posts
# (besttime_add_response_parse_steps._VENUE = "Laca Burguer Boa Viagem",
# "Av. Conselheiro Aguiar 123, Recife - PE, Brazil"): accents, case, and
# punctuation differ, so the reconcile must fold to match.
_RECOVERED_VENUE_ID = "ven_recovered_timeout_001"
_INVENTORY_MATCH_ROW = {
    "venue_id": _RECOVERED_VENUE_ID,
    "venue_name": "LAÇA BURGUER, BOA VIAGEM",
    "venue_address": "Av. Conselheiro Aguiar, 123, Recife - PE, Brazil",
    "venue_lat": -8.119,
    "venue_lng": -34.904,
    "venue_forecasted": True,
}
_INVENTORY_UNRELATED_ROW = {
    "venue_id": "ven_unrelated_999",
    "venue_name": "Completely Different Bar",
    "venue_address": "Av. Outra Coisa 999, Olinda - PE, Brazil",
    "venue_lat": -8.01,
    "venue_lng": -34.85,
    "venue_forecasted": True,
}


# ---------------------------------------------------------------------------
# Givens
# ---------------------------------------------------------------------------


@given("a venue create that exceeds the BestTime timeout")
def step_create_times_out(context):
    context.besttime_http_error = httpx.ReadTimeout("simulated create timeout")
    # Skip the production grace delay so the BDD run stays fast.
    context.add_venue_handler.timeout_recovery_grace_seconds = 0.0


@given("the venue appears in the BestTime account inventory")
def step_venue_in_inventory(context):
    context.besttime_inventory_body = [_INVENTORY_MATCH_ROW]


@given("the venue does not appear in the BestTime account inventory")
def step_venue_not_in_inventory(context):
    context.besttime_inventory_body = [_INVENTORY_UNRELATED_ROW]


@given("the account inventory read also fails")
def step_inventory_read_fails(context):
    context.besttime_inventory_error = httpx.ConnectError(
        "simulated inventory list failure"
    )


@given("BestTime rejects a venue create with an explanatory message")
def step_besttime_rejects_with_message(context):
    context.besttime_reject_message = (
        "Venue cannot be forecasted: not enough foot traffic data"
    )
    context.besttime_http_body = {
        "status": "Error",
        "message": context.besttime_reject_message,
    }
    # The geo fallback that follows the rejection finds nothing at the mock
    # transport (404 -> fallback unavailable), the path that must still carry
    # BestTime's own message through to the operator.


@given("the add-venue timeout is configured at sixty seconds")
def step_timeout_configured_sixty(context):
    default = Settings.model_fields["besttime_add_venue_timeout_seconds"].default
    assert default == 60.0, (
        f"besttime_add_venue_timeout_seconds default must be 60.0, got {default}"
    )


# ---------------------------------------------------------------------------
# Whens (the plain add steps are shared from besttime_add_response_parse_steps)
# ---------------------------------------------------------------------------


@when("an operator adds a venue and BestTime responds within that window")
def step_operator_adds_within_window(context):
    context.besttime_http_body = {
        "status": "OK",
        "venue_info": {
            "venue_id": "ven_within_window_001",
            "venue_name": "Laca Burguer Boa Viagem",
            "venue_address": "Av. Conselheiro Aguiar 123, Recife - PE, Brazil",
            "venue_lat": -8.119,
            "venue_lon": -34.904,
        },
        "analysis": [],
    }
    context.execute_steps("When an operator adds the venue")


# ---------------------------------------------------------------------------
# Thens
# ---------------------------------------------------------------------------


@then("the add returns created with a recovered-from-timeout marker")
def step_created_with_recovery_marker(context):
    assert context.response.status_code == 201, (
        f"expected 201, got {context.response.status_code} "
        f"body={context.response.text[:500]}"
    )
    body = context.response.json()
    assert body.get("recovered_from_timeout") is True, body
    assert body.get("venue_id") == _RECOVERED_VENUE_ID, body


@then("no second create call is made to BestTime")
def step_single_create_call(context):
    creates = [
        r
        for r in context.besttime_http_requests
        if r["method"] == "POST" and r["path"].endswith("/forecasts")
    ]
    assert len(creates) == 1, (
        f"expected exactly one POST /forecasts, saw {creates} "
        f"(all requests: {context.besttime_http_requests})"
    )


@then("the add fails telling the operator the create timed out unconfirmed")
def step_fails_timed_out_unconfirmed(context):
    assert context.response.status_code == 502, (
        f"expected 502, got {context.response.status_code} "
        f"body={context.response.text[:500]}"
    )
    detail = (context.response.json().get("detail") or "").lower()
    assert "timed out" in detail, context.response.text
    assert "not confirmed" in detail, context.response.text


@then("the operator is told a later retry maps to the same venue id")
def step_retry_is_duplicate_safe(context):
    detail = (context.response.json().get("detail") or "").lower()
    assert "retry" in detail and "same venue" in detail, context.response.text


@then("the add fails with the timeout error")
def step_fails_with_timeout_error(context):
    assert context.response.status_code == 502, (
        f"expected 502, got {context.response.status_code} "
        f"body={context.response.text[:500]}"
    )
    detail = (context.response.json().get("detail") or "").lower()
    assert "timed out" in detail, context.response.text


@then("the error response includes BestTime's message alongside the detail")
def step_error_includes_besttime_message(context):
    body = context.response.json()
    assert body.get("detail"), body
    assert body.get("besttime_message") == context.besttime_reject_message, body


@then("the add succeeds instead of timing out early")
def step_add_succeeds(context):
    assert context.response.status_code == 201, (
        f"expected 201, got {context.response.status_code} "
        f"body={context.response.text[:500]}"
    )
