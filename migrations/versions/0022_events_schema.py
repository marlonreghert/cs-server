"""Create the events schema and venue_event_profile table.

Persists the two-stage event venue targeting verdict (see
plans/260804_event-venue-targeting.md): a free category-gate pass/fail plus a
bounded evidence-gate tier, one row per venue.

`evaluated_at` is nullable ON PURPOSE and carries no default — that nullability
is the mechanism that distinguishes "the bounded evidence gate never reached
this venue" (NULL) from "it reached this venue and rejected it" (set). Reading
a NULL as a verdict would turn an uncrawled venue into a silently confirmed
"no events here" fact, which is exactly what this schema exists to prevent.

`tier` is indexed: every event run that follows (extraction, promoter crawls)
filters on it to build its target set.

Additive-only: a new schema and a new table, no existing table touched. The
downgrade is a plain drop — unlike 0021_venue_source's, which is documented as
destructive because it can make live rows indistinguishable from a different
class of venue, dropping this table only discards targeting verdicts, and a
subsequent run recomputes them from the (untouched) servable catalog for free.

Revision ID: 0022_events_schema
Revises: 0021_venue_source
"""
from alembic import op

revision = "0022_events_schema"
down_revision = "0021_venue_source"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE SCHEMA IF NOT EXISTS events;

CREATE TABLE IF NOT EXISTS events.venue_event_profile (
  venue_id        text PRIMARY KEY REFERENCES venues.venue(venue_id),
  tier            text NOT NULL,
  category_pass   boolean NOT NULL,
  category_reason text,
  evidence_score  integer,
  evidence_sample jsonb,
  -- No DEFAULT: NULL means the evidence gate never reached this venue. A
  -- default here would silently manufacture an evaluation timestamp for
  -- every category-only row.
  evaluated_at    timestamptz,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_venue_event_profile_tier
    ON events.venue_event_profile (tier);
"""

DOWNGRADE = r"""
DROP INDEX IF EXISTS events.ix_venue_event_profile_tier;
DROP TABLE IF EXISTS events.venue_event_profile;
DROP SCHEMA IF EXISTS events;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
