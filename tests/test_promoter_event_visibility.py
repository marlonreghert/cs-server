"""Unit tests for plans/260813_hide-promoter-events.md.

`TestIsPromoterOnlyItem` pins the unanimity predicate itself (plan §A) —
the single riskiest piece of this feature: "any", not "all", would hide a
mixed-source item and remove real venue coverage, the exact opposite of the
plan's purpose. `TestLoadHidePromoterEvents` pins the admin-config
Redis-mirror-with-fallback shape (mirrors `tests/test_menu_item_lifecycle.py`'s
own coverage of `load_menu_expiry_days`, which this module's loader is a
byte-for-byte structural copy of). `TestListEventsFilterAndCountAgree` drives
the REAL admin API (`app.routers.admin_events_router`) over the shared
in-memory RDS fake to prove the list body and the `X-Total-Count` header
cannot disagree — the plan's own named failure mode ("a queue badge that
counts hidden rows is a stranding bug this project has already shipped
once"), and that nothing stored is ever mutated by a read.
"""
from __future__ import annotations

import json

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.promoter_event_visibility import (
    ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY,
    DEFAULT_HIDE_PROMOTER_EVENTS,
    is_promoter_only_item,
    load_hide_promoter_events,
    validate_hide_promoter_events_config,
)
from app.models.venue import Venue
from app.routers.admin_events_router import router as admin_events_router
from app.routers.admin_events_router import set_container as set_events_container
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

_VENUE_ID = "pev_venue_1"


class TestIsPromoterOnlyItem:
    def test_all_promoter_sources_is_promoter_only(self):
        sources = [{"source_kind": "promoter_post"}, {"source_kind": "promoter_post"}]
        assert is_promoter_only_item(sources) is True

    def test_all_venue_sources_is_not_promoter_only(self):
        sources = [{"source_kind": "venue_post"}, {"source_kind": "venue_post"}]
        assert is_promoter_only_item(sources) is False

    def test_mixed_sources_is_not_promoter_only(self):
        """The riskiest case: cross-post merging can attach a promoter
        source to an item that already carries a venue source. ANY
        venue-sourced evidence keeps the whole item visible."""
        sources = [{"source_kind": "promoter_post"}, {"source_kind": "venue_post"}]
        assert is_promoter_only_item(sources) is False

    def test_empty_sources_is_not_promoter_only(self):
        """No sources is no promoter evidence either — stays visible."""
        assert is_promoter_only_item([]) is False

    def test_single_promoter_source_is_promoter_only(self):
        assert is_promoter_only_item([{"source_kind": "promoter_post"}]) is True

    def test_single_venue_source_is_not_promoter_only(self):
        assert is_promoter_only_item([{"source_kind": "venue_post"}]) is False

    def test_source_with_missing_kind_is_not_promoter_only(self):
        assert is_promoter_only_item([{"source_kind": None}]) is False


class TestValidateHidePromoterEventsConfig:
    def test_accepts_true(self):
        assert validate_hide_promoter_events_config(True) is True

    def test_accepts_false(self):
        assert validate_hide_promoter_events_config(False) is False

    def test_rejects_non_bool(self):
        with pytest.raises(TypeError):
            validate_hide_promoter_events_config("true")

    def test_rejects_int(self):
        # bool is a subclass of int in Python; 1/0 must still be rejected —
        # isinstance(1, bool) is False, so this is a real, distinct check.
        with pytest.raises(TypeError):
            validate_hide_promoter_events_config(1)


class TestLoadHidePromoterEvents:
    def test_default_is_true_when_redis_client_is_none(self):
        value, fallback_reason = load_hide_promoter_events(None)
        assert value is DEFAULT_HIDE_PROMOTER_EVENTS is True
        assert fallback_reason is None

    def test_default_is_true_when_key_unset(self):
        redis_like = fakeredis.FakeRedis(decode_responses=True)
        value, fallback_reason = load_hide_promoter_events(redis_like)
        assert value is True
        assert fallback_reason is None

    def test_honours_live_override_false(self):
        redis_like = fakeredis.FakeRedis(decode_responses=True)
        redis_like.set(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY, json.dumps(False))
        value, fallback_reason = load_hide_promoter_events(redis_like)
        assert value is False
        assert fallback_reason is None

    def test_honours_live_override_true(self):
        redis_like = fakeredis.FakeRedis(decode_responses=True)
        redis_like.set(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY, json.dumps(True))
        value, fallback_reason = load_hide_promoter_events(redis_like)
        assert value is True

    def test_falls_back_to_default_on_invalid_json(self):
        redis_like = fakeredis.FakeRedis(decode_responses=True)
        redis_like.set(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY, "{not json")
        value, fallback_reason = load_hide_promoter_events(redis_like)
        assert value is True
        assert fallback_reason == "invalid_json"

    def test_falls_back_to_default_on_invalid_shape(self):
        redis_like = fakeredis.FakeRedis(decode_responses=True)
        redis_like.set(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY, json.dumps("hidden"))
        value, fallback_reason = load_hide_promoter_events(redis_like)
        assert value is True
        assert fallback_reason == "invalid_shape"


def _store_with_venue() -> InMemoryRdsVenueStore:
    store = InMemoryRdsVenueStore()
    store.upsert_venue(
        Venue(venue_id=_VENUE_ID, venue_name="Unit Test Venue", venue_lat=-8.05, venue_lng=-34.88)
    )
    return store


def _seed(store: InMemoryRdsVenueStore, *, kind: str, status="pending_review") -> str:
    event_id = new_event_id()
    store.insert_event({
        "event_id": event_id, "venue_id": _VENUE_ID, "status": status,
        "source_kind": kind, "source_handle": f"{event_id}_h",
        "source_shortcode": f"{event_id}_sc",
    })
    return event_id


def _build_client(dao, redis_client) -> TestClient:
    app = FastAPI()
    app.include_router(admin_events_router)
    set_events_container(type("C", (), {
        "pipeline_repository": dao, "redis_client": redis_client,
    })())
    return TestClient(app)


class TestListEventsFilterAndCountAgree:
    """Plan's own Test Plan bullet: "Filtered list length and the reported
    count are derived from the same predicate — assert they cannot
    disagree." Drives the real HTTP endpoint, not the predicate in
    isolation, so a future change that filters the list but forgets the
    header (or vice versa) fails here."""

    def test_list_length_equals_reported_total_when_hiding(self):
        dao = _store_with_venue()
        for _ in range(4):
            _seed(dao, kind="venue_post")
        for _ in range(6):
            _seed(dao, kind="promoter_post")
        redis_client = fakeredis.FakeRedis(decode_responses=True)
        client = _build_client(dao, redis_client)

        resp = client.get("/admin/events")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 4
        assert resp.headers["x-total-count"] == "4"
        assert resp.headers["x-total-count"] == str(len(body))

    def test_list_length_equals_reported_total_when_not_hiding(self):
        dao = _store_with_venue()
        for _ in range(4):
            _seed(dao, kind="venue_post")
        for _ in range(6):
            _seed(dao, kind="promoter_post")
        redis_client = fakeredis.FakeRedis(decode_responses=True)
        redis_client.set(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY, json.dumps(False))
        client = _build_client(dao, redis_client)

        resp = client.get("/admin/events")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 10
        assert resp.headers["x-total-count"] == "10"

    def test_zero_venue_items_reports_zero_not_a_mismatch(self):
        dao = _store_with_venue()
        for _ in range(5):
            _seed(dao, kind="promoter_post")
        client = _build_client(dao, fakeredis.FakeRedis(decode_responses=True))

        resp = client.get("/admin/events")

        assert resp.json() == []
        assert resp.headers["x-total-count"] == "0"

    def test_hidden_promoter_row_is_not_mutated(self):
        """Proof nothing is mutated: a read-time filter must never touch
        `status`, `venue_id`, or the source rows it hides."""
        dao = _store_with_venue()
        event_id = _seed(dao, kind="promoter_post", status="pending_review")
        before = dao.get_event(event_id)
        before_sources = dao.list_event_sources(event_id)
        client = _build_client(dao, fakeredis.FakeRedis(decode_responses=True))

        resp = client.get("/admin/events")

        assert resp.status_code == 200
        assert [item["event_id"] for item in resp.json()] == []  # hidden from the response
        after = dao.get_event(event_id)
        after_sources = dao.list_event_sources(event_id)
        assert after["status"] == before["status"] == "pending_review"
        assert after == before
        assert after_sources == before_sources
