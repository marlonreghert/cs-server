"""Guards for the crawl-target migration (0030). No local Postgres in CI
(see tests/README.md), so this pins the revision chain and the load-bearing
SQL fragments by inspection — the same offline-guard shape
tests/test_promoter_accounts_migration.py (0024) already established.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0030_crawl_target.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0030", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0030_crawl_target"
    # Chains from the ACTUAL head at write time (0029_blocked_venues) — the
    # plan text named 0028_event_ticket_info_and_attractions, which was
    # already stale by the time this migration was written (0029_blocked_
    # venues had already merged to main).
    assert m.down_revision == "0029_blocked_venues"
    assert len(m.revision) <= 32


def test_creates_crawl_target_table_keyed_on_handle():
    m = _load()
    assert "CREATE TABLE IF NOT EXISTS events.crawl_target" in m.UPGRADE
    assert "handle              text PRIMARY KEY" in m.UPGRADE


def test_kind_and_cron_are_not_null_with_no_default():
    """`kind`/`cron` are NOT NULL with NO default — a target is created BY
    an operator WITH a schedule, never inserted schedule-less. Both must be
    absent from any DEFAULT clause."""
    m = _load()
    assert "kind                text NOT NULL," in m.UPGRADE
    assert "cron                text NOT NULL," in m.UPGRADE


def test_kind_check_constraint_restricts_to_venue_or_promoter():
    m = _load()
    assert "CONSTRAINT ck_crawl_target_kind CHECK (kind IN ('venue', 'promoter'))" in m.UPGRADE


def test_enabled_defaults_true_and_is_indexed():
    m = _load()
    assert "enabled             boolean NOT NULL DEFAULT true" in m.UPGRADE
    assert "ix_crawl_target_enabled" in m.UPGRADE
    assert "ON events.crawl_target (enabled)" in m.UPGRADE


def test_timezone_defaults_to_america_recife():
    m = _load()
    assert "timezone            text NOT NULL DEFAULT 'America/Recife'" in m.UPGRADE


def test_two_independent_cursor_columns_both_nullable():
    """§B/§E: separate cursors for posts and reels — a single shared column
    would silently drop whichever stream advances second."""
    m = _load()
    assert "cursor_posts_at     timestamptz," in m.UPGRADE
    assert "cursor_reels_at     timestamptz," in m.UPGRADE


def test_last_run_results_and_consecutive_failures_default_zero_not_null():
    m = _load()
    assert "last_run_results    integer NOT NULL DEFAULT 0" in m.UPGRADE
    assert "consecutive_failures integer NOT NULL DEFAULT 0" in m.UPGRADE


def test_downgrade_drops_exactly_what_upgrade_created():
    m = _load()
    assert "DROP TABLE IF EXISTS events.crawl_target" in m.DOWNGRADE
    assert "DROP INDEX IF EXISTS events.ix_crawl_target_enabled" in m.DOWNGRADE
    # Additive-only: no other table's name appears in either direction.
    for other_table in (
        "events.event", "events.promoter_account", "instagram.handle", "venues.venue",
    ):
        assert other_table not in m.UPGRADE
        assert other_table not in m.DOWNGRADE
