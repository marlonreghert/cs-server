"""Guards for the multi-event-posts migration (0025).

No local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection — the same
offline-guard shape as tests/test_events_schema_migration.py (0022),
tests/test_event_table_migration.py (0023), and
tests/test_promoter_accounts_migration.py (0024).

Two things are unique to this migration and get their own tests beyond that
established shape:
  - The backfill must run BEFORE the new constraint is added (§A of
    plans/260806_multi-event-posts.md) — proven here by DRIVING upgrade()
    against a fake bind and asserting the call order, not just checking two
    substrings exist somewhere in a string.
  - downgrade() must REFUSE when any post holds more than one event —
    proven by driving downgrade() against a fake bind that reports a
    violation, and asserting the destructive SQL never runs.
"""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0025_multi_event_posts.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0025", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _FakeBind:
    """Records every statement (plain string OR sa.text(...) clause) in
    execution order, and answers the two read queries the migration issues
    (the unbackfilled-rows SELECT and the multi-event-posts COUNT) from
    caller-programmed fixtures — everything else is a write that returns an
    empty result."""

    def __init__(self, unbackfilled_rows=None, multi_event_count=0):
        self.executed: list[str] = []
        self._unbackfilled_rows = unbackfilled_rows or []
        self._multi_event_count = multi_event_count
        # event_id -> key actually written by a backfill UPDATE, so a test
        # can prove the WRITTEN value matches the runtime function's output
        # rather than merely that some UPDATE statement ran.
        self.backfilled_keys: dict[str, str] = {}

    def execute(self, clause, params=None):
        sql = str(clause)
        self.executed.append(sql)
        if "SELECT event_id, title, starts_at FROM events.event" in sql:
            return _FakeResult(self._unbackfilled_rows)
        if "UPDATE events.event SET source_event_key" in sql and params:
            self.backfilled_keys[params["event_id"]] = params["key"]
            return _FakeResult([])
        if "multi_event_posts" in sql:
            return _FakeResult([(self._multi_event_count,)])
        return _FakeResult([])


class _FakeOp:
    """`op.execute(str)` and `op.get_bind()` share ONE recorder, so the test
    can assert the true, single, chronological order of every statement the
    migration issues regardless of which API produced it."""

    def __init__(self, unbackfilled_rows=None, multi_event_count=0):
        self.bind = _FakeBind(unbackfilled_rows, multi_event_count)

    def execute(self, sql):
        self.bind.execute(sql)

    def get_bind(self):
        return self.bind


def test_revision_chain():
    m = _load()
    assert m.revision == "0025_multi_event_posts"
    assert m.down_revision == "0024_promoter_accounts"
    assert len(m.revision) <= 32


def test_adds_the_two_nullable_columns_with_no_default():
    m = _load()
    assert "ADD COLUMN IF NOT EXISTS source_event_key text" in m._ADD_COLUMNS
    assert "ADD COLUMN IF NOT EXISTS source_event_index integer" in m._ADD_COLUMNS
    lowered = m._ADD_COLUMNS.lower()
    assert "source_event_key text not null" not in lowered
    assert "source_event_index integer not null" not in lowered
    assert "default" not in lowered


def test_new_constraint_is_the_three_column_unique():
    m = _load()
    assert (
        "UNIQUE (source_handle, source_shortcode, source_event_key)"
        in m._ADD_NEW_CONSTRAINT
    )


def test_old_constraint_is_dropped():
    m = _load()
    assert "DROP CONSTRAINT IF EXISTS uq_event_source;" in m._DROP_OLD_CONSTRAINT


# ── the ordering trap: backfill BEFORE the new constraint ────────────────────
def test_upgrade_backfills_every_row_before_adding_the_new_constraint():
    """Drives the REAL upgrade() against a fake bind carrying two
    unbackfilled rows, and asserts the true statement order: columns added,
    THEN every row updated, THEN the old constraint dropped and the new one
    added. Adding the constraint before the backfill completes is exactly
    the ordering bug §A of the plan calls out — this fails loudly if that
    order ever regresses, not just "both statements exist somewhere"."""
    m = _load()
    rows = [
        {"event_id": "evt_1", "title": "O Homem do Fraque Verde",
         "starts_at": datetime(2026, 8, 5, tzinfo=timezone.utc)},
        {"event_id": "evt_2", "title": "Festa Sem Data", "starts_at": None},
    ]
    fake_op = _FakeOp(unbackfilled_rows=rows)
    m.op = fake_op
    m.upgrade()

    executed = fake_op.bind.executed
    add_columns_idx = next(i for i, s in enumerate(executed) if "ADD COLUMN IF NOT EXISTS source_event_key" in s)
    update_indices = [i for i, s in enumerate(executed) if "UPDATE events.event SET source_event_key" in s]
    drop_old_idx = next(i for i, s in enumerate(executed) if "DROP CONSTRAINT IF EXISTS uq_event_source;" in s)
    add_new_idx = next(i for i, s in enumerate(executed) if "uq_event_source_key" in s and "ADD CONSTRAINT" in s)

    assert len(update_indices) == 2, executed  # one backfill UPDATE per unbackfilled row
    assert add_columns_idx < min(update_indices), executed
    assert max(update_indices) < drop_old_idx, executed
    assert drop_old_idx < add_new_idx, executed


def test_upgrade_derives_the_backfilled_key_the_same_way_the_runtime_does():
    """The backfilled key must be byte-for-byte what
    app.services.event_identity.compute_source_event_key produces for the
    same (title, starts_at) — anything else and the next re-extraction of
    that post fails to find its own row and duplicates it."""
    m = _load()
    from app.services.event_identity import compute_source_event_key

    starts_at = datetime(2026, 8, 5, tzinfo=timezone.utc)
    rows = [{"event_id": "evt_1", "title": "O Homem do Fraque Verde", "starts_at": starts_at}]
    fake_op = _FakeOp(unbackfilled_rows=rows)
    m.op = fake_op
    m.upgrade()

    expected_key = compute_source_event_key("O Homem do Fraque Verde", starts_at)
    assert fake_op.bind.backfilled_keys.get("evt_1") == expected_key


def test_upgrade_is_a_noop_backfill_when_no_rows_are_unbackfilled():
    m = _load()
    fake_op = _FakeOp(unbackfilled_rows=[])
    m.op = fake_op
    m.upgrade()
    updates = [s for s in fake_op.bind.executed if "UPDATE events.event SET source_event_key" in s]
    assert updates == []


# ── downgrade refusal ─────────────────────────────────────────────────────────
def test_downgrade_refuses_when_a_post_holds_more_than_one_event():
    m = _load()
    fake_op = _FakeOp(multi_event_count=1)
    m.op = fake_op
    with pytest.raises(m.MultiEventDowngradeRefused):
        m.downgrade()
    # The refusal must happen BEFORE any destructive statement runs.
    assert not any("DROP CONSTRAINT IF EXISTS uq_event_source_key" in s for s in fake_op.bind.executed)
    assert not any("DROP COLUMN IF EXISTS source_event_key" in s for s in fake_op.bind.executed)


def test_downgrade_proceeds_when_every_post_holds_at_most_one_event():
    m = _load()
    fake_op = _FakeOp(multi_event_count=0)
    m.op = fake_op
    m.downgrade()  # must not raise
    executed = fake_op.bind.executed
    assert any("DROP CONSTRAINT IF EXISTS uq_event_source_key" in s for s in executed)
    assert any("uq_event_source UNIQUE (source_handle, source_shortcode)" in s for s in executed)
    assert any("DROP COLUMN IF EXISTS source_event_key" in s for s in executed)
