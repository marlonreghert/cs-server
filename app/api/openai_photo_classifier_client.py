"""OpenAI vision client for per-photo classification.

**One pass.** The category, that category's attributes and the people block come
back from a single call, because the schema is small enough to fit one prompt
without blunting it. Two passes would send every image twice — image tokens are
the bill here, and a second look buys nothing when the second question is four
coarse enums.

The model reports a confidence **per attribute**, not one for the photo: it can
be certain a room is a bar and unsure whether it has screens, and those two
facts must not share a fate. The service drops anything under the bar to
`not_classified`.

Takes a plain list of image urls and does not care where they came from —
provider CDN links during a live run, presigned S3 urls when an archived run is
re-classified after the schema changes. That is what lets the schema grow
without paying a provider again.

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
    CATEGORY_OTHER,
    NOT_APPLICABLE,
    NOT_CLASSIFIED,
    QUALITY_VALUES,
    describe_categories,
    describe_people,
    describe_schema,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"


def _prompt(with_attributes: bool = True) -> str:
    head = (
        "You are labelling photos from a bar/restaurant's Google Maps listing "
        "for a Brazilian nightlife app.\n\n"
        "Put each image in EXACTLY ONE category:\n"
        f"{describe_categories()}\n\n"
        "Also report for each image:\n"
        "- quality: good or poor — is this worth showing in an app\n"
        # Spelled out as opposing checklists because a vaguer version of this
        # returned `by_visitor` for 20 photos out of 20 on a real run: the model
        # will pick the safer-sounding label for everything unless the two are
        # made concretely distinguishable, and a constant is not a signal.
        "- likely_authorship: WHO TOOK THIS PHOTO, as an object with a value "
        "and a confidence.\n"
        "    by_owner  — looks commissioned: even/studio lighting, deliberate "
        "composition, empty of customers, food styled and centred, wide "
        "architectural shot of the room, a logo or branding in frame.\n"
        "    by_visitor — looks handheld: phone framing, flash or mixed light, "
        "other diners visible, a half-eaten plate, a tilted horizon, a drink "
        "held up to the camera.\n"
        "    Most Google Maps photos are by_visitor, so only say by_owner when "
        "the staged cues are actually there — but a venue's own gallery is "
        "usually by_owner, and both DO occur. Give a LOW confidence when the "
        "cues are mixed; a low-confidence guess is dropped, which is the right "
        "outcome for a photo that could be either.\n"
        f"- confidence: 0 to 1 for the category. Be honest — a low confidence "
        f"is filed as `other`, which is better than a wrong label.\n"
    )
    if not with_attributes:
        return head + (
            '\nReply ONLY with JSON:\n'
            '{"results": [{"index": 0, "category": "interior", '
            '"quality": {"value": "good", "confidence": 0.9}, '
            '"confidence": 0.93}]}\n\n'
            "One entry per image, in order."
        )
    return head + (
        "\nThen describe the image using ONLY the attributes of the category "
        "you chose:\n\n"
        f"{describe_schema()}\n\n"
        "And, ONLY if people are visible — whatever the category — a `people` "
        "object. A photo with nobody in it must have NO people object:\n"
        f"{describe_people()}\n\n"
        "EVERY attribute is an object with a value AND your confidence in that "
        f"one attribute: {{\"value\": ..., \"confidence\": 0.0-1.0}}. Confidence "
        "is per attribute, not for the whole photo — be sure about one field "
        "and unsure about the next.\n\n"
        # These two get conflated constantly, and conflating them makes an
        # accurate classifier read as a failing one: `drink_type` scored 4/13 on
        # a real run almost entirely because nine of those photos were food with
        # no drink in them at all.
        "Two different ways of not answering, and the difference matters:\n"
        f"- `{NOT_APPLICABLE}` — the question DOES NOT ARISE for this photo. A "
        "plate of dessert has no drink in it, so drink_type is "
        f"`{NOT_APPLICABLE}`. Use this whenever the thing being asked about is "
        "simply not in the frame. It is a confident answer: give it a HIGH "
        "confidence.\n"
        f"- `{NOT_CLASSIFIED}` — the question arises but you CANNOT TELL. The "
        "glass is there but too dark to identify. Use this only when the "
        "subject is present and you genuinely cannot read it.\n"
        f"Both are expected outcomes, not failures. Never guess to avoid "
        f"them.\n\n"
        f"When the category is `{CATEGORY_OTHER}`, other_kind is REQUIRED — it "
        "is the only record of why the photo could not be categorized, and "
        f"`{NOT_CLASSIFIED}` there means the photo is lost to us.\n\n"
        "Reply ONLY with JSON:\n"
        '{"results": [{"index": 0, "category": "interior", "confidence": 0.93,\n'
        '  "quality": {"value": "good", "confidence": 0.95},\n'
        '  "likely_authorship": {"value": "by_visitor", "confidence": 0.82},\n'
        '  "attributes": {"space_type": {"value": "bar", "confidence": 0.91},\n'
        '                 "lighting": {"value": "dim", "confidence": 0.88}},\n'
        '  "people": {"crowd_level": {"value": "busy", "confidence": 0.9}}}]}\n\n'
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
        self, photo_urls: list[str], *, model: Optional[str] = None,
        batch_size: int = 10, with_attributes: bool = True,
    ) -> list[dict]:
        """One verdict per photo, index-aligned with `photo_urls`.

        `with_attributes=False` asks for the category alone — the operator's
        cheap mode, which skips the attribute JSON that dominates output cost.
        """
        if not photo_urls:
            return []
        prompt = _prompt(with_attributes)
        out: list[dict] = []
        for batch in _batched(list(photo_urls), batch_size):
            # A failed batch yields empty verdicts for ITS photos only. One bad
            # batch must not cost a venue the photos in every other batch.
            results = await self._one_batch(batch, prompt, model)
            by_index = {r.get("index"): r for r in results if isinstance(r, dict)}
            out.extend(dict(by_index.get(i) or {}) for i in range(len(batch)))
        return out

    async def _one_batch(
        self, urls: list[str], prompt: str, model: Optional[str]
    ) -> list[dict]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for url in urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": url, "detail": "low"},
            })

        started = time.perf_counter()
        endpoint = "photo_classify"
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
            return self._parse(response.choices[0].message.content or "")
        except Exception as e:  # noqa: BLE001 — the caller degrades, never fails
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint=endpoint).observe(
                time.perf_counter() - started
            )
            OPENAI_API_CALLS_TOTAL.labels(endpoint=endpoint, status="error").inc()
            logger.error(f"[PhotoClassifier] batch failed: {e}")
            return []

    def _parse(self, raw_text: str) -> list[dict]:
        try:
            data = json.loads(_strip_fences(raw_text))
        except json.JSONDecodeError as e:
            logger.error(f"[PhotoClassifier] returned unparseable JSON: {e}")
            return []
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            logger.warning("[PhotoClassifier] response had no results array")
            return []
        return [r for r in results if isinstance(r, dict)]


# Re-exported so a caller can state the quality vocabulary without reaching into
# the taxonomy module for it.
__all__ = ["OpenAIPhotoClassifierClient", "DEFAULT_MODEL", "QUALITY_VALUES"]
