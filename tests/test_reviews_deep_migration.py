"""Guards for the deep-review-corpus migration (0040). No local Postgres in
CI (see tests/README.md), so this pins the revision chain and the load-
bearing SQL fragments by inspection — the same offline-guard shape
tests/test_crawl_target_posts_dormant_migration.py (0038) established.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0040_reviews_deep.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0040", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0040_reviews_deep"
    # Verified against the ACTUAL migrations/versions/ head at execution time
    # (0039_event_merge_suggestions), not trusted from the plan text — see
    # 0030_crawl_target.py's own docstring for why that verification matters.
    assert m.down_revision == "0039_event_merge_suggestions"
    assert len(m.revision) <= 32


def test_creates_exactly_one_new_table():
    m = _load()
    assert "CREATE TABLE IF NOT EXISTS venues.reviews_deep" in m.UPGRADE
    for other_table in (
        "google_places.reviews", "venues.menu_data", "venues.vibe_profile",
        "events.event_merge_suggestion",
    ):
        assert other_table not in m.UPGRADE
        assert other_table not in m.DOWNGRADE


def test_table_shape_matches_the_other_enrichment_tables():
    m = _load()
    assert "venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id)" in m.UPGRADE
    assert "payload    jsonb NOT NULL" in m.UPGRADE
    assert "deleted_at timestamptz" in m.UPGRADE
    assert "updated_at timestamptz NOT NULL DEFAULT now()" in m.UPGRADE


def test_downgrade_drops_exactly_the_table_this_migration_added():
    m = _load()
    assert "DROP TABLE IF EXISTS venues.reviews_deep;" in m.DOWNGRADE


def test_upgrade_and_downgrade_call_op_execute_exactly_once():
    import unittest.mock as mock

    m = _load()
    with mock.patch.object(m, "op") as mocked_op:
        m.upgrade()
        mocked_op.execute.assert_called_once_with(m.UPGRADE)
    with mock.patch.object(m, "op") as mocked_op:
        m.downgrade()
        mocked_op.execute.assert_called_once_with(m.DOWNGRADE)
