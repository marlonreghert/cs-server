"""Add `events.post_item.display_title` —
plans/260912_events-venue-night-duplication.md §G.

When five posts about five acts collapse into one listing, the surviving
`title` is whichever source `merge_event_fields` last saw: an arbitrary pick
for the one string a reader actually looks at. This column holds a chosen
DISPLAY string — deterministic where one source title obviously contains the
others, and otherwise the answer to exactly one model call per merge group,
put through a deterministic validation gate before it is ever written.

## Why a new column and not `title`

`title` is an INPUT to `app.services.event_merge.compute_event_identity`. A
synthesised title would change which rows the EXACT identity pass considers
siblings on the next crawl, and a title no post contains has a distinctive
set that no future post's title can be contained in — the merge layer would
stop recognising its own canonical. `display_title` is presentation only and
touches no key: nothing hashes it, nothing groups on it, and
`is_selectable`/`expand_occurrences` never read it.

## Nullable, and NULL means "serve `title`"

No back-fill: the serving projection writes `display_title or title`
(`app.services.redis_projection_service`), so every existing row keeps
serving exactly what it serves today until something chooses a display title
for it. NULL is also what `_finish_absorption` restores whenever a merge
changes a canonical's source membership, so a stale title cannot survive a
group's growth.

Additive and nullable only: one new column on an existing table, no existing
column touched, no index, no other table changed. Downgrade drops it, which
is safe because the column is DERIVED — recomputable by re-running the
display-title pass (`scripts/backfill_event_display_titles.py`).

The column must ALSO be added to `RdsVenueStore._EVENT_COLUMNS`: that
allowlist is what lets `update_event` reach a column at all, and its own
comment records the failure mode when it is forgotten — the SQL store
silently no-ops the write while the in-memory fake accepts it, which passes
every BDD scenario and fails only in prod.

Revision ID: 0046_event_display_title
Revises: 0045_venue_address_provenance
"""
from alembic import op

revision = "0046_event_display_title"
down_revision = "0045_venue_address_provenance"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.post_item
  ADD COLUMN IF NOT EXISTS display_title text;
"""

DOWNGRADE = r"""
ALTER TABLE events.post_item
  DROP COLUMN IF EXISTS display_title;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
