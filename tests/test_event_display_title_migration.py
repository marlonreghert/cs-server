"""Guards for the display-title migration (0046_event_display_title).

plans/260912_events-venue-night-duplication.md's own BDD exemption: "migration
0046's own up/down round trip has no externally observable behaviour — pinned
instead by an offline SQL-inspection test in the shape of
`tests/test_event_sources_migration.py`". No local Postgres in CI (see
tests/README.md), so this pins the revision chain and the load-bearing SQL by
inspection, the same offline-guard shape
`tests/test_venue_address_provenance_migration.py` (0045) and
`tests/test_time_known_migration.py` (0035) already use for an additive,
nullable column.

The one thing a migration file alone cannot guarantee — that the column is
also in `RdsVenueStore._EVENT_COLUMNS`, without which the SQL store silently
no-ops every write while the in-memory fake accepts it — is asserted here
too, because that failure passes every BDD scenario and fails only in prod.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0046_event_display_title.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0046_event_display_title", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0046_event_display_title"
    assert m.down_revision == "0045_venue_address_provenance"
    assert len(m.revision) <= 32


def test_upgrade_adds_one_nullable_text_column():
    """Nullable, no default, no back-fill: the projection writes
    `display_title or title`, so every existing row keeps serving exactly
    what it serves today until something chooses a display title for it."""
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS display_title text" in m.UPGRADE
    lowered = m.UPGRADE.lower()
    assert "not null" not in lowered
    assert "default" not in lowered
    assert "update" not in lowered  # no back-fill


def test_upgrade_touches_only_events_post_item():
    m = _load()
    assert "ALTER TABLE events.post_item" in m.UPGRADE
    assert "CREATE TABLE" not in m.UPGRADE
    assert "CREATE INDEX" not in m.UPGRADE
    assert "DROP" not in m.UPGRADE


def test_the_migration_never_touches_title_itself():
    """`title` is an INPUT to `compute_event_identity`. A migration that
    rewrote it would re-key the corpus — the exact hazard `260812` §A is
    built around."""
    m = _load()
    assert "title text" not in m.UPGRADE.replace("display_title text", "")
    assert "RENAME" not in m.UPGRADE


def test_downgrade_drops_exactly_the_one_column_this_migration_added():
    m = _load()
    assert "DROP COLUMN IF EXISTS display_title" in m.DOWNGRADE
    assert "DROP TABLE" not in m.DOWNGRADE
    assert "DROP INDEX" not in m.DOWNGRADE
    assert "post_item_id" not in m.DOWNGRADE


def test_the_column_is_in_the_dao_write_allowlist():
    """The failure `_EVENT_COLUMNS`' own comment warns about: omit a new
    column there and `update_event` silently no-ops against real Postgres
    while the fake accepts it, which passes every test and fails only in
    prod."""
    from app.dao.rds_venue_store import RdsVenueStore

    assert "display_title" in RdsVenueStore._EVENT_COLUMNS


def test_the_column_is_never_part_of_operator_field_protection():
    """`display_title` is DERIVED presentation, not content an operator
    edits — it must never join the protectable-scalar table, or a merge
    would start treating a chosen string as a value to defend."""
    from app.services.event_reconciliation import PROTECTABLE_EVENT_FIELDS

    assert "display_title" not in PROTECTABLE_EVENT_FIELDS


def test_the_upgrade_is_idempotent_in_shape():
    m = _load()
    assert "IF NOT EXISTS" in m.UPGRADE
    assert "IF EXISTS" in m.DOWNGRADE
