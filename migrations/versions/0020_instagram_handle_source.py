"""Promote the cascade source onto instagram.handle.

The handle cascade resolves from three tiers, two of them free. "How many
handles came free versus paid" is the first question the feature has to answer,
and answering it from jsonb extraction across the whole catalog is exactly the
query that gets written wrong — so the tier is a real, indexable column.

Additive and nullable: existing rows predate the cascade and legitimately have
no source, and nothing reads it as required.

Revision ID: 0020_instagram_handle_source
Revises: 0019_venue_closure_signal
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_instagram_handle_source"
down_revision = "0019_venue_closure_signal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "handle", sa.Column("source", sa.Text(), nullable=True), schema="instagram"
    )
    op.create_index(
        "ix_instagram_handle_source", "handle", ["source"], schema="instagram"
    )


def downgrade() -> None:
    op.drop_index("ix_instagram_handle_source", table_name="handle", schema="instagram")
    op.drop_column("handle", "source", schema="instagram")
