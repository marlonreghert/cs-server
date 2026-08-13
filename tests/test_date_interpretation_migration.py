"""Guards for the date-interpretation migration (0037). See
plans/260812_event-attribution-and-dates.md §C.

No local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection — the same
offline-guard shape as tests/test_source_media_type_migration.py (0036) and
tests/test_time_known_migration.py (0035).
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0037_date_interpretation.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0037_date_interpretation", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0037_date_interpretation"
    assert m.down_revision == "0036_source_media_type"
    assert len(m.revision) <= 32


def test_upgrade_adds_the_nullable_jsonb_column():
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS date_interpretation jsonb" in m.UPGRADE
    # No back-fill -- every pre-existing row has nothing to reuse.
    assert "UPDATE events.post_item_source" not in m.UPGRADE


def test_upgrade_touches_only_post_item_source():
    m = _load()
    assert "ALTER TABLE events.post_item_source" in m.UPGRADE
    assert "ALTER TABLE events.post_item " not in m.UPGRADE
    assert "ALTER TABLE events.crawl_target" not in m.UPGRADE
    assert "CREATE TABLE" not in m.UPGRADE
    assert "DROP TABLE" not in m.UPGRADE


def test_downgrade_drops_the_column():
    m = _load()
    assert "DROP COLUMN IF EXISTS date_interpretation" in m.DOWNGRADE
    assert "ALTER TABLE events.post_item_source" in m.DOWNGRADE


def test_upgrade_and_downgrade_are_callable():
    """Guards against a typo turning upgrade/downgrade into dead code paths;
    does not execute SQL (no local Postgres in this offline suite), only
    calls the real function with a recording fake `op` to prove the bodies
    run without raising."""
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


def test_upgrade_downgrade_upgrade_round_trip_leaves_column_present():
    """The up/down round trip the plan requires, in the shape
    tests/test_source_media_type_migration.py already established: run
    upgrade, confirm the ADD COLUMN statement executed, run downgrade,
    confirm the DROP COLUMN statement executed, run upgrade again, confirm
    the ADD COLUMN statement is present a second time (idempotent —
    IF NOT EXISTS/IF EXISTS on every clause means a real Postgres tolerates
    re-running either direction)."""
    m = _load()

    class _FakeOp:
        def __init__(self):
            self.executed = []

        def execute(self, sql):
            self.executed.append(sql)

    fake_op = _FakeOp()
    m.op = fake_op

    m.upgrade()
    assert "date_interpretation" in fake_op.executed[-1]
    m.downgrade()
    assert "DROP COLUMN IF EXISTS date_interpretation" in fake_op.executed[-1]
    m.upgrade()
    assert "date_interpretation" in fake_op.executed[-1]
    assert len(fake_op.executed) == 3
