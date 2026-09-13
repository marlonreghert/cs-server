"""Unit tests for `app.services.event_venue_advisor_validator` —
plans/260913_dedup-agentic-mitigation-discovery.md Phase 3.

Division of labour against the sibling BDD feature
(`tests/bdd/enrichment/dedup-agentic-mitigation-discovery.feature`): the BDD
scenarios pin the USER-FACING behaviour in Gherkin ("the recommendation is
accepted" / "rejected, and the rejection reason records ..."); this file
pins the pure function's exact edge cases — both acceptance and every
hallucination shape named in the plan (a venue outside the candidate set, an
invented quote, a quote borrowed from a different candidate's own evidence)
— the same house style `tests/test_event_display_title.py` already
establishes for this pipeline's one other LLM-adjacent validator.
"""
from __future__ import annotations

from app.services.event_venue_advisor_validator import (
    REJECTION_EVIDENCE_NOT_VERBATIM,
    REJECTION_MALFORMED,
    REJECTION_VENUE_OUTSIDE_CANDIDATE_SET,
    VenueCandidate,
    validate_event_venue_recommendation,
)

# The corpus's own real "unrelated venue" case (see
# tests/fixtures/dedup_agentic_discovery/corpus.json,
# casa_bacurau_unrelated_venue_gap) reused here as a real-string acceptance
# fixture — the same "pin against the production strings" discipline
# test_event_display_title.py takes with _RODOLPHO/_FIVE_ACTS.
_CASA_BACURAU_CANDIDATES = [
    VenueCandidate(venue_id="vsg_b7996fe3895c7f6a5bbbbec0", venue_name="Casa Bacurau"),
    VenueCandidate(
        venue_id="ven_3875515066524b54446e5a52637771667775376e6564464a496843",
        venue_name="Clube Ferroviário de Afogados",
    ),
]
_CASA_BACURAU_LOCATION_TEXT = "Clube Ferroviário de Afogados"


class TestAcceptance:
    def test_accepts_a_venue_in_the_set_with_a_verbatim_quote(self):
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES,
            {
                "venue_id": "ven_3875515066524b54446e5a52637771667775376e6564464a496843",
                "evidence_quote": "Clube Ferroviário de Afogados",
            },
            location_text=_CASA_BACURAU_LOCATION_TEXT,
        )
        assert verdict.accepted is True
        assert verdict.venue_id == "ven_3875515066524b54446e5a52637771667775376e6564464a496843"
        assert verdict.rejection_reason is None

    def test_accepts_a_partial_verbatim_quote(self):
        """The evidence need not be the WHOLE location_text — any verbatim
        substring is real evidence, exactly like `validate_display_title`
        lets a model select/shorten what a post said."""
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES,
            {
                "venue_id": "ven_3875515066524b54446e5a52637771667775376e6564464a496843",
                "evidence_quote": "Ferroviário de Afogados",
            },
            location_text=_CASA_BACURAU_LOCATION_TEXT,
        )
        assert verdict.accepted is True

    def test_accepts_evidence_drawn_from_the_caption_when_location_text_is_absent(self):
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES,
            {
                "venue_id": "ven_3875515066524b54446e5a52637771667775376e6564464a496843",
                "evidence_quote": "hoje é no Clube Ferroviário de Afogados",
            },
            location_text=None,
            caption="E aí galera, hoje é no Clube Ferroviário de Afogados, não perca!",
        )
        assert verdict.accepted is True

    def test_accepts_the_single_venue_candidate_set(self):
        """A closed set of exactly one candidate is still a closed set —
        the same single-candidate posture `gate_auto_link` already takes
        for the resolution ladder."""
        verdict = validate_event_venue_recommendation(
            [VenueCandidate(venue_id="v1", venue_name="Only Venue")],
            {"venue_id": "v1", "evidence_quote": "at Only Venue tonight"},
            location_text="Come at Only Venue tonight",
        )
        assert verdict.accepted is True

    def test_accepts_plain_dict_candidates_not_just_the_dataclass(self):
        verdict = validate_event_venue_recommendation(
            [{"venue_id": "v1", "venue_name": "Only Venue"}],
            {"venue_id": "v1", "evidence_quote": "Only Venue"},
            location_text="Only Venue",
        )
        assert verdict.accepted is True


class TestRejectVenueOutsideCandidateSet:
    def test_rejects_a_venue_not_in_the_candidate_set(self):
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES,
            {"venue_id": "some-other-venue-id", "evidence_quote": "Clube Ferroviário de Afogados"},
            location_text=_CASA_BACURAU_LOCATION_TEXT,
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_VENUE_OUTSIDE_CANDIDATE_SET

    def test_rejects_against_an_empty_candidate_set(self):
        verdict = validate_event_venue_recommendation(
            [], {"venue_id": "v1", "evidence_quote": "anything"}, location_text="anything",
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_VENUE_OUTSIDE_CANDIDATE_SET


class TestRejectEvidenceNotVerbatim:
    def test_rejects_an_invented_quote_that_appears_nowhere(self):
        """The plain hallucination case: the quoted text does not appear in
        either source text at all."""
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES,
            {
                "venue_id": "ven_3875515066524b54446e5a52637771667775376e6564464a496843",
                "evidence_quote": "nosso point favorito em Afogados",
            },
            location_text=_CASA_BACURAU_LOCATION_TEXT,
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM

    def test_rejects_a_paraphrase_even_when_close(self):
        """Close is not verbatim — a model that reorders or rewords the
        real text must be rejected exactly like an invented string, never
        given partial credit for being almost right."""
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES,
            {
                "venue_id": "ven_3875515066524b54446e5a52637771667775376e6564464a496843",
                "evidence_quote": "no Clube dos Ferroviários de Afogados",
            },
            location_text=_CASA_BACURAU_LOCATION_TEXT,
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM

    def test_rejects_when_neither_location_text_nor_caption_is_given(self):
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES,
            {
                "venue_id": "ven_3875515066524b54446e5a52637771667775376e6564464a496843",
                "evidence_quote": "Clube Ferroviário de Afogados",
            },
            location_text=None, caption=None,
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM

    def test_rejects_evidence_borrowed_from_a_different_candidates_own_note(self):
        """The cross-contamination case (plan Phase 3, BDD: 'a model answer
        naming one candidate but quoting evidence that only appears in a
        note about a different candidate'). The candidate's OWN `evidence`
        annotation is never a source of truth for this check — only the
        EVENT's own location_text/caption is — so text that is real
        SOMEWHERE (a different candidate's ranking evidence) but was never
        actually written by this event's own post is rejected exactly like
        an invented quote."""
        candidates = [
            VenueCandidate(venue_id="v1", venue_name="Venue One"),
            VenueCandidate(venue_id="v2", venue_name="Venue Two"),
        ]
        # The model claims v1, quoting text that is real but lives only in
        # a NOTE about v2 (e.g. v2's own neighbourhood-match evidence) —
        # never in this event's own location_text.
        verdict = validate_event_venue_recommendation(
            candidates,
            {"venue_id": "v1", "evidence_quote": "mentioned near Venue Two's own address"},
            location_text="a plain post naming only Venue One",
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_EVIDENCE_NOT_VERBATIM


class TestMalformedRecommendation:
    def test_rejects_a_missing_venue_id(self):
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES,
            {"evidence_quote": "Clube Ferroviário de Afogados"},
            location_text=_CASA_BACURAU_LOCATION_TEXT,
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_MALFORMED

    def test_rejects_a_missing_evidence_quote(self):
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES,
            {"venue_id": "ven_3875515066524b54446e5a52637771667775376e6564464a496843"},
            location_text=_CASA_BACURAU_LOCATION_TEXT,
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_MALFORMED

    def test_rejects_a_null_venue_id_the_model_returns_when_unsure(self):
        """The prompt explicitly allows `venue_id: null` when nothing fits —
        this must be a clean rejection, never a crash."""
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES,
            {"venue_id": None, "evidence_quote": "Clube Ferroviário de Afogados"},
            location_text=_CASA_BACURAU_LOCATION_TEXT,
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_MALFORMED

    def test_rejects_none_entirely(self):
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES, None, location_text=_CASA_BACURAU_LOCATION_TEXT,
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_MALFORMED

    def test_rejects_a_non_dict_recommendation(self):
        verdict = validate_event_venue_recommendation(
            _CASA_BACURAU_CANDIDATES, "not a dict at all", location_text=_CASA_BACURAU_LOCATION_TEXT,
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_MALFORMED

    def test_never_raises_on_a_malformed_candidate_entry(self):
        """A candidate list with a junk entry (e.g. a bare string) must not
        crash this function — it simply contributes nothing to the
        candidate-id set."""
        verdict = validate_event_venue_recommendation(
            ["not-a-candidate", None, 42],
            {"venue_id": "v1", "evidence_quote": "anything"},
            location_text="anything",
        )
        assert verdict.accepted is False
        assert verdict.rejection_reason == REJECTION_VENUE_OUTSIDE_CANDIDATE_SET
