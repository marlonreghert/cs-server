"""Guards for the promoter-accounts migration (0024).

No local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection — the same offline-guard
shape as tests/test_events_schema_migration.py (0022) and
tests/test_event_table_migration.py (0023), which this migration chains from.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0024_promoter_accounts.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0024", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0024_promoter_accounts"
    assert m.down_revision == "0023_event_table"
    assert len(m.revision) <= 32


def test_creates_promoter_account_table():
    m = _load()
    assert "CREATE TABLE IF NOT EXISTS events.promoter_account" in m.UPGRADE
    assert "handle                    text PRIMARY KEY" in m.UPGRADE


def test_promoter_account_status_defaults_to_candidate():
    """Discovery proposes, an operator disposes: a freshly discovered or
    freshly inserted row must never default to crawlable."""
    m = _load()
    assert "status                    text NOT NULL DEFAULT 'candidate'" in m.UPGRADE


def test_promoter_account_status_is_indexed():
    m = _load()
    assert "ix_promoter_account_status" in m.UPGRADE
    assert "ON events.promoter_account (status)" in m.UPGRADE


def test_creates_event_venue_link_candidate_table():
    m = _load()
    assert "CREATE TABLE IF NOT EXISTS events.event_venue_link_candidate" in m.UPGRADE


def test_link_candidate_references_event_and_venue():
    m = _load()
    assert "event_id   text NOT NULL REFERENCES events.event(event_id)" in m.UPGRADE
    assert "venue_id   text NOT NULL REFERENCES venues.venue(venue_id)" in m.UPGRADE


def test_link_candidate_event_is_indexed():
    m = _load()
    assert "ix_event_venue_link_candidate_event" in m.UPGRADE
    assert "ON events.event_venue_link_candidate (event_id)" in m.UPGRADE


def test_event_gains_four_additive_nullable_columns():
    """No NOT NULL, no DEFAULT, on any of the four — a pre-existing
    venue-owned event row must not be silently relabeled by this migration."""
    m = _load()
    for fragment in (
        "ADD COLUMN IF NOT EXISTS location_resolution text",
        "ADD COLUMN IF NOT EXISTS location_confidence double precision",
        "ADD COLUMN IF NOT EXISTS linked_by text",
        "ADD COLUMN IF NOT EXISTS linked_at timestamptz",
    ):
        assert fragment in m.UPGRADE, fragment
    lowered = m.UPGRADE.lower()
    assert "location_resolution text not null" not in lowered
    assert "location_confidence double precision not null" not in lowered
    assert "linked_by text not null" not in lowered
    assert "linked_at timestamptz not null" not in lowered


def test_downgrade_drops_exactly_what_this_migration_created():
    """Additive migration onto tables 0022/0023 own: the downgrade must drop
    only what THIS migration created — never the `events` schema, never
    `events.event` itself, never `events.venue_event_profile`."""
    m = _load()
    assert "DROP TABLE IF EXISTS events.event_venue_link_candidate" in m.DOWNGRADE
    assert "DROP TABLE IF EXISTS events.promoter_account" in m.DOWNGRADE
    assert "DROP TABLE IF EXISTS events.event;" not in m.DOWNGRADE
    assert "DROP SCHEMA" not in m.DOWNGRADE
    assert "venue_event_profile" not in m.DOWNGRADE


def test_downgrade_drops_its_own_indexes():
    m = _load()
    assert "DROP INDEX IF EXISTS events.ix_promoter_account_status" in m.DOWNGRADE
    assert (
        "DROP INDEX IF EXISTS events.ix_event_venue_link_candidate_event"
        in m.DOWNGRADE
    )


def test_downgrade_drops_the_four_columns_it_added():
    m = _load()
    for fragment in (
        "DROP COLUMN IF EXISTS location_resolution",
        "DROP COLUMN IF EXISTS location_confidence",
        "DROP COLUMN IF EXISTS linked_by",
        "DROP COLUMN IF EXISTS linked_at",
    ):
        assert fragment in m.DOWNGRADE, fragment


def test_upgrade_and_downgrade_are_callable():
    """Guards against a typo turning upgrade/downgrade into dead code paths;
    does not execute SQL (no local Postgres), only calls the real function
    with a recording fake `op` to prove the bodies run without raising."""
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
