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
from app.models.taxonomy import TAXONOMY, validate_category_labels

logger = logging.getLogger(__name__)

ENDPOINT = "event_extract"
DEFAULT_MODEL = "gpt-5.6-luna"
# Reasoning tokens (gpt-5.6 is a reasoning model) count against
# max_completion_tokens, so this carries real headroom above a typical
# lineup+description payload rather than the ~1-2k a non-reasoning extractor
# would need. Bumped alongside the multi-event budgets below
# (plans/260808_event-ticket-info-and-attractions.md §E): this is a FLAT cap,
# not scaled by event count, and turning each performer into a four-field
# `attractions` object is a real per-entry token increase on TOP of the
# reasoning overhead a classification/vocabulary-mapping task adds.
DEFAULT_MAX_COMPLETION_TOKENS = 6144

# plans/260808_event-ticket-info-and-attractions.md §C: `attractions[].styles`
# is constrained to the SAME `taxonomy.musica` vocabulary the venue vibe
# profile already validates against (`validate_category_labels`) — quoted
# into both prompts below so the model is told the vocabulary up front
# rather than only finding out a label was dropped after the fact.
_MUSIC_STYLES = ", ".join(TAXONOMY["musica"])

# Shared by both prompts below (single-line, embedded via an f-string) so the
# two can never drift on what an `attractions` entry looks like — extending
# only one prompt is the exact half-fix the plan calls out.
_ATTRACTIONS_FIELD_DOC = f"""- attractions: array of every DJ, live act or performer named for this
  event, each as an object — NOT a separate "lineup" field; the performer
  list is derived from this array, so do not repeat it as its own field:
    - name: the act/DJ name or handle, exactly as written
    - type: "dj" for a DJ set, "live" for a live band/show, "other" when the
      text does not say which
    - stage: the stage/pista/room this act plays, exactly as written, or
      null when the flyer names only one stage or none at all
    - styles: array of this act's musical styles, using ONLY these labels:
      {_MUSIC_STYLES}. Omit any style not in this list. Empty array if no
      style is stated.
  Empty array if the post names no acts at all.
- ticket_info: any purchase reference that is NOT itself a URL — a bare
  emoji ticket label, "link na bio", "ingressos na bilheteria", a WhatsApp
  number, an @handle, a lote/deadline note — copied verbatim. Populate this
  ALONGSIDE ticket_url when the caption states both a link and a note;
  neither suppresses the other. Null when nothing about buying or entry is
  stated."""

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
""" + _ATTRACTIONS_FIELD_DOC + """
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
 "is_recurring": false, "recurrence_text": null,
 "attractions": [{"name": "DJ X", "type": "dj", "stage": null, "styles": ["House"]}],
 "ticket_url": null, "ticket_info": null, "price_text": "R$30", "location_text": null,
 "confidence": 0.85}
"""

# See plans/260806_multi-event-posts.md: a city-listings account packs
# several events at several different venues into ONE caption/carousel
# ("Quarta (05) com exibição dO Homem do Fraque Verde no Cinema São Luiz,
# Adilson Ramos no RioMar, Khrystal no Terra e muito mais"). The single-event
# prompt above keeps only the first and silently drops the rest. This prompt
# asks for EVERY distinct event and wraps them in a list — a single-event
# post still yields a list of one, so no account needs to be flagged as a
# special archetype.
MULTI_EVENT_EXTRACTION_PROMPT = """## Role
You read a single Instagram post (caption + one flyer/event photo, when a
photo is attached) from a Brazilian bar/club/event venue or city-listings
account. The post may announce ONE event or SEVERAL — a daily roundup
naming several different parties at several different venues is common.
Extract EVERY distinct event the post announces, however many there are.

## Critical rule about dates and times
Copy date and time EXACTLY AS WRITTEN OR SPOKEN on the flyer or in the
caption, for EACH event independently. Do NOT compute, normalize, or resolve
a relative expression yourself — "amanhã", "este sábado", "toda quinta",
"15/08" and "22h" must all be returned as the literal text you saw, character
for character (aside from trimming whitespace). A downstream deterministic
resolver — not you — turns that text into a real date, anchored to the
post's own timestamp, separately for each event. Guessing or computing a
date yourself here would be invisible and wrong.

If no date or time appears anywhere for a given event, leave the
corresponding field null for that event. Never invent one.

## Critical rule about venues
Each event may be at a DIFFERENT venue. Copy each event's own venue/address
text into ITS OWN `location_text`. If an event names no venue, leave
`location_text` null for THAT event — never reuse or guess another event's
venue for it.

## Fields to extract, PER EVENT
- title: the event's name/headline
- description: any additional descriptive text (optional)
- date_text: the raw date expression exactly as printed/spoken, or null
- time_text: the raw time expression exactly as printed/spoken (may be a
  range like "22h às 04h"), or null
- is_recurring: true only if the text explicitly states a recurring
  cadence ("toda quinta", "todo sábado", "semanalmente")
- recurrence_text: the raw recurrence phrase when is_recurring, else null
""" + _ATTRACTIONS_FIELD_DOC + """
- ticket_url: a ticketing link if present, else null
- price_text: price/cover text exactly as stated (e.g. "R$30 antecipado"),
  else null
- location_text: THIS event's own venue/address text, or null if none stated
- confidence: your own 0.0-1.0 confidence that this is a real, identifiable
  event

## Output
Reply with ONLY a JSON object, no markdown fences, wrapping every event in an
"events" array (a single-event post still returns a list of one):
{"events": [
  {"title": "...", "description": null, "date_text": "15/08", "time_text": "22h",
   "is_recurring": false, "recurrence_text": null,
   "attractions": [{"name": "DJ X", "type": "dj", "stage": null, "styles": ["House"]}],
   "ticket_url": null, "ticket_info": null, "price_text": "R$30", "location_text": "Venue A",
   "confidence": 0.85}
]}
"""

# Reasoning tokens (gpt-5.6 is a reasoning model) count against
# max_completion_tokens on TOP of the visible per-event JSON, and a
# multi-event roundup's output length scales with how many events it
# announces — a 17-event roundup is a far longer answer than a one-party
# flyer. docs/venue-retrieval-storage.md §4 already recorded a flat budget
# truncating a variable-length response and losing the WHOLE batch while
# reporting success; this budget scales with the configured per-post ceiling
# (`event_extraction_max_events_per_post`) rather than staying flat.
#
# Both bumped together for plans/260808_event-ticket-info-and-attractions.md
# (E): turning each performer from a bare string into a four-field
# `attractions` object is roughly a 4-6x per-entry token increase, and
# gpt-5.6-luna's invisible reasoning tokens (spent classifying dj-vs-live and
# mapping free text into the styles vocabulary) count against this SAME
# budget. MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS carries the bulk of the
# increase (it scales per event); MULTI_EVENT_BASE_COMPLETION_TOKENS only a
# modest one (reasoning overhead does not scale purely per event).
MULTI_EVENT_BASE_COMPLETION_TOKENS = 2048
MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS = 500


def compute_multi_event_max_completion_tokens(max_events: int) -> int:
    """Output token budget for a multi-event extraction call, scaled by the
    configured per-post ceiling — not by an actual event count, which is
    unknown before the call. At least one event's worth of headroom always
    applies, even if `max_events` is misconfigured to 0."""
    events = max(1, int(max_events or 1))
    return MULTI_EVENT_BASE_COMPLETION_TOKENS + MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS * events


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


# The coarse, event-scoped type vocabulary plans/260808_event-ticket-info-
# and-attractions.md §C deliberately chose OVER taxonomy.music_format:
# forcing "Show @x" into "Banda ao vivo" vs "Som ao vivo" makes the model
# guess a detail the caption never states. An attraction whose type is
# missing or unrecognised defaults to "other" — a coarser fact, never a
# reason to drop the whole entry (only a missing/blank `name` is malformed;
# see _normalize_attractions).
ATTRACTION_TYPES = ("dj", "live", "other")


def _normalize_attractions(raw) -> tuple[list[dict], int]:
    """One event's raw `attractions` value -> (normalized entries, malformed
    count). Mirrors parse_multi_event_extraction_response's own per-item
    isolation one level down: an entry that is not an object, or IS an
    object but has no usable `name` (the one field with no sane default —
    everything else coarsens instead of failing), is skipped and counted,
    never fatal to its siblings. `raw` not being a list at all (the field
    omitted, or the model returning something else entirely) is NOT
    malformed — it is read as "no attractions stated", the same posture
    `lineup` already takes for a non-list value.

    `type` unrecognised/absent -> "other" (never a reason to drop the
    entry). `stage` blank/whitespace-only -> None, never an empty string
    (event_field_is_absent-style emptiness, kept consistent for the union in
    app.services.event_reconciliation.union_attractions). `styles` runs
    through the SAME `validate_category_labels` the venue vibe profile uses
    against `taxonomy.musica` — an unlisted label is DROPPED, never stored,
    per the plan's explicit "an unvalidated label would silently corrupt the
    vocabulary the venue side depends on."
    """
    if not isinstance(raw, list):
        return [], 0

    attractions: list[dict] = []
    malformed = 0
    for item in raw:
        if not isinstance(item, dict):
            malformed += 1
            continue
        name_raw = item.get("name")
        name = str(name_raw).strip() if name_raw is not None else ""
        if not name:
            malformed += 1
            continue

        type_raw = item.get("type")
        attraction_type = type_raw if type_raw in ATTRACTION_TYPES else "other"

        stage_raw = item.get("stage")
        stage = str(stage_raw).strip() if stage_raw is not None else ""
        stage = stage or None

        styles_raw = item.get("styles")
        styles = [str(s) for s in styles_raw] if isinstance(styles_raw, list) else []
        styles = validate_category_labels("musica", styles)

        attractions.append({
            "name": name, "type": attraction_type, "stage": stage, "styles": styles,
        })
    return attractions, malformed


def _parse_event_fields(data: dict) -> tuple[dict, int]:
    """The per-event field normalization shared by the single-event and
    multi-event response shapes — one definition of "what a parsed event
    looks like", never two that can drift apart.

    Returns `(fields, malformed_attractions)` — the second element is this
    ONE event's own malformed-attraction count (see _normalize_attractions),
    for the caller to aggregate and report
    (EVENT_EXTRACTION_MALFORMED_ATTRACTIONS_TOTAL), the same convention
    parse_multi_event_extraction_response already uses for a malformed EVENT
    one level up.

    `lineup` is DERIVED from `attractions[].name`, in order, whenever the
    response carries a real `attractions` list (plans/260808_event-ticket-
    info-and-attractions.md §C) — the model is asked for attractions only
    (see _ATTRACTIONS_FIELD_DOC), never a separate "lineup" field, so one
    answer yields both representations with no duplicate question. A
    response with NO `attractions` list at all (every pre-existing BDD/unit
    fixture simulating the OLD single-list-of-names shape) falls back to
    reading a raw `lineup` field exactly as before — this keeps `lineup`'s
    own cross-post union behaviour (app.services.event_reconciliation.
    union_lineup) completely unchanged, per the plan's own non-goal.
    """

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

    attractions_raw = data.get("attractions")
    attractions, malformed_attractions = _normalize_attractions(attractions_raw)
    if isinstance(attractions_raw, list):
        lineup = [a["name"] for a in attractions]
    else:
        lineup_raw = data.get("lineup")
        lineup = [str(x) for x in lineup_raw] if isinstance(lineup_raw, list) else []

    fields = {
        "title": _text("title"),
        "description": _text("description"),
        "date_text": _text("date_text"),
        "time_text": _text("time_text"),
        "is_recurring": bool(data.get("is_recurring", False)),
        "recurrence_text": _text("recurrence_text"),
        "lineup": lineup,
        "attractions": attractions,
        "ticket_url": _text("ticket_url"),
        "ticket_info": _text("ticket_info"),
        "price_text": _text("price_text"),
        "location_text": _text("location_text"),
        "confidence": confidence,
    }
    return fields, malformed_attractions


def parse_extraction_response(raw_text: str) -> dict:
    """Parse the model's raw JSON text into a normalized extraction dict.

    Pure and side-effect free so truncated/malformed/extra-field responses can
    be unit tested without a real API call. Raises EventExtractionParseError
    on anything that is not a usable JSON object — the caller decides what to
    do with a failure (record `extraction_failed`, never guess a result).

    This is the SINGLE-event shape, used by the venue-owned extraction
    pipeline (app/services/event_extraction_service.py) and the confirmed-
    event re-extraction path — both of which only ever need one event per
    post. See parse_multi_event_extraction_response for the promoter-post
    "several events in one caption" shape (plans/260806_multi-event-posts.md).

    Drops the per-event malformed-attraction count `_parse_event_fields`
    returns: this function has no metrics-aggregating caller today (both
    real runtime paths call the multi-event shape below), so there is
    nothing to report it to.
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
    fields, _malformed_attractions = _parse_event_fields(data)
    return fields


def parse_multi_event_extraction_response(
    raw_text: str, *, max_events: Optional[int] = None,
) -> tuple[list[dict], int, int]:
    """Parse a `{"events": [...]}` response into (parsed_events,
    malformed_event_count, malformed_attraction_count).

    A post can announce several events at several venues (plans/260806_multi-
    event-posts.md) — a city-listings roundup, not a single-party flyer. Each
    entry in `events` is normalized the SAME way a single-event response is
    (`_parse_event_fields`). An individual entry that is not a JSON object is
    MALFORMED and skipped, counted, never fatal to its siblings — the same
    per-item failure isolation the pipeline already applies per venue.
    `malformed_attraction_count` is the SUM of each surviving event's own
    malformed-attraction count (plans/260808_event-ticket-info-and-
    attractions.md) — one level down: a defect inside one event's
    `attractions` list, never fatal to that event or its siblings either.

    Raises EventExtractionParseError only when the response ITSELF is
    unusable (empty, invalid JSON, not an object, or missing/non-list
    "events") — never for one bad item inside an otherwise-valid list.

    `max_events` caps how many RAW entries are even considered — the sanity
    bound (`event_extraction_max_events_per_post`); entries beyond it are
    silently dropped, not counted as malformed (they are not defective, just
    over the configured ceiling).
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

    events_raw = data.get("events")
    if not isinstance(events_raw, list):
        raise EventExtractionParseError("response has no 'events' list")
    if max_events:
        events_raw = events_raw[:max_events]

    events: list[dict] = []
    malformed = 0
    malformed_attractions = 0
    for item in events_raw:
        if not isinstance(item, dict):
            malformed += 1
            continue
        fields, item_malformed_attractions = _parse_event_fields(item)
        events.append(fields)
        malformed_attractions += item_malformed_attractions
    return events, malformed, malformed_attractions


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

    async def extract_events(
        self, *, caption: Optional[str], image_data_uri: Optional[str] = None,
        max_events: int,
    ) -> tuple[str, bool]:
        """Multi-event extraction: returns (raw_response_text, truncated).

        `truncated` is read from the API's OWN `finish_reason == "length"` —
        never guessed from the output's shape, because a well-formed-looking
        JSON string can still be a value cut off mid-field. See
        plans/260806_multi-event-posts.md: on truncation the caller must
        persist NOTHING partial and record the post as `truncated`, a
        distinct outcome from `extraction_failed` (a truncated response means
        the budget is too small, a different fix from a model error).

        The output budget scales with `max_events` (the configured sanity
        bound), not a runtime event count — the count is unknown before the
        call completes.
        """
        content: list[dict] = [{
            "type": "text",
            "text": MULTI_EVENT_EXTRACTION_PROMPT + f"\n\nCaption:\n{caption or '(no caption)'}",
        }]
        if image_data_uri:
            content.append({
                "type": "image_url",
                "image_url": {"url": image_data_uri, "detail": "high"},
            })

        max_completion_tokens = compute_multi_event_max_completion_tokens(max_events)
        start = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": content}],
                **sampling_kwargs(self.model, 0.1),
                max_completion_tokens=max_completion_tokens,
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
            choice = response.choices[0]
            truncated = choice.finish_reason == "length"
            return choice.message.content or "", truncated
        except Exception as e:
            duration = time.perf_counter() - start
            OPENAI_API_CALL_DURATION_SECONDS.labels(endpoint=ENDPOINT).observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(endpoint=ENDPOINT, status="error").inc()
            logger.error(f"[OpenAIEventExtraction] multi-event call failed: {e}")
            raise


__all__ = [
    "OpenAIEventExtractionClient", "EventExtractionParseError",
    "parse_extraction_response", "parse_multi_event_extraction_response",
    "compute_multi_event_max_completion_tokens",
    "DEFAULT_MODEL", "DEFAULT_MAX_COMPLETION_TOKENS",
    "MULTI_EVENT_BASE_COMPLETION_TOKENS", "MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS",
    "ATTRACTION_TYPES", "EXTRACTION_PROMPT", "MULTI_EVENT_EXTRACTION_PROMPT",
]
