"""OpenAI vision client for Instagram event extraction.

One call per post (see app/services/event_extraction_service.py for why: a
flyer's output is variable-length — a twelve-act lineup vs one DJ — and a
batch shares one output budget, which is exactly the failure mode measured in
docs/venue-retrieval-storage.md §4 (16 verdicts back for 20 images, a flat
`max_tokens` truncating the JSON and losing the whole batch).

The model extracts RAW text, never a computed date. `date_text`/`time_text`
are copied verbatim from what the flyer/caption says ("15/08", "toda quinta",
"22h às 04h") — resolving them into a real instant is
app/services/event_date_resolver.py's job, a deterministic, unit-tested
Python function, not something asked of the model. A model cannot reliably do
temporal arithmetic against a reference date it cannot be trusted to hold onto
across a long prompt, and CLAUDE.md/the plan both require that resolution be
provable, not asserted by a vendor.

Images are inlined as a `data:` URI (see
MediaArchiveStore.read_image_data_uri) rather than a presigned S3 url: a
presigned url signed with the instance role's temporary STS credentials is
~1,900 characters and OpenAI rejects it with `invalid_image_url` even though
it fetches fine from inside the VPC.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from openai import AsyncOpenAI

from app.api.openai_compat import sampling_kwargs
from app.metrics import (
    OPENAI_API_CALLS_TOTAL,
    OPENAI_API_CALL_DURATION_SECONDS,
    OPENAI_TOKENS_TOTAL,
)

logger = logging.getLogger(__name__)

ENDPOINT = "event_extract"
DEFAULT_MODEL = "gpt-5.6-luna"
# Reasoning tokens (gpt-5.6 is a reasoning model) count against
# max_completion_tokens, so this carries real headroom above a typical
# lineup+description payload rather than the ~1-2k a non-reasoning extractor
# would need.
DEFAULT_MAX_COMPLETION_TOKENS = 4096

EXTRACTION_PROMPT = """## Role
You read a single Instagram post (caption + one flyer/event photo, when a
photo is attached) from a Brazilian bar/club/event venue and extract the
event it announces.

## Critical rule about dates and times
Copy date and time EXACTLY AS WRITTEN OR SPOKEN on the flyer or in the
caption. Do NOT compute, normalize, or resolve a relative expression
yourself — "amanhã", "este sábado", "toda quinta", "15/08" and "22h" must all
be returned as the literal text you saw, character for character (aside from
trimming whitespace). A downstream deterministic resolver — not you — turns
that text into a real date, anchored to the post's own timestamp. Guessing or
computing a date yourself here would be invisible and wrong.

If no date or time appears anywhere, leave the corresponding field null.
Never invent one.

## Fields to extract
- title: the event's name/headline
- description: any additional descriptive text (optional)
- date_text: the raw date expression exactly as printed/spoken, or null
- time_text: the raw time expression exactly as printed/spoken (may be a
  range like "22h às 04h"), or null
- is_recurring: true only if the text explicitly states a recurring
  cadence ("toda quinta", "todo sábado", "semanalmente")
- recurrence_text: the raw recurrence phrase when is_recurring, else null
- lineup: array of performer/DJ names, empty array if none stated
- ticket_url: a ticketing link if present, else null
- price_text: price/cover text exactly as stated (e.g. "R$30 antecipado"),
  else null
- location_text: any venue/address text named in the post (do not resolve
  it, just copy it), else null
- confidence: your own 0.0-1.0 confidence that this post announces a real,
  identifiable event

## Output
Reply with ONLY a JSON object, no markdown fences:
{"title": "...", "description": null, "date_text": "15/08", "time_text": "22h",
 "is_recurring": false, "recurrence_text": null, "lineup": ["DJ X"],
 "ticket_url": null, "price_text": "R$30", "location_text": null,
 "confidence": 0.85}
"""


class EventExtractionParseError(ValueError):
    """Raised when the model's response is not the expected JSON object.

    The caller records the post as `extraction_failed` and keeps the raw text
    verbatim — nothing about this exception should ever be swallowed into a
    guessed event.
    """


def _strip_fences(raw_text: str) -> str:
    cleaned = (raw_text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    return re.sub(r"\s*```$", "", cleaned)


def parse_extraction_response(raw_text: str) -> dict:
    """Parse the model's raw JSON text into a normalized extraction dict.

    Pure and side-effect free so truncated/malformed/extra-field responses can
    be unit tested without a real API call. Raises EventExtractionParseError
    on anything that is not a usable JSON object — the caller decides what to
    do with a failure (record `extraction_failed`, never guess a result).
    """
    cleaned = _strip_fences(raw_text)
    if not cleaned:
        raise EventExtractionParseError("empty response")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise EventExtractionParseError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise EventExtractionParseError("response is not a JSON object")

    def _text(key: str) -> Optional[str]:
        value = data.get(key)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    confidence_raw = data.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    lineup_raw = data.get("lineup")
    lineup = [str(x) for x in lineup_raw] if isinstance(lineup_raw, list) else []

    return {
        "title": _text("title"),
        "description": _text("description"),
        "date_text": _text("date_text"),
        "time_text": _text("time_text"),
        "is_recurring": bool(data.get("is_recurring", False)),
        "recurrence_text": _text("recurrence_text"),
        "lineup": lineup,
        "ticket_url": _text("ticket_url"),
        "price_text": _text("price_text"),
        "location_text": _text("location_text"),
        "confidence": confidence,
    }


class OpenAIEventExtractionClient:
    """Async client: one post (caption + optional flyer image) per call."""

    def __init__(
        self, api_key: str, model: str = DEFAULT_MODEL,
        max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
    ):
        self.model = model
        self.max_completion_tokens = max_completion_tokens
        self.client = AsyncOpenAI(api_key=api_key)

    async def close(self):
        await self.client.close()

    async def extract(
        self, *, caption: Optional[str], image_data_uri: Optional[str] = None,
    ) -> str:
        """Returns the RAW response text. Parsing is a separate, pure step
        (parse_extraction_response) so a malformed reply can be recorded
        verbatim without losing the post."""
        content: list[dict] = [{
            "type": "text",
            "text": EXTRACTION_PROMPT + f"\n\nCaption:\n{caption or '(no caption)'}",
        }]
        if image_data_uri:
            content.append({
                "type": "image_url",
                "image_url": {"url": image_data_uri, "detail": "high"},
            })

        start = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                **sampling_kwargs(self.model, 0.1),
                max_completion_tokens=self.max_completion_tokens,
                response_format={"type": "json_object"},
            )
            duration = time.perf_counter() - start
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint=ENDPOINT).observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint=ENDPOINT, status="success").inc()
            if response.usage:
                OPENAI_TOKENS_TOTAL.labels(endpoint=ENDPOINT, direction="input").inc(
                    response.usage.prompt_tokens or 0
                )
                OPENAI_TOKENS_TOTAL.labels(endpoint=ENDPOINT, direction="output").inc(
                    response.usage.completion_tokens or 0
                )
            return response.choices[0].message.content or ""
        except Exception as e:
            duration = time.perf_counter() - start
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint=ENDPOINT).observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint=ENDPOINT, status="error").inc()
            logger.error(f"[OpenAIEventExtraction] call failed: {e}")
            raise


__all__ = [
    "OpenAIEventExtractionClient", "EventExtractionParseError",
    "parse_extraction_response", "DEFAULT_MODEL", "DEFAULT_MAX_COMPLETION_TOKENS",
]
