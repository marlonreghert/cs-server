"""One text-only model call asking whether a handle's own posts support the
venue it is mapped to — plans/260914_agentic-venue-resolution-fallback.md.

## Why this is its own module and not a method on OpenAIEventExtractionClient

Every other prompt in this pipeline lives on that client, and that would
normally be the house location for this one too. It is separate for two
reasons, one process and one substantive:

- The advisor's own `EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS` bug (plan §F)
  is being fixed in that file, concurrently, as its own independent change.
  Two edits to one file across two in-flight branches is an avoidable
  coordination cost for zero benefit.
- More importantly, the two calls have genuinely different budgets, and that
  difference is the whole point. The advisor's 200-token cap is the bug; this
  module's cap is a MEASURED value (below). Housing them together invites
  exactly the "make them consistent" tidy-up that would reintroduce the bug.

If the two are ever merged back, keep the two budgets separate.

## The token budget is measured, not guessed

`gpt-5.6-luna` is a reasoning model: its invisible reasoning tokens count
against `max_completion_tokens`. Measured live on 2026-09-14 over the same
12 production pairs, 120 calls per setting:

| max_completion_tokens | empty responses |
|---|---|
| 400  | **9 / 120 (7.5%)** |
| 3000 | **0 / 120** |

An empty body parses to `None` and reads downstream as a malformed answer.
Do not lower this without re-measuring; a "reasonable-looking" smaller number
is precisely how the advisor acquired its own defect.

## This module never decides whether an answer is trustworthy

It makes the call and counts it, returning the RAW response text. Parsing
(`venue_link_audit_reviewer.parse_venue_link_review_response`) and the
deterministic gate (`venue_link_audit_reviewer_validator.
validate_venue_link_review`) are the caller's — the same posture every
method on `OpenAIEventExtractionClient` already takes.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from openai import AsyncOpenAI

from app.api.openai_compat import sampling_kwargs
from app.metrics import (
    OPENAI_API_CALL_DURATION_SECONDS,
    OPENAI_API_CALLS_TOTAL,
    OPENAI_TOKENS_TOTAL,
)

logger = logging.getLogger(__name__)

# Its OWN endpoint label, never `event_venue_advisor` — plan's observability
# section: the two passes' spend must never merge, and this one makes K calls
# per pair where the advisor makes one per event.
ENDPOINT_VENUE_LINK_REVIEW = "venue_link_review"

# See the module docstring. MEASURED, not guessed.
VENUE_LINK_REVIEW_MAX_COMPLETION_TOKENS = 3000

VENUE_LINK_REVIEW_PROMPT = """You are auditing whether an Instagram account's own posts support the venue that account is currently mapped to in a venue catalog.

You are given the catalog record for ONE venue under review, and location texts extracted from that account's posts. The texts are split into ones a deterministic text check already matched to this venue, and ones it could not match (those non-matches are why this pair was flagged for review).

Decide ONE verdict about the MAPPING as a whole:
- "confirm": the post evidence refers to this same venue. Differences in spelling, accents, abbreviation, article words, casing or word order are still the same place. A post giving this venue's own street address confirms it even if it names the venue differently or not at all.
- "contradict": the post evidence refers to a DIFFERENT place - a different street, a different neighbourhood, or a clearly different named establishment that is not this one.
- "insufficient": the texts do not settle it either way, or different texts point to genuinely different places so no single venue is supported.

Rules:
- Judge ONLY from the text given below. Do not use outside knowledge about any of these places.
- evidence_quote must be copied VERBATIM (character for character) from ONE of the location texts listed below. Never paraphrase, summarise or invent it.
- matched_field must be exactly one of: name, street, neighborhood, city, none.
- You are judging ONLY the venue shown under "Venue under review". Other venues mapped to the same account are listed for context only; never answer about them.

Answer with JSON only: {"verdict": "...", "evidence_quote": "...", "matched_field": "...", "reason": "<one short sentence>"}"""

# Bounds on what reaches the prompt. A handle's text volume varies by two
# orders of magnitude (3 to 69 events, measured); these caps keep one pair's
# prompt — and therefore K times its cost — bounded regardless. The texts are
# already de-duplicated by `build_review_evidence`, so a cap bites only on
# genuinely distinct spellings.
MAX_CORROBORATING_TEXTS = 6
MAX_NON_CORROBORATING_TEXTS = 12


def build_venue_link_review_prompt(evidence) -> str:
    """The whole user message for one pair — TEXT ONLY: no image, no S3 read,
    no presigned URL. Everything shown here is already stored.

    `evidence` is a `venue_link_audit_reviewer.ReviewEvidence`. The corroborating
    texts are included deliberately, not just the flagged ones: showing only
    the non-matches would bias the model toward `contradict` on a handle that
    overwhelmingly posts about its own venue and occasionally plays a gig
    elsewhere — the real `saladerebocorecife` shape (15 corroborating, 2
    off-site)."""
    lines = [VENUE_LINK_REVIEW_PROMPT, "", "Venue under review:"]
    lines.append(f"- name: {evidence.venue_name or '(unknown)'}")
    lines.append(f"- street: {evidence.street or '(unknown)'}")
    lines.append(f"- neighborhood: {evidence.neighborhood or '(unknown)'}")
    lines.append(f"- city: {evidence.city or '(unknown)'}")

    if evidence.siblings:
        lines.append("")
        lines.append("Other venues also mapped to this account (context only):")
        for sibling in evidence.siblings:
            lines.append(
                f"- name: {sibling.get('venue_name') or '(unknown)'}, "
                f"street: {sibling.get('street') or '(unknown)'}, "
                f"neighborhood: {sibling.get('neighborhood') or '(unknown)'}"
            )

    corroborating = list(evidence.corroborating)[:MAX_CORROBORATING_TEXTS]
    lines.append("")
    lines.append(
        f"Location texts that ALREADY matched this venue ({len(evidence.corroborating)} total):"
    )
    for text in corroborating or ["(none)"]:
        lines.append(f"- {text}")

    non_corroborating = list(evidence.non_corroborating)[:MAX_NON_CORROBORATING_TEXTS]
    lines.append("")
    lines.append(
        f"Location texts that did NOT match this venue "
        f"({len(evidence.non_corroborating)} total) - the reason this pair was flagged:"
    )
    for text in non_corroborating or ["(none)"]:
        lines.append(f"- {text}")
    return "\n".join(lines)


class VenueLinkReviewClient:
    """Async client: one review call per invocation. The caller makes K of
    them per pair and tallies the verdicts itself — consensus is a rule, and
    every rule in this feature lives in the pure service core, never here."""

    def __init__(
        self, api_key: str, model: str,
        max_completion_tokens: int = VENUE_LINK_REVIEW_MAX_COMPLETION_TOKENS,
    ):
        self.model = model
        self.max_completion_tokens = max_completion_tokens
        self.client = AsyncOpenAI(api_key=api_key)

    async def close(self) -> None:
        await self.client.close()

    async def review_venue_link(self, *, evidence) -> str:
        """Returns the RAW response text (possibly empty — the caller counts
        that case under its own outcome; see the module docstring). Raises on
        an API failure; the service catches and counts it."""
        prompt = build_venue_link_review_prompt(evidence)
        start = time.perf_counter()
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                **sampling_kwargs(self.model, 0.1),
                max_completion_tokens=self.max_completion_tokens,
                response_format={"type": "json_object"},
            )
            duration = time.perf_counter() - start
            OPENAI_API_CALL_DURATION_SECONDS.labels(
                endpoint=ENDPOINT_VENUE_LINK_REVIEW).observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(
                endpoint=ENDPOINT_VENUE_LINK_REVIEW, status="success").inc()
            if response.usage:
                OPENAI_TOKENS_TOTAL.labels(
                    endpoint=ENDPOINT_VENUE_LINK_REVIEW, direction="input",
                ).inc(response.usage.prompt_tokens or 0)
                OPENAI_TOKENS_TOTAL.labels(
                    endpoint=ENDPOINT_VENUE_LINK_REVIEW, direction="output",
                ).inc(response.usage.completion_tokens or 0)
            return response.choices[0].message.content or ""
        except Exception as e:
            duration = time.perf_counter() - start
            OPENAI_API_CALL_DURATION_SECONDS.labels(
                endpoint=ENDPOINT_VENUE_LINK_REVIEW).observe(duration)
            OPENAI_API_CALLS_TOTAL.labels(
                endpoint=ENDPOINT_VENUE_LINK_REVIEW, status="error").inc()
            logger.error(f"[VenueLinkReview] review call failed: {e}")
            raise


__all__ = [
    "ENDPOINT_VENUE_LINK_REVIEW", "VENUE_LINK_REVIEW_MAX_COMPLETION_TOKENS",
    "VENUE_LINK_REVIEW_PROMPT", "MAX_CORROBORATING_TEXTS",
    "MAX_NON_CORROBORATING_TEXTS",
    "build_venue_link_review_prompt", "VenueLinkReviewClient",
]
