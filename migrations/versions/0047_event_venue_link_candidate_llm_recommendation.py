"""Add `events.event_venue_link_candidate.llm_recommendation` —
plans/260913_dedup-agentic-mitigation-discovery.md Phase 4.

One nullable `jsonb` column on the EXISTING `event_venue_link_candidate`
table (migration `0024_promoter_accounts`), never a new table. Holds the
validated (`app.services.event_venue_advisor_validator.
validate_event_venue_recommendation`) advisory recommendation the
`event_venue_advisor_enabled` admin-config-gated pass may attach to one
ranked candidate row for an event still sitting in the `RESOLUTION_QUEUED`
state — never the recommendation's raw, unvalidated form, and never anything
this migration itself writes.

## Additive, nullable, no back-fill

Every existing row keeps `llm_recommendation IS NULL` after this migration —
there is nothing to derive it from historically, and NULL is exactly what
`GET /admin/events/review` already treats as "no recommendation yet" (see
`app.routers.admin_events_router.ReviewQueueItemOut`). No index: this column
is read by event_id (the existing `ix_event_venue_link_candidate_event`
index already serves that), never queried or filtered on its own value.

## Never touched by `replace_event_venue_link_candidates`

`app.dao.rds_venue_store.RdsVenueStore.replace_event_venue_link_candidates`
DELETEs and re-INSERTs a whole event's candidate rows every time the
resolution ladder re-runs (a later crawl of the same post). The INSERT this
migration's column is added alongside does not set `llm_recommendation`, so
it defaults NULL on every fresh row — a stale recommendation from a PRIOR
ladder run can never survive a re-resolution. It is written only by the new,
separate `set_event_venue_link_candidate_recommendation` DAO method the
advisor service calls after its own validation gate accepts an answer.

## Downgrade

Safe: the column is derived, advisory-only data with no other table or key
depending on it. Dropping it loses only in-flight, unactioned
recommendations, exactly like every other advisory annotation in this
codebase (`events.post_item.display_title`, `0046`, is the direct
precedent).

Revision ID: 0047_event_venue_link_candidate_llm_recommendation
Revises: 0046_event_display_title
"""
from alembic import op

revision = "0047_event_venue_link_candidate_llm_recommendation"
down_revision = "0046_event_display_title"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.event_venue_link_candidate
  ADD COLUMN IF NOT EXISTS llm_recommendation jsonb;
"""

DOWNGRADE = r"""
ALTER TABLE events.event_venue_link_candidate
  DROP COLUMN IF EXISTS llm_recommendation;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
