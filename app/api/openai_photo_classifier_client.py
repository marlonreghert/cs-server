"""OpenAI vision client for per-photo classification.

Two passes, deliberately not one:

  * `classify_photos` — a small prompt, one category per image. Runs BEFORE the
    image is stored, so it lands in the right folder with no second write.
  * `derive_attributes` — photos grouped by category, one focused prompt per
    category, `other` skipped entirely.

Two passes cost more image tokens than one six-schema mega-prompt, and buy two
things worth more than that: a focused prompt is measurably more accurate than
one carrying every schema at once, and adding a field later re-runs ONE category
rather than the whole catalogue.

Both take a plain list of image urls and neither cares where they came from —
provider CDN links during a live run, presigned S3 urls when attributes are
re-derived over an archived run. That is what lets the schema grow without
paying a provider again.

Modelled on `OpenAIMenuClient.classify_menu_photos`: same batching, same
`detail: "low"` thumbnails, same json_object response format.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from openai import AsyncOpenAI

from app.metrics import (
    OPENAI_API_CALLS_TOTAL,
    OPENAI_API_CALL_DURATION_SECONDS,
)
from app.models.photo_taxonomy import (
    describe_attributes,
    describe_categories,
    describe_other_kind,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"


def _category_prompt() -> str:
    return (
        "You are labelling photos from a bar/restaurant's Google Maps listing "
        "for a Brazilian nightlife app.\n\n"
        "Put each image in EXACTLY ONE category:\n"
        f"{describe_categories()}\n\n"
        "Also report, for each image:\n"
        "- quality: one of boa, escura, borrada, baixa_resolucao\n"
        # Asked HERE, in pass 1, because pass 2 skips `other` entirely.
        f"{describe_other_kind()}\n"
        "- likely_authorship: by_owner or by_visitor. Staged, empty, evenly lit "
        "and professionally plated reads as by_owner; handheld, candid, with "
        "people and mixed light reads as by_visitor. Omit if unsure.\n"
        "- confidence: 0 to 1. Be honest — a low confidence is filed as `other`, "
        "which is better than a wrong label.\n\n"
        "Reply ONLY with JSON:\n"
        '{"results": [{"index": 0, "category": "interior", "quality": "boa", '
        '"likely_authorship": "by_owner", "confidence": 0.93}]}\n\n'
        "One entry per image, in order."
    )


def _attribute_prompt(category: str) -> str:
    return (
        f"You are describing photos already classified as `{category}` from a "
        "bar/restaurant's Google Maps listing, for a Brazilian nightlife app.\n\n"
        "For each image, report these attributes. OMIT any attribute you cannot "
        "read from the photo — a missing value is far better than a guess.\n\n"
        f"{describe_attributes(category)}\n\n"
        "Reply ONLY with JSON:\n"
        '{"results": [{"index": 0, "attributes": {...}, "people": {...}}]}\n\n'
        "One entry per image, in order. Use only the listed values."
    )


def _strip_fences(raw_text: str) -> str:
    cleaned = (raw_text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    return re.sub(r"\s*```$", "", cleaned)


def _batched(items: list, size: int) -> list[list]:
    size = max(1, int(size))
    return [items[i:i + size] for i in range(0, len(items), size)]


class OpenAIPhotoClassifierClient:
    """Async vision client for the photo classifier."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL):
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key)

    async def close(self) -> None:
        await self.client.close()

    async def classify_photos(
        self, photo_urls: list[str], *, model: Optional[str] = None, batch_size: int = 10
    ) -> list[dict]:
        """One verdict per photo, index-aligned with `photo_urls`."""
        return await self._run_passes(
            photo_urls, _category_prompt(), "photo_classify", model, batch_size
        )

    async def derive_attributes(
        self, category: str, photo_urls: list[str], *,
        model: Optional[str] = None, batch_size: int = 10,
    ) -> list[dict]:
        """One attribute object per photo, index-aligned with `photo_urls`."""
        return await self._run_passes(
            photo_urls, _attribute_prompt(category), "photo_attributes",
            model, batch_size,
        )

    async def _run_passes(
        self, photo_urls: list[str], prompt: str, endpoint: str,
        model: Optional[str], batch_size: int,
    ) -> list[dict]:
        if not photo_urls:
            return []
        out: list[dict] = []
        for batch in _batched(list(photo_urls), batch_size):
            # A failed batch yields empty verdicts for ITS photos only. One bad
            # batch must not cost a venue the photos in every other batch.
            results = await self._one_batch(batch, prompt, endpoint, model)
            by_index = {r.get("index"): r for r in results if isinstance(r, dict)}
            out.extend(dict(by_index.get(i) or {}) for i in range(len(batch)))
        return out

    async def _one_batch(
        self, urls: list[str], prompt: str, endpoint: str, model: Optional[str]
    ) -> list[dict]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for url in urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": url, "detail": "low"},
            })

        started = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=model or self.model,
                messages=[{"role": "user", "content": content}],
                temperature=0.1,
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
            duration = time.perf_counter() - started
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint=endpoint, status="success").inc()
            return self._parse(response.choices[0].message.content or "", endpoint)
        except Exception as e:  # noqa: BLE001 — the caller degrades, never fails
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(
                time.perf_counter() - started
            )
            OPENAI_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            logger.error(f"[PhotoClassifier] {endpoint} batch failed: {e}")
            return []

    def _parse(self, raw_text: str, endpoint: str) -> list[dict]:
        try:
            data = json.loads(_strip_fences(raw_text))
        except json.JSONDecodeError as e:
            logger.error(f"[PhotoClassifier] {endpoint} returned unparseable JSON: {e}")
            return []
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            logger.warning(f"[PhotoClassifier] {endpoint} response had no results array")
            return []
        return [r for r in results if isinstance(r, dict)]
