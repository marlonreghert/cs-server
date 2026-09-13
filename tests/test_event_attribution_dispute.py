"""Unit tests for app/services/event_attribution_dispute.py.

See plans/260912_events-venue-night-duplication.md §C (Defect 2). Pins the
dispute predicate on bare dicts/`VenueLite`s — no DAO, no extraction — against
the RCA's own production strings (`CASA FORTE`, `BeerDock Boa Viagem`,
`@beerdock.casaforte`).

The load-bearing assertions are the NEGATIVE ones. `260806_venue-post-multi-
event.md` §D bans re-attributing a venue's own post on a fuzzy NAME match,
and `260813_handle-attribution-hardening.md` exists because rung 4 scored
`@mahalilacafe` at 0.76 against "Maria Café". A rung-4 or rung-5 answer
reaching this predicate would re-open both; `test_a_name_match_never_disputes`
and `test_a_caption_mention_never_disputes` are what stop that, and anything
that widens the rule has to break one of them first.

Division of labour against the BDD sibling
(`tests/bdd/enrichment/events-venue-night-duplication.feature`): the
scenarios there prove what a REAL extraction stores and what an operator
sees; this file pins the predicate itself, including the brand-root shapes a
scenario would need a whole extra catalog to contrive.
"""
from __future__ import annotations

import pytest

from app.services.event_attribution_dispute import (
    DEFAULT_DISPUTE_ACTION,
    DEFAULT_DISPUTE_WITHHOLD_ENABLED,
    DISPUTE_ACTION_FLAG,
    DISPUTE_ACTION_REATTRIBUTE,
    AttributionDisputeConfig,
    brand_root_venues,
    evaluate_attribution_dispute,
    fold_review_reason,
    load_attribution_dispute_config,
    validate_dispute_action_config,
    validate_dispute_withhold_enabled_config,
)
from app.services.event_venue_resolution import (
    METHOD_HANDLE_MENTION,
    METHOD_NEIGHBOURHOOD_MATCH,
    METHOD_VENUE_NOT_IN_CATALOG,
    VenueLite,
)

BOA_VIAGEM = VenueLite(
    venue_id="v_bv", venue_name="BeerDock Boa Viagem",
    address="Av. Cons. Aguiar, 1000 - Boa Viagem, Recife - PE", lat=-8.12, lng=-34.90,
)
CASA_FORTE = VenueLite(
    venue_id="v_cf", venue_name="BeerDock Casa Forte",
    address="Av. Rui Barbosa, 500 - Casa Forte, Recife - PE", lat=-8.03, lng=-34.91,
)
MADALENA = VenueLite(
    venue_id="v_md", venue_name="BeerDock Madalena",
    address="R. José Bonifácio, 20 - Madalena, Recife - PE", lat=-8.05, lng=-34.92,
)
MARIA_CAFE = VenueLite(
    venue_id="v_mc", venue_name="Maria Café",
    address="R. do Sol, 7 - Santo Antônio, Recife - PE", lat=-8.06, lng=-34.87,
)
BAR_DO_ZE = VenueLite(
    venue_id="v_bz", venue_name="Bar do Zé",
    address="R. da Aurora, 10 - Boa Vista, Recife - PE",
)
BAR_CENTRAL = VenueLite(
    venue_id="v_bc", venue_name="Bar Central",
    address="R. Nova, 3 - Casa Forte, Recife - PE",
)

CATALOG = [BOA_VIAGEM, CASA_FORTE, MADALENA, MARIA_CAFE]
HANDLE_INDEX = {"beerdock_recife": "v_bv", "beerdock.casaforte": "v_cf"}


def _dispute(location_text, *, mapped="v_bv", venues=None, handle_index=None, **kwargs):
    return evaluate_attribution_dispute(
        mapped_venue_id=mapped, location_text=location_text,
        venues=venues if venues is not None else CATALOG,
        handle_index=handle_index if handle_index is not None else HANDLE_INDEX,
        promoter_handle="beerdock_recife", **kwargs,
    )


class TestTheRungsThatDoDispute:
    def test_a_handle_mention_of_another_branch_disputes(self):
        verdict = _dispute("@beerdock.casaforte")
        assert verdict is not None
        assert verdict.method == METHOD_HANDLE_MENTION
        assert verdict.target_venue_id == "v_cf"
        assert verdict.target_venue_name == "BeerDock Casa Forte"
        assert verdict.has_target is True

    def test_a_neighbourhood_inside_a_sibling_branch_s_address_disputes(self):
        # The production string the RCA measured: 20 of 44 live rows at
        # "BeerDock Boa Viagem" carry location_text = 'CASA FORTE'.
        verdict = _dispute("CASA FORTE")
        assert verdict is not None
        assert verdict.method == METHOD_NEIGHBOURHOOD_MATCH
        assert verdict.target_venue_id == "v_cf"

    def test_a_location_tag_naming_another_venue_disputes(self):
        verdict = _dispute(
            "nosso espaço", location_tag={"name": "Maria Café", "lat": -8.06, "lng": -34.87},
        )
        assert verdict is not None
        assert verdict.target_venue_id == "v_mc"

    def test_the_verdict_carries_the_ranked_candidates_an_operator_accepts(self):
        verdict = _dispute("CASA FORTE")
        assert verdict.candidates, "a flagged dispute must offer a ranked candidate"
        assert verdict.candidates[0].venue_id == "v_cf"


class TestTheRungsThatNeverDispute:
    def test_a_name_match_never_disputes(self):
        # `260806_venue-post-multi-event.md` §D's whole objection: rung 4 is
        # the fuzzy, scored rung, and re-attributing a venue's own post on it
        # is exactly what produced the `@mahalilacafe` -> "Maria Café" links.
        assert _dispute("nosso cafe") is None

    def test_a_caption_mention_never_disputes_because_the_caption_is_never_read(self):
        # Rung 5 is structurally unreachable here: this module passes
        # caption=None into the ladder. Proven by the fact that text naming
        # NO venue produces no verdict even though a caption naming Casa
        # Forte would resolve one.
        assert _dispute("vem pro rolê") is None

    def test_the_mapped_venue_s_own_neighbourhood_never_disputes(self):
        assert _dispute("BOA VIAGEM") is None

    def test_the_mapped_venue_s_own_handle_never_disputes(self):
        assert _dispute("@beerdock_recife") is None

    def test_resolving_to_the_mapped_venue_itself_is_not_a_dispute(self):
        assert _dispute("@beerdock.casaforte", mapped="v_cf") is None

    def test_empty_location_text_never_disputes(self):
        assert _dispute(None) is None
        assert _dispute("") is None

    def test_no_mapped_venue_never_disputes(self):
        assert _dispute("CASA FORTE", mapped=None) is None

    def test_an_operator_edited_venue_never_disputes(self):
        assert _dispute("CASA FORTE", operator_edited_fields=["venue_id"]) is None

    def test_an_operator_edit_of_some_other_field_still_disputes(self):
        assert _dispute("CASA FORTE", operator_edited_fields=["title"]) is not None


class TestTheUnknownBranch:
    def test_an_unknown_handle_disputes_with_no_target(self):
        verdict = _dispute("@beerdock.madalena", handle_index={"beerdock_recife": "v_bv"})
        assert verdict is not None
        assert verdict.method == METHOD_VENUE_NOT_IN_CATALOG
        assert verdict.has_target is False
        assert verdict.target_venue_id is None

    def test_the_unknown_branch_verdict_keeps_the_text_for_the_acquisition_backlog(self):
        verdict = _dispute("@beerdock.madalena", handle_index={"beerdock_recife": "v_bv"})
        assert verdict.location_text == "@beerdock.madalena"


class TestBrandRoot:
    def test_the_brand_root_collects_every_branch_sharing_the_leading_token(self):
        ids = {v.venue_id for v in brand_root_venues("v_bv", CATALOG)}
        assert ids == {"v_bv", "v_cf", "v_md"}

    def test_an_unrelated_venue_is_never_in_the_brand_root(self):
        assert "v_mc" not in {v.venue_id for v in brand_root_venues("v_bv", CATALOG)}

    def test_a_generic_leading_token_never_forms_a_brand_root(self):
        # "Bar do Zé" and "Bar Central" are not branches of one chain, and a
        # brand root built on the word `bar` would hand rung 3 a candidate
        # set it was never meant to see.
        root = brand_root_venues("v_bz", [BAR_DO_ZE, BAR_CENTRAL])
        assert [v.venue_id for v in root] == ["v_bz"]

    def test_a_venue_with_no_siblings_yields_a_single_member_root(self):
        assert [v.venue_id for v in brand_root_venues("v_mc", CATALOG)] == ["v_mc"]

    def test_a_single_member_brand_root_disables_the_neighbourhood_rung(self):
        # `resolve_event_venue` gates rung 3 on `len(same_account_venues) >= 2`
        # — with no sibling there is no branch question to answer, so "CASA
        # FORTE" against a lone venue must NOT dispute.
        assert _dispute("CASA FORTE", venues=[BOA_VIAGEM, MARIA_CAFE]) is None

    def test_an_unknown_mapped_venue_yields_an_empty_root(self):
        assert brand_root_venues("v_nope", CATALOG) == []


class TestConfig:
    def test_the_shipped_defaults_change_nothing(self):
        config = AttributionDisputeConfig()
        assert config.action == DISPUTE_ACTION_FLAG == DEFAULT_DISPUTE_ACTION
        assert config.withhold_enabled is False is DEFAULT_DISPUTE_WITHHOLD_ENABLED
        assert config.reattributes is False

    def test_no_redis_means_the_shipped_defaults(self):
        config = load_attribution_dispute_config(None)
        assert config.action == DISPUTE_ACTION_FLAG
        assert config.withhold_enabled is False

    def test_the_action_validator_accepts_only_the_two_values(self):
        assert validate_dispute_action_config("flag") == DISPUTE_ACTION_FLAG
        assert validate_dispute_action_config("reattribute") == DISPUTE_ACTION_REATTRIBUTE
        with pytest.raises(ValueError):
            validate_dispute_action_config("detach")
        with pytest.raises(ValueError):
            validate_dispute_action_config(True)

    def test_the_withhold_validator_rejects_a_string(self):
        # `bool("false")` -> True is the coercion this whole validated-read
        # path exists to prevent (260814 §D).
        with pytest.raises(TypeError):
            validate_dispute_withhold_enabled_config("false")
        assert validate_dispute_withhold_enabled_config(True) is True

    def test_a_stored_string_where_a_bool_belongs_falls_back_rather_than_coercing(self):
        class _FakeRedis:
            def get(self, key):
                return '"false"'

        config = load_attribution_dispute_config(_FakeRedis())
        assert config.withhold_enabled is False

    def test_a_valid_stored_value_is_honoured(self):
        import json

        from app.services.event_attribution_dispute import (
            ADMIN_CONFIG_DISPUTE_ACTION_KEY, ADMIN_CONFIG_DISPUTE_WITHHOLD_KEY,
        )

        class _FakeRedis:
            def get(self, key):
                if key == ADMIN_CONFIG_DISPUTE_ACTION_KEY:
                    return json.dumps("reattribute")
                if key == ADMIN_CONFIG_DISPUTE_WITHHOLD_KEY:
                    return json.dumps(True)
                return None

        config = load_attribution_dispute_config(_FakeRedis())
        assert config.reattributes is True
        assert config.withhold_enabled is True


class TestFoldReviewReason:
    def test_a_token_is_appended_to_an_existing_set(self):
        assert fold_review_reason("missing_date", "location_text_disputes_venue") == (
            "missing_date; location_text_disputes_venue"
        )

    def test_a_token_already_present_is_never_repeated(self):
        assert fold_review_reason(
            "location_text_disputes_venue", "location_text_disputes_venue",
        ) == "location_text_disputes_venue"

    def test_an_empty_existing_reason_yields_the_token_alone(self):
        assert fold_review_reason(None, "location_text_disputes_venue") == (
            "location_text_disputes_venue"
        )
