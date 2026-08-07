"""Behave steps for tests/bdd/enrichment/review-queue-completeness.feature.

See plans/260807_review-queue-completeness-and-venue-names.md. Reuses the
shared harness from instagram_promoter_events_steps.py (REAL VenueRepository
over the in-memory RDS fake, the REAL admin_events_router mounted on a
TestClient) rather than re-building it — the same cross-file reuse pattern
classify_bytes_and_durable_covers_steps.py already uses. Deliberately does
NOT redefine `@when("the review queue is requested")`: that step is already
registered by instagram_promoter_events_steps.py, and behave raises
AmbiguousStep on a second identical registration. Every `Given` step here
calls `ipe._reset_context` directly (there is no Background in this feature
file), matching what instagram-promoter-events.feature's own Background does
before each of ITS scenarios.
"""
from __future__ import annotations

from datetime import datetime, timezone

from behave import given, then, when  # type: ignore[import-untyped]

import tests.bdd.steps.instagram_promoter_events_steps as ipe
from app.services.event_date_resolver import REASON_MISSING_DATE
from app.services.event_extraction_service import new_event_id


def _insert_event(
    context, *, source_kind: str, status: str, location_resolution=None,
    venue_id=None, starts_at=None, first_seen_at=None, review_reason=None,
) -> str:
    """Insert one events.event row directly — the review queue's SQL is the
    behavior under test here, not extraction, so scenarios seed rows exactly
    like instagram_promoter_events_steps.py's own queue scenario does
    (line ~549) rather than driving a full crawl."""
    ipe._ensure_context(context)
    context.rq_counter = getattr(context, "rq_counter", 0) + 1
    n = context.rq_counter
    fields = {
        "event_id": new_event_id(),
        "venue_id": venue_id,
        "source_kind": source_kind,
        "source_handle": f"rq_handle_{n}",
        "source_shortcode": f"rq_post_{n}",
        "status": status,
        "location_resolution": location_resolution,
        "review_reason": review_reason,
        "starts_at": starts_at,
    }
    if first_seen_at is not None:
        fields["first_seen_at"] = first_seen_at
    row = context.pe_dao.insert_event(fields)
    return row["event_id"]


# ── Given ─────────────────────────────────────────────────────────────────
@given('a venue-post event with the status "{status}"')
def step_given_venue_post_event_with_status(context, status):
    ipe._reset_context(context)
    context.rq_event_id = _insert_event(context, source_kind="venue_post", status=status)
    ipe._build_admin_events_app(context)


@given("a venue-post event with no start time queued for review")
def step_given_venue_post_event_no_start_time(context):
    ipe._reset_context(context)
    context.rq_event_id = _insert_event(
        context, source_kind="venue_post", status="pending_review",
        starts_at=None, review_reason=REASON_MISSING_DATE,
    )
    ipe._build_admin_events_app(context)


@given("a promoter event with no location decision")
def step_given_promoter_event_no_location_decision(context):
    ipe._reset_context(context)
    context.rq_event_id = _insert_event(
        context, source_kind="promoter_post", status="pending_review",
        location_resolution=None,
    )
    ipe._build_admin_events_app(context)


@given('a promoter event with the status "{status}" and no location decision')
def step_given_promoter_event_with_status_no_location_decision(context, status):
    ipe._reset_context(context)
    context.rq_event_id = _insert_event(
        context, source_kind="promoter_post", status=status, location_resolution=None,
    )
    ipe._build_admin_events_app(context)


@given("a confirmed venue-post event whose venue is settled")
def step_given_confirmed_venue_post_event_settled(context):
    ipe._reset_context(context)
    venue_id = ipe._create_venue(context, "Settled Venue")
    context.rq_event_id = _insert_event(
        context, source_kind="venue_post", status="confirmed", venue_id=venue_id,
        location_resolution=None,
    )
    ipe._build_admin_events_app(context)


@given("a rejected event and a superseded event")
def step_given_rejected_and_superseded_events(context):
    ipe._reset_context(context)
    # Both are promoter events with location_resolution still NULL (never
    # linked before the operator/reconciliation closed them out) — the
    # sharpest version of this case, since a naive `source_kind='promoter_post'
    # AND location_resolution IS NULL` clause would wrongly resurrect them.
    context.rq_rejected_event_id = _insert_event(
        context, source_kind="promoter_post", status="rejected", location_resolution=None,
    )
    context.rq_superseded_event_id = _insert_event(
        context, source_kind="promoter_post", status="superseded", location_resolution=None,
    )
    ipe._build_admin_events_app(context)


@given("a promoter event with three candidate venues")
def step_given_promoter_event_three_candidate_venues(context):
    ipe._reset_context(context)
    vids = [ipe._create_venue(context, f"RQ Candidate {i}") for i in range(1, 4)]
    event_id = _insert_event(
        context, source_kind="promoter_post", status="pending_review",
        location_resolution=None,
    )
    candidates = [
        {"venue_id": vids[0], "rank": 1, "score": 0.9, "method": "name_match", "evidence": {}},
        {"venue_id": vids[1], "rank": 2, "score": 0.8, "method": "name_match", "evidence": {}},
        {"venue_id": vids[2], "rank": 3, "score": 0.7, "method": "name_match", "evidence": {}},
    ]
    context.pe_dao.replace_event_venue_link_candidates(event_id, candidates)
    context.rq_event_id = event_id
    ipe._build_admin_events_app(context)


@given("a venue-post event and a promoter event awaiting review with different ages")
def step_given_two_events_different_ages(context):
    ipe._reset_context(context)
    older = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    newer = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    context.rq_older_event_id = _insert_event(
        context, source_kind="venue_post", status="pending_review", first_seen_at=older,
    )
    context.rq_newer_event_id = _insert_event(
        context, source_kind="promoter_post", status="pending_review",
        location_resolution=None, first_seen_at=newer,
    )
    ipe._build_admin_events_app(context)


@given('a venue-post event linked to a venue named "{name}"')
def step_given_venue_post_event_linked_to_named_venue(context, name):
    ipe._reset_context(context)
    venue_id = ipe._create_venue(context, name)
    context.rq_event_id = _insert_event(
        context, source_kind="venue_post", status="pending_review", venue_id=venue_id,
    )
    ipe._build_admin_events_app(context)


@given("a promoter event with no venue")
def step_given_promoter_event_no_venue(context):
    ipe._reset_context(context)
    context.rq_event_id = _insert_event(
        context, source_kind="promoter_post", status="pending_review",
        location_resolution=None, venue_id=None,
    )
    ipe._build_admin_events_app(context)


@given("an event whose venue id matches no venue")
def step_given_event_with_dangling_venue_id(context):
    ipe._reset_context(context)
    context.rq_event_id = _insert_event(
        context, source_kind="venue_post", status="pending_review",
        venue_id="ven_does_not_exist_12345",
    )
    ipe._build_admin_events_app(context)


# ── When ──────────────────────────────────────────────────────────────────
# "the review queue is requested" is intentionally NOT redefined here — see
# module docstring. It sets context.pe_review_response via context.pe_client.


@when("the events are listed")
def step_when_the_events_are_listed(context):
    context.rq_list_response = context.pe_client.get("/admin/events")


@when("that event is requested on its own")
def step_when_that_event_is_requested_on_its_own(context):
    context.rq_detail_response = context.pe_client.get(f"/admin/events/{context.rq_event_id}")


# ── Then ──────────────────────────────────────────────────────────────────
def _queue_ids(context) -> list[str]:
    assert context.pe_review_response.status_code == 200, context.pe_review_response.text
    return [item["event_id"] for item in context.pe_review_response.json()]


def _queue_item(context, event_id: str) -> dict:
    body = context.pe_review_response.json()
    item = next((i for i in body if i["event_id"] == event_id), None)
    assert item is not None, f"event {event_id!r} not found in queue: {body}"
    return item


def _last_listing_response(context):
    """Scenario 13 lists via `/admin/events`, not `/admin/events/review`, so
    prefer whichever endpoint this scenario actually called."""
    response = getattr(context, "pe_review_response", None)
    if response is not None:
        return response
    return context.rq_list_response


@then("that event is in the queue")
def step_then_that_event_is_in_the_queue(context):
    ids = _queue_ids(context)
    assert context.rq_event_id in ids, ids


@then("it carries the reason it is awaiting review")
def step_then_it_carries_the_reason_it_is_awaiting_review(context):
    item = _queue_item(context, context.rq_event_id)
    assert item["review_reason"], item


@then("that event is not in the queue")
def step_then_that_event_is_not_in_the_queue(context):
    ids = _queue_ids(context)
    assert context.rq_event_id not in ids, ids


@then("neither event is in the queue")
def step_then_neither_event_is_in_the_queue(context):
    ids = _queue_ids(context)
    assert context.rq_rejected_event_id not in ids, ids
    assert context.rq_superseded_event_id not in ids, ids


@then("that event carries its three candidates in rank order")
def step_then_event_carries_three_candidates_in_rank_order(context):
    item = _queue_item(context, context.rq_event_id)
    ranks = [c["rank"] for c in item["candidates"]]
    assert ranks == [1, 2, 3], ranks


@then("that event carries an empty candidate list")
def step_then_event_carries_empty_candidate_list(context):
    item = _queue_item(context, context.rq_event_id)
    assert item["candidates"] == [], item["candidates"]


@then("the request does not fail")
def step_then_the_request_does_not_fail(context):
    assert context.pe_review_response.status_code == 200, context.pe_review_response.text


@then("the older event is listed first")
def step_then_the_older_event_is_listed_first(context):
    ids = _queue_ids(context)
    assert context.rq_older_event_id in ids, ids
    assert context.rq_newer_event_id in ids, ids
    assert ids.index(context.rq_older_event_id) < ids.index(context.rq_newer_event_id), ids


@then('that event carries the venue name "{name}"')
def step_then_event_carries_venue_name(context, name):
    response = _last_listing_response(context)
    assert response.status_code == 200, response.text
    body = response.json()
    item = next((i for i in body if i["event_id"] == context.rq_event_id), None)
    assert item is not None, f"event {context.rq_event_id!r} not found: {body}"
    assert item.get("venue_name") == name, item


@then("that event carries no venue name")
def step_then_event_carries_no_venue_name(context):
    item = _queue_item(context, context.rq_event_id)
    # Assert the key is genuinely PRESENT-and-null, not merely absent — a
    # response shape that never grew `venue_name` at all must not read as a
    # pass here (that is exactly what defect 2 looks like pre-fix).
    assert "venue_name" in item, item
    assert item["venue_name"] is None, item


@then("that event is still returned")
def step_then_event_is_still_returned(context):
    ids = _queue_ids(context)
    assert context.rq_event_id in ids, ids


@then('it carries the venue name "{name}"')
def step_then_it_carries_the_venue_name(context, name):
    assert context.rq_detail_response.status_code == 200, context.rq_detail_response.text
    body = context.rq_detail_response.json()
    assert body.get("venue_name") == name, body
