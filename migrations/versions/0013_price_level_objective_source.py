"""price_level objective source — 1..4/NULL tier + raw audit signals

Re-sources the served price tier so `venues.venue.price_level` is an int 1..4 or
NULL (unknown), never 0. The legacy single 0..4 int rendered `0` (565/1255 active
venues, incl. clearly-expensive Vasto Restaurante Recife) as the cheapest tier.

This migration:
  1. Adds the raw, auditable price-signal columns (promoted, projector-rebuildable):
       price_range          jsonb  -- structured Google range {currency,min,max}
       google_price_level   text   -- raw Google priceLevel enum string
       besttime_price_level int    -- raw BestTime price (derivation step 3)
       price_level_source   text   -- google_enum | google_range | besttime | null
  2. Data-migrates every existing `price_level = 0` to NULL.
  3. Adds a CHECK guard (price_level IS NULL OR BETWEEN 1 AND 4), AFTER the 0->NULL
     step so pre-existing 0s do not violate it.

OPERATIONS — take an RDS snapshot BEFORE applying. The `0 -> NULL` data step is
IRREVERSIBLE (the original 0 values are lost): the snapshot is the only true
rollback. Slightly-stale serving data after a restore is acceptable — the BestTime
pipelines overwrite live data on their next runs and the projector re-asserts the
Redis serving projection from RDS, so a restore self-heals within a cycle.

Deploy does NOT run alembic (CI-only) — apply this manually on RDS, e.g.
`docker exec vibes_bot-cs-server-1 alembic upgrade head`.

`downgrade()` drops the four new columns + the CHECK constraint. It does NOT and
cannot restore the migrated 0 values — rely on the pre-migration snapshot for a
true revert.

Revision ID: 0013_price_level_objective_source
Revises: 0012_engagement_app_session_day
Create Date: 2026-06-25
"""
from alembic import op

revision = "0013_price_level_objective_source"
down_revision = "0012_engagement_app_session_day"
branch_labels = None
depends_on = None

UPGRADE = r"""
ALTER TABLE venues.venue
  ADD COLUMN IF NOT EXISTS price_range          jsonb,
  ADD COLUMN IF NOT EXISTS google_price_level   text,
  ADD COLUMN IF NOT EXISTS besttime_price_level int,
  ADD COLUMN IF NOT EXISTS price_level_source   text;

-- Eliminate the legacy "0 = unknown rendered as cheapest" tier. IRREVERSIBLE.
UPDATE venues.venue SET price_level = NULL WHERE price_level = 0;

-- Hard guard backing the derivation helper: the served tier is 1..4 or NULL.
-- Added AFTER the 0 -> NULL update so existing rows never violate it.
ALTER TABLE venues.venue
  ADD CONSTRAINT price_level_1_4_or_null
  CHECK (price_level IS NULL OR price_level BETWEEN 1 AND 4);
"""

DOWNGRADE = r"""
ALTER TABLE venues.venue DROP CONSTRAINT IF EXISTS price_level_1_4_or_null;
ALTER TABLE venues.venue
  DROP COLUMN IF EXISTS price_range,
  DROP COLUMN IF EXISTS google_price_level,
  DROP COLUMN IF EXISTS besttime_price_level,
  DROP COLUMN IF EXISTS price_level_source;
-- NOTE: the 0 -> NULL data step is not reversible; restore the pre-migration RDS
-- snapshot to recover the original price_level integers.
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
