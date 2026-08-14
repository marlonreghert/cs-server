"""Add `events.crawl_target.reels_seeded_at` — whether the reels stream has
reached its one-time seed to COMPLETION, as its own fact separate from
`cursor_reels_at`. See plans/260814_seeded-state-and-config-validation.md §A.

## Why this needs a new column (the defect, restated)

`reels_already_seeded` used to gate on `cursor_reels_at` alone — set only
when a reels stream returns `OUTCOME_SUCCESS` (real kept items). A stream
that runs and legitimately finds NOTHING returns early with no `new_cursor`
at all, so `cursor_reels_at` stays NULL — the exact same NULL state a
genuine FAILURE leaves behind. `reels_already_seeded` could not tell "we
never tried" apart from "we tried and there was nothing," so an empty
account's one-time seed was re-purchased on every subsequent scheduled
crawl, forever (measured in production, 2026-08-13: `burburinhobar` and
`downtownrecife`, both enabled, both zero reels, both re-billing three times
a week).

`cursor_reels_at` cannot be widened to carry both meanings — it is written
FROM the newest reel's own timestamp (`_newest_timestamp`), and an empty
result has no timestamp to write. Two questions ("what did we reach" vs
"has the seed happened") need two fields; `cursor_reels_at` keeps its
existing, unchanged meaning and stays NULL when nothing was reached.

## Why nullable, no backfill here

Nullable, `NOT NULL DEFAULT` intentionally NOT used: unlike
`posts_dormant` (migration 0038, recomputed fresh on every future run
regardless of its prior value), whether a target's reels seed already
completed is NOT recomputable from anything else this migration can see —
that is exactly the four already-stuck production targets' problem, and
exactly why plans/260814_seeded-state-and-config-validation.md §B is a
separate, reviewable, operator-run script (`scripts/backfill_reels_seeded.py`)
rather than a data migration alongside this schema change. Every
pre-existing row reads as `reels_seeded_at IS NULL` — for a target whose
reels legitimately already succeeded with real items, that is harmless
(`cursor_reels_at` is already set and `run_target` records
`reels_seeded_at` again, in the SAME commit, the very next time that
target's reels stream would even be attempted — which for that target is
never, since its cursor already gates it out) — but for the two truly
stuck targets, only the backfill script (run separately, by the operator,
after this migration deploys) fixes their standing state.

Chains from 0040_reviews_deep, the current head (verified against
migrations/versions/ directly, not trusted from a plan's stated chain — see
0030_crawl_target.py's own docstring for why that verification step
exists). Additive only: one new column, no existing column touched.

Revision ID: 0041_crawl_target_reels_seeded
Revises: 0040_reviews_deep
"""
from alembic import op

revision = "0041_crawl_target_reels_seeded"
down_revision = "0040_reviews_deep"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.crawl_target
  ADD COLUMN IF NOT EXISTS reels_seeded_at timestamptz;
"""

DOWNGRADE = r"""
ALTER TABLE events.crawl_target
  DROP COLUMN IF EXISTS reels_seeded_at;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
