"""Guards for migration 0047_event_venue_link_candidate_llm_recommendation —
plans/260913_dedup-agentic-mitigation-discovery.md Phase 4.

`# bdd-exempt: migration 0047's own up/down round trip has no externally
observable behaviour` — pinned instead by this offline SQL-inspection test,
the same shape `tests/test_event_display_title_migration.py` (0046) already
uses for an additive, nullable column, since there is no local Postgres in
CI (see tests/README.md).

The one thing a migration file alone cannot guarantee — that the new column
is actually READ by `RdsVenueStore.list_event_venue_link_candidates` and
WRITABLE through a dedicated method, without which the column exists in the
schema but every DAO caller is blind to it — is asserted here too, mirroring
0046's own `test_the_column_is_in_the_dao_write_allowlist`'s rationale: that
exact failure shape passes every BDD scenario (the in-memory fake accepts
anything) and fails only in prod.
"""
import importlib.util
import inspect
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions"
    / "0047_event_venue_link_candidate_llm_recommendation.py"
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "m0047_event_venue_link_candidate_llm_recommendation", _PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0047_event_venue_link_candidate_llm_recommendation"
    assert m.down_revision == "0046_event_display_title"


def test_upgrade_adds_one_nullable_jsonb_column():
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS llm_recommendation jsonb" in m.UPGRADE
    lowered = m.UPGRADE.lower()
    assert "not null" not in lowered
    assert "default" not in lowered
    assert "update" not in lowered  # no back-fill


def test_upgrade_touches_only_event_venue_link_candidate():
    m = _load()
    assert "ALTER TABLE events.event_venue_link_candidate" in m.UPGRADE
    assert "CREATE TABLE" not in m.UPGRADE
    assert "CREATE INDEX" not in m.UPGRADE
    assert "DROP" not in m.UPGRADE


def test_the_migration_never_touches_an_existing_column():
    """`venue_id`/`rank`/`score`/`method`/`evidence` are real decision
    inputs the resolution ladder and `event_attribution_dispute` already
    read — this migration must add exactly one new column and rewrite
    none of them."""
    m = _load()
    for existing in ("venue_id", "rank", "score", " method ", "evidence"):
        assert existing not in m.UPGRADE.replace("llm_recommendation jsonb", "")
    assert "RENAME" not in m.UPGRADE


def test_downgrade_drops_exactly_the_one_column_this_migration_added():
    m = _load()
    assert "DROP COLUMN IF EXISTS llm_recommendation" in m.DOWNGRADE
    assert "DROP TABLE" not in m.DOWNGRADE
    assert "DROP INDEX" not in m.DOWNGRADE
    assert "event_id" not in m.DOWNGRADE


def test_the_upgrade_is_idempotent_in_shape():
    m = _load()
    assert "IF NOT EXISTS" in m.UPGRADE


def test_the_real_store_reads_the_column():
    from app.dao.rds_venue_store import RdsVenueStore

    source = inspect.getsource(RdsVenueStore.list_event_venue_link_candidates)
    assert "llm_recommendation" in source


def test_the_real_store_exposes_a_dedicated_write_method():
    """Never piggy-backed onto `replace_event_venue_link_candidates` (which
    must keep clearing this column to NULL on every fresh ladder run, never
    setting it) — a SEPARATE, narrowly-scoped method is the only way to
    write it."""
    from app.dao.rds_venue_store import RdsVenueStore

    assert hasattr(RdsVenueStore, "set_event_venue_link_candidate_recommendation")
    replace_source = inspect.getsource(RdsVenueStore.replace_event_venue_link_candidates)
    assert "llm_recommendation" not in replace_source


def test_the_venue_repository_exposes_the_same_write_method():
    from app.dao.venue_repository import VenueRepository

    assert hasattr(VenueRepository, "set_event_venue_link_candidate_recommendation")


def test_the_fake_store_clears_the_column_on_every_replace():
    """The in-memory fake must mirror the real store's `replace_event_venue
    _link_candidates` contract exactly (a stale recommendation from a PRIOR
    ladder run must never survive a re-resolution), or a BDD scenario could
    pass against the fake while the real store behaves differently."""
    from tests.rds_fake import InMemoryRdsVenueStore

    store = InMemoryRdsVenueStore()
    store.events["e1"] = {"event_id": "e1", "post_type": "event"}
    store.replace_event_venue_link_candidates("e1", [
        {"venue_id": "v1", "rank": 1, "score": 0.9, "method": "name_match", "evidence": {}},
    ])
    store.set_event_venue_link_candidate_recommendation(
        "e1", "v1", {"venue_id": "v1", "evidence_quote": "x"},
    )
    rows = store.list_event_venue_link_candidates("e1")
    assert rows[0]["llm_recommendation"] == {"venue_id": "v1", "evidence_quote": "x"}

    # A fresh ladder run (re-resolution) must wipe it.
    store.replace_event_venue_link_candidates("e1", [
        {"venue_id": "v1", "rank": 1, "score": 0.9, "method": "name_match", "evidence": {}},
    ])
    rows = store.list_event_venue_link_candidates("e1")
    assert rows[0]["llm_recommendation"] is None
