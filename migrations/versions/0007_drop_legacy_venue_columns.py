"""drop legacy venue columns: payload + address columns (batched contract — DROP half)

Irreversible data drop. Run ONLY after the contract code (which stops reading and
writing these columns) is deployed and the redis↔rds serving diff verifies clean.

  - `payload`        — the Ex1 retained v1 baseline (reconstruction is now
                       columns + residual `extra`).
  - `venue_address`,
    `venue_lat`,
    `venue_lng`      — the Ex3 v1 baseline; address now lives solely in
                       venues.address, which also feeds the geo rebuild.

PRE-DROP GATE (operator runbook): take a fresh pg_dump first (the in-DB baseline
is gone after this — the dump is the only rollback, per rds-migration-rollback-
policy), and assert every venue has a 1:1 address row:
    SELECT count(*) FROM venues.venue = SELECT count(*) FROM venues.address
Otherwise a venue missing its address row loses lat/lng and drops from serving.

Revision ID: 0007_drop_legacy_venue_columns
Revises: 0006_relax_legacy_not_null
Create Date: 2026-06-07
"""
from alembic import op

revision = "0007_drop_legacy_venue_columns"
down_revision = "0006_relax_legacy_not_null"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE venues.venue
  DROP COLUMN IF EXISTS payload,
  DROP COLUMN IF EXISTS venue_address,
  DROP COLUMN IF EXISTS venue_lat,
  DROP COLUMN IF EXISTS venue_lng;
"""

DOWNGRADE = r"""
-- Shape-only undo. Recreate the columns (matching the post-0006 nullability),
-- then recover address from venues.address (still intact). `payload` cannot be
-- reconstructed here — restore it from the pre-drop pg_dump if a full rollback is
-- needed (see rds-migration-rollback-policy).
ALTER TABLE venues.venue
  ADD COLUMN IF NOT EXISTS payload       jsonb,
  ADD COLUMN IF NOT EXISTS venue_address text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS venue_lat     double precision,
  ADD COLUMN IF NOT EXISTS venue_lng     double precision;

UPDATE venues.venue v
   SET venue_address = a.raw_text, venue_lat = a.lat, venue_lng = a.lng
  FROM venues.address a
 WHERE a.venue_id = v.venue_id;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
