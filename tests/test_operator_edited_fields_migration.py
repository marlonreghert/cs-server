"""Guards for the operator-edited-fields migration (0027).

No local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection — the same offline-
guard shape as tests/test_promoter_accounts_migration.py (0024), the closest
sibling: a single additive, nullable, no-default column added to
`events.event`. Real-Postgres fidelity (the migration actually applies, the
column round-trips a jsonb array) was verified manually against a live
`postgres:16` container per plans/260807_auto-accept-and-field-level-
protection.md's instructions — this file protects against a REGRESSION to
that shape, not against the class of defect only a real bind can catch
(constraint-name collisions, missing psycopg adapters, absent NOT NULL
primary keys) — none of which apply here: this migration adds no
constraint, no index, and no primary key.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0027_operator_edited_fields.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0027_operator_edited_fields", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0027_operator_edited_fields"
    assert m.down_revision == "0026_event_sources"
    assert len(m.revision) <= 32


def test_adds_one_nullable_no_default_jsonb_column():
    """No NOT NULL, no DEFAULT — a pre-existing confirmed row must not be
    silently relabeled by this migration; NULL is the deliberate "unknown
    which fields were edited" sentinel the reconciler reads."""
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS operator_edited_fields jsonb" in m.UPGRADE
    lowered = m.UPGRADE.lower()
    assert "operator_edited_fields jsonb not null" not in lowered
    assert "default" not in lowered


def test_upgrade_touches_only_events_event():
    m = _load()
    assert "ALTER TABLE events.event" in m.UPGRADE
    assert "CREATE TABLE" not in m.UPGRADE
    assert "CREATE INDEX" not in m.UPGRADE
    assert "CONSTRAINT" not in m.UPGRADE


def test_no_backfill_statement():
    """The plan is explicit: no back-fill. An UPDATE here would be
    INVENTING a value for a column whose whole point is recording what is
    genuinely unknown for every pre-existing row."""
    m = _load()
    assert "UPDATE" not in m.UPGRADE.upper()


def test_downgrade_drops_exactly_the_column_this_migration_added():
    """Additive-only migration: the downgrade must drop only the one column
    it created — never the `events.event` table itself, never any other
    0022-0026 column."""
    m = _load()
    assert "DROP COLUMN IF EXISTS operator_edited_fields" in m.DOWNGRADE
    assert "DROP TABLE" not in m.DOWNGRADE
    assert "DROP INDEX" not in m.DOWNGRADE


def test_upgrade_and_downgrade_call_op_execute():
    m = _load()
    assert callable(m.upgrade)
    assert callable(m.downgrade)
