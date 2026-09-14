"""Unit tests for the `pick_display_title` call site in
`app.api.openai_event_extraction_client` — plans/260914_openai-token-budget-repo-audit.md.

Mirrors `tests/test_event_venue_advisor_client.py` exactly: same module, same
bug class, written the same day. `TITLE_PICK_MAX_COMPLETION_TOKENS` gates the
budget for a model (`gpt-5.6-luna`, `DEFAULT_MODEL` below) whose invisible
reasoning tokens are billed against it. `plans/260914_event-venue-advisor-
token-budget.md` measured, against real production traffic on 2026-09-14,
that 200 completion tokens routinely exhausts on reasoning alone before any
visible output — 9/120 empty responses at 400, 0/120 at 3000. The old
TITLE_PICK value (160) was smaller than the proven-unsafe 200, on the same
model, for a comparably small/simple decision task. That 120-call
measurement is not reproduced here (CLAUDE.md forbids live OpenAI calls in
tests, and a deterministic fake would return whatever content it is told
regardless of the token budget passed); what IS tested here is that the
client actually wires the constant through to the real API call, and that
the constant itself has not drifted back down.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

from app.api.openai_event_extraction_client import (
    TITLE_PICK_MAX_COMPLETION_TOKENS,
    OpenAIEventExtractionClient,
)


def _run(coro):
    return asyncio.run(coro)


def _fake_response(content: str):
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message, finish_reason="stop")
    usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=20)
    return types.SimpleNamespace(choices=[choice], usage=usage)


class TestConfiguredBudget:
    def test_the_current_value_is_the_evidence_backed_one(self):
        """Regression guard: pins the real, current value so this cannot
        silently drift back toward the 160 that was smaller than the
        already-proven-unsafe 200 (see the module comment and
        plans/260914_openai-token-budget-repo-audit.md). Same style as
        `tests/test_event_venue_advisor_client.py`'s own regression guard."""
        assert TITLE_PICK_MAX_COMPLETION_TOKENS == 4096


class TestPickDisplayTitleCallShape:
    def test_passes_the_configured_budget_to_max_completion_tokens(self):
        """Proves the client actually wires the constant through to the real
        API call rather than the constant existing only on paper — same
        style as `TestExtractEventsTruncationDetection.
        test_the_call_budget_scales_with_max_events`."""
        client = OpenAIEventExtractionClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response('{"display_title": "x"}'))
        client.client.chat.completions.create = create

        _run(client.pick_display_title(
            venue_name="Venue", local_date="2026-09-14", source_titles=["A", "B"],
        ))

        _, kwargs = create.call_args
        assert kwargs["max_completion_tokens"] == TITLE_PICK_MAX_COMPLETION_TOKENS

    def test_never_passes_the_legacy_max_tokens_param(self):
        """gpt-5.6 rejects the legacy `max_tokens` param with a 400 — this
        call site must always bound output via `max_completion_tokens`."""
        client = OpenAIEventExtractionClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response('{"display_title": "x"}'))
        client.client.chat.completions.create = create

        _run(client.pick_display_title(
            venue_name="Venue", local_date="2026-09-14", source_titles=["A"],
        ))

        _, kwargs = create.call_args
        assert "max_tokens" not in kwargs
        assert "max_completion_tokens" in kwargs

    def test_returns_the_raw_response_text_unparsed(self):
        """Confirms an empty `content` (the exact failure mode this fix
        targets) still comes back as `""`, not an exception, so the caller's
        existing malformed-rejection path is the only thing that changes
        behavior once the budget is raised."""
        client = OpenAIEventExtractionClient(api_key="sk-test")
        client.client.chat.completions.create = AsyncMock(return_value=_fake_response(""))

        raw = _run(client.pick_display_title(
            venue_name="Venue", local_date="2026-09-14", source_titles=["A"],
        ))

        assert raw == ""
