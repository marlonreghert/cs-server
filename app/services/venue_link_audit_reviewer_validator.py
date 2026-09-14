"""The deterministic gate on one model answer about one flagged
`(handle, venue)` pair — plans/260914_agentic-venue-resolution-fallback.md §3.

PURE: no DAO, no Redis, no OpenAI client, no admin-config read. Never
raises. Mirrors `app.services.event_venue_advisor_validator`'s posture
exactly — frozen dataclasses, rejection reasons as constants, and a raw
model payload that is never logged — because this module gates a pass that
can SUPPRESS an audit flag, which is a strictly larger authority than that
module's advisory annotation.

## The verbatim-evidence gate is unchanged, and stays strict

`evidence_quote` must be a substring of one of the `location_text` values
actually shown to the model. Plain Python `in`, no normalisation, no
casefold, no accent-fold, no fuzzy ratio — identical to
`validate_event_venue_recommendation`'s own test, for the identical reason:
a quote the model "remembered" rather than copied is the exact failure this
gate exists to catch.

Measured before it was written (plan §F): across 120 live calls against all
12 currently-flagged production pairs, **120/120** answers quoted a span
present character-for-character in a supplied text. This gate is therefore
known not to be the bottleneck, which is why it is not softened here.

## An acting verdict must name the field it acted on

`confirm` and `contradict` both CHANGE what an operator sees, so both must
carry a quote and a real `matched_field`. `insufficient` changes nothing and
is allowed to carry neither — a model that genuinely cannot decide should
not be forced to manufacture evidence in order to say so.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

VERDICT_CONFIRM = "confirm"
VERDICT_CONTRADICT = "contradict"
VERDICT_INSUFFICIENT = "insufficient"
VERDICTS = (VERDICT_CONFIRM, VERDICT_CONTRADICT, VERDICT_INSUFFICIENT)
# The two that change what an operator sees, and therefore must cite
# evidence. See the module docstring.
ACTING_VERDICTS = (VERDICT_CONFIRM, VERDICT_CONTRADICT)

FIELD_NAME = "name"
FIELD_STREET = "street"
FIELD_NEIGHBORHOOD = "neighborhood"
FIELD_CITY = "city"
FIELD_NONE = "none"
MATCHED_FIELDS = (FIELD_NAME, FIELD_STREET, FIELD_NEIGHBORHOOD, FIELD_CITY, FIELD_NONE)

REJECTION_MALFORMED = "malformed"
REJECTION_VERDICT_NOT_RECOGNISED = "verdict_not_recognised"
REJECTION_MATCHED_FIELD_NOT_RECOGNISED = "matched_field_not_recognised"
REJECTION_EVIDENCE_NOT_VERBATIM = "evidence_not_verbatim"
REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE = "acting_verdict_without_evidence"


@dataclass(frozen=True)
class ReviewAnswer:
    """One parsed model answer. Deliberately mirrors the JSON shape the
    prompt asks for, one field per key, so a malformed reply fails at the
    boundary rather than halfway through the service."""

    verdict: Optional[str] = None
    evidence_quote: Optional[str] = None
    matched_field: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class ReviewVerdict:
    """`accepted=False` carries `rejection_reason` and nothing actionable —
    the caller must never read `verdict` off a rejected result."""

    accepted: bool
    verdict: Optional[str] = None
    evidence_quote: Optional[str] = None
    matched_field: Optional[str] = None
    reason: Optional[str] = None
    rejection_reason: Optional[str] = None


def _as_answer(answer) -> Optional[ReviewAnswer]:
    """Accept a `ReviewAnswer` or the plain dict `parse_*` produces; anything
    else is malformed. Mirrors `event_venue_advisor_validator._as_
    recommendation`'s own forgiving-of-shape / never-of-content posture."""
    if isinstance(answer, ReviewAnswer):
        return answer
    if isinstance(answer, dict):
        return ReviewAnswer(
            verdict=answer.get("verdict"),
            evidence_quote=answer.get("evidence_quote"),
            matched_field=answer.get("matched_field"),
            reason=answer.get("reason"),
        )
    return None


def _reject(reason: str) -> ReviewVerdict:
    # Log the rejection REASON, never the raw model payload — the same
    # discipline event_venue_advisor_validator already holds.
    logger.info("[VenueLinkAuditReviewValidator] rejected answer: %s", reason)
    return ReviewVerdict(accepted=False, rejection_reason=reason)


def validate_venue_link_review(answer, *, location_texts) -> ReviewVerdict:
    """Gate one model answer against the texts it was actually shown.

    `location_texts` is every `location_text` supplied in the prompt — both
    the corroborating and the non-corroborating ones. Nothing else is a
    valid haystack: not the venue's own catalogue fields, not a sibling
    venue's, not a candidate row's stored `evidence`. Quoting the venue
    record back at us is not evidence ABOUT the venue record."""
    parsed = _as_answer(answer)
    if parsed is None or not parsed.verdict:
        return _reject(REJECTION_MALFORMED)

    if parsed.verdict not in VERDICTS:
        return _reject(REJECTION_VERDICT_NOT_RECOGNISED)

    # `None` is tolerated only for `insufficient`, which is checked below;
    # any PRESENT value must still be one we recognise.
    if parsed.matched_field is not None and parsed.matched_field not in MATCHED_FIELDS:
        return _reject(REJECTION_MATCHED_FIELD_NOT_RECOGNISED)

    if parsed.verdict in ACTING_VERDICTS:
        # A verdict that changes what an operator sees must name both the
        # quote and the field it acted on. `matched_field="none"` on an
        # acting verdict is a contradiction in terms, not a soft answer.
        if not parsed.evidence_quote:
            return _reject(REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE)
        if not parsed.matched_field or parsed.matched_field == FIELD_NONE:
            return _reject(REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE)

    if parsed.evidence_quote:
        # A quote that is not a STRING cannot be compared against the
        # supplied texts at all — `218 in "…, 218"` raises TypeError, not a
        # rejection. Genuinely reachable: the prompt asks for JSON and
        # `response_format={"type": "json_object"}` enforces it, so
        # `{"evidence_quote": 218}` (a model quoting a bare house number) is
        # a perfectly valid reply. It is a malformed FIELD, and it must be
        # REJECTED rather than raised: `VenueLinkAuditReviewerService.
        # _review_one` calls this outside its try/except, so an exception
        # here would escape `run()` and abort a whole batch over one bad
        # answer — breaking both this module's "never raises" posture and
        # the service's own "never raises into its caller" contract.
        if not isinstance(parsed.evidence_quote, str):
            return _reject(REJECTION_MALFORMED)
        # Same reasoning on the caller's side of the gate: a non-string
        # entry is not a haystack, it is not searchable.
        texts = [t for t in (location_texts or []) if t and isinstance(t, str)]
        if not any(parsed.evidence_quote in t for t in texts):
            return _reject(REJECTION_EVIDENCE_NOT_VERBATIM)

    return ReviewVerdict(
        accepted=True,
        verdict=parsed.verdict,
        evidence_quote=parsed.evidence_quote,
        matched_field=parsed.matched_field,
        reason=parsed.reason,
    )


__all__ = [
    "VERDICT_CONFIRM", "VERDICT_CONTRADICT", "VERDICT_INSUFFICIENT",
    "VERDICTS", "ACTING_VERDICTS",
    "FIELD_NAME", "FIELD_STREET", "FIELD_NEIGHBORHOOD", "FIELD_CITY",
    "FIELD_NONE", "MATCHED_FIELDS",
    "REJECTION_MALFORMED", "REJECTION_VERDICT_NOT_RECOGNISED",
    "REJECTION_MATCHED_FIELD_NOT_RECOGNISED", "REJECTION_EVIDENCE_NOT_VERBATIM",
    "REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE",
    "ReviewAnswer", "ReviewVerdict", "validate_venue_link_review",
]
