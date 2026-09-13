"""The closed-candidate-set + verbatim-evidence gate for an LLM's event-venue
recommendation. See plans/260913_dedup-agentic-mitigation-discovery.md
Phase 3.

## Why this exists before anything calls it

`app/services/event_display_title.py` is the one existing precedent for an
LLM anywhere in this pipeline, and its own docstring states the rule this
module generalizes: a model answer is safe to accept only when a
DETERMINISTIC gate proves it could not have invented anything — there,
"the returned title's distinctive token set must be a SUBSET OF THE UNION of
the group's own source titles"; here, the analogous two-part proof for a
venue RECOMMENDATION rather than a title:

  1. **Closed candidate-set membership.** The recommended `venue_id` must be
     one of the candidates the DETERMINISTIC resolution ladder
     (`event_venue_resolution.resolve_event_venue`) already computed and
     ranked for this event — the same `event_venue_link_candidate` row shape.
     A model cannot invent a venue the ladder never considered; it may only
     pick among candidates the deterministic code already vetted.
  2. **Verbatim evidence.** The quoted `evidence_quote` must appear,
     character-for-character, inside the event's OWN `location_text` or
     `caption` — never a paraphrase, never text borrowed from a candidate's
     own ranking evidence (`event_venue_link_candidate.evidence`, e.g. a
     neighbourhood-match's matched address fragment), which is annotation
     ABOUT a candidate, not something the event's own post ever said.
     Requiring the substring check to run only against the event's real
     source text is what rejects a cross-contaminated quote: text that is
     real (it exists somewhere in this event's candidate data) but was never
     actually written by the post the event came from is not verbatim
     evidence for this event, and does not pass here just because it is a
     real string from *somewhere*.

Both checks must pass. Either failing is a REJECTION, never a weakened
accept — matching `validate_display_title`'s own "on ANY failure ... nothing
is written" posture.

## What this module deliberately does not do

No DAO access, no Redis, no OpenAI client, no admin-config read. This is a
pure function over plain data, exactly like `app.services.event_dedup`'s own
module docstring rule ("every function here is PURE"). It has NO caller in
this plan (Phase 3's own scope: "built and proven, not wired") — Phase 4,
conditional on Phase 2's measured numbers, is the only thing that would ever
call it in production, and even then only behind its own admin-config gate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ── rejection reasons (Error Handling: "logs every accept/reject with its
# reason (candidate-set miss vs. evidence-not-verbatim)") ───────────────────
REJECTION_MALFORMED = "malformed"
REJECTION_VENUE_OUTSIDE_CANDIDATE_SET = "venue_outside_candidate_set"
REJECTION_EVIDENCE_NOT_VERBATIM = "evidence_not_verbatim"


@dataclass(frozen=True)
class VenueCandidate:
    """The slice of an `event_venue_link_candidate` row this gate needs —
    nothing more. `venue_name` is carried for logging/debugging only; the
    membership check is on `venue_id` alone, an opaque key, never a fuzzy
    name comparison (the exact failure mode `event_venue_resolution.py`'s
    rung 4 already teaches this codebase to avoid)."""

    venue_id: str
    venue_name: Optional[str] = None


@dataclass(frozen=True)
class AdvisorRecommendation:
    """The raw shape a model answer is expected to parse into:
    `{"venue_id": str, "evidence_quote": str}`. Both fields are required —
    a recommendation naming a venue with no quoted evidence is exactly as
    unverifiable as one quoting evidence with no named venue, so neither
    field gets a free pass."""

    venue_id: Optional[str] = None
    evidence_quote: Optional[str] = None


@dataclass(frozen=True)
class ValidationVerdict:
    accepted: bool
    venue_id: Optional[str] = None
    evidence_quote: Optional[str] = None
    # None when accepted; one of the REJECTION_* constants above otherwise.
    rejection_reason: Optional[str] = None


def _as_recommendation(raw) -> Optional[AdvisorRecommendation]:
    """Accepts either an `AdvisorRecommendation` or the plain
    `{"venue_id": ..., "evidence_quote": ...}` dict a parsed model JSON
    answer would actually hand this function — mirroring
    `event_display_title.parse_display_title_response`'s posture of being
    forgiving of SHAPE but not of content. Returns `None` for anything that
    cannot even be read as a recommendation at all."""
    if isinstance(raw, AdvisorRecommendation):
        return raw
    if isinstance(raw, dict):
        return AdvisorRecommendation(
            venue_id=raw.get("venue_id"), evidence_quote=raw.get("evidence_quote"),
        )
    return None


def validate_event_venue_recommendation(
    candidates: list,
    recommendation,
    *,
    location_text: Optional[str] = None,
    caption: Optional[str] = None,
) -> ValidationVerdict:
    """The whole gate. `candidates` is the event's own closed candidate set —
    a list of `VenueCandidate` (or plain dicts with `venue_id`/`venue_name`
    keys, accepted the same forgiving way `_as_recommendation` reads a
    recommendation). `recommendation` is the model's raw answer. Returns a
    `ValidationVerdict` — `accepted=False` always carries a
    `rejection_reason`, `accepted=True` never does.

    Never raises: a malformed candidate list or recommendation is a
    rejection, exactly like `event_display_title.validate_display_title`
    turns a malformed model answer into "write nothing" rather than an
    exception a caller would have to remember to catch.
    """
    rec = _as_recommendation(recommendation)
    if rec is None or not rec.venue_id or not rec.evidence_quote:
        logger.info(
            "[EventVenueAdvisorValidator] rejected: malformed recommendation"
        )
        return ValidationVerdict(accepted=False, rejection_reason=REJECTION_MALFORMED)

    candidate_ids = set()
    for c in candidates or ():
        if isinstance(c, VenueCandidate):
            candidate_ids.add(c.venue_id)
        elif isinstance(c, dict):
            venue_id = c.get("venue_id")
            if venue_id:
                candidate_ids.add(venue_id)

    if rec.venue_id not in candidate_ids:
        logger.info(
            "[EventVenueAdvisorValidator] rejected: venue_id %r is outside the "
            "closed candidate set (%d candidates)", rec.venue_id, len(candidate_ids),
        )
        return ValidationVerdict(
            accepted=False, venue_id=rec.venue_id, evidence_quote=rec.evidence_quote,
            rejection_reason=REJECTION_VENUE_OUTSIDE_CANDIDATE_SET,
        )

    # Verbatim-substring check against the EVENT'S OWN source text only —
    # never against a candidate's `evidence` field (a candidate's own
    # ranking annotation, not something the post itself said) and never a
    # normalised/fuzzy comparison. This is what rejects both the plain
    # hallucination case (invented text, matches nothing) and the
    # cross-contamination case (real text, but never written by THIS
    # event's own post).
    source_texts = [t for t in (location_text, caption) if t]
    verbatim = any(rec.evidence_quote in text for text in source_texts)
    if not verbatim:
        logger.info(
            "[EventVenueAdvisorValidator] rejected: evidence quote is not a "
            "verbatim substring of the event's own location_text/caption"
        )
        return ValidationVerdict(
            accepted=False, venue_id=rec.venue_id, evidence_quote=rec.evidence_quote,
            rejection_reason=REJECTION_EVIDENCE_NOT_VERBATIM,
        )

    logger.info(
        "[EventVenueAdvisorValidator] accepted: venue_id=%s", rec.venue_id,
    )
    return ValidationVerdict(
        accepted=True, venue_id=rec.venue_id, evidence_quote=rec.evidence_quote,
    )


__all__ = [
    "REJECTION_MALFORMED", "REJECTION_VENUE_OUTSIDE_CANDIDATE_SET",
    "REJECTION_EVIDENCE_NOT_VERBATIM",
    "VenueCandidate", "AdvisorRecommendation", "ValidationVerdict",
    "validate_event_venue_recommendation",
]
