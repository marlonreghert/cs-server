"""Unit tests for `app.api.openai_vibe_client` —
plans/260914_openai-token-budget-repo-audit.md.

No dedicated test file existed for this client before this plan. Both
`classify_venue_vibes_stage_a` and `classify_venue_vibes_stage_b` run, in
production, on `settings.vibe_classifier_stage_a_model`/`_stage_b_model`
(this class's own per-call `model` PARAMETER defaults,
`gpt-5.4-nano`/`gpt-5.4-mini`, are never what `app.container` actually
passes — it always supplies the settings value, which defaults to
`gpt-5.6-luna` for both, a reasoning model whose invisible reasoning tokens
bill against these same budgets). Both calls were previously a flat 3072
with no accounting for that, and no per-site live measurement exists (CLAUDE.md
forbids live OpenAI calls in tests) — see the module-level comment above
`STAGE_A_MAX_COMPLETION_TOKENS`/`STAGE_B_MAX_COMPLETION_TOKENS` in
`app/api/openai_vibe_client.py` for the full reasoning. This suite proves the
two constants are wired through to their real calls and pins their values
and relative ordering (Stage A's schema is structurally richer than Stage
B's, so Stage A's budget must stay above Stage B's).
"""
from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock

from app.api.openai_vibe_client import (
    STAGE_A_MAX_COMPLETION_TOKENS,
    STAGE_B_MAX_COMPLETION_TOKENS,
    OpenAIVibeClient,
)


def _run(coro):
    return asyncio.run(coro)


def _fake_response(content: str):
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message, finish_reason="stop")
    usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    return types.SimpleNamespace(choices=[choice], usage=usage)


_STAGE_A_JSON = json.dumps({"overall_confidence": 0.5, "top_vibes": []})
_STAGE_B_JSON = json.dumps({"refined_categories": {}, "overall_confidence": 0.7})


class TestConfiguredBudgets:
    def test_stage_a_is_the_evidence_backed_value(self):
        assert STAGE_A_MAX_COMPLETION_TOKENS == 6144

    def test_stage_b_is_the_evidence_backed_value(self):
        assert STAGE_B_MAX_COMPLETION_TOKENS == 5120

    def test_stage_a_stays_above_stage_b(self):
        """Stage A's schema (8 category blocks + a photos[] entry per photo)
        is structurally richer than Stage B's (only the categories
        _should_escalate flagged uncertain) — the file's relative-output-size
        ordering must survive both being raised."""
        assert STAGE_A_MAX_COMPLETION_TOKENS > STAGE_B_MAX_COMPLETION_TOKENS


class TestStageACallShape:
    def test_passes_the_configured_budget_to_max_completion_tokens(self):
        client = OpenAIVibeClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response(_STAGE_A_JSON))
        client.client.chat.completions.create = create

        _run(client.classify_venue_vibes_stage_a(
            photo_urls=["https://example.com/0.jpg"], venue_name="Bar X",
        ))

        _, kwargs = create.call_args
        assert kwargs["max_completion_tokens"] == STAGE_A_MAX_COMPLETION_TOKENS

    def test_never_passes_the_legacy_max_tokens_param(self):
        client = OpenAIVibeClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response(_STAGE_A_JSON))
        client.client.chat.completions.create = create

        _run(client.classify_venue_vibes_stage_a(
            photo_urls=["https://example.com/0.jpg"], venue_name="Bar X",
        ))

        _, kwargs = create.call_args
        assert "max_tokens" not in kwargs
        assert "max_completion_tokens" in kwargs


class TestStageBCallShape:
    def test_passes_the_configured_budget_to_max_completion_tokens(self):
        client = OpenAIVibeClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response(_STAGE_B_JSON))
        client.client.chat.completions.create = create

        _run(client.classify_venue_vibes_stage_b(
            photo_urls=["https://example.com/0.jpg"],
            stage_a_result={"estilo_do_lugar": {"labels": []}},
            uncertain_facets=["estilo_do_lugar"],
            venue_name="Bar X",
        ))

        _, kwargs = create.call_args
        assert kwargs["max_completion_tokens"] == STAGE_B_MAX_COMPLETION_TOKENS

    def test_never_passes_the_legacy_max_tokens_param(self):
        client = OpenAIVibeClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response(_STAGE_B_JSON))
        client.client.chat.completions.create = create

        _run(client.classify_venue_vibes_stage_b(
            photo_urls=["https://example.com/0.jpg"],
            stage_a_result={"estilo_do_lugar": {"labels": []}},
            uncertain_facets=["estilo_do_lugar"],
            venue_name="Bar X",
        ))

        _, kwargs = create.call_args
        assert "max_tokens" not in kwargs
        assert "max_completion_tokens" in kwargs
