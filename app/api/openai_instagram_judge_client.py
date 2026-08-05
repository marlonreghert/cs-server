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
                max_completion_tokens=200,
                **sampling_kwargs(model, 0),
            )
            status = "success"
            return json.loads(response.choices[0].message.content or "{}")
        finally:
            OPENAI_API_CALLS_TOTAL.labels(endpoint=ENDPOINT_LABEL, status=status).inc()
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint=ENDPOINT_LABEL).observe(
                time.time() - started
            )
