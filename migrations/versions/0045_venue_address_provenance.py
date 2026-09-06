"""Add per-field provenance columns to `venues.address` —
plans/260906_address-components-backfill.md, Phase 2.

`update_venue_address_components` (added yesterday by migration-adjacent
commit `200f9f0`, plans/260905_events-serving-projection.md Phase 2) is a
BLIND, per-field COALESCE: any non-empty incoming value always overwrites
any prior value, with no way to know whether a stored value came from an
operator, Google, or a parser. That is correct for Google re-enriching
itself (its own test suite asserts a fresh non-null answer always wins) but
becomes unsafe the moment a SECOND, lower-trust writer (this plan's text
parser) exists: a parsed guess written through that same blind method would
permanently block Google's later, better answer.

This migration adds four nullable text columns, one per structured
`venues.address` field, each constrained to the three provenance tiers this
plan defines (`operator` > `google` > `parsed`; `llm` is a fourth,
lower-still tier Phase 5 may add later, not created by this migration).
`CHECK (col IS NULL OR col IN (...))` matches this repo's established
raw-SQL-text-plus-CHECK-constraint style for an enum-like text column
(`0042_venue_add_job_run.py`'s `job_type`/`status`) rather than a Postgres
enum type.

Every existing row gets NULL sources — correct, not a compromise: nothing
has ever been written under this provenance regime, and the production
census (this plan's own Evidence section) confirms every one of the four
data columns is null on all 3,600 rows today, so there is nothing to
backfill sources FOR.

Additive and nullable only: four new columns on an existing table, no
existing column touched, no index, no other table changed. Downgrade drops
exactly those four columns. Verified against a real, throwaway `postgres:16`
container while writing this migration (this repo's own established
practice for a migration whose real-Postgres fidelity isn't exercised by
the offline fakes — see `tests/test_operator_edited_fields_migration.py`'s
docstring for the identical posture): `alembic upgrade head` from a clean
0044 baseline -> confirmed all four `*_source` columns exist, nullable, with
the CHECK constraint rejecting an out-of-set value and accepting NULL and
each of the three tiers -> `downgrade -1` -> confirmed all four columns gone
and every 0044 column/constraint/index untouched -> `upgrade head` again,
confirming the round trip is clean.

Revision ID: 0045_venue_address_provenance
Revises: 0044_event_flyer_media
"""
from alembic import op

revision = "0045_venue_address_provenance"
down_revision = "0044_event_flyer_media"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE venues.address
  ADD COLUMN IF NOT EXISTS street_source text,
  ADD COLUMN IF NOT EXISTS neighborhood_source text,
  ADD COLUMN IF NOT EXISTS city_source text,
  ADD COLUMN IF NOT EXISTS postal_code_source text;

ALTER TABLE venues.address
  ADD CONSTRAINT ck_venue_address_street_source
    CHECK (street_source IS NULL OR street_source IN ('operator', 'google', 'parsed')),
  ADD CONSTRAINT ck_venue_address_neighborhood_source
    CHECK (neighborhood_source IS NULL OR neighborhood_source IN ('operator', 'google', 'parsed')),
  ADD CONSTRAINT ck_venue_address_city_source
    CHECK (city_source IS NULL OR city_source IN ('operator', 'google', 'parsed')),
  ADD CONSTRAINT ck_venue_address_postal_code_source
    CHECK (postal_code_source IS NULL OR postal_code_source IN ('operator', 'google', 'parsed'));
"""

DOWNGRADE = r"""
ALTER TABLE venues.address
  DROP CONSTRAINT IF EXISTS ck_venue_address_postal_code_source,
  DROP CONSTRAINT IF EXISTS ck_venue_address_city_source,
  DROP CONSTRAINT IF EXISTS ck_venue_address_neighborhood_source,
  DROP CONSTRAINT IF EXISTS ck_venue_address_street_source;

ALTER TABLE venues.address
  DROP COLUMN IF EXISTS postal_code_source,
  DROP COLUMN IF EXISTS city_source,
  DROP COLUMN IF EXISTS neighborhood_source,
  DROP COLUMN IF EXISTS street_source;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
