"""Add `events.post_item_source.source_media_type` and `.source_uploaded_at`,
`events.post_item_source.source_events_truncated`, and
`events.crawl_target.last_failure_kind` / `.last_failure_at`.

See plans/260812_crawl-error-visibility.md.

## Why these five columns, in one migration

All three are the same shape of gap: a fact the pipeline already computes
(or could) and then drops on the floor.

- `source_media_type text NULL` — Apify's own item `type` ("Video", "Image",
  "Sidecar") already flows through every manifest entry
  (`app.services.archive_sources._instagram_photo`'s `post_type` key,
  `InstagramCrawlChainer._archive_venue_posts`'s photo dict) but was never
  persisted past the S3 manifest. Named `source_media_type`, never
  `post_type`, so it cannot be confused with the UNRELATED
  `events.post_item.post_type` column (event/promotion/menu) — see the
  plan's own evidence section for the collision this sidesteps.
- `source_uploaded_at timestamptz NULL` — the post's OWN timestamp (Apify's
  `timestamp` field, already carried into every manifest entry's
  `uploaded_at` key and read back into `ArchivedPost.timestamp`), stored
  nowhere in RDS today. `resolve_event_datetime` anchors every date in the
  system on this value, and until now the anchor existed only in the S3
  manifest — any future re-resolution of a stored `date_text` needed an S3
  join to answer "relative to when?". This column makes that a pure RDS
  read.
- `source_events_truncated boolean NOT NULL DEFAULT false` — whether
  `parse_multi_event_extraction_response` (app/api/
  openai_event_extraction_client.py) dropped trailing entries because the
  post's own event list exceeded `max_events_per_post`. The model already
  generated (and was already paid for) every event; the cap only decides
  how many are kept. Without this column a capped roundup is
  indistinguishable from a complete one.
- `last_failure_kind text NULL` / `last_failure_at timestamptz NULL` on
  `events.crawl_target` — §B's "outcomes that mean what they say" needs
  somewhere to persist which of those outcomes a target's last run actually
  hit (`blocked`, `handle_not_found`, or the generic `failed`), and when —
  surfaced on `admin_crawl_router.CrawlTargetOut` so an operator can see WHY
  a target stopped producing without reading logs.

## Column shape and back-fill

Every column here is nullable (or, for `source_events_truncated`, defaulted
`false` — "not truncated" is the correct reading for every row extracted
before this column existed, since the cap already applied at extraction
time and a pre-existing row that exists at all was, by definition, not
dropped by it).

`source_media_type` and `source_uploaded_at` are NOT back-filled by this
migration. Both values are RECOVERABLE from the S3 archive manifest
(`archive_sources.py`'s own `uploaded_at`/`post_type` fields, written by
every run since the archive existed) by a separate, idempotent operator
script — mirroring the shape `260812_backfill-misattributed-links.md` uses
(dry-run by default, no Apify/OpenAI calls). That script is DEFERRED out of
this PR (see the PR notes for why) — the columns stay nullable for every
pre-existing row until it runs, which is an honest reading only because the
value is recoverable and a follow-up is tracked, not because NULL is
actually correct for those rows.

## Rollback

Every column drops independently and safely: nothing here merges, deletes,
or reinterprets an existing row. Losing `last_failure_kind`/`last_failure_at`
only means the admin API stops explaining a target's last failure; losing
`source_media_type`/`source_uploaded_at`/`source_events_truncated` only
means those facts go back to living solely in the S3 manifest (recoverable
by the same deferred backfill script, run again after a future re-upgrade).

Revision ID: 0036_source_media_type
Revises: 0035_time_known
"""
from alembic import op

revision = "0036_source_media_type"
down_revision = "0035_time_known"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.post_item_source
  ADD COLUMN IF NOT EXISTS source_media_type text,
  ADD COLUMN IF NOT EXISTS source_uploaded_at timestamptz,
  ADD COLUMN IF NOT EXISTS source_events_truncated boolean NOT NULL DEFAULT false;

ALTER TABLE events.crawl_target
  ADD COLUMN IF NOT EXISTS last_failure_kind text,
  ADD COLUMN IF NOT EXISTS last_failure_at timestamptz;
"""

DOWNGRADE = r"""
ALTER TABLE events.crawl_target
  DROP COLUMN IF EXISTS last_failure_kind,
  DROP COLUMN IF EXISTS last_failure_at;

ALTER TABLE events.post_item_source
  DROP COLUMN IF EXISTS source_events_truncated,
  DROP COLUMN IF EXISTS source_uploaded_at,
  DROP COLUMN IF EXISTS source_media_type;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
