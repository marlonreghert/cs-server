"""Add flyer-media columns to `events.post_item` — plans/260905_events-
serving-projection.md, Phase 1.

Chains from 0043_instagram_profile_photo, verified against
migrations/versions/ directly rather than trusted from the plan text (see
0030_crawl_target.py's own docstring for why that verification step exists).

## Columns

`events.post_item` gains five nullable columns, filled ONLY by
`app/services/event_flyer_service.py` (no back-fill; every existing row
starts with all five NULL):

- `flyer_url` — the public CloudFront url the app renders directly.
- `flyer_s3_key` — the content-addressed key under `event-flyers/*` in the
  app media bucket (`<post_item_id>/<sha256[:16]>.jpg`).
- `flyer_content_hash` — the FULL sha256 hexdigest of the copied bytes. This
  is what makes the copy idempotent: an unchanged source photo re-hashes to
  the identical value, so a re-run recomputes the identical key/url without
  re-uploading — the same property `instagram.profile_photo.content_hash`
  (0043 / venue_profile_photo_service.py) already relies on.
- `flyer_copied_at` — when the copy last ran for this event.
- `flyer_byte_size` — the copied object's size in bytes. NOT in the original
  plan's four-column list; added here because the plan's own Retention
  section asks for an `event_flyer_bytes` gauge tracking total bytes stored
  under `event-flyers/*`, and there is no other way to compute that
  honestly: the media-bucket IAM grant is PutObject-only (infra/media/
  main.tf), so cs-server cannot list/read the bucket back to total it from
  S3 itself. Storing the size at write time is a cheap, additive, nullable
  column that makes the gauge a real, restart-safe read of the system of
  record instead of an in-process counter that resets to zero on every
  deploy. See app/services/event_flyer_service.py and
  RdsVenueStore.count_event_flyers.

Additive only: five new nullable columns on an existing table, no existing
column touched, no Redis key reshaped. Downgrade drops them — safe, because
everything in them is re-derivable (the S3 objects are content-addressed and
survive the drop; the next projection cycle simply re-copies and re-persists
them, spending no Apify/Google/BestTime/OpenAI quota either way — the bytes
already live in the data lake).

Revision ID: 0044_event_flyer_media
Revises: 0043_instagram_profile_photo
"""
from alembic import op

revision = "0044_event_flyer_media"
down_revision = "0043_instagram_profile_photo"
branch_labels = None
depends_on = None


UPGRADE = r"""
ALTER TABLE events.post_item
  ADD COLUMN IF NOT EXISTS flyer_url text,
  ADD COLUMN IF NOT EXISTS flyer_s3_key text,
  ADD COLUMN IF NOT EXISTS flyer_content_hash text,
  ADD COLUMN IF NOT EXISTS flyer_copied_at timestamptz,
  ADD COLUMN IF NOT EXISTS flyer_byte_size integer;
"""

DOWNGRADE = r"""
ALTER TABLE events.post_item
  DROP COLUMN IF EXISTS flyer_byte_size,
  DROP COLUMN IF EXISTS flyer_copied_at,
  DROP COLUMN IF EXISTS flyer_content_hash,
  DROP COLUMN IF EXISTS flyer_s3_key,
  DROP COLUMN IF EXISTS flyer_url;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
