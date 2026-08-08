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
    DEFAULT_MAX_COMPLETION_TOKENS,
    EXTRACTION_PROMPT,
    EventExtractionParseError,
    MULTI_EVENT_BASE_COMPLETION_TOKENS,
    MULTI_EVENT_EXTRACTION_PROMPT,
    MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS,
    OpenAIEventExtractionClient,
    compute_multi_event_max_completion_tokens,
    parse_extraction_response,
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
        events, malformed, malformed_attractions = parse_multi_event_extraction_response(raw)
        assert [e["title"] for e in events] == ["O Homem do Fraque Verde", "Adilson Ramos", "Khrystal"]
        assert malformed == 0
        assert malformed_attractions == 0

    def test_a_single_event_post_yields_a_list_of_one(self):
        """The plan's own invariant: a single-event post must behave exactly
        like today, just wrapped in a list of one."""
        raw = json.dumps({"events": [{"title": "Festa", "confidence": 0.9}]})
        events, malformed, malformed_attractions = parse_multi_event_extraction_response(raw)
        assert len(events) == 1
        assert events[0]["title"] == "Festa"
        assert malformed == 0
        assert malformed_attractions == 0

    def test_a_non_dict_item_is_malformed_and_skipped_not_fatal_to_its_siblings(self):
        raw = json.dumps({"events": [
            {"title": "Valid A", "confidence": 0.9},
            "not-an-object",
            {"title": "Valid B", "confidence": 0.9},
        ]})
        events, malformed, malformed_attractions = parse_multi_event_extraction_response(raw)
        assert [e["title"] for e in events] == ["Valid A", "Valid B"]
        assert malformed == 1
        assert malformed_attractions == 0

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
        events, malformed, malformed_attractions = parse_multi_event_extraction_response(
            raw, max_events=2,
        )
        assert len(events) == 2
        assert malformed == 0
        assert malformed_attractions == 0


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

    @pytest.mark.parametrize("event_count", [1, 10, 20])
    def test_budget_arithmetic_at_1_10_and_20_events(self, event_count):
        """plans/260808_event-ticket-info-and-attractions.md §E: pins the
        formula at the exact event counts the plan calls out (a one-party
        flyer, a mid-size roundup, the configured ceiling), so a future edit
        to either constant is caught here even if it keeps the formula's
        SHAPE intact."""
        expected = (
            MULTI_EVENT_BASE_COMPLETION_TOKENS
            + MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS * event_count
        )
        assert compute_multi_event_max_completion_tokens(event_count) == expected

    def test_all_three_token_budgets_moved_up_for_the_richer_attractions_schema(self):
        """plans/260808_event-ticket-info-and-attractions.md §E: all three
        budgets must move TOGETHER — the flat single-event cap
        (DEFAULT_MAX_COMPLETION_TOKENS) is the one most likely to be
        forgotten, since turning each performer into a four-field object
        does not visibly change that constant's own call site."""
        assert DEFAULT_MAX_COMPLETION_TOKENS > 4096
        assert MULTI_EVENT_BASE_COMPLETION_TOKENS > 1536
        assert MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS > 300


# ── attractions / ticket_info (plans/260808_event-ticket-info-and-
# attractions.md) ─────────────────────────────────────────────────────────────
class TestAttractionNormalizer:
    """The per-attraction normaliser embedded in _parse_event_fields, driven
    through the public parse_extraction_response (single-event shape) — one
    definition, exercised the same way TestParseExtractionResponse in
    tests/test_event_extraction_service.py already drives the rest of that
    function."""

    def test_well_formed_attraction_survives_round_trip(self):
        raw = json.dumps({"attractions": [
            {"name": "DJ Ramon", "type": "dj", "stage": "Pista NY", "styles": ["Pop"]},
        ]})
        parsed = parse_extraction_response(raw)
        assert parsed["attractions"] == [
            {"name": "DJ Ramon", "type": "dj", "stage": "Pista NY", "styles": ["Pop"]},
        ]

    def test_missing_optional_stage_normalizes_to_none(self):
        raw = json.dumps({"attractions": [{"name": "DJ Ramon", "type": "dj", "styles": []}]})
        parsed = parse_extraction_response(raw)
        assert parsed["attractions"][0]["stage"] is None

    def test_blank_stage_normalizes_to_none(self):
        raw = json.dumps({"attractions": [
            {"name": "DJ Ramon", "type": "dj", "stage": "  ", "styles": []},
        ]})
        parsed = parse_extraction_response(raw)
        assert parsed["attractions"][0]["stage"] is None

    def test_unknown_type_defaults_to_other_without_dropping_the_entry(self):
        raw = json.dumps({"attractions": [
            {"name": "Some Act", "type": "headliner", "styles": []},
        ]})
        parsed = parse_extraction_response(raw)
        assert parsed["attractions"][0]["type"] == "other"

    def test_missing_type_defaults_to_other(self):
        raw = json.dumps({"attractions": [{"name": "Some Act", "styles": []}]})
        parsed = parse_extraction_response(raw)
        assert parsed["attractions"][0]["type"] == "other"

    def test_non_list_styles_normalizes_to_empty_list(self):
        raw = json.dumps({"attractions": [
            {"name": "DJ Ramon", "type": "dj", "styles": "Pop"},
        ]})
        parsed = parse_extraction_response(raw)
        assert parsed["attractions"][0]["styles"] == []

    def test_an_unlisted_style_is_dropped_not_stored(self):
        raw = json.dumps({"attractions": [
            {"name": "DJ Ramon", "type": "dj", "styles": ["Pop", "Not A Real Style"]},
        ]})
        parsed = parse_extraction_response(raw)
        assert parsed["attractions"][0]["styles"] == ["Pop"]

    def test_an_entry_that_is_not_an_object_is_malformed_and_skipped(self):
        raw = json.dumps({"attractions": ["just a string", {"name": "DJ Good", "styles": []}]})
        parsed = parse_extraction_response(raw)
        assert [a["name"] for a in parsed["attractions"]] == ["DJ Good"]

    def test_an_entry_with_no_usable_name_is_malformed_and_skipped(self):
        raw = json.dumps({"attractions": [
            {"type": "dj", "styles": []}, {"name": "DJ Good", "styles": []},
        ]})
        parsed = parse_extraction_response(raw)
        assert [a["name"] for a in parsed["attractions"]] == ["DJ Good"]

    def test_no_attractions_list_at_all_is_read_as_none_stated_not_malformed(self):
        parsed = parse_extraction_response(json.dumps({"title": "Festa"}))
        assert parsed["attractions"] == []


class TestLineupDerivedFromAttractions:
    def test_lineup_matches_attraction_names_in_order(self):
        raw = json.dumps({"attractions": [
            {"name": "DJ A", "styles": []}, {"name": "DJ B", "styles": []},
            {"name": "DJ C", "styles": []},
        ]})
        parsed = parse_extraction_response(raw)
        assert parsed["lineup"] == ["DJ A", "DJ B", "DJ C"]

    def test_lineup_excludes_a_malformed_attractions_name(self):
        raw = json.dumps({"attractions": [
            {"name": "DJ A", "styles": []}, {"type": "dj", "styles": []},
        ]})
        parsed = parse_extraction_response(raw)
        assert parsed["lineup"] == ["DJ A"]

    def test_a_response_with_no_attractions_list_falls_back_to_a_raw_lineup_field(self):
        """Backward compatibility for the pre-existing response shape (the
        model's own "lineup" field, asked for before this feature): the
        derivation only kicks in once a real "attractions" list is present,
        so every fixture built for the OLD shape keeps working unchanged."""
        raw = json.dumps({"lineup": ["DJ X", "DJ Y"]})
        parsed = parse_extraction_response(raw)
        assert parsed["lineup"] == ["DJ X", "DJ Y"]
        assert parsed["attractions"] == []


class TestMalformedAttractionsCountedPerMultiEventResponse:
    def test_malformed_attractions_are_summed_across_every_event_in_the_response(self):
        raw = json.dumps({"events": [
            {
                "title": "Event A",
                "attractions": [{"name": "DJ Good", "styles": []}, {"type": "dj"}],
            },
            {
                "title": "Event B",
                "attractions": [{"styles": []}, {"styles": []}],
            },
        ]})
        events, malformed, malformed_attractions = parse_multi_event_extraction_response(raw)
        assert malformed == 0
        assert malformed_attractions == 3
        assert [e["title"] for e in events] == ["Event A", "Event B"]


class TestTicketInfoField:
    def test_ticket_info_copied_verbatim(self):
        raw = json.dumps({"ticket_info": "\U0001f3ab TICKETS"})
        parsed = parse_extraction_response(raw)
        assert parsed["ticket_info"] == "\U0001f3ab TICKETS"

    def test_ticket_info_and_ticket_url_coexist(self):
        raw = json.dumps({"ticket_info": "lote até 23h", "ticket_url": "https://x"})
        parsed = parse_extraction_response(raw)
        assert parsed["ticket_info"] == "lote até 23h"
        assert parsed["ticket_url"] == "https://x"

    def test_missing_ticket_info_is_null(self):
        parsed = parse_extraction_response(json.dumps({}))
        assert parsed["ticket_info"] is None


class TestBothPromptsExtendedForAttractionsAndTicketInfo:
    """plans/260808_event-ticket-info-and-attractions.md §E: extending only
    the multi-event prompt is the likely half-fix — asserted directly on
    BOTH prompt strings, since a bug here is invisible to any test that only
    exercises the multi-event path (both real runtime callers use it)."""

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_mentions_attractions_and_ticket_info(self, prompt):
        assert "attractions" in prompt
        assert "ticket_info" in prompt

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_states_the_musical_styles_vocabulary(self, prompt):
        from app.models.taxonomy import TAXONOMY

        for style in TAXONOMY["musica"]:
            assert style in prompt


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
