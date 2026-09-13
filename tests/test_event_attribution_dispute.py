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
    DEFAULT_BRAND_ROOT_STOPWORDS,
    DEFAULT_DISPUTE_ACTION,
    MAX_BRAND_ROOT_VENUES,
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


# ── the ladder's one superlinear rung, and the proof it is safe to skip ────
@pytest.fixture
def full_ladder(monkeypatch):
    """Forces `identity_methods_only=False` for the duration of a test, so
    the SAME production functions produce both verdicts and the ONLY
    difference is the flag. Nothing about the verdict logic is reproduced
    here — that would test the copy, not the code."""
    import app.services.event_attribution_dispute as mod

    real = mod.resolve_event_venue

    def _full(**kwargs):
        kwargs["identity_methods_only"] = False
        return real(**kwargs)

    monkeypatch.setattr(mod, "resolve_event_venue", _full)


# Every shape the fast path has to get right, including the two that decide
# it: a name-matchable text with NO handle (rung 4 skipped, and its
# `name_match` answer was going to be discarded anyway) and the same text
# WITH an unrecognized handle (rung 4 must still run, because returning
# early there is what suppresses the `venue_not_in_catalog` sentinel).
_EQUIVALENCE_TEXTS = [
    None, "", "   ",
    "CASA FORTE", "BOA VIAGEM", "MADALENA",
    "@beerdock.casaforte", "@beerdock.madalena", "@beerdock_recife",
    "nossa unidade de Boa Viagem",
    "BeerDock Boa Viagem — Av. Cons. Aguiar, 1000",
    "Av. Cons. Aguiar, 1000 — Boa Viagem, Recife",
    "Maria Café",                      # name-matchable, NO handle
    "@naoexiste Maria Café",           # name-matchable, unrecognized handle
    "@naoexiste",                      # handle only, nothing to name-match
    "@naoexiste CASA FORTE",           # unrecognized handle AND a rung-3 hit
    "nosso cafe", "vem pro rolê", "Rua da Aurora, 123",
    "ZS • @mahalilacafe",
]


def _verdict_shape(verdict):
    if verdict is None:
        return None
    return (verdict.method, verdict.target_venue_id, verdict.location_text)


@pytest.mark.parametrize("location_text", _EQUIVALENCE_TEXTS)
@pytest.mark.parametrize("mapped", ["v_bv", "v_cf", "v_mc"])
def test_skipping_rung_four_changes_no_verdict(location_text, mapped, request):
    """The hotfix's correctness guarantee, differential-tested: for every
    text and every mapped venue, the verdict with the ladder's superlinear
    rung skipped is IDENTICAL to the verdict with it run."""
    fast = _verdict_shape(_dispute(location_text, mapped=mapped))

    request.getfixturevalue("full_ladder")
    slow = _verdict_shape(_dispute(location_text, mapped=mapped))

    assert fast == slow, (location_text, mapped, fast, slow)


def test_a_name_matchable_text_with_an_unrecognized_handle_still_runs_rung_four(
    monkeypatch,
):
    """The guard that makes the skip safe. Rung 4 returning early is what
    suppresses `venue_not_in_catalog`; with an unrecognized handle present
    that suppression is observable, so the rung must still run."""
    calls = []
    import app.services.event_venue_resolution as evr

    real = evr.name_similarity
    monkeypatch.setattr(
        evr, "name_similarity",
        lambda *a, **kw: (calls.append(a), real(*a, **kw))[1],
    )
    _dispute("@naoexiste Maria Café")
    assert calls, "rung 4 must run when an unrecognized handle could be suppressed"


def test_a_text_with_no_handle_never_reaches_rung_four(monkeypatch):
    """The other half: with nothing to suppress, the rung is unobservable
    and is not computed. This is the entire performance fix."""
    calls = []
    import app.services.event_venue_resolution as evr

    real = evr.name_similarity
    monkeypatch.setattr(
        evr, "name_similarity",
        lambda *a, **kw: (calls.append(a), real(*a, **kw))[1],
    )
    _dispute("Maria Café")
    assert calls == [], "rung 4 ran even though its answer is discarded here"


def test_the_full_ladder_is_still_the_default_for_every_other_caller(monkeypatch):
    """`identity_methods_only` defaults False, so the promoter/extraction
    callers — which DO act on `name_match` and persist its ranked candidates
    — are untouched."""
    calls = []
    import app.services.event_venue_resolution as evr

    real = evr.name_similarity
    monkeypatch.setattr(
        evr, "name_similarity",
        lambda *a, **kw: (calls.append(a), real(*a, **kw))[1],
    )
    result = evr.resolve_event_venue(
        caption=None, location_text="Maria Café", location_tag=None,
        promoter_handle="beerdock_recife", venues=CATALOG, handle_index=HANDLE_INDEX,
    )
    assert calls, "the default path must still score every venue"
    assert result.candidates, "and must still return its ranked candidates"


# ── the brand root must stay a BRAND root ──────────────────────────────────
# Real catalog strings throughout. `brand_root_venues` is rung 3's entire
# safety argument — the rung does bounded substring matching against
# "this account's own branches", and that is only safe if the set really is
# a handful of branches. Measured against the live catalog (2,694 venues),
# it was not: `bar` was caught by the vocabulary, but `casa` gave "Casa
# Bacurau" 33 "siblings" across six cities and `villa` gave 12 including
# Berlin and Munich.
_CASA_FAMILY = [
    VenueLite(venue_id=f"v_casa_{i}", venue_name=name, address=addr)
    for i, (name, addr) in enumerate([
        ("Casa Bacurau", "Rua Capitão Lima, 100 - Santo Amaro, Recife - PE"),
        ("Casa de Jorge Amado", "Largo do Pelourinho - Salvador - BA"),
        ("Casa da Felicidade Arte & Música & Gastronomia", "Fortaleza - CE"),
        ("Casa Teresa&Jorge", "Rio de Janeiro - RJ"),
        ("Casa de Show Lugar Comum", "Olinda - PE"),
        ("Casa Rosa", "São Paulo - SP"),
        ("Casa Amarela", "Teresina - PI"),
        ("Casa Verde", "João Pessoa - PB"),
        ("Casa Azul", "Natal - RN"),
    ])
]
_VILLA_FAMILY = [
    VenueLite(venue_id=f"v_villa_{i}", venue_name=name, address=addr)
    for i, (name, addr) in enumerate([
        ("Villa Setúbal Botequim", "Setúbal, Recife - PE"),
        ("Villa Neukölln", "Berlin, Germany"),
        ("Villa Country", "São Paulo - SP"),
        ("Villa Blué Restaurante no recreio", "Rio de Janeiro - RJ"),
    ])
]
# A real national chain, at the size the live catalog actually holds.
_COCO_BAMBU = [
    VenueLite(venue_id=f"v_coco_{i}", venue_name=name, address=addr)
    for i, (name, addr) in enumerate([
        ("Coco Bambu", "Av. Boa Viagem, Recife - PE"),
        ("Coco Bambu Dom Pastel", "Dom Pastel, Recife - PE"),
        ("Coco Bambu Shopping Recife", "Shopping Recife, Boa Viagem - PE"),
        ("Coco Bambu Derby", "Derby, Recife - PE"),
        ("Coco Bambu Teresina", "Teresina - PI"),
        ("Coco Bambu Fortaleza", "Fortaleza - CE"),
    ])
]


class TestTheBrandRootStaysBounded:
    def test_casa_bacurau_has_no_siblings(self):
        """The live bug: 33 "siblings" across six cities, and Casa Bacurau is
        one of the venues in scope for the attribution repair."""
        root = brand_root_venues("v_casa_0", _CASA_FAMILY)
        assert [v.venue_name for v in root] == ["Casa Bacurau"]

    def test_villa_has_no_siblings(self):
        root = brand_root_venues("v_villa_0", _VILLA_FAMILY)
        assert [v.venue_name for v in root] == ["Villa Setúbal Botequim"]

    def test_the_beerdock_chain_is_untouched(self):
        root = brand_root_venues("v_bv", CATALOG)
        assert {v.venue_name for v in root} == {
            "BeerDock Boa Viagem", "BeerDock Casa Forte", "BeerDock Madalena",
        }

    def test_a_real_six_branch_chain_is_not_clipped_by_the_ceiling(self):
        """The ceiling has to clear the largest GENUINE chain in the catalog,
        which is Coco Bambu at 6 — measured, not assumed."""
        root = brand_root_venues("v_coco_0", _COCO_BAMBU)
        assert len(root) == 6

    def test_the_ceiling_collapses_an_oversized_root_whatever_the_vocabulary_knows(self):
        """Defense in depth: a hand-maintained word list is always incomplete
        for the NEXT unforeseen generic word, so size alone must also be
        evidence. `zzz` is in no vocabulary."""
        big = [
            VenueLite(venue_id=f"v_z_{i}", venue_name=f"Zzz Lugar {i}", address=f"Rua {i}")
            for i in range(MAX_BRAND_ROOT_VENUES + 1)
        ]
        assert len(brand_root_venues("v_z_0", big)) == 1

    def test_a_root_exactly_at_the_ceiling_survives(self):
        big = [
            VenueLite(venue_id=f"v_z_{i}", venue_name=f"Zzz Lugar {i}", address=f"Rua {i}")
            for i in range(MAX_BRAND_ROOT_VENUES)
        ]
        assert len(brand_root_venues("v_z_0", big)) == MAX_BRAND_ROOT_VENUES

    def test_the_measured_generic_venue_words_never_form_a_root(self):
        """Every one of these leads 4+ unrelated venues in the live catalog."""
        for word in ("restaurante", "boteco", "casa", "villa", "the", "cervejaria",
                     "teatro", "praca", "boate", "cafe", "espaco", "quintal"):
            family = [
                VenueLite(venue_id=f"v_{word}_{i}",
                          venue_name=f"{word.title()} Exemplo {i}", address=f"Rua {i}")
                for i in range(3)
            ]
            assert len(brand_root_venues(f"v_{word}_0", family)) == 1, word

    def test_the_title_dedup_vocabulary_is_deliberately_untouched(self):
        """`DEFAULT_GENERIC_VOCABULARY` feeds `distinctive_set`, the TITLE
        merge bar — live with auto-merge on and pinned by 260812's
        false-positive corpus. Venue-name words belong in their own list;
        adding "casa" there would change which EVENT TITLES merge."""
        from app.services.event_dedup import DEFAULT_GENERIC_VOCABULARY

        for word in ("casa", "villa", "restaurante", "boteco"):
            assert word not in DEFAULT_GENERIC_VOCABULARY, word
        assert "casa" in DEFAULT_BRAND_ROOT_STOPWORDS
        assert "villa" in DEFAULT_BRAND_ROOT_STOPWORDS

    def test_an_unbounded_root_cannot_produce_a_dispute(self):
        """The point of all of it: with no sibling set, rung 3 cannot fire,
        so a generic leading word can never re-attribute anything."""
        assert evaluate_attribution_dispute(
            mapped_venue_id="v_casa_0", location_text="Salvador",
            venues=_CASA_FAMILY, handle_index={}, promoter_handle="casabacurau",
        ) is None


class TestRungThreeMatchesASiblingsName:
    """`Beerdock Casa Forte`'s stored address is "R. dos Arcos 1745 - Poço da
    Panela Recife - PE" — its geocoded neighbourhood is Poço da Panela while
    the branch is commercially Casa Forte. Address-only matching therefore
    found NOTHING, and the repair proposed zero changes for the case it was
    built around."""

    def test_the_beerdock_case_resolves_on_the_sibling_s_name(self):
        verdict = _dispute("CASA FORTE")
        assert verdict is not None
        assert verdict.method == METHOD_NEIGHBOURHOOD_MATCH
        assert verdict.target_venue_name == "BeerDock Casa Forte"

    def test_it_works_even_when_no_address_mentions_the_neighbourhood(self):
        catalog = [
            VenueLite(venue_id="v_a", venue_name="BeerDock Boa Viagem",
                      address="Rua Professor Eduardo Wanderley Filho, 242 - Boa Viagem"),
            VenueLite(venue_id="v_b", venue_name="Beerdock Casa Forte",
                      address="R. dos Arcos 1745 - Poço da Panela Recife - PE"),
        ]
        verdict = evaluate_attribution_dispute(
            mapped_venue_id="v_a", location_text="CASA FORTE",
            venues=catalog, handle_index={}, promoter_handle="beerdock_recife",
        )
        assert verdict is not None and verdict.target_venue_id == "v_b"
        assert verdict.candidates[0].evidence["matched_on"] == "venue_name"

    def test_an_address_hit_still_reports_itself_as_an_address_hit(self):
        verdict = _dispute("Rui Barbosa")
        assert verdict is not None
        assert verdict.candidates[0].evidence["matched_on"] == "address"

    def test_the_shared_brand_prefix_can_never_resolve_a_branch(self):
        """"BeerDock" matches EVERY branch's name, so the ladder sees more
        than one candidate and refuses rather than guessing. This is what
        stops the brand word itself from re-attributing anything."""
        assert _dispute("BeerDock") is None
        assert _dispute("Casa BeerDock") is None

    def test_matching_the_mapped_venue_s_own_name_is_not_a_dispute(self):
        assert _dispute("BOA VIAGEM") is None
        assert _dispute("BeerDock Boa Viagem") is None

    def test_two_unrelated_venues_sharing_a_word_still_never_dispute(self):
        """The §D guarantee, restated for the name check: this must not
        become fuzzy cross-brand name matching by another door. "Maria Café"
        and "Maria Antonieta" share a leading word, and `maria` is a measured
        generic — so they are not siblings and cannot match each other."""
        family = [
            VenueLite(venue_id="v_m1", venue_name="Maria Café", address="R. do Sol, 7"),
            VenueLite(venue_id="v_m2", venue_name="Maria Antonieta", address="R. Nova, 3"),
        ]
        assert evaluate_attribution_dispute(
            mapped_venue_id="v_m1", location_text="Antonieta",
            venues=family, handle_index={}, promoter_handle="mariacafe",
        ) is None

    def test_a_name_match_across_the_whole_catalog_is_still_never_a_dispute(self):
        """Rung 4 remains excluded. "Maria Café" is a real catalog venue and
        is NOT a BeerDock sibling, so no amount of name resemblance reaches
        a verdict."""
        assert _dispute("Maria Café") is None
        assert _dispute("nosso cafe") is None

    def test_a_too_short_text_never_matches_a_name(self):
        assert _dispute("BV") is None
