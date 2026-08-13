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
        events, malformed, malformed_attractions, _truncated_by_cap = parse_multi_event_extraction_response(raw)
        assert [e["title"] for e in events] == ["O Homem do Fraque Verde", "Adilson Ramos", "Khrystal"]
        assert malformed == 0
        assert malformed_attractions == 0

    def test_a_single_event_post_yields_a_list_of_one(self):
        """The plan's own invariant: a single-event post must behave exactly
        like today, just wrapped in a list of one."""
        raw = json.dumps({"events": [{"title": "Festa", "confidence": 0.9}]})
        events, malformed, malformed_attractions, _truncated_by_cap = parse_multi_event_extraction_response(raw)
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
        events, malformed, malformed_attractions, _truncated_by_cap = parse_multi_event_extraction_response(raw)
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
        events, malformed, malformed_attractions, truncated_by_cap = parse_multi_event_extraction_response(
            raw, max_events=2,
        )
        assert len(events) == 2
        assert malformed == 0
        assert malformed_attractions == 0
        assert truncated_by_cap is True

    def test_truncated_by_cap_is_false_when_the_raw_list_fits(self):
        """plans/260812_crawl-error-visibility.md §D: a post whose raw list
        is AT or under the cap is not truncated — 'the cap bit' must mean
        something was actually dropped, not merely that a cap exists."""
        raw = json.dumps({"events": [{"title": f"E{i}", "confidence": 0.9} for i in range(2)]})
        _events, _malformed, _malformed_attractions, truncated_by_cap = (
            parse_multi_event_extraction_response(raw, max_events=2)
        )
        assert truncated_by_cap is False

    def test_truncated_by_cap_is_false_when_no_cap_is_given(self):
        raw = json.dumps({"events": [{"title": f"E{i}", "confidence": 0.9} for i in range(5)]})
        _events, _malformed, _malformed_attractions, truncated_by_cap = (
            parse_multi_event_extraction_response(raw)
        )
        assert truncated_by_cap is False


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
        events, malformed, malformed_attractions, _truncated_by_cap = parse_multi_event_extraction_response(raw)
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


# ── plans/260810_post-kind-and-post-extraction-attribution.md §A ────────────
class TestBothPromptsStateKindAndItsPrecedence:
    """Extending only MULTI_EVENT_EXTRACTION_PROMPT with `kind` is the exact
    same half-fix TestBothPromptsExtendedForAttractionsAndTicketInfo already
    guards against above — asserted directly on BOTH prompt strings, since a
    bug here is invisible to any test that only exercises the multi-event
    path (both real runtime callers use it).

    The precedence rule specifically (not just the word "kind") is asserted
    verbatim in both, because that is the whole point: these categories
    overlap in real captions, and an unstated order makes classification
    vary run to run.
    """

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_names_every_kind(self, prompt):
        for kind in ("event", "promotion", "menu", "food", "other"):
            assert kind in prompt

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_states_the_precedence_order_verbatim(self, prompt):
        # Whitespace-normalized: the prompt hard-wraps this sentence across
        # several lines, but the ORDER of the words is what must never drift.
        normalized = " ".join(prompt.split())
        precedence = (
            'a happening with a date or recurring schedule -> "event"; else an '
            'offer or price advantage -> "promotion"; else a named dish or menu '
            '-> "menu"; else food or drink imagery -> "food"; else "other"'
        )
        assert precedence in normalized

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_puts_event_first_deliberately(self, prompt):
        assert 'This puts "event" FIRST on' in prompt
        assert "purpose" in prompt

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_marks_kind_required(self, prompt):
        assert "This field is REQUIRED" in prompt


# ── plans/260811_post-items-and-categories.md §C ────────────────────────────
class TestBothPromptsStateCategoryAndItsVocabulary:
    """Extending only MULTI_EVENT_EXTRACTION_PROMPT with `category` is the
    exact same half-fix TestBothPromptsStateKindAndItsPrecedence already
    guards against above — asserted directly on BOTH prompt strings, since a
    bug here is invisible to any test that only exercises the multi-event
    path (both real runtime callers use it). EXTRACTION_PROMPT/
    MULTI_EVENT_EXTRACTION_PROMPT are built from DEFAULT_CATEGORY_VOCABULARY
    at import time (app.models.post_category) — the OUTGOING network call
    rebuilds the prompt from the LIVE admin-configured vocabulary instead
    (OpenAIEventExtractionClient._category_vocabulary), covered by
    tests/bdd/enrichment/post-items-and-categories.feature's "Read the
    vocabulary from configuration" scenario, not here."""

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_mentions_category(self, prompt):
        assert "category" in prompt

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_states_the_seeded_category_vocabulary(self, prompt):
        from app.models.post_category import DEFAULT_CATEGORY_VOCABULARY

        for category in DEFAULT_CATEGORY_VOCABULARY:
            assert category in prompt

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_steers_without_confining(self, prompt):
        """The plan's own distinction: PREFER a listed word when one fits,
        answer freely otherwise — never confined to the list."""
        assert "PREFER" in prompt
        assert "answer freely" in prompt


class TestKindFieldParsing:
    """`_parse_event_fields` (via parse_extraction_response / parse_multi_
    event_extraction_response) stores the model's raw `kind` verbatim,
    lowercased/stripped — never coerced into a known value. What a
    missing/unrecognised kind is STORED AS on `post_item.post_type` is
    enforced by the CALLERS (app.models.event_kind.resolve_post_type: a
    missing/blank kind defaults to "event"; an unrecognised one is kept
    verbatim — plans/260811_post-items-and-categories.md §B), not by this
    parsing step, which is deliberately dumb: it records exactly what the
    model said."""

    def test_kind_is_parsed_and_lowercased(self):
        parsed = parse_extraction_response(json.dumps({"kind": "MENU"}))
        assert parsed["kind"] == "menu"

    def test_missing_kind_is_none(self):
        parsed = parse_extraction_response(json.dumps({}))
        assert parsed["kind"] is None

    def test_blank_kind_is_none(self):
        parsed = parse_extraction_response(json.dumps({"kind": "   "}))
        assert parsed["kind"] is None

    def test_an_unrecognised_kind_is_stored_verbatim_not_dropped(self):
        parsed = parse_extraction_response(json.dumps({"kind": "giveaway"}))
        assert parsed["kind"] == "giveaway"

    def test_kind_survives_the_multi_event_shape_too(self):
        events, _malformed, _malformed_attractions, _truncated_by_cap = parse_multi_event_extraction_response(
            json.dumps({"events": [{"kind": "promotion"}, {"kind": "event"}]}),
        )
        assert [e["kind"] for e in events] == ["promotion", "event"]


# ── plans/260811_post-items-and-categories.md §C ────────────────────────────
class TestCategoryFieldParsing:
    """`_parse_event_fields` stores the model's raw `category` trimmed but
    otherwise unchanged — matching against the admin-configured vocabulary
    and canonicalizing the stored spelling is the SERVICE layer's job
    (app.models.post_category, via event_extraction_service.py/
    promoter_crawl_service.py), the same split `kind` already uses."""

    def test_category_is_parsed_and_trimmed_but_not_canonicalized(self):
        parsed = parse_extraction_response(json.dumps({"category": "  ROCK  "}))
        assert parsed["category"] == "ROCK"

    def test_missing_category_is_none(self):
        parsed = parse_extraction_response(json.dumps({}))
        assert parsed["category"] is None

    def test_blank_category_is_none(self):
        parsed = parse_extraction_response(json.dumps({"category": "   "}))
        assert parsed["category"] is None

    def test_category_survives_the_multi_event_shape_too(self):
        events, _malformed, _malformed_attractions, _truncated_by_cap = parse_multi_event_extraction_response(
            json.dumps({"events": [{"category": "samba"}, {"category": "rock"}]}),
        )
        assert [e["category"] for e in events] == ["samba", "rock"]


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


# ── plans/260812_event-attribution-and-dates.md §C ───────────────────────────
class TestBothPromptsStateDateInterpretation:
    """Extending only one prompt with `date_interpretation` would be the
    exact same half-fix TestBothPromptsStateKindAndItsPrecedence already
    guards against for `kind` — asserted directly on BOTH prompt strings,
    since a bug here is invisible to any test that only exercises the
    multi-event path (both real runtime callers use it)."""

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_mentions_date_interpretation(self, prompt):
        assert "date_interpretation" in prompt

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_states_every_interpretation_kind(self, prompt):
        for kind in ("relative", "day_month", "day_month_year", "weekday", "weekday_day", "range"):
            assert kind in prompt

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_states_it_is_optional_and_never_a_computed_date(self, prompt):
        assert "OPTIONAL" in prompt
        assert "NEVER a computed or absolute date" in prompt


class TestDateInterpretationParsing:
    """`_normalize_date_interpretation`, driven through the public parse
    functions — mirrors TestAttractionsNormalization's own convention just
    above in this file."""

    def test_a_relative_interpretation_is_parsed(self):
        raw = json.dumps({
            "title": "Festa", "date_text": "É HOJE",
            "date_interpretation": {"kind": "relative", "relative": "hoje"},
        })
        parsed = parse_extraction_response(raw)
        assert parsed["date_interpretation"] == {"kind": "relative", "relative": "hoje"}

    def test_absent_date_interpretation_parses_to_none(self):
        raw = json.dumps({"title": "Festa", "date_text": "15/08"})
        parsed = parse_extraction_response(raw)
        assert parsed["date_interpretation"] is None

    def test_an_unrecognised_kind_parses_to_none(self):
        raw = json.dumps({
            "title": "Festa", "date_text": "algo",
            "date_interpretation": {"kind": "not_a_real_kind"},
        })
        parsed = parse_extraction_response(raw)
        assert parsed["date_interpretation"] is None

    def test_a_non_object_date_interpretation_parses_to_none(self):
        raw = json.dumps({
            "title": "Festa", "date_text": "algo", "date_interpretation": "hoje",
        })
        parsed = parse_extraction_response(raw)
        assert parsed["date_interpretation"] is None

    def test_only_the_recognised_keys_survive_an_extra_absolute_date_field(self):
        """The hard constraint the plan pins: a model-invented extra key
        that looks like a computed date is dropped at PARSE time, the first
        of two independent places this repo ignores it (event_date_resolver.
        _interpretation_to_date is the second)."""
        raw = json.dumps({
            "title": "Festa", "date_text": "É HOJE",
            "date_interpretation": {
                "kind": "relative", "relative": "hoje",
                "absolute_date": "2027-01-01", "resolved_date": "2027-01-01",
            },
        })
        parsed = parse_extraction_response(raw)
        assert parsed["date_interpretation"] == {"kind": "relative", "relative": "hoje"}

    def test_a_weekday_day_interpretation_keeps_its_int_fields(self):
        raw = json.dumps({
            "title": "Festa", "date_text": "Quinta (02)",
            "date_interpretation": {"kind": "weekday_day", "weekday": "Quinta", "day": 2},
        })
        parsed = parse_extraction_response(raw)
        assert parsed["date_interpretation"] == {
            "kind": "weekday_day", "weekday": "quinta", "day": 2,
        }

    def test_a_bool_day_is_never_treated_as_an_int(self):
        """`bool` subclasses `int` in Python — the same guard this project's
        other admin-config validators already apply."""
        raw = json.dumps({
            "title": "Festa", "date_text": "algo",
            "date_interpretation": {"kind": "day_month", "day": True, "month": 2},
        })
        parsed = parse_extraction_response(raw)
        assert "day" not in parsed["date_interpretation"]


# ── plans/260812_event-attribution-and-dates.md §E ───────────────────────────
class TestBothPromptsDistinguishAnnouncementFromGreetingOrRecap:
    """Extending only one prompt would be the exact same half-fix already
    guarded against above for `kind`/`date_interpretation` — asserted
    directly on BOTH prompt strings."""

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_states_the_attendable_test(self, prompt):
        # Whitespace-normalized: the prompt hard-wraps this sentence across
        # lines, matching TestBothPromptsStateKindAndItsPrecedence's own
        # convention above for the SAME reason.
        normalized = " ".join(prompt.split())
        assert "does this announce something attendable" in normalized

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_names_greetings_and_recaps_as_other(self, prompt):
        normalized = " ".join(prompt.split())
        assert "congratulates, thanks, recaps, or reports" in normalized

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_states_the_birthday_greeting_stays_other_even_with_a_date_and_venue(self, prompt):
        normalized = " ".join(prompt.split())
        assert "even when it names a real event, a real venue, a real date" in normalized

    @pytest.mark.parametrize("prompt", [EXTRACTION_PROMPT, MULTI_EVENT_EXTRACTION_PROMPT])
    def test_prompt_names_the_recap_case_explicitly(self, prompt):
        normalized = " ".join(prompt.split())
        assert "RECAP" in normalized
        assert "already over" in normalized


class TestOtherVerdictSurvivesIntoThePostTypeColumn:
    """plans/260812_event-attribution-and-dates.md §E: `resolve_post_type`
    and `NON_EVENT_KINDS` already carry `other` and need no code change —
    this is the parser test that proves an `other` verdict the model
    returns (the §E prompt work's whole point) survives into the parsed
    `kind`/eventual `post_type` column rather than being coerced back to
    `event` somewhere along the way."""

    def test_a_birthday_greeting_response_parses_to_kind_other(self):
        raw = json.dumps({
            "kind": "other", "title": "31 Anos",
            "description": "Parabéns pelos seus 31 anos! Feliz aniversário!",
            "date_text": "08/08", "location_text": "Bar Tal", "confidence": 0.9,
        })
        parsed = parse_extraction_response(raw)
        assert parsed["kind"] == "other"

        from app.models.event_kind import resolve_post_type

        assert resolve_post_type(parsed["kind"]) == "other"
        assert resolve_post_type(parsed["kind"]) != "event"

    def test_a_recap_response_parses_to_kind_other(self):
        raw = json.dumps({
            "kind": "other", "title": "Obrigado!",
            "description": "Valeu a todo mundo que veio na sexta passada!",
            "date_text": "sexta passada", "confidence": 0.9,
        })
        parsed = parse_extraction_response(raw)

        from app.models.event_kind import resolve_post_type

        assert resolve_post_type(parsed["kind"]) == "other"

    def test_a_genuine_announcement_still_parses_to_kind_event(self):
        raw = json.dumps({
            "kind": "event", "title": "Festa de Sexta", "date_text": "sexta",
            "lineup": ["DJ X"], "price_text": "R$20", "confidence": 0.9,
        })
        parsed = parse_extraction_response(raw)

        from app.models.event_kind import resolve_post_type

        assert resolve_post_type(parsed["kind"]) == "event"
