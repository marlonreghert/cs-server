"""Unit tests for app/services/event_extraction_service.py and
app/api/openai_event_extraction_client.py.

Covers: the pre-filter cost gate (post_qualifies, on call count), idempotent
upsert on re-extraction, the confirmed-preserve rule, model-failure handling,
and response parsing (valid/truncated/extra-field/missing-field). BDD
(tests/bdd/enrichment/instagram-event-extraction.feature) covers the
end-to-end scenarios; this file protects the lower-level rules that back them.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.api.openai_event_extraction_client import (
    EventExtractionParseError,
    parse_extraction_response,
)
from app.dao.venue_repository import VenueRepository
from app.models.instagram import VenueInstagram
from app.models.venue import Venue
from app.services.event_extraction_service import (
    ArchivedPost,
    EventExtractionService,
    OUTCOME_EXTRACTED,
    OUTCOME_EXTRACTION_FAILED,
    OUTCOME_LOW_CONFIDENCE,
    OUTCOME_NOT_EVENT_LIKE,
    post_qualifies,
)
from tests.rds_fake import InMemoryRdsVenueStore


def _run(coro):
    return asyncio.run(coro)


# ── the cost gate: post_qualifies ────────────────────────────────────────────
def _post(shortcode="abc", **overrides):
    base = dict(
        shortcode=shortcode, permalink=f"https://instagram.com/p/{shortcode}",
        caption="uma foto qualquer", timestamp=None,
        flyer_photo_key=None, flyer_confidence=None, any_photo_key="k1",
    )
    base.update(overrides)
    return ArchivedPost(**base)


class TestPostQualifies:
    def test_flyer_above_floor_qualifies(self):
        post = _post(flyer_photo_key="k1", flyer_confidence=0.9)
        ok, reason = post_qualifies(post, flyer_confidence_floor=0.6)
        assert ok is True
        assert reason == "flyer"

    def test_caption_marker_qualifies_without_a_flyer(self):
        post = _post(caption="Ingressos abertos! Sábado 22h")
        ok, reason = post_qualifies(post, flyer_confidence_floor=0.6)
        assert ok is True
        assert reason == "caption"

    def test_both_signals_qualify(self):
        post = _post(flyer_photo_key="k1", flyer_confidence=0.9, caption="Ingressos abertos!")
        ok, _reason = post_qualifies(post, flyer_confidence_floor=0.6)
        assert ok is True

    def test_neither_signal_does_not_qualify(self):
        post = _post(caption="Nosso prato do dia é maravilhoso")
        ok, reason = post_qualifies(post, flyer_confidence_floor=0.6)
        assert ok is False
        assert reason == OUTCOME_NOT_EVENT_LIKE

    def test_low_confidence_flyer_does_not_qualify_on_its_own(self):
        post = _post(flyer_photo_key="k1", flyer_confidence=0.4, caption="Nosso prato do dia")
        ok, reason = post_qualifies(post, flyer_confidence_floor=0.6)
        assert ok is False
        assert reason == OUTCOME_NOT_EVENT_LIKE

    def test_flyer_with_missing_confidence_does_not_qualify(self):
        post = _post(flyer_photo_key="k1", flyer_confidence=None)
        ok, _reason = post_qualifies(post, flyer_confidence_floor=0.6)
        assert ok is False


# ── response parsing ──────────────────────────────────────────────────────────
class TestParseExtractionResponse:
    def test_valid_response_parses_all_fields(self):
        raw = json.dumps({
            "title": "Festa X", "description": "desc", "date_text": "15/08",
            "time_text": "22h", "is_recurring": False, "recurrence_text": None,
            "lineup": ["DJ A", "DJ B"], "ticket_url": "https://x", "price_text": "R$30",
            "location_text": "Rua Y", "confidence": 0.87,
        })
        parsed = parse_extraction_response(raw)
        assert parsed["title"] == "Festa X"
        assert parsed["lineup"] == ["DJ A", "DJ B"]
        assert parsed["confidence"] == 0.87

    def test_markdown_fenced_response_is_stripped(self):
        raw = "```json\n" + json.dumps({"title": "X", "confidence": 0.5}) + "\n```"
        parsed = parse_extraction_response(raw)
        assert parsed["title"] == "X"

    def test_extra_unknown_fields_are_ignored(self):
        raw = json.dumps({"title": "X", "confidence": 0.5, "something_new": "ignored"})
        parsed = parse_extraction_response(raw)
        assert "something_new" not in parsed

    def test_missing_fields_default_safely_without_inventing_values(self):
        parsed = parse_extraction_response(json.dumps({}))
        assert parsed["title"] is None
        assert parsed["date_text"] is None
        assert parsed["lineup"] == []
        assert parsed["confidence"] == 0.0

    def test_truncated_json_raises_parse_error(self):
        raw = '{"title": "Festa X", "lineup": ["DJ A"'
        with pytest.raises(EventExtractionParseError):
            parse_extraction_response(raw)

    def test_non_object_json_raises_parse_error(self):
        with pytest.raises(EventExtractionParseError):
            parse_extraction_response("[1, 2, 3]")

    def test_empty_response_raises_parse_error(self):
        with pytest.raises(EventExtractionParseError):
            parse_extraction_response("")


# ── service-level fakes (deterministic, no I/O) ──────────────────────────────
class _FakePostSource:
    def __init__(self, posts_by_venue: dict[str, list[ArchivedPost]]):
        self.posts_by_venue = posts_by_venue

    async def posts_for_venue(self, venue_id, since):
        return self.posts_by_venue.get(venue_id, [])

    async def image_data_uri(self, key):
        return f"data:image/jpeg;base64,FAKE_{key}" if key else None


class _FakeOpenAIClient:
    """Programmed with one response (a JSON string) or exception per call, in
    order. `.calls` proves the cost-gate assertion: it must stay 0 for a
    non-qualifying post."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0

    async def extract(self, *, caption, image_data_uri=None):
        self.calls += 1
        if not self._responses:
            raise AssertionError("fake OpenAI client called more times than programmed")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _dao() -> VenueRepository:
    return VenueRepository(client=None, rds_store=InMemoryRdsVenueStore())


def _seed_venue(dao: VenueRepository, vid: str, handle: str) -> None:
    dao.upsert_venue(Venue(venue_id=vid, venue_name=f"V {vid}", venue_lat=-8.05, venue_lng=-34.88))
    dao.set_venue_instagram(VenueInstagram(venue_id=vid, instagram_handle=handle, status="found"))


def _extraction_json(**overrides) -> str:
    payload = {
        "title": "Festa", "description": None, "date_text": "15/08",
        "time_text": "22h", "is_recurring": False, "recurrence_text": None,
        "lineup": ["DJ A"], "ticket_url": None, "price_text": "R$30",
        "location_text": None, "confidence": 0.9,
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestZeroModelCallsForNonQualifying:
    def test_non_qualifying_post_never_calls_the_model(self):
        dao = _dao()
        _seed_venue(dao, "v1", "v1_handle")
        posts = {"v1": [_post("s1", caption="prato do dia")]}  # no flyer, no marker
        openai_client = _FakeOpenAIClient([])
        service = EventExtractionService(dao, _FakePostSource(posts), openai_client)

        result = _run(service.run({"eligibility": {"mode": "venue_ids", "venue_ids": "v1"}}))

        assert openai_client.calls == 0
        assert result["outcomes"].get(OUTCOME_NOT_EVENT_LIKE) == 1
        assert dao.get_event_by_source("v1_handle", "s1") is None


class TestIdempotentReExtraction:
    def test_two_runs_over_the_same_post_leave_one_row(self):
        dao = _dao()
        _seed_venue(dao, "v1", "v1_handle")
        post = _post(
            "s1", caption="Ingressos abertos!", flyer_photo_key="s1.jpg",
            flyer_confidence=0.9, timestamp=datetime(2026, 7, 1, 20, tzinfo=timezone.utc),
        )
        posts = {"v1": [post]}
        cfg = {"eligibility": {"mode": "venue_ids", "venue_ids": "v1"}}

        client = _FakeOpenAIClient([_extraction_json(), _extraction_json(title="Festa 2")])
        service = EventExtractionService(dao, _FakePostSource(posts), client)

        r1 = _run(service.run(cfg))
        r2 = _run(service.run(cfg))

        assert r1["outcomes"].get(OUTCOME_EXTRACTED) == 1
        assert r2["outcomes"].get(OUTCOME_EXTRACTED) == 1
        matches = [
            e for e in dao.list_events(venue_id="v1")
            if e["source_shortcode"] == "s1"
        ]
        assert len(matches) == 1, matches
        # The second run's answer (updated title) is what is now stored.
        assert matches[0]["title"] == "Festa 2"
        assert client.calls == 2


class TestConfirmedIsNeverReverted:
    def test_confirmed_title_and_date_survive_reextraction(self):
        dao = _dao()
        _seed_venue(dao, "v1", "v1_handle")
        post = _post(
            "s1", caption="Ingressos abertos!", flyer_photo_key="s1.jpg",
            flyer_confidence=0.9, timestamp=datetime(2026, 7, 1, 20, tzinfo=timezone.utc),
        )
        posts = {"v1": [post]}
        cfg = {"eligibility": {"mode": "venue_ids", "venue_ids": "v1"}}

        client = _FakeOpenAIClient([_extraction_json(title="Original title")])
        service = EventExtractionService(dao, _FakePostSource(posts), client)
        _run(service.run(cfg))

        stored = dao.get_event_by_source("v1_handle", "s1")
        # Operator confirms with a corrected title.
        dao.update_event(stored["event_id"], {"status": "confirmed", "title": "Corrected by operator"})

        client2 = _FakeOpenAIClient([_extraction_json(title="A completely different title")])
        service2 = EventExtractionService(dao, _FakePostSource(posts), client2)
        _run(service2.run(cfg))

        after = dao.get_event_by_source("v1_handle", "s1")
        assert after["status"] == "confirmed"
        assert after["title"] == "Corrected by operator"
        assert after["review_reason"] == "model_diverges_from_confirmed_record"
        assert after["raw_extraction"]["title"] == "A completely different title"


class TestExtractionFailureRecordsAndContinues:
    def test_unparseable_json_is_recorded_and_the_run_continues(self):
        dao = _dao()
        _seed_venue(dao, "v1", "v1_handle")
        posts = {"v1": [
            _post("bad", caption="Ingressos!", flyer_photo_key="bad.jpg", flyer_confidence=0.9),
            _post("good", caption="Ingressos!", flyer_photo_key="good.jpg", flyer_confidence=0.9),
        ]}
        cfg = {"eligibility": {"mode": "venue_ids", "venue_ids": "v1"}}
        client = _FakeOpenAIClient(["not json at all {{{", _extraction_json()])
        service = EventExtractionService(dao, _FakePostSource(posts), client)

        result = _run(service.run(cfg))

        assert result["outcomes"].get(OUTCOME_EXTRACTION_FAILED) == 1
        assert result["outcomes"].get(OUTCOME_EXTRACTED) == 1
        failed = dao.get_event_by_source("v1_handle", "bad")
        assert failed["status"] == "extraction_failed"
        assert failed["raw_extraction"]["raw_response"] == "not json at all {{{"
        # The run reached the second post despite the first one failing.
        assert dao.get_event_by_source("v1_handle", "good") is not None


class TestLowConfidenceQueuesForReview:
    def test_low_confidence_extraction_is_pending_review(self):
        dao = _dao()
        _seed_venue(dao, "v1", "v1_handle")
        posts = {"v1": [_post(
            "s1", caption="Ingressos!", flyer_photo_key="s1.jpg", flyer_confidence=0.9,
            timestamp=datetime(2026, 7, 1, 20, tzinfo=timezone.utc),
        )]}
        cfg = {"eligibility": {"mode": "venue_ids", "venue_ids": "v1"}}
        client = _FakeOpenAIClient([_extraction_json(confidence=0.1)])
        service = EventExtractionService(
            dao, _FakePostSource(posts), client, min_confidence=0.5,
        )

        result = _run(service.run(cfg))

        assert result["outcomes"].get(OUTCOME_LOW_CONFIDENCE) == 1
        stored = dao.get_event_by_source("v1_handle", "s1")
        assert stored["status"] == "pending_review"
        assert "low_confidence" in stored["review_reason"]


class TestDryRunSpendsNothing:
    def test_dry_run_reports_qualifying_and_writes_nothing(self):
        dao = _dao()
        _seed_venue(dao, "v1", "v1_handle")
        posts = {"v1": [
            _post("q1", caption="Ingressos abertos!"),
            _post("n1", caption="prato do dia"),
        ]}
        cfg = {"eligibility": {"mode": "venue_ids", "venue_ids": "v1"}, "dry_run": True}
        client = _FakeOpenAIClient([])
        service = EventExtractionService(dao, _FakePostSource(posts), client)

        result = _run(service.run(cfg))

        assert result["qualifying_posts"] == 1
        assert client.calls == 0
        assert dao.list_events() == []
