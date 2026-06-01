"""baseline: venue system-of-record schemas

Creates the five domain schemas (venues, besttime, google_places, instagram,
admin) plus engagement and audit. Enrichment/label tables are never
hard-deleted: removals set deleted_at and every write appends an
audit.enrichment_history row. Live busyness is intentionally NOT modeled
(Redis-only, ephemeral). See plans/rds_system_of_record_01_06_26.md.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-01
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


DDL = r"""
-- ── venues ────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS venues;
CREATE TABLE venues.venue (
  venue_id               text PRIMARY KEY,
  venue_name             text NOT NULL DEFAULT '',
  venue_address          text NOT NULL DEFAULT '',
  venue_lat              double precision NOT NULL,   -- kept so Redis geo index is rebuildable
  venue_lng              double precision NOT NULL,
  venue_type             text,
  price_level            int,
  rating                 double precision,
  reviews                int,
  forecast               boolean NOT NULL DEFAULT false,
  processed              boolean NOT NULL DEFAULT false,
  lifecycle_status       text NOT NULL DEFAULT 'active',
  deprecated_reason      text,
  deprecated_source      text,
  deprecated_at          timestamptz,
  google_business_status text,
  payload                jsonb NOT NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_venue_lifecycle ON venues.venue (lifecycle_status);
CREATE INDEX ix_venue_deprecated_reason ON venues.venue (deprecated_reason);

CREATE TABLE venues.vibe_profile (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE venues.menu_data (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE venues.menu_photos (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now());

-- ── besttime (weekly forecast + live busyness) ──────────────────────────────
CREATE SCHEMA IF NOT EXISTS besttime;
CREATE TABLE besttime.weekly_forecast (
  venue_id   text NOT NULL REFERENCES venues.venue(venue_id),
  day_int    int NOT NULL CHECK (day_int BETWEEN 0 AND 6),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (venue_id, day_int));
-- Live busyness: current-state snapshot, refreshed by the live cron and
-- upserted here so all data lives in RDS (Redis is purely the read interface).
-- High-churn + self-healing, so it is NOT append-only-historied (like photos).
CREATE TABLE besttime.live_forecast (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now());

-- ── google_places ───────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS google_places;
CREATE TABLE google_places.vibe_attributes (
  venue_id            text PRIMARY KEY REFERENCES venues.venue(venue_id),
  google_place_id     text,
  google_primary_type text,
  payload             jsonb NOT NULL,
  deleted_at          timestamptz,
  updated_at          timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_vibe_attrs_google_type ON google_places.vibe_attributes (google_primary_type);
CREATE TABLE google_places.opening_hours (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now());
-- photos: current-value only (URLs expire; excluded from deep history)
CREATE TABLE google_places.photos (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE google_places.reviews (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now());

-- ── instagram ───────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS instagram;
CREATE TABLE instagram.handle (
  venue_id         text PRIMARY KEY REFERENCES venues.venue(venue_id),
  instagram_handle text,
  payload          jsonb NOT NULL,
  deleted_at       timestamptz,
  updated_at       timestamptz NOT NULL DEFAULT now());
CREATE TABLE instagram.posts (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now());

-- ── admin ─────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS admin;
CREATE TABLE admin.admin_config (
  key        text PRIMARY KEY,
  value      jsonb NOT NULL,
  updated_by text,
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE admin.rejection_reason (
  code        text PRIMARY KEY,
  description text NOT NULL,
  category    text);

-- ── engagement (vibes_bot user data, pseudonymized) ─────────────────────────
CREATE SCHEMA IF NOT EXISTS engagement;
CREATE TABLE engagement.favorite (
  user_pseudo text NOT NULL,                 -- HMAC(user_id); raw id never stored
  venue_id    text NOT NULL REFERENCES venues.venue(venue_id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  deleted_at  timestamptz,                   -- un-favorite = soft-delete
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_pseudo, venue_id));
CREATE INDEX ix_favorite_venue ON engagement.favorite (venue_id);
-- hot_likes recorded as append-only events for durable metrics
CREATE TABLE engagement.hot_like_event (
  id          bigserial PRIMARY KEY,
  user_pseudo text NOT NULL,
  venue_id    text NOT NULL REFERENCES venues.venue(venue_id),
  created_at  timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_hot_like_event_venue ON engagement.hot_like_event (venue_id, created_at);

-- ── audit (append-only history for expensive derived labels) ────────────────
CREATE SCHEMA IF NOT EXISTS audit;
CREATE TABLE audit.enrichment_history (
  id          bigserial PRIMARY KEY,
  schema_name text NOT NULL,
  table_name  text NOT NULL,
  venue_id    text NOT NULL,
  payload     jsonb NOT NULL,
  operation   text NOT NULL,                 -- upsert | soft_delete
  written_at  timestamptz NOT NULL DEFAULT now());
CREATE INDEX ix_enrichment_history_lookup
  ON audit.enrichment_history (schema_name, table_name, venue_id, written_at);
"""

SEED_REJECTION_REASONS = r"""
INSERT INTO admin.rejection_reason (code, description, category) VALUES
  ('ineligible_empty_name',          'Venue has no name',                         'eligibility'),
  ('ineligible_name_keyword',        'Name matches a blocked keyword',            'eligibility'),
  ('ineligible_besttime_type',       'BestTime type is blocked',                  'eligibility'),
  ('ineligible_google_type',         'Google Places type is blocked',             'eligibility'),
  ('google_places_closed_permanently','Google reports the venue permanently closed','closure')
ON CONFLICT (code) DO NOTHING;
"""


def upgrade() -> None:
    op.execute(DDL)
    op.execute(SEED_REJECTION_REASONS)


def downgrade() -> None:
    for schema in (
        "audit",
        "engagement",
        "admin",
        "instagram",
        "google_places",
        "besttime",
        "venues",
    ):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
