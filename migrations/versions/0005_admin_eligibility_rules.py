"""normalized admin eligibility rules + observability views (Ex2 expand)

Breaks the venue_eligibility JSON blob into admin.eligibility_rule rows (one row
per block-list entry) so a single rule is a one-row edit. Backfilled from the
existing override blob; normalization mirrors EligibilityConfig.from_dict
(BestTime types upper, Google types + name keywords lower; the blocked_name_keywords
alias folds into hard). Only categories PRESENT in the blob emit rows — absent
categories stay on the hardcoded defaults. Also adds the effect views.

EXPAND only: admin.admin_config (incl. the venue_eligibility blob) is retained as
the rollback baseline and reassembled into the Redis mirror by the app; it is
dropped by the later batched contract. See plans/260605_rds-schema-normalization.md.

Revision ID: 0005_admin_eligibility_rules
Revises: 0004_address_table
Create Date: 2026-06-06
"""
from alembic import op

revision = "0005_admin_eligibility_rules"
down_revision = "0004_address_table"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE IF NOT EXISTS admin.eligibility_rule (
  rule_type  text NOT NULL,
  value      text NOT NULL,
  updated_by text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (rule_type, value)
);
CREATE INDEX IF NOT EXISTS ix_eligibility_rule_type ON admin.eligibility_rule (rule_type);

-- Backfill from the existing venue_eligibility override blob (if any).
INSERT INTO admin.eligibility_rule (rule_type, value)
SELECT 'blocked_venue_type', upper(e.v)
FROM admin.admin_config c,
     jsonb_array_elements_text(COALESCE(c.value->'blocked_venue_types', '[]'::jsonb)) AS e(v)
WHERE c.key = 'venue_eligibility' ON CONFLICT DO NOTHING;

INSERT INTO admin.eligibility_rule (rule_type, value)
SELECT 'blocked_google_type', lower(e.v)
FROM admin.admin_config c,
     jsonb_array_elements_text(COALESCE(c.value->'blocked_google_types', '[]'::jsonb)) AS e(v)
WHERE c.key = 'venue_eligibility' ON CONFLICT DO NOTHING;

INSERT INTO admin.eligibility_rule (rule_type, value)
SELECT 'hard_blocked_name_keyword', lower(e.v)
FROM admin.admin_config c,
     jsonb_array_elements_text(COALESCE(c.value->'hard_blocked_name_keywords', '[]'::jsonb)) AS e(v)
WHERE c.key = 'venue_eligibility' ON CONFLICT DO NOTHING;

INSERT INTO admin.eligibility_rule (rule_type, value)
SELECT 'ambiguous_name_keyword', lower(e.v)
FROM admin.admin_config c,
     jsonb_array_elements_text(COALESCE(c.value->'ambiguous_name_keywords', '[]'::jsonb)) AS e(v)
WHERE c.key = 'venue_eligibility' ON CONFLICT DO NOTHING;

-- Operator alias: a single blocked_name_keywords list is treated as hard.
INSERT INTO admin.eligibility_rule (rule_type, value)
SELECT 'hard_blocked_name_keyword', lower(e.v)
FROM admin.admin_config c,
     jsonb_array_elements_text(COALESCE(c.value->'blocked_name_keywords', '[]'::jsonb)) AS e(v)
WHERE c.key = 'venue_eligibility' ON CONFLICT DO NOTHING;

-- ── observability views: see the *effect* of the config, not just the rules ──
CREATE OR REPLACE VIEW admin.v_blocked_google_type_effect AS
SELECT r.value AS google_type,
       count(va.venue_id)                                        AS venues_with_type,
       count(*) FILTER (WHERE v.lifecycle_status = 'active')     AS active,
       count(*) FILTER (WHERE v.lifecycle_status = 'deprecated') AS deprecated
FROM admin.eligibility_rule r
LEFT JOIN google_places.vibe_attributes va
       ON va.google_primary_type = r.value AND va.deleted_at IS NULL
LEFT JOIN venues.venue v ON v.venue_id = va.venue_id
WHERE r.rule_type = 'blocked_google_type'
GROUP BY r.value;

CREATE OR REPLACE VIEW admin.v_rejection_reason_effect AS
SELECT rr.code, rr.category, rr.description,
       count(v.venue_id)    AS deprecated_venues,
       max(v.deprecated_at) AS last_deprecated_at
FROM admin.rejection_reason rr
LEFT JOIN venues.venue v
       ON v.deprecated_reason = rr.code AND v.lifecycle_status = 'deprecated'
GROUP BY rr.code, rr.category, rr.description;
"""

DOWNGRADE = r"""
DROP VIEW IF EXISTS admin.v_rejection_reason_effect;
DROP VIEW IF EXISTS admin.v_blocked_google_type_effect;
DROP TABLE IF EXISTS admin.eligibility_rule;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
