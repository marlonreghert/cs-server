"""Add `instagram.profile_photo` (+ `instagram.profile_photo_attempt`) — the
RDS system of record for a venue's archived Instagram profile picture, and the
negative cache that stops a venue with no photo from being re-billed forever.
See plans/260816_instagram-profile-photo-hero.md.

Chains from 0042_venue_add_job_run, verified against migrations/versions/
directly rather than trusted from the plan text (see 0030_crawl_target.py's
own docstring for why that verification step exists: a plan's stated chain
reference can go stale between being written and being executed). This
migration adds exactly TWO new tables, in the existing `instagram` schema,
and touches nothing 0001-0042 own.

## `instagram.profile_photo`

The standard enrichment-facet shape used by every other table in this schema
family — `venue_id` PK + FK, `payload` jsonb, `deleted_at`, `updated_at` —
structurally identical to `venues.reviews_deep` (0040) and to
`instagram.handle` / `instagram.posts` in 0001_baseline_schemas.py. Being the
standard shape is what lets the generic machinery pick it up with a registry
entry instead of new code: `RdsVenueStore._ENRICHMENT` for read/write and
`RedisProjectionService._REBUILD_MODELS` for projection (including the
deleter, so a soft-deleted row's Redis key cannot outlive it).

`payload` holds the full `VenueInstagramProfilePhoto` model: the public
CloudFront URL, the S3 key, the full sha256 of the stored bytes, the content
type and byte size, and when it was fetched. The hash is what makes a re-run
over an unchanged photo free — it re-uploads nothing and leaves the served
URL byte-identical, so no CDN or device cache is invalidated for nothing.

`updated_at` is load-bearing, not decoration: it is the input to the
freshness gate that runs BEFORE any billed Apify call
(`instagram_profile_photo_refresh_days`). A skip that happened after the
scrape would have already spent the money it exists to save.

Deliberately a SEPARATE table from `instagram.handle`, not a widened column
on it: handle discovery and profile-photo archival have different write
cadences, different failure modes and different costs, and folding them
together would mean a photo refresh rewrites the row the discovery cascade
owns.

## `instagram.profile_photo_attempt`

The negative cache, in the same facet shape. One row per venue whose last
attempt produced NO photo — a profile with no picture, a failed scrape, a
failed download, a failed upload — holding the attempted handle and the
outcome, with `updated_at` as the retry clock
(`instagram_profile_photo_retry_days`). Without it a venue that can never
yield a photo has no row at all, so the selection gate finds it due on EVERY
run and re-buys the same scrape forever; once enough of them accumulate they
fill the per-run cap and no venue that COULD get a photo ever gets one.
This is the same shape the repo already uses for handle discovery's
`instagram_not_found_cache_ttl_days`, one facet over.

Deliberately a SEPARATE table from `instagram.profile_photo`, not a status
flag inside it, for one load-bearing reason: a refresh whose scrape fails
must not overwrite the row a venue's LIVE hero is projected from. Writing a
failure marker into the photo row would delete the venue's photo from Redis
(the projector mirrors that row) over a transient Apify error. Keeping the
two apart means `instagram.profile_photo` still holds one thing only — a
real, stored photo — which is the cross-repo contract vibes_bot reads, and
this table is never registered in `RedisProjectionService._REBUILD_MODELS`,
so no Redis key can ever be produced from an attempt row.

Additive only: two new tables, no existing table touched, no Redis key
reshaped. Downgrade drops them — safe, because everything in them is
re-derivable (the S3 objects are content-addressed and survive the drop; a
later run simply re-asserts the rows, re-paying the Apify scrape).

Revision ID: 0043_instagram_profile_photo
Revises: 0042_venue_add_job_run
"""
from alembic import op

revision = "0043_instagram_profile_photo"
down_revision = "0042_venue_add_job_run"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE IF NOT EXISTS instagram.profile_photo (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS instagram.profile_photo_attempt (
  venue_id   text PRIMARY KEY REFERENCES venues.venue(venue_id),
  payload    jsonb NOT NULL,
  deleted_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS instagram.profile_photo_attempt;
DROP TABLE IF EXISTS instagram.profile_photo;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
