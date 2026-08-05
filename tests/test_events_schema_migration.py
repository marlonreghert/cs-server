"""Guards for the events-schema migration (0022).

No local Postgres in CI (see tests/README.md), so this pins the revision chain
and the load-bearing SQL fragments by inspection, the same offline-guard shape
as tests/test_widen_alembic_version.py.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0022_events_schema.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0022", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0022_events_schema"
    assert m.down_revision == "0021_venue_source"
    assert len(m.revision) <= 32


def test_creates_schema_and_table():
    m = _load()
    assert "CREATE SCHEMA IF NOT EXISTS events" in m.UPGRADE
    assert "CREATE TABLE IF NOT EXISTS events.venue_event_profile" in m.UPGRADE


def test_evaluated_at_is_nullable_with_no_default():
    """The whole point of this migration: NULL must mean 'never evaluated',
    not a manufactured timestamp. No DEFAULT, no NOT NULL, on evaluated_at."""
    m = _load()
    assert "evaluated_at    timestamptz," in m.UPGRADE
    assert "evaluated_at timestamptz not null" not in m.UPGRADE.lower()
    assert "evaluated_at timestamptz default" not in m.UPGRADE.lower()


def test_tier_is_indexed():
    m = _load()
    assert "ix_venue_event_profile_tier" in m.UPGRADE
    assert "ON events.venue_event_profile (tier)" in m.UPGRADE


def test_downgrade_is_real_and_additive_safe():
    """Additive migration (new schema + table only) -> the downgrade is a
    plain, safe drop, unlike 0021_venue_source's documented-destructive one."""
    m = _load()
    assert "DROP TABLE IF EXISTS events.venue_event_profile" in m.DOWNGRADE
    assert "DROP SCHEMA IF EXISTS events" in m.DOWNGRADE
    assert "DROP INDEX IF EXISTS events.ix_venue_event_profile_tier" in m.DOWNGRADE


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
