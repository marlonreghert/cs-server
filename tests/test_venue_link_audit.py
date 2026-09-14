"""Unit tests for `app.services.venue_link_audit` —
plans/260913_venue-handle-link-audit.md.

Division of labour against the sibling BDD feature
(`tests/bdd/enrichment/venue-handle-link-audit.feature`): the BDD scenarios
pin the USER-FACING behaviour in Gherkin; this file pins the pure
comparison and aggregation functions' exact edge cases, using the same
re-verified real strings this plan's Evidence section and
`tests/fixtures/venue_link_audit/corpus.json` both carry — never an
invented string for a case central to the design.
"""
from __future__ import annotations

from app.dao.venue_repository import VenueRepository
from app.models.venue import Venue
from app.services.event_venue_resolution import DEFAULT_CONFIDENCE_FLOOR
from app.services.instagram_cascade_service import name_similarity
from app.services.venue_link_audit import (
    MIN_NON_CORROBORATING_EVENTS,
    collect_venue_link_audit,
    compute_venue_link_audit,
    venue_corroborates_location_text,
)
from tests.rds_fake import InMemoryRdsVenueStore

_CASA_DA_CULTURA = "Casa da Cultura de Pernambuco"
_CASA_DA_CULTURA_NEIGHBOURHOOD = "São José"
_THEATER = "Theater Luís Mendonça"
_THEATER_NEIGHBOURHOOD = "Boa Viagem"


class TestVenueCorroboratesLocationText:
    """The pure comparison — real, re-verified values only."""

    def test_empty_text_is_not_checkable(self):
        assert venue_corroborates_location_text(_THEATER, _THEATER_NEIGHBOURHOOD, "") is None
        assert venue_corroborates_location_text(_THEATER, _THEATER_NEIGHBOURHOOD, None) is None

    def test_too_short_text_is_not_checkable(self):
        # Folds to fewer than 3 alphabetic characters.
        assert venue_corroborates_location_text(_THEATER, _THEATER_NEIGHBOURHOOD, "À.") is None

    def test_a_different_specific_place_does_not_corroborate(self):
        # Real, re-verified: name_similarity=0.2791, no neighbourhood hit.
        assert venue_corroborates_location_text(
            _CASA_DA_CULTURA, _CASA_DA_CULTURA_NEIGHBOURHOOD, "Torre Malakoff",
        ) is False

    def test_a_spelling_variant_of_the_venues_own_name_corroborates(self):
        # Real, re-verified: name_similarity=0.8649, comfortably above floor.
        assert venue_corroborates_location_text(
            _THEATER, _THEATER_NEIGHBOURHOOD, "Teatro Luiz Mendonça",
        ) is True

    def test_name_plus_own_address_corroborates_via_neighbourhood_not_name(self):
        """The historical regression this module's docstring names:
        `event_dedup_backlog.py` once compared the OTHER containment
        direction and broke on self-referential prose. Combining the
        venue's name with its own address drops the raw name_similarity
        score below the floor (real, re-verified: 0.4776) — this text is
        recovered ONLY by the neighbourhood check."""
        text = "Teatro Luiz Mendonça — Av. Boa Viagem, S/N, Boa Viagem, Recife/PE"
        raw_score = name_similarity(_THEATER, text, None)
        assert raw_score < DEFAULT_CONFIDENCE_FLOOR, raw_score
        assert venue_corroborates_location_text(_THEATER, _THEATER_NEIGHBOURHOOD, text) is True

    def test_the_same_self_referential_shape_beerdock_once_got_wrong(self):
        """The EXACT historical false-positive `event_dedup_backlog.py`'s
        docstring records: "nossa unidade de Boa Viagem" is not a substring
        of "BeerDock Boa Viagem" under the OLD (rejected) containment
        direction, but IS correctly recognised as corroborating here."""
        assert venue_corroborates_location_text(
            "BeerDock Boa Viagem", "Boa Viagem", "nossa unidade de Boa Viagem",
        ) is True

    def test_a_generic_single_word_that_is_part_of_the_venues_own_name_corroborates(self):
        # Real, re-verified: "Pernambuco" alone scores 0.5714 against the
        # mapped venue's own name (which itself contains "Pernambuco") —
        # just clears the floor, so this is NOT flagged as contrary
        # evidence (consistent with the task's own false-positive guard:
        # a bare state name is too generic to be decisive either way).
        assert venue_corroborates_location_text(
            _CASA_DA_CULTURA, _CASA_DA_CULTURA_NEIGHBOURHOOD, "Pernambuco",
        ) is True

    def test_no_neighbourhood_falls_back_to_name_match_alone(self):
        assert venue_corroborates_location_text(_THEATER, None, "Teatro Luiz Mendonça") is True
        assert venue_corroborates_location_text(_THEATER, None, "Torre Malakoff") is False

    def test_no_venue_name_falls_back_to_neighbourhood_match_alone(self):
        assert venue_corroborates_location_text(
            None, _THEATER_NEIGHBOURHOOD, "a show at Boa Viagem tonight",
        ) is True
        assert venue_corroborates_location_text(None, _THEATER_NEIGHBOURHOOD, "Torre Malakoff") is False


class TestComputeVenueLinkAudit:
    """The pure aggregation, validated against the three re-verified seed
    cases plus the pattern-bar and double-mapped-handle isolation cases."""

    def test_editaisculturape_is_flagged(self):
        """Real, re-verified: 10 of 14 events carry empty location_text
        (not checkable); of the 4 with text, exactly 2 are decisive
        non-corroborating evidence — clears the pattern bar of 2."""
        target = {
            "handle": "editaisculturape",
            "mapped_venues": [{
                "venue_id": "casa_da_cultura", "venue_name": _CASA_DA_CULTURA,
                "neighborhood": _CASA_DA_CULTURA_NEIGHBOURHOOD,
            }],
        }
        events = [
            {"location_text": ""}, {"location_text": ""}, {"location_text": ""},
            {"location_text": ""}, {"location_text": ""},
            {"location_text": "Casa de Câmara e Cadeia de Brejo da Madre de Deus"},
            {"location_text": "Torre Malakoff"},
            {"location_text": "Mapa Cultural de Pernambuco"},
            {"location_text": ""}, {"location_text": ""},
            {"location_text": "Pernambuco"},
            {"location_text": ""}, {"location_text": ""}, {"location_text": ""},
        ]
        candidates = compute_venue_link_audit(
            [target], {"editaisculturape": events},
        )
        assert len(candidates) == 1, candidates
        [candidate] = candidates
        assert candidate.handle == "editaisculturape"
        flagged = {v.venue_id for v in candidate.mapped_venues if v.flagged}
        assert flagged == {"casa_da_cultura"}, candidate
        [venue] = candidate.mapped_venues
        assert venue.checkable_count == 4, venue  # the 4 non-empty location_text values
        assert venue.non_corroborating_count == 2, venue  # Casa de Câmara... and Torre Malakoff

    def test_teatroluizmendonca_is_not_flagged(self):
        """Real, re-verified: 9 of 10 events corroborate via name match; the
        one exception scores 0.5556, just above the floor — zero
        non-corroborating events, safely under any reasonable bar."""
        target = {
            "handle": "teatroluizmendonca",
            "mapped_venues": [{
                "venue_id": "theater", "venue_name": _THEATER,
                "neighborhood": _THEATER_NEIGHBOURHOOD,
            }],
        }
        events = [{"location_text": "Teatro Luiz Mendonça"}] * 9 + [
            {"location_text": "Teatro Nacional"},
        ]
        candidates = compute_venue_link_audit(
            [target], {"teatroluizmendonca": events},
        )
        assert candidates == [], candidates

    def test_real_botequim_flags_only_the_wrong_sibling(self):
        """Real, re-verified double-mapped handle: the correct venue's own
        neighbourhood ("Casa Forte") is found in every event's
        location_text; the unrelated Sao Paulo venue's neighbourhood
        ("Pinheiros") never is."""
        target = {
            "handle": "real.botequim",
            "mapped_venues": [
                {"venue_id": "bar_real_botequim", "venue_name": "Bar Real Botequim", "neighborhood": "Casa Forte"},
                {"venue_id": "real_bar_e_lanches", "venue_name": "Real Bar e Lanches", "neighborhood": "Pinheiros"},
            ],
        }
        events = [
            {"location_text": "Real Botequim\nAv. 17 de agosto, 1761 - Casa Forte"},
        ] * 5
        candidates = compute_venue_link_audit(
            [target], {"real.botequim": events},
        )
        assert len(candidates) == 1, candidates
        [candidate] = candidates
        flagged = {v.venue_id for v in candidate.mapped_venues if v.flagged}
        assert flagged == {"real_bar_e_lanches"}, candidate
        # The correct sibling stays VISIBLE in the same record, never dropped.
        present = {v.venue_id for v in candidate.mapped_venues}
        assert present == {"bar_real_botequim", "real_bar_e_lanches"}, candidate

    def test_a_single_non_corroborating_event_does_not_clear_the_pattern_bar(self):
        assert MIN_NON_CORROBORATING_EVENTS == 2, (
            "this test and its reasoning assume a bar of 2 — re-read the "
            "module docstring before changing the constant"
        )
        target = {
            "handle": "solo",
            "mapped_venues": [{"venue_id": "v1", "venue_name": _THEATER, "neighborhood": _THEATER_NEIGHBOURHOOD}],
        }
        events = [
            {"location_text": "Teatro Luiz Mendonça"},
            {"location_text": "Teatro Luiz Mendonça"},
            {"location_text": "Torre Malakoff"},  # exactly one non-corroborating event
        ]
        candidates = compute_venue_link_audit([target], {"solo": events})
        assert candidates == [], candidates

    def test_an_orphaned_target_with_no_mapped_venues_produces_nothing(self):
        target = {"handle": "orphan", "mapped_venues": []}
        candidates = compute_venue_link_audit([target], {"orphan": []})
        assert candidates == []

    def test_a_handle_with_no_events_at_all_produces_nothing(self):
        target = {
            "handle": "quiet",
            "mapped_venues": [{"venue_id": "v1", "venue_name": _THEATER, "neighborhood": _THEATER_NEIGHBOURHOOD}],
        }
        candidates = compute_venue_link_audit([target], {"quiet": []})
        assert candidates == []

    def test_events_already_reattributed_elsewhere_never_flag_their_original_mapping(self):
        """plans/260914_venue-link-audit-checks-current-attribution.md: the
        real beerdock_recife shape. 16 events already carry
        venue_id='beerdock_casa_forte' — a DIFFERENT, specific venue,
        already correctly resolved by an unrelated mechanism hours before
        this sweep runs. They must not count as non-corroborating evidence
        against the ORIGINAL mapped venue, "BeerDock Boa Viagem", which
        they were never force-assigned to and are no longer evidence
        about."""
        target = {
            "handle": "beerdock_recife",
            "mapped_venues": [{
                "venue_id": "beerdock_boa_viagem", "venue_name": "BeerDock Boa Viagem",
                "neighborhood": "Boa Viagem",
            }],
        }
        events = [
            {"location_text": "CASA FORTE", "venue_id": "beerdock_casa_forte"},
        ] * 16
        candidates = compute_venue_link_audit(
            [target], {"beerdock_recife": events},
        )
        assert candidates == [], candidates

    def test_an_unresolved_event_still_counts_toward_the_mapped_venue(self):
        """The regression guard the fix above needs: `venue_id=None`
        (never resolved to anything) is NOT the same as "already resolved
        elsewhere" — it must still count, or the fix over-corrects into
        excluding every non-corroborating event regardless of shape."""
        target = {
            "handle": "editaisculturape",
            "mapped_venues": [{
                "venue_id": "casa_da_cultura", "venue_name": _CASA_DA_CULTURA,
                "neighborhood": _CASA_DA_CULTURA_NEIGHBOURHOOD,
            }],
        }
        events = [
            {"location_text": "Torre Malakoff", "venue_id": None},
            {"location_text": "Casa de Câmara e Cadeia de Brejo da Madre de Deus", "venue_id": None},
        ]
        candidates = compute_venue_link_audit(
            [target], {"editaisculturape": events},
        )
        assert len(candidates) == 1, candidates
        flagged = {v.venue_id for v in candidates[0].mapped_venues if v.flagged}
        assert flagged == {"casa_da_cultura"}, candidates

    def test_an_event_already_resolved_to_the_mapped_venue_itself_still_counts(self):
        """The third branch of the filter: venue_id == the venue being
        checked (not None, not a different venue) must still be a
        checkable event, or a force-assigned handle whose events all
        already carry the one mapped venue_id (the real editaisculturape
        shape) would silently stop being auditable at all."""
        target = {
            "handle": "editaisculturape",
            "mapped_venues": [{
                "venue_id": "casa_da_cultura", "venue_name": _CASA_DA_CULTURA,
                "neighborhood": _CASA_DA_CULTURA_NEIGHBOURHOOD,
            }],
        }
        events = [
            {"location_text": "Torre Malakoff", "venue_id": "casa_da_cultura"},
            {"location_text": "Casa de Câmara e Cadeia de Brejo da Madre de Deus", "venue_id": "casa_da_cultura"},
        ]
        candidates = compute_venue_link_audit(
            [target], {"editaisculturape": events},
        )
        assert len(candidates) == 1, candidates


class TestCollectVenueLinkAuditThroughVenueRepository:
    """`collect_venue_link_audit` is called in production with a
    `VenueRepository` instance (the router's `_dao()`), never the raw RDS
    store directly — every other test in this file exercises
    `InMemoryRdsVenueStore` directly, which would NOT catch a forwarding
    bug in `VenueRepository.get_address_bulk` (added by this plan). This
    class closes that gap, mirroring `tests/test_venue_repository_
    instagram.py`'s own construction pattern."""

    def _repo(self):
        store = InMemoryRdsVenueStore()
        repo = VenueRepository(client=None, rds_store=store)
        return repo, store

    def test_flags_through_the_real_repository_boundary(self):
        repo, store = self._repo()
        store.upsert_venue(Venue(
            venue_id="v1", venue_name=_CASA_DA_CULTURA, venue_lat=-8.06, venue_lng=-34.88,
            venue_address="R.Floriano Peixoto - São José, Recife - PE",
        ))
        store.update_venue_address_components(
            "v1", source="operator", neighborhood=_CASA_DA_CULTURA_NEIGHBOURHOOD,
        )
        store.enrichment.setdefault("instagram.handle", {})["v1"] = {
            "instagram_handle": "editaisculturape",
        }
        store.upsert_crawl_target("editaisculturape", {
            "kind": "venue", "enabled": True, "cron": "0 0 * * *",
        })
        for text in ("Torre Malakoff", "Casa de Câmara e Cadeia de Brejo da Madre de Deus"):
            store.insert_event({
                "event_id": f"evt_{text[:4]}", "venue_id": "v1", "starts_at": None,
                "title": "EVENT", "post_type": "event", "status": "accepted",
                "location_text": text, "lineup": [],
                "source_kind": "venue_post", "source_handle": "editaisculturape",
                "source_shortcode": f"sc_{text[:4]}",
            })

        candidates = collect_venue_link_audit(repo)
        assert len(candidates) == 1, candidates
        flagged = {v.venue_id for c in candidates for v in c.mapped_venues if v.flagged}
        assert flagged == {"v1"}, candidates
