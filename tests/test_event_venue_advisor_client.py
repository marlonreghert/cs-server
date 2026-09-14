"""Unit tests for the `recommend_event_venue` call site in
`app.api.openai_event_extraction_client` — plans/260914_event-venue-advisor-token-budget.md.

Division of labour against the rest of the `test_event_venue_advisor*` family:
`test_event_venue_advisor.py` covers `EventVenueAdvisorService` against a
`_FakeOpenAI` fake (service layer); `test_event_venue_advisor_validator.py`
covers the pure validator; this file covers the one thing neither of those
touches — the real `OpenAIEventExtractionClient.recommend_event_venue` call
site and the `max_completion_tokens` budget it passes through.

`EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS` gates that budget for a model
(`gpt-5.6-luna`) whose invisible reasoning tokens are billed against it. A
sibling investigation measured, against real production traffic on
2026-09-14, that 200 (and even 400) completion tokens routinely exhausts on
reasoning alone before any visible output — 9/120 empty responses at 400,
0/120 at 3000 — which the validator cannot distinguish from a legitimate
"no candidate supported by the text" rejection
(`app.services.event_venue_advisor_validator.REJECTION_MALFORMED`). That
120-call measurement is not reproduced here (CLAUDE.md forbids live OpenAI
calls in tests, and a deterministic fake would return whatever content it is
told regardless of the token budget passed); what IS tested here is that the
client actually wires the constant through to the real API call, and that
the constant itself has not drifted back down.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

from app.api.openai_event_extraction_client import (
    EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS,
    OpenAIEventExtractionClient,
)


def _run(coro):
    return asyncio.run(coro)


def _fake_response(content: str):
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message, finish_reason="stop")
    usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=20)
    return types.SimpleNamespace(choices=[choice], usage=usage)


_CANDIDATES = [{"venue_id": "v1", "venue_name": "Venue One"}]


class TestConfiguredBudget:
    def test_the_current_value_is_the_evidence_backed_one(self):
        """Regression guard: pins the real, current value so this cannot
        silently drift back toward the 200 that measured 7.5% empty
        responses in production (see the module docstring and
        plans/260914_event-venue-advisor-token-budget.md). Same style as
        `tests/test_event_venue_resolution.py`'s
        `test_default_top_k_is_twenty`."""
        assert EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS == 4096


class TestRecommendEventVenueCallShape:
    def test_passes_the_configured_budget_to_max_completion_tokens(self):
        """Same style as `tests/test_multi_event_extraction.py`'s
        `TestExtractEventsTruncationDetection.
        test_the_call_budget_scales_with_max_events` — proves the client
        actually wires the constant through to the real API call rather than
        the constant existing only on paper."""
        client = OpenAIEventExtractionClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response('{"venue_id": "v1", "evidence_quote": "x"}'))
        client.client.chat.completions.create = create

        _run(client.recommend_event_venue(
            location_text="x", caption=None, candidates=_CANDIDATES,
        ))

        _, kwargs = create.call_args
        assert kwargs["max_completion_tokens"] == EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS

    def test_never_passes_the_legacy_max_tokens_param(self):
        """gpt-5.6 rejects the legacy `max_tokens` param with a 400 — this
        call site must always bound output via `max_completion_tokens`, the
        same guard `test_multi_event_extraction.py` keeps for `extract_events`."""
        client = OpenAIEventExtractionClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response('{"venue_id": null, "evidence_quote": null}'))
        client.client.chat.completions.create = create

        _run(client.recommend_event_venue(
            location_text="x", caption=None, candidates=_CANDIDATES,
        ))

        _, kwargs = create.call_args
        assert "max_tokens" not in kwargs
        assert "max_completion_tokens" in kwargs

    def test_returns_the_raw_response_text_unparsed(self):
        """`recommend_event_venue` itself never parses or validates — that is
        `parse_event_venue_advisor_response`'s and the validator's job (see
        the client method's own docstring). Confirms an empty `content`
        (the exact failure mode this fix targets) still comes back as `""`,
        not an exception, so the caller's existing malformed-rejection path
        is the only thing that changes behavior once the budget is raised."""
        client = OpenAIEventExtractionClient(api_key="sk-test")
        client.client.chat.completions.create = AsyncMock(return_value=_fake_response(""))

        raw = _run(client.recommend_event_venue(
            location_text="x", caption=None, candidates=_CANDIDATES,
        ))

        assert raw == ""
