"""Guards for the post-items-and-categories migration (0034).

No local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection — the same
offline-guard shape as tests/test_event_table_migration.py (0023) and
tests/test_event_sources_migration.py (0026).

Unlike those two, this migration was ALSO run against a real, throwaway
Postgres 16 (every migration through 0033, then this one) while writing it:
`upgrade head` -> introspected via `pg_constraint`/`pg_indexes` to read the
REAL pre-existing constraint/index names this migration renames (they are
quoted in the migration's own module docstring), then `downgrade -1` ->
verified the schema matched pre-migration byte-for-byte (same constraint
names, same index names, same columns) -> `upgrade head` again, with a row
inserted before the downgrade surviving the round trip with
`post_type='event'`. This file is the offline CI guard that travels with the
repo; the real-Postgres run is not reproducible in CI (no local Postgres
here) so it cannot be encoded as a pytest case, only pinned by inspection.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0034_post_items.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0034_post_items", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0034_post_items"
    assert m.down_revision == "0033_crawl_target_reels_overlap"
    assert len(m.revision) <= 32


def test_upgrade_renames_both_tables_with_alter_table_rename():
    """ALTER TABLE ... RENAME, never CREATE-and-copy — the tables carry live
    production data and foreign keys; a copy would silently drop whatever it
    forgets."""
    m = _load()
    assert "ALTER TABLE events.event RENAME TO post_item;" in m.UPGRADE
    assert "ALTER TABLE events.event_source RENAME TO post_item_source;" in m.UPGRADE
    assert "CREATE TABLE" not in m.UPGRADE
    assert "INSERT INTO" not in m.UPGRADE


def test_upgrade_renames_both_id_columns():
    m = _load()
    assert "ALTER TABLE events.post_item RENAME COLUMN event_id TO post_item_id;" in m.UPGRADE
    assert (
        "ALTER TABLE events.post_item_source RENAME COLUMN event_id TO post_item_id;"
        in m.UPGRADE
    )


def test_upgrade_renames_every_constraint_and_index_read_off_real_postgres():
    """Every name below is the ACTUAL pre-migration name, read back from a
    real Postgres that had run every migration through 0033 (see this
    module's own docstring, and the migration file's) — none of it is
    guessed from the CREATE TABLE statements, which is exactly how this
    repo has been bitten by a constraint-name collision before."""
    m = _load()
    renames = [
        ("event_pkey", "post_item_pkey"),
        ("event_venue_id_fkey", "post_item_venue_id_fkey"),
        ("event_source_pkey", "post_item_source_pkey"),
        ("event_source_event_id_fkey", "post_item_source_post_item_id_fkey"),
        ("uq_event_source_post", "uq_post_item_source_post"),
    ]
    for old, new in renames:
        assert f"RENAME CONSTRAINT {old} TO {new};" in m.UPGRADE, old

    index_renames = [
        ("ix_event_status", "ix_post_item_status"),
        ("ix_event_venue_starts_at", "ix_post_item_venue_starts_at"),
        ("ix_event_source_event_id", "ix_post_item_source_post_item_id"),
    ]
    for old, new in index_renames:
        assert f"ALTER INDEX events.{old} RENAME TO {new};" in m.UPGRADE, old


def test_upgrade_adds_post_type_not_null_with_default_and_nullable_category():
    """post_type NOT NULL DEFAULT 'event' is what back-fills every EXISTING
    row (a fast, catalog-only ADD COLUMN on Postgres 11+, no table rewrite —
    not a data-touching UPDATE) — 'event' is what the pipeline believed when
    it wrote them. No CHECK constraint: an unrecognised post_type is stored
    verbatim at the application layer (app.models.event_kind.
    resolve_post_type), and a CHECK would defeat exactly that. category has
    no default and no back-fill — it was never extracted for an existing
    row, and inventing one from a title would be a guess."""
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS post_type text NOT NULL DEFAULT 'event'" in m.UPGRADE
    assert "ADD COLUMN IF NOT EXISTS category text" in m.UPGRADE
    assert "CHECK" not in m.UPGRADE


def test_downgrade_drops_the_two_new_columns_first():
    """The new columns must go before anything is renamed back — the SAME
    ordering discipline 0026's migration documents (drop what a later step
    needs gone before an earlier-numbered rename touches the same table)."""
    m = _load()
    downgrade_stripped = m.DOWNGRADE.strip()
    assert downgrade_stripped.startswith("ALTER TABLE events.post_item\n  DROP COLUMN IF EXISTS category,\n  DROP COLUMN IF EXISTS post_type;")


def test_downgrade_reverses_every_rename_the_upgrade_made():
    """Every rename the upgrade performs has an exact inverse in the
    downgrade — proven pairwise rather than trusted by symmetry of intent,
    since a rename migration that cannot go back is a trap."""
    m = _load()
    constraint_renames = [
        ("post_item_pkey", "event_pkey"),
        ("post_item_venue_id_fkey", "event_venue_id_fkey"),
        ("post_item_source_pkey", "event_source_pkey"),
        ("post_item_source_post_item_id_fkey", "event_source_event_id_fkey"),
        ("uq_post_item_source_post", "uq_event_source_post"),
    ]
    for old, new in constraint_renames:
        assert f"RENAME CONSTRAINT {old} TO {new};" in m.DOWNGRADE, old

    index_renames = [
        ("ix_post_item_status", "ix_event_status"),
        ("ix_post_item_venue_starts_at", "ix_event_venue_starts_at"),
        ("ix_post_item_source_post_item_id", "ix_event_source_event_id"),
    ]
    for old, new in index_renames:
        assert f"ALTER INDEX events.{old} RENAME TO {new};" in m.DOWNGRADE, old

    assert "ALTER TABLE events.post_item_source RENAME COLUMN post_item_id TO event_id;" in m.DOWNGRADE
    assert "ALTER TABLE events.post_item RENAME COLUMN post_item_id TO event_id;" in m.DOWNGRADE
    assert "ALTER TABLE events.post_item_source RENAME TO event_source;" in m.DOWNGRADE
    assert "ALTER TABLE events.post_item RENAME TO event;" in m.DOWNGRADE
    assert "DROP TABLE" not in m.DOWNGRADE


def test_upgrade_and_downgrade_are_callable():
    """Guards against a typo turning upgrade/downgrade into dead code paths;
    does not execute SQL (no local Postgres in this offline suite — the real
    round trip was run separately, see this module's docstring), only calls
    the real function with a recording fake `op` to prove the bodies run
    without raising."""
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
