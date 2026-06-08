"""drop the redundant admin_config:venue_eligibility blob row

The eligibility serving mirror is now rebuilt directly from the
admin.eligibility_rule rows (on startup + each projector cycle), and the write
path no longer persists the derived blob to admin.admin_config. This deletes the
existing redundant row so the rows are the sole durable truth.

Safe in either deploy order — the row is not serving truth (serving reads the
Redis mirror; the dedicated eligibility GET reads the rows). Downgrade is a no-op:
the row is derived from the rows and is re-derivable.

Revision ID: 0008_drop_eligibility_admin_blob
Revises: 0007_drop_legacy_venue_columns
Create Date: 2026-06-08
"""
from alembic import op

revision = "0008_drop_eligibility_admin_blob"
down_revision = "0007_drop_legacy_venue_columns"
branch_labels = None
depends_on = None


UPGRADE = r"""
DELETE FROM admin.admin_config WHERE key = 'venue_eligibility';
"""

DOWNGRADE = r"""
-- No-op: the blob was a derived projection of admin.eligibility_rule. If a
-- rollback needs it, an admin write (or the rule service) reassembles it.
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
