"""Guards for the venue-address-provenance migration (0045).

No local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection — the same offline-
guard shape as tests/test_operator_edited_fields_migration.py (0027) and
tests/test_time_known_migration.py (0035): additive, nullable columns with
a CHECK constraint on an enum-like text column.

Real-Postgres fidelity (the migration applies cleanly, the CHECK constraint
rejects an out-of-set value, downgrade drops exactly the four new columns
and leaves 0044 untouched, and re-upgrade round-trips cleanly) was verified
against a real, throwaway `postgres:16` container while writing this
migration, per this repo's own established practice for a migration whose
real-Postgres fidelity isn't exercised by the offline fakes. The precedence
SQL itself (the CASE-guarded UPDATE) is additionally proven against BOTH
the fake and — when RDS_TEST_URL is set — the real store in
tests/test_rds_store_contract.py's `test_address_precedence_*` cases; this
file protects against a REGRESSION to the migration's own shape, not
against the class of defect only a real bind can catch.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0045_venue_address_provenance.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0045_venue_address_provenance", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0045_venue_address_provenance"
    assert m.down_revision == "0044_event_flyer_media"
    assert len(m.revision) <= 32


def test_upgrade_adds_four_nullable_text_columns():
    """Nullable, no default — every existing row (all 3,600, per the
    plan's own census) gets NULL sources, correctly: nothing has been
    written under this provenance regime yet."""
    m = _load()
    for column in ("street_source", "neighborhood_source", "city_source", "postal_code_source"):
        assert f"ADD COLUMN IF NOT EXISTS {column} text" in m.UPGRADE
    lowered = m.UPGRADE.lower()
    assert "not null" not in lowered
    assert "default" not in lowered


def test_upgrade_touches_only_venues_address():
    m = _load()
    assert "ALTER TABLE venues.address" in m.UPGRADE
    assert "CREATE TABLE" not in m.UPGRADE
    assert "CREATE INDEX" not in m.UPGRADE
    assert "DROP" not in m.UPGRADE  # nothing dropped by the upgrade


def test_upgrade_adds_a_check_constraint_per_column_matching_the_three_tiers():
    """Matches 0042_venue_add_job_run.py's own raw-SQL-CHECK-constraint
    style (job_type/status) rather than a Postgres enum type. `llm` — a
    fourth, lower-still tier Phase 5 may add later — is deliberately NOT
    in this set; this migration creates only the three tiers this plan
    actually writes."""
    m = _load()
    for column in ("street_source", "neighborhood_source", "city_source", "postal_code_source"):
        assert (
            f"CHECK ({column} IS NULL OR {column} IN ('operator', 'google', 'parsed'))"
            in m.UPGRADE
        )
    assert "'llm'" not in m.UPGRADE


def test_downgrade_drops_exactly_the_four_columns_this_migration_added():
    m = _load()
    for column in ("street_source", "neighborhood_source", "city_source", "postal_code_source"):
        assert f"DROP COLUMN IF EXISTS {column}" in m.DOWNGRADE
    assert "DROP TABLE" not in m.DOWNGRADE
    assert "DROP INDEX" not in m.DOWNGRADE
    # Every constraint this migration added is explicitly dropped first
    # (Postgres would otherwise refuse to drop a column a CHECK references).
    for column in ("street_source", "neighborhood_source", "city_source", "postal_code_source"):
        assert f"DROP CONSTRAINT IF EXISTS ck_venue_address_{column}" in m.DOWNGRADE


def test_upgrade_and_downgrade_are_callable():
    """Guards against a typo turning upgrade/downgrade into dead code paths;
    does not execute SQL (no local Postgres in this offline suite — the
    real round trip was run separately against a throwaway container, see
    this module's own docstring), only calls the real function with a
    recording fake `op` to prove the bodies run without raising."""
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


class TestPrecedenceRankInlinedInSql:
    """`app/dao/rds_venue_store.py`'s own CASE-guarded precedence RANK must
    match the three tiers THIS migration's CHECK constraint permits —
    pinned here (not just in the migration's own docstring) so the two
    files cannot silently drift apart."""

    def test_rank_matches_the_migrations_three_permitted_tiers(self):
        from app.dao.rds_venue_store import RdsVenueStore

        assert set(RdsVenueStore._PROVENANCE_RANK) == {"operator", "google", "parsed"}
        assert RdsVenueStore._PROVENANCE_RANK["operator"] > RdsVenueStore._PROVENANCE_RANK["google"]
        assert RdsVenueStore._PROVENANCE_RANK["google"] > RdsVenueStore._PROVENANCE_RANK["parsed"]

    def test_fake_store_mirrors_the_same_rank(self):
        from tests.rds_fake import InMemoryRdsVenueStore

        assert InMemoryRdsVenueStore._PROVENANCE_RANK == {"operator": 3, "google": 2, "parsed": 1}
