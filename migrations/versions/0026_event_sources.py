"""Recognise that several posts can announce ONE event: a child
`events.event_source` table, a one-time historical collapse of existing
duplicates, and the retirement of the single-source columns on
`events.event`.

See plans/260807_one-event-many-posts.md. Three Instagram posts ran a
countdown for one 8 August event at Club Metrópole (`Dbs1FdsEWr7` "NOITE DA
PATROA • 08/AGO", `DbtSQngKcPm` "Faltam 2 dias...", `DbvhZJqkUyf` "É
AMANHÃ!") and produced THREE `events.event` rows, because `UNIQUE
(source_handle, source_shortcode, source_event_key)` (migration 0025) is
scoped PER POST, deliberately — it exists to make re-extracting the SAME
post idempotent, and does that correctly. Nothing was ever built to notice
that several posts describe one real-world night.

## Statement order is load-bearing

1. CREATE `events.event_source` — the child table, empty.
2. BACK-FILL one source row per EXISTING event from its current (soon to be
   dropped) single-source columns. This MUST precede step 3: step 3 groups
   and folds events by reading `events.event_source`, and step 4 drops the
   columns step 2 reads FROM — either one running early loses data
   permanently (a pre-existing event that never gets a source row becomes an
   orphaned merge target forever, indistinguishable from one that was never
   extracted).
3. COLLAPSE duplicates: group backfilled events by `(venue_id,
   starts_at::date, normalize_title(title))` where BOTH venue_id and
   starts_at are non-null (see `app.services.event_merge.
   compute_event_identity` — imported, not re-implemented, the same
   discipline this migration's own sibling 0025 established for
   `compute_source_event_key`). Per group: `choose_canonical` picks the sole
   confirmed/manually-linked event if there is exactly one, else the oldest
   `event_id`; a group holding MORE than one such protected event is left
   entirely alone (an operator confirmed two rows — only they can say they
   are one). Every other group member's sources are re-pointed at the
   canonical event (`app.services.event_merge.merge_event_fields` folds its
   fields in first), its ranked venue-link candidates are cleared (ephemeral
   scoring evidence, recomputed on the next crawl — never provenance), and
   the now-sourceless duplicate event row is DELETED. This step is
   DESTRUCTIVE and runs inside this migration's own transaction.
4. DROP the old `uq_event_source_key` constraint (its three columns just
   left the table it constrained) and the ten single-source columns from
   `events.event`. Only now, because step 2 needed them to exist and step 3
   needed the sources they were copied into to already be attached.

## The new constraint needs its OWN name

The idempotency guarantee moves to `events.event_source`, but its Postgres
constraint name cannot reuse `uq_event_source_key`: that identifier is
`events.event`'s CURRENT constraint (added by 0025) until step 4 drops it,
and a UNIQUE constraint's backing index is named per-SCHEMA, not per-table —
both tables live in `events`. Creating the child table's constraint under the
same name before step 4 runs would collide with the still-live original
(`DuplicateTable` on the index Postgres creates for it), so the child table's
copy is named `uq_event_source_post` instead. Renaming, not reordering: the
alternative (dropping 0025's constraint in step 1, before the columns it
covers are backfilled into the child table) would leave `events.event`
unprotected mid-migration for no benefit — see step 2's own ordering
requirement above.

## Downgrade refuses to run once a real merge happened

The downgrade counts events carrying MORE than one source BEFORE touching
anything — exactly the shape 0025's downgrade already takes for a post
holding more than one event. Restoring the single-source columns on
`events.event` for an event with several sources would have to pick (or
concatenate) one, which is not a schema rollback, it is data loss disguised
as one, so the downgrade raises instead. This is very likely to trigger
immediately after this migration's own upgrade: the historical collapse in
step 3 is exactly what produces the first multi-source events, the same way
0025's very first backfill could already have posts holding several events.

Revision ID: 0026_event_sources
Revises: 0025_multi_event_posts
"""
import json

from alembic import op
import sqlalchemy as sa

from app.services.event_merge import choose_canonical, compute_event_identity, merge_event_fields
from app.services.pipeline_run_registry import new_run_id

revision = "0026_event_sources"
down_revision = "0025_multi_event_posts"
branch_labels = None
depends_on = None


_CREATE_EVENT_SOURCE = r"""
CREATE TABLE IF NOT EXISTS events.event_source (
  id                  text PRIMARY KEY,
  event_id            text NOT NULL REFERENCES events.event(event_id),
  source_kind         text NOT NULL DEFAULT 'venue_post',
  source_handle       text NOT NULL,
  source_shortcode    text NOT NULL,
  source_permalink    text,
  source_event_key    text,
  source_event_index  integer,
  cover_photo_key     text,
  raw_extraction      jsonb,
  first_seen_at       timestamptz NOT NULL DEFAULT now(),
  last_seen_at        timestamptz NOT NULL DEFAULT now(),
  -- The idempotency guarantee, moved here from events.event's
  -- uq_event_source_key (0025): re-extracting the same post must update its
  -- SOURCE row in place, never insert a duplicate — now scoped to the
  -- attaching post, independent of which event it attaches to. Named
  -- uq_event_source_post, NOT uq_event_source_key: that name is still
  -- events.event's own live constraint until step 4 drops it, and a schema
  -- cannot hold two same-named constraints even on different tables (see
  -- "The new constraint needs its OWN name" above).
  CONSTRAINT uq_event_source_post UNIQUE (source_handle, source_shortcode, source_event_key)
);

CREATE INDEX IF NOT EXISTS ix_event_source_event_id ON events.event_source (event_id);
"""

_SELECT_EVENTS_TO_BACKFILL = (
    "SELECT event_id, source_kind, source_handle, source_shortcode, source_permalink, "
    "source_event_key, source_event_index, cover_photo_key, raw_extraction, "
    "first_seen_at, last_seen_at FROM events.event"
)

_BACKFILL_ONE_SOURCE = (
    "INSERT INTO events.event_source "
    "(id, event_id, source_kind, source_handle, source_shortcode, source_permalink, "
    "source_event_key, source_event_index, cover_photo_key, raw_extraction, "
    "first_seen_at, last_seen_at) "
    "VALUES (:id, :event_id, :source_kind, :source_handle, :source_shortcode, "
    ":source_permalink, :source_event_key, :source_event_index, :cover_photo_key, "
    "CAST(:raw_extraction AS jsonb), :first_seen_at, :last_seen_at)"
)

# Read back AFTER the backfill (step 3 groups on this) — venue_id/starts_at/
# title drive identity, the rest are exactly the scalars
# app.services.event_merge.merge_event_fields folds, plus last_seen_at for
# its "more recently seen wins a disagreement" rule. A JOIN, not a second
# events.event scan: immediately post-backfill every event has EXACTLY one
# source, so this is still one row per event.
_SELECT_EVENTS_FOR_COLLAPSE = (
    "SELECT e.event_id, e.venue_id, e.starts_at, e.title, e.status, "
    "e.location_resolution, e.review_reason, e.ends_at, e.is_recurring, "
    "e.recurrence_text, e.description, e.lineup, e.ticket_url, e.price_text, "
    "e.location_text, e.confidence, es.last_seen_at "
    "FROM events.event e JOIN events.event_source es ON es.event_id = e.event_id"
)

_REATTACH_SOURCES = (
    "UPDATE events.event_source SET event_id=:to_id WHERE event_id=:from_id"
)

_CLEAR_LINK_CANDIDATES = (
    "DELETE FROM events.event_venue_link_candidate WHERE event_id=:event_id"
)

_DELETE_DUPLICATE_EVENT = "DELETE FROM events.event WHERE event_id=:event_id"

_DROP_OLD_CONSTRAINT = r"""
ALTER TABLE events.event DROP CONSTRAINT IF EXISTS uq_event_source_key;
"""

_DROP_SINGLE_SOURCE_COLUMNS = r"""
ALTER TABLE events.event
  DROP COLUMN IF EXISTS source_kind,
  DROP COLUMN IF EXISTS source_handle,
  DROP COLUMN IF EXISTS source_shortcode,
  DROP COLUMN IF EXISTS source_permalink,
  DROP COLUMN IF EXISTS cover_photo_key,
  DROP COLUMN IF EXISTS raw_extraction,
  DROP COLUMN IF EXISTS first_seen_at,
  DROP COLUMN IF EXISTS last_seen_at,
  DROP COLUMN IF EXISTS source_event_key,
  DROP COLUMN IF EXISTS source_event_index;
"""

# Any event_id with more than one event_source row means a real merge
# happened (this migration's own step 3, or a runtime merge since) — exactly
# what the downgrade must refuse to collapse back onto a single-source row.
_COUNT_MULTI_SOURCE_EVENTS = r"""
SELECT count(*) FROM (
  SELECT event_id FROM events.event_source GROUP BY event_id HAVING count(*) > 1
) AS multi_source_events
"""

_ADD_BACK_COLUMNS = r"""
ALTER TABLE events.event
  ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'venue_post',
  ADD COLUMN IF NOT EXISTS source_handle text,
  ADD COLUMN IF NOT EXISTS source_shortcode text,
  ADD COLUMN IF NOT EXISTS source_permalink text,
  ADD COLUMN IF NOT EXISTS cover_photo_key text,
  ADD COLUMN IF NOT EXISTS raw_extraction jsonb,
  ADD COLUMN IF NOT EXISTS first_seen_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_seen_at timestamptz,
  ADD COLUMN IF NOT EXISTS source_event_key text,
  ADD COLUMN IF NOT EXISTS source_event_index integer;
"""

# Every event has EXACTLY one source at this point (the refusal above already
# guarded that), so this is a plain 1:1 restore, not a fold.
_SELECT_SOURCES_FOR_DOWNGRADE = (
    "SELECT event_id, source_kind, source_handle, source_shortcode, source_permalink, "
    "cover_photo_key, raw_extraction, first_seen_at, last_seen_at, source_event_key, "
    "source_event_index FROM events.event_source"
)

_RESTORE_ONE_EVENT = (
    "UPDATE events.event SET source_kind=:source_kind, source_handle=:source_handle, "
    "source_shortcode=:source_shortcode, source_permalink=:source_permalink, "
    "cover_photo_key=:cover_photo_key, raw_extraction=CAST(:raw_extraction AS jsonb), "
    "first_seen_at=:first_seen_at, last_seen_at=:last_seen_at, "
    "source_event_key=:source_event_key, source_event_index=:source_event_index "
    "WHERE event_id=:event_id"
)

_RESTORE_NOT_NULL = r"""
ALTER TABLE events.event
  ALTER COLUMN source_handle SET NOT NULL,
  ALTER COLUMN source_shortcode SET NOT NULL,
  ALTER COLUMN first_seen_at SET NOT NULL,
  ALTER COLUMN last_seen_at SET NOT NULL;
"""

# Restores 0025's ORIGINAL name on events.event — the child table's own
# uq_event_source_post (still live at this point; DROP_EVENT_SOURCE_TABLE
# runs after this) is a different identifier, so the two never collide.
_ADD_OLD_CONSTRAINT = r"""
ALTER TABLE events.event
  ADD CONSTRAINT uq_event_source_key UNIQUE (source_handle, source_shortcode, source_event_key);
"""

_DROP_EVENT_SOURCE_TABLE = r"""
DROP INDEX IF EXISTS events.ix_event_source_event_id;
DROP TABLE IF EXISTS events.event_source;
"""


class EventSourcesDowngradeRefused(RuntimeError):
    """Raised when the downgrade would have to collapse a real event_source
    merge to restore the single-source columns on events.event — a rollback
    must never silently destroy provenance."""


def _new_source_id() -> str:
    return f"evsrc_{new_run_id()}"


def _jsonb(value):
    """psycopg (v3) has no default adapter for a bare Python dict/list bound
    against a `CAST(:param AS jsonb)` placeholder — SELECTing a jsonb column
    back through `.mappings()` decodes it INTO a dict/list automatically, but
    writing it back out requires re-serializing first (the same thing
    app.dao.rds_venue_store.RdsVenueStore's insert_event/update_event already
    do for every jsonb column, for the identical reason). Every jsonb value
    this migration writes — the backfilled `raw_extraction`, the collapse's
    merged `lineup`, the downgrade's restored `raw_extraction` — routes
    through this."""
    return json.dumps(value) if value is not None else None


def upgrade() -> None:
    # 1. The child table, empty, constraint and all.
    op.execute(_CREATE_EVENT_SOURCE)

    # 2. Back-fill one source row per EXISTING event from its current
    # (about-to-be-dropped) single-source columns — MUST precede step 3,
    # which reads events.event_source, and step 4, which drops the columns
    # this step reads FROM.
    bind = op.get_bind()
    existing_events = bind.execute(sa.text(_SELECT_EVENTS_TO_BACKFILL)).mappings().all()
    for row in existing_events:
        bind.execute(sa.text(_BACKFILL_ONE_SOURCE), {
            "id": _new_source_id(), "event_id": row["event_id"],
            "source_kind": row["source_kind"], "source_handle": row["source_handle"],
            "source_shortcode": row["source_shortcode"],
            "source_permalink": row["source_permalink"],
            "source_event_key": row["source_event_key"],
            "source_event_index": row["source_event_index"],
            "cover_photo_key": row["cover_photo_key"],
            "raw_extraction": _jsonb(row["raw_extraction"]),
            "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"],
        })

    # 3. Collapse historical duplicates — several posts that, before this
    # feature existed, could only ever produce separate events. Destructive;
    # runs inside this migration's own transaction.
    events = bind.execute(sa.text(_SELECT_EVENTS_FOR_COLLAPSE)).mappings().all()
    groups: dict = {}
    for row in events:
        identity = compute_event_identity(row.get("venue_id"), row.get("starts_at"), row.get("title"))
        if identity is None:
            continue  # no venue or no date — never a merge candidate
        groups.setdefault(identity, []).append(dict(row))

    for members in groups.values():
        if len(members) < 2:
            continue
        canonical = choose_canonical(members)
        if canonical is None:
            continue  # more than one confirmed/manually-linked event — leave alone

        canonical_id = canonical["event_id"]
        for other in members:
            if other["event_id"] == canonical_id:
                continue
            changed_fields, review_reason = merge_event_fields(canonical, other)
            update_fields = dict(changed_fields)
            if review_reason != canonical.get("review_reason"):
                update_fields["review_reason"] = review_reason
            if update_fields:
                set_clauses = []
                params = {"event_id": canonical_id}
                for field, value in update_fields.items():
                    if field == "lineup":
                        set_clauses.append("lineup=CAST(:lineup AS jsonb)")
                        value = _jsonb(value)
                    else:
                        set_clauses.append(f"{field}=:{field}")
                    params[field] = value
                bind.execute(
                    sa.text(f"UPDATE events.event SET {', '.join(set_clauses)} WHERE event_id=:event_id"),
                    params,
                )
                canonical.update(update_fields)

            bind.execute(sa.text(_REATTACH_SOURCES), {
                "to_id": canonical_id, "from_id": other["event_id"],
            })
            bind.execute(sa.text(_CLEAR_LINK_CANDIDATES), {"event_id": other["event_id"]})
            bind.execute(sa.text(_DELETE_DUPLICATE_EVENT), {"event_id": other["event_id"]})

    # 4. Only now: the constraint and columns these first three steps relied
    # on having populated elsewhere.
    op.execute(_DROP_OLD_CONSTRAINT)
    op.execute(_DROP_SINGLE_SOURCE_COLUMNS)


def downgrade() -> None:
    bind = op.get_bind()
    multi_source_events = bind.execute(sa.text(_COUNT_MULTI_SOURCE_EVENTS)).scalar() or 0
    if multi_source_events:
        raise EventSourcesDowngradeRefused(
            f"refusing to downgrade 0026_event_sources: {multi_source_events} "
            "event(s) carry more than one source; restoring the single-source "
            "columns on events.event would require collapsing them, silently "
            "destroying provenance"
        )

    op.execute(_ADD_BACK_COLUMNS)
    bind = op.get_bind()
    rows = bind.execute(sa.text(_SELECT_SOURCES_FOR_DOWNGRADE)).mappings().all()
    for row in rows:
        params = dict(row)
        params["raw_extraction"] = _jsonb(params["raw_extraction"])
        bind.execute(sa.text(_RESTORE_ONE_EVENT), params)
    op.execute(_RESTORE_NOT_NULL)
    op.execute(_ADD_OLD_CONSTRAINT)
    op.execute(_DROP_EVENT_SOURCE_TABLE)
