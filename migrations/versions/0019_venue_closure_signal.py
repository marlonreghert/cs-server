"""closed-venue detection: admin.venue_closure_signal + serving view predicate

A venue whose most recent review reports it permanently closed must not be
served as an open, busy place. The evidence already lives in RDS
(`google_places.reviews`), so detection is a read over data we hold.

This migration (EXPAND, additive):
  - creates `admin.venue_closure_signal`, one row per venue carrying the derived
    signal (closed, reason, confidence, the evidence review's publish time, and
    the phrase that matched);
  - re-asserts `serving.eligible_venue` with one extra predicate excluding
    venues whose signal is closed AND high-confidence.

Closure is deliberately a THIRD reversible predicate alongside eligibility and
the geo-fence, not a lifecycle change and not a soft-delete: a venue that
reopens gets a newer ordinary review, the detector clears the signal, and the
venue returns to serving on the next projection with no operator action. This
mirrors the reasoning in 0009 that made eligibility a dynamic view rather than a
destructive sweep.

Only `confidence = 'high'` excludes. A closure claim contradicted by a
near-contemporaneous ordinary review is recorded as 'low' for operator review
and does not change serving — the same high/low contract
`venue_eligibility.EligibilityResult` already uses.

The view body below is 0015's, unchanged except for the appended closure
predicate; `tests/test_eligibility_serving_view_parity.py` pins it against the
Python evaluator.

Revision ID: 0019_venue_closure_signal
Revises: 0018_vibe_attributes_primary_type_locked
Create Date: 2026-07-26
"""
from alembic import op
from sqlalchemy import text

revision = "0019_venue_closure_signal"
down_revision = "0018_vibe_attributes_primary_type_locked"
branch_labels = None
depends_on = None


CREATE_TABLE = r"""
CREATE TABLE IF NOT EXISTS admin.venue_closure_signal (
  venue_id              text PRIMARY KEY,
  closed                boolean     NOT NULL DEFAULT false,
  reason                text,
  confidence            text        NOT NULL DEFAULT 'high'
                                    CHECK (confidence IN ('high', 'low')),
  evidence_publish_time text,
  matched_phrase        text,
  detected_at           timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);

-- The serving view's predicate only ever looks for closed high-confidence rows.
CREATE INDEX IF NOT EXISTS venue_closure_signal_serving_idx
  ON admin.venue_closure_signal (venue_id)
  WHERE closed AND confidence = 'high';
"""

# 0015's view body + the closure predicate as the final AND.
VIEW_WITH_CLOSURE = r"""
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
                       * pow(sin(radians(g.lng - c.lng) / 2), 2))) <= c.radius_km))
  -- Closure (reversible): a venue whose newest review reports it permanently
  -- closed leaves serving. Absent signal = servable, so this is fail-open and
  -- an empty table changes nothing.
  AND NOT EXISTS (
        SELECT 1 FROM admin.venue_closure_signal cs
        WHERE cs.venue_id = g.venue_id
          AND cs.closed
          AND cs.confidence = 'high');
"""

# 0015's CREATE_VIEW_CIRCLES body verbatim, for the downgrade path.
VIEW_WITHOUT_CLOSURE = r"""
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


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS admin;"))
    for statement in filter(None, (s.strip() for s in CREATE_TABLE.split(";"))):
        conn.execute(text(statement))
    conn.execute(text(VIEW_WITH_CLOSURE))


def downgrade() -> None:
    conn = op.get_bind()
    # Restore the view first: it depends on the table.
    conn.execute(text(VIEW_WITHOUT_CLOSURE))
    conn.execute(text("DROP TABLE IF EXISTS admin.venue_closure_signal;"))
