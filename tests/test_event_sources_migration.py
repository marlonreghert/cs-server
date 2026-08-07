"""Guards for the one-event-many-posts migration (0026_event_sources).

No local Postgres in CI (see tests/README.md), so this pins the revision
chain and the load-bearing SQL fragments by inspection — the same offline-
guard shape as tests/test_multi_event_posts_migration.py (0025).

Three things get their own tests beyond that established shape:
  - the back-fill (step 2) must run BEFORE the collapse (step 3), which must
    run BEFORE the constraint/column drop (step 4) — proven by DRIVING the
    real upgrade() against a fake bind and asserting true statement order,
    not just that all three kinds of statement exist somewhere;
  - the collapse must actually fold a countdown campaign into one canonical
    event (every non-canonical source reattached, every now-sourceless
    duplicate deleted, fields merged) and must NEVER touch a group holding
    more than one confirmed/manually-linked event, or a "group" whose
    members lack a venue or a date;
  - downgrade() must REFUSE when any event carries more than one source.
"""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "0026_event_sources.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0026_event_sources", _PATH)
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
    execution order, answers the migration's read queries from caller-
    programmed fixtures, and files every WRITE into a purpose-built bucket so
    a test can assert exactly what the collapse did — not just that some
    statement ran."""

    def __init__(self, unbackfilled_rows=None, collapse_rows=None,
                 multi_source_count=0, downgrade_rows=None):
        self.executed: list[str] = []
        self._unbackfilled_rows = unbackfilled_rows or []
        self._collapse_rows = (
            collapse_rows if collapse_rows is not None else list(self._unbackfilled_rows)
        )
        self._multi_source_count = multi_source_count
        self._downgrade_rows = downgrade_rows or []

        self.backfilled_sources: list[dict] = []
        self.canonical_updates: list[dict] = []
        self.reattachments: list[tuple] = []
        self.cleared_candidates: list[str] = []
        self.deleted_events: list[str] = []
        self.restored_events: list[dict] = []

    def execute(self, clause, params=None):
        sql = str(clause)
        self.executed.append(sql)
        params = dict(params or {})

        if sql.endswith("FROM events.event"):
            return _FakeResult(self._unbackfilled_rows)
        if "INSERT INTO events.event_source" in sql:
            self.backfilled_sources.append(params)
            return _FakeResult([])
        if "JOIN events.event_source es ON" in sql:
            return _FakeResult(self._collapse_rows)
        if "UPDATE events.event_source SET event_id=" in sql:
            self.reattachments.append((params.get("from_id"), params.get("to_id")))
            return _FakeResult([])
        if "DELETE FROM events.event_venue_link_candidate" in sql:
            self.cleared_candidates.append(params.get("event_id"))
            return _FakeResult([])
        if sql.startswith("DELETE FROM events.event WHERE"):
            self.deleted_events.append(params.get("event_id"))
            return _FakeResult([])
        if sql.startswith("UPDATE events.event SET") and "source_kind=" not in sql:
            self.canonical_updates.append(params)
            return _FakeResult([])
        if "multi_source_events" in sql:
            return _FakeResult([(self._multi_source_count,)])
        if sql.startswith("SELECT") and "FROM events.event_source" in sql:
            return _FakeResult(self._downgrade_rows)
        if sql.startswith("UPDATE events.event SET") and "source_kind=" in sql:
            self.restored_events.append(params)
            return _FakeResult([])
        return _FakeResult([])


class _FakeOp:
    """`op.execute(str)` and `op.get_bind()` share ONE recorder, so a test
    can assert the true, single, chronological order of every statement the
    migration issues regardless of which API produced it."""

    def __init__(self, **kwargs):
        self.bind = _FakeBind(**kwargs)

    def execute(self, sql):
        self.bind.execute(sql)

    def get_bind(self):
        return self.bind


def _row(event_id, *, venue_id="v1", title="Noite da Patroa", starts_at=None,
         status="pending_review", location_resolution=None, review_reason=None,
         last_seen_at=None, **overrides) -> dict:
    base = {
        "event_id": event_id, "venue_id": venue_id, "title": title,
        "starts_at": starts_at, "status": status,
        "location_resolution": location_resolution, "review_reason": review_reason,
        "ends_at": None, "is_recurring": False, "recurrence_text": None,
        "description": None, "lineup": [], "ticket_url": None, "price_text": None,
        "location_text": None, "confidence": None,
        "last_seen_at": last_seen_at,
        # single-source columns the backfill (step 2) reads — irrelevant to
        # the collapse select but harmless to carry along in one fixture.
        "source_kind": "venue_post", "source_handle": "h", "source_shortcode": event_id,
        "source_permalink": None, "source_event_key": f"key_{event_id}",
        "source_event_index": 1, "cover_photo_key": None, "raw_extraction": {},
        "first_seen_at": last_seen_at,
    }
    base.update(overrides)
    return base


def test_revision_chain():
    m = _load()
    assert m.revision == "0026_event_sources"
    assert m.down_revision == "0025_multi_event_posts"
    assert len(m.revision) <= 32


def test_creates_the_child_table_with_the_composite_unique_constraint():
    m = _load()
    assert "CREATE TABLE IF NOT EXISTS events.event_source" in m._CREATE_EVENT_SOURCE
    assert (
        "CONSTRAINT uq_event_source_post UNIQUE (source_handle, source_shortcode, source_event_key)"
        in m._CREATE_EVENT_SOURCE
    )
    assert "REFERENCES events.event(event_id)" in m._CREATE_EVENT_SOURCE


def test_the_new_constraint_never_collides_with_events_events_own_name():
    """Regression guard for a real CI failure (psycopg.errors.DuplicateTable:
    relation "uq_event_source_key" already exists): a UNIQUE constraint's
    backing index is named per-SCHEMA in Postgres, not per-table, and
    events.event and events.event_source share the `events` schema.
    `events.event`'s OWN uq_event_source_key (added by 0025) is still live
    when step 1 runs — only step 4 drops it — so the child table's copy MUST
    use a different identifier. This asserts the two names are literally
    different strings, not just that each individually looks right."""
    m = _load()
    # The CONSTRAINT clause itself, not just a comment mentioning the old
    # name for context (this migration's own docstring/comments legitimately
    # reference "uq_event_source_key" when explaining the rename).
    assert "CONSTRAINT uq_event_source_post UNIQUE" in m._CREATE_EVENT_SOURCE
    assert "CONSTRAINT uq_event_source_key" not in m._CREATE_EVENT_SOURCE
    assert "uq_event_source_key" in m._DROP_OLD_CONSTRAINT
    assert "uq_event_source_key" in m._ADD_OLD_CONSTRAINT


def test_drops_the_old_constraint_and_every_single_source_column():
    m = _load()
    assert "DROP CONSTRAINT IF EXISTS uq_event_source_key" in m._DROP_OLD_CONSTRAINT
    for col in (
        "source_kind", "source_handle", "source_shortcode", "source_permalink",
        "cover_photo_key", "raw_extraction", "first_seen_at", "last_seen_at",
        "source_event_key", "source_event_index",
    ):
        assert f"DROP COLUMN IF EXISTS {col}" in m._DROP_SINGLE_SOURCE_COLUMNS


# ── the ordering trap: backfill -> collapse -> drop constraint/columns ──────
def test_upgrade_backfills_before_collapsing_and_before_dropping_columns():
    """Drives the REAL upgrade() against a fake bind carrying two unrelated,
    unmergeable events (different venues), and asserts the true statement
    order: the table is created, THEN every row is backfilled into
    events.event_source, THEN the collapse SELECT runs, THEN — only at the
    very end — the old constraint and the single-source columns are
    dropped. Getting this wrong the way 0025 warns against (constraint/
    columns dropped before the backfill, or the collapse reading
    events.event_source before it is populated) loses provenance forever."""
    m = _load()
    rows = [
        _row("evt_1", venue_id="v1", starts_at=datetime(2026, 8, 10, tzinfo=timezone.utc)),
        _row("evt_2", venue_id="v2", starts_at=datetime(2026, 8, 11, tzinfo=timezone.utc)),
    ]
    fake_op = _FakeOp(unbackfilled_rows=rows)
    m.op = fake_op
    m.upgrade()

    executed = fake_op.bind.executed
    create_idx = next(
        i for i, s in enumerate(executed)
        if "CREATE TABLE IF NOT EXISTS events.event_source" in s
    )
    backfill_indices = [i for i, s in enumerate(executed) if "INSERT INTO events.event_source" in s]
    collapse_select_idx = next(i for i, s in enumerate(executed) if "JOIN events.event_source es ON" in s)
    drop_constraint_idx = next(
        i for i, s in enumerate(executed) if "DROP CONSTRAINT IF EXISTS uq_event_source_key" in s
    )
    drop_columns_idx = next(i for i, s in enumerate(executed) if "DROP COLUMN IF EXISTS source_kind" in s)

    assert len(backfill_indices) == 2, executed
    assert create_idx < min(backfill_indices), executed
    assert max(backfill_indices) < collapse_select_idx, executed
    assert collapse_select_idx < drop_constraint_idx, executed
    assert drop_constraint_idx < drop_columns_idx, executed


def test_upgrade_backfills_a_source_row_from_each_events_own_columns():
    m = _load()
    rows = [_row(
        "evt_1", source_permalink="https://ig/p/abc", cover_photo_key="k1.jpg",
        raw_extraction={"title": "Noite da Patroa"},
    )]
    fake_op = _FakeOp(unbackfilled_rows=rows)
    m.op = fake_op
    m.upgrade()

    assert len(fake_op.bind.backfilled_sources) == 1
    backfilled = fake_op.bind.backfilled_sources[0]
    assert backfilled["event_id"] == "evt_1"
    assert backfilled["source_permalink"] == "https://ig/p/abc"
    assert backfilled["cover_photo_key"] == "k1.jpg"
    # Bound as a JSON STRING, not a bare dict: psycopg has no adapter for a
    # raw Python dict against a `CAST(:param AS jsonb)` placeholder — a real
    # Postgres run caught this (`cannot adapt type 'dict'`) where the fake
    # bind alone could not, since it never actually executes SQL.
    assert json.loads(backfilled["raw_extraction"]) == {"title": "Noite da Patroa"}


def test_upgrade_is_a_noop_collapse_when_no_rows_exist():
    m = _load()
    fake_op = _FakeOp(unbackfilled_rows=[])
    m.op = fake_op
    m.upgrade()
    assert fake_op.bind.backfilled_sources == []
    assert fake_op.bind.reattachments == []
    assert fake_op.bind.deleted_events == []


# ── the collapse itself ──────────────────────────────────────────────────────
class TestCollapse:
    def test_three_posts_for_the_same_night_collapse_onto_the_oldest(self):
        """The exact Club Metrópole scenario the plan is about: three
        distinct posts (source_event_key differs per row, mirroring three
        real shortcodes) but ONE real-world night — same venue, same date,
        same normalized title ("NOITE DA PATROA" vs "Noite da Patroa")."""
        m = _load()
        starts_at = datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc)
        rows = [
            _row("evt_a", title="NOITE DA PATROA", starts_at=starts_at,
                 last_seen_at=datetime(2026, 8, 1, tzinfo=timezone.utc)),
            _row("evt_b", title="Noite da Patroa", starts_at=starts_at,
                 last_seen_at=datetime(2026, 8, 6, tzinfo=timezone.utc), lineup=["DJ X"]),
            _row("evt_c", title="Noite da Patroa", starts_at=starts_at,
                 last_seen_at=datetime(2026, 8, 8, tzinfo=timezone.utc), lineup=["DJ Y"]),
        ]
        fake_op = _FakeOp(unbackfilled_rows=rows)
        m.op = fake_op
        m.upgrade()

        # evt_a is the oldest event_id -> canonical; evt_b and evt_c fold into it.
        assert set(fake_op.bind.deleted_events) == {"evt_b", "evt_c"}
        assert ("evt_b", "evt_a") in fake_op.bind.reattachments
        assert ("evt_c", "evt_a") in fake_op.bind.reattachments
        assert set(fake_op.bind.cleared_candidates) == {"evt_b", "evt_c"}

        # The lineup unions across all three (canonical started empty). Bound
        # as a JSON STRING (see test_upgrade_backfills_a_source_row_from_
        # each_events_own_columns's comment on why).
        merged_lineup = None
        for update in fake_op.bind.canonical_updates:
            if "lineup" in update:
                merged_lineup = json.loads(update["lineup"])
        assert merged_lineup is not None, fake_op.bind.canonical_updates
        assert set(merged_lineup) == {"DJ X", "DJ Y"}

    def test_a_group_with_two_confirmed_events_is_left_alone(self):
        m = _load()
        starts_at = datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc)
        rows = [
            _row("evt_a", starts_at=starts_at, status="confirmed"),
            _row("evt_b", starts_at=starts_at, status="confirmed"),
        ]
        fake_op = _FakeOp(unbackfilled_rows=rows)
        m.op = fake_op
        m.upgrade()

        assert fake_op.bind.deleted_events == []
        assert fake_op.bind.reattachments == []
        assert fake_op.bind.canonical_updates == []

    def test_a_confirmed_and_a_manually_linked_event_together_are_also_left_alone(self):
        m = _load()
        starts_at = datetime(2026, 8, 8, tzinfo=timezone.utc)
        rows = [
            _row("evt_a", starts_at=starts_at, status="confirmed"),
            _row("evt_b", starts_at=starts_at, location_resolution="manual"),
        ]
        fake_op = _FakeOp(unbackfilled_rows=rows)
        m.op = fake_op
        m.upgrade()
        assert fake_op.bind.deleted_events == []

    def test_a_confirmed_events_fields_are_never_recomputed(self):
        """The confirmed row's title is the SAME normalized identity as the
        duplicate's (that is WHY they group at all — see the plan's honest
        limitation: a materially different title never merges, confirmed or
        not); the operator's correction here is `description`, which the
        duplicate disagrees with — proving the confirmed row's OWN fields
        never move while the disagreement is still flagged."""
        m = _load()
        starts_at = datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc)
        rows = [
            _row("evt_a", title="Noite da Patroa", description="Operator's own description",
                 starts_at=starts_at, status="confirmed",
                 last_seen_at=datetime(2026, 8, 1, tzinfo=timezone.utc)),
            _row("evt_b", title="NOITE DA PATROA", description="Something else entirely",
                 starts_at=starts_at, last_seen_at=datetime(2026, 8, 8, tzinfo=timezone.utc)),
        ]
        fake_op = _FakeOp(unbackfilled_rows=rows)
        m.op = fake_op
        m.upgrade()

        assert fake_op.bind.deleted_events == ["evt_b"]
        assert ("evt_b", "evt_a") in fake_op.bind.reattachments
        # The confirmed row's own fields are NEVER part of the update
        # payload — only review_reason (the divergence flag) may be.
        for update in fake_op.bind.canonical_updates:
            assert "description" not in update
            assert "title" not in update
        assert any(
            update.get("review_reason") == "model_diverges_from_confirmed_record"
            for update in fake_op.bind.canonical_updates
        )

    def test_events_missing_a_venue_are_never_grouped(self):
        m = _load()
        starts_at = datetime(2026, 8, 8, tzinfo=timezone.utc)
        rows = [
            _row("evt_a", venue_id=None, starts_at=starts_at),
            _row("evt_b", venue_id=None, starts_at=starts_at),
        ]
        fake_op = _FakeOp(unbackfilled_rows=rows)
        m.op = fake_op
        m.upgrade()
        assert fake_op.bind.deleted_events == []

    def test_events_missing_a_date_are_never_grouped(self):
        m = _load()
        rows = [
            _row("evt_a", venue_id="v1", starts_at=None),
            _row("evt_b", venue_id="v1", starts_at=None),
        ]
        fake_op = _FakeOp(unbackfilled_rows=rows)
        m.op = fake_op
        m.upgrade()
        assert fake_op.bind.deleted_events == []

    def test_a_single_event_group_is_never_touched(self):
        m = _load()
        rows = [_row("evt_a", starts_at=datetime(2026, 8, 8, tzinfo=timezone.utc))]
        fake_op = _FakeOp(unbackfilled_rows=rows)
        m.op = fake_op
        m.upgrade()
        assert fake_op.bind.deleted_events == []
        assert fake_op.bind.reattachments == []


# ── downgrade refusal ─────────────────────────────────────────────────────────
def test_downgrade_refuses_when_any_event_has_more_than_one_source():
    m = _load()
    fake_op = _FakeOp(multi_source_count=1)
    m.op = fake_op
    with pytest.raises(m.EventSourcesDowngradeRefused):
        m.downgrade()
    # The refusal must happen BEFORE any destructive/reconstructive statement runs.
    assert not any("DROP TABLE IF EXISTS events.event_source" in s for s in fake_op.bind.executed)
    assert not any("ADD COLUMN IF NOT EXISTS source_kind" in s for s in fake_op.bind.executed)


def test_downgrade_proceeds_when_every_event_has_exactly_one_source():
    m = _load()
    downgrade_rows = [{
        "event_id": "evt_1", "source_kind": "venue_post", "source_handle": "h",
        "source_shortcode": "s1", "source_permalink": None, "cover_photo_key": None,
        "raw_extraction": {}, "first_seen_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "last_seen_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "source_event_key": "k1", "source_event_index": 1,
    }]
    fake_op = _FakeOp(multi_source_count=0, downgrade_rows=downgrade_rows)
    m.op = fake_op
    m.downgrade()  # must not raise

    executed = fake_op.bind.executed
    assert any("ADD COLUMN IF NOT EXISTS source_kind" in s for s in executed)
    assert len(fake_op.bind.restored_events) == 1
    assert fake_op.bind.restored_events[0]["event_id"] == "evt_1"
    assert any("uq_event_source_key UNIQUE" in s for s in executed)
    assert any("DROP TABLE IF EXISTS events.event_source" in s for s in executed)
