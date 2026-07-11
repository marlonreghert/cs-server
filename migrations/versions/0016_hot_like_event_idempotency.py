"""engagement.hot_like_event idempotency — unique (user, venue, business day)

PROBLEM: engagement_router.py mandates vibes_bot retry the hot-like write on a
5xx. engagement_service.add_hot_like() commits the RDS insert BEFORE the Redis
projection; if Redis fails after the RDS commit, the retried request re-runs
the RDS insert too. Today's schema (0001_baseline_schemas.py) has no natural
key on engagement.hot_like_event beyond the bigserial `id` —

    id bigserial PRIMARY KEY, user_pseudo text, venue_id text, created_at timestamptz

— so a retry silently persists a second (or third) row for the same tap.
`record_app_session`/engagement.app_session_day (0012) already solved the
identical shape for app-session pings with a PK on (user_pseudo,
activity_date) + ON CONFLICT DO NOTHING; this migration applies the same
"one row per local calendar day" idempotency key to hot-likes.

WHY A NEW COLUMN, NOT AN EXPRESSION INDEX: the natural dedup key is a Recife
CALENDAR DAY, not `created_at` itself (two taps on the same day, however far
apart in wall-clock time within that day, must collapse to one row — this is
what the BDD scenario "in the current business period" tests). Postgres
requires index expressions to be IMMUTABLE; `(created_at AT TIME ZONE
'America/Recife')::date` is only STABLE (it depends on the zone's offset
table), so it cannot back a unique index directly. A real, stored
`business_period date` column — backfilled once from existing rows — is
required.

MIGRATION ORDER (each step depends on the one before it; do not reorder):
  1. ADD COLUMN business_period date (nullable) — a NOT NULL column cannot be
     added directly onto a non-empty table without a backfill first.
  2. BACKFILL business_period for every existing row from created_at, using
     the same America/Recife calendar-day convention as
     app.utils.recife_time.recife_today() / engagement.app_session_day.
  3. SET NOT NULL now that every row has a value.
  4. COLLAPSE DUPLICATES (IRREVERSIBLE) — pre-existing rows that share
     (user_pseudo, venue_id, business_period) are exactly the duplicates this
     migration exists to prevent going forward; the unique index in step 5
     cannot be created while they exist. Keep-first: for each natural-key
     group, delete every row except the one with the MINIMUM `id` (bigserial
     insertion order — the earliest-recorded event for that user/venue/day).
     THIS DELETES ROWS. There is no undo: once collapsed, the discarded
     duplicate hot_like_event rows (and whatever they individually
     contributed to historical trending metrics) are gone. This is the same
     accepted trade-off as the pre-existing app_session_day idempotency
     design — durable per-tap history was never the contract here (comment at
     app/services/engagement_service.py:60: "hot_like_event log is immutable
     history and is intentionally kept", scoped to genuinely distinct events,
     not machine-retried duplicates of the same tap).
  5. CREATE the unique index the new ON CONFLICT DO NOTHING insert relies on.
     Must run AFTER step 4 — Postgres refuses to build a unique index over
     rows that violate it.

DEPLOY ORDER: cs-server has no auto-migrate (deploy does NOT run alembic).
This migration MUST be applied to the RDS instance manually BEFORE the new
application code is deployed, e.g.
`docker exec vibes_bot-cs-server-1 alembic upgrade head`. The new insert
(`INSERT ... ON CONFLICT (user_pseudo, venue_id, business_period) DO NOTHING`)
raises `UndefinedColumn`/`InvalidColumnReference` against the pre-migration
schema — deploying the code first breaks every hot-like write.

DOWNGRADE: drops the unique index AND the business_period column, restoring
the pre-migration table shape (compatible with the pre-migration application
code, which never references business_period). This is a real, schema-level
reversal — the ONLY irreversible part of this migration is the row deletion
in step 4 above.

Revision ID: 0016_hot_like_event_idempotency
Revises: 0015_geofence_city_circles
Create Date: 2026-07-11
"""
from alembic import op

revision = "0016_hot_like_event_idempotency"
down_revision = "0015_geofence_city_circles"
branch_labels = None
depends_on = None

ADD_COLUMN = r"""
ALTER TABLE engagement.hot_like_event ADD COLUMN IF NOT EXISTS business_period date;
"""

BACKFILL = r"""
UPDATE engagement.hot_like_event
   SET business_period = (created_at AT TIME ZONE 'America/Recife')::date
 WHERE business_period IS NULL;
"""

SET_NOT_NULL = r"""
ALTER TABLE engagement.hot_like_event ALTER COLUMN business_period SET NOT NULL;
"""

# IRREVERSIBLE: collapses pre-existing duplicate (user_pseudo, venue_id,
# business_period) rows, keep-first by minimum `id` (bigserial insertion
# order). MUST run before CREATE_UNIQUE_INDEX below.
COLLAPSE_DUPLICATES = r"""
DELETE FROM engagement.hot_like_event a
      USING engagement.hot_like_event b
      WHERE a.user_pseudo = b.user_pseudo
        AND a.venue_id = b.venue_id
        AND a.business_period = b.business_period
        AND a.id > b.id;
"""

CREATE_UNIQUE_INDEX = r"""
CREATE UNIQUE INDEX IF NOT EXISTS ux_hot_like_event_user_venue_period
    ON engagement.hot_like_event (user_pseudo, venue_id, business_period);
"""

DOWNGRADE = r"""
DROP INDEX IF EXISTS engagement.ux_hot_like_event_user_venue_period;
ALTER TABLE engagement.hot_like_event DROP COLUMN IF EXISTS business_period;
"""


def upgrade() -> None:
    op.execute(ADD_COLUMN)
    op.execute(BACKFILL)
    op.execute(SET_NOT_NULL)
    op.execute(COLLAPSE_DUPLICATES)  # irreversible — see module docstring
    op.execute(CREATE_UNIQUE_INDEX)


def downgrade() -> None:
    # Schema-reversible (drops the index + column so pre-migration code can
    # run again). The row deletions COLLAPSE_DUPLICATES performed during
    # upgrade() are NOT restored — that data loss is permanent by design.
    op.execute(DOWNGRADE)
