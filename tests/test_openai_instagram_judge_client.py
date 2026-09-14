"""Unit tests for `app.api.openai_instagram_judge_client` —
plans/260914_openai-token-budget-repo-audit.md.

No dedicated test file existed for this client before this plan.
`MAX_COMPLETION_TOKENS` gates the budget for a model that, in production
(`settings.instagram_judge_model`, pinned in
`tests/test_container_judge_wiring.py::TestSettingsAreNotDuplicated::
test_the_default_model_is_the_reasoning_one`), is `gpt-5.6-luna` — a
reasoning model whose invisible reasoning tokens are billed against this same
budget. The old value (200) was the EXACT figure
`plans/260914_event-venue-advisor-token-budget.md` measured against real
production traffic to silently return empty content 7.5% of the time on this
same model for a comparably small/simple decision call. That 120-call
measurement is not reproduced here (CLAUDE.md forbids live OpenAI calls in
tests); what IS tested here is that the client wires the constant through to
the real API call, and that the constant itself has not drifted back down.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

from app.api.openai_instagram_judge_client import (
    MAX_COMPLETION_TOKENS,
    OpenAIInstagramJudgeClient,
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
        silently drift back toward the 200 that measured 7.5% empty
        responses in production for a comparable call on this same model."""
        assert MAX_COMPLETION_TOKENS == 4096


class TestJudgeInstagramMatchCallShape:
    def test_passes_the_configured_budget_to_max_completion_tokens(self):
        """Proves the client actually wires the constant through to the real
        API call rather than the constant existing only on paper."""
        client = OpenAIInstagramJudgeClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response('{"is_match": true, "confidence": 0.8}'))
        client.client.chat.completions.create = create

        _run(client.judge_instagram_match(prompt="x", model="gpt-5.6-luna"))

        _, kwargs = create.call_args
        assert kwargs["max_completion_tokens"] == MAX_COMPLETION_TOKENS

    def test_never_passes_the_legacy_max_tokens_param(self):
        """gpt-5.6 rejects the legacy `max_tokens` param with a 400 — this
        call site must always bound output via `max_completion_tokens`."""
        client = OpenAIInstagramJudgeClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response('{"is_match": false, "confidence": 0.1}'))
        client.client.chat.completions.create = create

        _run(client.judge_instagram_match(prompt="x", model="gpt-5.6-luna"))

        _, kwargs = create.call_args
        assert "max_tokens" not in kwargs
        assert "max_completion_tokens" in kwargs

    def test_the_budget_is_the_same_regardless_of_image_count(self):
        """This call's output shape is fixed (`{"is_match", "confidence",
        "reason"}`) whatever mode it runs in (text-only, partial, or both
        images) — unlike the menu/vibe/multi-event budgets, there is no known
        input signal to scale by here, so the budget stays a flat constant."""
        client = OpenAIInstagramJudgeClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response('{"is_match": true, "confidence": 0.5}'))
        client.client.chat.completions.create = create

        _run(client.judge_instagram_match(
            prompt="x", model="gpt-5.6-luna",
            profile_image_url="https://example.com/p.jpg",
            venue_photos=["https://example.com/a.jpg", "https://example.com/b.jpg"],
        ))

        _, kwargs = create.call_args
        assert kwargs["max_completion_tokens"] == MAX_COMPLETION_TOKENS
