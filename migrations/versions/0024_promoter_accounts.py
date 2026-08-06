"""Promoter accounts, the venue-link candidate audit trail, and event
resolution columns.

See plans/260804_instagram-promoter-events.md. Chains from 0023_event_table,
the current head — this migration adds two new tables to the `events` schema
0022 created and four additive columns onto `events.event`, and touches
nothing else 0022/0023 own.

`events.promoter_account` — the registry. `handle` is the natural key (an
Instagram handle is already globally unique and human-meaningful; a surrogate
id would only add a lookup nobody needs). `status` defaults to `candidate`:
discovery PROPOSES, an operator DISPOSES — a row must never default to
crawlable. `mention_count`/`posts_crawled`/`events_extracted` default to 0 so
a freshly discovered or freshly activated account reads as "nothing has
happened yet" rather than NULL, which the crawl job would otherwise have to
special-case on every increment.

`events.event_venue_link_candidate` — the ranked alternatives behind a
resolution decision, win or lose. Storing the losing candidates (not just the
winner) is what makes the review queue usable: an operator chooses between
named venues with scores and reasons instead of being handed a location
string and a search box. `rank`/`score`/`method`/`evidence` together let a
past decision be explained without re-running the resolver. FK to
`events.event` (cascades with the event's own lifecycle — a candidate row is
meaningless without its event) and to `venues.venue` (every candidate, win or
lose, names a real venue; there is no "candidate for no venue").

`events.event` gains four columns:
  - `location_resolution` (`auto` | `manual` | `unresolved`) — how, if at
    all, this event's venue was decided. NULL for a venue-owned post's event,
    which was never routed through the resolution ladder at all (the plan's
    non-goal boundary: extraction from a venue's own handle already knows its
    venue and never touches this column).
  - `location_confidence` — the winning candidate's score, or NULL when there
    was no scored candidate (a handle mention is certain, not scored).
  - `linked_by` — who/what made the link: an operator's identifier for a
    manual link, or a resolution method name for an automatic one. NULL until
    something links the event.
  - `linked_at` — when the current link was made. Overwritten by a manual
    link and never by a later crawl (a manual link outranks the model, the
    same principle as a confirmed event surviving re-extraction).

All four are nullable with no default: a pre-existing venue-owned event row
must not be silently relabeled by this migration, and an unresolved promoter
event legitimately carries all four as NULL forever.

Additive only: two new tables, two new indexes, four new nullable columns.
The downgrade drops exactly what this migration created — the two new
tables, their indexes, and the four columns — and touches neither the
`events` schema, nor `events.event` itself, nor `events.venue_event_profile`.

Revision ID: 0024_promoter_accounts
Revises: 0023_event_table
"""
from alembic import op

revision = "0024_promoter_accounts"
down_revision = "0023_event_table"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE IF NOT EXISTS events.promoter_account (
  handle                    text PRIMARY KEY,
  display_name              text,
  status                    text NOT NULL DEFAULT 'candidate',
  discovery_source          text NOT NULL DEFAULT 'manual',
  discovered_from_event_id  text,
  mention_count             integer NOT NULL DEFAULT 0,
  notes                     text,
  added_by                  text,
  last_crawled_at           timestamptz,
  posts_crawled             integer NOT NULL DEFAULT 0,
  events_extracted          integer NOT NULL DEFAULT 0,
  created_at                timestamptz NOT NULL DEFAULT now(),
  updated_at                timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_promoter_account_status
    ON events.promoter_account (status);

CREATE TABLE IF NOT EXISTS events.event_venue_link_candidate (
  id         bigserial PRIMARY KEY,
  event_id   text NOT NULL REFERENCES events.event(event_id),
  venue_id   text NOT NULL REFERENCES venues.venue(venue_id),
  rank       integer NOT NULL,
  score      double precision,
  method     text NOT NULL,
  evidence   jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_event_venue_link_candidate_event
    ON events.event_venue_link_candidate (event_id);

ALTER TABLE events.event
  ADD COLUMN IF NOT EXISTS location_resolution text,
  ADD COLUMN IF NOT EXISTS location_confidence double precision,
  ADD COLUMN IF NOT EXISTS linked_by text,
  ADD COLUMN IF NOT EXISTS linked_at timestamptz;
"""

DOWNGRADE = r"""
ALTER TABLE events.event
  DROP COLUMN IF EXISTS linked_at,
  DROP COLUMN IF EXISTS linked_by,
  DROP COLUMN IF EXISTS location_confidence,
  DROP COLUMN IF EXISTS location_resolution;

DROP INDEX IF EXISTS events.ix_event_venue_link_candidate_event;
DROP TABLE IF EXISTS events.event_venue_link_candidate;

DROP INDEX IF EXISTS events.ix_promoter_account_status;
DROP TABLE IF EXISTS events.promoter_account;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
