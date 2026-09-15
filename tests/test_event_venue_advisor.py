"""Unit tests for app/services/event_venue_advisor.py —
plans/260913_dedup-agentic-mitigation-discovery.md Phase 4.

Division of labour against the BDD sibling
(`tests/bdd/enrichment/dedup-agentic-mitigation-discovery.feature`, the
advisory-hook scenarios): the scenarios prove the end-to-end, flag-gated
behaviour through the real post-reconciliation pipeline; this file pins the
service's own exact edge cases — the same house style
`tests/test_event_display_title.py` already establishes for this pipeline's
other LLM-adjacent pass.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.models.venue import Venue
from app.services.event_reconciliation import new_event_id
from app.services.event_venue_advisor import (
    ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY,
    OUTCOME_ERROR,
    OUTCOME_REJECTED,
    OUTCOME_SKIPPED_NON_MUSIC,
    OUTCOME_SUGGESTED,
    EventVenueAdvisorService,
    parse_event_venue_advisor_response,
)
from tests.rds_fake import InMemoryRdsVenueStore


class _FakeOpenAI:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls: list = []

    async def recommend_event_venue(self, *, location_text, caption, candidates):
        self.calls.append({
            "location_text": location_text, "caption": caption,
            "candidates": list(candidates),
        })
        if not self._answers:
            raise AssertionError("the fake client was called more times than programmed")
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _store():
    store = InMemoryRdsVenueStore()
    store.upsert_venue(Venue(venue_id="v1", venue_name="Venue One", venue_lat=0, venue_lng=0))
    store.upsert_venue(Venue(venue_id="v2", venue_name="Venue Two", venue_lat=0, venue_lng=0))
    return store


def _seed_queued_event(store, *, location_text="Venue Two", candidate_venue_ids=("v1", "v2")):
    """An event still sitting at RESOLUTION_QUEUED (both link columns NULL)
    with ranked candidates already written — the ladder's own shape for
    this state, per `event_venue_resolution.py`'s own docstring."""
    event_id = new_event_id()
    store.insert_event({
        "event_id": event_id, "venue_id": None, "location_resolution": None,
        "starts_at": None, "title": "Some Night", "post_type": "event",
        "status": "pending_review", "lineup": [], "location_text": location_text,
        "source_kind": "venue_post", "source_handle": "myhandle",
        "source_shortcode": f"sc_{event_id}", "raw_extraction": {},
    })
    store.replace_event_venue_link_candidates(event_id, [
        {"venue_id": vid, "rank": i + 1, "score": 0.6, "method": "name_match", "evidence": {}}
        for i, vid in enumerate(candidate_venue_ids)
    ])
    return event_id


def _seed_auto_resolved_event(store):
    event_id = new_event_id()
    store.insert_event({
        "event_id": event_id, "venue_id": "v1", "location_resolution": "auto",
        "linked_by": "handle_mention", "starts_at": None, "title": "Resolved Night",
        "post_type": "event", "status": "accepted", "lineup": [], "location_text": "@venue_one",
        "source_kind": "venue_post", "source_handle": "myhandle",
        "source_shortcode": f"sc_{event_id}", "raw_extraction": {},
    })
    return event_id


def _run(service, ids):
    return asyncio.run(service.run_for_events(ids))


@pytest.fixture
def enabled_redis():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.set(ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY, json.dumps(True))
    return redis


class TestKillSwitch:
    def test_disabled_by_default_makes_no_call_and_writes_nothing(self):
        """No redis at all -> the shipped default (False) applies, matching
        every other admin-config loader in this codebase when `redis_like`
        is None."""
        store = _store()
        event_id = _seed_queued_event(store)
        client = _FakeOpenAI()
        counts = _run(EventVenueAdvisorService(store, client, redis_client=None), [event_id])
        assert counts == {}
        assert client.calls == []
        row = store.get_event(event_id)
        assert row["venue_id"] is None
        assert row["location_resolution"] is None

    def test_explicitly_disabled_makes_no_call(self):
        import fakeredis

        store = _store()
        event_id = _seed_queued_event(store)
        redis = fakeredis.FakeRedis(decode_responses=True)
        redis.set(ADMIN_CONFIG_EVENT_VENUE_ADVISOR_ENABLED_KEY, json.dumps(False))
        client = _FakeOpenAI()
        counts = _run(EventVenueAdvisorService(store, client, redis_client=redis), [event_id])
        assert counts == {}
        assert client.calls == []


class TestTheCallIsAvoidedWhereverPossible:
    def test_never_calls_for_an_event_the_ladder_already_resolved_automatically(self, enabled_redis):
        store = _store()
        event_id = _seed_auto_resolved_event(store)
        client = _FakeOpenAI()
        counts = _run(EventVenueAdvisorService(store, client, redis_client=enabled_redis), [event_id])
        assert counts == {}
        assert client.calls == []

    def test_never_calls_for_a_queued_event_with_no_candidates_at_all(self, enabled_redis):
        store = _store()
        event_id = _seed_queued_event(store, candidate_venue_ids=())
        client = _FakeOpenAI()
        counts = _run(EventVenueAdvisorService(store, client, redis_client=enabled_redis), [event_id])
        assert counts == {}
        assert client.calls == []

    def test_never_calls_for_a_superseded_row(self, enabled_redis):
        store = _store()
        event_id = _seed_queued_event(store)
        store.update_event(event_id, {"status": "superseded"})
        client = _FakeOpenAI()
        _run(EventVenueAdvisorService(store, client, redis_client=enabled_redis), [event_id])
        assert client.calls == []

    def test_a_duplicate_event_id_is_still_one_call(self, enabled_redis):
        store = _store()
        event_id = _seed_queued_event(store)
        client = _FakeOpenAI(json.dumps({"venue_id": "v2", "evidence_quote": "Venue Two"}))
        _run(EventVenueAdvisorService(store, client, redis_client=enabled_redis), [event_id, event_id])
        assert len(client.calls) == 1


class TestTheOneCall:
    def test_a_validated_recommendation_is_written_to_the_candidate_row_only(self, enabled_redis):
        store = _store()
        event_id = _seed_queued_event(store, location_text="Venue Two")
        client = _FakeOpenAI(json.dumps({"venue_id": "v2", "evidence_quote": "Venue Two"}))
        counts = _run(EventVenueAdvisorService(store, client, redis_client=enabled_redis), [event_id])
        assert counts == {OUTCOME_SUGGESTED: 1}

        candidates = store.list_event_venue_link_candidates(event_id)
        by_venue = {c["venue_id"]: c for c in candidates}
        assert by_venue["v2"]["llm_recommendation"] == {"venue_id": "v2", "evidence_quote": "Venue Two"}
        assert by_venue["v1"]["llm_recommendation"] is None

        # NEVER the event's own venue_id/location_resolution.
        row = store.get_event(event_id)
        assert row["venue_id"] is None
        assert row["location_resolution"] is None

    def test_a_recommendation_naming_a_venue_outside_the_candidate_set_is_rejected(self, enabled_redis):
        store = _store()
        event_id = _seed_queued_event(store, location_text="Venue Two")
        client = _FakeOpenAI(json.dumps({"venue_id": "some-other-venue", "evidence_quote": "Venue Two"}))
        counts = _run(EventVenueAdvisorService(store, client, redis_client=enabled_redis), [event_id])
        assert counts == {OUTCOME_REJECTED: 1}
        candidates = store.list_event_venue_link_candidates(event_id)
        assert all(c["llm_recommendation"] is None for c in candidates)

    def test_an_invented_quote_is_rejected_and_nothing_is_written(self, enabled_redis):
        store = _store()
        event_id = _seed_queued_event(store, location_text="Venue Two")
        client = _FakeOpenAI(json.dumps({"venue_id": "v2", "evidence_quote": "a quote nobody wrote"}))
        counts = _run(EventVenueAdvisorService(store, client, redis_client=enabled_redis), [event_id])
        assert counts == {OUTCOME_REJECTED: 1}
        candidates = store.list_event_venue_link_candidates(event_id)
        assert all(c["llm_recommendation"] is None for c in candidates)

    def test_a_failed_call_is_counted_and_nothing_is_written(self, enabled_redis):
        store = _store()
        event_id = _seed_queued_event(store)
        client = _FakeOpenAI(RuntimeError("the API is down"))
        counts = _run(EventVenueAdvisorService(store, client, redis_client=enabled_redis), [event_id])
        assert counts == {OUTCOME_ERROR: 1}

    def test_no_client_at_all_is_an_error_not_a_crash(self, enabled_redis):
        store = _store()
        event_id = _seed_queued_event(store)
        counts = _run(EventVenueAdvisorService(store, None, redis_client=enabled_redis), [event_id])
        assert counts == {OUTCOME_ERROR: 1}

    def test_exactly_one_call_per_qualifying_event(self, enabled_redis):
        store = _store()
        event_id = _seed_queued_event(store)
        client = _FakeOpenAI(json.dumps({"venue_id": "v2", "evidence_quote": "Venue Two"}))
        _run(EventVenueAdvisorService(store, client, redis_client=enabled_redis), [event_id])
        assert len(client.calls) == 1
        assert client.calls[0]["caption"] is None  # never persisted; see module docstring
        call_venue_ids = {c["venue_id"] for c in client.calls[0]["candidates"]}
        assert call_venue_ids == {"v1", "v2"}


class TestParseResponse:
    def test_parses_a_well_formed_answer(self):
        assert parse_event_venue_advisor_response(
            json.dumps({"venue_id": "v1", "evidence_quote": "x"})
        ) == {"venue_id": "v1", "evidence_quote": "x"}

    def test_parses_a_markdown_fenced_answer(self):
        raw = "```json\n" + json.dumps({"venue_id": "v1", "evidence_quote": "x"}) + "\n```"
        assert parse_event_venue_advisor_response(raw) == {"venue_id": "v1", "evidence_quote": "x"}

    def test_returns_none_for_malformed_json(self):
        assert parse_event_venue_advisor_response("not json") is None

    def test_returns_none_for_a_json_list(self):
        assert parse_event_venue_advisor_response("[1, 2, 3]") is None

    def test_returns_none_for_empty_input(self):
        assert parse_event_venue_advisor_response("") is None
        assert parse_event_venue_advisor_response(None) is None
