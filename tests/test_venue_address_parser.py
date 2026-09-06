"""Unit coverage for app.services.venue_address_parser — Phase 1 of
plans/260906_address-components-backfill.md.

Every case here is a real, verbatim production `raw_text` string from the
plan's own Evidence/worked-example sections and the executing agent's brief,
not a synthetic simplification. Step 4's UF-adjacent-dash strip is the single
most important line in the parser (an earlier draft that omitted it would
extract NOTHING for ~93% of the catalog — the dominant space-joined format),
so it gets its own explicit non-empty-blob assertions independent of the
higher-level parse_address behavior.
"""
from __future__ import annotations

from app.services.venue_address_parser import (
    clean_tail,
    extract_blob,
    extract_postal_code,
    extract_uf_and_tail,
    parse_address,
    split_street_and_blob,
)

_NITEROI = {"name": "Niterói", "lat": -22.8832, "lng": -43.1034, "ambiguous": False}
_SAO_PAULO = {"name": "São Paulo", "lat": -23.5505, "lng": -46.6333, "ambiguous": False}
_RECIFE = {"name": "Recife", "lat": -8.0476, "lng": -34.8770, "ambiguous": False}
_OLINDA = {"name": "Olinda", "lat": -7.9922, "lng": -34.8451, "ambiguous": False}
# Real Roraima capital coordinates — used by the ambiguity-guard cases.
_BOA_VISTA_RR_AMBIGUOUS = {
    "name": "Boa Vista", "lat": 2.8235, "lng": -60.6758, "ambiguous": True,
}
_BOA_VISTA_RR_PLAIN = {
    "name": "Boa Vista", "lat": 2.8235, "lng": -60.6758, "ambiguous": False,
}

_BASE_VOCAB = [_NITEROI, _SAO_PAULO, _RECIFE, _OLINDA]


# ── step 4: the UF-adjacent-dash strip (the one bug that would zero out
#    93% of the catalog) — each of these must yield a NON-EMPTY blob ──────
class TestStep4UfAdjacentDashStrip:
    def test_paulo_cesar_niteroi_blob_is_not_empty(self):
        raw = "R. Dr. Paulo César 225 - Santa Rosa Niterói - RJ 24220-400 Brazil"
        assert extract_blob(raw) == "Santa Rosa Niterói"

    def test_abdon_batista_santo_amaro_blob_is_not_empty(self):
        raw = "R. Abdon Batista, 300 - Santo Amaro Recife - PE 50100-460 Brazil"
        assert extract_blob(raw) == "Santo Amaro Recife"

    def test_herculano_bandeira_pina_blob_is_not_empty(self):
        raw = "Av. Herculano Bandeira, 21 - Pina Recife - PE 51011-000 Brazil"
        assert extract_blob(raw) == "Pina Recife"

    def test_comma_format_casa_caiada_olinda_blob_is_not_empty(self):
        raw = "R. Carmelita Muniz de Araújo, 225 - Casa Caiada, Olinda - PE, 53130-645, Brazil"
        assert extract_blob(raw) == "Casa Caiada, Olinda"

    def test_clean_tail_strips_trailing_dash_then_rstrips(self):
        assert clean_tail("R. X 225 - Santa Rosa Niterói - ") == "R. X 225 - Santa Rosa Niterói"

    def test_clean_tail_strips_trailing_comma_variant(self):
        assert clean_tail("R. X 225 - Santa Rosa Niterói ,") == "R. X 225 - Santa Rosa Niterói"

    def test_clean_tail_is_a_no_op_when_nothing_trails(self):
        assert clean_tail("R. X 225 - Santa Rosa Niterói") == "R. X 225 - Santa Rosa Niterói"


# ── the ten required production strings, end to end ───────────────────────
class TestRequiredProductionStrings:
    def test_paulo_cesar_niteroi(self):
        raw = "R. Dr. Paulo César 225 - Santa Rosa Niterói - RJ 24220-400 Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-22.88, venue_lng=-43.10)
        assert result == {
            "street": "R. Dr. Paulo César 225",
            "neighborhood": "Santa Rosa",
            "city": "Niterói",
            "postal_code": "24220-400",
        }

    def test_abdon_batista_santo_amaro_recife(self):
        raw = "R. Abdon Batista, 300 - Santo Amaro Recife - PE 50100-460 Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result == {
            "street": "R. Abdon Batista, 300",
            "neighborhood": "Santo Amaro",
            "city": "Recife",
            "postal_code": "50100-460",
        }

    def test_comma_delimited_casa_caiada_olinda(self):
        raw = "R. Carmelita Muniz de Araújo, 225 - Casa Caiada, Olinda - PE, 53130-645, Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-7.99, venue_lng=-34.84)
        assert result == {
            "street": "R. Carmelita Muniz de Araújo, 225",
            "neighborhood": "Casa Caiada",
            "city": "Olinda",
            "postal_code": "53130-645",
        }

    def test_comma_delimited_city_not_yet_in_vocabulary_still_splits(self):
        """The comma format needs no vocabulary lookup to split correctly —
        only to validate a match found some other way. Olinda resolves here
        even absent from the vocabulary entirely."""
        raw = "R. Carmelita Muniz de Araújo, 225 - Casa Caiada, Olinda - PE, 53130-645, Brazil"
        result = parse_address(raw, [_RECIFE], venue_lat=-7.99, venue_lng=-34.84)
        assert result["neighborhood"] == "Casa Caiada"
        assert result["city"] == "Olinda"

    def test_herculano_bandeira_pina_recife(self):
        raw = "Av. Herculano Bandeira, 21 - Pina Recife - PE 51011-000 Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.08, venue_lng=-34.90)
        assert result == {
            "street": "Av. Herculano Bandeira, 21",
            "neighborhood": "Pina",
            "city": "Recife",
            "postal_code": "51011-000",
        }

    def test_no_bairro_stated_space_joined_no_dash_before_uf(self):
        raw = "R. da Guia, 207 - Recife PE 50030-220 Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result == {
            "street": "R. da Guia, 207",
            "neighborhood": None,
            "city": "Recife",
            "postal_code": "50030-220",
        }

    def test_bairro_is_also_a_capital_name(self):
        """"Boa Vista" here is the BAIRRO (not the matched city) — vocabulary
        never checks the bairro half, so this resolves independent of
        whether "Boa Vista" the city name is present/ambiguous anywhere."""
        raw = "R. Imperatriz Teresa Cristina 218 - Boa Vista Recife - PE 50060-120 Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result["neighborhood"] == "Boa Vista"
        assert result["city"] == "Recife"
        assert result["postal_code"] == "50060-120"

    def test_bairro_contains_a_city_name(self):
        raw = "R. Claudino José de Lima, 138 - Jardim São Paulo, Recife - PE, 50920-280"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result["neighborhood"] == "Jardim São Paulo"
        assert result["city"] == "Recife"
        assert result["postal_code"] == "50920-280"

    def test_guard_rejects_cohab_bairro(self):
        raw = "COHAB Recife - PE 51340-670 Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result == {
            "street": None,
            "neighborhood": None,
            "city": "Recife",
            "postal_code": "51340-670",
        }

    def test_guard_rejects_shopping_mall_bairro(self):
        raw = "Shopping Riomar Pina Recife - PE 51110-131, Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result["neighborhood"] is None
        # "Recife" is still the correct trailing city even though the
        # leading remainder ("Shopping Riomar Pina") is rejected as a bairro.
        assert result["city"] == "Recife"

    def test_non_brazilian_address_resolves_nothing(self):
        raw = "1 Sky Garden Walk London EC3M 8AF United Kingdom"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=51.51, venue_lng=-0.09)
        assert result == {
            "street": None, "neighborhood": None, "city": None, "postal_code": None,
        }


# ── step 8's plausibility guard: BOTH directions of the SAME rule ─────────
class TestStep8PlausibilityGuardBothDirections:
    def test_jardim_sao_paulo_comma_format_accepted(self):
        raw = "R. Claudino José de Lima, 138 - Jardim São Paulo, Recife - PE, 50920-280"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result["neighborhood"] == "Jardim São Paulo"

    def test_boa_vista_recife_format_a_accepted(self):
        raw = "R. Imperatriz Teresa Cristina 218 - Boa Vista Recife - PE 50060-120 Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result["neighborhood"] == "Boa Vista"

    def test_no_leading_words_is_not_a_guard_rejection(self):
        raw = "R. da Guia, 207 - Recife PE 50030-220 Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result["neighborhood"] is None  # empty, not guard-rejected
        assert result["city"] == "Recife"

    def test_cohab_rejected_stoplist_hit_city_kept(self):
        raw = "COHAB Recife - PE 51340-670 Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result["neighborhood"] is None
        assert result["city"] == "Recife"

    def test_jardim_santa_maria_sao_paulo_no_street_accepted(self):
        """3 words, no digit, no stoplist hit -> accepted even with no
        street/number anchoring it — the SAME rule that rejects COHAB."""
        raw = "Jardim Santa Maria São Paulo - SP 04120-000 Brazil"
        result = parse_address(raw, [_SAO_PAULO], venue_lat=-23.55, venue_lng=-46.63)
        assert result == {
            "street": None,
            "neighborhood": "Jardim Santa Maria",
            "city": "São Paulo",
            "postal_code": "04120-000",
        }

    def test_guard_only_applies_when_street_is_none(self):
        """A 4-word bairro WOULD be guard-rejected with no street before it,
        but a real street/number anchors it, so the guard never runs."""
        raw = "Av. Um, 10 - Um Dois Tres Quatro Recife - PE 50000-000 Brazil"
        result = parse_address(raw, _BASE_VOCAB, venue_lat=-8.05, venue_lng=-34.88)
        assert result["street"] == "Av. Um, 10"
        assert result["neighborhood"] == "Um Dois Tres Quatro"
        assert result["city"] == "Recife"


# ── longest-suffix-first: a look-alike city name inside the bairro ────────
class TestLongestSuffixFirst:
    def test_true_trailing_city_wins_over_lookalike_in_bairro(self):
        raw = "Av. Norte, 900 - Boa Vista Recife - PE 50070-100 Brazil"
        result = parse_address(raw, [_RECIFE], venue_lat=-8.05, venue_lng=-34.88)
        assert result["neighborhood"] == "Boa Vista"
        assert result["city"] == "Recife"

    def test_three_word_capital_matched_whole_not_its_last_word(self):
        """Guards against seizing just "Paulo" out of "São Paulo" — the
        n=2 suffix must be tried (and win) before falling back to n=1."""
        raw = "R. Teste, 5 - Bairro X São Paulo - SP 01000-000 Brazil"
        result = parse_address(raw, [_SAO_PAULO], venue_lat=-23.55, venue_lng=-46.63)
        assert result["city"] == "São Paulo"
        assert result["neighborhood"] == "Bairro X"


# ── step 9: the ambiguity guard ────────────────────────────────────────────
class TestAmbiguityGuard:
    def test_ambiguous_match_accepted_when_coordinates_corroborate(self):
        raw = "Estr. do Sítio, s/n - Boa Vista - RR 69300-000 Brazil"
        result = parse_address(
            raw, [_BOA_VISTA_RR_AMBIGUOUS], venue_lat=2.85, venue_lng=-60.70,
        )
        assert result["city"] == "Boa Vista"

    def test_ambiguous_match_rejected_when_coordinates_do_not_corroborate(self):
        raw = "Av. Norte, 900 - Boa Vista - PE 50070-100 Brazil"
        result = parse_address(
            raw, [_BOA_VISTA_RR_AMBIGUOUS], venue_lat=-8.05, venue_lng=-34.88,
        )
        assert result["city"] is None
        assert result["neighborhood"] is None

    def test_ambiguous_match_rejected_when_venue_coordinates_are_missing(self):
        raw = "Av. Norte, 900 - Boa Vista - PE 50070-100 Brazil"
        result = parse_address(raw, [_BOA_VISTA_RR_AMBIGUOUS], venue_lat=None, venue_lng=None)
        assert result["city"] is None

    def test_non_ambiguous_entry_skips_the_check_entirely(self):
        """A non-ambiguous vocabulary entry is trusted regardless of the
        venue's own coordinates (the fast path — most matches never need
        corroboration)."""
        raw = "Av. Norte, 900 - Boa Vista - PE 50070-100 Brazil"
        result = parse_address(raw, [_BOA_VISTA_RR_PLAIN], venue_lat=-8.05, venue_lng=-34.88)
        assert result["city"] == "Boa Vista"


# ── step 6's guard against a street-comma masquerading as the real one ────
class TestFormatBStreetCommaGuard:
    def test_digit_in_city_half_falls_through_to_format_a(self):
        raw = "R. Teste, 12, Loja 3 - Bairro X Recife - PE 50000-000 Brazil"
        result = parse_address(raw, [_RECIFE], venue_lat=-8.05, venue_lng=-34.88)
        # The comma split's city half ("Bairro X Recife") is fine on its own,
        # but the GUARD is exercised by putting the offending comma INSIDE
        # blob itself:
        raw2 = "R. Teste, 12 - Bairro X, Sala 3 Recife - PE 50000-000 Brazil"
        result2 = parse_address(raw2, [_RECIFE], venue_lat=-8.05, venue_lng=-34.88)
        assert result2["city"] == "Recife"

    def test_over_four_words_in_city_half_falls_through_to_format_a(self):
        raw = "R. Teste, 12 - Bairro X, Um Dois Tres Quatro Cinco Recife - PE 50000-000 Brazil"
        result = parse_address(raw, [_RECIFE], venue_lat=-8.05, venue_lng=-34.88)
        assert result["city"] == "Recife"

    def test_paripueira_production_string_recovers_sem_numero_marker(self):
        """The confirmed production defect: a comma-format city half that is
        JUST the sem-numero marker ("S/N" — no street number) plus the real
        city name must have the marker stripped before being judged, not
        accepted as if "S/N" were part of the city name. Real verbatim
        production `raw_text`; "Paripueira" is deliberately absent from the
        vocabulary passed here — recovery-then-heuristic-accept must not
        require a vocabulary hit, exactly like every other Format-B result."""
        raw = (
            "Praia de Costabrava R. Projetada Sv Vinte e Três, S/N Paripueira"
            " - AL 57935-000, Brazil"
        )
        result = parse_address(raw, [_RECIFE], venue_lat=-9.55, venue_lng=-35.45)
        assert result["city"] == "Paripueira"
        assert result["postal_code"] == "57935-000"

    def test_sem_numero_marker_alone_recovers_to_empty_and_falls_through(self):
        """A nonsense short fragment: the marker recovers to an EMPTY string
        when no real city name follows it. That must reject the split
        (never accept "S/N" itself as a city) and fall through to Format A
        — which also finds no match here, so the row is correctly left
        unresolved rather than given a wrong answer."""
        raw = "R. Teste, 45 - Vila Nova, S/N - AL 57935-000 Brazil"
        result = parse_address(raw, [_RECIFE, _OLINDA], venue_lat=-9.55, venue_lng=-35.45)
        assert result["city"] is None
        assert result["neighborhood"] is None

    def test_street_type_token_after_comma_falls_through_to_format_a(self):
        """A street/road word ("Rua") landing in the city half is a street
        fragment, not a city name, even though it has no digit and is only
        2 words (so the pre-fix guard would have accepted it)."""
        raw = "R. Teste, 45 - Bairro Y, Rua Grande - AL 57935-000 Brazil"
        result = parse_address(raw, [_RECIFE, _OLINDA], venue_lat=-9.55, venue_lng=-35.45)
        assert result["city"] is None

    def test_stoplist_word_after_comma_falls_through_to_format_a(self):
        """Step 8's mall/housing-complex stoplist, reused (not duplicated)
        for the city half: a bare stoplist word has no digit and is only
        1 word, so the pre-fix guard would have accepted it too."""
        raw = "R. Teste, 45 - Bairro Z, Shopping - AL 57935-000 Brazil"
        result = parse_address(raw, [_RECIFE, _OLINDA], venue_lat=-9.55, venue_lng=-35.45)
        assert result["city"] is None

    def test_bare_number_marker_recovers_then_vocabulary_hit_accepts(self):
        """The OTHER marker shape step 6 recovers, alongside "S/N": a bare
        street number leaking in front of the real city strips the same
        way, and a vocabulary hit then trusts the recovered name outright
        (step 3), independent of the heuristics in step 2."""
        raw = "R. Teste, 45 - Bairro W, 225 Niterói - AL 57935-000 Brazil"
        result = parse_address(raw, [_NITEROI], venue_lat=-22.88, venue_lng=-43.10)
        assert result["city"] == "Niterói"
        assert result["neighborhood"] == "Bairro W"

    def test_regression_casa_caiada_olinda_still_correct(self):
        """This guard must never re-reject a well-formed comma split just
        because its validation is new — "Olinda" has no marker to recover
        and hits no rejection heuristic, so it must resolve exactly as it
        did before this fix (and Olinda is still absent from the
        vocabulary passed here, proving no vocabulary hit is involved)."""
        raw = "R. Carmelita Muniz de Araújo, 225 - Casa Caiada, Olinda - PE, 53130-645, Brazil"
        result = parse_address(raw, [_RECIFE], venue_lat=-7.99, venue_lng=-34.84)
        assert result["neighborhood"] == "Casa Caiada"
        assert result["city"] == "Olinda"

    def test_regression_jardim_sao_paulo_recife_still_correct(self):
        raw = "R. Claudino José de Lima, 138 - Jardim São Paulo, Recife - PE, 50920-280"
        result = parse_address(raw, [_RECIFE, _OLINDA], venue_lat=-8.05, venue_lng=-34.88)
        assert result["neighborhood"] == "Jardim São Paulo"
        assert result["city"] == "Recife"


# ── postal code / UF extraction as independent primitives ────────────────
class TestPrimitives:
    def test_extract_postal_code_normalizes_missing_dash(self):
        assert extract_postal_code("... 50100460 Brazil") == "50100-460"

    def test_extract_postal_code_takes_the_last_match(self):
        assert extract_postal_code("50000-000 then later 50100-460") == "50100-460"

    def test_extract_postal_code_none_when_absent(self):
        assert extract_postal_code("no postal code here") is None

    def test_extract_uf_and_tail_finds_rightmost_uf(self):
        uf, tail = extract_uf_and_tail("R. X - Recife - PE 50000-000 Brazil")
        assert uf == "PE"
        assert tail == "R. X - Recife - "

    def test_extract_uf_and_tail_none_when_no_uf_token(self):
        uf, tail = extract_uf_and_tail("1 Sky Garden Walk London EC3M 8AF United Kingdom")
        assert uf is None
        assert tail is None

    def test_split_street_and_blob_no_dash_returns_none_street(self):
        street, blob = split_street_and_blob("COHAB Recife")
        assert street is None
        assert blob == "COHAB Recife"

    def test_split_street_and_blob_ignores_compound_word_hyphen(self):
        street, blob = split_street_and_blob("R. X, 10 - Bairro Ceará-Mirim")
        assert street == "R. X, 10"
        assert blob == "Bairro Ceará-Mirim"
