"""Add ('bakery','google') to admin.category_good_type for the new BAKERY category.

The Python resolve_category maps now classify Google ``bakery`` to the new
``BAKERY`` category (app/models/venue_category.py). ``admin.category_good_type``
is the SQL-side mirror of those maps that ``serving.eligible_venue`` uses for its
``good_category`` check (see 0009_eligibility_serving_view). On an existing
database the seed in 0009 already ran against the old map, so this migration adds
the new row; on a fresh database 0009 re-reads the updated map and this insert is
a harmless no-op (ON CONFLICT DO NOTHING). Without it, a ``bakery`` venue whose
name contains a hard/ambiguous blocked keyword could be wrongly filtered by the
live serving view (Python/SQL drift).

Revision ID: 0017_bakery_good_type
Revises: 0016_hot_like_event_idempotency
Create Date: 2026-07-24
"""
from alembic import op

revision = "0017_bakery_good_type"
down_revision = "0016_hot_like_event_idempotency"
branch_labels = None
depends_on = None

INSERT_BAKERY = r"""
INSERT INTO admin.category_good_type (token, kind)
VALUES ('bakery', 'google')
ON CONFLICT DO NOTHING;
"""

DELETE_BAKERY = r"""
DELETE FROM admin.category_good_type WHERE token = 'bakery' AND kind = 'google';
"""


def upgrade() -> None:
    op.execute(INSERT_BAKERY)


def downgrade() -> None:
    op.execute(DELETE_BAKERY)
