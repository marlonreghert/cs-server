"""Behave steps for tests/bdd/enrichment/event-venue-candidate-cap.feature.

See plans/260913_candidate-cap-and-handle-time-merge.md Part A. Every
resolution step calls the REAL production functions under test —
`app.services.event_venue_resolution.build_location_text_attribute_fn`/
`resolve_event_venue`, `app.services.event_reconciliation.
reconcile_post_events`, and `scripts.backfill_event_venue_candidate_cap`'s
own `measure`/`apply_cap` — never a hand-rolled restatement of what those
functions do, mirroring `dedup_agentic_mitigation_discovery_steps.py`'s own
posture.

Own namespace (`context.cap_*`) throughout, matching this repo's per-feature
context-prefix convention so this file can run alongside every other steps
module in one `behave` process without clobbering shared context state.
"""
from __future__ import annotations

from datetime import datetime, timezone

import fakeredis
from behave import given, then, when  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

import scripts.backfill_event_venue_candidate_cap as backfill
from app.dao.venue_repository import VenueRepository
from app.metrics import EVENT_VENUE_NAME_MATCH_CANDIDATES_TRUNCATED_TOTAL
from app.models.venue import Venue
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from app.services.event_reconciliation import reconcile_post_events
from app.services.event_venue_resolution import (
    DEFAULT_NAME_MATCH_TOP_K,
    build_location_text_attribute_fn,
)
from tests.rds_fake import InMemoryRdsVenueStore

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
_STARTS_AT = datetime(2026, 9, 13, 22, 0, tzinfo=timezone.utc)
_PROMOTER_HANDLE = "cap_test_promoter"


def _ensure(context) -> None:
    if hasattr(context, "cap_dao"):
        return
    context.cap_dao = VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())
    context.cap_redis = fakeredis.FakeRedis(decode_responses=True)
    context.cap_client = None
    context.cap_top_k = DEFAULT_NAME_MATCH_TOP_K
    context.cap_venue_ids_by_name: dict[str, str] = {}
    context.cap_handle_index: dict[str, str] = {}
    context.cap_seq = 0
    context.cap_truncated_before = EVENT_VENUE_NAME_MATCH_CANDIDATES_TRUNCATED_TOTAL._value.get()


def _upsert_venue(context, name: str) -> str:
    if name in context.cap_venue_ids_by_name:
        return context.cap_venue_ids_by_name[name]
    context.cap_seq += 1
    venue_id = f"cap_venue_{context.cap_seq}"
    context.cap_dao.rds_store.upsert_venue(
        Venue(venue_id=venue_id, venue_name=name, venue_lat=-8.05, venue_lng=-34.88)
    )
    context.cap_venue_ids_by_name[name] = venue_id
    return venue_id


def _base_event(**overrides) -> dict:
    base = {
        "starts_at": _STARTS_AT, "ends_at": None, "is_recurring": False,
        "recurrence_text": None, "title": "Some Event", "description": None,
        "lineup": [], "ticket_url": None, "price_text": None,
        "location_text": None, "confidence": 0.9, "review_reason": None,
        "raw_extraction": {"title": "Some Event"},
    }
    base.update(overrides)
    return base


def _build_client(context) -> TestClient:
    if context.cap_client is not None:
        return context.cap_client
    app = FastAPI()
    app.include_router(admin_events_router)
    set_events_container(type("C", (), {
        "pipeline_repository": context.cap_dao, "redis_client": context.cap_redis,
    })())
    context.cap_client = TestClient(app)
    return context.cap_client


# ── Background / catalog setup ──────────────────────────────────────────
@given("the candidate top-K is {k:d}")
def step_given_top_k(context, k):
    _ensure(context)
    context.cap_top_k = k


@given('a venue catalog of {n:d} venues that all loosely match the location text "{text}"')
def step_given_similar_catalog(context, n, text):
    _ensure(context)
    # Drop the text's own LAST word before appending an index: e.g. "Espaco
    # Teste Show" -> "Espaco Teste {i}". Scores ~0.84 against the full text
    # (real overlap, definitely > 0) while staying comfortably more than
    # DEFAULT_MARGIN (0.08) below an EXACT match's 1.0 — echoing the whole
    # text back (e.g. "Espaco Teste Show {i}", score 0.95) would fail that
    # margin and wrongly land every scenario in RESOLUTION_QUEUED instead
    # of RESOLUTION_AUTO.
    stem = " ".join(text.split()[:-1]) or text
    for i in range(n):
        _upsert_venue(context, f"{stem} {i}")


@given('one of those venues is named exactly "{name}"')
def step_given_exact_venue(context, name):
    _ensure(context)
    _upsert_venue(context, name)


@given('the Instagram handle "{handle}" maps to a venue named "{name}"')
def step_given_handle_maps_to_venue(context, handle, name):
    _ensure(context)
    venue_id = _upsert_venue(context, name)
    context.cap_handle_index[handle] = venue_id


# ── triggering a resolution ──────────────────────────────────────────────
@given('an event\'s location text is "{text}" with no handle mention')
@when('an event\'s location text is "{text}" with no handle mention')
def step_when_event_location_text(context, text):
    _ensure(context)
    from app.services.event_venue_resolution import VenueLite

    catalog = [
        VenueLite(venue_id=vid, venue_name=name, lat=-8.05, lng=-34.88)
        for name, vid in context.cap_venue_ids_by_name.items()
    ]
    attribute_fn = build_location_text_attribute_fn(
        caption=None, location_tag=None, promoter_handle=_PROMOTER_HANDLE,
        venues=catalog, handle_index=context.cap_handle_index,
        venue_dao=context.cap_dao.rds_store, now=_NOW, top_k=context.cap_top_k,
    )
    context.cap_seq += 1
    shortcode = f"cap_sc_{context.cap_seq}"
    reconcile_post_events(
        venue_dao=context.cap_dao.rds_store, source_kind="venue_post",
        source_handle=_PROMOTER_HANDLE, source_shortcode=shortcode, source_permalink=None,
        prepared_events=[_base_event(location_text=text)], now=_NOW,
        attribute=attribute_fn,
    )
    rows = context.cap_dao.rds_store.list_events_by_source(_PROMOTER_HANDLE, shortcode)
    assert rows, "no event was persisted"
    context.cap_event_id = rows[0]["event_id"]


# ── Then: the cap itself ────────────────────────────────────────────────
@then("at most {k:d} ranked venue candidates are stored for that event")
def step_then_at_most_k_candidates(context, k):
    candidates = context.cap_dao.list_event_venue_link_candidates(context.cap_event_id)
    assert len(candidates) <= k, len(candidates)


@then("exactly {k:d} ranked venue candidates are stored for that event")
def step_then_exactly_k_candidates(context, k):
    candidates = context.cap_dao.list_event_venue_link_candidates(context.cap_event_id)
    assert len(candidates) == k, len(candidates)


@then("exactly {k:d} ranked venue candidate is stored for that event")
def step_then_exactly_one_candidate(context, k):
    step_then_exactly_k_candidates(context, k)


@then("the stored candidates are ordered best score first")
def step_then_candidates_ordered(context):
    candidates = context.cap_dao.list_event_venue_link_candidates(context.cap_event_id)
    scores = [c["score"] for c in candidates]
    assert scores == sorted(scores, reverse=True), scores


@then('the event auto-links to the venue named exactly "{name}"')
def step_then_auto_links_to_exact(context, name):
    row = context.cap_dao.get_event(context.cap_event_id)
    assert row["venue_id"] == context.cap_venue_ids_by_name[name], row


@then("a candidate-truncation is counted")
def step_then_truncation_counted(context):
    after = EVENT_VENUE_NAME_MATCH_CANDIDATES_TRUNCATED_TOTAL._value.get()
    assert after == context.cap_truncated_before + 1, (after, context.cap_truncated_before)


@then("no candidate-truncation is counted")
def step_then_truncation_not_counted(context):
    after = EVENT_VENUE_NAME_MATCH_CANDIDATES_TRUNCATED_TOTAL._value.get()
    assert after == context.cap_truncated_before, (after, context.cap_truncated_before)


# ── Then: the review queue ──────────────────────────────────────────────
@when("an operator requests the event review queue")
def step_when_review_queue_requested(context):
    client = _build_client(context)
    context.cap_review_response = client.get("/admin/events/review")
    assert context.cap_review_response.status_code == 200, context.cap_review_response.text


@then("that event's entry lists at most {k:d} ranked venue candidates")
def step_then_review_entry_at_most_k(context, k):
    body = context.cap_review_response.json()
    entry = next(item for item in body if item["event_id"] == context.cap_event_id)
    assert len(entry["candidates"]) <= k, len(entry["candidates"])


# ── the one-off backfill repair ──────────────────────────────────────────
def _seed_oversized_event(context, n: int, top_k: int) -> None:
    _ensure(context)
    venue_id = _upsert_venue(context, "Backfill Venue")
    context.cap_seq += 1
    event_id = f"cap_oversized_{context.cap_seq}"
    context.cap_dao.insert_event({
        "event_id": event_id, "venue_id": venue_id, "title": "Oversized Event",
        "post_type": "event", "status": "pending_review",
        "source_kind": "venue_post", "source_handle": "oversized_handle",
        "source_shortcode": event_id, "location_resolution": None, "review_reason": None,
    })
    context.cap_dao.replace_event_venue_link_candidates(event_id, [
        {"venue_id": f"cand_{rank}", "rank": rank, "score": round(1.0 - rank * 0.01, 4),
         "method": "name_match", "evidence": {}}
        for rank in range(1, n + 1)
    ])
    context.cap_event_id = event_id
    context.cap_backfill_top_k = top_k
    context.cap_event_before = context.cap_dao.get_event(event_id)


@given("an event already stores {n:d} ranked candidates, above the configured top-K of {k:d}")
def step_given_oversized_event(context, n, k):
    _seed_oversized_event(context, n, k)


@when("the candidate-cap backfill runs without apply")
def step_when_backfill_dry_run(context):
    context.cap_backfill_report = backfill.measure(context.cap_dao, top_k=context.cap_backfill_top_k)


@when("the candidate-cap backfill runs with apply")
def step_when_backfill_apply(context):
    context.cap_backfill_report = backfill.apply_cap(context.cap_dao, top_k=context.cap_backfill_top_k)


@given("the candidate-cap backfill has already applied once")
def step_given_backfill_already_applied(context):
    backfill.apply_cap(context.cap_dao, top_k=context.cap_backfill_top_k)
    context.cap_after_first_apply = list(
        context.cap_dao.list_event_venue_link_candidates(context.cap_event_id)
    )


@when("the candidate-cap backfill runs with apply a second time")
def step_when_backfill_apply_again(context):
    context.cap_backfill_report = backfill.apply_cap(context.cap_dao, top_k=context.cap_backfill_top_k)


@then("the report names that event with its current count {current:d} and its would-be count {would_be:d}")
def step_then_report_names_event(context, current, would_be):
    matching = [
        e for e in context.cap_backfill_report.oversized_events
        if e.event_id == context.cap_event_id
    ]
    assert matching, context.cap_backfill_report.oversized_events
    assert matching[0].current_count == current, matching[0]
    assert matching[0].would_be_count == would_be, matching[0]


@then("that event still stores {n:d} ranked candidates")
def step_then_event_still_stores_n(context, n):
    candidates = context.cap_dao.list_event_venue_link_candidates(context.cap_event_id)
    assert len(candidates) == n, len(candidates)


@then("that event stores at most {k:d} ranked candidates")
def step_then_event_stores_at_most_k(context, k):
    candidates = context.cap_dao.list_event_venue_link_candidates(context.cap_event_id)
    assert len(candidates) <= k, len(candidates)


@then("the event's venue_id, location_resolution, and review_reason are unchanged")
def step_then_event_columns_unchanged(context):
    after = context.cap_dao.get_event(context.cap_event_id)
    before = context.cap_event_before
    assert after["venue_id"] == before["venue_id"]
    assert after["location_resolution"] == before["location_resolution"]
    assert after["review_reason"] == before["review_reason"]


@then("no candidate row is inserted, updated, or deleted")
def step_then_no_candidate_row_changed(context):
    after = context.cap_dao.list_event_venue_link_candidates(context.cap_event_id)
    assert after == context.cap_after_first_apply, (after, context.cap_after_first_apply)
