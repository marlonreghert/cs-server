"""relax NOT NULL on legacy venue columns (batched contract — PRE-DEPLOY half)

The contract stops writing `payload` and the venues.venue address columns. Those
three (`payload`, `venue_lat`, `venue_lng`) are NOT NULL with no default, so the
brand-new-venue INSERT path would violate NOT NULL the moment the new code omits
them. This migration relaxes those constraints.

ORDERING (drops invert the usual migrate-before-deploy rule): apply this and
CONFIRM IT IS LIVE *before* the contract code is deployed. Relaxing a NOT NULL is
safe for the still-running old code (it keeps writing the columns). The column
drop itself is `0007`, applied AFTER deploy + a clean serving-diff verify.

`venue_address` keeps its `DEFAULT ''`, so omitting it in the INSERT is already
safe — it needs no relaxation here.

Revision ID: 0006_relax_legacy_not_null
Revises: 0005_admin_eligibility_rules
Create Date: 2026-06-07
"""
from alembic import op

revision = "0006_relax_legacy_not_null"
down_revision = "0005_admin_eligibility_rules"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE venues.venue
  ALTER COLUMN payload   DROP NOT NULL,
  ALTER COLUMN venue_lat DROP NOT NULL,
  ALTER COLUMN venue_lng DROP NOT NULL;
"""

DOWNGRADE = r"""
-- Re-assert NOT NULL. Safe only while every row still has non-null values for
-- these columns (true before 0007; after 0007 the columns no longer exist).
ALTER TABLE venues.venue
  ALTER COLUMN payload   SET NOT NULL,
  ALTER COLUMN venue_lat SET NOT NULL,
  ALTER COLUMN venue_lng SET NOT NULL;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
