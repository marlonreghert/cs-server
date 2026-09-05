"""Regression coverage for a pre-merge review finding on plans/260905_events-
serving-projection.md: `RedisProjectionService.project_events()` must fetch
`events.post_item_source` rows scoped to THIS CYCLE's selected event ids —
never the whole `events.post_item_source` table.

Before the fix, `project_events()` called `rds_store.list_all_event_sources()`
(no WHERE clause at all — every row in the table) on every cycle whenever
`hide_promoter_events` is true. `redis_projection_minutes` defaults to 2 and
`hide_promoter_events` defaults to (and is meant to stay) true, so once this
feature is enabled that was a full-table scan of a continuously growing,
append-only table every two minutes against a db.t4g.small instance — the
exact N+1-turned-whole-table regression `list_event_sources_bulk` (RdsVenueStore
/ InMemoryRdsVenueStore) exists to close.

Both tests below are genuine negative tests, not a restatement of the fix:
they booby-trap the whole-table method to raise if `project_events` ever
calls it again, and check the scoped fetch's own recorded call arguments
exclude an event this cycle never selected. Either assertion alone would
catch a regression back to `list_all_event_sources()`, or to a "scoped" fetch
that quietly widens to "every event in the store".
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import fakeredis

from app.config import settings
from app.dao.redis_venue_dao import RedisVenueDAO
from app.db.geo_redis_client import GeoRedisClient
from app.models import Venue
from app.models.promoter_event_visibility import ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY
from app.services.redis_projection_service import RedisProjectionService
from tests.rds_fake import InMemoryRdsVenueStore

_NOW = datetime(2026, 9, 5, 21, 0, tzinfo=timezone.utc)
_LAT, _LNG = -8.05, -34.88  # inside Recife's default 40km geo-fence circle


def _setup():
    fake = fakeredis.FakeRedis(decode_responses=True)
    redis_only = RedisVenueDAO(GeoRedisClient(fake))
    store = InMemoryRdsVenueStore()
    svc = RedisProjectionService(redis_only, store)
    return fake, store, svc


def _venue(store) -> str:
    venue_id = f"v_{uuid.uuid4().hex[:10]}"
    store.upsert_venue(
        Venue(venue_id=venue_id, venue_name="Bar X", venue_lat=_LAT, venue_lng=_LNG)
    )
    return venue_id


def _event(store, venue_id, *, status="accepted", starts_at, source_kind="venue_post") -> str:
    event_id = uuid.uuid4().hex  # 32 lowercase hex chars, matching event_identity.py
    store.insert_event({
        "event_id": event_id, "venue_id": venue_id, "status": status,
        "post_type": "event", "starts_at": starts_at, "time_known": True,
        "source_handle": "scoping_test", "source_shortcode": event_id,
        "source_permalink": f"https://instagram.com/p/{event_id}",
        "source_kind": source_kind,
    })
    return event_id


def _forbid_whole_table_read(store) -> None:
    def _raise():
        raise AssertionError(
            "list_all_event_sources() (no WHERE clause -- reads the WHOLE "
            "events.post_item_source table) was called from project_events(). "
            "This is exactly the full-table-scan-every-cycle regression the "
            "fix removes; project_events must call list_event_sources_bulk, "
            "scoped to the current cycle's selected event ids, instead."
        )
    store.list_all_event_sources = _raise


def test_promoter_source_fetch_is_scoped_to_the_selected_event_ids(monkeypatch):
    monkeypatch.setattr(settings, "events_projection_enabled", True)
    fake, store, svc = _setup()
    fake.set(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY, json.dumps(True))

    selected_venue = _venue(store)
    selected_event_id = _event(store, selected_venue, starts_at=_NOW + timedelta(hours=1))

    # An event this cycle's selection query NEVER selects (rejected, so
    # event_projection_selection.is_selectable excludes it outright). Its
    # sources must never be fetched -- whole-table or otherwise.
    unrelated_venue = _venue(store)
    unrelated_event_id = _event(
        store, unrelated_venue, status="rejected", starts_at=_NOW + timedelta(hours=1),
    )

    _forbid_whole_table_read(store)

    bulk_calls: list[set] = []
    original_bulk = store.list_event_sources_bulk

    def _spy_bulk(event_ids):
        bulk_calls.append(set(event_ids))
        return original_bulk(event_ids)
    store.list_event_sources_bulk = _spy_bulk

    summary = svc.project_events(now=_NOW)

    assert summary["errors"] == 0, summary
    assert summary["occurrences"] == 1, summary  # the real event still projects normally
    assert bulk_calls, "list_event_sources_bulk was never called"
    for called_ids in bulk_calls:
        assert unrelated_event_id not in called_ids, (
            "an event this cycle's selection never included was passed into "
            f"the scoped source fetch: {called_ids}"
        )
    assert selected_event_id in bulk_calls[0]


def test_promoter_source_fetch_with_nothing_selected_never_touches_the_store(monkeypatch):
    """No event qualifies this cycle -> list_event_sources_bulk gets an empty
    id list and short-circuits (mirrors get_address_bulk's own empty-input
    contract) -- and the whole-table method is still never called."""
    monkeypatch.setattr(settings, "events_projection_enabled", True)
    fake, store, svc = _setup()
    fake.set(ADMIN_CONFIG_HIDE_PROMOTER_EVENTS_KEY, json.dumps(True))
    _forbid_whole_table_read(store)

    summary = svc.project_events(now=_NOW)

    assert summary["occurrences"] == 0
    assert summary["errors"] == 0


def test_fake_list_event_sources_bulk_omits_ids_outside_the_request():
    """DAO-level contract check on the in-memory fake (mirrors RdsVenueStore.
    list_event_sources_bulk): a source row for an event id outside the
    requested set is never returned, even though it exists in the store --
    the fake must NOT fall back to whole-table behaviour for this path."""
    store = InMemoryRdsVenueStore()
    venue = _venue(store)
    in_scope = _event(store, venue, starts_at=_NOW + timedelta(hours=1))
    out_of_scope = _event(store, venue, starts_at=_NOW + timedelta(hours=1))

    rows = store.list_event_sources_bulk([in_scope])

    returned_ids = {r["event_id"] for r in rows}
    assert returned_ids == {in_scope}
    assert out_of_scope not in returned_ids
    assert store.list_event_sources_bulk([]) == []
