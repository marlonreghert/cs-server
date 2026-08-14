"""Guards for the crawl-target reels-seeded-state migration (0041). No local
Postgres in CI (see tests/README.md), so this pins the revision chain and
the load-bearing SQL fragments by inspection — the same offline-guard shape
tests/test_crawl_target_posts_dormant_migration.py (0038) already
established.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0041_crawl_target_reels_seeded.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0041", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0041_crawl_target_reels_seeded"
    assert m.down_revision == "0040_reviews_deep"
    # This repo's own test convention, not a database constraint.
    assert len(m.revision) <= 32


def test_adds_one_nullable_timestamptz_column():
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS reels_seeded_at timestamptz;" in m.UPGRADE
    # Deliberately NOT NOT NULL/DEFAULT — see the migration's own docstring
    # for why this column cannot be safely recomputed for every existing
    # row the way posts_dormant (0038) can.
    assert "NOT NULL" not in m.UPGRADE
    assert "DEFAULT" not in m.UPGRADE


def test_upgrade_touches_only_events_crawl_target():
    m = _load()
    assert "events.crawl_target" in m.UPGRADE
    for other_table in (
        "events.event", "events.promoter_account", "instagram.handle", "venues.venue",
    ):
        assert other_table not in m.UPGRADE
        assert other_table not in m.DOWNGRADE


def test_downgrade_drops_exactly_the_column_this_migration_added():
    m = _load()
    assert "DROP COLUMN IF EXISTS reels_seeded_at;" in m.DOWNGRADE


def test_upgrade_and_downgrade_call_op_execute():
    import unittest.mock as mock

    m = _load()
    with mock.patch.object(m, "op") as mocked_op:
        m.upgrade()
        mocked_op.execute.assert_called_once_with(m.UPGRADE)
    with mock.patch.object(m, "op") as mocked_op:
        m.downgrade()
        mocked_op.execute.assert_called_once_with(m.DOWNGRADE)


def test_does_not_touch_cursor_reels_at():
    """The migration's own docstring records why `cursor_reels_at` keeps
    its existing, unchanged meaning — this migration must never rename or
    touch it."""
    m = _load()
    assert "cursor_reels_at" not in m.UPGRADE
    assert "cursor_reels_at" not in m.DOWNGRADE
