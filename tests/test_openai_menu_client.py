"""Unit tests for `app.api.openai_menu_client` —
plans/260914_openai-token-budget-repo-audit.md.

No dedicated test file existed for this client before this plan.
`extract_menu_from_photos` runs, in production, on `settings.
menu_extraction_model` (this class's own `__init__` default,
`gpt-5.4-nano`, is never what actually gets constructed —
`app.container` always passes the settings value, which defaults to
`gpt-5.6-luna`, a reasoning model whose invisible reasoning tokens bill
against this same budget). The call was previously a FLAT 4096 regardless of
photo count, even though up to `settings.menu_photos_per_venue` (20) photos
can be sent in ONE call with no internal batching — this suite pins the new
`compute_menu_extraction_max_completion_tokens` formula the same way
`tests/test_multi_event_extraction.py::TestBudgetScaling` pins
`compute_multi_event_max_completion_tokens`.

`classify_menu_photos` is a separate, confirmed non-reasoning call
(`gpt-5.4-nano`, no override at its one call site) — its flat 1024 was left
unchanged by that plan; a regression guard below confirms this file's edit
did not touch it by accident.
"""
from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock

import pytest

from app.api.openai_menu_client import (
    MENU_EXTRACTION_BASE_COMPLETION_TOKENS,
    MENU_EXTRACTION_PER_PHOTO_COMPLETION_TOKENS,
    OpenAIMenuClient,
    compute_menu_extraction_max_completion_tokens,
)


def _run(coro):
    return asyncio.run(coro)


def _fake_response(content: str):
    message = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=message, finish_reason="stop")
    usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    return types.SimpleNamespace(choices=[choice], usage=usage)


_EMPTY_MENU_JSON = json.dumps({"menu_sections": [], "metadata": {}})
_MENU_FILTER_JSON = json.dumps({"results": [{"index": 0, "is_menu": True, "confidence": 0.9}]})


class TestBudgetScaling:
    """Mirrors tests/test_multi_event_extraction.py::TestBudgetScaling —
    same formula shape (BASE + PER_UNIT * max(1, count)), same reasoning."""

    def test_budget_increases_with_photo_count(self):
        small = compute_menu_extraction_max_completion_tokens(1)
        large = compute_menu_extraction_max_completion_tokens(20)
        assert large > small

    def test_budget_matches_the_documented_formula(self):
        assert compute_menu_extraction_max_completion_tokens(5) == (
            MENU_EXTRACTION_BASE_COMPLETION_TOKENS + MENU_EXTRACTION_PER_PHOTO_COMPLETION_TOKENS * 5
        )

    def test_a_misconfigured_zero_still_gets_at_least_one_photos_worth_of_headroom(self):
        assert compute_menu_extraction_max_completion_tokens(0) == (
            MENU_EXTRACTION_BASE_COMPLETION_TOKENS + MENU_EXTRACTION_PER_PHOTO_COMPLETION_TOKENS
        )

    @pytest.mark.parametrize("photo_count", [1, 10, 20])
    def test_budget_arithmetic_at_1_10_and_20_photos(self, photo_count):
        """20 is settings.menu_photos_per_venue — the configured ceiling on
        how many photos one `extract_menu_from_photos` call can receive."""
        expected = (
            MENU_EXTRACTION_BASE_COMPLETION_TOKENS
            + MENU_EXTRACTION_PER_PHOTO_COMPLETION_TOKENS * photo_count
        )
        assert compute_menu_extraction_max_completion_tokens(photo_count) == expected

    def test_the_floor_exceeds_the_old_flat_value(self):
        """The old flat budget (4096) must not be the CEILING of the new
        formula for any realistic photo count above 1 — otherwise "made it
        dynamic" would not actually raise anything for the venues that most
        needed it (a large multi-page menu spread across many photos)."""
        assert compute_menu_extraction_max_completion_tokens(5) > 4096


class TestExtractMenuFromPhotosCallShape:
    def test_passes_the_computed_budget_to_max_completion_tokens(self):
        client = OpenAIMenuClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response(_EMPTY_MENU_JSON))
        client.client.chat.completions.create = create

        urls = [f"https://example.com/{i}.jpg" for i in range(7)]
        _run(client.extract_menu_from_photos(urls))

        _, kwargs = create.call_args
        assert kwargs["max_completion_tokens"] == compute_menu_extraction_max_completion_tokens(7)

    def test_never_passes_the_legacy_max_tokens_param(self):
        client = OpenAIMenuClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response(_EMPTY_MENU_JSON))
        client.client.chat.completions.create = create

        _run(client.extract_menu_from_photos(["https://example.com/0.jpg"]))

        _, kwargs = create.call_args
        assert "max_tokens" not in kwargs
        assert "max_completion_tokens" in kwargs


class TestClassifyMenuPhotosUnchanged:
    """plans/260914_openai-token-budget-repo-audit.md left this call flat —
    confirmed non-reasoning model, small realistic worst case. Regression
    guard that editing extract_menu_from_photos in the same file did not
    accidentally change this one too."""

    def test_still_passes_exactly_1024(self):
        client = OpenAIMenuClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response(_MENU_FILTER_JSON))
        client.client.chat.completions.create = create

        _run(client.classify_menu_photos(["https://example.com/0.jpg"]))

        _, kwargs = create.call_args
        assert kwargs["max_completion_tokens"] == 1024

    def test_never_passes_the_legacy_max_tokens_param(self):
        client = OpenAIMenuClient(api_key="sk-test")
        create = AsyncMock(return_value=_fake_response(_MENU_FILTER_JSON))
        client.client.chat.completions.create = create

        _run(client.classify_menu_photos(["https://example.com/0.jpg"]))

        _, kwargs = create.call_args
        assert "max_tokens" not in kwargs
        assert "max_completion_tokens" in kwargs
