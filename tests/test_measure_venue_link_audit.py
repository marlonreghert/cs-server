"""Unit tests for `scripts.measure_venue_link_audit` —
plans/260913_venue-handle-link-audit.md.

House style (`tests/test_measure_agentic_mitigation_baseline.py`): exercise
the module's own `replay_case`/`measure_corpus` against small, explicit
fixtures for the general rules, and touch the real COMMITTED corpus once,
lightly, to catch a schema drift the synthetic fixtures cannot — but the
committed corpus's three real, re-verified cases are exactly what this plan
exists to validate against, so (unlike the sibling file) this suite DOES
pin their exact expected verdicts: a mismatch there is the one regression
this whole plan was commissioned to prevent.

Division of labour against the sibling BDD feature: the BDD scenario pins
the user-facing behaviour ("it flags the re-verified wrong-venue case");
this file pins the exact per-case replay mechanics and the read-only
guarantee.
"""
from __future__ import annotations

import inspect

import scripts.measure_venue_link_audit as m


def _case(case_id, handle, mapped_venues, events, expected_flagged_venue_ids):
    return {
        "case_id": case_id, "handle": handle, "mapped_venues": mapped_venues,
        "events": events, "expected_flagged_venue_ids": expected_flagged_venue_ids,
    }


class TestReplayCase:
    def test_a_flagged_case_matches_its_expectation(self):
        case = _case(
            "t-flag", "handle_a",
            [{"venue_id": "v1", "venue_name": "Theater Luís Mendonça", "neighborhood": "Boa Viagem"}],
            [{"location_text": "Torre Malakoff"}, {"location_text": "Pernambuco Dream"}],
            ["v1"],
        )
        result = m.replay_case(case)
        assert result["flagged"] is True
        assert result["flagged_venue_ids"] == ["v1"]
        assert result["matches_expectation"] is True

    def test_a_mismatch_is_reported_not_hidden(self):
        """A case whose corpus label disagrees with the real function's
        verdict must be VISIBLE in the report (matches_expectation=False),
        never silently coerced to match — this is the guard that would have
        caught the plan's own editaisculturape empty/non-empty count slip
        if the corpus had been wrong instead of this test."""
        case = _case(
            "t-mismatch", "handle_b",
            [{"venue_id": "v1", "venue_name": "Theater Luís Mendonça", "neighborhood": "Boa Viagem"}],
            [{"location_text": "Teatro Luiz Mendonça"}] * 5,
            ["v1"],  # wrong expectation: this venue is actually corroborated
        )
        result = m.replay_case(case)
        assert result["flagged"] is False
        assert result["matches_expectation"] is False

    def test_a_double_mapped_handle_replays_both_venues_independently(self):
        case = _case(
            "t-double", "real.botequim",
            [
                {"venue_id": "correct", "venue_name": "Bar Real Botequim", "neighborhood": "Casa Forte"},
                {"venue_id": "wrong", "venue_name": "Real Bar e Lanches", "neighborhood": "Pinheiros"},
            ],
            [{"location_text": "Real Botequim\nAv. 17 de agosto, 1761 - Casa Forte"}] * 5,
            ["wrong"],
        )
        result = m.replay_case(case)
        assert result["flagged_venue_ids"] == ["wrong"]
        assert result["matches_expectation"] is True


class TestMeasureCorpus:
    def test_keyed_by_case_id_one_entry_per_case(self):
        corpus = {
            "cases": [
                _case("a", "h1", [{"venue_id": "v1", "venue_name": "X", "neighborhood": "Y"}], [], []),
                _case("b", "h2", [{"venue_id": "v2", "venue_name": "X", "neighborhood": "Y"}], [], []),
            ],
        }
        result = m.measure_corpus(corpus)
        assert set(result.keys()) == {"a", "b"}


class TestTheCommittedCorpusMatchesThisPlansOwnFindings:
    """The three re-verified real cases this plan's Evidence section
    derives the whole detection design from. A mismatch here means either
    the corpus or the comparison logic drifted from the re-verified
    production data — exactly the regression this plan exists to prevent,
    so these are pinned exactly, unlike the sibling measurement script's
    own deliberately looser corpus guard."""

    def test_the_committed_corpus_loads_and_every_case_replays(self):
        report = m.measure_corpus()
        assert len(report) == 3

    def test_real_botequim_flags_only_the_wrong_venue(self):
        result = m.measure_corpus()["real_botequim_double_mapped_handle"]
        assert result["matches_expectation"] is True, result
        assert result["flagged_venue_ids"] == ["real_bar_e_lanches"], result

    def test_editaisculturape_is_flagged(self):
        result = m.measure_corpus()["editaisculturape_force_assigned_wrong_venue"]
        assert result["matches_expectation"] is True, result
        assert result["flagged"] is True, result

    def test_teatroluizmendonca_is_not_flagged(self):
        result = m.measure_corpus()["teatroluizmendonca_benign_control"]
        assert result["matches_expectation"] is True, result
        assert result["flagged"] is False, result


class TestReadOnlyGuarantee:
    def test_measure_module_calls_no_write_shaped_dao_method(self):
        """A structural guard, not a mock-based one: grep this module's own
        source for a write-shaped CALL (a write-method name immediately
        followed by an open paren) and find none."""
        source = inspect.getsource(m)
        banned_calls = (
            "insert_event(", "update_event(", "upsert_venue(", "upsert_crawl_target(",
            "update_crawl_target(", "replace_event_venue_link_candidates(",
            "set_event_venue_link_candidate_recommendation(",
        )
        for banned in banned_calls:
            assert banned not in source, f"found write-shaped call {banned!r} in measure_venue_link_audit.py"
