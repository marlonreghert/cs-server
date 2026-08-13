"""Add `events.event_merge_suggestion` and `events.post_item.superseded_by`
— the persistence §C/§E of plans/260812_event-dedup-fuzzy-title.md need.

Chains from 0037_date_interpretation, the current head — this migration adds
ONE new table and ONE nullable column, and touches nothing else 0022-0037
own.

## `events.event_merge_suggestion`

One row per fuzzy-title/shared-lineup pairwise decision this feature ever
acts on or surfaces — SUGGEST-band pairs (`decision='pending'`, awaiting an
operator) and every AUTO-band absorption this feature itself performs
(`decision='auto_merged'`, written by the SAME code path that performs the
absorption, in the SAME transaction shape `insert_event`/`update_event`
already use — never a second, drifting record of what happened). Plan §C
asks for "a table for suggested merges (event_id, candidate_event_id, the
two distinctive sets as evidence, created/decided timestamps, decision)" —
this is that table, extended with exactly what §E's reversibility requires
on top: `moved_source_ids` (which `events.post_item_source` rows this merge
reattached, so a reversal moves back precisely those and none of the
canonical's own) and `absorbed_status_before` (the duplicate's own status at
absorption time, so a reversal restores it exactly rather than guessing
`pending_review`). Both are NULL for a pending suggestion — there is nothing
to reverse until an operator (or the pipeline) actually applies one.

`event_id` is the SURVIVING/candidate-canonical side; `candidate_event_id`
is the row this decision is ABOUT absorbing. Both are plain `text`
references into `events.post_item(post_item_id)` — no FK is declared: a
suggestion can legitimately outlive the row it evaluated (`ON DELETE
CASCADE` would need constant re-litigating for every future events.post_item
deletion path, and the ONLY hard-delete this repo's event pipeline performs
is the exact-identity merge's own `_finish_absorption(mode="delete")`, which
never creates a row here in the first place — see the module docstring of
app.services.event_merge). Declaring an FK anyway would buy nothing and risk
a future exact-identity delete failing on a stale suggestion row it was
never designed to know about.

`band` (`'auto'` | `'suggest'`) records WHICH band produced this row —
distinct from `decision`, which records what happened to it since
(`'pending'` | `'auto_merged'` | `'applied'` | `'rejected'` | `'reversed'`).
`reasons` is a jsonb array of `'title_containment'` / `'shared_lineup'` (one
or both — plan §B2: "Signal independence: a pair passing containment but
not lineup auto-merges, a pair passing lineup but not containment
auto-merges").

Additive only: one new table, one new nullable column on
`events.post_item`. Downgrade drops both — safe, because every row either
table holds is DERIVED (recomputable from `events.post_item`/
`events.post_item_source` by re-running the merge pass) except
`superseded_by` itself, which the downgrade also drops; nothing outside this
feature reads either.

Revision ID: 0038_event_merge_suggestions
Revises: 0037_date_interpretation
"""
from alembic import op

revision = "0038_event_merge_suggestions"
down_revision = "0037_date_interpretation"
branch_labels = None
depends_on = None


_CREATE_TABLE = r"""
CREATE TABLE IF NOT EXISTS events.event_merge_suggestion (
  suggestion_id                text PRIMARY KEY,
  event_id                     text NOT NULL,
  candidate_event_id           text NOT NULL,
  band                         text NOT NULL,
  reasons                      jsonb NOT NULL DEFAULT '[]'::jsonb,
  event_distinctive_words      jsonb,
  candidate_distinctive_words  jsonb,
  shared_lineup_names          jsonb,
  decision                     text NOT NULL DEFAULT 'pending',
  moved_source_ids             jsonb,
  absorbed_status_before       text,
  created_at                   timestamptz NOT NULL DEFAULT now(),
  decided_at                   timestamptz,
  decided_by                   text
);

CREATE INDEX IF NOT EXISTS ix_event_merge_suggestion_event_id
  ON events.event_merge_suggestion (event_id);
CREATE INDEX IF NOT EXISTS ix_event_merge_suggestion_candidate_event_id
  ON events.event_merge_suggestion (candidate_event_id);
CREATE INDEX IF NOT EXISTS ix_event_merge_suggestion_decision
  ON events.event_merge_suggestion (decision);
"""

_ADD_SUPERSEDED_BY = r"""
ALTER TABLE events.post_item
  ADD COLUMN IF NOT EXISTS superseded_by text;
"""

_DROP_SUPERSEDED_BY = r"""
ALTER TABLE events.post_item
  DROP COLUMN IF EXISTS superseded_by;
"""

_DROP_TABLE = r"""
DROP INDEX IF EXISTS events.ix_event_merge_suggestion_decision;
DROP INDEX IF EXISTS events.ix_event_merge_suggestion_candidate_event_id;
DROP INDEX IF EXISTS events.ix_event_merge_suggestion_event_id;
DROP TABLE IF EXISTS events.event_merge_suggestion;
"""


def upgrade() -> None:
    op.execute(_CREATE_TABLE)
    op.execute(_ADD_SUPERSEDED_BY)


def downgrade() -> None:
    op.execute(_DROP_TABLE)
    op.execute(_DROP_SUPERSEDED_BY)
