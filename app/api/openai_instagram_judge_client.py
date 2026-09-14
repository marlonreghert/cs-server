"""OpenAI client for adjudicating whether an Instagram profile is a venue's.

Deliberately thin. `InstagramJudge` owns the prompt, the mode selection and the
text-only ceiling; this owns the call, the images and the JSON. Modelled on
`OpenAIPhotoClassifierClient`: same AsyncOpenAI usage, same `detail: "low"`
thumbnails, same `json_object` response format.

**It must work with no images at all.** That is the normal case here, not a
degraded one: Instagram blocks the datacenter IP, so there is usually no profile
picture to compare, and many venues have no archived photos either. When no
images exist the call is plain text — cheaper, and still the only signal that can
separate a venue's account from a lookalike.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from openai import AsyncOpenAI

from app.api.openai_compat import sampling_kwargs
from app.metrics import (
    OPENAI_API_CALLS_TOTAL,
    OPENAI_API_CALL_DURATION_SECONDS,
)

logger = logging.getLogger(__name__)

ENDPOINT_LABEL = "instagram_judge"

# plans/260914_openai-token-budget-repo-audit.md: this file's own
# `InstagramJudge.__init__` default LOOKS non-reasoning (`gpt-5.4-mini`,
# instagram_judge.py:87), but app.container._build_instagram_judge always
# constructs it with `model=settings.instagram_judge_model`
# (app/config.py:414), which defaults to `gpt-5.6-luna` — a reasoning model
# whose invisible reasoning tokens bill against this SAME budget before a
# single visible `{"is_match", "confidence", "reason"}` token is written.
# 200 is the exact value
# plans/260914_event-venue-advisor-token-budget.md measured against real
# production traffic to silently return EMPTY 7.5% of the time on this same
# model for a comparably small/simple decision call — this was the smallest
# budget of any reasoning-model call site found in that audit. No live
# sample exists for this call specifically; reusing the one figure this
# codebase HAS validated in production for this exact model on a comparably
# small text+image judgement task, rather than guessing a smaller number
# with no evidence behind it. max_completion_tokens is a ceiling, not a
# floor, so this costs nothing unless the model actually needs it.
MAX_COMPLETION_TOKENS = 4096


class OpenAIInstagramJudgeClient:
    def __init__(self, api_key: str, *, timeout: float = 30.0):
        self.client = AsyncOpenAI(api_key=api_key, timeout=timeout)

    async def judge_instagram_match(
        self,
        *,
        prompt: str,
        model: str,
        profile_image_url: Optional[str] = None,
        venue_photos: Optional[list] = None,
    ) -> Any:
        """Ask the model, return the parsed JSON object.

        Raises on failure — `InstagramJudge` catches everything and degrades the
        candidate rather than failing the venue, so there is nothing to swallow
        twice here.
        """
        content: list[dict] = [{"type": "text", "text": prompt}]
        for url in ([profile_image_url] if profile_image_url else []) + list(venue_photos or []):
            if url:
                content.append(
                    {"type": "image_url", "image_url": {"url": url, "detail": "low"}}
                )

        started = time.time()
        status = "error"
        try:
            response = await self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                response_format={"type": "json_object"},
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                **sampling_kwargs(model, 0),
            )
            status = "success"
            return json.loads(response.choices[0].message.content or "{}")
        finally:
            OPENAI_API_CALLS_TOTAL.labels(endpoint=ENDPOINT_LABEL, status=status).inc()
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint=ENDPOINT_LABEL).observe(
                time.time() - started
            )
