"""Guards for the ticket_info/attractions migration (0028).

Same offline-guard shape as tests/test_operator_edited_fields_migration.py
(the closest sibling: additive, nullable, no-default columns on
events.event, no constraint, no index) — no local Postgres in CI (see
tests/README.md), so this pins the revision chain and the load-bearing SQL
fragments by inspection. Real-Postgres round-trip fidelity (both columns
actually add, attractions survives a jsonb round trip) is covered by
tests/test_rds_store_contract.py's `store` fixture, which runs against a
real scratch Postgres too when RDS_TEST_URL is set.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0028_event_ticket_info_and_attractions.py"
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "m0028_event_ticket_info_and_attractions", _PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0028_event_ticket_info_and_attractions"
    assert m.down_revision == "0027_operator_edited_fields"


def test_adds_two_nullable_no_default_columns():
    """No NOT NULL, no DEFAULT for either column — neither describes
    information the model was ever asked to produce on earlier runs; NULL is
    the historically accurate value for every pre-existing row."""
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS ticket_info text" in m.UPGRADE
    assert "ADD COLUMN IF NOT EXISTS attractions jsonb" in m.UPGRADE
    lowered = m.UPGRADE.lower()
    assert "not null" not in lowered
    assert "default" not in lowered


def test_upgrade_touches_only_events_event():
    m = _load()
    assert "ALTER TABLE events.event" in m.UPGRADE
    assert "CREATE TABLE" not in m.UPGRADE
    assert "CREATE INDEX" not in m.UPGRADE
    assert "CONSTRAINT" not in m.UPGRADE


def test_no_backfill_statement():
    m = _load()
    assert "UPDATE" not in m.UPGRADE.upper()


def test_downgrade_drops_exactly_the_two_columns_this_migration_added():
    """Additive-only migration: the downgrade drops only ticket_info and
    attractions — no destructive-refusal guard needed (unlike 0026's),
    because dropping per-row detail captured after this migration ran never
    merges or deletes a row."""
    m = _load()
    assert "DROP COLUMN IF EXISTS ticket_info" in m.DOWNGRADE
    assert "DROP COLUMN IF EXISTS attractions" in m.DOWNGRADE
    assert "DROP TABLE" not in m.DOWNGRADE
    assert "DROP INDEX" not in m.DOWNGRADE


def test_upgrade_and_downgrade_call_op_execute():
    m = _load()
    assert callable(m.upgrade)
    assert callable(m.downgrade)


class TestBothEventProjectionsCarryBothColumns:
    """The plan's own named trap: `_EVENT_SELECT` and `_EVENT_SOURCE_SELECT`
    (app/dao/rds_venue_store.py) must BOTH project ticket_info/attractions —
    missing the second is invisible to most tests (list_events/get_event
    still work fine) and silently starves reconciliation of the values
    list_events_by_source is merging. Asserted on the real SQL text
    directly, the same posture test_review_queue_completeness.py already
    takes for this exact pair of query constants."""

    def test_event_select_projects_both_columns(self):
        from app.dao.rds_venue_store import RdsVenueStore

        sql = RdsVenueStore._EVENT_SELECT
        assert "e.ticket_info" in sql
        assert "e.attractions" in sql

    def test_event_source_select_projects_both_columns(self):
        from app.dao.rds_venue_store import RdsVenueStore

        sql = RdsVenueStore._EVENT_SOURCE_SELECT
        assert "e.ticket_info" in sql
        assert "e.attractions" in sql

    def test_event_columns_and_jsonb_columns_include_the_new_fields(self):
        from app.dao.rds_venue_store import RdsVenueStore

        assert "ticket_info" in RdsVenueStore._EVENT_COLUMNS
        assert "attractions" in RdsVenueStore._EVENT_COLUMNS
        # attractions binds through the jsonb CAST(:col AS jsonb) + json.dumps
        # path — psycopg has no adapter for a bare dict/list otherwise (the
        # trap migration 0026 already hit). ticket_info is plain text: it
        # must NOT be in this tuple, or the DAO would try to jsonb-cast a
        # string.
        assert "attractions" in RdsVenueStore._EVENT_JSONB_COLUMNS
        assert "ticket_info" not in RdsVenueStore._EVENT_JSONB_COLUMNS
