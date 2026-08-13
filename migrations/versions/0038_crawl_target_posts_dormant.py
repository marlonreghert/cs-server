"""Add `events.crawl_target.posts_dormant` — whether the posts stream's most
recent answer was the DORMANT signature (account answers, nothing in the
lookback window, not blocked) rather than a genuine failure. See plans/
260813_dormant-vs-broken-targets.md §A.

## Why this needs a new column (checked the two existing candidates first)

`last_failure_kind` (migration 0036_crawl_error_visibility) only ever holds a
member of `FAILURE_OUTCOMES` (`blocked`/`handle_not_found`/`failed`) and is
NEVER cleared by a later run that did not fail — it answers "what was the
LAST failure and when," on purpose, even if that failure was several runs
ago. Dormancy is explicitly NOT a failure (plan §A: "It is not a failure. It
must not increment `consecutive_failures` and must not disable a target"),
and it MUST clear itself the moment a target starts producing again (the
plan's "Stop reporting dormancy once the target produces a post" scenario) —
both properties are the opposite of what `last_failure_kind` is for. Reusing
it would either mean writing a non-failure value into a column whose every
other reader assumes FAILURE_OUTCOMES membership, or leaving a stale
"dormant" reading standing after a target starts failing for a real reason —
both wrong.

`posts_never_seeded` (app.services.instagram_crawl_service, derived, never
stored) is `cursor_posts_at IS NULL AND last_run_at IS NOT NULL` — true
whether the cursor never advanced because the account is dormant, because it
keeps getting blocked, or because every run has failed outright. It cannot
answer WHICH of those is true, and that is exactly the gap this plan closes:
an operator today cannot tell a dormant target from a broken one without an
out-of-band probe. So `posts_never_seeded` stays exactly as it is (this
migration does not touch it) and dormancy is recorded ALONGSIDE it, not
folded into it.

Neither existing column can carry this signal, so a new one is genuinely
needed.

Nullable would be defensible (no back-fill can know history), but every
target's CURRENT dormancy state is exactly recomputed on every future crawl
run regardless of what it was before (see `run_target`), so there is no
"unknown" state worth representing — `NOT NULL DEFAULT false` mirrors
`crawl_reels`/`classify_images`'s own boolean-column style on this same
table (migration 0030_crawl_target) and reads every pre-existing row as "not
known to be dormant," the correct, harmless starting value: `run_target`
overwrites it with the real answer the next time each target actually runs.

Chains from 0037_date_interpretation, the current head.

Revision ID: 0038_crawl_target_posts_dormant
Revises: 0037_date_interpretation
"""
from alembic import op

revision = "0038_crawl_target_posts_dormant"
down_revision = "0037_date_interpretation"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.crawl_target
  ADD COLUMN IF NOT EXISTS posts_dormant boolean NOT NULL DEFAULT false;
"""

DOWNGRADE = r"""
ALTER TABLE events.crawl_target
  DROP COLUMN IF EXISTS posts_dormant;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
