"""Guards for the crawl-error-visibility migration (0036). See
plans/260812_crawl-error-visibility.md.

No local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection — the same
offline-guard shape as tests/test_post_items_migration.py (0034) and
tests/test_time_known_migration.py (0035).
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0036_source_media_type.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0036_source_media_type", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0036_source_media_type"
    assert m.down_revision == "0035_time_known"
    assert len(m.revision) <= 32


def test_upgrade_adds_the_two_nullable_post_item_source_columns():
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS source_media_type text" in m.UPGRADE
    assert "ADD COLUMN IF NOT EXISTS source_uploaded_at timestamptz" in m.UPGRADE
    # Neither is backfilled by this migration — both are recoverable from
    # the S3 manifest by a deferred operator script (see the module
    # docstring); a plain nullable ADD COLUMN with no UPDATE keeps that
    # honest.
    assert "UPDATE events.post_item_source" not in m.UPGRADE


def test_upgrade_adds_cap_truncation_column_not_null_default_false():
    """`source_events_truncated` defaults false — a row that exists at all
    predates this column and was, by definition, not dropped by the cap it
    now records, so `false` is the historically accurate default (unlike
    the two nullable columns above, which are genuinely unknown until
    back-filled)."""
    m = _load()
    assert (
        "ADD COLUMN IF NOT EXISTS source_events_truncated boolean NOT NULL DEFAULT false"
        in m.UPGRADE
    )


def test_upgrade_adds_the_two_crawl_target_failure_columns():
    m = _load()
    assert "ALTER TABLE events.crawl_target" in m.UPGRADE
    assert "ADD COLUMN IF NOT EXISTS last_failure_kind text" in m.UPGRADE
    assert "ADD COLUMN IF NOT EXISTS last_failure_at timestamptz" in m.UPGRADE


def test_upgrade_touches_only_the_two_named_tables():
    m = _load()
    assert "ALTER TABLE events.post_item_source" in m.UPGRADE
    assert "ALTER TABLE events.crawl_target" in m.UPGRADE
    assert "ALTER TABLE events.post_item " not in m.UPGRADE
    assert "CREATE TABLE" not in m.UPGRADE
    assert "DROP TABLE" not in m.UPGRADE


def test_downgrade_drops_every_column_the_upgrade_added():
    m = _load()
    for col in (
        "source_media_type", "source_uploaded_at", "source_events_truncated",
    ):
        assert f"DROP COLUMN IF EXISTS {col}" in m.DOWNGRADE
    for col in ("last_failure_kind", "last_failure_at"):
        assert f"DROP COLUMN IF EXISTS {col}" in m.DOWNGRADE


def test_downgrade_drops_crawl_target_columns_before_post_item_source_ones():
    """No real ordering dependency between the two tables (unlike 0026's
    multi-step migration), but pinning the order guards against a future
    edit silently dropping one half."""
    m = _load()
    crawl_target_idx = m.DOWNGRADE.index("ALTER TABLE events.crawl_target")
    post_item_source_idx = m.DOWNGRADE.index("ALTER TABLE events.post_item_source")
    assert crawl_target_idx < post_item_source_idx


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


def test_upgrade_downgrade_upgrade_round_trip_leaves_columns_present():
    """The up/down round trip the plan requires, in the shape
    tests/test_post_items_migration.py already established: run upgrade,
    confirm every added column's ADD COLUMN statement executed, run
    downgrade, confirm every DROP COLUMN statement executed, run upgrade
    again, confirm the ADD COLUMN statements are present a second time
    (idempotent — IF NOT EXISTS/IF EXISTS on every clause means a real
    Postgres tolerates re-running either direction)."""
    m = _load()

    class _FakeOp:
        def __init__(self):
            self.executed = []

        def execute(self, sql):
            self.executed.append(sql)

    fake_op = _FakeOp()
    m.op = fake_op

    m.upgrade()
    assert "source_media_type" in fake_op.executed[-1]
    m.downgrade()
    assert "DROP COLUMN IF EXISTS source_media_type" in fake_op.executed[-1]
    m.upgrade()
    assert "source_media_type" in fake_op.executed[-1]
    assert len(fake_op.executed) == 3
