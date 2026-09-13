"""Unit tests for app/services/event_display_title.py.

See plans/260912_events-venue-night-duplication.md §G. Three things get
pinned here, and the order matters:

1. `obvious_canonical_title` against the PRODUCTION strings — the Rodolpho
   family returns the containing title with NO model call, Club Metrópole's
   five acts return `None`. This is the predicate that decides whether a call
   happens at all, so it is the reason §G does not contradict `260812` §C:
   even the decision to CALL is deterministic.
2. The validation gate. A model answer is written only if it can be built
   from what the posts actually said; anything else leaves `display_title`
   NULL and the row serving its stored title.
3. The wiring guard on the prompt itself, in the shape of
   `tests/test_event_extraction_prompt_kind.py` — BDD must not depend on a
   live model (CLAUDE.md's BDD policy forbids live OpenAI in BDD), so the
   prompt's load-bearing sentence is pinned here instead of by a scenario.

Division of labour against the BDD sibling
(`tests/bdd/persistence/events-venue-night-duplication-display-title.feature`):
the scenarios there prove the chosen title reaches the app through the real
projector, and that a model answer can never change which rows survive. This
file pins the pure functions and the DAO round-trip the fake alone would hide.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.api.openai_event_extraction_client import (
    ENDPOINT,
    ENDPOINT_TITLE_PICK,
    TITLE_PICK_PROMPT,
    build_title_pick_prompt,
)
from app.models.venue import Venue
from app.services import event_dedup
from app.services.event_display_title import (
    DEFAULT_DISPLAY_TITLE_ENABLED,
    MAX_DISPLAY_TITLE_LENGTH,
    OUTCOME_ERROR,
    OUTCOME_LLM_ACCEPTED,
    OUTCOME_OBVIOUS,
    OUTCOME_REJECTED,
    OUTCOME_SKIPPED_OPERATOR_EDITED,
    EventDisplayTitleService,
    load_display_title_enabled,
    obvious_canonical_title,
    parse_display_title_response,
    validate_display_title,
    validate_display_title_enabled_config,
)
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

RECIFE = ZoneInfo("America/Recife")
_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
_SATURDAY = datetime(2026, 9, 12, 22, 0, tzinfo=RECIFE)

_CONFIG = event_dedup.DedupConfig(
    generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
    stopwords=event_dedup.DEFAULT_STOPWORDS,
    lineup_threshold=event_dedup.DEFAULT_LINEUP_THRESHOLD,
    candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
    undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
    auto_merge_enabled=True,
)

# The production strings, from the plan's own §G and the RCA.
_RODOLPHO = ["Rodolpho", "Rodolpho Produções", "31º Rodolpho Produções"]
_FIVE_ACTS = [
    "ROWKA", "VITINHO POLÊMICO", "SÁBADO VAI FERVER",
    "SECRET CLUB com @neguindabasersv", "estreia de @neguindabasersv",
]


def _obvious(titles, venue_name=None):
    return obvious_canonical_title(titles, venue_name=venue_name, config=_CONFIG)


class TestObviousCanonicalTitle:
    def test_the_rodolpho_family_returns_the_containing_title(self):
        assert _obvious(_RODOLPHO) == "31º Rodolpho Produções"

    def test_the_rodolpho_family_is_order_independent(self):
        assert _obvious(list(reversed(_RODOLPHO))) == "31º Rodolpho Produções"

    def test_a_two_member_containment_returns_the_fuller_title(self):
        assert _obvious(["Rodolpho", "Rodolpho Produções"]) == "Rodolpho Produções"

    def test_club_metropole_s_five_acts_have_no_obvious_pick(self):
        assert _obvious(_FIVE_ACTS) is None

    def test_titles_differing_only_in_case_share_one_normalisation(self):
        chosen = _obvious(["NOITE DA PATROA", "Noite da Patroa"])
        assert chosen in ("NOITE DA PATROA", "Noite da Patroa")

    def test_the_oldest_source_wins_a_tie(self):
        # `source_titles` arrives oldest-first (what `list_event_sources`
        # returns), and the tie-break takes the oldest of several equally
        # maximal titles.
        assert _obvious(["NOITE DA PATROA", "Noite da Patroa"]) == "NOITE DA PATROA"
        assert _obvious(["Noite da Patroa", "NOITE DA PATROA"]) == "Noite da Patroa"

    def test_an_empty_distinctive_set_never_wins(self):
        # `SEXTOU NO CONCHITTAS BAR!` at Conchittas Bar reduces to an EMPTY
        # distinctive set (every token is generic or the venue's own name),
        # so it can never be the maximum and must never be chosen over a
        # title that actually says something.
        chosen = _obvious(
            ["SEXTOU NO CONCHITTAS BAR!", "Rodolpho Produções"], venue_name="Conchittas Bar",
        )
        assert chosen == "Rodolpho Produções"

    def test_an_empty_distinctive_set_never_forces_a_call_either(self):
        chosen = _obvious(
            ["SEXTOU NO CONCHITTAS BAR!", "Rodolpho", "Rodolpho Produções"],
            venue_name="Conchittas Bar",
        )
        assert chosen == "Rodolpho Produções"

    def test_an_all_generic_group_is_answered_without_a_call(self):
        # Nothing for a model to choose BETWEEN either — the oldest post's
        # own words are as good an answer as exists, and cost nothing.
        assert _obvious(["Sextou", "Festa"]) == "Sextou"

    def test_a_single_title_is_its_own_obvious_pick(self):
        assert _obvious(["Rodolpho Produções"]) == "Rodolpho Produções"

    def test_an_empty_group_has_no_pick(self):
        assert _obvious([]) is None
        assert _obvious([None, ""]) is None

    def test_the_venue_s_own_name_is_stripped_before_comparing(self):
        # Without the venue-name strip, "Rodolpho no Club Metrópole" would
        # reduce to {rodolpho, club, metropole} — non-comparable with
        # {rodolpho, producoes} — and would force a needless model call.
        assert _obvious(
            ["Rodolpho no Club Metrópole", "Rodolpho Produções"],
            venue_name="Club Metrópole",
        ) == "Rodolpho Produções"

    def test_maximal_titles_that_disagree_on_wording_have_no_obvious_pick(self):
        # Equal distinctive sets are not enough: both of these reduce to
        # {patroa}, so neither contains the other, and the two SPELLINGS are
        # genuinely different strings a reader would notice. That is a
        # decision for the model, not a coin toss here.
        assert _obvious(
            ["Noite da Patroa", "Noite da Patroa no Club Metrópole"],
            venue_name="Club Metrópole",
        ) is None


class TestValidationGate:
    def _validate(self, candidate, sources=None, venue_name=None):
        return validate_display_title(
            candidate, sources if sources is not None else _FIVE_ACTS,
            venue_name=venue_name, config=_CONFIG,
        )

    def test_a_title_built_from_what_the_posts_said_is_accepted(self):
        assert self._validate("ROWKA e VITINHO POLÊMICO") == "ROWKA e VITINHO POLÊMICO"

    def test_a_title_that_is_one_of_the_source_titles_is_accepted(self):
        assert self._validate("SÁBADO VAI FERVER") == "SÁBADO VAI FERVER"

    def test_a_title_introducing_an_unseen_proper_noun_is_rejected(self):
        # The failure this gate exists for: a fabricated act name on a card a
        # reader will believe.
        assert self._validate("ROWKA e ANITTA") is None

    def test_a_blank_title_is_rejected(self):
        assert self._validate("") is None
        assert self._validate("   ") is None

    def test_a_punctuation_only_title_is_rejected(self):
        assert self._validate("!!! ---") is None

    def test_a_non_string_answer_is_rejected(self):
        assert self._validate(None) is None
        assert self._validate(42) is None
        assert self._validate(["ROWKA"]) is None

    def test_an_over_long_title_is_rejected(self):
        long_title = "ROWKA " * 40
        assert len(long_title) > MAX_DISPLAY_TITLE_LENGTH
        assert self._validate(long_title) is None

    def test_a_title_of_exactly_the_maximum_length_is_accepted(self):
        padded = "ROWKA".ljust(MAX_DISPLAY_TITLE_LENGTH, " ")
        assert self._validate(padded) == "ROWKA"

    def test_a_generic_only_title_is_accepted_because_it_invents_nothing(self):
        # `Noite Especial` has an EMPTY distinctive set, which is trivially a
        # subset of the union: it says nothing the posts did not, which is
        # exactly what the gate checks for.
        assert self._validate("Noite Especial") == "Noite Especial"

    def test_the_venue_s_own_name_is_never_treated_as_an_invention(self):
        assert validate_display_title(
            "Noite da Patroa no Club Metrópole", ["Noite da Patroa"],
            venue_name="Club Metrópole", config=_CONFIG,
        ) == "Noite da Patroa no Club Metrópole"


class TestParseResponse:
    def test_a_well_formed_answer_parses(self):
        assert parse_display_title_response('{"display_title": "ROWKA"}') == "ROWKA"

    def test_a_fenced_answer_parses(self):
        assert parse_display_title_response(
            '```json\n{"display_title": "ROWKA"}\n```',
        ) == "ROWKA"

    def test_anything_malformed_is_a_rejection_never_a_partial_write(self):
        for raw in ("", None, "not json", "[]", '{"other": "x"}', '{"display_title": 7}'):
            assert parse_display_title_response(raw) is None


# ── the pass itself ─────────────────────────────────────────────────────────
class _FakeOpenAI:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.calls: list = []

    async def pick_display_title(self, *, venue_name, local_date, source_titles, lineup):
        self.calls.append({
            "venue_name": venue_name, "local_date": local_date,
            "source_titles": list(source_titles), "lineup": list(lineup),
        })
        if not self._answers:
            raise AssertionError("the fake client was called more times than programmed")
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _store():
    store = InMemoryRdsVenueStore()
    store.upsert_venue(Venue(
        venue_id="v_club", venue_name="Club Metrópole", venue_lat=-8.05, venue_lng=-34.88,
    ))
    return store


_SEQ = {"n": 0}


def _seed_merged(store, source_titles, *, operator_edited_fields=None, display_title=None,
                 lineup=None):
    """One canonical row carrying one source per title — the shape a merge
    leaves behind (the rows fold, the sources never do)."""
    _SEQ["n"] += 1
    event_id = new_event_id()
    for index, title in enumerate(source_titles):
        fields = {
            "event_id": event_id, "venue_id": "v_club", "starts_at": _SATURDAY,
            "title": source_titles[0], "post_type": "event", "status": "accepted",
            "lineup": lineup or [], "operator_edited_fields": operator_edited_fields,
            "display_title": display_title,
            "source_kind": "venue_post", "source_handle": "clubmetropole",
            "source_shortcode": f"dt_sc_{_SEQ['n']}_{index}",
            "raw_extraction": {"title": title},
            "first_seen_at": _NOW, "last_seen_at": _NOW,
        }
        if index == 0:
            store.insert_event(fields)
        else:
            store.insert_event({**fields, "event_id": new_event_id()})
            # Reattach so all the titles hang off ONE canonical, exactly as
            # `_finish_absorption` leaves them.
            other = [
                s for s in store.list_all_event_sources()
                if s["source_shortcode"] == fields["source_shortcode"]
            ][0]
            store.reattach_event_source_by_id(other["id"], event_id)
    return event_id


def _run(service, ids):
    return asyncio.run(service.run_for_events(ids, config=_CONFIG))


@pytest.fixture
def enabled_redis():
    import fakeredis

    from app.services.event_display_title import ADMIN_CONFIG_DISPLAY_TITLE_ENABLED_KEY

    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.set(ADMIN_CONFIG_DISPLAY_TITLE_ENABLED_KEY, json.dumps(True))
    return redis


class TestTheCallIsAvoidedWhereverPossible:
    def test_an_obvious_group_never_calls_the_model(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, ["Rodolpho", "Rodolpho Produções"])
        client = _FakeOpenAI()
        counts = _run(EventDisplayTitleService(store, client, redis_client=enabled_redis), [event_id])
        assert client.calls == []
        assert counts == {OUTCOME_OBVIOUS: 1}
        assert store.get_event(event_id)["display_title"] == "Rodolpho Produções"

    def test_a_single_source_group_never_calls_the_model(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, ["ROWKA"])
        client = _FakeOpenAI()
        _run(EventDisplayTitleService(store, client, redis_client=enabled_redis), [event_id])
        assert client.calls == []
        assert store.get_event(event_id)["display_title"] is None

    def test_an_operator_edited_title_never_calls_the_model(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, _FIVE_ACTS, operator_edited_fields=["title"])
        client = _FakeOpenAI()
        counts = _run(EventDisplayTitleService(store, client, redis_client=enabled_redis), [event_id])
        assert client.calls == []
        assert counts == {OUTCOME_SKIPPED_OPERATOR_EDITED: 1}
        assert store.get_event(event_id)["display_title"] is None

    def test_an_already_titled_group_never_calls_the_model(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, _FIVE_ACTS, display_title="ROWKA e amigos")
        client = _FakeOpenAI()
        _run(EventDisplayTitleService(store, client, redis_client=enabled_redis), [event_id])
        assert client.calls == []
        assert store.get_event(event_id)["display_title"] == "ROWKA e amigos"

    def test_the_pass_does_nothing_at_all_while_disabled(self):
        store = _store()
        event_id = _seed_merged(store, ["Rodolpho", "Rodolpho Produções"])
        client = _FakeOpenAI()
        counts = _run(EventDisplayTitleService(store, client, redis_client=None), [event_id])
        assert counts == {}
        assert client.calls == []
        assert store.get_event(event_id)["display_title"] is None


class TestTheOneCall:
    def test_exactly_one_call_per_group_with_every_source_title(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, _FIVE_ACTS)
        client = _FakeOpenAI(json.dumps({"display_title": "ROWKA e VITINHO POLÊMICO"}))
        counts = _run(EventDisplayTitleService(store, client, redis_client=enabled_redis), [event_id])
        assert len(client.calls) == 1
        assert set(client.calls[0]["source_titles"]) == set(_FIVE_ACTS)
        assert counts == {OUTCOME_LLM_ACCEPTED: 1}
        assert store.get_event(event_id)["display_title"] == "ROWKA e VITINHO POLÊMICO"

    def test_a_duplicate_event_id_is_still_one_call(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, _FIVE_ACTS)
        client = _FakeOpenAI(json.dumps({"display_title": "ROWKA"}))
        _run(
            EventDisplayTitleService(store, client, redis_client=enabled_redis),
            [event_id, event_id, event_id],
        )
        assert len(client.calls) == 1

    def test_an_invented_word_is_rejected_and_nothing_is_written(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, _FIVE_ACTS)
        client = _FakeOpenAI(json.dumps({"display_title": "ROWKA e ANITTA"}))
        counts = _run(EventDisplayTitleService(store, client, redis_client=enabled_redis), [event_id])
        assert counts == {OUTCOME_REJECTED: 1}
        assert store.get_event(event_id)["display_title"] is None

    def test_a_failed_call_is_counted_and_nothing_is_written(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, _FIVE_ACTS)
        client = _FakeOpenAI(RuntimeError("the API is down"))
        counts = _run(EventDisplayTitleService(store, client, redis_client=enabled_redis), [event_id])
        assert counts == {OUTCOME_ERROR: 1}
        assert store.get_event(event_id)["display_title"] is None

    def test_a_second_pass_makes_no_further_call(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, _FIVE_ACTS)
        client = _FakeOpenAI(json.dumps({"display_title": "ROWKA"}))
        service = EventDisplayTitleService(store, client, redis_client=enabled_redis)
        _run(service, [event_id])
        _run(service, [event_id])
        assert len(client.calls) == 1
        assert store.get_event(event_id)["display_title"] == "ROWKA"

    def test_a_superseded_row_is_never_given_a_title(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, _FIVE_ACTS)
        store.update_event(event_id, {"status": "superseded", "superseded_by": "evt_other"})
        client = _FakeOpenAI()
        _run(EventDisplayTitleService(store, client, redis_client=enabled_redis), [event_id])
        assert client.calls == []

    def test_no_client_at_all_is_an_error_not_a_crash(self, enabled_redis):
        store = _store()
        event_id = _seed_merged(store, _FIVE_ACTS)
        counts = _run(EventDisplayTitleService(store, None, redis_client=enabled_redis), [event_id])
        assert counts == {OUTCOME_ERROR: 1}
        assert store.get_event(event_id)["display_title"] is None


class TestConfig:
    def test_the_pass_ships_disabled(self):
        assert DEFAULT_DISPLAY_TITLE_ENABLED is False
        assert load_display_title_enabled(None) is False

    def test_a_stored_string_never_coerces_it_on(self):
        class _FakeRedis:
            def get(self, key):
                return '"true"'

        assert load_display_title_enabled(_FakeRedis()) is False

    def test_the_validator_rejects_a_non_boolean(self):
        with pytest.raises(TypeError):
            validate_display_title_enabled_config("true")
        assert validate_display_title_enabled_config(False) is False


class TestPromptWiring:
    """The shape `tests/test_event_extraction_prompt_kind.py` establishes: a
    prompt's load-bearing sentence is pinned by a unit test, never by a
    scenario — BDD must not depend on a live model."""

    def test_the_prompt_tells_the_model_to_choose_only_from_what_the_posts_say(self):
        assert "Choose only from what these posts say" in TITLE_PICK_PROMPT

    def test_the_prompt_forbids_inventing_a_performer(self):
        lowered = TITLE_PICK_PROMPT.lower()
        assert "do not invent" in lowered
        assert "performer" in lowered

    def test_the_prompt_states_the_length_limit_it_is_validated_against(self):
        assert str(MAX_DISPLAY_TITLE_LENGTH) in TITLE_PICK_PROMPT

    def test_the_prompt_asks_for_json_only(self):
        assert '{"display_title": "..."}' in TITLE_PICK_PROMPT

    def test_the_built_prompt_carries_every_source_title_and_the_lineup(self):
        prompt = build_title_pick_prompt(
            venue_name="Club Metrópole", local_date="2026-09-12",
            source_titles=_FIVE_ACTS, lineup=["@neguindabasersv"],
        )
        for title in _FIVE_ACTS:
            assert title in prompt
        assert "@neguindabasersv" in prompt
        assert "Club Metrópole" in prompt
        assert "2026-09-12" in prompt

    def test_title_pick_spend_is_never_conflated_with_extraction_spend(self):
        assert ENDPOINT_TITLE_PICK != ENDPOINT
        assert ENDPOINT_TITLE_PICK == "event_title_pick"


class TestDaoRoundTrip:
    """The failure `RdsVenueStore._EVENT_COLUMNS`' own comment warns about:
    omit a new column from that allowlist and the SQL store silently no-ops
    the write while the in-memory fake accepts it — passing every BDD
    scenario and failing only in prod. The fake cannot catch that, so this
    asserts the allowlist itself."""

    def test_display_title_is_in_the_event_column_allowlist(self):
        from app.dao.rds_venue_store import RdsVenueStore

        assert "display_title" in RdsVenueStore._EVENT_COLUMNS

    def test_the_fake_store_round_trips_the_column(self):
        store = _store()
        event_id = _seed_merged(store, ["Rodolpho", "Rodolpho Produções"])
        store.update_event(event_id, {"display_title": "Rodolpho Produções"})
        assert store.get_event(event_id)["display_title"] == "Rodolpho Produções"
        store.update_event(event_id, {"display_title": None})
        assert store.get_event(event_id)["display_title"] is None


class TestMergeClearsAStaleTitle:
    def test_absorbing_a_new_source_clears_the_canonical_s_display_title(self):
        from app.services.event_merge import merge_touched_events

        import fakeredis

        redis = fakeredis.FakeRedis(decode_responses=True)
        redis.set(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, json.dumps(True))
        store = _store()
        a = _seed_merged(store, ["Rodolpho"], display_title="Rodolpho")
        b = _seed_merged(store, ["Rodolpho Produções"])
        merge_touched_events(store, [a, b], _NOW, redis_like=redis)
        survivors = [
            eid for eid in (a, b)
            if store.get_event(eid) and store.get_event(eid)["status"] != "superseded"
        ]
        assert len(survivors) == 1
        assert store.get_event(survivors[0])["display_title"] is None, (
            "a title chosen for a three-act listing must not survive it becoming five"
        )
