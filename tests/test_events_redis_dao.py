"""Unit coverage for the events serving projection's Redis key family
(plans/260905_events-serving-projection.md, Phase 4 pytest list: "Redis key
formats, DAO serialization, and cache boundaries through mocks").

Uses real fakeredis (not a mock) so ZADD/ZRANGE/ZSCORE/ZREM semantics —
including "an emptied ZSET key stops existing" — are the genuine Redis
behaviour the production code relies on, not a hand-rolled approximation.
"""
from __future__ import annotations

import fakeredis

from app.dao.redis_venue_dao import (
    EVENT_OCCURRENCE_KEY_FORMAT,
    EVENTS_CITY_INDEX_KEY_FORMAT,
    EVENTS_KNOWN_CITIES_KEY,
    EVENTS_VENUE_INDEX_KEY_FORMAT,
    RedisVenueDAO,
)
from app.db.geo_redis_client import GeoRedisClient
from app.models.event_occurrence import EventOccurrence


def _dao() -> RedisVenueDAO:
    client = fakeredis.FakeRedis(decode_responses=True)
    return RedisVenueDAO(GeoRedisClient(client))


def _occurrence(occurrence_id="ev1", **overrides) -> EventOccurrence:
    base = dict(
        occurrence_id=occurrence_id, event_id="ev1", occurrence_date="2026-09-06",
        city_slug="recife",
    )
    base.update(overrides)
    return EventOccurrence(**base)


def test_occurrence_key_format_and_round_trip():
    dao = _dao()
    occ = _occurrence(title="Grande Encontro Techno", price_text="R$ 40")
    dao.set_event_occurrence(occ)
    raw = dao.client.get(EVENT_OCCURRENCE_KEY_FORMAT.format("ev1"))
    assert raw is not None
    fetched = dao.get_event_occurrence("ev1")
    assert fetched.title == "Grande Encontro Techno"
    assert fetched.price_text == "R$ 40"
    assert fetched.city_slug == "recife"


def test_get_missing_occurrence_returns_none():
    dao = _dao()
    assert dao.get_event_occurrence("does_not_exist") is None


def test_index_writes_the_identical_score_into_both_indexes_in_one_call():
    dao = _dao()
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=1700000000.0)
    assert dao.city_events_index_score("recife", "ev1") == 1700000000.0
    assert dao.venue_events_index_score("v1", "ev1") == 1700000000.0
    assert dao.get_city_events_index("recife") == ["ev1"]
    assert dao.get_venue_events_index("v1") == ["ev1"]


def test_reindexing_with_a_new_score_updates_both_indexes():
    dao = _dao()
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=100.0)
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=200.0)
    assert dao.city_events_index_score("recife", "ev1") == 200.0
    assert dao.venue_events_index_score("v1", "ev1") == 200.0
    assert dao.get_city_events_index("recife") == ["ev1"]  # not duplicated


def test_removing_from_the_city_index_leaves_the_venue_index_untouched():
    dao = _dao()
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=1.0)
    dao.remove_from_city_events_index("recife", "ev1")
    assert dao.get_city_events_index("recife") == []
    assert dao.get_venue_events_index("v1") == ["ev1"]


def test_removing_from_the_venue_index_leaves_the_city_index_untouched():
    dao = _dao()
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=1.0)
    dao.remove_from_venue_events_index("v1", "ev1")
    assert dao.get_venue_events_index("v1") == []
    assert dao.get_city_events_index("recife") == ["ev1"]


def test_an_emptied_index_key_does_not_persist_as_an_empty_zset():
    """Real Redis auto-deletes a ZSET once its last member is removed — the
    prune pass's `remove_from_*` calls rely on this so a city/venue that
    drops to zero occurrences is genuinely EMPTY, not a lingering key."""
    dao = _dao()
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=1.0)
    dao.remove_from_city_events_index("recife", "ev1")
    dao.remove_from_venue_events_index("v1", "ev1")
    assert dao.client.get(EVENTS_CITY_INDEX_KEY_FORMAT.format("recife")) is None or \
        dao.client.client.exists(EVENTS_CITY_INDEX_KEY_FORMAT.format("recife")) == 0
    assert dao.client.client.exists(EVENTS_VENUE_INDEX_KEY_FORMAT.format("v1")) == 0


def test_removing_from_an_index_the_member_was_never_in_is_a_safe_no_op():
    dao = _dao()
    assert dao.remove_from_city_events_index("recife", "never_there") is False
    assert dao.remove_from_venue_events_index("v1", "never_there") is False


def test_delete_occurrence_removes_only_the_payload_key_not_any_index():
    dao = _dao()
    dao.set_event_occurrence(_occurrence("ev1"))
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=1.0)
    assert dao.delete_event_occurrence("ev1") is True
    assert dao.get_event_occurrence("ev1") is None
    # The caller is responsible for de-indexing first; this method's own
    # contract is payload-only.
    assert dao.get_city_events_index("recife") == ["ev1"]
    assert dao.get_venue_events_index("v1") == ["ev1"]


def test_deleting_an_already_absent_occurrence_returns_false():
    dao = _dao()
    assert dao.delete_event_occurrence("nope") is False


def test_venue_index_and_city_index_are_independent_venues_and_cities():
    dao = _dao()
    dao.index_event_occurrence(city_slug="recife", venue_id="venue_a", occurrence_id="ev_a", score=1.0)
    dao.index_event_occurrence(city_slug="recife", venue_id="venue_b", occurrence_id="ev_b", score=2.0)
    assert dao.get_venue_events_index("venue_a") == ["ev_a"]
    assert dao.get_venue_events_index("venue_b") == ["ev_b"]
    assert sorted(dao.get_city_events_index("recife")) == ["ev_a", "ev_b"]


# ── durable known-city-slugs set (Finding 2: the city index prune must
#    survive a city being removed from admin.geo_fence_city, which keeps no
#    history of its own) ─────────────────────────────────────────────────────
def test_indexing_an_occurrence_remembers_its_city_slug():
    dao = _dao()
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=1.0)
    assert dao.list_known_city_slugs() == ["recife"]


def test_known_city_slugs_accumulate_across_distinct_cities_without_duplicates():
    dao = _dao()
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=1.0)
    dao.index_event_occurrence(city_slug="joao-pessoa", venue_id="v2", occurrence_id="ev2", score=2.0)
    # Re-asserting the SAME slug on a later cycle must not duplicate it.
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=3.0)
    assert sorted(dao.list_known_city_slugs()) == ["joao-pessoa", "recife"]


def test_known_city_slug_survives_the_slug_being_emptied_from_its_own_index():
    """The whole point of Finding 2: emptying a city's ZSET (every occurrence
    pruned) must NOT forget that the slug was ever written -- otherwise the
    very next cycle would stop visiting it, and a re-added city would silently
    re-inherit whatever the durable set had already forgotten."""
    dao = _dao()
    dao.index_event_occurrence(city_slug="recife", venue_id="v1", occurrence_id="ev1", score=1.0)
    dao.remove_from_city_events_index("recife", "ev1")
    assert dao.get_city_events_index("recife") == []
    assert dao.list_known_city_slugs() == ["recife"]


def test_no_known_city_slugs_before_anything_is_ever_indexed():
    dao = _dao()
    assert dao.list_known_city_slugs() == []
    assert dao.client.client.exists(EVENTS_KNOWN_CITIES_KEY) == 0
