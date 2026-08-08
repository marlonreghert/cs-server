"""Per-user venue block list — `engagement.blocked_venue`.

See plans/260808_blocked-venues.md. Chains from
0028_event_ticket_info_and_attractions, the current head — this migration adds
one new table to the `engagement` schema (created by 0001) and touches nothing
else 0002-0028 own.

`engagement.blocked_venue` mirrors `engagement.favorite`'s shape exactly (same
column set, same PK, same soft-delete convention): `user_pseudo text NOT NULL`
(HMAC(user_id), the raw id is never stored — see `engagement.favorite`'s own
comment), `venue_id text NOT NULL REFERENCES venues.venue(venue_id)`,
`created_at`/`updated_at` timestamptz defaulting to `now()`, `deleted_at`
nullable (un-block = soft-delete, exactly like un-favorite), `PRIMARY KEY
(user_pseudo, venue_id)`, plus `ix_blocked_venue_venue` on `venue_id` (mirrors
`ix_favorite_venue`).

Deliberately named `blocked_venue`, NOT `block`/`block_list`: the repo already
has an unrelated, pre-existing "block-list" concept (`admin.eligibility_rule` /
`app/services/venue_eligibility.py`, admin-tunable Google/BestTime type +
keyword exclusion). `blocked_venue` lives in the `engagement` schema alongside
`favorite`/`hot_like_event` and is unambiguous against that older concept.

A user's block and their favorite for the same venue are mutually exclusive by
application-level invariant (`RdsVenueStore.block_venue` upserts this table and
soft-deletes any active `engagement.favorite` row for the same
`(user_pseudo, venue_id)` in one transaction) — nothing here enforces that at
the schema level (no cross-table constraint), the same way `favorite` itself
carries no schema-level tie to anything else.

Additive only: one new table, one new index. The downgrade drops exactly what
this migration created and touches no other table.

Revision ID: 0029_blocked_venues
Revises: 0028_event_ticket_info_and_attractions
"""
from alembic import op

revision = "0029_blocked_venues"
down_revision = "0028_event_ticket_info_and_attractions"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE IF NOT EXISTS engagement.blocked_venue (
  user_pseudo text NOT NULL,
  venue_id    text NOT NULL REFERENCES venues.venue(venue_id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  deleted_at  timestamptz,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_pseudo, venue_id)
);

CREATE INDEX IF NOT EXISTS ix_blocked_venue_venue ON engagement.blocked_venue (venue_id);
"""

DOWNGRADE = r"""
DROP INDEX IF EXISTS engagement.ix_blocked_venue_venue;
DROP TABLE IF EXISTS engagement.blocked_venue;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
