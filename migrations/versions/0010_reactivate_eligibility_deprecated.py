"""reactivate eligibility_filter-deprecated venues (gold-view Phase 3)

Phase 3 of plans/260613_eligibility-serving-view.md. With eligibility now a
non-destructive serving view (serving.eligible_venue), the venues the retired
sweep had soft-deleted for eligibility must be flipped back to active so the view
governs them: ones still ineligible simply stay out of the view (not served, not
deleted), and corrected false positives (e.g. book_store once unblocked) re-enter
serving on the next projection.

Scope: ONLY rows with deprecated_source='eligibility_filter' are reactivated.
Google permanently-closed venues (deprecated_source='google_places') stay
deprecated. google_business_status is left untouched (it is not a closure flag on
eligibility-deprecated rows). Redis is NOT touched here — the off-loop projector
re-projects the now-eligible venues on its next cycle.

ORDERING: run this AFTER the gold-view code (migration 0009 + the cutover) is
deployed and the cutover delta is validated — NOT before code like the schema
migrations. It is post-deploy data correction.

ROLLBACK: IRREVERSIBLE via alembic — it clears deprecated_reason/source/at, so the
prior values cannot be reconstructed. Capture + verify a pre-migration pg_dump
first (RDS rollback policy); roll back by restoring that snapshot, not by
`alembic downgrade`. Idempotent: re-running matches 0 rows.

Revision ID: 0010_reactivate_eligibility
Revises: 0009_eligibility_serving_view
Create Date: 2026-06-13
"""
import logging

from alembic import op
from sqlalchemy import text

revision = "0010_reactivate_eligibility"
down_revision = "0009_eligibility_serving_view"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_COUNT_SQL = (
    "SELECT count(*) FROM venues.venue "
    "WHERE lifecycle_status='deprecated' AND deprecated_source='eligibility_filter'"
)

REACTIVATE = """
UPDATE venues.venue
SET lifecycle_status = 'active',
    deprecated_reason = NULL,
    deprecated_source = NULL,
    deprecated_at     = NULL,
    updated_at        = now()
WHERE lifecycle_status = 'deprecated'
  AND deprecated_source = 'eligibility_filter'
"""


def upgrade() -> None:
    conn = op.get_bind()
    before = conn.execute(text(_COUNT_SQL)).scalar_one()
    result = conn.execute(text(REACTIVATE))
    # Observability: the count is logged here; the full before/after served-set
    # delta + deprecated_source breakdown (eligibility_filter -> 0) is captured by
    # scripts/validate_eligibility_view.py and reflected in the active/deprecated
    # data-quality gauges on the next metrics refresh.
    logger.info(
        "[0010] reactivated %s eligibility_filter-deprecated venues (rowcount=%s); "
        "the serving view now governs them",
        before, result.rowcount,
    )


def downgrade() -> None:
    raise RuntimeError(
        "0010 is irreversible via alembic (deprecated_reason/source/at were "
        "cleared and cannot be reconstructed). Restore the pre-migration pg_dump "
        "snapshot to roll back."
    )
