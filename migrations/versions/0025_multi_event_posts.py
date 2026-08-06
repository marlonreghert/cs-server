"""Let one post hold several events: content-derived identity, replacing
the two-column idempotency constraint.

See plans/260806_multi-event-posts.md. `0023_event_table` declared
`CONSTRAINT uq_event_source UNIQUE (source_handle, source_shortcode)` — one
event per post. That constraint IS the idempotency guarantee (a
re-extraction updates its row in place instead of duplicating it), and a
city-listings account whose caption announces several events at several
venues violates it on purpose the moment this plan ships. This migration
changes that contract, deliberately.

The row key CANNOT be positional. `source_event_index` (added here, for
DISPLAY ORDER ONLY) is not part of the new constraint: the model does not
guarantee list order between runs, so keying on "event #2" would silently
migrate an operator's confirmation onto a DIFFERENT event the moment the
model's ordering changed. `source_event_key` — a stable hash over
normalised title + resolved date, computed by
`app.services.event_identity.compute_source_event_key` — is CONTENT-derived
instead.

## Statement order is load-bearing

1. ADD the two new nullable columns (no default, no constraint yet — every
   existing row's `source_event_key` starts NULL).
2. BACK-FILL `source_event_key` for every existing row, calling the SAME
   Python function (`compute_source_event_key`) the runtime extraction path
   uses — not a SQL re-implementation that could drift from it. Getting this
   wrong is not a warning-level bug: a wrong backfill key means the very
   next re-extraction of that post fails to find its own row and inserts a
   SECOND one, silently duplicating every previously-extracted event in the
   system the first time this feature's write path runs against old data.
3. ONLY THEN drop the old two-column constraint and add the new
   three-column one. Adding the new constraint before the backfill completes
   would fail immediately — every existing row's `source_event_key` is NULL
   at that point, and while Postgres itself does not reject multiple NULLs
   under a UNIQUE constraint, doing this out of order would mean the new
   constraint goes live while rows that SHOULD already carry a real key
   still read as NULL, silently disabling the very idempotency the
   constraint exists to provide for every pre-existing row.

## Downgrade refuses to run over live multi-event data

The downgrade counts posts holding more than one event BEFORE touching
anything. Restoring the old two-column constraint over such a post would
have to delete (or arbitrarily pick one of) its rows to satisfy the
constraint — collapsing distinct events is not a schema rollback, it is data
loss, so the downgrade raises instead of running.

Revision ID: 0025_multi_event_posts
Revises: 0024_promoter_accounts
"""
from alembic import op
import sqlalchemy as sa

from app.services.event_identity import compute_source_event_key

revision = "0025_multi_event_posts"
down_revision = "0024_promoter_accounts"
branch_labels = None
depends_on = None


_ADD_COLUMNS = r"""
ALTER TABLE events.event
  ADD COLUMN IF NOT EXISTS source_event_key text,
  ADD COLUMN IF NOT EXISTS source_event_index integer;
"""

_SELECT_UNBACKFILLED = (
    "SELECT event_id, title, starts_at FROM events.event "
    "WHERE source_event_key IS NULL"
)

_BACKFILL_ONE = "UPDATE events.event SET source_event_key=:key, source_event_index=1 " \
                "WHERE event_id=:event_id"

_DROP_OLD_CONSTRAINT = r"""
ALTER TABLE events.event DROP CONSTRAINT IF EXISTS uq_event_source;
"""

_ADD_NEW_CONSTRAINT = r"""
ALTER TABLE events.event
  ADD CONSTRAINT uq_event_source_key UNIQUE (source_handle, source_shortcode, source_event_key);
"""

# Any (source_handle, source_shortcode) with more than one row means at
# least one post already holds several events — exactly what the downgrade
# must refuse to collapse.
_COUNT_MULTI_EVENT_POSTS = r"""
SELECT count(*) FROM (
  SELECT source_handle, source_shortcode
  FROM events.event
  GROUP BY source_handle, source_shortcode
  HAVING count(*) > 1
) AS multi_event_posts
"""

_DROP_NEW_CONSTRAINT = r"""
ALTER TABLE events.event DROP CONSTRAINT IF EXISTS uq_event_source_key;
"""

_RESTORE_OLD_CONSTRAINT = r"""
ALTER TABLE events.event
  ADD CONSTRAINT uq_event_source UNIQUE (source_handle, source_shortcode);
"""

_DROP_COLUMNS = r"""
ALTER TABLE events.event
  DROP COLUMN IF EXISTS source_event_index,
  DROP COLUMN IF EXISTS source_event_key;
"""


class MultiEventDowngradeRefused(RuntimeError):
    """Raised when the downgrade would have to collapse distinct events to
    restore the old two-column constraint — a rollback must never silently
    destroy rows."""


def upgrade() -> None:
    # 1. Columns first, both nullable, no constraint yet.
    op.execute(_ADD_COLUMNS)

    # 2. Back-fill EVERY existing row via the SAME function the runtime
    # uses, before the constraint exists to enforce anything — this is the
    # step a flat ADD CONSTRAINT-first ordering would skip straight past.
    bind = op.get_bind()
    rows = bind.execute(sa.text(_SELECT_UNBACKFILLED)).mappings().all()
    for row in rows:
        key = compute_source_event_key(row["title"], row["starts_at"])
        bind.execute(sa.text(_BACKFILL_ONE), {"key": key, "event_id": row["event_id"]})

    # 3. Only now replace the constraint — every row already has a real key.
    op.execute(_DROP_OLD_CONSTRAINT)
    op.execute(_ADD_NEW_CONSTRAINT)


def downgrade() -> None:
    bind = op.get_bind()
    multi_event_posts = bind.execute(sa.text(_COUNT_MULTI_EVENT_POSTS)).scalar() or 0
    if multi_event_posts:
        raise MultiEventDowngradeRefused(
            f"refusing to downgrade 0025_multi_event_posts: {multi_event_posts} "
            "post(s) hold more than one event; restoring the old "
            "UNIQUE (source_handle, source_shortcode) constraint would require "
            "collapsing them, silently destroying rows"
        )
    op.execute(_DROP_NEW_CONSTRAINT)
    op.execute(_RESTORE_OLD_CONSTRAINT)
    op.execute(_DROP_COLUMNS)
