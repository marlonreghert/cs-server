"""Unit tests for the multi-event extraction pieces of
app/api/openai_event_extraction_client.py. See plans/260806_multi-event-posts.md.

Covers: parsing a `{"events": [...]}` response (list handling, per-item
failure isolation, the max_events sanity cap), the output-token budget
scaling with the configured per-post ceiling, and truncation detection via
the API's own `finish_reason` — the exact mechanism that decides whether a
post is persisted at all or recorded as `truncated` with nothing partial
written. BDD (tests/bdd/enrichment/multi-event-posts.feature) covers the
end-to-end scenarios; this file protects the lower-level rules that back them.
"""
from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock

import pytest

from app.api.openai_event_extraction_client import (
    EventExtractionParseError,
    MULTI_EVENT_BASE_COMPLETION_TOKENS,
    MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS,
    OpenAIEventExtractionClient,
    compute_multi_event_max_completion_tokens,
    parse_multi_event_extraction_response,
)


def _run(coro):
    return asyncio.run(coro)


def _fake_response(content: str, finish_reason: str = "stop"):
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=20)
    return types.SimpleNamespace(choices=[choice], usage=usage)


# ── parse_multi_event_extraction_response ────────────────────────────────────
class TestParseMultiEventExtractionResponse:
    def test_parses_every_event_in_the_list(self):
        raw = json.dumps({"events": [
            {"title": "O Homem do Fraque Verde", "location_text": "Cinema São Luiz", "confidence": 0.9},
            {"title": "Adilson Ramos", "location_text": "RioMar", "confidence": 0.9},
            {"title": "Khrystal", "location_text": "Terra", "confidence": 0.9},
        ]})
        events, malformed = parse_multi_event_extraction_response(raw)
        assert [e["title"] for e in events] == ["O Homem do Fraque Verde", "Adilson Ramos", "Khrystal"]
        assert malformed == 0

    def test_a_single_event_post_yields_a_list_of_one(self):
        """The plan's own invariant: a single-event post must behave exactly
        like today, just wrapped in a list of one."""
        raw = json.dumps({"events": [{"title": "Festa", "confidence": 0.9}]})
        events, malformed = parse_multi_event_extraction_response(raw)
        assert len(events) == 1
        assert events[0]["title"] == "Festa"
        assert malformed == 0

    def test_a_non_dict_item_is_malformed_and_skipped_not_fatal_to_its_siblings(self):
        raw = json.dumps({"events": [
            {"title": "Valid A", "confidence": 0.9},
            "not-an-object",
            {"title": "Valid B", "confidence": 0.9},
        ]})
        events, malformed = parse_multi_event_extraction_response(raw)
        assert [e["title"] for e in events] == ["Valid A", "Valid B"]
        assert malformed == 1

    def test_missing_events_key_raises_a_top_level_parse_error(self):
        with pytest.raises(EventExtractionParseError):
            parse_multi_event_extraction_response(json.dumps({"title": "A"}))

    def test_events_not_a_list_raises_a_top_level_parse_error(self):
        with pytest.raises(EventExtractionParseError):
            parse_multi_event_extraction_response(json.dumps({"events": {"title": "A"}}))

    def test_invalid_json_raises(self):
        with pytest.raises(EventExtractionParseError):
            parse_multi_event_extraction_response("not json at all")

    def test_empty_response_raises(self):
        with pytest.raises(EventExtractionParseError):
            parse_multi_event_extraction_response("")

    def test_max_events_caps_the_raw_list_without_counting_the_rest_as_malformed(self):
        """The sanity bound (event_extraction_max_events_per_post) drops
        entries beyond the ceiling — they are over budget, not defective, so
        they must never inflate the malformed counter."""
        raw = json.dumps({"events": [{"title": f"E{i}", "confidence": 0.9} for i in range(5)]})
        events, malformed = parse_multi_event_extraction_response(raw, max_events=2)
        assert len(events) == 2
        assert malformed == 0


# ── output budget scaling ─────────────────────────────────────────────────────
class TestBudgetScaling:
    def test_budget_increases_with_the_configured_ceiling(self):
        small = compute_multi_event_max_completion_tokens(1)
        large = compute_multi_event_max_completion_tokens(20)
        assert large > small

    def test_budget_matches_the_documented_formula(self):
        assert compute_multi_event_max_completion_tokens(5) == (
            MULTI_EVENT_BASE_COMPLETION_TOKENS + MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS * 5
        )

    def test_a_misconfigured_zero_still_gets_at_least_one_events_worth_of_headroom(self):
        assert compute_multi_event_max_completion_tokens(0) == (
            MULTI_EVENT_BASE_COMPLETION_TOKENS + MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS
        )


# ── truncation detection via finish_reason ────────────────────────────────────
class TestExtractEventsTruncationDetection:
    def test_finish_reason_length_is_reported_as_truncated(self):
        client = OpenAIEventExtractionClient(api_key="sk-test")
        client.client.chat.completions.create = AsyncMock(
            return_value=_fake_response('{"events": [{"title": "cut off', finish_reason="length")
        )
        raw_text, truncated = _run(client.extract_events(caption="x", max_events=5))
        assert truncated is True
        assert raw_text == '{"events": [{"title": "cut off'

    def test_finish_reason_stop_is_not_truncated(self):
        client = OpenAIEventExtractionClient(api_key="sk-test")
        client.client.chat.completions.create = AsyncMock(
            return_value=_fake_response('{"events": []}', finish_reason="stop")
        )
        _raw_text, truncated = _run(client.extract_events(caption="x", max_events=5))
        assert truncated is False

    def test_the_call_budget_scales_with_max_events(self):
        client = OpenAIEventExtractionClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response('{"events": []}'))
        client.client.chat.completions.create = create
        _run(client.extract_events(caption="x", max_events=20))
        _, kwargs = create.call_args
        assert kwargs["max_completion_tokens"] == compute_multi_event_max_completion_tokens(20)

    def test_never_passes_max_tokens(self):
        """gpt-5.6 rejects the legacy max_tokens param with a 400 — this
        client must always bound output via max_completion_tokens."""
        client = OpenAIEventExtractionClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response('{"events": []}'))
        client.client.chat.completions.create = create
        _run(client.extract_events(caption="x", max_events=5))
        _, kwargs = create.call_args
        assert "max_tokens" not in kwargs
        assert "max_completion_tokens" in kwargs
