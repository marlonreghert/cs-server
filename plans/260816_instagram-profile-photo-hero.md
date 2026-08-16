# Instagram Profile Photo As The Venue List Hero

## Branch
feature/instagram-profile-photo-hero

## Goal
Give every venue with a confirmed Instagram handle a **durable, app-servable
thumbnail URL** — the venue's Instagram profile picture, archived to a dedicated
S3 media bucket and served through CloudFront — projected into Redis so
vibes_bot can put a photo on a list card without a serve-time Google call.

## Non-goals
- **Changing the venue DETAIL photo path.** `venue_photos_fresh_v1`,
  `POST /internal/venues/{id}/photos/resolve`, and `PhotoEnrichmentService` keep
  their exact current behavior. Google photos remain the detail carousel.
- **Touching `infra/datalake` or the `retrieved/` archive.** That bucket is
  internal-use-only by design (`docs/venue-retrieval-storage.md` §8) and its
  terraform has known drift. This feature gets its own module. The datalake
  writer policy — including its immutable `description` — is not edited.
- **Reviving the pre-baked Google hero pipeline.**
  `plans/260726_venue-list-hero-photo.md` is ⛔ BLOCKED and stays blocked.
- **Instagram post images.** `SOURCE_INSTAGRAM_POSTS` in the media archive is
  unrelated and unchanged; this is the profile avatar only.
- **Handle discovery.** This consumes confirmed handles; it never resolves them.
- Photo cropping, ranking, or quality scoring. One image per venue.

## Evidence
- `app/api/instagram_profile_probe.py:154` already parses `og:image` — the
  profile picture — from a crawler-UA fetch. **Measured against production
  reality it is not viable as the source:** the module's own docstring records
  that "from a datacenter IP Instagram serves the login wall for EVERY handle",
  and cs-server runs on EC2. Measured here on 2026-08-16, the og:image is also
  only **100x100** (1,901 bytes), and the CDN size directive is signature-locked
  — rewriting `stp=dst-jpg_s100x100` to `s320x320` returns **HTTP 403**. Too
  small for a 101x76dp card at 2-3x density, and unavailable in prod regardless.
- `app/api/apify_instagram_client.py:283` `fetch_recent_posts` already drives
  `apify~instagram-scraper` through `_run_actor_sync` (`:427`), with
  `ApifyCreditExhaustedError` and `ApifyTransportFailure` handling and a
  `FetchPostsResult` envelope. The profile fetch is a second `resultsType` on
  machinery that exists, not a new client.
- `app/config.py:554` `apify_instagram_post_cost_usd = 0.003` is the established
  per-unit price for this actor; a profile scrape is one unit per venue.
- `app/dao/rds_venue_store.py:45` `_ENRICHMENT` maps `"instagram.handle" ->
  ("instagram", "handle", [...])`. `:1024` `list_instagram_handles()` already
  returns `venue_id -> instagram_handle` for every venue with a confirmed,
  non-deleted handle. That is the exact selection set, already written.
- `app/services/redis_projection_service.py:64` `_REBUILD_MODELS` maps a facet
  table_key to `(model, setter, deleter)`; `:164` drives it generically and the
  deleter propagates absence so a stale key never outlives its RDS row. A new
  facet is a registry entry, not new projector logic.
- `migrations/versions/0040_reviews_deep.py:46` is the enrichment-facet table
  shape: `venue_id text PRIMARY KEY REFERENCES venues.venue(venue_id)`,
  `payload jsonb NOT NULL`, `deleted_at timestamptz`, `updated_at timestamptz
  NOT NULL DEFAULT now()`. Newest migration is `0042`, so this is `0043`.
- `infra/admin/main.tf:1-35,120-218` is a **merged, proven** private-S3 +
  CloudFront-with-OAC stack in this repo, on `apivibesensemiddleware.click`,
  which is Route53-hosted in this account — so ACM validation completes in one
  `terraform apply` with no phased apply. That is the template.
- `app/services/venue_photo_archive_service.py:1-22` records the cost ordering
  that must be preserved by any spend pipeline here: config validated before any
  paid call, and **the already-archived check runs BEFORE the paid fetch**.
- `app/api/s3_client.py:44` `upload_photo_bytes` is menu-photo-shaped (uuid key,
  fixed `places/.../menu/` prefix, no cache headers). Not reused as-is.

## Current Behavior
A venue list card gets its photo from vibes_bot's `POST /venues/photos`, which
resolves Google Places photos on demand for the ids currently on screen, bounded
by a spend ledger and a 6h TTL cache. Nothing is pre-baked, deliberately: only
`google_place_id` may be stored long-term, so the catalog-wide pre-bake in
`plans/260726_venue-list-hero-photo.md` was blocked as prohibited by the Places
policy. Coverage is therefore limited to what a user actually scrolls past, and
every uncached card is a billed Google request.

cs-server stores no venue avatar of any kind. The only Instagram imagery it
archives is post images, into the internal-use `retrieved/` lake.

## Desired Behavior
A scheduled, budget-capped job must, for each servable venue holding a confirmed
Instagram handle:

1. Skip the venue entirely — **before any billed call** — when it already has a
   profile-photo row younger than `instagram_profile_photo_refresh_days`.
2. Scrape the profile through Apify, taking `profilePicUrlHD` (falling back to
   `profilePicUrl`).
3. Download the image bytes from the Instagram CDN, enforcing a content-type
   allowlist and a byte cap.
4. Content-address them: `venue-profile-photos/<venue_id>/<sha256[:16]>.jpg`. An
   unchanged photo must re-upload nothing and must leave the served URL
   byte-identical, so CDN and device caches are never invalidated for free.
5. Upload to the new media bucket with `Cache-Control: public, max-age=31536000,
   immutable` — safe precisely because the key is content-addressed.
6. Persist the CloudFront URL to `instagram.profile_photo` in RDS.

The projector must then mirror that row to `venue_profile_photo_v1:{venue_id}`,
and must delete the Redis key when the RDS row is absent or soft-deleted.

A venue with no handle, no profile picture, or a failed fetch must simply have no
key — an absence, never an error, and never a partial write.

## Implementation Approach

**1. Terraform — `infra/media/` (new module, modelled on `infra/admin/`).**
All infrastructure is Terraform; nothing is created by hand and anything that
already exists is imported. The module owns:
- A private, versioned, SSE-S3 bucket `vibesense-media-<account_id>` with all
  four public-access blocks set — public reach comes from CloudFront, never ACLs.
- An `aws_cloudfront_origin_access_control` + distribution aliased to
  `media.apivibesensemiddleware.click`, ACM cert validated via the Route53 zone
  already in this account, and a bucket policy admitting only that
  distribution's `SourceArn`.
- A **new, separately-named** IAM policy granting the cs-server role
  `s3:PutObject` on `venue-profile-photos/*` in this bucket only. It must not
  extend, rename, or re-describe the datalake writer policy — that
  `description` is immutable in AWS and editing it forces a destroy/recreate
  window in which BestTime flushes are dropped, not retried.
- Outputs: bucket name, distribution id, distribution domain name.

Because a write outside the policy fails *after* the scrape is already paid for,
the apply lands and is verified before the job is ever enabled in prod.

**2. Apify profile fetch.** Add `fetch_profile(handle) -> ProfileFetchResult` to
`ApifyInstagramClient`, running the same actor with `directUrls=[profile url]`,
`resultsType="details"`, `resultsLimit=1`. It reuses `_run_actor_sync` and
translates `ApifyCreditExhaustedError` identically to `fetch_recent_posts`, so an
exhausted balance stops the run instead of being re-spent per venue.

**3. Media store.** Add `VenueMediaStore` (`app/dao/venue_media_store.py`) —
deliberately separate from `MediaArchiveStore`, which writes the run-scoped,
GetObject-denied `retrieved/` layout under different compliance rules. It exposes
`put_profile_photo(venue_id, content_hash, data, content_type) -> (key, cdn_url)`
and holds the cache-control policy and the CDN base URL in one place.

**4. Service** `app/services/venue_profile_photo_service.py`, following the
archive service's ordering guarantees: resolve config → select venues → apply the
freshness skip → *only then* spend. Per-venue and per-photo failure isolation, a
per-run venue cap, and a run summary bucketing every outcome (`stored`,
`unchanged`, `skipped_fresh`, `no_handle`, `no_pic`, `fetch_failed`,
`download_failed`, `upload_failed`, `credit_exhausted`).

**5. Persistence.** Migration `0043_instagram_profile_photo.py` creating
`instagram.profile_photo` in the standard facet shape; register
`"instagram.profile_photo"` in `_ENRICHMENT`; add `VenueInstagramProfilePhoto` to
`app/models/instagram.py`.

**6. Projection.** Register the facet in `_REBUILD_MODELS` with new
`set_venue_profile_photo` / `delete_venue_profile_photo` DAO methods writing
`venue_profile_photo_v1:{venue_id}`. Written **without a TTL**, unlike the
Google-photo projection: there is no ToS clock on our own S3 object, and the
projector re-asserts or deletes the key every cycle anyway.

**7. Wiring.** Container construction gated on the media bucket + CDN base being
configured; an APScheduler job at `instagram_profile_photo_interval_hours`; and
an admin trigger route for an operator-driven run.

## Data, Config, And API Impact
- **Migration:** `0043` adds `instagram.profile_photo`. Additive; no existing
  table, column, or Redis key changes shape.
- **New Redis key family:** `venue_profile_photo_v1:{venue_id}`, JSON, no TTL.
  Sized against the ~20 MB currently in use on a `maxmemory 0` box: ~1,500 venues
  x ~200 bytes ≈ **0.3 MB**. Negligible.
- **New config:** `media_bucket`, `media_cdn_base_url`,
  `instagram_profile_photo_enabled` (default **false** — prod turns it on only
  after the terraform apply is verified), `instagram_profile_photo_refresh_days`
  (30), `instagram_profile_photo_max_venues_per_run`,
  `instagram_profile_photo_max_bytes`, `instagram_profile_photo_interval_hours`,
  `apify_instagram_profile_cost_usd` (0.003).
- **New admin route:** `POST /admin/trigger/instagram-profile-photos`.
- **Public venue API:** unchanged. cs-server serves no new field; consumption is
  vibes_bot's read of the new Redis key.
- **Cost:** one Apify unit per venue per refresh window. At ~1,500 handled venues
  and a 30-day window, ≈ **$4.50/month**, plus S3 storage of ~45 MB (≈$0.001) and
  CloudFront egress well inside the free tier. Under the $10/month gate — but the
  execution run must report the real handle count from
  `list_instagram_handles()` before the first prod run, since the estimate scales
  directly with it.

## Error Handling And Observability
Every failure is per-venue and isolated; one bad venue never aborts a run that is
paying for others. A download that is not an allowed image content-type, or that
exceeds the byte cap, is discarded without an S3 write. A credit-exhausted signal
stops the run. Nothing partially written is ever persisted: the RDS row is
written only after the S3 upload returns.

New Prometheus metrics:
- `venue_profile_photo_runs_total{result}` and
  `venue_profile_photo_run_duration_seconds`
- `venue_profile_photo_venues_total{outcome}` over the outcome buckets above
- `venue_profile_photo_bytes_stored_total`
- `venue_profile_photo_apify_calls_total` and
  `venue_profile_photo_estimated_cost_usd`
- `venue_profile_photo_projected_venues` gauge, set by the projector

An `outcome` label that never appears is itself the diagnostic — an absent label
proves the path never ran.

## Test Plan
Feature file: `tests/bdd/enrichment/instagram-profile-photo-hero.feature`

Scenarios:
- A venue with a confirmed handle and a fresh profile picture stores the image
  and projects a CloudFront URL to Redis.
- A venue whose stored photo is younger than the refresh window is skipped **and
  no Apify call is made** — the cost gate, asserted as the absence of a billed
  call, not merely as a skip count.
- A re-run whose downloaded bytes hash identically re-uploads nothing and leaves
  the served URL unchanged.
- A venue without a confirmed handle is never fetched and never billed.
- A profile that returns no picture URL records `no_pic` and writes no key.
- A download exceeding the byte cap, or of a disallowed content-type, is
  discarded without an S3 write.
- An Apify credit-exhausted signal stops the run instead of continuing to spend.
- One venue's failure does not abort the run; the remaining venues still store.
- The projector deletes `venue_profile_photo_v1` when the RDS row is soft-deleted.
- The job is inert while `instagram_profile_photo_enabled` is false.

Pytest unit tests:
- `fetch_profile` parsing: `profilePicUrlHD` preferred, `profilePicUrl` fallback,
  error/empty items, and the credit-exhausted translation.
- Content-addressed key derivation and the unchanged-hash short-circuit.
- Run-summary bucketing across every outcome.
- `VenueMediaStore` cache-control and CDN URL construction.
- Projector registry entry: set on present row, delete on absent.

Manual or integration checks:
- `terraform plan` on `infra/media` reviewed before apply; confirm it touches
  **no** `infra/datalake` resource.
- After apply: upload one object and fetch it through the CloudFront domain over
  HTTPS; confirm the bucket itself is not publicly reachable.
- One bounded prod run (small `max_venues_per_run`) with the real handle count
  reported first; verify stored objects, RDS rows, and Redis keys agree.

## Acceptance Criteria
- A venue with a confirmed handle carries `venue_profile_photo_v1:{venue_id}`
  holding a CloudFront URL that returns HTTP 200 with a year-long
  `Cache-Control`.
- No Apify call is made for a venue skipped by the freshness gate, proven by the
  call counter, not by a log line.
- A second run over unchanged photos performs zero S3 uploads and changes zero
  served URLs.
- The media bucket is not publicly listable or readable except through
  CloudFront.
- `infra/datalake` is untouched — no resource diff, and the writer policy's
  `description` is byte-identical.
- The Google detail-photo path is byte-for-byte unchanged.
- The full BDD and pytest suites pass.

## Open Questions
- None. Source (Apify), serving (private S3 + CloudFront), and the Google
  fallback were decided with the operator on 2026-08-16.

## Compliance Note (operator decision, recorded not assumed)
This stores and serves scraped Instagram imagery. It is **outside** the Google
Places restriction that blocked `plans/260726_venue-list-hero-photo.md` — no
Places content is cached, and no Google attribution is implied on these images —
which is precisely why this approach is viable where that one was not. It does
rely on Apify scraping, the same posture this repo already accepted for menu
photos and the Instagram post archive (`docs/venue-retrieval-storage.md` §8
frames that as a per-run operator choice). Flagged here so the decision is
explicit and revisitable, not buried.
