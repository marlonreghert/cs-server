"""eligibility as a gold-layer serving view (serving.eligible_venue)

Makes eligibility a dynamic SQL view that the Redis projector reads, instead of a
destructive soft-delete sweep. `serving.eligible_venue` returns the venue ids that
are `lifecycle_status='active'` AND eligible under the live `admin.eligibility_rule`
block-list — so blocking/unblocking a type changes serving on the next projection
in both directions, with no lifecycle change and no soft-delete.

This migration (EXPAND, additive):
  - creates the `serving` schema and the `serving.eligible_venue` view;
  - creates `admin.category_good_type` (the SQL-side "good category" lookup) and
    seeds it from the Python `resolve_category` maps (parity-guarded by
    tests/test_eligibility_serving_view_parity.py against drift);
  - seeds the hardcoded default eligibility rules as rows (ON CONFLICT DO NOTHING,
    so existing admin overrides are preserved) so the view is self-sufficient and
    needs no SQL-side defaults.

The view encodes the same predicate as app/services/venue_eligibility.evaluate():
a venue is servable iff it is active and NOT high-confidence ineligible —
non-empty name AND google type not blocked AND besttime type not blocked AND NOT
(hard keyword AND not good-category) AND NOT (ambiguous keyword AND not
good-category AND google-labeled). Equivalence to evaluate() is pinned by the
parity test against real Postgres (there is no local Postgres in CI).

Revision ID: 0009_eligibility_serving_view
Revises: 0008_drop_eligibility_admin_blob
Create Date: 2026-06-13
"""
from alembic import op
from sqlalchemy import text

from app.models.venue_category import _BESTTIME_TO_CATEGORY, _GOOGLE_TO_CATEGORY
from app.services.venue_eligibility import (
    DEFAULT_AMBIGUOUS_NAME_KEYWORDS,
    DEFAULT_BLOCKED_GOOGLE_TYPES,
    DEFAULT_BLOCKED_VENUE_TYPES,
    DEFAULT_HARD_BLOCKED_NAME_KEYWORDS,
)

revision = "0009_eligibility_serving_view"
down_revision = "0008_drop_eligibility_admin_blob"
branch_labels = None
depends_on = None


SCHEMA_AND_TABLE = r"""
CREATE SCHEMA IF NOT EXISTS serving;

-- SQL-side "good category" lookup: a (token, kind) is present iff the Python
-- resolve_category map classifies that token to a non-OTHER category. The view
-- uses EXISTS over this table instead of re-encoding the category map in SQL.
CREATE TABLE IF NOT EXISTS admin.category_good_type (
  token text NOT NULL,
  kind  text NOT NULL CHECK (kind IN ('google', 'besttime')),
  PRIMARY KEY (token, kind)
);
"""

# The eligibility predicate, mirrored from venue_eligibility.evaluate(). gtype is
# lower(google_primary_type) (matches blocked_google_type/good-type 'google' rows);
# btype is upper(venue_type) (matches blocked_venue_type/good-type 'besttime' rows).
CREATE_VIEW = r"""
CREATE OR REPLACE VIEW serving.eligible_venue AS
WITH va AS (
  SELECT venue_id, lower(google_primary_type) AS gtype
  FROM google_places.vibe_attributes
  WHERE deleted_at IS NULL AND google_primary_type IS NOT NULL
),
v AS (
  SELECT
    ve.venue_id,
    lower(coalesce(ve.venue_name, '')) AS name_lower,
    btrim(coalesce(ve.venue_name, ''))  AS name_trim,
    upper(ve.venue_type)                AS btype,
    va.gtype                            AS gtype
  FROM venues.venue ve
  LEFT JOIN va ON va.venue_id = ve.venue_id
  WHERE ve.lifecycle_status = 'active'
),
g AS (
  SELECT v.venue_id,
         (EXISTS (SELECT 1 FROM admin.category_good_type c
                   WHERE c.kind = 'google'   AND c.token = v.gtype)
          OR EXISTS (SELECT 1 FROM admin.category_good_type c
                   WHERE c.kind = 'besttime' AND c.token = v.btype)) AS good_category,
         v.*
  FROM v
)
SELECT g.venue_id
FROM g
WHERE g.name_trim <> ''
  AND NOT EXISTS (
        SELECT 1 FROM admin.eligibility_rule r
        WHERE r.rule_type = 'blocked_google_type'
          AND g.gtype IS NOT NULL AND r.value = g.gtype)
  AND NOT EXISTS (
        SELECT 1 FROM admin.eligibility_rule r
        WHERE r.rule_type = 'blocked_venue_type'
          AND g.btype IS NOT NULL AND r.value = g.btype)
  AND NOT (
        NOT g.good_category
        AND EXISTS (
          SELECT 1 FROM admin.eligibility_rule r
          WHERE r.rule_type = 'hard_blocked_name_keyword'
            AND strpos(g.name_lower, r.value) > 0))
  AND NOT (
        NOT g.good_category
        AND g.gtype IS NOT NULL
        AND EXISTS (
          SELECT 1 FROM admin.eligibility_rule r
          WHERE r.rule_type = 'ambiguous_name_keyword'
            AND strpos(g.name_lower, r.value) > 0));
"""

DOWNGRADE = r"""
DROP VIEW IF EXISTS serving.eligible_venue;
DROP TABLE IF EXISTS admin.category_good_type;
DROP SCHEMA IF EXISTS serving;
"""


def _seed_good_types(conn) -> None:
    """Seed admin.category_good_type from the resolve_category maps. Google tokens
    are stored lowercase, BestTime tokens uppercase, matching the view's gtype/btype
    normalization. The parity test regenerates this from the maps and asserts no
    drift."""
    ins = text(
        "INSERT INTO admin.category_good_type (token, kind) VALUES (:t, :k) "
        "ON CONFLICT DO NOTHING"
    )
    for token in sorted(_GOOGLE_TO_CATEGORY):
        conn.execute(ins, {"t": token.lower(), "k": "google"})
    for token in sorted(_BESTTIME_TO_CATEGORY):
        conn.execute(ins, {"t": token.upper(), "k": "besttime"})


def _seed_default_rules(conn) -> None:
    """Seed the hardcoded default block-lists as admin.eligibility_rule rows so the
    view is self-sufficient (no SQL-side defaults). ON CONFLICT DO NOTHING preserves
    any existing admin override rows; normalization mirrors EligibilityConfig."""
    ins = text(
        "INSERT INTO admin.eligibility_rule (rule_type, value) VALUES (:rt, :v) "
        "ON CONFLICT DO NOTHING"
    )
    for value in sorted(DEFAULT_BLOCKED_VENUE_TYPES):
        conn.execute(ins, {"rt": "blocked_venue_type", "v": value.upper()})
    for value in sorted(DEFAULT_BLOCKED_GOOGLE_TYPES):
        conn.execute(ins, {"rt": "blocked_google_type", "v": value.lower()})
    for value in DEFAULT_HARD_BLOCKED_NAME_KEYWORDS:
        conn.execute(ins, {"rt": "hard_blocked_name_keyword", "v": value.lower()})
    for value in DEFAULT_AMBIGUOUS_NAME_KEYWORDS:
        conn.execute(ins, {"rt": "ambiguous_name_keyword", "v": value.lower()})


def upgrade() -> None:
    op.execute(SCHEMA_AND_TABLE)
    conn = op.get_bind()
    _seed_good_types(conn)
    _seed_default_rules(conn)
    op.execute(CREATE_VIEW)


def downgrade() -> None:
    # Leaves the seeded eligibility_rule rows (indistinguishable from operator
    # overrides; harmless — the pre-view code treats rows as the override set).
    op.execute(DOWNGRADE)
