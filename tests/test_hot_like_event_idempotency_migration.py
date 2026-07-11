"""Offline guards for the 0016 hot_like_event idempotency migration.

The SQL runs only against real Postgres (validated post-provisioning against a
scratch DB before the production backfill -- see the module docstring on the
migration itself), so these tests pin: the alembic chain, the mandatory
add-column -> backfill -> NOT NULL -> collapse-duplicates -> create-index
ORDERING (collapse must run before the unique index, or index creation fails
against real duplicate data), the keep-first (min id) collapse strategy, and
that downgrade() is schema-reversible while the row-collapse is irreversible
(the migration's own docstring, not code, is what documents that).
"""
import importlib.util
import re
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0016_hot_like_event_idempotency.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0016", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0016_hot_like_event_idempotency"
    assert m.down_revision == "0015_geofence_city_circles"


def test_adds_business_period_before_backfill_and_not_null():
    m = _load()
    add_sql = " ".join(m.ADD_COLUMN.split())
    assert "ADD COLUMN IF NOT EXISTS business_period date" in add_sql
    backfill_sql = " ".join(m.BACKFILL.split())
    # Backfill must derive the Recife CALENDAR DAY from created_at (the same
    # convention as app.utils.recife_time.recife_today()), not raw created_at.
    assert "AT TIME ZONE 'America/Recife'" in backfill_sql
    assert "::date" in backfill_sql
    assert "business_period" in backfill_sql
    not_null_sql = " ".join(m.SET_NOT_NULL.split())
    assert "SET NOT NULL" in not_null_sql
    assert "business_period" in not_null_sql


def test_collapse_keeps_minimum_id_per_natural_key_group():
    """Keep-first == keep the row with the smallest `id` (bigserial insertion
    order) per (user_pseudo, venue_id, business_period) group."""
    sql = " ".join(_load().COLLAPSE_DUPLICATES.split())
    assert "DELETE FROM engagement.hot_like_event" in sql
    assert "a.user_pseudo = b.user_pseudo" in sql
    assert "a.venue_id = b.venue_id" in sql
    assert "a.business_period = b.business_period" in sql
    assert "a.id > b.id" in sql  # deletes every row except the min-id survivor


def test_upgrade_runs_collapse_before_creating_the_unique_index():
    """The unique index cannot be created while duplicate rows exist -- the
    collapse step's op.execute call must appear before the create-index call
    in upgrade()'s source, in that literal order."""
    m = _load()
    src = Path(__file__).resolve().parent.parent.joinpath(
        "migrations", "versions", "0016_hot_like_event_idempotency.py"
    ).read_text()
    upgrade_body = re.search(r"def upgrade\(\).*?(?=\ndef downgrade)", src, re.S).group(0)
    collapse_pos = upgrade_body.index("COLLAPSE_DUPLICATES")
    index_pos = upgrade_body.index("CREATE_UNIQUE_INDEX")
    assert collapse_pos < index_pos, "COLLAPSE_DUPLICATES must run before CREATE_UNIQUE_INDEX"
    # also confirm NOT NULL and backfill precede the collapse (order matters
    # for a clean upgrade path, even though NOT NULL doesn't care about dups)
    backfill_pos = upgrade_body.index("BACKFILL")
    not_null_pos = upgrade_body.index("SET_NOT_NULL")
    assert backfill_pos < not_null_pos < collapse_pos


def test_creates_the_unique_index_on_the_exact_natural_key():
    sql = " ".join(_load().CREATE_UNIQUE_INDEX.split())
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_hot_like_event_user_venue_period" in sql
    assert "ON engagement.hot_like_event (user_pseudo, venue_id, business_period)" in sql


def test_downgrade_drops_index_and_column_not_the_table():
    sql = " ".join(_load().DOWNGRADE.split())
    assert "DROP INDEX IF EXISTS engagement.ux_hot_like_event_user_venue_period" in sql
    assert "DROP COLUMN IF EXISTS business_period" in sql
    assert "DROP TABLE" not in sql  # the append-only event log itself is untouched
