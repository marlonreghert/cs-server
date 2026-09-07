"""Unit coverage for the events half of RedisProjectionService.project_events
that BDD deliberately cannot make cheaply.

plans/260906_events-venue-bairro-and-ticket-url.md §3 (R15) and §5. The
load-bearing claim here is a CALL-COUNT one — `classify_ticket_url` runs
once per SOURCE ROW, not once per occurrence — which needs a spy on the
production call site rather than an assertion about a stored value: the
projected VALUE is identical either way, so only the counter (and the spy)
can tell a correct implementation from one that scales by recurrence
multiplicity.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fakeredis
import pytest

from app.config import settings
from app.dao.redis_venue_dao import RedisVenueDAO
from app.db.geo_redis_client import GeoRedisClient
from app.metrics import EVENTS_PROJECTION_TICKET_URL_TOTAL
from app.models import Venue
from app.services.event_date_resolver import RECIFE_TZ
from app.services.redis_projection_service import RedisProjectionService
from tests.rds_fake import InMemoryRdsVenueStore

_NOW = datetime(2026, 9, 6, 18, 0, tzinfo=RECIFE_TZ).astimezone(timezone.utc)


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(settings, "events_projection_enabled", True)
    fake = fakeredis.FakeRedis(decode_responses=True)
    redis_only = RedisVenueDAO(GeoRedisClient(fake))
    store = InMemoryRdsVenueStore()
    store.upsert_venue(Venue(
        venue_id="evp_venue", venue_name="Downtown Beer Garden",
        venue_address="R. Test, 1 - Recife PE", venue_lat=-8.05, venue_lng=-34.88,
    ))
    service = RedisProjectionService(redis_only, store)
    return fake, redis_only, store, service


def _insert(store, **fields):
    event_id = uuid.uuid4().hex
    row = {
        "event_id": event_id, "venue_id": "evp_venue", "status": "accepted",
        "post_type": "event", "source_handle": "evp", "source_shortcode": event_id,
        "source_permalink": f"https://instagram.com/p/{event_id}",
        "starts_at": _NOW + timedelta(hours=2), "time_known": True,
    }
    row.update(fields)
    store.insert_event(row)
    return event_id


def _occurrences(redis_only, event_id):
    found = []
    for member in redis_only.get_city_events_index("recife"):
        occ = redis_only.get_event_occurrence(member)
        if occ is not None and occ.event_id == event_id:
            found.append(occ)
    return found


def _counter(outcome):
    return EVENTS_PROJECTION_TICKET_URL_TOTAL.labels(outcome=outcome)._value.get()


class TestTicketUrlIsNormalisedOncePerSourceRow:
    def test_a_recurring_row_calls_the_normaliser_exactly_once(self, harness):
        """R15: `expand_occurrences` emits one occurrence per matching day,
        so a call inside the loop would run up to 22 times per row and make
        ONE badly-extracted recurring row read as 22 rejections."""
        _fake, redis_only, store, service = harness
        stale = (_NOW - timedelta(days=60)).astimezone(RECIFE_TZ)
        event_id = _insert(
            store, is_recurring=True, recurrence_text="todo dia",
            starts_at=stale.replace(hour=22, minute=0).astimezone(timezone.utc),
            ticket_url="ingressos na portaria",
        )

        target = "app.services.redis_projection_service.classify_ticket_url"
        with patch(target, wraps=__import__(
            "app.services.event_ticket_url", fromlist=["classify_ticket_url"]
        ).classify_ticket_url) as spy:
            service.project_events(now=_NOW)

        assert spy.call_count == 1, spy.call_args_list
        occurrences = _occurrences(redis_only, event_id)
        assert len(occurrences) > 1
        assert all(o.ticket_url is None for o in occurrences)

    def test_the_counter_moves_by_one_per_row_not_per_occurrence(self, harness):
        _fake, redis_only, store, service = harness
        stale = (_NOW - timedelta(days=60)).astimezone(RECIFE_TZ)
        event_id = _insert(
            store, is_recurring=True, recurrence_text="todo dia",
            starts_at=stale.replace(hour=22, minute=0).astimezone(timezone.utc),
            ticket_url="evenyx.com",
        )

        before = _counter("scheme_added")
        service.project_events(now=_NOW)
        after = _counter("scheme_added")

        assert after - before == 1
        occurrences = _occurrences(redis_only, event_id)
        assert len(occurrences) > 1
        assert {o.ticket_url for o in occurrences} == {"https://evenyx.com"}

    def test_every_occurrence_of_one_row_carries_the_same_normalised_value(self, harness):
        _fake, redis_only, store, service = harness
        stale = (_NOW - timedelta(days=60)).astimezone(RECIFE_TZ)
        event_id = _insert(
            store, is_recurring=True, recurrence_text="toda quinta",
            starts_at=stale.replace(hour=22, minute=0).astimezone(timezone.utc),
            ticket_url="evenyx.com:8080/lote2",
        )
        service.project_events(now=_NOW)
        occurrences = _occurrences(redis_only, event_id)
        assert len(occurrences) >= 3
        assert {o.ticket_url for o in occurrences} == {"https://evenyx.com:8080/lote2"}

    def test_two_rows_bump_the_counter_twice(self, harness):
        """The bump is per row, so a second selected row must be counted —
        moving the call out of the loop must not accidentally hoist it out
        of the per-event loop as well."""
        _fake, _redis_only, store, service = harness
        _insert(store, ticket_url="evenyx.com")
        _insert(store, ticket_url="sympla.com.br/x")

        before = _counter("scheme_added")
        service.project_events(now=_NOW)
        assert _counter("scheme_added") - before == 2

    def test_the_rds_row_is_never_rewritten(self, harness):
        _fake, _redis_only, store, service = harness
        event_id = _insert(store, ticket_url="evenyx.com")
        service.project_events(now=_NOW)
        service.project_events(now=_NOW)
        assert store.get_event(event_id)["ticket_url"] == "evenyx.com"


class TestConfiguredCitiesAreRemembered:
    def test_a_configured_city_with_no_events_reaches_the_vocabulary(self, harness):
        """The gap F02 names: the only writer used to be
        `index_event_occurrence`, so a configured-but-quiet city was never
        in the set vibes_bot validates an incoming `city` against."""
        _fake, redis_only, store, service = harness
        store.set_geo_fence({"enabled": True, "cities": [
            {"slug": "recife", "name": "Recife", "lat": -8.0476, "lng": -34.8770, "radius_km": 150.0},
            {"slug": "joao-pessoa", "name": "João Pessoa", "lat": -7.1195, "lng": -34.8450, "radius_km": 150.0},
        ]})
        service.project_events(now=_NOW)
        assert sorted(redis_only.list_known_city_slugs()) == ["joao-pessoa", "recife"]

    def test_a_failing_remember_write_does_not_cost_the_cycle(self, harness):
        _fake, redis_only, store, service = harness

        def _boom(city_slug):
            raise RuntimeError("SADD failed")

        redis_only.remember_city_slug = _boom
        summary = service.project_events(now=_NOW)
        assert summary["errors"] == 0
