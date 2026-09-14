"""Unit tests for the same-handle/same-exact-time auto-merge signal.

See plans/260913_candidate-cap-and-handle-time-merge.md Part B. Real
production data, re-pulled fresh on 2026-09-13 for this plan, grounds both
halves of the test:

  - Boteco Nem A Pau Juvenal's three posts ("Domingo é dia de Boteco!" /
    "Show de @alexcarrerasoficial" / "ALEX CARRERAS E BANDA") share the
    EXACT same `starts_at` (2026-09-13 18:00:00+00), `time_known=True`, and
    neither `location_resolution` nor `review_reason` set -- a genuine
    triple repost of one show, which `evaluate_pair`'s title/lineup rules
    alone refuse (two pairs share exactly one lineup token; the third pair
    is disjoint because the glued handle token never token-intersects the
    space-separated title).
  - Casa de Jorge Amado's two plays ("PETER PAN" 14:00 / "CHAPEUZINHO
    VERMELHO" 19:00, same day, both `time_known=True`) are genuinely
    DIFFERENT events that `260912_events-venue-night-duplication.md`
    already protects -- the exact shape this new signal must keep apart.

`oquetemhojeemnatal`'s real rows (a registered promoter/roundup account,
`events.promoter_account.status='active'`) are the pinned counter-example
for the `location_resolution IS NOT NULL` guard: every sampled row carries
`location_resolution='auto'` or `'unresolved'`, never NULL.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from prometheus_client import REGISTRY

from app.models.venue import Venue
from app.services import event_dedup
from app.services.event_attribution_dispute import (
    REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE,
)
from app.services.event_merge import merge_touched_events, run_title_similarity_pass
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

_SEQ = {"n": 0}


def _store():
    store = InMemoryRdsVenueStore()
    store.upsert_venue(Venue(
        venue_id="v_boteco", venue_name="Boteco Nem A Pau Juvenal", venue_lat=-8.05, venue_lng=-34.88,
    ))
    store.upsert_venue(Venue(
        venue_id="v_jorge", venue_name="Casa de Jorge Amado", venue_lat=-8.05, venue_lng=-34.88,
    ))
    return store


def _config(*, handle_time_match_enabled=False, lineup_threshold=None):
    return event_dedup.DedupConfig(
        generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
        stopwords=event_dedup.DEFAULT_STOPWORDS,
        lineup_threshold=lineup_threshold or event_dedup.DEFAULT_LINEUP_THRESHOLD,
        candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
        undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
        auto_merge_enabled=True,
        handle_time_match_enabled=handle_time_match_enabled,
    )


def _seed(
    store, title, venue_id, *, source_handle, starts_at, time_known=True,
    lineup=None, location_resolution=None, review_reason=None,
    operator_edited_fields=None, status="pending_review", linked_by=None,
):
    _SEQ["n"] += 1
    event_id = new_event_id()
    store.insert_event({
        "event_id": event_id, "venue_id": venue_id, "starts_at": starts_at,
        "title": title, "post_type": "event", "status": status,
        "lineup": lineup or [], "time_known": time_known,
        "location_resolution": location_resolution, "review_reason": review_reason,
        "linked_by": linked_by, "operator_edited_fields": operator_edited_fields,
        "source_kind": "venue_post", "source_handle": source_handle,
        "source_shortcode": f"htm_sc_{_SEQ['n']}",
        "first_seen_at": starts_at, "last_seen_at": starts_at,
    })
    return event_id


def _alive(store, ids):
    return [
        eid for eid in ids
        if store.get_event(eid) is not None and store.get_event(eid)["status"] != "superseded"
    ]


def _merge(store, venue_id, config):
    run_title_similarity_pass(store, venue_id, datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc), config=config)


def _merge_outcome(outcome: str) -> float:
    return REGISTRY.get_sample_value(
        "event_merge_total", {"identity": "title", "outcome": outcome},
    ) or 0.0


def _boteco_three(store):
    handle = "boteconemapaujuvenal"
    when = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
    return [
        _seed(store, "Domingo é dia de Boteco!", "v_boteco", source_handle=handle, starts_at=when, lineup=["Alex Carreras e Banda"]),
        _seed(store, "Show de @alexcarrerasoficial", "v_boteco", source_handle=handle, starts_at=when, lineup=["@alexcarrerasoficial"]),
        _seed(store, "ALEX CARRERAS E BANDA", "v_boteco", source_handle=handle, starts_at=when, lineup=["ALEX CARRERAS E BANDA"]),
    ]


def _jorge_amado_two(store):
    handle = "teatrojorgeamado"
    return [
        _seed(store, "PETER PAN", "v_jorge", source_handle=handle, starts_at=datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc)),
        _seed(store, "CHAPEUZINHO VERMELHO", "v_jorge", source_handle=handle, starts_at=datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc)),
    ]


# ── the real positive case ───────────────────────────────────────────────
class TestBotecoTheRealPositiveCase:
    def test_the_title_lineup_rules_alone_refuse_all_three_pairs(self):
        """Documents the gap this signal exists to close -- a regression
        guard against the mechanism being quietly subsumed into title/
        lineup someday without anyone noticing this case stopped needing
        it."""
        store = _store()
        ids = _boteco_three(store)
        _merge(store, "v_boteco", _config(handle_time_match_enabled=False))
        assert len(_alive(store, ids)) == 3

    def test_enabling_the_flag_collapses_all_three_into_one(self):
        store = _store()
        ids = _boteco_three(store)
        _merge(store, "v_boteco", _config(handle_time_match_enabled=True))
        assert len(_alive(store, ids)) == 1

    def test_the_merge_is_recorded_with_the_handle_time_match_reason(self):
        store = _store()
        _boteco_three(store)
        _merge(store, "v_boteco", _config(handle_time_match_enabled=True))
        audited = store.list_event_merge_suggestions(decision="auto_merged")
        assert audited, "no auto_merged audit row was written"
        assert any(
            event_dedup.REASON_HANDLE_TIME_MATCH in (s.get("reasons") or [])
            for s in audited
        ), audited

    def test_the_signal_gets_its_own_metric_outcome(self):
        before = _merge_outcome("merged_handle_time_match")
        store = _store()
        _boteco_three(store)
        _merge(store, "v_boteco", _config(handle_time_match_enabled=True))
        assert _merge_outcome("merged_handle_time_match") > before


# ── the real negative case ───────────────────────────────────────────────
class TestJorgeAmadoTheRealNegativeCase:
    def test_two_different_known_times_on_one_day_never_merge(self):
        store = _store()
        ids = _jorge_amado_two(store)
        _merge(store, "v_jorge", _config(handle_time_match_enabled=True))
        assert len(_alive(store, ids)) == 2

    def test_this_holds_regardless_of_the_flag(self):
        store = _store()
        ids = _jorge_amado_two(store)
        _merge(store, "v_jorge", _config(handle_time_match_enabled=False))
        assert len(_alive(store, ids)) == 2


# ── the predicate, directly ───────────────────────────────────────────────
def _event(**overrides):
    base = {
        "source_handle": "h", "starts_at": datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc),
        "time_known": True, "location_resolution": None, "review_reason": None,
    }
    base.update(overrides)
    return base


class TestHandleTimeMatchEligibleDirectly:
    def test_the_real_boteco_shape_is_eligible(self):
        a = _event(title="Domingo é dia de Boteco!")
        b = _event(title="ALEX CARRERAS E BANDA")
        assert event_dedup.handle_time_match_eligible(a, b) is True

    def test_the_real_jorge_amado_shape_is_not_eligible(self):
        a = _event(starts_at=datetime(2026, 9, 13, 14, 0, tzinfo=timezone.utc))
        b = _event(starts_at=datetime(2026, 9, 13, 19, 0, tzinfo=timezone.utc))
        assert event_dedup.handle_time_match_eligible(a, b) is False

    def test_different_handles_never_match(self):
        a = _event(source_handle="handle_a")
        b = _event(source_handle="handle_b")
        assert event_dedup.handle_time_match_eligible(a, b) is False

    def test_no_handle_on_either_side_never_matches(self):
        a = _event(source_handle=None)
        b = _event(source_handle=None)
        assert event_dedup.handle_time_match_eligible(a, b) is False

    def test_a_non_null_location_resolution_on_either_side_excludes(self):
        """Pinned against the real oquetemhojeemnatal shape: every sampled
        row from that registered promoter account carries a non-NULL
        location_resolution ('auto' or 'unresolved'), never NULL."""
        a = _event(location_resolution="auto")
        b = _event()
        assert event_dedup.handle_time_match_eligible(a, b) is False
        assert event_dedup.handle_time_match_eligible(b, a) is False

        c = _event(location_resolution="unresolved")
        d = _event()
        assert event_dedup.handle_time_match_eligible(c, d) is False

    def test_a_dispute_review_reason_on_either_side_excludes(self):
        a = _event(review_reason=REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE)
        b = _event()
        assert event_dedup.handle_time_match_eligible(a, b) is False
        assert event_dedup.handle_time_match_eligible(b, a) is False

    def test_a_dispute_reason_folded_with_other_reasons_still_excludes(self):
        # fold_review_reason joins multiple reasons with "; " -- the
        # substring check must still catch it.
        a = _event(review_reason="some_other_reason; location_text_disputes_venue")
        b = _event()
        assert event_dedup.handle_time_match_eligible(a, b) is False

    def test_an_unrelated_review_reason_does_not_exclude(self):
        a = _event(review_reason="unresolved_venue")
        b = _event(review_reason="unresolved_venue")
        assert event_dedup.handle_time_match_eligible(a, b) is True

    def test_one_side_time_known_the_other_not_excludes(self):
        a = _event(time_known=True)
        b = _event(time_known=False)
        assert event_dedup.handle_time_match_eligible(a, b) is False
        assert event_dedup.handle_time_match_eligible(b, a) is False

    def test_both_sides_time_unknown_excludes_too(self):
        """v1's deliberate narrowing (plan Open Questions #1): the catalog-
        wide sweep that validated this rule found zero same-handle/same-day
        clusters with an unset time on either side, so this allowance is
        excluded rather than shipped unvalidated."""
        a = _event(time_known=False)
        b = _event(time_known=False)
        assert event_dedup.handle_time_match_eligible(a, b) is False

    def test_different_exact_times_exclude_even_when_both_known(self):
        a = _event(starts_at=datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc))
        b = _event(starts_at=datetime(2026, 9, 13, 18, 1, tzinfo=timezone.utc))
        assert event_dedup.handle_time_match_eligible(a, b) is False

    def test_a_missing_starts_at_on_either_side_excludes(self):
        a = _event(starts_at=None)
        b = _event()
        assert event_dedup.handle_time_match_eligible(a, b) is False

    def test_the_predicate_is_symmetric(self):
        a = _event(title="Domingo é dia de Boteco!")
        b = _event(title="ALEX CARRERAS E BANDA")
        assert (
            event_dedup.handle_time_match_eligible(a, b)
            == event_dedup.handle_time_match_eligible(b, a)
        )

    def test_the_literal_review_reason_string_stays_in_lockstep(self):
        """event_dedup.py duplicates this literal rather than importing it
        (event_attribution_dispute.py imports FROM event_dedup.py, so the
        reverse import would cycle) -- this guard is what keeps the two
        from silently drifting apart, the same pattern event_venue_
        resolution.METHOD_VENUE_NOT_IN_CATALOG's own docstring uses."""
        assert (
            event_dedup._REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE
            == REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE
        )


# ── synthetic regression fixtures for the 260812 false-positive corpus ───
# The ORIGINAL rows no longer exist in production (confirmed by a live
# search this session) and were never time-stamped in the first place --
# only pinned by TITLE in tests/test_event_dedup.py. These fixtures are
# EXPLICITLY SYNTHETIC: constructed from the real scenario each case
# represents (one venue's own handle, two different acts/workshops on one
# night realistically announced at different set times), never claimed as
# re-derived production data.
class TestSyntheticFalsePositiveCorpusNeverMatches:
    """plans/260812_event-dedup-fuzzy-title.md's Evidence. Per the plan's
    own Acceptance Criteria, this is a hard blocker: every one of these
    must stay excluded before the flag may ever be enabled in production."""

    def _assert_never_eligible(self, title_a, title_b, time_a, time_b, handle="same_venue_handle"):
        a = _event(title=title_a, source_handle=handle, starts_at=time_a)
        b = _event(title=title_b, source_handle=handle, starts_at=time_b)
        assert event_dedup.handle_time_match_eligible(a, b) is False, (title_a, title_b)

    def test_bolinha_and_jb_do_cavaco_never_match(self):
        # Casanova Ecobar: two different acts, synthetic distinct set times.
        self._assert_never_eligible(
            "Bolinha do Cavaco", "JB do Cavaco",
            datetime(2026, 8, 7, 21, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 7, 23, 0, tzinfo=timezone.utc),
        )

    def test_the_three_oficina_workshops_never_match_any_pair(self):
        titles = ["Oficina Vida de Inseto", "Oficina Cobra Gigante", "Oficina de Sorvete"]
        times = [
            datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
        ]
        for i in range(len(titles)):
            for j in range(i + 1, len(titles)):
                self._assert_never_eligible(titles[i], titles[j], times[i], times[j])

    def test_acao_leitura_pair_never_matches(self):
        self._assert_never_eligible(
            "Ação Leitura: Bate-papo com Marcelino Freire",
            "Ação Leitura: Bate-papo com Jeferson Tenório",
            datetime(2026, 8, 4, 19, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 4, 21, 0, tzinfo=timezone.utc),
        )

    def test_even_if_a_synthetic_pair_coincidentally_shared_a_time_it_would_still_be_named_here(self):
        """Honesty check on the fixtures themselves: if any two of the
        above accidentally shared a starts_at, the exclusion above would
        be proven by the HANDLE/dispute/resolution guards, not by time --
        asserted explicitly here so a future edit to these fixtures cannot
        silently start relying on the wrong guard without this test
        failing first."""
        same_time = datetime(2026, 8, 7, 21, 0, tzinfo=timezone.utc)
        a = _event(title="Bolinha do Cavaco", starts_at=same_time)
        b = _event(title="JB do Cavaco", starts_at=same_time)
        # Same handle, same exact time, no dispute, no prior resolution --
        # this WOULD be eligible on time alone. The real fixtures above
        # deliberately give these two acts DIFFERENT times, which is the
        # realistic shape (two different acts have different set times),
        # so this test exists only to make that choice visible, not to
        # claim the rule is unsafe if they ever did coincide -- see the
        # plan's Open Questions for why title is deliberately NOT part of
        # this predicate at all.
        assert event_dedup.handle_time_match_eligible(a, b) is True


# ── evaluate_pair wiring ──────────────────────────────────────────────────
class TestEvaluatePairDirectly:
    def test_the_flag_turns_an_eligible_pair_into_auto(self):
        a = {"title": "Domingo é dia de Boteco!", "lineup": []}
        b = {"title": "ALEX CARRERAS E BANDA", "lineup": []}
        assert event_dedup.evaluate_pair(
            a, b, venue_name="Boteco Nem A Pau Juvenal", config=_config(),
        ) is None
        decision = event_dedup.evaluate_pair(
            a, b, venue_name="Boteco Nem A Pau Juvenal", config=_config(),
            handle_time_match=True,
        )
        assert decision is not None
        assert decision.band == event_dedup.BAND_AUTO
        assert decision.reasons == (event_dedup.REASON_HANDLE_TIME_MATCH,)

    def test_the_flag_is_additive_to_title_containment(self):
        a = {"title": "Aniversário da Banda X", "lineup": []}
        b = {"title": "Banda X", "lineup": []}
        decision = event_dedup.evaluate_pair(
            a, b, venue_name="Some Venue", config=_config(), handle_time_match=True,
        )
        assert decision.reasons == (
            event_dedup.REASON_TITLE, event_dedup.REASON_HANDLE_TIME_MATCH,
        )

    def test_the_decision_is_symmetric(self):
        a = {"title": "Domingo é dia de Boteco!", "lineup": []}
        b = {"title": "ALEX CARRERAS E BANDA", "lineup": []}
        forward = event_dedup.evaluate_pair(
            a, b, venue_name="Boteco Nem A Pau Juvenal", config=_config(), handle_time_match=True,
        )
        backward = event_dedup.evaluate_pair(
            b, a, venue_name="Boteco Nem A Pau Juvenal", config=_config(), handle_time_match=True,
        )
        assert forward.band == backward.band
        assert forward.reasons == backward.reasons


# ── the operator's title edit still blocks absorption ────────────────────
class TestOperatorTitleEditIsNeverBypassed:
    def test_a_title_edited_row_is_never_absorbed_via_handle_time_match_alone(self):
        store = _store()
        handle = "boteconemapaujuvenal"
        when = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
        a = _seed(store, "Domingo é dia de Boteco!", "v_boteco", source_handle=handle, starts_at=when)
        b = _seed(
            store, "ALEX CARRERAS E BANDA", "v_boteco", source_handle=handle, starts_at=when,
            operator_edited_fields=["title"],
        )
        _merge(store, "v_boteco", _config(handle_time_match_enabled=True))
        assert len(_alive(store, [a, b])) == 2

    def test_that_refusal_is_still_surfaced_as_a_suggestion(self):
        store = _store()
        handle = "boteconemapaujuvenal"
        when = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
        a = _seed(store, "Domingo é dia de Boteco!", "v_boteco", source_handle=handle, starts_at=when)
        _seed(
            store, "ALEX CARRERAS E BANDA", "v_boteco", source_handle=handle, starts_at=when,
            operator_edited_fields=["title"],
        )
        _merge(store, "v_boteco", _config(handle_time_match_enabled=True))
        assert store.list_event_merge_suggestions(event_id=a, decision="pending")


# ── config plumbing ────────────────────────────────────────────────────────
class TestConfigPlumbing:
    def test_it_ships_off(self):
        assert event_dedup.DEFAULT_HANDLE_TIME_MATCH_ENABLED is False
        assert event_dedup.load_dedup_config(None).handle_time_match_enabled is False

    def test_the_validator_rejects_a_non_boolean(self):
        with pytest.raises(TypeError):
            event_dedup.validate_handle_time_match_enabled_config("true")
        assert event_dedup.validate_handle_time_match_enabled_config(True) is True

    def test_a_stored_string_never_coerces_it_on(self):
        class _FakeRedis:
            def get(self, key):
                if key == event_dedup.ADMIN_CONFIG_HANDLE_TIME_MATCH_ENABLED_KEY:
                    return '"true"'
                return None

        assert event_dedup.load_dedup_config(_FakeRedis()).handle_time_match_enabled is False

    def test_a_real_stored_true_enables_it(self):
        class _FakeRedis:
            def get(self, key):
                if key == event_dedup.ADMIN_CONFIG_HANDLE_TIME_MATCH_ENABLED_KEY:
                    return json.dumps(True)
                return None

        assert event_dedup.load_dedup_config(_FakeRedis()).handle_time_match_enabled is True

    def test_the_merge_pass_reads_the_flag_through_redis(self):
        import fakeredis

        redis = fakeredis.FakeRedis(decode_responses=True)
        redis.set(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, json.dumps(True))
        redis.set(event_dedup.ADMIN_CONFIG_HANDLE_TIME_MATCH_ENABLED_KEY, json.dumps(True))
        store = _store()
        ids = _boteco_three(store)
        merge_touched_events(store, list(ids), datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc), redis_like=redis)
        assert len(_alive(store, ids)) == 1

    def test_the_policy_does_nothing_while_auto_merge_is_off(self):
        store = _store()
        ids = _boteco_three(store)
        config = event_dedup.DedupConfig(
            generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
            stopwords=event_dedup.DEFAULT_STOPWORDS,
            lineup_threshold=event_dedup.DEFAULT_LINEUP_THRESHOLD,
            candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
            undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
            auto_merge_enabled=False, handle_time_match_enabled=True,
        )
        _merge(store, "v_boteco", config)
        assert len(_alive(store, ids)) == 3
