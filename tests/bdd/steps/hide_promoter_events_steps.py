"""Behave steps for tests/bdd/api/hide-promoter-events.feature.

See plans/260813_hide-promoter-events.md. Self-contained per scenario (own
fresh `InMemoryRdsVenueStore` + fakeredis-backed `admin_events_router` app),
mirroring `tests/bdd/steps/menu_item_lifecycle_steps.py`'s own
`_build_menu_admin_app` — this feature only exercises the READ path over
events + sources seeded directly through the DAO, never a real extraction
pipeline, so there is no reason to share that heavier harness.

A "mixed sources" item (one promoter source + one venue source) is produced
by the EXACT sequence `app.services.event_merge` uses for a real cross-post
merge — `insert_event` a standalone second event, then
`reattach_event_sources` + `delete_event` — rather than poking
`InMemoryRdsVenueStore.event_sources` directly, so the seeded fixture is
genuinely reachable through the DAO's own contract, not a shape only this
test file could produce.
"""
from __future__ import annotations

import json

import fakeredis
from behave import given, then, when  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.promoter_event_visibility import ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY
from app.models.venue import Venue
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

_VENUE_ID = "hpe_venue_1"
_PROMOTER = "promoter_post"
_VENUE_POST = "venue_post"


def _reset(context) -> None:
    context.hpe_dao = InMemoryRdsVenueStore()
    context.hpe_dao.upsert_venue(
        Venue(venue_id=_VENUE_ID, venue_name="Hide Promoter Test Venue", venue_lat=-8.05, venue_lng=-34.88)
    )
    context.hpe_fake_redis = fakeredis.FakeRedis(decode_responses=True)
    context.hpe_last_event_id = None
    context.hpe_last_event_original_status = None


def _build_app(context) -> None:
    app = FastAPI()
    app.include_router(admin_events_router)
    set_events_container(type("C", (), {
        "pipeline_repository": context.hpe_dao,
        "redis_client": context.hpe_fake_redis,
    })())
    context.hpe_client = TestClient(app)


def _seed_source(context, *, event_id: str, kind: str, handle: str) -> None:
    context.hpe_dao.insert_event({
        "event_id": event_id, "venue_id": _VENUE_ID, "status": "pending_review",
        "source_kind": kind, "source_handle": handle,
        "source_shortcode": f"{event_id}_sc", "title": "Seeded Item",
    })


def _seed_item_with_sources(context, kinds: list[str]) -> str:
    """One event whose `event_sources` carry exactly `kinds`, one per entry.
    The first is the event's own primary source; every additional kind is
    attached via the real cross-post-merge sequence (see module docstring)."""
    primary_id = new_event_id()
    _seed_source(context, event_id=primary_id, kind=kinds[0], handle="hpe_handle_0")
    for i, kind in enumerate(kinds[1:], start=1):
        extra_id = new_event_id()
        _seed_source(context, event_id=extra_id, kind=kind, handle=f"hpe_handle_{i}")
        context.hpe_dao.reattach_event_sources(extra_id, primary_id)
        context.hpe_dao.delete_event(extra_id)
    return primary_id


def _seed_item_with_no_sources(context) -> str:
    """An event row with zero `event_sources` — no post ever attached (the
    `extraction_failed`-adjacent placeholder shape this repo already allows;
    see app.models.menu_lifecycle's own "should not occur in practice, but
    never assumed" comment for the precedent)."""
    event_id = new_event_id()
    context.hpe_dao.events[event_id] = {
        "event_id": event_id, "venue_id": _VENUE_ID, "status": "pending_review",
        "post_type": "event", "time_known": False,
    }
    return event_id


# ── Background ───────────────────────────────────────────────────────────
@given("promoter events are hidden")
def step_given_promoter_events_are_hidden(context):
    _reset(context)
    # Deliberately does NOT write the admin-config key — this proves the
    # SHIPPED DEFAULT is hidden (plan: "Default true"), not merely that an
    # explicit write can hide them.


@given("promoter events are not hidden")
def step_given_promoter_events_are_not_hidden(context):
    context.hpe_fake_redis.set(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY, json.dumps(False))


# ── item fixtures ────────────────────────────────────────────────────────
@given("an item whose only source is a promoter post")
def step_given_item_only_promoter_source(context):
    context.hpe_last_event_id = _seed_item_with_sources(context, [_PROMOTER])
    context.hpe_last_event_original_status = "pending_review"


@given("an item whose only source is a promoter post and which needs a decision")
def step_given_item_only_promoter_source_needs_decision(context):
    context.hpe_last_event_id = _seed_item_with_sources(context, [_PROMOTER])
    context.hpe_last_event_original_status = "pending_review"


@given("an item whose only source is a venue post and which needs a decision")
def step_given_item_only_venue_source_needs_decision(context):
    context.hpe_last_event_id = _seed_item_with_sources(context, [_VENUE_POST])
    context.hpe_last_event_original_status = "pending_review"


@given("an item with one promoter source and one venue source")
def step_given_item_mixed_sources(context):
    context.hpe_last_event_id = _seed_item_with_sources(context, [_PROMOTER, _VENUE_POST])
    context.hpe_last_event_original_status = "pending_review"


@given("an item with no sources")
def step_given_item_no_sources(context):
    context.hpe_last_event_id = _seed_item_with_no_sources(context)
    context.hpe_last_event_original_status = "pending_review"


@given("{venue_n:d} venue-sourced items and {promoter_n:d} promoter-sourced items")
def step_given_n_venue_and_m_promoter_items(context, venue_n, promoter_n):
    for _ in range(venue_n):
        _seed_item_with_sources(context, [_VENUE_POST])
    for _ in range(promoter_n):
        _seed_item_with_sources(context, [_PROMOTER])


# ── When ─────────────────────────────────────────────────────────────────
@when("an operator reads the events list")
def step_when_operator_reads_events_list(context):
    _build_app(context)
    context.hpe_list_response = context.hpe_client.get("/admin/events")
    assert context.hpe_list_response.status_code == 200, context.hpe_list_response.text


@when("an operator reads the review queue")
def step_when_operator_reads_review_queue(context):
    _build_app(context)
    context.hpe_queue_response = context.hpe_client.get("/admin/events/review")
    assert context.hpe_queue_response.status_code == 200, context.hpe_queue_response.text


@when("an operator reads the admin configuration")
def step_when_operator_reads_admin_configuration(context):
    _build_app(context)
    context.hpe_config_response = context.hpe_client.get("/admin/events/config")
    assert context.hpe_config_response.status_code == 200, context.hpe_config_response.text


# ── Then ─────────────────────────────────────────────────────────────────
def _list_ids(response) -> set:
    return {item["event_id"] for item in response.json()}


@then("that item is not listed")
def step_then_item_not_listed(context):
    assert context.hpe_last_event_id not in _list_ids(context.hpe_list_response)


@then("that item is listed")
def step_then_item_listed(context):
    assert context.hpe_last_event_id in _list_ids(context.hpe_list_response)


@then("that item is not queued")
def step_then_item_not_queued(context):
    assert context.hpe_last_event_id not in _list_ids(context.hpe_queue_response)


@then("that item is queued")
def step_then_item_queued(context):
    assert context.hpe_last_event_id in _list_ids(context.hpe_queue_response)


@then("{n:d} items are listed")
def step_then_n_items_listed(context, n):
    body = context.hpe_list_response.json()
    assert len(body) == n, body


@then("the reported total is {n:d}")
def step_then_reported_total_is_n(context, n):
    header = context.hpe_list_response.headers.get("x-total-count")
    assert header == str(n), header
    # Same predicate backs both — they cannot disagree (plan §B's own
    # warning: a queue badge counting hidden rows is a stranding bug this
    # project has already shipped once).
    assert int(header) == len(context.hpe_list_response.json())


@then("it reports that promoter events are hidden")
def step_then_reports_promoter_events_hidden(context):
    body = context.hpe_config_response.json()
    assert body["hide_promoter_events"] is True, body


@then("the stored item still exists with its original status")
def step_then_stored_item_unchanged_status(context):
    row = context.hpe_dao.get_event(context.hpe_last_event_id)
    assert row is not None
    assert row["status"] == context.hpe_last_event_original_status, row


@then("the stored item still carries its promoter source")
def step_then_stored_item_still_carries_promoter_source(context):
    sources = context.hpe_dao.list_event_sources(context.hpe_last_event_id)
    assert sources, sources
    assert all(s["source_kind"] == _PROMOTER for s in sources), sources
