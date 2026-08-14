"""Add `venues.reviews_deep` — the deep review corpus's RDS system of record.
See plans/260813_deep-review-corpus.md.

Chains from 0039_event_merge_suggestions, the current head (verified against
migrations/versions/ directly rather than trusted from the plan text — see
0030_crawl_target.py's own docstring for why that verification step exists:
a plan's stated chain reference can go stale between being written and being
executed). This migration adds exactly ONE new table, in the existing
`venues` schema, and touches nothing 0001-0039 own.

## `venues.reviews_deep`

Same shape as every other enrichment table in this schema (venue_id PK,
payload jsonb, deleted_at, updated_at) — see `venues.menu_data` /
`venues.vibe_profile` in 0001_baseline_schemas.py, which this is
structurally identical to. `payload` holds the full `VenueReviewsDeep`
model (venue_id, the full review list, window_days, fetched_at,
oldest/newest_publish_time, truncated) — RDS is the system of record for
the WHOLE corpus; the Redis projection carries only a bounded, newest-first
slice (`reviews_deep_projection_max`), never the full payload this table
stores.

Deliberately a SEPARATE table from `google_places.reviews`, not a widened
column on it: the plan's Non-goals are explicit that the existing free
5-review Places path (`google_places.reviews`, `venue_reviews_v1`, and the
app's `GET /venues/{id}` response) must stay byte-identical. Folding the
deep corpus into the same row would make that impossible to guarantee.

Additive only: one new table, no existing table touched. Downgrade drops it
— safe, because the deep corpus is independently re-fetchable from Apify
(at a real dollar cost, which is exactly why the two budget gates in
`DeepReviewCrawlService` exist) and nothing outside this feature reads it.

Revision ID: 0040_reviews_deep
Revises: 0039_event_merge_suggestions
"""
from alembic import op

revision = "0040_reviews_deep"
down_revision = "0039_event_merge_suggestions"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE IF NOT EXISTS venues.reviews_deep (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS venues.reviews_deep;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
