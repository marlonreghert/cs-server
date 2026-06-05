"""venue residual payload (Ex1 expand)

Adds venues.venue.extra (jsonb) — the slim residual holding ONLY the nested Venue
fields columns cannot hold (foot-traffic forecast, dwell times). Scalars are the
source of truth in their own columns; `payload` is retained as the v1 golden
baseline for the equivalence diff until the (separate, soak-gated) 0003b contract
migration drops it. See plans/260605_rds-schema-normalization.md (Step Ex1).

This is the EXPAND half only. The contract (drop `payload`) ships in its own PR
after a green full-dataset golden diff, because the application still reads
`payload` as the diff baseline until then.

Revision ID: 0003_venue_residual
Revises: 0002_venue_priority
Create Date: 2026-06-05
"""
from alembic import op

revision = "0003_venue_residual"
down_revision = "0002_venue_priority"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE venues.venue
  ADD COLUMN IF NOT EXISTS extra jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Backfill the residual from the existing payload for rows written before the
-- application started dual-writing `extra`. Mirrors venue_row.split_venue_for_storage:
-- exactly the nested fields, each present (JSON null when absent), so a
-- column+residual reconstruction equals the payload reconstruction.
UPDATE venues.venue
SET extra = jsonb_build_object(
  'venue_dwell_time_min',        payload->'venue_dwell_time_min',
  'venue_dwell_time_max',        payload->'venue_dwell_time_max',
  'venue_foot_traffic_forecast', payload->'venue_foot_traffic_forecast'
)
WHERE extra = '{}'::jsonb;
"""

DOWNGRADE = r"""
ALTER TABLE venues.venue DROP COLUMN IF EXISTS extra;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
