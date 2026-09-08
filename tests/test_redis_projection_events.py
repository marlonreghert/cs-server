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
from app.metrics import (
    EVENTS_PROJECTION_CATEGORY_TOTAL,
    EVENTS_PROJECTION_ENDS_AT_TOTAL,
    EVENTS_PROJECTION_RECURRENCE_TEXT_TOTAL,
    EVENTS_PROJECTION_TICKET_URL_TOTAL,
)
from app.models import Venue
from app.models.post_category import (
    ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY,
    load_post_category_vocabulary,
)
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


# ══════════════════════════════════════════════════════════════════════════
# plans/260907_events-non-event-and-recurrence-normalisation.md §3
#
# The same CALL-COUNT claim the ticket-url block above protects, extended to
# the three substitutions that plan adds — plus the one thing BDD cannot
# reach cheaply: which BRANCH the projector routes an occurrence to. Keying
# `ends_at` on `is_recurring` instead of on whether the occurrence was
# RE-DERIVED is the defect revision 1 of that plan carried, and asserting it
# only inside `resolve_occurrence_end` would not catch a projector that
# passes the wrong `derived` flag.
# ══════════════════════════════════════════════════════════════════════════
_CATEGORY_OUTCOMES = ("canonicalized", "unchanged", "off_vocabulary", "absent")
_RECURRENCE_OUTCOMES = ("normalized", "unchanged", "absent")
_ENDS_AT_OUTCOMES = (
    "carried", "derived", "dropped_inverted", "dropped_zero",
    "dropped_implausible", "absent",
)


def _category_counter(outcome):
    return EVENTS_PROJECTION_CATEGORY_TOTAL.labels(outcome=outcome)._value.get()


def _recurrence_counter(outcome):
    return EVENTS_PROJECTION_RECURRENCE_TEXT_TOTAL.labels(outcome=outcome)._value.get()


def _ends_at_counter(outcome):
    return EVENTS_PROJECTION_ENDS_AT_TOTAL.labels(outcome=outcome)._value.get()


def _insert_recurring(store, **fields):
    """A recurring row whose stored `starts_at` is deliberately 60 days
    stale, so its projected days can only have come from its weekday
    pattern. `last_seen_at` is set explicitly because a recurring row's
    selectability is bounded by SOURCE FRESHNESS alone."""
    stale = (_NOW - timedelta(days=60)).astimezone(RECIFE_TZ)
    row = {
        "is_recurring": True, "recurrence_text": "Toda quarta",
        "starts_at": stale.replace(
            hour=22, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc),
    }
    row.update(fields)
    event_id = _insert(store, **row)
    store.update_event(event_id, {"last_seen_at": _NOW - timedelta(days=3)})
    return event_id


class TestPerRowSubstitutionsAreCountedOncePerSourceRow:
    def test_a_six_occurrence_row_increments_each_counter_exactly_once(self, harness):
        """The defect `classify_ticket_url`'s own comment warns about,
        extended to all three: a per-occurrence bump would scale every
        counter by recurrence multiplicity."""
        _fake, redis_only, store, service = harness
        event_id = _insert_recurring(
            store, recurrence_text="Toda QUARTA", category="forró",
            ends_at=None,
        )
        with patch.object(settings, "events_projection_horizon_days", 38):
            before = (
                sum(_recurrence_counter(o) for o in _RECURRENCE_OUTCOMES),
                sum(_category_counter(o) for o in _CATEGORY_OUTCOMES),
                sum(_ends_at_counter(o) for o in _ENDS_AT_OUTCOMES),
            )
            service.project_events(now=_NOW)
            after = (
                sum(_recurrence_counter(o) for o in _RECURRENCE_OUTCOMES),
                sum(_category_counter(o) for o in _CATEGORY_OUTCOMES),
                sum(_ends_at_counter(o) for o in _ENDS_AT_OUTCOMES),
            )
        # The precondition, checked rather than assumed: a one-occurrence
        # row would satisfy "counted once" vacuously.
        assert len(_occurrences(redis_only, event_id)) == 6
        assert [a - b for a, b in zip(after, before)] == [1, 1, 1]

    def test_the_rds_row_keeps_the_verbatim_recurrence_and_category(self, harness):
        _fake, _redis_only, store, service = harness
        event_id = _insert_recurring(store, recurrence_text="Toda QUARTA", category="forró")
        service.project_events(now=_NOW)
        service.project_events(now=_NOW)
        row = store.get_event(event_id)
        assert row["recurrence_text"] == "Toda QUARTA"
        assert row["category"] == "forró"


class TestEveryCategoryOutcomeLabelIsExercised:
    """N3: `unchanged` was declared without ever being reached, in a plan
    whose whole observability posture is that a zero-filled label's presence
    is the evidence. All four are asserted here."""

    @pytest.mark.parametrize(
        "vocabulary,stored,projected,outcome",
        [
            (["Forró", "Party"], "forró", "Forró", "canonicalized"),
            (["Forró", "Party"], "Forró", "Forró", "unchanged"),
            (["Forró", "Party"], "brega", "brega", "off_vocabulary"),
            (["Forró", "Party"], None, None, "absent"),
        ],
    )
    def test_each_outcome(self, harness, vocabulary, stored, projected, outcome):
        import json

        fake, redis_only, store, service = harness
        fake.set(
            ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY, json.dumps(vocabulary)
        )
        event_id = _insert(store, category=stored)
        before = _category_counter(outcome)
        service.project_events(now=_NOW)
        assert _category_counter(outcome) - before == 1
        occs = _occurrences(redis_only, event_id)
        assert occs and occs[0].category == projected

    def test_an_unreadable_vocabulary_config_does_not_cost_the_cycle(self, harness):
        """The loader already falls back to the shipped defaults, so the
        projector must not turn a config outage into a lost cycle."""
        _fake, redis_only, store, service = harness
        original_get = service.redis_only_dao.client.get

        def _failing_get(key, *args, **kwargs):
            if key == ADMIN_CONFIG_POST_CATEGORY_VOCABULARY_KEY:
                raise RuntimeError("admin config read failed")
            return original_get(key, *args, **kwargs)

        service.redis_only_dao.client.get = _failing_get
        event_id = _insert(store, category="forró")
        summary = service.project_events(now=_NOW)
        assert summary["errors"] == 0
        occs = _occurrences(redis_only, event_id)
        # `forró` IS in DEFAULT_CATEGORY_VOCABULARY, spelled exactly so.
        assert occs and occs[0].category == "forró"

    def test_the_vocabulary_is_read_once_per_cycle_not_once_per_row(self, harness):
        _fake, _redis_only, store, service = harness
        for _ in range(3):
            _insert(store, category="forró")
        with patch(
            "app.services.redis_projection_service.load_post_category_vocabulary",
            wraps=load_post_category_vocabulary,
        ) as spy:
            service.project_events(now=_NOW)
        assert spy.call_count == 1


class TestTheProjectorRoutesToTheRightEndsAtBranch:
    def test_a_parseable_recurrence_takes_the_derived_branch(self, harness):
        _fake, redis_only, store, service = harness
        stale = (_NOW - timedelta(days=60)).astimezone(RECIFE_TZ)
        starts_at = stale.replace(
            hour=22, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)
        event_id = _insert_recurring(
            store, recurrence_text="Toda quarta", starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
        )
        before = _ends_at_counter("derived")
        service.project_events(now=_NOW)
        assert _ends_at_counter("derived") - before == 1
        occs = _occurrences(redis_only, event_id)
        assert occs
        for occ in occs:
            assert occ.occurrence_id != occ.event_id
            assert occ.ends_at == occ.starts_at + timedelta(hours=1)

    def test_an_unparseable_recurrence_takes_the_carried_branch(self, harness):
        """`is_recurring` is TRUE here, and the stored end must still be
        served verbatim: `expand_occurrences` sent this row to the
        single-occurrence shape, so its stored end is the end of that exact
        interval. Keying on `is_recurring` would delete it."""
        _fake, redis_only, store, service = harness
        starts_at = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
        ends_at = datetime(2026, 10, 4, 7, 0, tzinfo=timezone.utc)
        event_id = _insert_recurring(
            store, recurrence_text="toda semana", starts_at=starts_at,
            ends_at=ends_at,
        )
        before = _ends_at_counter("carried")
        service.project_events(now=_NOW)
        assert _ends_at_counter("carried") - before == 1
        occs = _occurrences(redis_only, event_id)
        assert len(occs) == 1
        assert occs[0].occurrence_id == occs[0].event_id
        # 82 hours out, and NOT truncated by the derived branch's 24h bound.
        assert occs[0].ends_at == ends_at
        assert occs[0].ends_at - occs[0].starts_at > timedelta(hours=24)

    def test_an_inverted_stored_pair_never_reaches_the_payload(self, harness):
        _fake, redis_only, store, service = harness
        starts_at = _NOW + timedelta(hours=2)
        event_id = _insert(
            store, starts_at=starts_at, ends_at=starts_at - timedelta(hours=3)
        )
        before = _ends_at_counter("dropped_inverted")
        service.project_events(now=_NOW)
        assert _ends_at_counter("dropped_inverted") - before == 1
        occs = _occurrences(redis_only, event_id)
        assert occs and occs[0].ends_at is None
