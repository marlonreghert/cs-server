"""Guards for the blocked_venue migration (0029).

Same offline-guard shape as tests/test_event_ticket_info_and_attractions_migration.py
-- no local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection. Real-Postgres
round-trip fidelity (the table actually creates, the FK/PK/index hold) is
covered by tests/test_rds_store_contract.py's `store` fixture, which runs
against a real scratch Postgres too when RDS_TEST_URL is set.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0029_blocked_venues.py"
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "m0029_blocked_venues", _PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0029_blocked_venues"
    assert m.down_revision == "0028_event_ticket_info_and_attractions"


def test_creates_blocked_venue_table_mirroring_favorite_shape():
    """Same column set, PK, and soft-delete convention as engagement.favorite
    (migrations/versions/0001_baseline_schemas.py) -- the plan's explicit
    contract."""
    m = _load()
    upgrade = m.UPGRADE
    assert "CREATE TABLE IF NOT EXISTS engagement.blocked_venue" in upgrade
    assert "user_pseudo text NOT NULL" in upgrade
    assert "venue_id    text NOT NULL REFERENCES venues.venue(venue_id)" in upgrade
    assert "created_at  timestamptz NOT NULL DEFAULT now()" in upgrade
    assert "deleted_at  timestamptz," in upgrade
    assert "updated_at  timestamptz NOT NULL DEFAULT now()" in upgrade
    assert "PRIMARY KEY (user_pseudo, venue_id)" in upgrade


def test_creates_the_venue_index():
    m = _load()
    assert "CREATE INDEX IF NOT EXISTS ix_blocked_venue_venue ON engagement.blocked_venue (venue_id)" in m.UPGRADE


def test_upgrade_touches_only_the_new_table():
    m = _load()
    assert "ALTER TABLE" not in m.UPGRADE
    assert m.UPGRADE.count("CREATE TABLE") == 1
    assert m.UPGRADE.count("CREATE INDEX") == 1


def test_downgrade_drops_exactly_the_table_and_index_this_migration_added():
    m = _load()
    assert "DROP INDEX IF EXISTS engagement.ix_blocked_venue_venue" in m.DOWNGRADE
    assert "DROP TABLE IF EXISTS engagement.blocked_venue" in m.DOWNGRADE
    assert "DROP COLUMN" not in m.DOWNGRADE


def test_upgrade_and_downgrade_call_op_execute():
    m = _load()
    assert callable(m.upgrade)
    assert callable(m.downgrade)
