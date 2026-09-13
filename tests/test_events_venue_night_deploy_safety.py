"""The deploy of this branch must provably change NO stored row.

plans/260912_events-venue-night-duplication.md's central safety property, and
its first Acceptance Criterion: "every new flag defaults to today's behaviour,
and `event_dedup_lineup_threshold`'s shipped default stays 2. Demonstrated by
running the full pipeline against a seeded corpus with no config set and
asserting byte-identical rows."

This is not the same claim as "the new tests pass". Every feature in this plan
is behind a flag, and a flag is only a safety property if UNSET means UNSET.
`event_dedup_auto_merge_enabled` is ALREADY `true` in production (set
2026-08-14), so the pipeline this corpus is driven through is the LIVE one:
anything that widened a bar would merge here, on this corpus, on the very next
crawl.

Four guards, each protecting a different way that could break:

1. **Byte-identical rows.** The whole merge pass + the display-title pass +
   the backlog scan, over a corpus holding every shape this plan touches, with
   an admin-config store that has only what production has today. Every row
   must come out of it exactly as it went in.
2. **Identity is untouched.** `compute_source_event_key` and
   `compute_event_identity` over the production title list, pinned to the
   literal values they hash to. `260812` §A: migrations 0025 and 0026 call
   those functions directly against real rows, so a moved hash orphans every
   operator confirmation and turns the next run into a duplicate generator.
3. **`_finish_absorption(mode="delete")` still deletes**, so 0026's historical
   replay is provably untouched by §G's added `display_title` clear.
4. **`expand_occurrences` is unchanged**, including with §D's flag on: the
   recurring window is a MERGE-time widening and must never reach serving.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import fakeredis
import pytest

from app.models.venue import Venue
from app.services import event_dedup
from app.services.event_dedup_backlog import collect_dedup_backlog
from app.services.event_identity import compute_source_event_key
from app.services.event_merge import compute_event_identity, merge_touched_events
from app.services.event_reconciliation import new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

RECIFE = ZoneInfo("America/Recife")
_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
_SATURDAY = datetime(2026, 9, 12, 22, 0, tzinfo=RECIFE)
_WEDNESDAY_1 = datetime(2026, 8, 5, 20, 0, tzinfo=RECIFE)
_WEDNESDAY_4 = datetime(2026, 8, 26, 20, 0, tzinfo=RECIFE)

_SEQ = {"n": 0}


def _store():
    store = InMemoryRdsVenueStore()
    for venue_id, name, address in (
        ("v_club", "Club Metrópole", "R. das Ninfas, 125 - Boa Vista, Recife - PE"),
        ("v_reboco", "Sala de Reboco", "R. Gervásio Pires, 1 - Recife - PE"),
        ("v_bd_bv", "BeerDock Boa Viagem", "Av. Cons. Aguiar, 1000 - Boa Viagem, Recife - PE"),
        ("v_bd_cf", "BeerDock Casa Forte", "Av. Rui Barbosa, 500 - Casa Forte, Recife - PE"),
        ("v_casanova", "Casanova Ecobar", "R. do Sol, 9 - Recife - PE"),
        ("v_bode", "Entre Amigos O Bode", "R. Setúbal, 2 - Recife - PE"),
    ):
        store.upsert_venue(Venue(
            venue_id=venue_id, venue_name=name, venue_address=address,
            venue_lat=-8.05, venue_lng=-34.88,
        ))
    return store


def _seed(store, title, venue_id, *, starts_at=_SATURDAY, lineup=None, post_type="event",
          status="accepted", is_recurring=False, recurrence_text=None,
          location_text=None, operator_edited_fields=None, source_handle="a_handle"):
    _SEQ["n"] += 1
    event_id = new_event_id()
    store.insert_event({
        "event_id": event_id, "venue_id": venue_id, "starts_at": starts_at,
        "title": title, "post_type": post_type, "status": status,
        "lineup": lineup or [], "confidence": 0.9,
        "is_recurring": is_recurring, "recurrence_text": recurrence_text,
        "operator_edited_fields": operator_edited_fields,
        "source_kind": "venue_post", "source_handle": source_handle,
        "source_shortcode": f"safety_sc_{_SEQ['n']}",
        "source_event_key": compute_source_event_key(title, starts_at),
        "raw_extraction": {"title": title, "location_text": location_text},
        "first_seen_at": _NOW, "last_seen_at": _NOW,
    })
    return event_id


def _seed_corpus(store) -> list:
    """Every shape this plan touches, in one corpus."""
    ids = []
    # Defect 1: the Club Metrópole cluster (five acts, one Saturday).
    for title, lineup in (
        ("ROWKA", ["ROWKA"]),
        ("VITINHO POLÊMICO", ["VITINHO POLÊMICO"]),
        ("SÁBADO VAI FERVER", ["MC Baixinho"]),
        ("SECRET CLUB com @neguindabasersv", ["@neguindabasersv"]),
        ("estreia de @neguindabasersv", ["@neguindabasersv"]),
    ):
        ids.append(_seed(store, title, "v_club", lineup=lineup, source_handle="clubmetropole"))
    # Defect 3: the two live weekly rows, stored 21 days apart.
    for starts_at in (_WEDNESDAY_1, _WEDNESDAY_4):
        ids.append(_seed(
            store, "Aula de FORRÓ na Sala de Reboco", "v_reboco", starts_at=starts_at,
            is_recurring=True, recurrence_text="Toda QUARTA", source_handle="saladereboco",
        ))
    # Defect 2: a venue-post row whose own text names a sibling branch.
    ids.append(_seed(
        store, "SAMBINHA", "v_bd_bv", location_text="CASA FORTE",
        source_handle="beerdock_recife",
    ))
    # `260812`'s measured false positives, which must stay apart forever.
    for title in ("Bolinha do Cavaco", "JB do Cavaco"):
        ids.append(_seed(store, title, "v_casanova", source_handle="casanovaecobar"))
    for title in ("Oficina Vida de Inseto", "Oficina Cobra Gigante", "Oficina de Sorvete"):
        ids.append(_seed(store, title, "v_bode", source_handle="entreamigosobode"))
    # A confirmed row, a menu item, and an operator-edited title.
    ids.append(_seed(store, "NOITE CONFIRMADA", "v_club", status="confirmed"))
    ids.append(_seed(store, "Especial do dia", "v_club", post_type="menu", starts_at=None))
    ids.append(_seed(
        store, "TÍTULO DO OPERADOR", "v_club", operator_edited_fields=["title"],
    ))
    return ids


@pytest.fixture
def production_redis():
    """An admin-config store holding exactly what PRODUCTION holds today: the
    auto-merge flag, already true since 2026-08-14 — and NOTHING this plan
    adds. That is what makes this a deploy simulation rather than a wish."""
    redis = fakeredis.FakeRedis(decode_responses=True)
    redis.set(event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY, json.dumps(True))
    return redis


def _snapshot(store) -> dict:
    return {
        row["event_id"]: copy.deepcopy(row)
        for row in store.list_events()
    }


class TestTheDeployChangesNoStoredRow:
    def test_the_whole_pipeline_leaves_every_row_byte_identical(self, production_redis):
        import asyncio

        from app.services.event_display_title import EventDisplayTitleService

        store = _store()
        ids = _seed_corpus(store)
        before = _snapshot(store)

        merge_touched_events(store, list(ids), _NOW, redis_like=production_redis)
        asyncio.run(
            EventDisplayTitleService(store, None, redis_client=production_redis)
            .run_for_events(list(ids))
        )
        collect_dedup_backlog(store, redis_like=production_redis)

        assert _snapshot(store) == before

    def test_no_row_is_superseded_or_deleted(self, production_redis):
        store = _store()
        ids = _seed_corpus(store)
        merge_touched_events(store, list(ids), _NOW, redis_like=production_redis)
        for event_id in ids:
            row = store.get_event(event_id)
            assert row is not None, f"{event_id} was deleted"
            assert row["status"] != "superseded", row

    def test_the_measured_false_positives_are_still_refused(self, production_redis):
        store = _store()
        ids = _seed_corpus(store)
        merge_touched_events(store, list(ids), _NOW, redis_like=production_redis)
        titles = {store.get_event(e)["title"] for e in ids if store.get_event(e)}
        for title in (
            "Bolinha do Cavaco", "JB do Cavaco",
            "Oficina Vida de Inseto", "Oficina Cobra Gigante", "Oficina de Sorvete",
        ):
            assert title in titles, title

    def test_every_new_flag_ships_off(self):
        config = event_dedup.load_dedup_config(None)
        assert config.recurring_window_enabled is False
        assert config.single_night_venues == ()
        # The shipped lineup threshold is NOT changed by this branch; §E1's
        # decision is an admin-config edit made after a measurement.
        assert config.lineup_threshold == 2
        assert event_dedup.DEFAULT_LINEUP_THRESHOLD == 2

        from app.services.event_attribution_dispute import (
            DISPUTE_ACTION_FLAG, load_attribution_dispute_config,
        )
        from app.services.event_display_title import load_display_title_enabled

        dispute = load_attribution_dispute_config(None)
        assert dispute.action == DISPUTE_ACTION_FLAG
        assert dispute.withhold_enabled is False
        assert load_display_title_enabled(None) is False

    def test_the_dispute_rule_moves_no_venue_at_its_shipped_default(self, production_redis):
        """The attribution half, at the level the runtime actually decides
        it: `flag` keeps `venue_id` exactly where it was."""
        from app.services.event_attribution_dispute import (
            evaluate_attribution_dispute, load_attribution_dispute_config,
        )
        from app.services.event_venue_resolution import (
            build_handle_index, build_venue_catalog,
        )
        from app.dao.venue_repository import VenueRepository

        store = _store()
        dao = VenueRepository(client=None, rds_store=store)
        verdict = evaluate_attribution_dispute(
            mapped_venue_id="v_bd_bv", location_text="CASA FORTE",
            venues=build_venue_catalog(dao), handle_index=build_handle_index(dao),
            promoter_handle="beerdock_recife",
        )
        # The dispute IS detected — that is the feature — but the shipped
        # action only flags it.
        assert verdict is not None and verdict.target_venue_id == "v_bd_cf"
        assert load_attribution_dispute_config(production_redis).reattributes is False


# ── guard 2: identity is untouched ─────────────────────────────────────────
# The literal values `compute_source_event_key` and `compute_event_identity`
# produce for the production title list. `260812` §A: migration 0025 CALLS
# `compute_source_event_key` to back-fill every existing row before adding
# `UNIQUE (source_handle, source_shortcode, source_event_key)`, and 0026's
# historical collapse calls `compute_event_identity` directly — a moved hash
# orphans every operator confirmation and turns the next run into a duplicate
# generator.
_GOLDEN = [
    ("ROWKA", _SATURDAY, "b00db7fc02946d21ac7b3e26cd071ee8", "rowka"),
    ("VITINHO POLÊMICO", _SATURDAY, "cd50ec90918ff7183bdcc89359fbc2a6", "vitinho polemico"),
    ("SÁBADO VAI FERVER", _SATURDAY, "c2477dbfa3953084fb4ca3d9ce5dddd9", "sabado vai ferver"),
    ("SECRET CLUB com @neguindabasersv", _SATURDAY,
     "87ba1d01d38cd5ee1080a2bd74606062", "secret club com @neguindabasersv"),
    ("estreia de @neguindabasersv", _SATURDAY,
     "7f16b5667f79769e41af72444af39b5d", "estreia de @neguindabasersv"),
    ("Aula de FORRÓ na Sala de Reboco", _WEDNESDAY_1,
     "f0e999c45cd8699728ad092ab3bf362c", "aula de forro na sala de reboco"),
    ("Bolinha do Cavaco", _SATURDAY, "bc630400a6b3798d455e09d3a4b7a7eb", "bolinha do cavaco"),
    ("JB do Cavaco", _SATURDAY, "02b2c8d8a984db7a5c526a2a2c2baf80", "jb do cavaco"),
    ("Oficina Vida de Inseto", _SATURDAY,
     "429d2fb3c7de652172cc3ce2574cd2e2", "oficina vida de inseto"),
]


@pytest.mark.parametrize("title,starts_at,golden_key,golden_normalized", _GOLDEN)
def test_identity_is_byte_identical_before_and_after_this_branch(
    title, starts_at, golden_key, golden_normalized,
):
    assert compute_source_event_key(title, starts_at) == golden_key
    venue_id, calendar_date, normalized = compute_event_identity("v_club", starts_at, title)
    assert venue_id == "v_club"
    assert calendar_date == starts_at.date()
    assert normalized == golden_normalized


# ── guard 3: the delete mode still deletes ─────────────────────────────────
def test_finish_absorption_in_delete_mode_still_hard_deletes():
    """0026's historical replay depends on today's behaviour. §G added a
    `display_title` clear to `_finish_absorption`; this proves the DELETE
    half is otherwise exactly as it was."""
    from app.services.event_merge import MERGE_MODE_DELETE, _finish_absorption

    store = _store()
    canonical = _seed(store, "NOITE DA PATROA", "v_club")
    duplicate = _seed(store, "noite da patroa", "v_club")
    absorbed: set = set()

    _finish_absorption(store, duplicate, canonical, absorbed, mode=MERGE_MODE_DELETE)

    assert store.get_event(duplicate) is None, "delete mode must still hard-delete"
    assert duplicate in absorbed
    survivor = store.get_event(canonical)
    assert survivor is not None
    assert len(store.list_event_sources(canonical)) == 2
    assert survivor.get("display_title") is None


def test_finish_absorption_in_supersede_mode_keeps_the_row_readable():
    from app.services.event_merge import MERGE_MODE_SUPERSEDE, _finish_absorption

    store = _store()
    canonical = _seed(store, "Rodolpho Produções", "v_club")
    duplicate = _seed(store, "Rodolpho", "v_club")
    absorbed: set = set()

    _finish_absorption(store, duplicate, canonical, absorbed, mode=MERGE_MODE_SUPERSEDE)

    row = store.get_event(duplicate)
    assert row is not None
    assert row["status"] == "superseded"
    assert row["superseded_by"] == canonical


# ── guard 4: serving is untouched by the merge-time widening ───────────────
def test_expand_occurrences_is_unaffected_by_the_recurring_window_flag():
    """§D widens a MERGE-time candidate window. `expand_occurrences` has no
    knowledge of it and must not gain any: the serving expansion is a
    non-goal of this plan, and its own docstring's "re-derive the days from
    the weekday pattern" rule is load-bearing."""
    import inspect

    from app.services import event_occurrences

    source = inspect.getsource(event_occurrences)
    assert "recurring_window" not in source
    assert "single_night" not in source
    assert "display_title" not in source

    row = {
        "event_id": "evt_x", "starts_at": _WEDNESDAY_1,
        "is_recurring": True, "recurrence_text": "Toda QUARTA",
    }
    occurrences = event_occurrences.expand_occurrences(
        row, horizon_days=21, reference_time=_WEDNESDAY_1,
    )
    assert [o.occurrence_date for o in occurrences] == [
        "2026-08-05", "2026-08-12", "2026-08-19", "2026-08-26",
    ]
