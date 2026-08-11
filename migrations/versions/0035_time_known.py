"""Add `events.post_item.time_known` — whether an item's `starts_at` clock
time was actually read from the post, or is a defaulted midnight standing in
for "no time was stated".

See plans/260811_expose-time-known.md.

## Why this exists

`app.services.event_date_resolver.resolve_event_datetime` already computes
this (`time_known: bool`, "a stated '00h' and a defaulted midnight land on
the exact same `starts_at` instant") and both extraction paths already fold
it into `raw_extraction["time_known"]`. But `raw_extraction` is a per-SOURCE
jsonb blob (`events.post_item_source`), and `EventOut(**row)` — the admin
API's response model — silently discards any key it does not declare.
Production reads `03:00:00Z` (midnight in Recife) for nearly every item, and
there is no way to tell "we don't know" from a genuine midnight show by
looking at the clock.

## Why a real column, not a serve-time JSONB read

`_EVENT_SELECT`'s `ps` (primary source) picks the MOST RECENTLY SEEN
source's `raw_extraction` — correct for the four legacy per-post scalars it
already derives (source_handle, cover_photo_key, ...), but WRONG for
`time_known` once a later post's fields get merged into an EARLIER post's
still-standing `starts_at`. `app.services.event_reconciliation.
event_field_is_absent`'s starts_at-specific rule already treats a fresh
`starts_at` as absent whenever ITS OWN source did not know the time — so a
later, time-unaware post never overwrites an earlier, time-aware one's
`starts_at`. Reading `time_known` from the primary source's raw_extraction at
serve time would then report the LATER post's "unknown" against the EARLIER
post's still-known `starts_at` — wrong, not just indirect. A real column,
written from the SAME resolver output as `starts_at` at every write site
(never re-derived from a blob), keeps the two in lockstep by construction.

It is deliberately NOT a member of `app.services.event_reconciliation.
PROTECTABLE_EVENT_FIELDS` — an earlier version of this plan put it there,
alongside `is_recurring`, and a genuine-red BDD run over the sibling ticket-
info/attractions feature caught the defect: comparing `time_known` as an
ordinary independent scalar flags `REVIEW_REASON_DIVERGES_FROM_CONFIRMED`
purely because two posts disagree on whether a time was stated, even when
`starts_at` itself never changed (event_field_is_absent's own starts_at
rule already treats a later, time-unaware post as absent when the existing
row already knows a time — `changed` never touches `starts_at` there, so an
independent `time_known` "disagreement" would contradict a row nothing else
about this decision touched). Instead, `_confirmed_update_fields`
(event_reconciliation.py) and `merge_event_fields` (event_merge.py) set
`time_known` explicitly, in lockstep, only when `starts_at` itself is the
field that changed — see either docstring.

## Column shape and back-fill

`time_known boolean NOT NULL DEFAULT false` — the DEFAULT clause is what
back-fills every row that has no post_item_source at all (an
`extraction_failed` placeholder) or whose sources carry no `time_known` key
(pre-existing data from before this plan): honest "unknown" rather than an
invented answer, the same fail-toward-honest direction the date resolver
already takes, and the plan's explicit instruction ("default to False,
never True").

The UPDATE below only ever sets `true`, and only where determinable: for
each `post_item`, the MOST RECENTLY SEEN source (matching `_EVENT_SELECT`'s
own `ps` LATERAL exactly — `ORDER BY last_seen_at DESC, id DESC`) is
consulted, and `time_known` becomes `true` only when the JSON literal
`raw_extraction->>'time_known'` on THAT record its `'true'`. Every other row
keeps the column default (`false`) already applied by the ADD COLUMN
itself — this UPDATE never needs to (and does not) write `false` anywhere.

## Rollback

`DROP COLUMN time_known` — safe: no other column or table is touched, and
nothing here merges or deletes rows the way migration 0026's downgrade
could, so no destructive-refusal guard is needed. Downgrading loses the
distinction (every future read goes back to "cannot tell"), never a row.

## Real-Postgres verification

Run against a throwaway `postgres:16` container (migrated through 0034
first, exactly `.github/workflows/tests.yml`'s scratch-Postgres step):
`upgrade head` -> confirmed `time_known` present, `NOT NULL`, default
`false` -> inserted a `post_item`/`post_item_source` pair whose source's
`raw_extraction` carries `{"time_known": true}` and re-ran the UPDATE in
isolation to confirm it flips that row's column to `true` while a sibling
row with no matching key stays `false` -> `downgrade -1` -> confirmed the
column is gone and every other 0034 column/constraint/index is untouched ->
`upgrade head` again, confirming the round trip. Not reproducible in CI (no
local Postgres there — see tests/test_rds_store_contract.py's own
docstring), so, like 0034, this offline guard travels with the repo instead;
tests/test_rds_store_contract.py's `test_event_time_known_...` cases are the
ones that DO run against RDS_TEST_URL when it is set.

Revision ID: 0035_time_known
Revises: 0034_post_items
"""
from alembic import op

revision = "0035_time_known"
down_revision = "0034_post_items"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.post_item
  ADD COLUMN IF NOT EXISTS time_known boolean NOT NULL DEFAULT false;

UPDATE events.post_item e
SET time_known = true
FROM (
  SELECT DISTINCT ON (es.post_item_id) es.post_item_id, es.raw_extraction
  FROM events.post_item_source es
  ORDER BY es.post_item_id, es.last_seen_at DESC, es.id DESC
) ps
WHERE ps.post_item_id = e.post_item_id
  AND (ps.raw_extraction ->> 'time_known') = 'true';
"""

DOWNGRADE = r"""
ALTER TABLE events.post_item
  DROP COLUMN IF EXISTS time_known;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
