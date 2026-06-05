"""venue refresh priority

Adds a refresh-selection priority to venues.venue. Live + weekly forecast
refresh select the top-X active venues ordered by priority ascending (0 = most
important … 5 = least), spending BestTime's scarce monthly unique-venue
allowance on the venues that matter. Priority is a refresh-selection concern
read from RDS; it is intentionally NOT projected to Redis (serving does not
need it). See plans/priority_bounded_besttime_refresh_04_06_26.md.

Revision ID: 0002_venue_priority
Revises: 0001_baseline
Create Date: 2026-06-04
"""
from alembic import op

revision = "0002_venue_priority"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE venues.venue ADD COLUMN IF NOT EXISTS priority smallint NOT NULL DEFAULT 5;
-- Partial index supports the bounded-refresh selection query, which always
-- filters lifecycle_status='active' and orders by priority.
CREATE INDEX IF NOT EXISTS ix_venue_priority
  ON venues.venue (priority)
  WHERE lifecycle_status = 'active';
"""

DOWNGRADE = r"""
DROP INDEX IF EXISTS venues.ix_venue_priority;
ALTER TABLE venues.venue DROP COLUMN IF EXISTS priority;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
