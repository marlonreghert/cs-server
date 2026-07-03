"""Recife-metro geo-fence eligibility (admin.geo_fence + serving.eligible_venue)

Adds a location dimension to serving eligibility so only venues inside the allowed
Recife/Olinda metro box are served. Driven by the 2026-07-01 incident: 826 of 1648
active venues sit outside the region and were being served + consuming the scarce
BestTime refresh budget.

This migration:
  1. Creates the single-row `admin.geo_fence` table (min/max lat/lng + enabled),
     seeded with the confirmed default box (lat -8.30..-7.85, lng -35.10..-34.80).
     The serving view reads this row directly (that is why it is a typed table, not
     an admin.admin_config blob); the admin PUT /admin/config/geofence edits it.
  2. Redefines `serving.eligible_venue` to LEFT JOIN venues.address and additionally
     require — when the fence is enabled and coordinates are present — that lat/lng
     fall inside the box. Missing address / missing coords / a disabled fence are
     FAIL-OPEN (never geo-excluded), consistent with the reversible, never-soft-
     delete policy. All the name/type eligibility logic from 0009 is preserved
     unchanged; the geo term is an orthogonal top-level predicate.

Geo-exclusion is a THIRD state: a matching venue is dropped from serving but is
never soft-deleted and stays active in venues.venue. Editing/widening the box (or
disabling the fence) re-includes venues on the next projection — fully reversible.

Parity with app/services/venue_eligibility.geo_excluded() (the Python predicate the
fake store + parity test use) is pinned by tests/test_eligibility_serving_view_parity.py
against real Postgres (there is no local Postgres in CI).

OPERATIONS — deploy does NOT run alembic (CI-only). Apply manually on RDS, e.g.
`docker exec vibes_bot-cs-server-1 alembic upgrade head`. `downgrade()` drops
admin.geo_fence and restores the 0009 (no-geo) view definition — reversible.

Revision ID: 0014_geofence_eligible_venue
Revises: 0013_price_level_objective_source
Create Date: 2026-07-01
"""
from alembic import op

# Historical seed values, frozen at this revision. (This box WAS
# app.services.venue_eligibility.DEFAULT_GEO_FENCE when 0014 shipped; 0015
# replaced that constant with capital-city circles, so the box is inlined here
# to keep the 0001→head chain runnable on a fresh database.)
_SEED_BOX = {
    "min_lat": -8.30,
    "max_lat": -7.85,
    "min_lng": -35.10,
    "max_lng": -34.80,
    "enabled": True,
}

revision = "0014_geofence_eligible_venue"
down_revision = "0013_price_level_objective_source"
branch_labels = None
depends_on = None


# Single-row geo-fence table (id=1 singleton, guarded by CHECK). Seeded with the
# confirmed default Recife/Olinda box. lat in [-90,90], lng in [-180,180], min<max
# mirror validate_geo_fence().
CREATE_TABLE = r"""
CREATE TABLE IF NOT EXISTS admin.geo_fence (
  id         integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  min_lat    double precision NOT NULL,
  max_lat    double precision NOT NULL,
  min_lng    double precision NOT NULL,
  max_lng    double precision NOT NULL,
  enabled    boolean NOT NULL DEFAULT true,
  updated_by text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (min_lat BETWEEN -90 AND 90 AND max_lat BETWEEN -90 AND 90),
  CHECK (min_lng BETWEEN -180 AND 180 AND max_lng BETWEEN -180 AND 180),
  CHECK (min_lat < max_lat AND min_lng < max_lng)
);
"""

SEED_ROW = r"""
INSERT INTO admin.geo_fence (id, min_lat, max_lat, min_lng, max_lng, enabled, updated_by)
VALUES (1, :min_lat, :max_lat, :min_lng, :max_lng, :enabled, 'migration_0014')
ON CONFLICT (id) DO NOTHING;
"""

# Redefined view: same name/type eligibility predicate as 0009, plus the geo term.
# The `v` CTE now LEFT JOINs venues.address for lat/lng; a one-row cross join to
# admin.geo_fence exposes the box. The geo term is fail-open: a venue is geo-OK when
# the fence is disabled/absent, OR its coords are missing, OR its coords fall inside
# the box.
CREATE_VIEW_GEO = r"""
CREATE OR REPLACE VIEW serving.eligible_venue AS
WITH fence AS (
  SELECT min_lat, max_lat, min_lng, max_lng, enabled
  FROM admin.geo_fence WHERE id = 1
),
va AS (
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
    va.gtype                            AS gtype,
    addr.lat                            AS lat,
    addr.lng                            AS lng
  FROM venues.venue ve
  LEFT JOIN va ON va.venue_id = ve.venue_id
  LEFT JOIN venues.address addr ON addr.venue_id = ve.venue_id
  WHERE ve.lifecycle_status = 'active'
),
g AS (
  SELECT (EXISTS (SELECT 1 FROM admin.category_good_type c
                   WHERE c.kind = 'google'   AND c.token = v.gtype)
          OR EXISTS (SELECT 1 FROM admin.category_good_type c
                   WHERE c.kind = 'besttime' AND c.token = v.btype)) AS good_category,
         v.*
  FROM v
)
SELECT g.venue_id
FROM g
LEFT JOIN fence ON true
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
            AND strpos(g.name_lower, r.value) > 0))
  -- Geo-fence (fail-open): OK when the fence is absent/disabled, coords are
  -- missing, or coords fall inside the box.
  AND (
        fence.enabled IS NOT TRUE
        OR g.lat IS NULL OR g.lng IS NULL
        OR (g.lat BETWEEN fence.min_lat AND fence.max_lat
            AND g.lng BETWEEN fence.min_lng AND fence.max_lng));
"""

# 0009's view definition (no geo term) — restored on downgrade.
CREATE_VIEW_0009 = r"""
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
  SELECT (EXISTS (SELECT 1 FROM admin.category_good_type c
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


def upgrade() -> None:
    from sqlalchemy import text

    op.execute(CREATE_TABLE)
    op.get_bind().execute(text(SEED_ROW), dict(_SEED_BOX))
    op.execute(CREATE_VIEW_GEO)


def downgrade() -> None:
    # Restore the 0009 (no-geo) view, then drop the geo-fence table. Reversible —
    # no venue is soft-deleted by this migration, so the servable set simply grows
    # back to its pre-geo membership on the next projection.
    op.execute(CREATE_VIEW_0009)
    op.execute("DROP TABLE IF EXISTS admin.geo_fence;")
