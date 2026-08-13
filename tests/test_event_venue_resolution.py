"""Unit coverage for the promoter-event resolution ladder
(app/services/event_venue_resolution.py). See
plans/260804_instagram-promoter-events.md §D.

BDD (tests/bdd/enrichment/instagram-promoter-events.feature) proves the
ladder end-to-end through the real crawl pipeline; these tests isolate the
pure decision logic — the ordering, the two gates, and the lower-level
helpers — so a regression in the arithmetic fails fast and close to the bug.
"""
import pytest

from app.metrics import EVENT_VENUE_NAME_MATCH_SKIPPED_TOTAL
from app.services.event_reconciliation import REVIEW_REASON_VENUE_NOT_IN_CATALOG
from app.services.event_venue_resolution import (
    DEFAULT_CONFIDENCE_FLOOR,
    METHOD_AMBIGUOUS_CAPTION_REFUSAL,
    METHOD_CAPTION_HANDLE_MENTION,
    METHOD_HANDLE_MENTION,
    METHOD_LOCATION_TAG,
    METHOD_NAME_MATCH,
    METHOD_NEIGHBOURHOOD_MATCH,
    METHOD_VENUE_NOT_IN_CATALOG,
    RESOLUTION_AUTO,
    RESOLUTION_QUEUED,
    RESOLUTION_UNRESOLVED,
    LinkCandidate,
    VenueLite,
    _name_match_candidates,
    _strip_handles_for_name_match,
    extract_mentions,
    gate_auto_link,
    resolve_event_venue,
)


def _venue(vid, name, lat=-8.05, lng=-34.88, address=None):
    return VenueLite(venue_id=vid, venue_name=name, lat=lat, lng=lng, address=address)


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
    def test_per_event_name_match_wins_over_a_caption_mention(self):
        """plans/260812_event-attribution-and-dates.md §A reordered the
        ladder: per-event evidence (here, a location_text NAME match, rung
        4) now outranks a post-level caption @-mention (rung 5, demoted).
        This is the precedence bug measured at 487/494 wrong links in
        production — a caption mentioning some OTHER venue first must never
        win over what the event's own text says, even when that per-event
        signal is a fuzzy name match rather than an exact @-mention."""
        mentioned = _venue("v_mentioned", "Totally Unrelated Name")
        name_match_target = _venue("v_name_match", "Casa Rosa Exata")
        handle_index = {"casarosaexata": "v_name_match", "mentionedvenue": "v_mentioned"}

        result = resolve_event_venue(
            caption="Bora pro @mentionedvenue hoje!",
            location_text="Casa Rosa Exata",  # the event's OWN evidence
            location_tag=None,
            promoter_handle="somepromoter",
            venues=[mentioned, name_match_target],
            handle_index=handle_index,
            confidence_floor=0.55, margin=0.08,
        )
        assert result.resolution == RESOLUTION_AUTO
        assert result.venue_id == "v_name_match"
        assert result.method == METHOD_NAME_MATCH

    def test_event_own_handle_mention_wins_over_a_different_caption_mention(self):
        """The sharpest version of §A's fix: the event's own location_text
        names ONE venue by @-handle, the caption's FIRST mention names a
        DIFFERENT one — the event's own text must win, and `linked_by` must
        say so via `METHOD_HANDLE_MENTION` (not the demoted
        `METHOD_CAPTION_HANDLE_MENTION`)."""
        caption_first_venue = _venue("v_caption", "Sempre Rock Bar")
        event_own_venue = _venue("v_event", "Taverna Pub")
        handle_index = {"semprerockbar": "v_caption", "tavernapubnatal": "v_event"}

        result = resolve_event_venue(
            caption="Confira o roteiro: @semprerockbar tem festa hoje!",
            location_text="@tavernapubnatal",
            location_tag=None,
            promoter_handle="oquetemhojeemnatal",
            venues=[caption_first_venue, event_own_venue],
            handle_index=handle_index,
        )
        assert result.resolution == RESOLUTION_AUTO
        assert result.venue_id == "v_event"
        assert result.method == METHOD_HANDLE_MENTION

    def test_mixed_case_handle_in_location_text_still_resolves(self):
        """Handle-case parity guard (plan's own test discipline note): every
        OTHER fixture in this file seeds a lowercase handle, which makes the
        single-venue `_handle_for`-style lookup and the shared-handle
        `target["handle"]` normalization coincide by accident and would hide
        a real case-handling bug. This fixture's handle_index key is
        deliberately mixed-case-normalized (as `build_handle_index` always
        produces via `normalize_handle`), while the event's OWN
        location_text uses a differently-cased mention."""
        venue = _venue("v1", "Taverna Pub")
        handle_index = {"tavernapubnatal": "v1"}  # normalize_handle output
        result = resolve_event_venue(
            caption="Ingressos abertos!",
            location_text="@TavernaPubNatal",  # mixed case, as a flyer might read
            location_tag=None,
            promoter_handle="promo",
            venues=[venue],
            handle_index=handle_index,
        )
        assert result.resolution == RESOLUTION_AUTO
        assert result.venue_id == "v1"
        assert result.method == METHOD_HANDLE_MENTION

    def test_caption_mention_still_resolves_when_unambiguous_and_event_has_nothing(self):
        """Rung 5 is demoted, not removed: a caption naming EXACTLY one
        known venue is still good evidence when the event's own text gives
        nothing at all."""
        venue = _venue("v1", "Sempre Rock Bar")
        handle_index = {"semprerockbar": "v1"}
        result = resolve_event_venue(
            caption="Hoje: @semprerockbar com festa!",
            location_text=None, location_tag=None,
            promoter_handle="promo", venues=[venue], handle_index=handle_index,
        )
        assert result.resolution == RESOLUTION_AUTO
        assert result.venue_id == "v1"
        assert result.method == METHOD_CAPTION_HANDLE_MENTION

    def test_ambiguous_caption_with_no_event_evidence_refuses_to_link(self):
        """A caption naming several known venues is worthless as evidence
        for one event with nothing of its own to say — refuse rather than
        guess, the precedence bug's exact shape (487/494 wrong links all
        inherited the caption's FIRST mention)."""
        v1 = _venue("v1", "Sempre Rock Bar")
        v2 = _venue("v2", "Taverna Pub")
        handle_index = {"semprerockbar": "v1", "tavernapubnatal": "v2"}
        result = resolve_event_venue(
            caption="Roteiro: @semprerockbar, @tavernapubnatal e muito mais!",
            location_text=None, location_tag=None,
            promoter_handle="promo", venues=[v1, v2], handle_index=handle_index,
        )
        assert result.resolution == RESOLUTION_UNRESOLVED
        assert result.venue_id is None
        assert result.method == METHOD_AMBIGUOUS_CAPTION_REFUSAL

    def test_twenty_events_one_roundup_resolve_to_twenty_distinct_venues(self):
        """The roundup shape from the plan's own Evidence section: one
        promoter post lists 20 events at 20 different venues under one
        caption. Every event must resolve to the venue ITS OWN
        location_text names, never all inheriting the caption's first
        mention."""
        venues = [_venue(f"v{i}", f"Venue {i}") for i in range(20)]
        handle_index = {f"venue{i}handle": f"v{i}" for i in range(20)}
        caption = "Roteiro da noite: " + ", ".join(f"@venue{i}handle" for i in range(20))

        resolved_ids = []
        for i in range(20):
            result = resolve_event_venue(
                caption=caption,
                location_text=f"@venue{i}handle",
                location_tag=None,
                promoter_handle="oquetemhojeemnatal",
                venues=venues, handle_index=handle_index,
            )
            assert result.resolution == RESOLUTION_AUTO, (i, result)
            resolved_ids.append(result.venue_id)

        assert resolved_ids == [f"v{i}" for i in range(20)]
        assert len(set(resolved_ids)) == 20  # no two events collapsed onto one venue

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


# ── §B: two venues behind one account ────────────────────────────────────
class TestNeighbourhoodMatching:
    def _two_branches(self):
        boa_viagem = _venue("v_bv", "BeerDock Boa Viagem", address="Av. Domingos Ferreira, 1000 - Boa Viagem, Recife")
        casa_forte = _venue("v_cf", "BeerDock Casa Forte", address="Rua Alfredo Lisboa, 200 - Casa Forte, Recife")
        return boa_viagem, casa_forte

    def test_neighbourhood_text_routes_to_the_matching_branch(self):
        boa_viagem, casa_forte = self._two_branches()
        result = resolve_event_venue(
            caption="Confira nossa unidade!", location_text="CASA FORTE",
            location_tag=None, promoter_handle="beerdock_recife",
            venues=[boa_viagem, casa_forte], handle_index={},
            same_account_venues=[boa_viagem, casa_forte],
        )
        assert result.resolution == RESOLUTION_AUTO
        assert result.venue_id == "v_cf"
        assert result.method == METHOD_NEIGHBOURHOOD_MATCH

    def test_the_other_branch_keeps_its_own_event(self):
        boa_viagem, casa_forte = self._two_branches()
        result = resolve_event_venue(
            caption="Confira nossa unidade!", location_text="BOA VIAGEM",
            location_tag=None, promoter_handle="beerdock_recife",
            venues=[boa_viagem, casa_forte], handle_index={},
            same_account_venues=[boa_viagem, casa_forte],
        )
        assert result.resolution == RESOLUTION_AUTO
        assert result.venue_id == "v_bv"

    def test_neither_neighbourhood_named_falls_through_to_queue_or_unresolved(self):
        boa_viagem, casa_forte = self._two_branches()
        result = resolve_event_venue(
            caption="Confira nossa unidade!", location_text="alguma coisa qualquer",
            location_tag=None, promoter_handle="beerdock_recife",
            venues=[boa_viagem, casa_forte], handle_index={},
            same_account_venues=[boa_viagem, casa_forte],
        )
        assert result.resolution != RESOLUTION_AUTO
        assert result.venue_id is None

    def test_a_single_member_same_account_set_never_triggers_the_rung(self):
        """Restricted to venues that share the target's handle/brand — a
        SINGLE-venue account has no branch question at all, so the rung is
        skipped entirely (falling straight to name-match on the FULL
        `venues` set) rather than trivially "matching" its own one member
        regardless of text."""
        unrelated = _venue("v_unrelated", "Unrelated Bar", address="Rua X, 50 - Casa Forte, Recife")
        conchittas = _venue("v_conchittas", "Conchittas Bar", address="Rua Y, 10 - Boa Vista, Recife")
        result = resolve_event_venue(
            caption="Bora!", location_text="CASA FORTE",
            location_tag=None, promoter_handle="conchittasbar",
            venues=[unrelated, conchittas], handle_index={},
            same_account_venues=[conchittas],  # only ONE member — no branch question
        )
        assert result.venue_id != "v_unrelated"

    def test_a_neighbourhood_string_never_drags_an_unrelated_venue_across_town(self):
        """The full `venues` catalog can contain an unrelated venue whose
        ADDRESS also happens to sit in the same neighbourhood — but the
        neighbourhood rung only ever consults `same_account_venues`, never
        the whole catalog, so an unrelated venue is safe by construction
        even when its address would otherwise match."""
        unrelated = _venue("v_unrelated", "Unrelated Bar", address="Rua X, 50 - Casa Forte, Recife")
        conchittas = _venue("v_conchittas", "Conchittas Bar", address="Rua Y, 10 - Boa Vista, Recife")
        result = resolve_event_venue(
            caption="Bora!", location_text="CASA FORTE",
            location_tag=None, promoter_handle="conchittasbar",
            venues=[unrelated, conchittas], handle_index={},
            same_account_venues=None,
        )
        assert result.venue_id != "v_unrelated"

    def test_more_than_one_address_match_falls_through_rather_than_guessing(self):
        v1 = _venue("v1", "Branch One", address="Rua A, 1 - Boa Viagem, Recife")
        v2 = _venue("v2", "Branch Two", address="Rua B, 2 - Boa Viagem, Recife")
        result = resolve_event_venue(
            caption="Bora!", location_text="BOA VIAGEM",
            location_tag=None, promoter_handle="brandhandle",
            venues=[v1, v2], handle_index={},
            same_account_venues=[v1, v2],
        )
        # Two equally-good address matches -- not a rung-3 auto-link; falls
        # through to name-match, which also cannot pick a winner from a bare
        # neighbourhood string, so this must not silently resolve.
        assert result.resolution != RESOLUTION_AUTO


# ── §A: venue_not_in_catalog ──────────────────────────────────────────────
class TestVenueNotInCatalog:
    def test_literal_value_matches_the_review_reason_constant(self):
        """The two constants live in different modules (to avoid an import
        cycle — see each one's own docstring) and MUST stay byte-identical,
        or a freshly-crawled row and a backfilled row would describe the
        same situation in two different words."""
        assert METHOD_VENUE_NOT_IN_CATALOG == REVIEW_REASON_VENUE_NOT_IN_CATALOG

    def test_an_unrecognized_handle_in_location_text_is_reported_distinctly(self):
        """The event's own location_text names a SPECIFIC @-handle that is
        not in handle_index at all -- concrete evidence of exactly where
        this is, never confused with a genuine no-evidence unresolved."""
        known = _venue("v1", "Known Venue")
        result = resolve_event_venue(
            caption="Ingressos abertos!", location_text="@somehandlewedontcarry",
            location_tag=None, promoter_handle="promo",
            venues=[known], handle_index={"knownvenue": "v1"},
        )
        assert result.resolution == RESOLUTION_UNRESOLVED
        assert result.venue_id is None
        assert result.method == METHOD_VENUE_NOT_IN_CATALOG

    def test_an_unrecognized_handle_still_yields_to_a_resolving_rung(self):
        """The unrecognized-handle signal is remembered but never short-
        circuits the ladder -- a LATER rung that DOES resolve still wins."""
        known = _venue("v1", "Sempre Rock Bar")
        result = resolve_event_venue(
            caption="Hoje: @semprerockbar com festa! Chama tambem @naocatalogado",
            location_text=None, location_tag=None,
            promoter_handle="promo", venues=[known], handle_index={"semprerockbar": "v1"},
        )
        assert result.resolution == RESOLUTION_AUTO
        assert result.venue_id == "v1"


# ── §A: handle stripping before rung 4 ────────────────────────────────────
# plans/260813_handle-attribution-hardening.md
class TestStripHandlesForNameMatch:
    def test_handle_only_strips_to_none(self):
        assert _strip_handles_for_name_match("@mahalilacafe") is None

    def test_handle_plus_a_place_name_keeps_the_place_name(self):
        assert _strip_handles_for_name_match("@obarpraia Ponta Negra") == "Ponta Negra"

    def test_a_name_with_no_handle_is_untouched(self):
        assert _strip_handles_for_name_match("Conchittas Bar - Rua da Imperatriz, 218") == (
            "Conchittas Bar - Rua da Imperatriz, 218"
        )

    def test_empty_string_stays_empty_not_none(self):
        """`None` is reserved for "nothing with alphabetic content survived
        the strip" — a genuinely empty input was never a handle to begin
        with, so it must not be conflated with that outcome."""
        assert _strip_handles_for_name_match("") == ""

    def test_none_input_passes_through(self):
        assert _strip_handles_for_name_match(None) is None

    def test_handle_with_a_dot_strips_to_none(self):
        """Real production handle — plans/260813_handle-attribution-
        hardening.md's own Evidence."""
        assert _strip_handles_for_name_match("@espaco.muta") is None

    def test_handle_with_a_trailing_underscore_strips_to_none(self):
        """Real production handle."""
        assert _strip_handles_for_name_match("@bar54_") is None

    def test_handle_with_underscores_throughout_strips_to_none(self):
        """Real production handle."""
        assert _strip_handles_for_name_match("@letra_a_") is None

    def test_punctuation_left_after_stripping_is_still_treated_as_nothing(self):
        """Only PUNCTUATION survives the strip (no letters) -- must not be
        handed to the fuzzy scorer as if it were a name."""
        assert _strip_handles_for_name_match("@mahalilacafe - !!!") is None

    def test_an_email_address_is_never_treated_as_a_handle(self):
        """The same email guard rung 1/5 already rely on
        (`_MENTION_RE`'s lookbehind) -- an '@' preceded by a letter is not a
        mention, so nothing is stripped."""
        text = "Contato: contato@barexample.com"
        assert _strip_handles_for_name_match(text) == text


class TestRungFourSkippedWhenHandleOnly:
    def test_rung_four_never_runs_when_nothing_alphabetic_remains(self):
        """plans/260813_handle-attribution-hardening.md §A: an unresolvable
        handle must never reach the fuzzy scorer at all -- not "score it and
        reject the result", but never compute a score in the first place.
        `candidates` stays empty even though "Maria Café" and "Espaço
        Tucano" are both in the catalog and DO fuzzy-match
        `@mahalilacafe`'s characters (the live false positive)."""
        maria = _venue("v_maria", "Maria Café")
        espaco = _venue("v_espaco", "Espaço Tucano")
        result = resolve_event_venue(
            caption="Confira a programação de hoje!", location_text="@mahalilacafe",
            location_tag=None, promoter_handle="promo",
            venues=[maria, espaco], handle_index={},
        )
        assert result.candidates == []

    def test_skip_metric_increments_when_rung_four_is_skipped(self):
        before = EVENT_VENUE_NAME_MATCH_SKIPPED_TOTAL._value.get()
        resolve_event_venue(
            caption="Confira a programação de hoje!", location_text="@mahalilacafe",
            location_tag=None, promoter_handle="promo",
            venues=[_venue("v_maria", "Maria Café")], handle_index={},
        )
        after = EVENT_VENUE_NAME_MATCH_SKIPPED_TOTAL._value.get()
        assert after == before + 1

    def test_skip_metric_does_not_increment_for_a_genuine_name_match(self):
        before = EVENT_VENUE_NAME_MATCH_SKIPPED_TOTAL._value.get()
        resolve_event_venue(
            caption="Ingressos abertos!", location_text="Sempre Rock Bar",
            location_tag=None, promoter_handle="promo",
            venues=[_venue("v1", "Sempre Rock Bar")], handle_index={},
        )
        after = EVENT_VENUE_NAME_MATCH_SKIPPED_TOTAL._value.get()
        assert after == before

    def test_skip_metric_does_not_increment_when_location_text_is_absent(self):
        """No location_text at all is "nothing to work with", not "a handle
        with nothing left after stripping" -- the two must not be conflated
        in the metric that is supposed to prove the fix is doing work."""
        before = EVENT_VENUE_NAME_MATCH_SKIPPED_TOTAL._value.get()
        resolve_event_venue(
            caption="Ingressos abertos!", location_text=None,
            location_tag=None, promoter_handle="promo",
            venues=[_venue("v1", "Sempre Rock Bar")], handle_index={},
        )
        after = EVENT_VENUE_NAME_MATCH_SKIPPED_TOTAL._value.get()
        assert after == before

    def test_rung_three_is_unaffected_by_handle_stripping(self):
        """The plan is explicit that rung 3 (neighbourhood/address) is
        bounded to a caller-supplied candidate set and untouched by §A --
        asserted here rather than assumed. An @handle-shaped location_text
        still lets the RAW text (never stripped) win the neighbourhood
        rung, exactly as an ordinary neighbourhood string would."""
        boa_viagem = _venue(
            "v_bv", "BeerDock Boa Viagem",
            address="Av. Domingos Ferreira, 1000 - @casaforte, Recife",
        )
        casa_forte = _venue(
            "v_cf", "BeerDock Casa Forte",
            address="Rua Alfredo Lisboa, 200 - Casa Forte, Recife",
        )
        result = resolve_event_venue(
            caption="Confira nossa unidade!", location_text="@casaforte",
            location_tag=None, promoter_handle="beerdock_recife",
            venues=[boa_viagem, casa_forte], handle_index={},
            same_account_venues=[boa_viagem, casa_forte],
        )
        # Rung 3 matched on the RAW (unstripped) "@casaforte" against
        # `boa_viagem`'s own address, which literally contains that
        # substring -- proof rung 3 never saw a stripped/altered text.
        assert result.resolution == RESOLUTION_AUTO
        assert result.method == METHOD_NEIGHBOURHOOD_MATCH
        assert result.venue_id == "v_bv"


# ── §C: _name_match_candidates refuses handle-tainted text directly ───────
class TestNameMatchCandidatesRefusesHandleTaintedText:
    def test_a_raw_handle_never_produces_a_candidate_even_if_it_would_score_high(self):
        """Defense in depth: even called DIRECTLY (bypassing
        `resolve_event_venue`'s own §A strip), `_name_match_candidates`
        refuses text that still carries an @handle -- the guarantee is a
        property of the scorer itself, not a caller convention."""
        maria = _venue("v_maria", "Maria Café")
        assert _name_match_candidates("@mahalilacafe", [maria]) == []

    def test_stripped_text_still_scores_normally(self):
        target = _venue("v1", "Ponta Negra Beach Club")
        candidates = _name_match_candidates("Ponta Negra", [target])
        assert len(candidates) == 1
        assert candidates[0].venue_id == "v1"


# ── the three production false positives, pinned by NAME ──────────────────
class TestProductionFalsePositivesByName:
    """plans/260813_handle-attribution-hardening.md Evidence: these two
    scored 0.76 and 0.73 against DEFAULT_CONFIDENCE_FLOOR=0.55 on the live
    resolver before this plan -- comfortably auto-linked. Anything that
    loosens the handle-stripping rule must break one of these first."""

    def test_mahalilacafe_never_links_to_maria_cafe(self):
        maria = _venue("v_maria", "Maria Café")
        result = resolve_event_venue(
            caption="Confira a programação de hoje! Ingressos abertos.",
            location_text="@mahalilacafe", location_tag=None,
            promoter_handle="oquetemhojeemnatal", venues=[maria], handle_index={},
        )
        assert result.venue_id != "v_maria"
        assert result.resolution != RESOLUTION_AUTO
        assert result.method == METHOD_VENUE_NOT_IN_CATALOG

    def test_espaco_muta_never_links_to_espaco_tucano(self):
        espaco = _venue("v_espaco", "Espaço Tucano")
        result = resolve_event_venue(
            caption="Confira a programação de hoje! Ingressos abertos.",
            location_text="@espaco.muta", location_tag=None,
            promoter_handle="oquetemhojeemnatal", venues=[espaco], handle_index={},
        )
        assert result.venue_id != "v_espaco"
        assert result.resolution != RESOLUTION_AUTO
        assert result.method == METHOD_VENUE_NOT_IN_CATALOG


# ── the six production links that were already CORRECT — must not regress ─
class TestProductionCorrectLinksUnaffected:
    """plans/260813_handle-attribution-hardening.md: the 2026-08-13 live run
    also produced six links that were already right. A fix for the three
    wrong ones must not touch these — every one resolves via rung 1
    (handle_mention), which §A/§C never modify, but pinned here anyway so a
    future regression fails close to the bug."""

    _VENUES = [
        ("Seu Chico Botequim", "seuchicobotequim"),
        ("Sempre Rock Bar", "semprerockbar"),
        ("Taverna Pub Medieval Bar & Avalon Events", "tavernapubnatal"),
        ("Ô Bar Restaurante - Ponta Negra", "obarpontanegra"),
        ("Bar 54", "bar54_"),
    ]

    def _catalog(self):
        venues = [_venue(f"v_{handle}", name) for name, handle in self._VENUES]
        handle_index = {handle: f"v_{handle}" for _name, handle in self._VENUES}
        return venues, handle_index

    @pytest.mark.parametrize("venue_name,handle", _VENUES)
    def test_handle_still_links_to_its_venue(self, venue_name, handle):
        venues, handle_index = self._catalog()
        result = resolve_event_venue(
            caption="Roteiro de hoje em Natal! Ingressos abertos.",
            location_text=f"@{handle}", location_tag=None,
            promoter_handle="oquetemhojeemnatal", venues=venues, handle_index=handle_index,
        )
        assert result.resolution == RESOLUTION_AUTO
        assert result.venue_id == f"v_{handle}"
        assert result.method == METHOD_HANDLE_MENTION


# ── §B: an event's own unrecognized handle outranks an ambiguous caption ──
class TestUnrecognizedHandleOutranksAmbiguousCaption:
    """plans/260813_handle-attribution-hardening.md §B, the diagnosis's own
    finding: measured on the real pipeline, a promoter roundup whose CAPTION
    names several known venues caused every event with an unrecognized
    handle of its OWN to be reported as the generic ambiguous-caption
    refusal instead of venue_not_in_catalog, because that branch returned
    before the unrecognized-handle fallback was ever reached. Per-event
    evidence must outrank post-level evidence for WHY an event has no venue,
    the same precedence plans/260812_event-attribution-and-dates.md §A
    already established for WHICH venue an event links to."""

    def test_unrecognized_handle_wins_over_an_ambiguous_caption(self):
        v1 = _venue("v1", "Seu Chico Botequim")
        v2 = _venue("v2", "Sempre Rock Bar")
        handle_index = {"seuchicobotequim": "v1", "semprerockbar": "v2"}
        result = resolve_event_venue(
            caption="Roteiro: @seuchicobotequim e @semprerockbar bombando! Ingressos abertos.",
            location_text="@totallyunknownhandle999", location_tag=None,
            promoter_handle="oquetemhojeemnatal", venues=[v1, v2], handle_index=handle_index,
        )
        assert result.resolution == RESOLUTION_UNRESOLVED
        assert result.venue_id is None
        assert result.method == METHOD_VENUE_NOT_IN_CATALOG

    def test_a_genuinely_ambiguous_caption_with_no_event_evidence_still_refuses(self):
        """Regression guard for the fix above: when the event's own text
        gives NOTHING (not even an unrecognized handle), the ambiguous-
        caption refusal must still fire exactly as it did before this
        plan -- the precedence only applies when there IS a specific
        per-event signal to prefer."""
        v1 = _venue("v1", "Seu Chico Botequim")
        v2 = _venue("v2", "Sempre Rock Bar")
        handle_index = {"seuchicobotequim": "v1", "semprerockbar": "v2"}
        result = resolve_event_venue(
            caption="Roteiro: @seuchicobotequim e @semprerockbar bombando! Ingressos abertos.",
            location_text=None, location_tag=None,
            promoter_handle="oquetemhojeemnatal", venues=[v1, v2], handle_index=handle_index,
        )
        assert result.resolution == RESOLUTION_UNRESOLVED
        assert result.venue_id is None
        assert result.method == METHOD_AMBIGUOUS_CAPTION_REFUSAL
