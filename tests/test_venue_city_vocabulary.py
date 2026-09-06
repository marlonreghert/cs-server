"""Unit coverage for app.services.venue_city_vocabulary — Phase 1 of
plans/260906_address-components-backfill.md.

Covers: the STATE_CAPITALS union read-path (an approval payload that omits
the 27 capitals must never drop them from what the parser consumes),
candidate-generation's frequency + geo-tightness gates, a synthetic
reproduction of the discovery-time Icaraí/Niterói over-merge (proving the
miner does NOT auto-promote a wrong multi-word merge — it is never applied
regardless of what it surfaces), the particle/prefix extension heuristic on
the real multi-word municipality names named in the plan's Evidence section,
and the ambiguity flag against a synthetic Boa-Vista-style collision.
"""
from __future__ import annotations

import pytest

from app.services.venue_city_vocabulary import (
    load_city_vocabulary,
    mine_city_vocabulary_candidates,
    validate_address_city_vocabulary_config,
)

_RECIFE = -8.0476, -34.8770


class _FakeAdminConfig:
    """Minimal `.get(key)` stand-in — the vocabulary module never calls
    `.set`, only `.get`."""

    def __init__(self, value=None):
        self._value = value

    def get(self, key):
        return self._value


def _row(raw_text, lat, lng):
    return {"raw_text": raw_text, "lat": lat, "lng": lng}


# ── load_city_vocabulary: the union read-path ─────────────────────────────
class TestLoadCityVocabularyUnion:
    def test_no_admin_config_service_yields_capitals_only(self):
        vocab = load_city_vocabulary(None)
        names = {e["name"] for e in vocab}
        assert "Recife" in names
        assert "São Paulo" in names
        assert len(vocab) == 27

    def test_missing_key_yields_capitals_only(self):
        vocab = load_city_vocabulary(_FakeAdminConfig(None))
        assert len(vocab) == 27

    def test_approval_payload_omitting_capitals_does_not_drop_them(self):
        """The core contract: address_city_vocabulary holds only NEW
        additions. An operator approving just "Maringá" must never regress
        the capitals-derived baseline."""
        cfg = _FakeAdminConfig([
            {"name": "Maringá", "lat": -23.4210, "lng": -51.9331, "ambiguous": False},
        ])
        vocab = load_city_vocabulary(cfg)
        names = {e["name"] for e in vocab}
        assert "Recife" in names
        assert "São Paulo" in names
        assert "Niterói" not in names  # not a capital, not in this payload either
        assert "Maringá" in names
        assert len(vocab) == 28

    def test_capitals_win_geography_on_a_name_collision(self):
        """An operator-submitted entry sharing a capital's name must never
        relocate it — the server owns coordinates (mirrors
        venue_eligibility's own posture)."""
        cfg = _FakeAdminConfig([
            {"name": "Recife", "lat": 0.0, "lng": 0.0, "ambiguous": False},
        ])
        vocab = load_city_vocabulary(cfg)
        recife = next(e for e in vocab if e["name"] == "Recife")
        assert (recife["lat"], recife["lng"]) == _RECIFE
        assert len(vocab) == 27  # no duplicate row created

    def test_ambiguous_flag_can_still_be_attached_to_a_capital(self):
        """The one way a capital becomes ambiguous for the live parser: an
        approved config entry sharing its name, carrying the flag. Geography
        still comes from the capital, not the config entry."""
        cfg = _FakeAdminConfig([
            {"name": "Boa Vista", "lat": 2.8235, "lng": -60.6758, "ambiguous": True},
        ])
        vocab = load_city_vocabulary(cfg)
        boa_vista = next(e for e in vocab if e["name"] == "Boa Vista")
        assert boa_vista["ambiguous"] is True
        assert boa_vista["lat"] == pytest.approx(2.8235)
        assert boa_vista["lng"] == pytest.approx(-60.6758)
        assert len(vocab) == 27

    def test_malformed_entries_are_skipped_not_fatal(self):
        cfg = _FakeAdminConfig(["not a dict", {"lat": 1, "lng": 1}, {"name": ""}])
        vocab = load_city_vocabulary(cfg)
        assert len(vocab) == 27  # every malformed entry ignored, capitals intact

    def test_a_raising_admin_config_degrades_to_capitals_only(self):
        class _Raising:
            def get(self, key):
                raise RuntimeError("redis is down")

        vocab = load_city_vocabulary(_Raising())
        assert len(vocab) == 27


# ── validate_address_city_vocabulary_config ────────────────────────────────
class TestValidateAddressCityVocabularyConfig:
    def test_valid_payload_normalizes_and_defaults_ambiguous_false(self):
        result = validate_address_city_vocabulary_config([
            {"name": "Maringá", "lat": -23.42, "lng": -51.93},
        ])
        assert result == [{"name": "Maringá", "lat": -23.42, "lng": -51.93, "ambiguous": False}]

    def test_rejects_non_list(self):
        with pytest.raises(ValueError):
            validate_address_city_vocabulary_config({"name": "x"})

    def test_rejects_empty_list(self):
        with pytest.raises(ValueError):
            validate_address_city_vocabulary_config([])

    def test_rejects_missing_name(self):
        with pytest.raises(ValueError):
            validate_address_city_vocabulary_config([{"lat": 1.0, "lng": -50.0}])

    def test_rejects_non_numeric_lat(self):
        with pytest.raises(ValueError):
            validate_address_city_vocabulary_config([{"name": "X", "lat": "north", "lng": -50.0}])

    def test_rejects_out_of_range_lat(self):
        with pytest.raises(ValueError):
            validate_address_city_vocabulary_config([{"name": "X", "lat": 89.0, "lng": -50.0}])

    def test_rejects_out_of_range_lng(self):
        with pytest.raises(ValueError):
            validate_address_city_vocabulary_config([{"name": "X", "lat": -10.0, "lng": 120.0}])

    def test_rejects_duplicate_name(self):
        with pytest.raises(ValueError):
            validate_address_city_vocabulary_config([
                {"name": "Maringá", "lat": -23.42, "lng": -51.93},
                {"name": "maringá", "lat": -23.40, "lng": -51.90},
            ])

    def test_rejects_non_bool_ambiguous(self):
        with pytest.raises(ValueError):
            validate_address_city_vocabulary_config(
                [{"name": "X", "lat": -10.0, "lng": -50.0, "ambiguous": "yes"}]
            )


# ── mine_city_vocabulary_candidates: candidate generation ──────────────────
class TestCandidateGeneration:
    def test_frequency_and_geo_tightness_gates_promote_a_real_new_city(self):
        rows = [
            _row("R. A, 1 - Zona Norte Maringá - PR 87013-000 Brazil", -23.420, -51.930),
            _row("R. B, 2 - Zona Sul Maringá - PR 87020-000 Brazil", -23.425, -51.935),
            _row("R. C, 3 - Centro Maringá - PR 87013-100 Brazil", -23.415, -51.925),
        ]
        result = mine_city_vocabulary_candidates(rows, current_vocabulary=[])
        names = {c["ngram"] for c in result["candidates"]}
        assert "Maringá" in names

    def test_below_frequency_threshold_is_not_promoted(self):
        rows = [
            _row("R. A, 1 - Zona Norte Maringá - PR 87013-000 Brazil", -23.420, -51.930),
            _row("R. B, 2 - Zona Sul Maringá - PR 87020-000 Brazil", -23.425, -51.935),
        ]
        result = mine_city_vocabulary_candidates(
            rows, current_vocabulary=[], min_distinct_remainders=3,
        )
        names = {c["ngram"] for c in result["candidates"]}
        assert "Maringá" not in names

    def test_geographically_scattered_rows_fail_the_tightness_gate(self):
        """Same trailing word, same frequency, but the points are nowhere
        near each other — a coincidental name collision, not a real city."""
        rows = [
            _row("R. A, 1 - Bairro1 Miramar - PE 50000-000 Brazil", -8.05, -34.88),
            _row("R. B, 2 - Bairro2 Miramar - RJ 20000-000 Brazil", -22.90, -43.20),
            _row("R. C, 3 - Bairro3 Miramar - RS 90000-000 Brazil", -30.03, -51.23),
        ]
        result = mine_city_vocabulary_candidates(rows, current_vocabulary=[])
        names = {c["ngram"] for c in result["candidates"]}
        assert "Miramar" not in names

    def test_a_resolved_row_is_never_fed_to_candidate_generation(self):
        """Once "Maringá" is already approved, these rows are no longer
        residual — the miner must not propose it (or anything else) from
        rows the live parser already resolves correctly."""
        vocab = [{"name": "Maringá", "lat": -23.42, "lng": -51.93, "ambiguous": False}]
        rows = [
            _row("R. A, 1 - Zona Norte Maringá - PR 87013-000 Brazil", -23.420, -51.930),
            _row("R. B, 2 - Zona Sul Maringá - PR 87020-000 Brazil", -23.425, -51.935),
            _row("R. C, 3 - Centro Maringá - PR 87013-100 Brazil", -23.415, -51.925),
        ]
        result = mine_city_vocabulary_candidates(rows, current_vocabulary=vocab)
        assert result["candidates"] == []


class TestIcaraiNiteroiOverMergeNotAutoPromoted:
    """Reproduces the discovery-time over-merge the plan's Evidence section
    names outright (a naive frequency-only pass mis-derived "Icarai
    Niteroi" as if it were one city). The fix is structural — candidates,
    right or wrong, NEVER apply themselves — proven here rather than
    asserted only in prose."""

    def _rows(self):
        return [
            _row("R. A, 1 - Praia Icaraí Niterói - RJ 24230-001 Brazil", -22.900, -43.100),
            _row("R. B, 2 - Centro Icaraí Niterói - RJ 24230-002 Brazil", -22.902, -43.102),
            _row("R. C, 3 - Fonseca Icaraí Niterói - RJ 24230-003 Brazil", -22.898, -43.098),
        ]

    def test_both_the_short_and_the_over_merged_ngram_surface_side_by_side(self):
        """Not a bug to hide: a purely statistical pass cannot always tell
        the true (short) name from a longer fragment sharing the identical
        points, so both are surfaced for an operator to judge."""
        result = mine_city_vocabulary_candidates(self._rows(), current_vocabulary=[])
        ngrams = {c["ngram"] for c in result["candidates"]}
        assert "Niterói" in ngrams
        assert "Icaraí Niterói" in ngrams

    def test_neither_candidate_is_auto_applied_to_the_live_vocabulary(self):
        vocab = load_city_vocabulary(_FakeAdminConfig(None))
        names = {e["name"] for e in vocab}
        assert "Icaraí Niterói" not in names
        assert "Niterói" not in names  # not approved either — still just a candidate


class TestParticlePrefixExtensionHeuristic:
    """Real multi-word municipality names from the plan's own census
    Evidence ("Lauro de Freitas 21", "Jaboatão dos Guararapes 28"), plus
    "São Gonçalo" and "Nova Iguaçu" named explicitly as the heuristic's
    worked targets."""

    def _mine(self, city, bairros):
        rows = [
            _row(f"R. {i}, {i} - {bairro} {city} - PE 00000-00{i} Brazil", -8.0 - i * 0.001, -34.9)
            for i, bairro in enumerate(bairros, start=1)
        ]
        return mine_city_vocabulary_candidates(rows, current_vocabulary=[])

    def test_sao_goncalo_short_candidate_is_extended_correctly(self):
        result = self._mine("São Gonçalo", ["Alcântara", "Trindade", "Mutuá"])
        n1 = next(c for c in result["candidates"] if c["ngram"] == "Gonçalo" and c["n"] == 1)
        assert n1["suggested_name"] == "São Gonçalo"

    def test_nova_iguacu_short_candidate_is_extended_correctly(self):
        result = self._mine("Nova Iguaçu", ["Centro", "Comendador Soares", "Austin"])
        n1 = next(c for c in result["candidates"] if c["ngram"] == "Iguaçu" and c["n"] == 1)
        assert n1["suggested_name"] == "Nova Iguaçu"

    def test_lauro_de_freitas_full_name_is_among_the_surfaced_candidates(self):
        result = self._mine("Lauro de Freitas", ["Ipitanga", "Vilas do Atlântico", "Portao"])
        names = {c["ngram"] for c in result["candidates"]} | {
            c["suggested_name"] for c in result["candidates"]
        }
        assert "Lauro de Freitas" in names

    def test_jaboatao_dos_guararapes_full_name_is_among_the_surfaced_candidates(self):
        result = self._mine("Jaboatão dos Guararapes", ["Piedade", "Candeias", "Prazeres"])
        names = {c["ngram"] for c in result["candidates"]} | {
            c["suggested_name"] for c in result["candidates"]
        }
        assert "Jaboatão dos Guararapes" in names


class TestAmbiguityFlag:
    def test_boa_vista_style_collision_is_flagged(self):
        vocab = [
            {"name": "Recife", "lat": -8.0476, "lng": -34.8770, "ambiguous": False},
            {"name": "Boa Vista", "lat": 2.8235, "lng": -60.6758, "ambiguous": False},
        ]
        rows = [
            _row(f"Av. Norte, {i} - Boa Vista Recife - PE 5007{i}-000 Brazil", -8.05, -34.88)
            for i in range(1, 4)
        ]
        result = mine_city_vocabulary_candidates(rows, current_vocabulary=vocab)
        flagged = {a["name"] for a in result["ambiguous"]}
        assert "Boa Vista" in flagged

    def test_the_actually_resolved_city_is_never_flagged_from_its_own_matches(self):
        vocab = [
            {"name": "Recife", "lat": -8.0476, "lng": -34.8770, "ambiguous": False},
            {"name": "Boa Vista", "lat": 2.8235, "lng": -60.6758, "ambiguous": False},
        ]
        rows = [
            _row(f"Av. Norte, {i} - Boa Vista Recife - PE 5007{i}-000 Brazil", -8.05, -34.88)
            for i in range(1, 4)
        ]
        result = mine_city_vocabulary_candidates(rows, current_vocabulary=vocab)
        flagged = {a["name"] for a in result["ambiguous"]}
        assert "Recife" not in flagged

    def test_below_threshold_occurrences_are_not_flagged(self):
        vocab = [
            {"name": "Recife", "lat": -8.0476, "lng": -34.8770, "ambiguous": False},
            {"name": "Boa Vista", "lat": 2.8235, "lng": -60.6758, "ambiguous": False},
        ]
        rows = [_row("Av. Norte, 1 - Boa Vista Recife - PE 50071-000 Brazil", -8.05, -34.88)]
        result = mine_city_vocabulary_candidates(
            rows, current_vocabulary=vocab, ambiguous_ngram_min_occurrences=2,
        )
        flagged = {a["name"] for a in result["ambiguous"]}
        assert "Boa Vista" not in flagged
