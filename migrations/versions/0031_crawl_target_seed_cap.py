"""Add `events.crawl_target.seed_results_limit` — a SEPARATE result cap for
a target's one-time seed crawl, distinct from `results_limit` (the
steady-state per-run cap). See plans/260809_scheduled-incremental-instagram-
crawl.md and the follow-up review that found this: `run_target` was
resolving ONE cap for every run —

    results_limit = int(target.get("results_limit") or self.config.default_results_limit)

— so a brand-new target with a null cursor asked Apify for three months of
history and then took only the first `results_limit` (default 10) of it.
The operator's own requirement was "a default config to get a huge amount
of posts starting from 3 months ago"; ten is not that. A venue posting
daily has ~90 posts in a 3-month window — the date bound was doing its job
and the shared cap was silently undoing it.

One cap cannot serve two jobs that want opposite values: steady state wants
a SMALL cap (a venue posts a handful of times between two scheduled fires,
and the cap exists to stop a prolific account from running away, not to
bound history depth); the seed wants a LARGE one (it happens once per
target, ever, and its whole purpose is depth). `seed_results_limit` is that
second cap, applied by `app.services.instagram_crawl_service.
ScheduledInstagramCrawlService._run_stream` ONLY when the relevant cursor
(`cursor_posts_at`/`cursor_reels_at`, independently per stream — the same
per-stream split §B/§E already established) is NULL — i.e., at most once
per target per stream, ever, exactly the seed crawl and never a steady-state
one.

Chains from 0030_crawl_target, the current head. Nullable, no default,
mirroring `results_limit`'s own convention on the same table: NULL means
"use the settings-level default" (`crawl_default_seed_results_limit`, 200 —
an operator-tunable starting point, not a fact), so a target need not
repeat the global default to use it. Existing rows get NULL — deliberately;
inventing a value for an already-seeded target would be inventing history
that already happened one way, not the other.

Additive only: one new nullable column, no index, no constraint. Downgrade
drops exactly it — safe, since it only ever governs the ONE-TIME seed crawl
of a target whose cursor is still null; a target that has already seeded
never reads it again regardless of whether the column exists.

Revision ID: 0031_crawl_target_seed_cap
Revises: 0030_crawl_target

(Revision id kept SHORT deliberately — `alembic_version.version_num` was
widened to 32 chars by migration 0011_widen_alembic_version, and the more
descriptive `0031_crawl_target_seed_results_limit` is 36, over the limit.
Caught here, offline, by tests/test_crawl_target_seed_cap_migration.py's
own `len(revision) <= 32` assertion — the same class of "only caught by a
real Postgres" identifier trap CLAUDE.md warns about for index/constraint
names, just one level up, at the revision id itself.)
"""
from alembic import op

revision = "0031_crawl_target_seed_cap"
down_revision = "0030_crawl_target"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.crawl_target
  ADD COLUMN IF NOT EXISTS seed_results_limit integer;
"""

DOWNGRADE = r"""
ALTER TABLE events.crawl_target
  DROP COLUMN IF EXISTS seed_results_limit;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
