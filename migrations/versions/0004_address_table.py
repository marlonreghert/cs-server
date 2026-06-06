"""structured address table (Ex3 expand)

Extracts a venue's address into venues.address (1:1, referenced by venue_id) with
the raw text + lat/lng (backfilled from the existing venues.venue columns) plus
structured components (street/neighborhood/city/postal_code) that stay null until
Google Places enrichment fills them. lat/lng stay NOT NULL so the Redis geo index
still rebuilds. See plans/260605_rds-schema-normalization.md (Step Ex3).

EXPAND half only: the venues.venue address columns are kept as a rollback baseline
and only dropped by the later batched contract, after the reading code no longer
references them.

Revision ID: 0004_address_table
Revises: 0003_venue_residual
Create Date: 2026-06-06
"""
from alembic import op

revision = "0004_address_table"
down_revision = "0003_venue_residual"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE IF NOT EXISTS venues.address (
  venue_id     text PRIMARY KEY REFERENCES venues.venue(venue_id),
  raw_text     text,
  street       text,
  neighborhood text,
  city         text,
  postal_code  text,
  lat          double precision NOT NULL,
  lng          double precision NOT NULL,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Backfill one address row per venue from the existing columns (components null).
INSERT INTO venues.address (venue_id, raw_text, lat, lng)
SELECT venue_id, venue_address, venue_lat, venue_lng FROM venues.venue
ON CONFLICT (venue_id) DO NOTHING;
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS venues.address;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
