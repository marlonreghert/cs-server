"""Rename `events.event` -> `events.post_item` and `events.event_source` ->
`events.post_item_source`; persist every extracted item's `post_type`
(replacing the silent drop 0034 removes below); add a free-text `category`.

See plans/260811_post-items-and-categories.md.

## Why the rename

`events.event` held whatever a post produced, but production rows are a
weekday lunch special, a children's craft workshop, a bingo, and a club
night — only the last is an event by the operator's own definition (a
happening sold on experience; a promotion sells a price). Every query, every
metric and every reviewer read "event" and got something else. The table is
renamed to name what it actually holds: an item a post announced. Not
`post` — one post can yield several items (the multi-event work exists
because a flyer lists four weeks at once), so a row is an ITEM, not the
post.

The `events` schema keeps its name: it is the event-extraction PIPELINE,
still true regardless of what any one row turns out to be.

## Why ALTER TABLE ... RENAME, never create-and-copy

Both tables carry live production data and are referenced by foreign keys,
indexes and unique constraints (see `event_venue_link_candidate.event_id`,
a real non-deferrable FK into `events.event`, untouched by this migration —
Postgres tracks that reference by OID, not by name, so it keeps resolving
correctly once the table and column it points at are renamed underneath it,
with no ALTER on that table at all). A copy-and-recreate would silently
drop whatever it forgot; a rename cannot.

## Constraints and indexes are renamed too, verified against a real Postgres

`uq_event_source_post` on a table called `post_item_source` is exactly the
next reader's confusion this repo has already been bitten by (a constraint-
name collision that only CI's scratch Postgres caught) — so every
constraint and index on both tables is renamed to match, not just the
tables and columns. The BEFORE names below were read back from a real
Postgres 16 that had run every migration through 0033 (this repo keeps no
local Postgres in CI, so nothing here was guessed):

  events.event:
    - PRIMARY KEY  event_pkey                  (event_id)
    - FOREIGN KEY  event_venue_id_fkey         (venue_id -> venues.venue)
    - INDEX        ix_event_status             (status)
    - INDEX        ix_event_venue_starts_at    (venue_id, starts_at)
  events.event_source:
    - PRIMARY KEY  event_source_pkey           (id)
    - FOREIGN KEY  event_source_event_id_fkey  (event_id -> events.event)
    - UNIQUE       uq_event_source_post        (source_handle,
                                                 source_shortcode,
                                                 source_event_key)
    - INDEX        ix_event_source_event_id    (event_id)

`ALTER TABLE ... RENAME CONSTRAINT` on `uq_event_source_post` also renames
its auto-created backing index (verified against the same real Postgres) —
so the constraint rename alone is sufficient there; the two INDEXes (which
are not backing any constraint) need their own explicit `ALTER INDEX ...
RENAME`.

## post_type: persisted, not dropped

`260810_post-kind-and-post-extraction-attribution.md` computed the model's
`kind` and then dropped every non-event without persisting it — a
deliberate interim, taken when the alternative was a discriminator column
on a table that should not have existed in that shape. `post_type text NOT
NULL DEFAULT 'event'` removes that interim: every extracted item is stored,
typed, from here on. The DEFAULT is what back-fills every EXISTING row to
'event' — a fast, catalog-only write on Postgres 11+, not a table rewrite —
because 'event' is what the pipeline believed when it wrote them and what
the console already shows; nothing here re-classifies a single existing
row.

An unrecognised `post_type` is stored VERBATIM at the application layer
(app.models.post_type.normalize_post_type, unchanged by this migration) —
this column places NO CHECK constraint on the value, deliberately: the
fail-toward-visible rule now relaxes (a persisted, typed row is inspectable
and correctable, unlike the dropped row it replaces), and a CHECK would
defeat exactly that by rejecting the write.

## category: free text, no back-fill

`category text NULL`. Existing rows get a type, not a category: category
was never extracted for them, and inventing one from a title is a guess.
NULL is the historically accurate value.

## Rollback

The downgrade renames everything back and drops both new columns — proven
against a real Postgres by running upgrade -> downgrade -> upgrade in
sequence (see tests/test_post_items_migration.py), not merely asserted by
symmetry. Dropping `post_type`/`category` cannot lose provenance the way
0026's downgrade could (no rows are merged or deleted here), so it needs no
destructive-refusal guard.

Revision ID: 0034_post_items
Revises: 0033_crawl_target_reels_overlap
"""
from alembic import op

revision = "0034_post_items"
down_revision = "0033_crawl_target_reels_overlap"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.event RENAME TO post_item;
ALTER TABLE events.event_source RENAME TO post_item_source;

ALTER TABLE events.post_item RENAME COLUMN event_id TO post_item_id;
ALTER TABLE events.post_item_source RENAME COLUMN event_id TO post_item_id;

ALTER TABLE events.post_item RENAME CONSTRAINT event_pkey TO post_item_pkey;
ALTER TABLE events.post_item RENAME CONSTRAINT event_venue_id_fkey TO post_item_venue_id_fkey;
ALTER INDEX events.ix_event_status RENAME TO ix_post_item_status;
ALTER INDEX events.ix_event_venue_starts_at RENAME TO ix_post_item_venue_starts_at;

ALTER TABLE events.post_item_source RENAME CONSTRAINT event_source_pkey TO post_item_source_pkey;
ALTER TABLE events.post_item_source RENAME CONSTRAINT event_source_event_id_fkey TO post_item_source_post_item_id_fkey;
ALTER TABLE events.post_item_source RENAME CONSTRAINT uq_event_source_post TO uq_post_item_source_post;
ALTER INDEX events.ix_event_source_event_id RENAME TO ix_post_item_source_post_item_id;

ALTER TABLE events.post_item
  ADD COLUMN IF NOT EXISTS post_type text NOT NULL DEFAULT 'event',
  ADD COLUMN IF NOT EXISTS category text;
"""

DOWNGRADE = r"""
ALTER TABLE events.post_item
  DROP COLUMN IF EXISTS category,
  DROP COLUMN IF EXISTS post_type;

ALTER INDEX events.ix_post_item_source_post_item_id RENAME TO ix_event_source_event_id;
ALTER TABLE events.post_item_source RENAME CONSTRAINT uq_post_item_source_post TO uq_event_source_post;
ALTER TABLE events.post_item_source RENAME CONSTRAINT post_item_source_post_item_id_fkey TO event_source_event_id_fkey;
ALTER TABLE events.post_item_source RENAME CONSTRAINT post_item_source_pkey TO event_source_pkey;

ALTER INDEX events.ix_post_item_venue_starts_at RENAME TO ix_event_venue_starts_at;
ALTER INDEX events.ix_post_item_status RENAME TO ix_event_status;
ALTER TABLE events.post_item RENAME CONSTRAINT post_item_venue_id_fkey TO event_venue_id_fkey;
ALTER TABLE events.post_item RENAME CONSTRAINT post_item_pkey TO event_pkey;

ALTER TABLE events.post_item_source RENAME COLUMN post_item_id TO event_id;
ALTER TABLE events.post_item RENAME COLUMN post_item_id TO event_id;

ALTER TABLE events.post_item_source RENAME TO event_source;
ALTER TABLE events.post_item RENAME TO event;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
