"""Guards for the expose-time-known migration (0035).

Same offline-guard shape as tests/test_event_ticket_info_and_attractions_
migration.py (the closest sibling: additive column on events.post_item) —
no local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection.

Unlike 0028 (nullable, no default, no back-fill), this migration WAS also
run against a real, throwaway `postgres:16` container while writing it,
migrated through 0034 first (exactly `.github/workflows/tests.yml`'s
scratch-Postgres step): `upgrade head` -> confirmed `time_known boolean NOT
NULL DEFAULT false` -> inserted a post_item/post_item_source pair via the
real `RdsVenueStore` whose source's `raw_extraction` carried
`{"time_known": true}` (column read back `false`, matching the DAO's
"only inserts columns present in `fields`" contract) -> ran this
migration's own UPDATE statement in isolation, which flipped that row to
`true` while a sibling row with no matching raw_extraction key stayed
`false` -> `downgrade -1` -> confirmed the column was gone and every 0034
column/constraint/index was untouched -> `upgrade head` again, confirming
BOTH rows' data survived the round trip (the UPDATE re-ran and reproduced
the same true/false split). tests/test_rds_store_contract.py's
`test_event_time_known_*` cases are what run against RDS_TEST_URL in CI/dev
when it is set; this file is the offline guard that travels with the repo,
matching the posture tests/test_post_items_migration.py already
established for 0034.
"""
import importlib.util
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0035_time_known.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0035_time_known", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_chain():
    m = _load()
    assert m.revision == "0035_time_known"
    assert m.down_revision == "0034_post_items"
    assert len(m.revision) <= 32


def test_upgrade_adds_not_null_default_false_column():
    """NOT NULL DEFAULT false — never nullable, never DEFAULT true. The
    plan's own instruction: an item with no recorded flag is one whose time
    cannot be vouched for."""
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS time_known boolean NOT NULL DEFAULT false" in m.UPGRADE


def test_upgrade_touches_only_events_post_item():
    """events.post_item_source is READ (the backfill's FROM/subquery), never
    ALTERed — the only table this migration modifies is events.post_item."""
    m = _load()
    assert "ALTER TABLE events.post_item\n" in m.UPGRADE
    assert "ALTER TABLE events.post_item_source" not in m.UPGRADE
    assert "UPDATE events.post_item " in m.UPGRADE
    assert "UPDATE events.post_item_source" not in m.UPGRADE
    assert "FROM events.post_item_source" in m.UPGRADE
    assert "CREATE TABLE" not in m.UPGRADE
    assert "CREATE INDEX" not in m.UPGRADE
    assert "CONSTRAINT" not in m.UPGRADE


def test_backfill_only_ever_sets_true_never_false():
    """The ADD COLUMN's own DEFAULT already back-fills every row to false —
    the UPDATE exists only to promote the determinable subset to true, and
    must never write the literal false anywhere (that would risk stomping a
    value the DEFAULT already got right, and contradicts the plan's "leave
    the rest false" — nothing should ever need to un-default a row back to
    what it already is)."""
    m = _load()
    assert "SET time_known = true" in m.UPGRADE
    assert "SET time_known = false" not in m.UPGRADE.lower().replace("SET time_known = true", "")


def test_backfill_reads_the_most_recently_seen_source_matching_event_select():
    """Must pick the SAME source `_EVENT_SELECT`'s `ps` LATERAL would
    (app/dao/rds_venue_store.py) — the most recently seen one — or the
    back-filled value could disagree with what the API actually serves for
    that row afterward."""
    m = _load()
    assert "DISTINCT ON (es.post_item_id)" in m.UPGRADE
    assert "ORDER BY es.post_item_id, es.last_seen_at DESC, es.id DESC" in m.UPGRADE


def test_backfill_reads_time_known_from_raw_extraction_as_a_json_bool_literal():
    m = _load()
    assert "raw_extraction ->> 'time_known') = 'true'" in m.UPGRADE


def test_downgrade_drops_exactly_the_one_column_this_migration_added():
    m = _load()
    assert m.DOWNGRADE.strip() == (
        "ALTER TABLE events.post_item\n  DROP COLUMN IF EXISTS time_known;"
    )
    assert "DROP TABLE" not in m.DOWNGRADE
    assert "DROP INDEX" not in m.DOWNGRADE


def test_upgrade_and_downgrade_are_callable():
    """Guards against a typo turning upgrade/downgrade into dead code paths;
    does not execute SQL (no local Postgres in this offline suite — the real
    round trip was run separately against a throwaway container, see this
    module's own docstring), only calls the real function with a recording
    fake `op` to prove the bodies run without raising."""
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


class TestBothEventProjectionsCarryTimeKnown:
    """The plan's own named trap: `_EVENT_SELECT` and `_EVENT_SOURCE_SELECT`
    (app/dao/rds_venue_store.py) must BOTH project `time_known` — missing
    the second is invisible to most tests (list_events/get_event still work
    fine) and silently drops the value list_events_by_source's callers rely
    on. Asserted on the real SQL text directly, the same posture
    tests/test_event_ticket_info_and_attractions_migration.py already takes
    for the ticket_info/attractions pair."""

    def test_event_select_projects_time_known(self):
        from app.dao.rds_venue_store import RdsVenueStore

        assert "e.time_known" in RdsVenueStore._EVENT_SELECT

    def test_event_source_select_projects_time_known(self):
        from app.dao.rds_venue_store import RdsVenueStore

        assert "e.time_known" in RdsVenueStore._EVENT_SOURCE_SELECT

    def test_event_columns_include_time_known_as_a_plain_scalar(self):
        from app.dao.rds_venue_store import RdsVenueStore

        assert "time_known" in RdsVenueStore._EVENT_COLUMNS
        # A plain boolean column — never jsonb-cast.
        assert "time_known" not in RdsVenueStore._EVENT_JSONB_COLUMNS

    def test_time_known_is_not_an_independent_protectable_field(self):
        """Deliberately excluded from PROTECTABLE_EVENT_FIELDS: it is
        metadata ABOUT starts_at, not independent content, and comparing it
        as an ordinary scalar would flag REVIEW_REASON_DIVERGES_FROM_
        CONFIRMED purely because two posts disagree on whether a time was
        stated, even when starts_at itself did not change. See
        app.services.event_reconciliation._confirmed_update_fields's
        docstring — it travels WITH starts_at instead, handled explicitly
        wherever starts_at's own merge decision is made."""
        from app.services.event_reconciliation import PROTECTABLE_EVENT_FIELDS

        assert "time_known" not in PROTECTABLE_EVENT_FIELDS
