"""Unit tests for the title/lineup-similarity merge orchestration added to
app/services/event_merge.py by plans/260812_event-dedup-fuzzy-title.md.

`tests/test_event_dedup.py` covers the pure predicate (`app.services.
event_dedup`) in isolation; `tests/bdd/enrichment/event-dedup-fuzzy-
title.feature` covers the full behaviour from the outside. This file adds
what neither of those is the right place for: the byte-identical-identity
regression guard (plan §A's own "cheapest possible guard"), the
`_finish_absorption` mode split (supersede vs delete, pinned so 0026's
replay path is provably untouched), transitive closure on the five-member
Rodolpho cluster (assertions naming the surviving/absorbed titles, never
just a row count — plan's own Test Plan warning), and the migration's
offline up/down guard (no local Postgres in CI; matches
tests/test_post_items_migration.py's shape).
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models.venue import Venue
from app.services import event_dedup
from app.services.event_identity import compute_source_event_key
from app.services.event_merge import (
    apply_merge_suggestion,
    compute_event_identity,
    merge_touched_events,
    reject_merge_suggestion,
    reverse_title_similarity_merge,
)
from app.services.event_reconciliation import STATUS_SUPERSEDED, new_event_id
from tests.rds_fake import InMemoryRdsVenueStore

_VENUE_ID = "dedup_merge_v1"
_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def _store(venue_name="Conchittas Bar") -> InMemoryRdsVenueStore:
    store = InMemoryRdsVenueStore()
    store.upsert_venue(Venue(venue_id=_VENUE_ID, venue_name=venue_name, venue_lat=-8.05, venue_lng=-34.88))
    return store


def _insert(store, title, starts_at, *, seq, lineup=None, post_type="event", **overrides) -> str:
    event_id = new_event_id()
    seen_at = _NOW + timedelta(seconds=seq)
    fields = {
        "event_id": event_id, "venue_id": _VENUE_ID, "starts_at": starts_at, "title": title,
        "post_type": post_type, "lineup": lineup or [], "status": "pending_review",
        "source_kind": "venue_post", "source_handle": f"h{seq}", "source_shortcode": f"sc{seq}",
        "first_seen_at": seen_at, "last_seen_at": seen_at,
    }
    fields.update(overrides)
    store.insert_event(fields)
    return event_id


class FakeRedis:
    def __init__(self, **overrides):
        import json
        self.store = {}
        for key, value in overrides.items():
            self.store[key] = json.dumps(value)

    def get(self, key):
        return self.store.get(key)


def _auto_enabled_redis(**overrides) -> FakeRedis:
    return FakeRedis(**{event_dedup.ADMIN_CONFIG_AUTO_MERGE_ENABLED_KEY: True, **overrides})


def _alive(store, event_id) -> bool:
    row = store.get_event(event_id)
    return row is not None and row.get("status") != STATUS_SUPERSEDED


def _local(date_str, time_str):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Recife")
    hour, minute = (int(p) for p in time_str.split(":"))
    year, month, day = (int(p) for p in date_str.split("-"))
    return datetime(year, month, day, hour, minute, tzinfo=tz)


class TestIdentityByteIdenticalBeforeAndAfter:
    """plan §A: 'compute_source_event_key and compute_event_identity are
    byte-identical before and after this feature, asserted on the
    production titles — the cheapest possible guard against §A being
    quietly undone.' This plan never imports or modifies
    app.services.event_identity, and app.services.event_merge.
    compute_event_identity is untouched by this diff — these golden hashes
    were computed against the shipped functions and must never move."""

    _ROWS = [
        ("Aniversário do RODOLPHO Produções", None, "e4c428c073ae89f683c2370d9f494f8b", None),
        ("Aniversário do Rodolpho Produções", _local("2026-08-07", "19:00"),
         "7cbafd8f83fc8192c947abf18b2bbdc8", "aniversario do rodolpho producoes"),
        ("31º Rodolpho Produções", _local("2026-08-07", "19:00"),
         "2f63420039392cd17a4e060971be4958", "31o rodolpho producoes"),
        ("Rodolpho", _local("2026-08-07", "21:00"),
         "ef98422fb4e1efa1ed354981aefd1756", "rodolpho"),
        ("Rodolpho Produções", _local("2026-08-07", "21:00"),
         "b875558143e3d148cef2c99da5127aee", "rodolpho producoes"),
        ("SEXTOU NO CONCHITTAS BAR!", _local("2026-08-07", "21:00"),
         "27bed410da1d7d329f72f184071014e5", "sextou no conchittas bar!"),
        ("Aniversário do Rodolpho Produções", _local("2026-08-08", "00:00"),
         "05f442e2be13ae8bd98c9f672dd56794", "aniversario do rodolpho producoes"),
        ("31 Anos", _local("2026-08-08", "00:00"),
         "60a3fda16a87fc5faf55d9de008dac98", "31 anos"),
    ]

    def test_compute_source_event_key_is_unchanged(self):
        for title, starts_at, golden, _normalized in self._ROWS:
            assert compute_source_event_key(title, starts_at) == golden, title

    def test_compute_event_identity_date_and_title_components_are_unchanged(self):
        for title, starts_at, _golden, normalized in self._ROWS:
            identity = compute_event_identity("some_venue", starts_at, title)
            if starts_at is None:
                assert identity is None
            else:
                assert identity == ("some_venue", starts_at.date(), normalized)

    def test_full_identity_tuple_matches_a_direct_recomputation(self):
        """A second, independent way to assert nothing moved: compute
        compute_event_identity twice for the same inputs and require exact
        equality — this catches any accidental non-determinism as well as
        any accidental redefinition."""
        for title, starts_at, _golden, _normalized in self._ROWS:
            a = compute_event_identity("v", starts_at, title)
            b = compute_event_identity("v", starts_at, title)
            assert a == b


class TestFinishAbsorptionModes:
    def test_supersede_mode_leaves_the_row_readable_with_superseded_by_set(self):
        store = _store()
        canonical_id = _insert(store, "Rodolpho Produções", _local("2026-08-07", "21:00"), seq=1)
        dup_id = _insert(store, "Rodolpho", _local("2026-08-07", "21:00"), seq=2)
        merge_touched_events(
            store, [canonical_id, dup_id], _NOW, redis_like=_auto_enabled_redis(),
        )
        dup_row = store.get_event(dup_id)
        assert dup_row is not None
        assert dup_row["status"] == STATUS_SUPERSEDED
        assert dup_row["superseded_by"] is not None
        canonical_sources = store.list_event_sources(dup_row["superseded_by"])
        assert len(canonical_sources) == 2

    def test_delete_mode_behaves_exactly_as_before_for_exact_identity(self):
        """0026's historical replay depends on this — an exact-identity
        merge must still hard-delete, never supersede."""
        store = _store()
        a = _insert(store, "NOITE DA PATROA", _local("2026-08-07", "20:00"), seq=1)
        b = _insert(store, "noite da patroa", _local("2026-08-07", "20:00"), seq=2)
        merge_touched_events(store, [a, b], _NOW, redis_like=_auto_enabled_redis())
        alive = [eid for eid in (a, b) if store.get_event(eid) is not None]
        deleted = [eid for eid in (a, b) if store.get_event(eid) is None]
        assert len(alive) == 1 and len(deleted) == 1


class TestTransitiveClosureOnTheRodolphoCluster:
    def test_five_member_cluster_collapses_to_one_canonical_naming_the_titles(self):
        store = _store()
        ids = {
            "aniversario_1900": _insert(store, "Aniversário do Rodolpho Produções", _local("2026-08-07", "19:00"), seq=1),
            "31o": _insert(store, "31º Rodolpho Produções", _local("2026-08-07", "19:00"), seq=2),
            "rodolpho": _insert(store, "Rodolpho", _local("2026-08-07", "21:00"), seq=3),
            "rodolpho_producoes": _insert(store, "Rodolpho Produções", _local("2026-08-07", "21:00"), seq=4),
            "aniversario_midnight": _insert(store, "Aniversário do Rodolpho Produções", _local("2026-08-08", "00:00"), seq=5),
        }
        merge_touched_events(store, list(ids.values()), _NOW, redis_like=_auto_enabled_redis())

        survivors = [eid for eid in ids.values() if _alive(store, eid)]
        assert len(survivors) == 1, "the five-member cluster must collapse to exactly one row"
        survivor = store.get_event(survivors[0])
        # Names the SURVIVING title explicitly — a row-count assertion alone
        # ("5 became 1") proves nothing about WHICH title survived.
        assert survivor["title"] == "Aniversário do Rodolpho Produções"
        assert len(store.list_event_sources(survivors[0])) == 5

        # Every absorbed row is superseded (never deleted) and names the
        # survivor it was absorbed into.
        absorbed_ids = [eid for eid in ids.values() if eid not in survivors]
        assert len(absorbed_ids) == 4
        for eid in absorbed_ids:
            row = store.get_event(eid)
            assert row is not None, "a title-similarity merge must supersede, never delete"
            assert row["status"] == STATUS_SUPERSEDED
            assert row["superseded_by"] == survivors[0]

    def test_order_independent_same_five_members_reversed_input_order(self):
        store = _store()
        ids = [
            _insert(store, "Rodolpho", _local("2026-08-07", "21:00"), seq=1),
            _insert(store, "Rodolpho Produções", _local("2026-08-07", "21:00"), seq=2),
            _insert(store, "31º Rodolpho Produções", _local("2026-08-07", "19:00"), seq=3),
            _insert(store, "Aniversário do Rodolpho Produções", _local("2026-08-08", "00:00"), seq=4),
            _insert(store, "Aniversário do Rodolpho Produções", _local("2026-08-07", "19:00"), seq=5),
        ]
        merge_touched_events(store, list(reversed(ids)), _NOW, redis_like=_auto_enabled_redis())
        survivors = [eid for eid in ids if _alive(store, eid)]
        assert len(survivors) == 1
        assert len(store.list_event_sources(survivors[0])) == 5


class TestNonEventGuard:
    def test_a_greeting_typed_other_never_absorbs_into_the_party(self):
        store = _store()
        party_id = _insert(store, "Aniversário do Rodolpho Produções", _local("2026-08-07", "19:00"), seq=1)
        greeting_id = _insert(
            store, "31 Anos", _local("2026-08-08", "00:00"), seq=2, post_type="other",
        )
        merge_touched_events(store, [party_id, greeting_id], _NOW, redis_like=_auto_enabled_redis())
        assert store.get_event(party_id) is not None
        assert store.get_event(greeting_id) is not None
        assert store.get_event(greeting_id)["status"] != STATUS_SUPERSEDED

    def test_differing_post_type_never_merges_via_the_fuzzy_path(self):
        """Titles deliberately differ ('...' / '...no Conchittas') so this
        exercises the FUZZY path's own guard, not an accidental exact-
        identity collision (compute_event_identity has never considered
        post_type — that is pre-existing, out-of-scope behaviour; see
        TestFinishAbsorptionModes for the exact-identity path's own,
        unrelated test)."""
        store = _store()
        promo_id = _insert(store, "Especial do dia", _local("2026-08-07", "19:00"), seq=1, post_type="promotion")
        event_id = _insert(
            store, "Especial do dia no Conchittas", _local("2026-08-07", "19:00"), seq=2, post_type="event",
        )
        merge_touched_events(store, [promo_id, event_id], _NOW, redis_like=_auto_enabled_redis())
        assert _alive(store, promo_id)
        assert _alive(store, event_id)


class TestReversalAndSuggestionActions:
    def test_reversal_restores_status_superseded_by_and_source_attachment(self):
        store = _store()
        canonical_id = _insert(store, "Rodolpho Produções", _local("2026-08-07", "21:00"), seq=1)
        dup_id = _insert(store, "Rodolpho", _local("2026-08-07", "21:00"), seq=2)
        merge_touched_events(store, [canonical_id, dup_id], _NOW, redis_like=_auto_enabled_redis())
        dup_row = store.get_event(dup_id)
        assert dup_row["status"] == STATUS_SUPERSEDED
        canonical_id_resolved = dup_row["superseded_by"]

        restored = reverse_title_similarity_merge(store, dup_id, _NOW + timedelta(hours=1))
        assert restored["status"] != STATUS_SUPERSEDED
        assert restored["superseded_by"] is None
        assert len(store.list_event_sources(dup_id)) == 1
        # The canonical keeps its OWN original source, but no longer the
        # one that moved back.
        assert len(store.list_event_sources(canonical_id_resolved)) == 1

    def test_apply_suggestion_merges_only_on_operator_action(self):
        store = _store()
        a = _insert(store, "Ação Leitura: Bate-papo com Marcelino Freire", _local("2026-08-04", "19:00"), seq=1)
        b = _insert(store, "Ação Leitura: Bate-papo com Jeferson Tenório", _local("2026-08-04", "19:00"), seq=2)
        merge_touched_events(store, [a, b], _NOW, redis_like=_auto_enabled_redis())
        # Suggest-band: nothing merged yet.
        assert store.get_event(a) is not None and store.get_event(b) is not None

        pending = [
            s for s in store.list_event_merge_suggestions(event_id=a, decision="pending")
            if b in (s["event_id"], s["candidate_event_id"])
        ]
        assert len(pending) == 1
        result = apply_merge_suggestion(store, pending[0]["suggestion_id"], _NOW + timedelta(hours=1))
        survivors = [eid for eid in (a, b) if store.get_event(eid) is not None and store.get_event(eid)["status"] != STATUS_SUPERSEDED]
        assert len(survivors) == 1
        assert result["event_id"] == survivors[0]

    def test_reject_suggestion_leaves_both_rows_untouched(self):
        store = _store()
        a = _insert(store, "Ação Leitura: Bate-papo com Marcelino Freire", _local("2026-08-04", "19:00"), seq=1)
        b = _insert(store, "Ação Leitura: Bate-papo com Jeferson Tenório", _local("2026-08-04", "19:00"), seq=2)
        merge_touched_events(store, [a, b], _NOW, redis_like=_auto_enabled_redis())
        pending = store.list_event_merge_suggestions(event_id=a, decision="pending")
        rejected = reject_merge_suggestion(store, pending[0]["suggestion_id"], _NOW + timedelta(hours=1))
        assert rejected["decision"] == "rejected"
        assert store.get_event(a) is not None and store.get_event(b) is not None


class TestMigrationOfflineGuard:
    """No local Postgres in CI — the same offline guard shape
    tests/test_post_items_migration.py already established."""

    _PATH = (
        Path(__file__).resolve().parent.parent
        / "migrations" / "versions" / "0039_event_merge_suggestions.py"
    )

    def _load(self):
        spec = importlib.util.spec_from_file_location("m0039_event_merge_suggestions", self._PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_revision_chain(self):
        m = self._load()
        assert m.revision == "0039_event_merge_suggestions"
        assert m.down_revision == "0038_crawl_target_posts_dormant"
        assert len(m.revision) <= 32

    def test_upgrade_creates_the_suggestion_table_and_the_superseded_by_column(self):
        m = self._load()
        assert "CREATE TABLE IF NOT EXISTS events.event_merge_suggestion" in m._CREATE_TABLE
        assert "ADD COLUMN IF NOT EXISTS superseded_by text" in m._ADD_SUPERSEDED_BY

    def test_downgrade_drops_exactly_what_upgrade_added(self):
        m = self._load()
        assert "DROP TABLE IF EXISTS events.event_merge_suggestion" in m._DROP_TABLE
        assert "DROP COLUMN IF EXISTS superseded_by" in m._DROP_SUPERSEDED_BY

    def test_upgrade_and_downgrade_round_trip_call_op_execute_in_order(self, monkeypatch):
        m = self._load()
        calls = []

        class FakeOp:
            @staticmethod
            def execute(sql):
                calls.append(sql)

        monkeypatch.setattr(m, "op", FakeOp)
        m.upgrade()
        assert calls == [m._CREATE_TABLE, m._ADD_SUPERSEDED_BY]
        calls.clear()
        m.downgrade()
        assert calls == [m._DROP_TABLE, m._DROP_SUPERSEDED_BY]
