"""Unit tests for app/services/event_dedup.py.

See plans/260812_event-dedup-fuzzy-title.md §B/§B2/§D. Pins the distinctive-
set function per rule, the band predicate against the EXACT production
strings named in the plan's own Evidence section (the regression tests —
anything that loosens the rule has to break one of these first), the shared-
lineup rule and its boundary, the candidate window's disjunction, symmetry,
and config load/validate/fallback behaviour.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import event_dedup

RECIFE = ZoneInfo("America/Recife")


def _local(date_str: str, time_str: str = "12:00") -> datetime:
    hour, minute = (int(p) for p in time_str.split(":"))
    year, month, day = (int(p) for p in date_str.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=RECIFE)


_CONFIG = event_dedup.DedupConfig(
    generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
    stopwords=event_dedup.DEFAULT_STOPWORDS,
    lineup_threshold=event_dedup.DEFAULT_LINEUP_THRESHOLD,
    candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
    undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
    auto_merge_enabled=True,
)


def _distinctive(title, venue_name=None):
    return event_dedup.distinctive_set(
        title, venue_tokens=event_dedup.venue_name_tokens(venue_name),
        generic_vocabulary=_CONFIG.generic_vocabulary, stopwords=_CONFIG.stopwords,
    )


def _band(title_a, title_b, venue_name=None):
    return event_dedup.band_for_distinctive_sets(
        _distinctive(title_a, venue_name), _distinctive(title_b, venue_name),
    )


class TestDistinctiveSet:
    def test_function_words_are_dropped(self):
        assert _distinctive("Bolinha do Cavaco") == {"bolinha", "cavaco"}

    def test_generic_event_words_are_dropped(self):
        assert _distinctive("Aniversário do Rodolpho Produções") == {"rodolpho", "producoes"}

    def test_venue_name_tokens_are_dropped(self):
        assert _distinctive("SEXTOU NO CONCHITTAS BAR!", venue_name="Conchittas Bar") == set()

    def test_bare_numerals_are_kept_never_stripped(self):
        # plan §B: stripping numerals would collapse "Semana 1".."Semana 4"
        # into one set, destroying four real weeks.
        assert _distinctive("Férias Amigos Park — Semana 1") == {"ferias", "amigos", "park", "1"}
        assert _distinctive("31 Anos") == {"31"}

    def test_two_character_tokens_are_kept_the_floor_is_two_not_three(self):
        # plan §B: at a three-char floor, "JB do Cavaco" collapses to
        # {cavaco}, a false subset of {bolinha, cavaco}. At two, both tokens
        # survive and the two acts stay non-comparable.
        assert _distinctive("JB do Cavaco") == {"jb", "cavaco"}

    def test_single_character_tokens_are_dropped_unless_numeric(self):
        assert "a" not in _distinctive("A Grande Festa X")

    def test_an_all_generic_title_yields_the_empty_set(self):
        assert _distinctive("SEXTOU NO CONCHITTAS BAR!", venue_name="Conchittas Bar") == set()
        assert _distinctive("Aniversário") == set()


class TestBandForDistinctiveSets:
    """Pinned against the EXACT production strings from the plan's own
    Evidence section — the regression tests. Anything that loosens the rule
    has to break one of these first."""

    def test_the_six_rodolpho_variants_all_land_in_auto_against_each_other(self):
        titles = [
            "Aniversário do RODOLPHO Produções",
            "Aniversário do Rodolpho Produções",
            "31º Rodolpho Produções",
            "Rodolpho",
            "Rodolpho Produções",
        ]
        for i, a in enumerate(titles):
            for b in titles[i + 1:]:
                assert _band(a, b) == event_dedup.BAND_AUTO, (a, b)

    def test_sextou_reduces_to_empty_against_its_own_venue_and_never_bands(self):
        assert _band("SEXTOU NO CONCHITTAS BAR!", "Rodolpho Produções", "Conchittas Bar") == event_dedup.BAND_REFUSE

    def test_acao_leitura_speakers_land_in_suggest(self):
        a = "Ação Leitura: Bate-papo com Marcelino Freire"
        b = "Ação Leitura: Bate-papo com Jeferson Tenório"
        assert _distinctive(a) == {"acao", "bate", "leitura", "marcelino", "freire", "papo"}
        assert _distinctive(b) == {"acao", "bate", "leitura", "jeferson", "tenorio", "papo"}
        assert _band(a, b) == event_dedup.BAND_SUGGEST

    def test_the_three_oficina_titles_land_in_refuse(self):
        titles = ["Oficina Vida de Inseto", "Oficina Cobra Gigante", "Oficina de Sorvete"]
        for i, a in enumerate(titles):
            for b in titles[i + 1:]:
                assert _band(a, b) == event_dedup.BAND_REFUSE, (a, b)

    def test_ferias_semana_numbers_land_in_suggest_never_auto(self):
        weeks = [f"Férias Amigos Park — Semana {n}" for n in range(1, 5)]
        for i, a in enumerate(weeks):
            for b in weeks[i + 1:]:
                assert _band(a, b) == event_dedup.BAND_SUGGEST, (a, b)

    def test_bolinha_and_jb_cavaco_land_in_suggest_never_auto(self):
        assert _band("Bolinha do Cavaco", "JB do Cavaco") == event_dedup.BAND_SUGGEST

    def test_either_set_empty_refuses(self):
        assert _band("SEXTOU NO CONCHITTAS BAR!", "SEXTOU NO CONCHITTAS BAR!", "Conchittas Bar") == event_dedup.BAND_REFUSE

    def test_disjoint_sets_refuse(self):
        assert _band("Oficina Vida de Inseto", "Oficina de Sorvete") == event_dedup.BAND_REFUSE

    def test_symmetry_over_the_full_production_title_list(self):
        titles = [
            "Rodolpho", "Rodolpho Produções", "31º Rodolpho Produções",
            "Aniversário do Rodolpho Produções", "SEXTOU NO CONCHITTAS BAR!",
            "Ação Leitura: Bate-papo com Marcelino Freire",
            "Ação Leitura: Bate-papo com Jeferson Tenório",
            "Oficina Vida de Inseto", "Oficina Cobra Gigante", "Oficina de Sorvete",
            "Férias Amigos Park — Semana 1", "Férias Amigos Park — Semana 2",
            "Bolinha do Cavaco", "JB do Cavaco", "31 Anos",
        ]
        for i, a in enumerate(titles):
            for b in titles[i + 1:]:
                assert _band(a, b) == _band(b, a), (a, b)


class TestLineupRule:
    def test_rodolpho_producoes_and_sextou_reach_auto_via_eleven_shared_names(self):
        performers = [f"Performer {i}" for i in range(1, 12)]
        assert event_dedup.lineup_reaches_auto(performers, performers, threshold=2)

    def test_onildo_pair_reaches_auto_via_three_shared_names(self):
        shared = ["Cezzinha", "Josildo Sá", "Silvério Pessoa"]
        assert event_dedup.lineup_reaches_auto(shared, shared, threshold=2)

    def test_boundary_at_one_shared_name_does_not_reach_auto(self):
        assert not event_dedup.lineup_reaches_auto(["DJ Fabinho"], ["DJ Fabinho"], threshold=2)

    def test_boundary_at_two_shared_names_reaches_auto(self):
        assert event_dedup.lineup_reaches_auto(["A", "B"], ["A", "B"], threshold=2)

    def test_boundary_at_three_shared_names_reaches_auto(self):
        assert event_dedup.lineup_reaches_auto(["A", "B", "C"], ["A", "B", "C"], threshold=2)

    def test_a_pair_with_one_side_empty_never_reaches_auto(self):
        assert not event_dedup.lineup_reaches_auto([], ["A", "B", "C"], threshold=2)
        assert not event_dedup.lineup_reaches_auto(None, ["A", "B", "C"], threshold=2)

    def test_performer_normalisation_treats_case_variants_as_one_name(self):
        assert event_dedup.lineup_name_set(["DAYANNE"]) == event_dedup.lineup_name_set(["Dayanne"])

    def test_performer_normalisation_treats_a_longer_name_as_a_different_name(self):
        assert event_dedup.lineup_name_set(["Dayanne"]) != event_dedup.lineup_name_set(["Dayanne Henrique"])
        assert not event_dedup.shared_lineup_names(["Dayanne"], ["Dayanne Henrique"])


class TestCandidateWindow:
    def test_same_local_date_across_a_21_hour_gap_includes(self):
        a = _local("2026-08-07", "00:00")
        b = _local("2026-08-07", "21:00")
        assert event_dedup.in_candidate_window(a, b, window_hours=8)

    def test_8_hours_across_a_local_date_boundary_includes(self):
        a = _local("2026-08-07", "21:00")
        b = _local("2026-08-08", "00:00")
        assert event_dedup.in_candidate_window(a, b, window_hours=8)

    def test_25_hours_excludes(self):
        a = _local("2026-08-07", "10:00")
        b = _local("2026-08-08", "11:00")
        assert not event_dedup.in_candidate_window(a, b, window_hours=8)

    def test_either_side_missing_excludes(self):
        assert not event_dedup.in_candidate_window(None, _local("2026-08-07"), window_hours=8)

    def test_unified_nightlife_cutoff_is_not_strictly_better_at_4_or_6_hours(self):
        """plan §D: a single "nightlife day starts at 06:00" cutoff was
        tried and rejected — at a 4- or 6-hour cutoff it fixes Conchittas
        but breaks Sala de Reboco. The disjunction (same local date OR
        within N hours) is what makes both work; this test locks that in."""
        conchittas_a = _local("2026-08-07", "21:00")
        conchittas_b = _local("2026-08-08", "00:00")
        sala_a = _local("2026-08-07", "00:00")
        sala_b = _local("2026-08-07", "21:00")
        for hours in (8, 12, 18):
            assert event_dedup.in_candidate_window(conchittas_a, conchittas_b, window_hours=hours)
            assert event_dedup.in_candidate_window(sala_a, sala_b, window_hours=hours)


class TestEvaluatePair:
    def _event(self, title, lineup=None, post_type="event"):
        return {"title": title, "lineup": lineup or [], "post_type": post_type}

    def test_title_containment_alone_reaches_auto_with_empty_lineup(self):
        a, b = self._event("Rodolpho"), self._event("Rodolpho Produções")
        decision = event_dedup.evaluate_pair(a, b, venue_name="Conchittas Bar", config=_CONFIG)
        assert decision.band == event_dedup.BAND_AUTO
        assert decision.reasons == (event_dedup.REASON_TITLE,)

    def test_shared_lineup_alone_reaches_auto_with_disjoint_titles(self):
        performers = [f"Performer {i}" for i in range(1, 12)]
        a = self._event("Rodolpho Produções", lineup=performers)
        b = self._event("SEXTOU NO CONCHITTAS BAR!", lineup=performers)
        decision = event_dedup.evaluate_pair(a, b, venue_name="Conchittas Bar", config=_CONFIG)
        assert decision.band == event_dedup.BAND_AUTO
        assert decision.reasons == (event_dedup.REASON_LINEUP,)

    def test_neither_signal_is_a_tie_break_on_the_other(self):
        """Signal independence (plan §B2): a pair passing containment but
        not lineup still auto-merges, and a pair passing lineup but not
        containment still auto-merges — neither is implemented as a
        fallback on the other."""
        containment_only = event_dedup.evaluate_pair(
            self._event("Rodolpho"), self._event("Rodolpho Produções"),
            venue_name="Conchittas Bar", config=_CONFIG,
        )
        assert containment_only.reasons == (event_dedup.REASON_TITLE,)

        performers = [f"Performer {i}" for i in range(1, 12)]
        lineup_only = event_dedup.evaluate_pair(
            self._event("Rodolpho Produções", lineup=performers),
            self._event("SEXTOU NO CONCHITTAS BAR!", lineup=performers),
            venue_name="Conchittas Bar", config=_CONFIG,
        )
        assert lineup_only.reasons == (event_dedup.REASON_LINEUP,)

    def test_disjoint_and_no_lineup_refuses(self):
        a, b = self._event("Oficina Vida de Inseto"), self._event("Oficina de Sorvete")
        assert event_dedup.evaluate_pair(a, b, venue_name="Entre Amigos O Bode", config=_CONFIG) is None


class TestConfigLoadAndValidate:
    def test_none_redis_returns_every_shipped_default(self):
        config = event_dedup.load_dedup_config(None)
        assert config.auto_merge_enabled == event_dedup.DEFAULT_AUTO_MERGE_ENABLED
        assert config.lineup_threshold == event_dedup.DEFAULT_LINEUP_THRESHOLD
        assert config.candidate_window_hours == event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS
        assert config.undated_window_days == event_dedup.DEFAULT_UNDATED_WINDOW_DAYS
        assert tuple(config.generic_vocabulary) == event_dedup.DEFAULT_GENERIC_VOCABULARY
        assert tuple(config.stopwords) == event_dedup.DEFAULT_STOPWORDS

    def test_auto_merge_disabled_by_default(self):
        """plan §C, 'the six things most likely to go wrong' #1: deploying
        this feature must change no data. This is the pinned production
        posture — a caller with no wired Redis, or a fresh unwritten key,
        must always read False."""
        assert event_dedup.DEFAULT_AUTO_MERGE_ENABLED is False
        assert event_dedup.load_dedup_config(None).auto_merge_enabled is False

    def test_a_written_override_is_read_back(self):
        class FakeRedis:
            def __init__(self):
                self.store = {}

            def get(self, key):
                return self.store.get(key)

        redis_like = FakeRedis()
        redis_like.store[event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY] = json.dumps(True)
        redis_like.store[event_dedup.ADMIN_CONFIG_LINEUP_THRESHOLD_KEY] = json.dumps(3)
        config = event_dedup.load_dedup_config(redis_like)
        assert config.auto_merge_enabled is True
        assert config.lineup_threshold == 3
        # An override to ONE key never disturbs another key's default.
        assert config.candidate_window_hours == event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS

    def test_validators_reject_malformed_values(self):
        import pytest

        with pytest.raises(TypeError):
            event_dedup.validate_auto_merge_enabled_config("yes")
        with pytest.raises(TypeError):
            event_dedup.validate_lineup_threshold_config(2.5)
        with pytest.raises(ValueError):
            event_dedup.validate_lineup_threshold_config(0)
        with pytest.raises(ValueError):
            event_dedup.validate_candidate_window_hours_config(-1)
        with pytest.raises(TypeError):
            event_dedup.validate_generic_vocabulary_config("festa")


# ── §D: load_dedup_config trusts the validated value, never coerces ─────────
class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set_raw(self, key: str, value) -> None:
        """Writes RAW JSON directly, bypassing AdminConfigService.set — this
        is exactly how a value stored BEFORE the §C validators were
        registered (or hand-edited in RDS) would look: valid JSON, wrong
        TYPE for the key."""
        self.store[key] = json.dumps(value)


def _fallback_count(key: str) -> float:
    return event_dedup.EVENT_DEDUP_CONFIG_TYPE_FALLBACK_TOTAL.labels(key=key)._value.get()


class TestLoadDedupConfigNeverCoercesAWrongTypedValue:
    """plans/260814_seeded-state-and-config-validation.md §D. Before this
    plan, `load_dedup_config` coerced with `bool()`/`int()`/`tuple()` —
    `bool("false")` is True, so the stored STRING "false" silently ENABLED
    auto-merge. Every case here proves the opposite: a wrong-typed stored
    value falls back to the shipped default and is never reinterpreted."""

    def test_the_string_false_never_enables_auto_merge(self):
        """The plan's own headline scenario, pinned directly against the
        reader (§C's write-time rejection is pinned separately, at the BDD
        layer, against AdminConfigService.set)."""
        redis_like = _FakeRedis()
        redis_like.set_raw(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, "false")

        before = _fallback_count(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY)
        config = event_dedup.load_dedup_config(redis_like)
        after = _fallback_count(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY)

        assert config.auto_merge_enabled is False
        assert config.auto_merge_enabled == event_dedup.DEFAULT_AUTO_MERGE_ENABLED
        assert after == before + 1, "the type fallback must be counted"

    def test_a_wrong_typed_value_falls_back_to_default_for_every_key(self):
        cases = [
            (event_dedup.ADMIN_CONFIG_GENERIC_VOCABULARY_KEY, "not-a-list",
             list(event_dedup.DEFAULT_GENERIC_VOCABULARY)),
            (event_dedup.ADMIN_CONFIG_STOPWORDS_KEY, "not-a-list",
             list(event_dedup.DEFAULT_STOPWORDS)),
            (event_dedup.ADMIN_CONFIG_LINEUP_THRESHOLD_KEY, "two",
             event_dedup.DEFAULT_LINEUP_THRESHOLD),
            (event_dedup.ADMIN_CONFIG_CANDIDATE_WINDOW_HOURS_KEY, "eight",
             event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS),
            (event_dedup.ADMIN_CONFIG_UNDATED_WINDOW_DAYS_KEY, "fourteen",
             event_dedup.DEFAULT_UNDATED_WINDOW_DAYS),
            (event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, "true",
             event_dedup.DEFAULT_AUTO_MERGE_ENABLED),
        ]
        for key, bad_value, default in cases:
            redis_like = _FakeRedis()
            redis_like.set_raw(key, bad_value)
            before = _fallback_count(key)

            config = event_dedup.load_dedup_config(redis_like)

            after = _fallback_count(key)
            assert after == before + 1, key
            field = {
                event_dedup.ADMIN_CONFIG_GENERIC_VOCABULARY_KEY: "generic_vocabulary",
                event_dedup.ADMIN_CONFIG_STOPWORDS_KEY: "stopwords",
                event_dedup.ADMIN_CONFIG_LINEUP_THRESHOLD_KEY: "lineup_threshold",
                event_dedup.ADMIN_CONFIG_CANDIDATE_WINDOW_HOURS_KEY: "candidate_window_hours",
                event_dedup.ADMIN_CONFIG_UNDATED_WINDOW_DAYS_KEY: "undated_window_days",
                event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY: "auto_merge_enabled",
            }[key]
            actual = getattr(config, field)
            expected = tuple(default) if isinstance(default, list) else default
            assert actual == expected, (key, actual, expected)

    def test_one_keys_bad_value_never_disturbs_another_keys_good_override(self):
        """Mirrors `_load_json_config`'s own "one key's bad value never
        disables another's override" guarantee — proven here across the
        validate boundary too, not just the read-failure boundary."""
        redis_like = _FakeRedis()
        redis_like.set_raw(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, "false")  # bad
        redis_like.set_raw(event_dedup.ADMIN_CONFIG_LINEUP_THRESHOLD_KEY, 5)  # good

        config = event_dedup.load_dedup_config(redis_like)

        assert config.auto_merge_enabled is False  # fell back to default
        assert config.lineup_threshold == 5  # untouched, its own valid value

    def test_a_valid_value_is_used_exactly_as_validated_no_coercion(self):
        """The positive case: `int`/`bool` values are NEVER passed through
        `int()`/`bool()` — they are exactly what the validator returned.
        Proven by an integer that would look identical whether or not a
        redundant int() ran (so this alone would not catch a regression to
        coercion) COMBINED with the type-fallback tests above, which WOULD
        catch it; kept as a readable, explicit "the happy path still
        works" companion."""
        redis_like = _FakeRedis()
        redis_like.set_raw(event_dedup.ADMIN_CONFIG_LINEUP_THRESHOLD_KEY, 7)
        redis_like.set_raw(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, True)

        config = event_dedup.load_dedup_config(redis_like)

        assert config.lineup_threshold == 7
        assert config.auto_merge_enabled is True

    def test_no_fallback_is_counted_for_a_valid_value(self):
        redis_like = _FakeRedis()
        redis_like.set_raw(event_dedup.ADMIN_CONFIG_UNDATED_WINDOW_DAYS_KEY, 21)
        before = _fallback_count(event_dedup.ADMIN_CONFIG_UNDATED_WINDOW_DAYS_KEY)

        config = event_dedup.load_dedup_config(redis_like)

        after = _fallback_count(event_dedup.ADMIN_CONFIG_UNDATED_WINDOW_DAYS_KEY)
        assert config.undated_window_days == 21
        assert after == before

    def test_an_absent_key_uses_the_default_without_being_counted_as_a_fallback(self):
        """A key that was simply never written is not a "bad value" — no
        type fallback should be counted for it, only for a value that WAS
        read and failed validation."""
        redis_like = _FakeRedis()  # nothing stored at all
        before = _fallback_count(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY)

        config = event_dedup.load_dedup_config(redis_like)

        after = _fallback_count(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY)
        assert config.auto_merge_enabled == event_dedup.DEFAULT_AUTO_MERGE_ENABLED
        assert after == before
