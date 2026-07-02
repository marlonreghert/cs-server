"""Behave steps for tests/bdd/api/geo-fallback-safe-linking.feature.

Rides the real-BestTime-client harness from besttime_add_response_parse_steps
(httpx.MockTransport at the HTTP boundary) plus the rejection body shape from
besttime_rejection_venue_info_steps. The matcher/body scenarios drive the geo
fallback over the Redis add path; the undo/re-add scenarios repoint the add
handler's venue_dao at the RDS-backed repository so the link writes the system
of record (where `created_at`, soft-delete, and the reactivation exemption
live), then undo/re-add through the real handler + admin route.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from behave import given, then, when  # type: ignore[import-untyped]

from besttime_add_response_parse_steps import (  # type: ignore[import-not-found]
    _VENUE,
    _post_add,
)
from app.models import Venue

# The shared "the add completes as matched via geo fallback" Then (defined in
# besttime_rejection_venue_info_steps) asserts this exact id, so every scenario
# that links a candidate uses it.
_MATCH_ID = "ven_geo_fallback_match_001"

_REJECTION_BODY = {
    "status": "Error",
    "message": "The venue could not be found or does not have enough foot traffic data.",
    "venue_info": {
        "venue_name": _VENUE["venue_name"],
        "venue_address": _VENUE["venue_address"],
    },
}


def _program_rejection(context) -> None:
    context.besttime_http_body = _REJECTION_BODY
    context.besttime_http_status = 404


def _candidate(
    venue_id: str,
    venue_name: str,
    venue_address: str,
    lat: float | None = None,
    lng: float | None = None,
) -> dict:
    return {
        "day_int": 0,
        "day_raw": [10] * 24,
        "venue_id": venue_id,
        "venue_name": venue_name,
        "venue_address": venue_address,
        "venue_lat": _VENUE["venue_lat"] if lat is None else lat,
        "venue_lng": _VENUE["venue_lng"] if lng is None else lng,
        "venue_type": "BAR",
    }


def _program_filter(context, candidates: list[dict]) -> None:
    context.besttime_filter_body = {
        "status": "OK",
        "venues_n": len(candidates),
        "venues": candidates,
    }


def _use_rds_add_path(context) -> None:
    """Repoint the add handler at the RDS-backed repository so the geo-fallback
    link writes the system of record. before_scenario rebuilds a fresh handler
    each scenario, so this is scenario-local."""
    context.add_venue_handler.venue_dao = context.repository


def _counter(context) -> int:
    year_month = getattr(context, "year_month", context.fixed_year_month)
    raw = context.fake_redis.get(f"venue_add_counter_v1:{year_month}")
    return int(raw) if raw else 0


def _undo(context, venue_id: str):
    return context.client.post(
        "/admin/venues/geo-link/undo", json={"venue_id": venue_id}
    )


def _drive_link(context) -> None:
    """Reject the create then geo-fallback-link the candidate through the real
    handler over the RDS repository, leaving the linked venue in the RDS store."""
    _use_rds_add_path(context)
    _program_rejection(context)
    _program_filter(
        context,
        [_candidate(_MATCH_ID, _VENUE["venue_name"], _VENUE["venue_address"])],
    )
    _post_add(context)
    assert context.response.status_code == 200, (
        f"geo-fallback link setup failed: {context.response.status_code} "
        f"{context.response.text[:300]}"
    )
    assert context.response.json().get("status") == "matched_via_geo_fallback", (
        context.response.text
    )
    context.linked_venue_id = _MATCH_ID


# ---------------------------------------------------------------------------
# Givens — matcher / body scenarios
# ---------------------------------------------------------------------------


@given("the geo fallback offers a candidate whose name differs only by accents and punctuation")
def step_candidate_accented(context):
    # Submitted name carries the accents/punctuation; the inventory candidate is
    # BestTime's normalized form. Folding must bridge them.
    context.add_request_override = {"venue_name": "Laça Burguer, Boa Viagem!"}
    _program_filter(
        context,
        [_candidate(_MATCH_ID, "Laca Burguer Boa Viagem", _VENUE["venue_address"])],
    )


@given("the geo fallback offers two same-named candidates at different addresses")
def step_two_candidates(context):
    # The non-overlapping candidate is listed FIRST so a first-hit matcher links
    # the wrong one; the second overlaps the request address and must win.
    other = _candidate(
        "ven_geo_addr_other_001",
        _VENUE["venue_name"],
        "Rua Qualquer 999, Olinda - PE, Brazil",
    )
    overlapping = _candidate(
        "ven_geo_addr_overlap_001",
        _VENUE["venue_name"],
        _VENUE["venue_address"],
    )
    _program_filter(context, [other, overlapping])
    context.expected_link_id = "ven_geo_addr_overlap_001"


@given("the operator's venue name is a short generic word contained in a nearby venue's name")
def step_short_generic_name(context):
    context.add_request_override = {"venue_name": "Bar"}
    _program_filter(
        context,
        [_candidate(_MATCH_ID, "Barcelona Bar", _VENUE["venue_address"])],
    )


@given("the geo fallback offers a candidate whose folded name equals the short name exactly")
def step_exact_short_name(context):
    context.add_request_override = {"venue_name": "Bar"}
    _program_filter(
        context,
        [_candidate(_MATCH_ID, "BAR", _VENUE["venue_address"])],
    )


@given("the geo fallback offers a matching candidate not yet in the catalog")
def step_matching_candidate_new(context):
    context.expected_match_reason = "exact"
    _program_filter(
        context,
        [_candidate(_MATCH_ID, _VENUE["venue_name"], _VENUE["venue_address"])],
    )


# ---------------------------------------------------------------------------
# Givens — undo / re-add scenarios
# ---------------------------------------------------------------------------


@given("a venue was newly linked via the geo fallback")
def step_venue_newly_linked(context):
    _drive_link(context)


@given("a venue was newly linked via the geo fallback and already undone")
def step_venue_linked_and_undone(context):
    _drive_link(context)
    resp = _undo(context, context.linked_venue_id)
    assert resp.status_code == 200, (resp.status_code, resp.text)
    context.counter_after_first_undo = _counter(context)


@given("a venue that has been in the catalog for more than a day")
def step_old_venue(context):
    venue_id = "ven_geo_old_001"
    context.rds_store.upsert_venue(
        Venue(
            processed=True,
            forecast=True,
            venue_id=venue_id,
            venue_name=_VENUE["venue_name"],
            venue_address=_VENUE["venue_address"],
            venue_lat=_VENUE["venue_lat"],
            venue_lng=_VENUE["venue_lng"],
        )
    )
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    context.rds_store.venues[venue_id]["created_at"] = old.isoformat()
    context.linked_venue_id = venue_id


@given("a venue was newly linked via the geo fallback and then undone")
def step_venue_linked_then_undone(context):
    # Submitted name (accented) differs from BestTime's normalized inventory name
    # — the realistic case a folding matcher exists for. This is what makes the
    # re-add reachability real: the address cache is keyed on the submitted name,
    # so the undo's drop (keyed on the stored name) misses it, and the re-add must
    # still fall through to BestTime and reactivate rather than short-circuit to
    # the deprecated row.
    context.add_request_override = {"venue_name": "Laça Burguer, Boa Viagem!"}
    _drive_link(context)
    resp = _undo(context, context.linked_venue_id)
    assert resp.status_code == 200, (resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# Whens
# ---------------------------------------------------------------------------


@when("the operator undoes the geo link")
def step_operator_undoes(context):
    context.response = _undo(context, context.linked_venue_id)


@when("the operator undoes the geo link again")
def step_operator_undoes_again(context):
    context.response = _undo(context, context.linked_venue_id)


@when("the same venue is added again and BestTime confirms it")
def step_readd_confirmed(context):
    # BestTime accepts on the re-add; the success body carries the SAME venue_id
    # so the reactivation targets the deprecated row.
    context.besttime_http_body = {
        "status": "OK",
        "venue_info": {
            "venue_id": context.linked_venue_id,
            "venue_name": _VENUE["venue_name"],
            "venue_address": _VENUE["venue_address"],
            "venue_lat": _VENUE["venue_lat"],
            "venue_lon": _VENUE["venue_lng"],
        },
        "analysis": [],
    }
    context.besttime_http_status = 200
    context.besttime_filter_body = None
    _post_add(context)


# ---------------------------------------------------------------------------
# Thens — matcher / body
# ---------------------------------------------------------------------------


@then("the linked venue is the one whose address overlaps the request")
def step_linked_is_overlap(context):
    assert context.response.status_code == 200, context.response.text
    body = context.response.json()
    assert body.get("venue_id") == context.expected_link_id, (
        f"expected {context.expected_link_id}, got {body.get('venue_id')}: "
        f"{context.response.text[:300]}"
    )


@then("the add fails telling the operator no matching venue was found nearby")
def step_add_fails_no_match(context):
    assert context.response.status_code == 502, (
        f"expected 502, got {context.response.status_code} "
        f"{context.response.text[:300]}"
    )
    detail = (context.response.json().get("detail") or "").lower()
    assert "no matching" in detail, context.response.text


@then("the geo fallback outcome reports the venue as newly linked")
def step_reports_newly_linked(context):
    assert context.response.status_code == 200, context.response.text
    body = context.response.json()
    assert body.get("newly_linked") is True, (
        f"expected newly_linked True: {context.response.text[:300]}"
    )


@then("the outcome reports which matching rule linked it")
def step_reports_match_reason(context):
    body = context.response.json()
    reason = body.get("match_reason")
    assert reason in ("exact", "containment"), (
        f"unexpected match_reason {reason!r}: {context.response.text[:300]}"
    )
    expected = getattr(context, "expected_match_reason", None)
    if expected is not None:
        assert reason == expected, f"expected {expected}, got {reason}"


# ---------------------------------------------------------------------------
# Thens — undo / re-add
# ---------------------------------------------------------------------------


@then("the venue is deprecated with the geo-link-undo source")
def step_deprecated_with_undo_source(context):
    row = context.rds_store.get_venue(context.linked_venue_id)
    assert row is not None, "venue vanished from RDS"
    assert row.get("lifecycle_status") == "deprecated", row
    assert row.get("deprecated_source") == "admin_geo_link_undo", row


@then("the monthly counter slot is returned")
def step_counter_slot_returned(context):
    assert _counter(context) == 0, (
        f"expected month counter back at 0, got {_counter(context)}"
    )


@then("the venue is no longer eligible for serving")
def step_not_eligible_for_serving(context):
    servable = context.rds_store.list_servable_venue_ids()
    assert context.linked_venue_id not in servable, (
        f"{context.linked_venue_id} still servable: {servable}"
    )


@then("the undo reports it was already undone")
def step_reports_already_undone(context):
    assert context.response.status_code == 200, context.response.text
    body = context.response.json()
    assert body.get("status") == "already_undone", context.response.text


@then("the monthly counter is not decremented a second time")
def step_counter_not_double_decremented(context):
    assert _counter(context) == context.counter_after_first_undo, (
        f"counter moved on the second undo: {_counter(context)} vs "
        f"{context.counter_after_first_undo}"
    )


@then("the undo is rejected with an explanatory error")
def step_undo_rejected(context):
    assert context.response.status_code == 409, (
        f"expected 409, got {context.response.status_code} "
        f"{context.response.text[:300]}"
    )
    detail = (context.response.json().get("detail") or "").lower()
    assert detail, "409 carried no explanatory detail"


@then("the venue is active again in the catalog")
def step_active_again(context):
    row = context.rds_store.get_venue(context.linked_venue_id)
    assert row is not None, "venue vanished from RDS"
    assert row.get("lifecycle_status") == "active", (
        f"venue not reactivated: {row}"
    )
