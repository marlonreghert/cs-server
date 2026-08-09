"""Add `events.crawl_target.reels_results_limit` and
`.reels_seed_results_limit` — per-target REELS-SPECIFIC overrides of the
two result caps 0031 introduced, requested by the operator: posts and
reels are already separate billed actor runs with independent cursors, so
one shared cap over-fetches the higher-volume stream and starves the
other. A venue posting three times a day and once a week to Reels is
served badly by a single number.

Deliberately NOT paired with new settings-level defaults
(`crawl_default_reels_*`). Considered and rejected: three months of reels
is far fewer items than three months of posts, so the existing seed
default (200) will rarely bind on reels anyway, and two more settings
would be config sprawl bought against no observed need. Instead, both
columns FALL BACK — resolved by
`app.services.instagram_crawl_service.resolve_results_limit` inside
`ScheduledInstagramCrawlService._run_stream` when `stream == "reels"`:

    reels-specific column -> the existing posts column (`results_limit` /
    `seed_results_limit`) -> the existing settings-level default.

A target that sets neither new column behaves EXACTLY as it does today —
this is additive-with-fallback, not a new required decision. `resolve_
results_limit` is a pure function (no target/stream logic duplicated
between the read model and the actual crawl), used both by `_run_stream`
itself and by `admin_crawl_router.py`'s `CrawlTargetOut` to expose the
FOUR effective caps (posts steady/seed, reels steady/seed) a console needs
to compute a target's true worst-case cost without re-implementing the
fallback chain.

Chains from 0031_crawl_target_seed_cap, the current head. Nullable, no
default, mirroring `results_limit`/`seed_results_limit`'s own convention:
NULL means "fall through to the next link in the chain," not zero and not
"disabled." Existing rows get NULL — deliberately, so nothing about an
existing target's resolved cap changes the moment this migration runs.

Additive only: two new nullable columns, no index, no constraint, no
backfill. Downgrade drops exactly them — safe, since `resolve_results_
limit` already treats an absent/NULL override as "fall through," so
losing the column is indistinguishable from every row having NULL in it,
which is the pre-migration state anyway.

Revision ID: 0032_crawl_target_reels_caps
Revises: 0031_crawl_target_seed_cap
"""
from alembic import op

revision = "0032_crawl_target_reels_caps"
down_revision = "0031_crawl_target_seed_cap"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.crawl_target
  ADD COLUMN IF NOT EXISTS reels_results_limit integer,
  ADD COLUMN IF NOT EXISTS reels_seed_results_limit integer;
"""

DOWNGRADE = r"""
ALTER TABLE events.crawl_target
  DROP COLUMN IF EXISTS reels_results_limit,
  DROP COLUMN IF EXISTS reels_seed_results_limit;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
