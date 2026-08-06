"""Unit coverage for the promoter-event resolution ladder
(app/services/event_venue_resolution.py). See
plans/260804_instagram-promoter-events.md §D.

BDD (tests/bdd/enrichment/instagram-promoter-events.feature) proves the
ladder end-to-end through the real crawl pipeline; these tests isolate the
pure decision logic — the ordering, the two gates, and the lower-level
helpers — so a regression in the arithmetic fails fast and close to the bug.
"""
import pytest

from app.services.event_venue_resolution import (
    DEFAULT_CONFIDENCE_FLOOR,
    METHOD_HANDLE_MENTION,
    METHOD_LOCATION_TAG,
    METHOD_NAME_MATCH,
    RESOLUTION_AUTO,
    RESOLUTION_QUEUED,
    RESOLUTION_UNRESOLVED,
    LinkCandidate,
    VenueLite,
    extract_mentions,
    gate_auto_link,
    resolve_event_venue,
)


def _venue(vid, name, lat=-8.05, lng=-34.88):
    return VenueLite(venue_id=vid, venue_name=name, lat=lat, lng=lng)


class TestExtractMentions:
    def test_multiple_mentions_in_order(self):
        caption = "Hoje: @barA e depois @barB, com apoio de @barA de novo"
        assert extract_mentions(caption) == ["bara", "barb", "bara"]

    def test_leading_at_and_case_normalized(self):
        assert extract_mentions("Vem pro @BarCentral hoje") == ["barcentral"]

    def test_an_email_address_is_never_a_mention(self):
        """An '@' immediately preceded by a letter is the local part of an
        email address, not a mention — the one rule that tells them apart."""
        caption = "Contato: contato@barexample.com para reservas"
        assert extract_mentions(caption) == []

    def test_email_and_real_mention_in_the_same_caption(self):
        caption = "Fala com a gente: contato@barexample.com ou chama @barcentral"
        assert extract_mentions(caption) == ["barcentral"]

    def test_no_caption_yields_no_mentions(self):
        assert extract_mentions(None) == []
        assert extract_mentions("") == []


class TestGateAutoLink:
    def _candidates(self, *scores):
        return [
            LinkCandidate(venue_id=f"v{i}", venue_name=f"V{i}", method=METHOD_NAME_MATCH, score=s)
            for i, s in enumerate(scores)
        ]

    def test_no_candidates_never_auto_links(self):
        ok, reason = gate_auto_link([], floor=0.55, margin=0.08)
        assert ok is False
        assert reason == "no_candidates"

    def test_single_candidate_above_floor_auto_links(self):
        """The single-candidate branch: no runner-up to compare against, so
        the floor alone decides. This is the branch the plan calls out as
        easy to get wrong."""
        ok, reason = gate_auto_link(self._candidates(0.60), floor=0.55, margin=0.08)
        assert ok is True
        assert reason == "single_candidate_above_floor"

    def test_single_candidate_below_floor_never_auto_links(self):
        ok, reason = gate_auto_link(self._candidates(0.40), floor=0.55, margin=0.08)
        assert ok is False
        assert reason == "below_floor"

    def test_top_below_floor_fails_even_with_a_wide_margin(self):
        """The floor is checked on the TOP score regardless of how far ahead
        it is of the runner-up — clearing the floor is a precondition, not
        something the margin can substitute for."""
        ok, reason = gate_auto_link(self._candidates(0.50, 0.10), floor=0.55, margin=0.08)
        assert ok is False
        assert reason == "below_floor"

    def test_margin_just_inside_the_threshold_fails(self):
        """0.91 vs 0.89: both above the floor, but 0.02 < 0.08 — a coin toss
        wearing a high score, per the plan's own framing."""
        ok, reason = gate_auto_link(self._candidates(0.91, 0.89), floor=0.55, margin=0.08)
        assert ok is False
        assert reason == "margin"

    def test_margin_exactly_at_the_threshold_clears_it(self):
        ok, reason = gate_auto_link(self._candidates(0.91, 0.83), floor=0.55, margin=0.08)
        assert ok is True
        assert reason == "auto"

    def test_margin_just_outside_the_threshold_clears_it(self):
        ok, reason = gate_auto_link(self._candidates(0.91, 0.82), floor=0.55, margin=0.08)
        assert ok is True
        assert reason == "auto"


class TestLadderOrdering:
    def test_handle_mention_wins_even_when_a_name_match_scores_higher(self):
        """Certainty beats score: rung 1 resolves before rung 3 is even
        computed, so a mentioned venue wins regardless of how well (or
        badly) its OWN name matches location_text, and regardless of how
        perfectly a DIFFERENT venue's name matches it."""
        mentioned = _venue("v_mentioned", "Totally Unrelated Name")
        name_match_target = _venue("v_name_match", "Casa Rosa Exata")
        handle_index = {"casarosaexata": "v_name_match", "mentionedvenue": "v_mentioned"}

        result = resolve_event_venue(
            caption="Bora pro @mentionedvenue hoje!",
            location_text="Casa Rosa Exata",  # would score ~1.0 on its own
            location_tag=None,
            promoter_handle="somepromoter",
            venues=[mentioned, name_match_target],
            handle_index=handle_index,
            confidence_floor=0.55, margin=0.08,
        )
        assert result.resolution == RESOLUTION_AUTO
        assert result.venue_id == "v_mentioned"
        assert result.method == METHOD_HANDLE_MENTION
        assert result.confidence == 1.0

    def test_own_handle_mention_is_never_used(self):
        """A promoter mentioning itself must not resolve a venue, even when
        that same handle happens to be a known venue's handle."""
        venue = _venue("v1", "Selfpromo Venue")
        handle_index = {"selfpromo": "v1"}
        result = resolve_event_venue(
            caption="Segue a gente @selfpromo!",
            location_text=None, location_tag=None,
            promoter_handle="selfpromo",
            venues=[venue], handle_index=handle_index,
        )
        assert result.venue_id is None
        assert result.method is None

    def test_location_tag_wins_over_name_match_when_no_mention(self):
        tag_venue = _venue("v_tag", "Tag Venue")
        other = _venue("v_other", "Other Venue")
        result = resolve_event_venue(
            caption="Ingressos abertos!",
            location_text="Other Venue",
            location_tag={"name": "Tag Venue", "lat": -8.05, "lng": -34.88},
            promoter_handle="promo",
            venues=[tag_venue, other],
            handle_index={},
        )
        assert result.resolution == RESOLUTION_AUTO
        assert result.venue_id == "v_tag"
        assert result.method == METHOD_LOCATION_TAG

    def test_a_weak_location_tag_degrades_to_name_match(self):
        """Tolerant of an unverified/weak tag (plan's Open Questions): a tag
        that does not clear the location-tag floor falls through to rungs
        3-4 rather than being trusted as an identity."""
        weak_tag_venue = _venue("v_weak", "Somewhat Similar Name")
        real_match = _venue("v_match", "Exact Match Venue")
        result = resolve_event_venue(
            caption="Ingressos abertos!",
            location_text="Exact Match Venue",
            location_tag={"name": "Vaguely Similar", "lat": -8.05, "lng": -34.88},
            promoter_handle="promo",
            venues=[weak_tag_venue, real_match],
            handle_index={},
        )
        assert result.method == METHOD_NAME_MATCH
        assert result.venue_id == "v_match"

    def test_no_location_text_and_no_tag_is_unresolved(self):
        result = resolve_event_venue(
            caption="Ingressos abertos!", location_text=None, location_tag=None,
            promoter_handle="promo", venues=[_venue("v1", "Any Venue")], handle_index={},
        )
        assert result.resolution == RESOLUTION_UNRESOLVED
        assert result.venue_id is None
        assert result.candidates == []

    def test_below_floor_name_match_is_unresolved_not_queued(self):
        venue = _venue("v1", "Zetta Lounge")
        result = resolve_event_venue(
            caption="Ingressos abertos!", location_text="Completely Different Text",
            location_tag=None, promoter_handle="promo", venues=[venue], handle_index={},
            confidence_floor=DEFAULT_CONFIDENCE_FLOOR,
        )
        assert result.resolution == RESOLUTION_UNRESOLVED
        assert result.venue_id is None

    def test_margin_failure_queues_with_both_candidates_ranked(self):
        v1 = _venue("v1", "Casa Rosa Centro")
        v2 = _venue("v2", "Casa Rosa Sul")
        result = resolve_event_venue(
            caption="Ingressos abertos!", location_text="Casa Rosa",
            location_tag=None, promoter_handle="promo", venues=[v1, v2], handle_index={},
            confidence_floor=0.55, margin=0.05,
        )
        assert result.resolution == RESOLUTION_QUEUED
        assert result.venue_id is None
        assert len(result.candidates) == 2


class TestProximityTieBreak:
    def test_nearer_venue_wins_between_two_identically_scored_venues(self):
        """Rung 4: when two venues score identically on name alone, the one
        closer to the location tag's coordinates ranks first."""
        far = VenueLite(venue_id="v_far", venue_name="Duplicado", lat=-8.50, lng=-35.30)
        near = VenueLite(venue_id="v_near", venue_name="Duplicado", lat=-8.051, lng=-34.881)
        result = resolve_event_venue(
            caption="Ingressos abertos!", location_text="Duplicado",
            location_tag={"name": "Nowhere Matching", "lat": -8.05, "lng": -34.88},
            promoter_handle="promo", venues=[far, near], handle_index={},
            confidence_floor=0.9, margin=0.5,  # forces queued (identical scores)
        )
        assert len(result.candidates) == 2
        assert result.candidates[0].venue_id == "v_near"
        assert result.candidates[1].venue_id == "v_far"


class TestReverseHandleLookup:
    """build_handle_index / normalize_handle behaviour, exercised through the
    ladder's rung 1 rather than re-testing normalize_handle in isolation
    (already covered by tests/test_venue_name_matching.py's neighbourhood)."""

    def _index(self):
        return {"knownvenue": "v_known"}

    def test_known_handle_resolves(self):
        result = resolve_event_venue(
            caption="Bora pro @knownvenue!", location_text=None, location_tag=None,
            promoter_handle="promo", venues=[_venue("v_known", "Known Venue")],
            handle_index=self._index(),
        )
        assert result.venue_id == "v_known"

    def test_unknown_handle_does_not_resolve(self):
        result = resolve_event_venue(
            caption="Bora pro @totallyunknown!", location_text=None, location_tag=None,
            promoter_handle="promo", venues=[_venue("v_known", "Known Venue")],
            handle_index=self._index(),
        )
        assert result.resolution == RESOLUTION_UNRESOLVED
        assert result.venue_id is None

    @pytest.mark.parametrize("mention", ["@KnownVenue", "@knownvenue", "@KNOWNVENUE"])
    def test_case_and_leading_at_variants_all_resolve(self, mention):
        result = resolve_event_venue(
            caption=f"Bora pro {mention}!", location_text=None, location_tag=None,
            promoter_handle="promo", venues=[_venue("v_known", "Known Venue")],
            handle_index=self._index(),
        )
        assert result.venue_id == "v_known"
