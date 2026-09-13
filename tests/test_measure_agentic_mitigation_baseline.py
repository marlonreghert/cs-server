"""Unit tests for `scripts.measure_agentic_mitigation_baseline` —
plans/260913_dedup-agentic-mitigation-discovery.md Phase 2.

House style (`tests/test_measure_event_dedup.py` and siblings): exercise the
module's own `measure()`/replay functions directly against small, explicit
fixtures — never the real committed corpus file's exact case count or exact
real-world numbers, which would make this suite brittle to a future
corpus edit. The committed corpus itself is exercised once, lightly, by
`TestRealCorpusLoadsAndReplays` below, to catch a schema drift the rest of
this file's synthetic fixtures cannot.

Division of labour against the sibling BDD feature: the BDD scenarios pin
the user-facing behaviour ("a report names, for each corpus case, which
existing signal already caught it"); this file pins the exact classification
rule for each of the three failure classes plus the non-duplicate-control
false-positive guard, and the read-only guarantee.
"""
from __future__ import annotations

import inspect

import scripts.measure_agentic_mitigation_baseline as m
from app.services import event_dedup


def _fragmentation_case(titles, lineups=None):
    lineups = lineups or [[] for _ in titles]
    return {
        "id": "t-fragmentation",
        "failure_class": "cross_post_fragmentation",
        "synthetic": True,
        "venue": {"venue_id": "v1", "venue_name": "Test Venue"},
        "sibling_venues": [], "unrelated_venues": [], "instagram_handle": None,
        "posts": [
            {
                "event_id": f"e{i}", "title": title, "lineup": lineup,
                "starts_at": "2026-09-12T22:00:00-03:00", "location_text": None, "caption": None,
            }
            for i, (title, lineup) in enumerate(zip(titles, lineups))
        ],
    }


class TestReplayDedupSignal:
    def test_distinct_titles_reach_band_refuse(self):
        case = _fragmentation_case(["ROWKA", "VITINHO POLÊMICO"])
        result = m.replay_dedup_signal(case)
        assert result["bands"][event_dedup.BAND_AUTO] == 0
        assert result["bands"][event_dedup.BAND_REFUSE] == 1

    def test_a_containing_title_pair_reaches_band_auto(self):
        case = _fragmentation_case(["Rodolpho", "Rodolpho Produções"])
        result = m.replay_dedup_signal(case)
        assert result["bands"][event_dedup.BAND_AUTO] == 1

    def test_an_intersecting_non_containing_pair_reaches_band_suggest(self):
        case = _fragmentation_case([
            "Ação Leitura: Bate-papo com Marcelino Freire",
            "Ação Leitura: Bate-papo com Jeferson Tenório",
        ])
        result = m.replay_dedup_signal(case)
        assert result["bands"][event_dedup.BAND_SUGGEST] == 1

    def test_shared_lineup_at_the_threshold_reaches_band_auto_even_with_disjoint_titles(self):
        case = _fragmentation_case(
            ["Night One", "Night Two"],
            lineups=[["DJ Alpha", "DJ Beta"], ["DJ Alpha", "DJ Beta", "DJ Gamma"]],
        )
        result = m.replay_dedup_signal(case)
        assert result["bands"][event_dedup.BAND_AUTO] == 1

    def test_one_shared_performer_alone_does_not_reach_auto(self):
        """The Club Metrópole arithmetic (RCA): one shared name is below
        DEFAULT_LINEUP_THRESHOLD=2, so it must land in refuse (no title
        containment either)."""
        case = _fragmentation_case(
            ["SÁBADO VAI FERVER", "SECRET CLUB com @neguindabasersv"],
            lineups=[["@neguindabasersv"], ["@neguindabasersv"]],
        )
        result = m.replay_dedup_signal(case)
        assert result["bands"][event_dedup.BAND_AUTO] == 0
        assert result["bands"][event_dedup.BAND_REFUSE] == 1


class TestReplayAttributionDispute:
    def _case(self, *, location_text, sibling_handle=None, sibling_address=None, sibling_name="Mapped Sibling"):
        sibling = []
        if sibling_handle or sibling_address:
            sibling = [{
                "venue_id": "v2", "venue_name": sibling_name, "address": sibling_address,
                "instagram_handle": sibling_handle,
            }]
        return {
            "id": "t-dispute", "failure_class": "multi_location_misattribution", "synthetic": True,
            "venue": {"venue_id": "v1", "venue_name": "Mapped Venue"},
            "sibling_venues": sibling, "unrelated_venues": [], "instagram_handle": "mapped_handle",
            "posts": [{
                "event_id": "e1", "title": "Some Night", "lineup": [],
                "starts_at": "2026-09-12T22:00:00-03:00", "location_text": location_text, "caption": None,
            }],
        }

    def test_a_handle_mention_of_a_sibling_disputes(self):
        case = self._case(location_text="@sibling_handle", sibling_handle="sibling_handle")
        result = m.replay_attribution_dispute(case)
        assert result["any_disputed"] is True
        assert result["posts"][0]["method"] == "handle_mention"

    def test_a_neighbourhood_match_against_the_siblings_name_disputes(self):
        case = self._case(location_text="MAPPED SIBLING", sibling_address="Rua X, 1 - Bairro Y")
        result = m.replay_attribution_dispute(case)
        assert result["any_disputed"] is True
        assert result["posts"][0]["method"] == "neighbourhood_match"

    def test_describing_the_mapped_venues_own_branch_never_disputes(self):
        case = self._case(location_text="MAPPED VENUE")
        result = m.replay_attribution_dispute(case)
        assert result["any_disputed"] is False

    def test_no_location_text_at_all_is_not_evaluated(self):
        case = self._case(location_text=None)
        result = m.replay_attribution_dispute(case)
        assert result["any_disputed"] is False
        assert result["posts"] == []


class TestReplayPromoterDiscoveryMentions:
    def _case(self, captions, own_handle="myhandle"):
        return {
            "id": "t-mentions", "failure_class": "multi_location_misattribution", "synthetic": True,
            "venue": {"venue_id": "v1", "venue_name": "V"}, "sibling_venues": [], "unrelated_venues": [],
            "instagram_handle": own_handle,
            "posts": [
                {"event_id": f"e{i}", "title": "T", "lineup": [], "starts_at": None,
                 "location_text": None, "caption": caption}
                for i, caption in enumerate(captions)
            ],
        }

    def test_a_handle_mentioned_across_three_distinct_posts_clears_the_default_threshold(self):
        case = self._case(["check out @otherhandle", "with @otherhandle tonight", "feat @otherhandle"])
        result = m.replay_promoter_discovery_mentions(case)
        assert "otherhandle" in result["proposed_candidates"]

    def test_a_handle_mentioned_in_only_one_post_never_clears_the_threshold(self):
        case = self._case(["only here @otherhandle"])
        result = m.replay_promoter_discovery_mentions(case)
        assert result["proposed_candidates"] == []

    def test_repeated_mentions_within_one_post_count_once(self):
        """The SAME distinct-per-post rule `PromoterRegistryService.
        run_discovery` enforces: a caption that repeats one handle several
        times must not let one post alone clear the threshold."""
        case = self._case(["@otherhandle @otherhandle @otherhandle", "nothing else", "nothing else either"])
        result = m.replay_promoter_discovery_mentions(case)
        assert result["mention_counts"].get("otherhandle") == 1
        assert result["proposed_candidates"] == []

    def test_the_posting_handles_own_mentions_of_itself_are_excluded(self):
        case = self._case(["@myhandle @myhandle", "@myhandle", "@myhandle"], own_handle="myhandle")
        result = m.replay_promoter_discovery_mentions(case)
        assert result["mention_counts"] == {}


class TestReplayCaseClassification:
    def test_an_unrelated_venue_gap_case_with_no_sibling_defined_is_not_caught_by_the_dispute_signal(self):
        """Pins the plan's own documented gap: METHOD_NAME_MATCH is outside
        DISPUTE_METHODS, so a case shaped like the real
        casa_bacurau_unrelated_venue_gap corpus case — an UNRELATED,
        already-catalogued venue named in plain text, no @ mention — is not
        disputed, and `replay_case` must report an empty `caught_by`."""
        case = {
            "id": "t-gap", "failure_class": "unrelated_venue_gap", "synthetic": True,
            "venue": {"venue_id": "v1", "venue_name": "Casa Bacurau"},
            "sibling_venues": [], "unrelated_venues": [
                {"venue_id": "v2", "venue_name": "Clube Ferroviário de Afogados"},
            ],
            "instagram_handle": "casabacurau",
            "posts": [{
                "event_id": "e1", "title": "NO BREGA", "lineup": [],
                "starts_at": "2026-09-06T21:00:00-03:00",
                "location_text": "Clube Ferroviário de Afogados", "caption": None,
            }],
        }
        result = m.replay_case(case)
        assert result.caught_by == []
        assert result.false_positive is None

    def test_a_non_duplicate_control_with_no_false_positive_has_false_positive_false(self):
        case = _fragmentation_case(["Oficina Vida de Inseto", "Oficina Cobra Gigante"])
        case["failure_class"] = "non_duplicate_control"
        result = m.replay_case(case)
        assert result.false_positive is False

    def test_a_non_duplicate_control_that_wrongly_auto_merges_is_flagged_a_false_positive(self):
        """A deliberately-broken control (two titles that DO contain each
        other) — exercises the false-positive guard path itself, never
        expected against the real shipped corpus."""
        case = _fragmentation_case(["Rodolpho", "Rodolpho Produções"])
        case["failure_class"] = "non_duplicate_control"
        result = m.replay_case(case)
        assert result.false_positive is True


class TestMeasureCorpus:
    def test_measure_corpus_returns_one_result_per_case_in_order(self):
        corpus = {"cases": [
            _fragmentation_case(["A", "B"]), _fragmentation_case(["C", "D"]),
        ]}
        corpus["cases"][0]["id"] = "first"
        corpus["cases"][1]["id"] = "second"
        results = m.measure_corpus(corpus)
        assert [r.case_id for r in results] == ["first", "second"]

    def test_measure_corpus_takes_no_dao_and_touches_no_database(self):
        """Read-only guarantee, corpus half: `measure_corpus`'s signature
        takes only `corpus` — there is no DAO/session parameter it could
        leak a write through."""
        sig = inspect.signature(m.measure_corpus)
        assert list(sig.parameters) == ["corpus"]


class TestRealCorpusLoadsAndReplays:
    """A light integration guard against the COMMITTED corpus file's own
    schema drifting out from under the replay functions — not a pin on its
    exact case count or exact findings, which belong to the PR's own report,
    not to this suite."""

    def test_the_committed_corpus_loads_and_every_case_replays_without_error(self):
        corpus = m.load_corpus()
        assert corpus.get("cases"), "the committed corpus must not be empty"
        results = m.measure_corpus(corpus)
        assert len(results) == len(corpus["cases"])

    def test_the_committed_corpus_covers_every_named_failure_class(self):
        corpus = m.load_corpus()
        classes = {c["failure_class"] for c in corpus["cases"]}
        assert {
            "cross_post_fragmentation", "multi_location_misattribution",
            "unrelated_venue_gap", "non_duplicate_control",
        } <= classes

    def test_the_committed_corpus_produces_no_false_positive(self):
        """The non-negotiable safety property for the committed corpus
        itself: whatever else changes, the existing false-positive corpus
        this repo already relies on must never auto-merge."""
        corpus = m.load_corpus()
        results = m.measure_corpus(corpus)
        assert not any(r.false_positive for r in results)


class TestReadOnlyGuarantee:
    def test_measure_module_calls_no_write_shaped_dao_method(self):
        """A structural guard, not a mock-based one: grep this module's own
        source for any CALL shaped like a write (a write-method name
        immediately followed by an open paren — this module's own
        docstring legitimately NAMES several such methods in prose, as
        what it does NOT call, so the check below requires the call
        syntax, not a bare substring, to avoid flagging its own
        documentation) and find none."""
        import scripts.measure_agentic_mitigation_baseline as mod
        source = inspect.getsource(mod)
        banned_calls = (
            "insert_event(", "update_event(", "upsert_promoter_account(",
            "replace_event_venue_link_candidates(",
            "set_event_venue_link_candidate_recommendation(",
            "def sweep(",
        )
        for banned in banned_calls:
            assert banned not in source, f"found write-shaped call {banned!r} in measure_agentic_mitigation_baseline.py"
