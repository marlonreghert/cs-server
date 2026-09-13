"""Unit tests for the single-night-venue policy.

See plans/260912_events-venue-night-duplication.md §E2. The operator's ask —
"events at the same place must never overlap" — DIRECTLY CONTRADICTS a prior
measured decision in this repo: `260812_event-dedup-fuzzy-title.md` names
`Bolinha do Cavaco` / `JB do Cavaco` at Casanova Ecobar as a false positive
that must never merge (two different acts, one venue, one night), and Club
Metrópole's five acts on one Saturday are the SAME SHAPE. Nothing in the rows
tells the two situations apart; the difference is a fact about the venue.

So the policy is a per-venue LIST, empty by default, and these tests pin both
halves of that: a listed venue's night collapses, an unlisted venue's night
does not, and — the part that matters most — the policy never bypasses a
single protection the auto band already applies.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from prometheus_client import REGISTRY

from app.models.venue import Venue
from app.services import event_dedup
from app.services.event_merge import merge_touched_events, run_title_similarity_pass
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

RECIFE = ZoneInfo("America/Recife")
_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
_SATURDAY = datetime(2026, 9, 12, 22, 0, tzinfo=RECIFE)

# Club Metrópole's five real posts for Saturday 12/09 (the RCA's own cluster).
_FIVE_ACTS = (
    ("ROWKA", ["ROWKA"]),
    ("VITINHO POLÊMICO", ["VITINHO POLÊMICO"]),
    ("SÁBADO VAI FERVER", ["MC Baixinho"]),
    ("SECRET CLUB com @neguindabasersv", ["@neguindabasersv"]),
    ("estreia de @neguindabasersv", ["@neguindabasersv"]),
)
# `260812`'s own measured false positives — the corpus that set the bar.
_CASANOVA = ("Bolinha do Cavaco", "JB do Cavaco")
_WORKSHOPS = ("Oficina Vida de Inseto", "Oficina Cobra Gigante", "Oficina de Sorvete")

_SEQ = {"n": 0}


def _store(*single_night_venue_ids):
    store = InMemoryRdsVenueStore()
    store.upsert_venue(Venue(
        venue_id="v_club", venue_name="Club Metrópole", venue_lat=-8.05, venue_lng=-34.88,
    ))
    store.upsert_venue(Venue(
        venue_id="v_bode", venue_name="Entre Amigos O Bode", venue_lat=-8.05, venue_lng=-34.88,
    ))
    store.upsert_venue(Venue(
        venue_id="v_casanova", venue_name="Casanova Ecobar", venue_lat=-8.05, venue_lng=-34.88,
    ))
    return store


def _config(*single_night_venue_ids, lineup_threshold=None):
    return event_dedup.DedupConfig(
        generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
        stopwords=event_dedup.DEFAULT_STOPWORDS,
        lineup_threshold=lineup_threshold or event_dedup.DEFAULT_LINEUP_THRESHOLD,
        candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
        undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
        auto_merge_enabled=True,
        single_night_venues=tuple(single_night_venue_ids),
    )


def _seed(store, title, venue_id="v_club", *, lineup=None, status="pending_review",
          post_type="event", operator_edited_fields=None, starts_at=_SATURDAY):
    _SEQ["n"] += 1
    event_id = new_event_id()
    store.insert_event({
        "event_id": event_id, "venue_id": venue_id, "starts_at": starts_at,
        "title": title, "post_type": post_type, "status": status,
        "lineup": lineup or [], "operator_edited_fields": operator_edited_fields,
        "source_kind": "venue_post", "source_handle": "clubmetropole",
        "source_shortcode": f"sn_sc_{_SEQ['n']}",
        "first_seen_at": _NOW, "last_seen_at": _NOW,
    })
    return event_id


def _seed_cluster(store, venue_id="v_club"):
    return [_seed(store, title, venue_id, lineup=list(acts)) for title, acts in _FIVE_ACTS]


def _alive(store, ids):
    return [
        eid for eid in ids
        if store.get_event(eid) is not None and store.get_event(eid)["status"] != "superseded"
    ]


def _merge(store, venue_id, config):
    run_title_similarity_pass(store, venue_id, _NOW, config=config)


def _merge_outcome(outcome: str) -> float:
    return REGISTRY.get_sample_value(
        "event_merge_total", {"identity": "title", "outcome": outcome},
    ) or 0.0


class TestTheListedVenue:
    def test_a_listed_venue_s_five_acts_collapse_to_one_listing(self):
        store = _store()
        ids = _seed_cluster(store)
        _merge(store, "v_club", _config("v_club"))
        survivors = _alive(store, ids)
        assert len(survivors) == 1, [store.get_event(e)["title"] for e in survivors]

    def test_the_surviving_listing_names_every_act(self):
        store = _store()
        ids = _seed_cluster(store)
        _merge(store, "v_club", _config("v_club"))
        lineup = set(store.get_event(_alive(store, ids)[0])["lineup"])
        for _title, acts in _FIVE_ACTS:
            assert set(acts) <= lineup, (acts, lineup)

    def test_every_absorbed_row_is_superseded_not_deleted(self):
        store = _store()
        ids = _seed_cluster(store)
        _merge(store, "v_club", _config("v_club"))
        survivor = _alive(store, ids)[0]
        for event_id in ids:
            if event_id == survivor:
                continue
            row = store.get_event(event_id)
            assert row is not None, "a single-night merge must be reversible"
            assert row["superseded_by"] == survivor

    def test_the_reason_token_reaches_the_audit_row(self):
        store = _store()
        ids = _seed_cluster(store)
        _merge(store, "v_club", _config("v_club"))
        audited = [
            s for s in store.list_event_merge_suggestions(decision="auto_merged")
        ]
        assert audited, "no auto_merged audit row was written"
        assert any(
            event_dedup.REASON_SINGLE_NIGHT_VENUE in (s.get("reasons") or [])
            for s in audited
        ), audited

    def test_the_policy_gets_its_own_metric_outcome(self):
        before = _merge_outcome("merged_single_night_venue")
        store = _store()
        _seed_cluster(store)
        _merge(store, "v_club", _config("v_club"))
        assert _merge_outcome("merged_single_night_venue") > before

    def test_a_pair_that_would_have_merged_anyway_stays_under_plain_merged(self):
        # `Rodolpho` / `Rodolpho Produções` passes title containment, so the
        # policy did not cause that merge and must not claim it.
        before_policy = _merge_outcome("merged_single_night_venue")
        before_plain = _merge_outcome("merged")
        store = _store()
        _seed(store, "Rodolpho")
        _seed(store, "Rodolpho Produções")
        _merge(store, "v_club", _config("v_club"))
        assert _merge_outcome("merged") > before_plain
        assert _merge_outcome("merged_single_night_venue") == before_policy


class TestTheUnlistedVenue:
    def test_an_unlisted_venue_s_night_is_left_completely_alone(self):
        store = _store()
        ids = _seed_cluster(store)
        _merge(store, "v_club", _config())
        assert len(_alive(store, ids)) == 5

    def test_the_shipped_default_list_is_empty(self):
        assert event_dedup.DEFAULT_SINGLE_NIGHT_VENUES == ()
        assert event_dedup.load_dedup_config(None).single_night_venues == ()

    def test_260812_s_measured_false_positives_still_never_auto_merge_unlisted(self):
        # The regression test the whole bar rests on: these pairs must stay
        # apart for a venue that is NOT on the list.
        store = _store()
        casanova = [_seed(store, title, "v_casanova") for title in _CASANOVA]
        bode = [_seed(store, title, "v_bode") for title in _WORKSHOPS]
        _merge(store, "v_casanova", _config())
        _merge(store, "v_bode", _config())
        assert len(_alive(store, casanova)) == 2
        assert len(_alive(store, bode)) == 3

    def test_listing_one_venue_never_affects_another(self):
        store = _store()
        club = _seed_cluster(store, "v_club")
        bode = [_seed(store, title, "v_bode") for title in _WORKSHOPS]
        config = _config("v_club")
        _merge(store, "v_club", config)
        _merge(store, "v_bode", config)
        assert len(_alive(store, club)) == 1
        assert len(_alive(store, bode)) == 3


class TestTheProtectionsAreNeverBypassed:
    def test_two_confirmed_rows_are_left_entirely_alone(self):
        store = _store()
        ids = [
            _seed(store, title, status="confirmed") for title, _acts in _FIVE_ACTS[:2]
        ]
        _merge(store, "v_club", _config("v_club"))
        assert len(_alive(store, ids)) == 2

    def test_a_row_whose_operator_edited_its_title_is_never_absorbed(self):
        store = _store()
        a = _seed(store, _FIVE_ACTS[0][0])
        b = _seed(store, _FIVE_ACTS[1][0], operator_edited_fields=["title"])
        _merge(store, "v_club", _config("v_club"))
        assert len(_alive(store, [a, b])) == 2

    def test_that_refusal_is_still_surfaced_as_a_suggestion(self):
        store = _store()
        a = _seed(store, _FIVE_ACTS[0][0])
        _seed(store, _FIVE_ACTS[1][0], operator_edited_fields=["title"])
        _merge(store, "v_club", _config("v_club"))
        assert store.list_event_merge_suggestions(event_id=a, decision="pending")

    def test_a_row_whose_operator_edited_its_venue_is_never_absorbed(self):
        store = _store()
        a = _seed(store, _FIVE_ACTS[0][0])
        b = _seed(store, _FIVE_ACTS[1][0], operator_edited_fields=["venue_id"])
        _merge(store, "v_club", _config("v_club"))
        assert len(_alive(store, [a, b])) == 2

    def test_a_non_event_row_never_enters_the_candidate_pool(self):
        store = _store()
        event_id = _seed(store, _FIVE_ACTS[0][0])
        greeting = _seed(store, "31 Anos", post_type="other")
        _merge(store, "v_club", _config("v_club"))
        assert _alive(store, [greeting]) == [greeting]
        assert store.get_event(greeting).get("superseded_by") is None
        assert _alive(store, [event_id]) == [event_id]

    def test_rows_on_different_nights_are_still_two_nights(self):
        store = _store()
        saturday = _seed(store, _FIVE_ACTS[0][0])
        sunday = _seed(
            store, _FIVE_ACTS[1][0],
            starts_at=datetime(2026, 9, 13, 22, 0, tzinfo=RECIFE),
        )
        _merge(store, "v_club", _config("v_club"))
        assert len(_alive(store, [saturday, sunday])) == 2

    def test_the_policy_does_nothing_while_auto_merge_is_off(self):
        store = _store()
        ids = _seed_cluster(store)
        config = event_dedup.DedupConfig(
            generic_vocabulary=event_dedup.DEFAULT_GENERIC_VOCABULARY,
            stopwords=event_dedup.DEFAULT_STOPWORDS,
            lineup_threshold=event_dedup.DEFAULT_LINEUP_THRESHOLD,
            candidate_window_hours=event_dedup.DEFAULT_CANDIDATE_WINDOW_HOURS,
            undated_window_days=event_dedup.DEFAULT_UNDATED_WINDOW_DAYS,
            auto_merge_enabled=False, single_night_venues=("v_club",),
        )
        _merge(store, "v_club", config)
        assert len(_alive(store, ids)) == 5


class TestDeterminism:
    def test_the_outcome_is_the_same_across_two_candidate_orderings(self):
        titles = [title for title, _acts in _FIVE_ACTS]
        results = []
        for ordering in (titles, list(reversed(titles))):
            store = _store()
            ids = [_seed(store, title) for title in ordering]
            _merge(store, "v_club", _config("v_club"))
            survivor = _alive(store, ids)[0]
            results.append(store.get_event(survivor)["title"])
        # The canonical is the OLDEST event_id in both runs, which is the
        # first row each ordering inserted — the assertion is that ONE row
        # survives deterministically, named, never a count.
        assert results == [titles[0], titles[-1]], results

    def test_a_second_pass_changes_nothing(self):
        store = _store()
        ids = _seed_cluster(store)
        config = _config("v_club")
        _merge(store, "v_club", config)
        after_first = {
            eid: store.get_event(eid)["status"] for eid in ids
        }
        _merge(store, "v_club", config)
        assert {eid: store.get_event(eid)["status"] for eid in ids} == after_first


class TestEvaluatePairDirectly:
    def test_the_flag_turns_a_refused_pair_into_an_auto_pair(self):
        a = {"title": "ROWKA", "lineup": []}
        b = {"title": "VITINHO POLÊMICO", "lineup": []}
        assert event_dedup.evaluate_pair(
            a, b, venue_name="Club Metrópole", config=_config(),
        ) is None
        decision = event_dedup.evaluate_pair(
            a, b, venue_name="Club Metrópole", config=_config(), single_night_venue=True,
        )
        assert decision is not None
        assert decision.band == event_dedup.BAND_AUTO
        assert decision.reasons == (event_dedup.REASON_SINGLE_NIGHT_VENUE,)

    def test_the_flag_is_additive_to_the_existing_reasons(self):
        a = {"title": "Rodolpho", "lineup": []}
        b = {"title": "Rodolpho Produções", "lineup": []}
        decision = event_dedup.evaluate_pair(
            a, b, venue_name="Conchittas Bar", config=_config(), single_night_venue=True,
        )
        assert decision.reasons == (
            event_dedup.REASON_TITLE, event_dedup.REASON_SINGLE_NIGHT_VENUE,
        )

    def test_the_decision_is_symmetric(self):
        a = {"title": "ROWKA", "lineup": []}
        b = {"title": "VITINHO POLÊMICO", "lineup": []}
        forward = event_dedup.evaluate_pair(
            a, b, venue_name="Club Metrópole", config=_config(), single_night_venue=True,
        )
        backward = event_dedup.evaluate_pair(
            b, a, venue_name="Club Metrópole", config=_config(), single_night_venue=True,
        )
        assert forward.band == backward.band
        assert forward.reasons == backward.reasons


class TestConfigPlumbing:
    def test_the_list_is_read_from_admin_config(self):
        class _FakeRedis:
            def get(self, key):
                if key == event_dedup.ADMIN_CONFIG_SINGLE_NIGHT_VENUES_KEY:
                    return json.dumps(["v_club", "v_other"])
                return None

        assert event_dedup.load_dedup_config(_FakeRedis()).single_night_venues == (
            "v_club", "v_other",
        )

    def test_a_malformed_stored_value_falls_back_to_the_empty_list(self):
        class _FakeRedis:
            def get(self, key):
                if key == event_dedup.ADMIN_CONFIG_SINGLE_NIGHT_VENUES_KEY:
                    return json.dumps("v_club")  # a bare string, not a list
                return None

        assert event_dedup.load_dedup_config(_FakeRedis()).single_night_venues == ()

    def test_venue_ids_are_never_normalised(self):
        # A venue_id is an opaque key; "helpfully" lowercasing it here would
        # silently stop matching the ids the merge pass compares against.
        assert event_dedup.validate_single_night_venues_config(
            ["V_ClUb", " v_other "],
        ) == ["V_ClUb", "v_other"]

    def test_the_merge_pass_reads_the_list_through_redis(self):
        import fakeredis

        redis = fakeredis.FakeRedis(decode_responses=True)
        redis.set(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, json.dumps(True))
        redis.set(
            event_dedup.ADMIN_CONFIG_SINGLE_NIGHT_VENUES_KEY, json.dumps(["v_club"]),
        )
        store = _store()
        ids = _seed_cluster(store)
        merge_touched_events(store, list(ids), _NOW, redis_like=redis)
        assert len(_alive(store, ids)) == 1
