"""Guards for the events.event migration (0023).

No local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection — the same offline-guard
shape as tests/test_events_schema_migration.py (0022), which this migration
chains from.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0023_event_table.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0023", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0023_event_table"
    assert m.down_revision == "0022_events_schema"
    assert len(m.revision) <= 32


def test_creates_event_table_in_events_schema():
    m = _load()
    assert "CREATE TABLE IF NOT EXISTS events.event" in m.UPGRADE


def test_venue_id_is_nullable():
    """The whole point of this nullability: plan 4's promoter events arrive
    unlinked. No NOT NULL, no DEFAULT, on venue_id."""
    m = _load()
    assert "venue_id         text REFERENCES venues.venue(venue_id)," in m.UPGRADE
    assert "venue_id text not null" not in m.UPGRADE.lower()


def test_unique_constraint_on_source_handle_and_shortcode():
    """Idempotency (requirement 7) is a CONSTRAINT, not a code path."""
    m = _load()
    assert "UNIQUE (source_handle, source_shortcode)" in m.UPGRADE


def test_indexes_present():
    m = _load()
    assert "ix_event_venue_starts_at" in m.UPGRADE
    assert "ON events.event (venue_id, starts_at)" in m.UPGRADE
    assert "ix_event_status" in m.UPGRADE
    assert "ON events.event (status)" in m.UPGRADE


def test_status_default_is_pending_review():
    m = _load()
    assert "status           text NOT NULL DEFAULT 'pending_review'" in m.UPGRADE


def test_downgrade_does_not_touch_the_events_schema_or_venue_event_profile():
    """Additive migration onto a schema 0022 already owns: the downgrade must
    drop only what THIS migration created, never the schema itself or
    venue_event_profile (0022's table, which must survive a 0023 rollback)."""
    m = _load()
    assert "DROP TABLE IF EXISTS events.event" in m.DOWNGRADE
    assert "DROP SCHEMA" not in m.DOWNGRADE
    assert "venue_event_profile" not in m.DOWNGRADE


def test_downgrade_drops_its_own_indexes():
    m = _load()
    assert "DROP INDEX IF EXISTS events.ix_event_status" in m.DOWNGRADE
    assert "DROP INDEX IF EXISTS events.ix_event_venue_starts_at" in m.DOWNGRADE


def test_upgrade_and_downgrade_are_callable():
    """Guards against a typo turning upgrade/downgrade into dead code paths;
    does not execute SQL (no local Postgres), only calls the real function with
    a recording fake `op` to prove the bodies run without raising."""
    m = _load()

    class _FakeOp:
        def __init__(self):
            self.executed = []

        def execute(self, sql):
            self.executed.append(sql)

    fake_op = _FakeOp()
    m.op = fake_op
    m.upgrade()
    m.downgrade()
    assert len(fake_op.executed) == 2
