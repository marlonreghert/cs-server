"""Add primary_type_locked to google_places.vibe_attributes (per-venue type override).

When an operator corrects a mis-typed venue via the per-venue type override, the
corrected google_primary_type is stored and this flag is set so re-enrichment
(force_refresh) does not overwrite it. The eligibility view and category
resolution already key off google_primary_type, so storing the corrected value
un-blocks + recategorizes the venue; this flag only protects it from being
clobbered.

Revision ID: 0018_vibe_attributes_primary_type_locked
Revises: 0017_bakery_good_type
Create Date: 2026-07-24
"""
from alembic import op

revision = "0018_vibe_attributes_primary_type_locked"
down_revision = "0017_bakery_good_type"
branch_labels = None
depends_on = None

ADD_COLUMN = r"""
ALTER TABLE google_places.vibe_attributes
  ADD COLUMN IF NOT EXISTS primary_type_locked boolean NOT NULL DEFAULT false;
"""

DROP_COLUMN = r"""
ALTER TABLE google_places.vibe_attributes DROP COLUMN IF EXISTS primary_type_locked;
"""


def upgrade() -> None:
    op.execute(ADD_COLUMN)


def downgrade() -> None:
    op.execute(DROP_COLUMN)
