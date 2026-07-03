"""Geo-fence as capital-city circles (admin.geo_fence_city + serving.eligible_venue)

Replaces the single Recife/Olinda bounding box (0014) with admin-managed
state-capital circles: each admin.geo_fence_city row is one capital (slug +
server-owned center coordinates) with an admin-chosen radius_km. The singleton
admin.geo_fence row keeps only the global `enabled` flag (box columns dropped,
current value preserved).

This migration:
  1. Creates `admin.geo_fence_city` (slug PK, name, lat/lng with range CHECKs,
     radius_km CHECK 1..200, updated_by/updated_at), seeded with the default
     fence (recife @ 40 km). The 40 km circle strictly contains the 0014 box —
     its farthest corner is ≈37.3 km from the Recife center — so NO venue
     served today is geo-excluded by this migration; genuinely-metro venues
     just outside the old box (e.g. Igarassu/Itapissuma) are re-included.
  2. Redefines `serving.eligible_venue`: name/type predicates unchanged from
     0014; the geo term becomes fail-open circle membership via core-Postgres
     haversine (no extension) — geo-OK when the fence is disabled, coords are
     missing, OR the venue lies within radius of ANY configured circle. An
     empty city table while enabled is fail-open at view level (unreachable
     through the admin API, which requires ≥1 city when enabled).
  3. Drops the admin.geo_fence box columns (after the view stops reading them).

Geo-exclusion stays a THIRD state: a matching venue is dropped from serving but
is never soft-deleted and stays active in venues.venue. Editing circles/radii
(or disabling the fence) re-includes venues on the next projection — reversible.

Parity with app/services/venue_eligibility.geo_excluded() (same haversine, same
mean Earth radius 6371.0088) is pinned by
tests/test_eligibility_serving_view_parity.py against real Postgres.

OPERATIONS — deploy does NOT run alembic (CI-only). Snapshot RDS first, then
apply manually, e.g. `docker exec vibes_bot-cs-server-1 alembic upgrade head`.
After applying, verify old-box vs new-circle membership over active venues
(expect 0 newly excluded) and that serving_view_venues does not drop.
`downgrade()` restores the 0014 table shape (re-seeding the original box) and
the 0014 view — reversible.

Revision ID: 0015_geofence_city_circles
Revises: 0014_geofence_eligible_venue
Create Date: 2026-07-02
"""
from alembic import op

# Historical seed, frozen at this revision (this WAS the code default when 0015
# shipped). Never import live constants into a migration — 0014 had to be
# patched for exactly that when the box default became city circles.
_SEED_CITIES = (
    {"slug": "recife", "name": "Recife", "lat": -8.0476, "lng": -34.8770,
     "radius_km": 40.0},
)

revision = "0015_geofence_city_circles"
down_revision = "0014_geofence_eligible_venue"
branch_labels = None
depends_on = None


# One row per configured capital circle. slug is the stable admin-facing id;
# name/lat/lng are copied from the server-side STATE_CAPITALS catalog on every
# write (the server owns coordinates). CHECKs mirror validate_geo_fence().
CREATE_CITY_TABLE = r"""
CREATE TABLE IF NOT EXISTS admin.geo_fence_city (
  slug       text PRIMARY KEY,
  name       text NOT NULL,
  lat        double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lng        double precision NOT NULL CHECK (lng BETWEEN -180 AND 180),
  radius_km  double precision NOT NULL CHECK (radius_km BETWEEN 1 AND 200),
  updated_by text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""

SEED_CITY = r"""
INSERT INTO admin.geo_fence_city (slug, name, lat, lng, radius_km, updated_by)
VALUES (:slug, :name, :lat, :lng, :radius_km, 'migration_0015')
ON CONFLICT (slug) DO NOTHING;
"""

# The 0014 box columns; admin.geo_fence keeps only id/enabled/updated_by/
# updated_at (the global on/off switch the fence CTE reads).
DROP_BOX_COLUMNS = r"""
ALTER TABLE admin.geo_fence
  DROP COLUMN IF EXISTS min_lat,
  DROP COLUMN IF EXISTS max_lat,
  DROP COLUMN IF EXISTS min_lng,
  DROP COLUMN IF EXISTS max_lng;
"""

# Redefined view: same name/type eligibility predicate as 0014; the geo term is
# circle membership. Fail-open: geo-OK when the fence is absent/disabled, coords
# are missing, or the venue is within radius of ANY configured circle (haversine
# on the 6371.0088 km mean Earth radius — parity with haversine_km()).
CREATE_VIEW_CIRCLES = r"""
CREATE OR REPLACE VIEW serving.eligible_venue AS
WITH fence AS (
  SELECT enabled FROM admin.geo_fence WHERE id = 1
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
  -- missing, NO circle is configured (empty table = restriction off, matching
  -- geo_excluded()'s fail-open), or the venue is inside ANY capital circle.
  AND (
        fence.enabled IS NOT TRUE
        OR g.lat IS NULL OR g.lng IS NULL
        OR NOT EXISTS (SELECT 1 FROM admin.geo_fence_city)
        OR EXISTS (
             SELECT 1 FROM admin.geo_fence_city c
             WHERE 2 * 6371.0088 * asin(sqrt(
                     pow(sin(radians(g.lat - c.lat) / 2), 2)
                     + cos(radians(c.lat)) * cos(radians(g.lat))
                       * pow(sin(radians(g.lng - c.lng) / 2), 2))) <= c.radius_km));
"""

# ── downgrade targets: the 0014 shapes, frozen ────────────────────────────────
# The confirmed Recife/Olinda box 0014 seeded (re-asserted on downgrade).
RESTORE_BOX_COLUMNS = r"""
ALTER TABLE admin.geo_fence
  ADD COLUMN IF NOT EXISTS min_lat double precision,
  ADD COLUMN IF NOT EXISTS max_lat double precision,
  ADD COLUMN IF NOT EXISTS min_lng double precision,
  ADD COLUMN IF NOT EXISTS max_lng double precision;
UPDATE admin.geo_fence
   SET min_lat = -8.30, max_lat = -7.85, min_lng = -35.10, max_lng = -34.80
 WHERE id = 1;
ALTER TABLE admin.geo_fence
  ALTER COLUMN min_lat SET NOT NULL,
  ALTER COLUMN max_lat SET NOT NULL,
  ALTER COLUMN min_lng SET NOT NULL,
  ALTER COLUMN max_lng SET NOT NULL,
  ADD CHECK (min_lat BETWEEN -90 AND 90 AND max_lat BETWEEN -90 AND 90),
  ADD CHECK (min_lng BETWEEN -180 AND 180 AND max_lng BETWEEN -180 AND 180),
  ADD CHECK (min_lat < max_lat AND min_lng < max_lng);
"""

# 0014's view definition (bounding-box geo term) — restored on downgrade.
CREATE_VIEW_0014 = r"""
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
  AND (
        fence.enabled IS NOT TRUE
        OR g.lat IS NULL OR g.lng IS NULL
        OR (g.lat BETWEEN fence.min_lat AND fence.max_lat
            AND g.lng BETWEEN fence.min_lng AND fence.max_lng));
"""


def upgrade() -> None:
    from sqlalchemy import text

    op.execute(CREATE_CITY_TABLE)
    bind = op.get_bind()
    # Seed the frozen default (recife @ 40 km) so the view, the Python
    # predicate, and a defaults-serving pre-migration reader agree on day one.
    for city in _SEED_CITIES:
        bind.execute(text(SEED_CITY), {
            "slug": city["slug"], "name": city["name"],
            "lat": city["lat"], "lng": city["lng"],
            "radius_km": city["radius_km"],
        })
    # Redefine the view BEFORE dropping the box columns it currently reads
    # (`enabled` is preserved on the singleton row throughout).
    op.execute(CREATE_VIEW_CIRCLES)
    op.execute(DROP_BOX_COLUMNS)


def downgrade() -> None:
    # Restore the box columns + original seeded box, re-point the view at them,
    # then drop the city table. Reversible — no venue is soft-deleted either
    # way; the servable set follows the fence shape on the next projection.
    op.execute(RESTORE_BOX_COLUMNS)
    op.execute(CREATE_VIEW_0014)
    op.execute("DROP TABLE IF EXISTS admin.geo_fence_city;")
