"""Add the venue_source discriminator column.

Lets a venue be catalogued from Google metadata alone when BestTime cannot
forecast it (see plans/260804_add-venue-google-only.md). `venue_source` is the
SQL predicate the bounded live/weekly refresh selection filters on every cycle
(a Google-only venue carries no BestTime id and must never be sent to
BestTime), and it lets operators count/audit/bulk-soft-delete these venues from
plain SQL — a jsonb `extra` key could do neither cheaply.

Additive and cheap: `ADD COLUMN ... DEFAULT 'besttime'` is catalog-only on
PostgreSQL 11+ (no table rewrite), and the partial index only covers the
minority non-besttime class, so its cost stays proportional to that class
regardless of catalog size.

Revision ID: 0021_venue_source
Revises: 0020_instagram_handle_source
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_venue_source"
down_revision = "0020_instagram_handle_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE venues.venue "
        "ADD COLUMN IF NOT EXISTS venue_source text NOT NULL DEFAULT 'besttime'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_venue_source_non_besttime "
        "ON venues.venue (venue_source) WHERE venue_source <> 'besttime'"
    )


def downgrade() -> None:
    # NEVER run this while venue_source='google_only' rows exist: dropping the
    # column makes those venues indistinguishable from BestTime venues, and the
    # very next refresh cycle would feed synthetic ids to BestTime. Deprecate
    # those rows first (see the plan's Rollback section), then drop.
    op.execute("DROP INDEX IF EXISTS ix_venue_source_non_besttime")
    op.execute("ALTER TABLE venues.venue DROP COLUMN IF EXISTS venue_source")
