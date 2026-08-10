"""Guards for the crawl-target reels-overlap-counts migration (0033). No
local Postgres in CI (see tests/README.md), so this pins the revision chain
and the load-bearing SQL fragments by inspection — the same offline-guard
shape tests/test_crawl_target_reels_caps_migration.py (0032) already
established.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0033_crawl_target_reels_overlap.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0033", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0033_crawl_target_reels_overlap"
    assert m.down_revision == "0032_crawl_target_reels_caps"
    # This repo's own test convention, not a database constraint.
    assert len(m.revision) <= 32


def test_adds_two_nullable_no_default_columns():
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS last_run_reels_fetched integer," in m.UPGRADE
    assert "ADD COLUMN IF NOT EXISTS last_run_reels_new integer;" in m.UPGRADE
    assert "DEFAULT" not in m.UPGRADE


def test_upgrade_touches_only_events_crawl_target():
    m = _load()
    assert "events.crawl_target" in m.UPGRADE
    for other_table in (
        "events.event", "events.promoter_account", "instagram.handle", "venues.venue",
    ):
        assert other_table not in m.UPGRADE
        assert other_table not in m.DOWNGRADE


def test_downgrade_drops_exactly_the_two_columns_this_migration_added():
    m = _load()
    assert "DROP COLUMN IF EXISTS last_run_reels_fetched," in m.DOWNGRADE
    assert "DROP COLUMN IF EXISTS last_run_reels_new;" in m.DOWNGRADE


def test_upgrade_and_downgrade_call_op_execute():
    import unittest.mock as mock

    m = _load()
    with mock.patch.object(m, "op") as mocked_op:
        m.upgrade()
        mocked_op.execute.assert_called_once_with(m.UPGRADE)
    with mock.patch.object(m, "op") as mocked_op:
        m.downgrade()
        mocked_op.execute.assert_called_once_with(m.DOWNGRADE)


def test_does_not_touch_crawl_reels_column():
    """Correction to the plan, recorded here too: the plan describes this
    revision as flipping `crawl_reels`'s column default. That default is
    already `false` since 0030_crawl_target and no later migration changed
    it — verified against a freshly-migrated scratch Postgres while
    executing this plan. This migration adds only the two overlap-count
    columns; it must never mention `crawl_reels` at all."""
    m = _load()
    assert "crawl_reels" not in m.UPGRADE
    assert "crawl_reels" not in m.DOWNGRADE
