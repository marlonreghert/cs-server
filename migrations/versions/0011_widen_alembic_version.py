"""widen alembic_version.version_num so long revision ids fit

Alembic's default ``alembic_version.version_num`` is ``varchar(32)``. This repo's
descriptive revision ids run right at that limit (``0008`` was exactly 32) and
``0010``'s original id (38 chars) overflowed it, so the post-migration version
stamp failed on Postgres (``StringDataRightTruncation``) — caught by the Postgres
parity CI. Widen the column to ``varchar(128)`` so future descriptive ids fit and
this class of failure cannot recur.

Revision ID: 0011_widen_alembic_version
Revises: 0010_reactivate_eligibility
Create Date: 2026-06-14
"""
from alembic import op

revision = "0011_widen_alembic_version"
down_revision = "0010_reactivate_eligibility"
branch_labels = None
depends_on = None

WIDEN = "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE varchar(128)"
NARROW = "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE varchar(32)"


def upgrade() -> None:
    op.execute(WIDEN)


def downgrade() -> None:
    # Safe while every applied revision id is <= 32 chars; narrowing would fail
    # (not silently truncate) if a longer id were present.
    op.execute(NARROW)
