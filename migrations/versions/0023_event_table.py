"""Create events.event: structured events extracted from archived posts.

See plans/260804_instagram-event-extraction.md. Chains from 0022_events_schema,
which created the `events` schema and `events.venue_event_profile` — this
migration adds a second table to that same schema and touches nothing it
created.

`venue_id` is NULLABLE ON PURPOSE even though every event produced by THIS
plan has one (every post here comes from a venue's own handle). Plan
260804_instagram-promoter-events.md adds events from promoter accounts whose
venue must be inferred and may never resolve — making the column NOT NULL now
would force a migration on a table that already has rows the moment that
plan ships, and nullable-then-populated is the cheaper order.

`UNIQUE (source_handle, source_shortcode)` is the idempotency guarantee for
requirement 7 (re-extracting a post updates its event in place) — a
CONSTRAINT, not a code path that a future change could accidentally bypass.

Additive-only: one new table, one new schema-scoped sequence of nothing (no
sequence — id is an app-generated ULID), two indexes. The downgrade drops only
what this migration created; it must NOT drop the `events` schema itself
(0022's venue_event_profile table still lives there) or venue_event_profile.

Revision ID: 0023_event_table
Revises: 0022_events_schema
"""
from alembic import op

revision = "0023_event_table"
down_revision = "0022_events_schema"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE IF NOT EXISTS events.event (
  event_id         text PRIMARY KEY,
  venue_id         text REFERENCES venues.venue(venue_id),
  source_kind      text NOT NULL DEFAULT 'venue_post',
  source_handle    text NOT NULL,
  source_shortcode text NOT NULL,
  source_permalink text,
  starts_at        timestamptz,
  ends_at          timestamptz,
  is_recurring     boolean NOT NULL DEFAULT false,
  recurrence_text  text,
  title            text,
  description      text,
  lineup           jsonb,
  ticket_url       text,
  price_text       text,
  location_text    text,
  cover_photo_key  text,
  confidence       double precision,
  status           text NOT NULL DEFAULT 'pending_review',
  review_reason    text,
  raw_extraction   jsonb,
  first_seen_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at     timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  -- The idempotency guarantee (requirement 7): re-extracting the same post
  -- must update its event in place, never insert a duplicate.
  CONSTRAINT uq_event_source UNIQUE (source_handle, source_shortcode)
);

CREATE INDEX IF NOT EXISTS ix_event_venue_starts_at
    ON events.event (venue_id, starts_at);

CREATE INDEX IF NOT EXISTS ix_event_status
    ON events.event (status);
"""

DOWNGRADE = r"""
DROP INDEX IF EXISTS events.ix_event_status;
DROP INDEX IF EXISTS events.ix_event_venue_starts_at;
DROP TABLE IF EXISTS events.event;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
