"""Unit tests for `app.services.venue_link_audit_reviewer_validator` —
plans/260914_agentic-venue-resolution-fallback.md §3.

Division of labour against the sibling BDD feature
(`tests/bdd/enrichment/agentic-venue-resolution-fallback.feature`): the BDD
scenarios pin the USER-FACING behaviour in Gherkin; this file pins the
deterministic gate's exact edge cases — EVERY rejection reason, the
precedence between them, and the two deliberate, documented properties of
the verbatim gate (case- and accent-sensitivity) that a well-meaning later
"normalise it a bit" edit would quietly destroy.

Strings are the real, re-verified production values the plan's §E table
carries — `conchittasbar`'s one-letter Teresa/Tereza difference and
`saladerebocorecife`'s accented street — never an invented string for a case
central to the design.
"""
from __future__ import annotations

import pytest

from app.services.venue_link_audit_reviewer_validator import (
    FIELD_NAME,
    FIELD_NONE,
    FIELD_STREET,
    REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE,
    REJECTION_EVIDENCE_NOT_VERBATIM,
    REJECTION_MALFORMED,
    REJECTION_MATCHED_FIELD_NOT_RECOGNISED,
    REJECTION_VERDICT_NOT_RECOGNISED,
    VERDICT_CONFIRM,
    VERDICT_CONTRADICT,
    VERDICT_INSUFFICIENT,
    ReviewAnswer,
    validate_venue_link_review,
)

# Real, re-verified (plan §E): the catalog street is "R. Imperatriz Teresa
# Cristina 218"; the handle's own posts spell it "Tereza" — one letter apart.
# The whole point of the pair, and the exact text the model is shown.
_CONCHITTAS_TEXT = "Rua da Imperatriz Tereza Cristina, 218"
# Real, re-verified (plan §E): `saladerebocorecife`'s own accented street.
_SALA_TEXT = "Sala de Reboco - R. Gregório Júnior, 264"
_TEXTS = [_CONCHITTAS_TEXT, _SALA_TEXT]


def _answer(**kwargs) -> dict:
    """The plain dict `parse_venue_link_review_response` produces — the shape
    the validator sees in production."""
    return {
        "verdict": kwargs.get("verdict"),
        "evidence_quote": kwargs.get("evidence_quote"),
        "matched_field": kwargs.get("matched_field"),
        "reason": kwargs.get("reason", "one short sentence"),
    }


# ── the accept path ─────────────────────────────────────────────────────────

def test_a_confirm_quoting_a_supplied_text_with_a_real_field_is_accepted():
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM, evidence_quote=_CONCHITTAS_TEXT,
                matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert result.accepted is True, result
    assert result.verdict == VERDICT_CONFIRM
    assert result.evidence_quote == _CONCHITTAS_TEXT
    assert result.matched_field == FIELD_STREET
    assert result.rejection_reason is None


def test_a_contradict_quoting_a_supplied_text_with_a_real_field_is_accepted():
    """`contradict` acts too — it keeps a pair flagged and annotates why —
    so it takes the identical accept path, not a softer one."""
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_CONTRADICT, evidence_quote="Rua Madre Rosa, 181",
                matched_field=FIELD_STREET),
        location_texts=["Rua Madre Rosa, 181 – Jd. São Paulo"],
    )
    assert result.accepted is True, result
    assert result.verdict == VERDICT_CONTRADICT


def test_a_substring_of_a_supplied_text_is_a_valid_quote():
    """The gate is `in`, not equality: a model quoting the decisive FRAGMENT
    of a longer text is copying, not remembering."""
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM, evidence_quote="Gregório Júnior, 264",
                matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert result.accepted is True, result


def test_a_review_answer_dataclass_is_accepted_as_well_as_a_dict():
    result = validate_venue_link_review(
        ReviewAnswer(verdict=VERDICT_CONFIRM, evidence_quote=_CONCHITTAS_TEXT,
                     matched_field=FIELD_STREET, reason="same street"),
        location_texts=_TEXTS,
    )
    assert result.accepted is True, result
    assert result.reason == "same street"


# ── `malformed` ─────────────────────────────────────────────────────────────

def test_none_is_malformed():
    """What `parse_venue_link_review_response` returns for an unparseable —
    or empty — model reply."""
    result = validate_venue_link_review(None, location_texts=_TEXTS)
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_MALFORMED


@pytest.mark.parametrize("payload", ["confirm", 42, ["confirm"], ("confirm",), 0.5, True])
def test_a_non_dict_non_answer_payload_is_malformed(payload):
    result = validate_venue_link_review(payload, location_texts=_TEXTS)
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_MALFORMED, payload


def test_a_dict_with_no_verdict_is_malformed():
    result = validate_venue_link_review(
        {"evidence_quote": _CONCHITTAS_TEXT, "matched_field": FIELD_STREET},
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_MALFORMED


def test_an_empty_verdict_string_is_malformed_not_verdict_not_recognised():
    """Plan §3 puts "a missing/empty required field" under `malformed`, and
    the implementation's `not parsed.verdict` check runs BEFORE the
    membership test — so "" is an ABSENT verdict, not an unrecognised one.
    Pinned because the two reasons drive different operator actions."""
    result = validate_venue_link_review(
        _answer(verdict="", evidence_quote=_CONCHITTAS_TEXT, matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_MALFORMED


def test_a_rejected_result_carries_nothing_actionable():
    """`ReviewVerdict`'s own docstring: the caller must never read `verdict`
    off a rejected result, so a rejection carries none of the model's
    fields — not even the ones that happened to be well-formed."""
    result = validate_venue_link_review(
        _answer(verdict="maybe", evidence_quote=_CONCHITTAS_TEXT, matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.verdict is None
    assert result.evidence_quote is None
    assert result.matched_field is None
    assert result.reason is None


# ── `verdict_not_recognised` ────────────────────────────────────────────────

def test_an_unrecognised_verdict_is_rejected():
    result = validate_venue_link_review(
        _answer(verdict="maybe", evidence_quote=_CONCHITTAS_TEXT, matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_VERDICT_NOT_RECOGNISED


def test_the_verdict_vocabulary_is_case_sensitive():
    """"CONFIRM" is not `confirm`. The prompt asks for the lowercase token
    and the gate takes it literally — a model shouting its answer is a
    prompt-adherence signal the operator should see as
    `verdict_not_recognised`, never silently normalised into an acting
    verdict that can close a flag."""
    result = validate_venue_link_review(
        _answer(verdict="CONFIRM", evidence_quote=_CONCHITTAS_TEXT, matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_VERDICT_NOT_RECOGNISED


# ── `matched_field_not_recognised` ──────────────────────────────────────────

@pytest.mark.parametrize("field", ["zip", "address", "Street", "STREET", "postal_code"])
def test_an_unrecognised_matched_field_is_rejected(field):
    """`address` is the trap worth naming: the venue record's own column is
    `venues.address`, so it is the plausible-looking word a model reaches
    for — but the gate's vocabulary is name/street/neighborhood/city/none,
    and an unrecognised field must not be mapped onto a recognised one."""
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM, evidence_quote=_CONCHITTAS_TEXT, matched_field=field),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_MATCHED_FIELD_NOT_RECOGNISED


def test_an_unrecognised_matched_field_is_rejected_on_insufficient_too():
    """A PRESENT value must be one we recognise even on the verdict that is
    allowed to carry none at all."""
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_INSUFFICIENT, evidence_quote=None, matched_field="zip"),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_MATCHED_FIELD_NOT_RECOGNISED


# ── `acting_verdict_without_evidence` ───────────────────────────────────────

@pytest.mark.parametrize("verdict", [VERDICT_CONFIRM, VERDICT_CONTRADICT])
def test_an_acting_verdict_without_a_quote_is_rejected(verdict):
    result = validate_venue_link_review(
        _answer(verdict=verdict, evidence_quote=None, matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE


@pytest.mark.parametrize("verdict", [VERDICT_CONFIRM, VERDICT_CONTRADICT])
def test_an_acting_verdict_with_an_empty_quote_is_rejected(verdict):
    result = validate_venue_link_review(
        _answer(verdict=verdict, evidence_quote="", matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE


@pytest.mark.parametrize("verdict", [VERDICT_CONFIRM, VERDICT_CONTRADICT])
def test_an_acting_verdict_with_matched_field_none_is_rejected(verdict):
    """Plan §3's own bullet: "a verdict that acts must name the field it
    acted on". `matched_field="none"` on an acting verdict is a
    contradiction in terms, not a soft answer."""
    result = validate_venue_link_review(
        _answer(verdict=verdict, evidence_quote=_CONCHITTAS_TEXT, matched_field=FIELD_NONE),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE


@pytest.mark.parametrize("verdict", [VERDICT_CONFIRM, VERDICT_CONTRADICT])
def test_an_acting_verdict_with_a_missing_matched_field_is_rejected(verdict):
    result = validate_venue_link_review(
        _answer(verdict=verdict, evidence_quote=_CONCHITTAS_TEXT, matched_field=None),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_ACTING_VERDICT_WITHOUT_EVIDENCE


# ── `evidence_not_verbatim` ─────────────────────────────────────────────────

def test_a_quote_absent_from_every_supplied_text_is_rejected():
    """A quote the model "remembered" rather than copied — including one
    quoting the VENUE RECORD back at us, which is never a valid haystack
    (the validator's own docstring: "Quoting the venue record back at us is
    not evidence ABOUT the venue record")."""
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM,
                evidence_quote="R. Imperatriz Teresa Cristina 218",  # the CATALOG spelling
                matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM


def test_the_verbatim_gate_is_case_sensitive():
    """DELIBERATE and documented (validator docstring: "Plain Python `in`,
    no normalisation, no casefold"). The supplied text spells it "Tereza";
    "TEREZA" is not in it. Measured 120/120 pass rate against real
    production answers (plan §F), so this strictness is known NOT to be the
    bottleneck — do not soften it to make a failing call pass."""
    shouted = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM, evidence_quote="TEREZA", matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert shouted.accepted is False
    assert shouted.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM
    # The same quote in the supplied text's own casing passes.
    copied = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM, evidence_quote="Tereza", matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert copied.accepted is True, copied


def test_the_verbatim_gate_is_accent_sensitive():
    """Also DELIBERATE: no accent-fold. `saladerebocorecife`'s street is
    "Gregório"; an unaccented "Gregorio" is a re-spelling, i.e. exactly the
    "remembered, not copied" failure this gate exists to catch."""
    unaccented = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM, evidence_quote="Gregorio", matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert unaccented.accepted is False
    assert unaccented.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM
    accented = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM, evidence_quote="Gregório", matched_field=FIELD_STREET),
        location_texts=_TEXTS,
    )
    assert accented.accepted is True, accented


def test_an_empty_location_texts_list_rejects_any_quoted_answer():
    """With nothing supplied there is no valid haystack at all, so every
    quote is unverifiable — never vacuously true."""
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM, evidence_quote=_CONCHITTAS_TEXT,
                matched_field=FIELD_STREET),
        location_texts=[],
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM


def test_a_quoted_insufficient_is_gated_too():
    """`insufficient` MAY carry no quote — but one it does carry is still
    checked. The exemption is from having to cite, not from honesty."""
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_INSUFFICIENT, evidence_quote="never supplied",
                matched_field=FIELD_NONE),
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM


def test_empty_and_none_supplied_texts_are_never_a_haystack():
    """`location_texts` carrying a blank entry must not make a quote
    verifiable — the falsy entries are dropped before the `in` test."""
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM, evidence_quote="anything",
                matched_field=FIELD_STREET),
        location_texts=["", None],
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM


# ── `insufficient`'s exemption ──────────────────────────────────────────────

def test_insufficient_may_carry_no_quote_and_no_matched_field():
    """Plan §3 / the validator docstring: "a model that genuinely cannot
    decide should not be forced to manufacture evidence in order to say
    so"."""
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_INSUFFICIENT, evidence_quote=None, matched_field=None),
        location_texts=_TEXTS,
    )
    assert result.accepted is True, result
    assert result.verdict == VERDICT_INSUFFICIENT
    assert result.evidence_quote is None
    assert result.matched_field is None


def test_insufficient_may_carry_matched_field_none():
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_INSUFFICIENT, evidence_quote=None, matched_field=FIELD_NONE),
        location_texts=_TEXTS,
    )
    assert result.accepted is True, result
    assert result.matched_field == FIELD_NONE


def test_insufficient_with_no_quote_is_accepted_even_with_no_supplied_texts():
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_INSUFFICIENT, evidence_quote=None, matched_field=None),
        location_texts=[],
    )
    assert result.accepted is True, result


# ── the never-raises contract ───────────────────────────────────────────────

_JUNK = [
    None, 0, 1, -1, 3.5, "", "   ", "confirm", b"confirm", [], {}, (), set(),
    [1, 2, 3], {"verdict": {}}, {"verdict": []}, {"verdict": 0},
    {"verdict": VERDICT_CONFIRM, "evidence_quote": 218, "matched_field": FIELD_STREET},
    {"verdict": VERDICT_CONFIRM, "evidence_quote": ["a"], "matched_field": FIELD_STREET},
    {"verdict": VERDICT_INSUFFICIENT, "evidence_quote": 5},
    {"verdict": VERDICT_CONFIRM, "matched_field": 7, "evidence_quote": _CONCHITTAS_TEXT},
    object(), Exception("boom"), ReviewAnswer(),
]


@pytest.mark.parametrize("junk", _JUNK)
@pytest.mark.parametrize("texts", [_TEXTS, [], None])
def test_the_validator_never_raises_whatever_the_model_returned(junk, texts):
    """The module's stated posture, and load-bearing: `VenueLinkAuditReviewer
    Service._review_one` calls this OUTSIDE its try/except, so an exception
    here would escape `run()` — breaking the service's own "never raises into
    its caller" contract and aborting a whole batch over one bad answer."""
    result = validate_venue_link_review(junk, location_texts=texts)
    assert result.accepted is False, (junk, texts)
    assert result.rejection_reason is not None


def test_a_non_string_evidence_quote_is_rejected_rather_than_crashing():
    """The specific junk shape that is genuinely reachable in production: the
    prompt asks for JSON, `response_format={"type": "json_object"}` enforces
    JSON, and `{"evidence_quote": 218}` is perfectly valid JSON — a model
    quoting a bare house number. `218 in "…, 218"` raises TypeError, so this
    must be caught as a malformed FIELD, not left to escape the service."""
    result = validate_venue_link_review(
        {"verdict": VERDICT_CONFIRM, "evidence_quote": 218, "matched_field": FIELD_STREET},
        location_texts=_TEXTS,
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_MALFORMED


def test_a_non_string_supplied_text_is_never_a_haystack():
    """Defensive mirror of the above on the caller's side of the gate."""
    result = validate_venue_link_review(
        _answer(verdict=VERDICT_CONFIRM, evidence_quote="218", matched_field=FIELD_NAME),
        location_texts=[218, None, {"text": "218"}],
    )
    assert result.accepted is False
    assert result.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM
