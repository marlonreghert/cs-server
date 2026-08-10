"""Add `events.crawl_target.last_run_reels_fetched` and
`.last_run_reels_new` — the read model's per-run reels-overlap counts. See
plans/260810_stream-dedupe-and-venue-attribution.md §B.

**Correction to the plan while executing it**: the plan's §Implementation
Approach B and §Data/Config/And/API/Impact both describe THIS revision as
"flip `crawl_reels`'s column default to off" (`0033_crawl_reels_default_off`).
That default is ALREADY `false` — verified against `events.crawl_target`'s
very first migration (0030_crawl_target: `crawl_reels boolean NOT NULL
DEFAULT false`, unchanged by any later migration, confirmed both by `git log`
and by inspecting a freshly-migrated scratch Postgres) and against
`CrawlTargetCreate.crawl_reels: bool = False` in admin_crawl_router.py, which
the create endpoint always supplies explicitly. There is nothing to flip, so
this migration does not touch that column at all — see the PR for the full
note.

What §B genuinely still needs a migration for is the OTHER half of the same
paragraph: "record, per run, how many reels results were already present in
the posts stream and how many were new, and expose it on the read model so
the console can show 'reels: 16 fetched, 3 new'." That is new persisted
state, not something `resolve_results_limit` or any existing column can
derive, so it needs its own two nullable columns — this revision.

Nullable, no default, no back-fill: a target that has never run reels (or
never run at all) reads both as NULL, which the read model treats as "not yet
measured," never as zero. `run_target` (app/services/instagram_crawl_service.py)
writes both only on a run whose reels stream actually executed; every other
existing row is untouched.

Chains from 0032_crawl_target_reels_caps, the current head.

Revision ID: 0033_crawl_target_reels_overlap
Revises: 0032_crawl_target_reels_caps
"""
from alembic import op

revision = "0033_crawl_target_reels_overlap"
down_revision = "0032_crawl_target_reels_caps"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.crawl_target
  ADD COLUMN IF NOT EXISTS last_run_reels_fetched integer,
  ADD COLUMN IF NOT EXISTS last_run_reels_new integer;
"""

DOWNGRADE = r"""
ALTER TABLE events.crawl_target
  DROP COLUMN IF EXISTS last_run_reels_fetched,
  DROP COLUMN IF EXISTS last_run_reels_new;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
