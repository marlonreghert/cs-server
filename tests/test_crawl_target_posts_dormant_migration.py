"""Guards for the crawl-target dormancy-column migration (0038). No local
Postgres in CI (see tests/README.md), so this pins the revision chain and the
load-bearing SQL fragments by inspection — the same offline-guard shape
tests/test_crawl_target_reels_overlap_migration.py (0033) already
established.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0038_crawl_target_posts_dormant.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0038", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0038_crawl_target_posts_dormant"
    assert m.down_revision == "0037_date_interpretation"
    # This repo's own test convention, not a database constraint.
    assert len(m.revision) <= 32


def test_adds_one_not_null_boolean_column_defaulting_false():
    m = _load()
    assert (
        "ADD COLUMN IF NOT EXISTS posts_dormant boolean NOT NULL DEFAULT false;"
        in m.UPGRADE
    )


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
    assert "DROP COLUMN IF EXISTS posts_dormant;" in m.DOWNGRADE


def test_upgrade_and_downgrade_call_op_execute():
    import unittest.mock as mock

    m = _load()
    with mock.patch.object(m, "op") as mocked_op:
        m.upgrade()
        mocked_op.execute.assert_called_once_with(m.UPGRADE)
    with mock.patch.object(m, "op") as mocked_op:
        m.downgrade()
        mocked_op.execute.assert_called_once_with(m.DOWNGRADE)


def test_does_not_touch_last_failure_kind_or_posts_never_seeded_columns():
    """The migration's own docstring records why neither existing column
    (`last_failure_kind`, and the derived-never-stored `posts_never_seeded`)
    could carry this signal — this migration must never rename or touch
    either."""
    m = _load()
    assert "last_failure_kind" not in m.UPGRADE
    assert "last_failure_kind" not in m.DOWNGRADE
    assert "posts_never_seeded" not in m.UPGRADE
    assert "posts_never_seeded" not in m.DOWNGRADE
