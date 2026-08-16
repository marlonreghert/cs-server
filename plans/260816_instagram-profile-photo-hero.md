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

The scheduled job is **backfill-only**. A profile photo, once captured, is good
indefinitely, so re-scraping the catalog on a clock buys almost nothing and is
pure recurring Apify spend. There is therefore **no refresh window at all** —
not a longer one, not one defaulted off. A venue that already has a photo is
skipped at any age.

Replacing photos the catalog already holds is an explicit, operator-triggered
action (`refresh_all`) with a visible cost estimate in front of it, never a
cron. Steady state after the backfill completes is **≈$0/month**: only
genuinely new venues, retries of past failures whose negative-cache window
expired, and venues whose Instagram handle was corrected cost anything.

For each servable venue holding a confirmed Instagram handle the job must:

1. Skip the venue entirely — **before any billed call** — when it already has a
   profile-photo row **for its current handle**, regardless of that row's age
   (`skipped_has_photo`). `refresh_all` ignores this gate; nothing else can.
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
archive service's ordering guarantees: resolve config → select venues → apply
the has-photo and negative-cache skips → *only then* spend. Per-venue and
per-photo failure isolation, a per-run venue cap, and a run summary bucketing
every outcome (`stored`, `unchanged`, `skipped_has_photo`,
`skipped_recent_failure`, `no_handle`, `no_pic`, `fetch_failed`,
`download_failed`, `upload_failed`, `credit_exhausted`).

All gating lives in **one** function, `select(mode)`, called by both the run and
the estimate. That is the invariant that keeps a priced run honest: an estimate
an operator approved is worthless if the run can scrape a different set, and two
copies of a gate drift the moment either is edited. `estimate()` is synchronous,
contains no `await` and touches no provider client, so "the estimate spends
nothing" is structural rather than conventional.

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
configured; an APScheduler job at `instagram_profile_photo_interval_hours` that
passes `mode=backfill` **explicitly** (so a future change to the service default
cannot turn the cron into a catalog-wide re-scrape); an admin trigger route for
an operator-driven run; and a **separate** estimate route, never a flag on the
trigger, so pricing a run can never start one.

## Data, Config, And API Impact
- **Migration:** `0043` adds `instagram.profile_photo` and (see the Review
  Amendment below) `instagram.profile_photo_attempt`. Additive; no existing
  table, column, or Redis key changes shape.
- **New Redis key family:** `venue_profile_photo_v1:{venue_id}`, JSON, no TTL.
  Sized against the ~20 MB currently in use on a `maxmemory 0` box: ~1,500 venues
  x ~200 bytes ≈ **0.3 MB**. Negligible.
- **New config:** `media_bucket`, `media_cdn_base_url`,
  `instagram_profile_photo_enabled` (default **false** — prod turns it on only
  after the terraform apply is verified), `instagram_profile_photo_retry_days`
  (7, see the Review Amendment), `instagram_profile_photo_max_venues_per_run`
  (200), `instagram_profile_photo_max_bytes`,
  `instagram_profile_photo_interval_hours`, `apify_instagram_profile_cost_usd`
  (0.003).
  There is deliberately **no** `instagram_profile_photo_refresh_days`: see the
  Backfill Amendment below. It was removed rather than defaulted to 0, because a
  dead knob still reads like a promise that a monthly refresh happens.
- **New admin routes:** `POST /admin/trigger/instagram_profile_photos` (the
  generic trigger; `mode` is its only config key) and
  `POST /admin/trigger/instagram_profile_photos/estimate`, aliased at
  `/admin/trigger/instagram-profile-photos/estimate`.
- **Public venue API:** unchanged. cs-server serves no new field; consumption is
  vibes_bot's read of the new Redis key.
- **Cost:** one Apify unit per venue, **once**. At ~1,500 handled venues the
  one-time backfill is ≈ **$4.50 total**, spread over ~8 daily runs by the
  200-venue per-run cap, plus S3 storage of ~45 MB (≈$0.001) and CloudFront
  egress well inside the free tier.
  **Steady state afterwards is ≈$0/month.** The only recurring spend is new
  venues (a handful a month), corrected handles, and failed venues retried once
  their 7-day negative-cache window expires — worst case, if F venues fail
  permanently, ≈ `F × 0.003 × (30/7)` per month, i.e. under $0.50/month even at
  F = 350. Far under the $10/month gate.
  An explicit `refresh_all` over the whole catalog costs ≈$4.50 again and is
  never automatic; the estimate endpoint prints that number, with a warning,
  before anything is spent. The execution run must still report the real handle
  count from `list_instagram_handles()` before the first prod run, since every
  figure here scales directly with it.

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
  `venue_profile_photo_estimated_cost_usd` (cumulative ACTUAL spend), plus
  `venue_profile_photo_estimate_cost_usd{mode}` — a **gauge**, what the last
  priced run *would* cost. Deliberately not folded into the counter: an
  estimate spends nothing, and adding it to cumulative spend would inflate the
  cost figure by exactly the amount an operator considered and declined.
- `venue_profile_photo_projected_venues` gauge, set by the projector

An `outcome` label that never appears is itself the diagnostic — an absent label
proves the path never ran.

## Test Plan
Feature file: `tests/bdd/enrichment/instagram-profile-photo-hero.feature`

Scenarios:
- A venue with a confirmed handle and a fresh profile picture stores the image
  and projects a CloudFront URL to Redis.
- A venue that already has a stored photo is skipped **and no Apify call is
  made** — the cost gate, asserted as the absence of a billed call, not merely
  as a skip count.
- A photo old enough to have expired under any refresh window (400 days) is
  **still** not re-scraped. This is the backfill-only guarantee; a 31-day case
  would pass under a reintroduced 90-day window and prove nothing.
- A venue whose handle was corrected IS re-scraped despite having a photo.
- `refresh_all` re-scrapes a venue the scheduled job skips.
- The estimate makes **zero** Apify calls, and the number it reports equals the
  number of billed scrapes the run then makes over the same fixture.
- The `refresh_all` estimate carries a `warning` string; the backfill estimate
  does not.
- A re-run whose downloaded bytes hash identically re-uploads nothing and leaves
  the served URL unchanged (driven in `refresh_all`, the only mode that reaches
  a venue which already has a photo).
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
- A stored photo is not re-scraped at 31 / 365 / 3650 days old.
- `refresh_all` reaches it; `refresh_all` still honours the negative cache, and
  `retry_days = 0` is the lever that overrides that.
- Mode parsing: absent/blank config → `backfill`; an unknown mode raises rather
  than defaulting, and such a run spends nothing.
- The APScheduler job passes `mode=backfill` explicitly (asserted on what
  arrives at the service, not on the service default).
- The estimate and the run go through the same `select()` (asserted by spying
  on it), the estimate makes no billed call, and the estimate never promises
  more than the per-run cap allows.

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
- No Apify call is made for a venue skipped by the has-photo gate, proven by the
  call counter, not by a log line — at any row age.
- No scheduled path can run `refresh_all`.
- The estimate makes zero billed calls and reports exactly the number of venues
  the run then scrapes.
- A second run over unchanged photos performs zero S3 uploads and changes zero
  served URLs.
- The media bucket is not publicly listable or readable except through
  CloudFront.
- `infra/datalake` is untouched — no resource diff, and the writer policy's
  `description` is byte-identical.
- The Google detail-photo path is byte-for-byte unchanged.
- The full BDD and pytest suites pass.

## Review Amendment (2026-08-16, PR #205 review)

Review of the implementation found the cost gate only half built, and the fix
is part of this same PR:

- **Negative caching (live cost defect).** Only `stored` and `unchanged` wrote
  a row, so a venue with no profile picture — or whose download/upload kept
  failing — had no row at all, and a venue with no row is unconditionally due.
  It was therefore re-scraped and re-billed on **every** run, forever; once
  `max_venues_per_run` (200) such venues accumulated they would consume the
  whole run budget (≈$0.60/day ≈ $18/month, breaching the $10/month gate) while
  no venue that could actually get a photo ever would. Fixed with
  `instagram.profile_photo_attempt` (added to migration **0043**, which had not
  been applied anywhere) plus `instagram_profile_photo_retry_days` (7, `0`
  disables) — the same negative-cache shape as
  `instagram_not_found_cache_ttl_days`. A separate table, not a status flag on
  the photo row, so a failed refresh can never overwrite (and so un-project)
  the hero a venue already has; it has no `_REBUILD_MODELS` entry, so
  `venue_profile_photo_v1:{venue_id}` still means exactly "this venue has a
  real stored photo".
- **The freshness gate ignored handle changes.** It read only `deleted_at` and
  `updated_at`, so a corrected Instagram handle kept the OLD business's logo on
  the venue's card for up to the full 30-day window. A stored handle that
  differs from the current one (compared case-insensitively, `@` stripped) now
  forces a re-fetch regardless of age; the same rule stops a stale attempt from
  suppressing a corrected handle's retry.
- **New outcome bucket:** `skipped_recent_failure`, on
  `venue_profile_photo_venues_total{outcome}`. It growing towards the whole
  catalog is the alarm that coverage has stalled.

## Backfill Amendment (2026-08-16, operator directive, PR #205)

The operator does **not** want a monthly re-scrape. A profile photo, once
captured, is good indefinitely; re-scraping the whole catalog every 30 days
buys almost nothing and is pure recurring Apify spend. Changed in this same PR:

- **The scheduled job is backfill-only.** A venue with a profile-photo row is
  skipped regardless of age. `instagram_profile_photo_refresh_days` and the
  `refresh_cutoff` it fed are **deleted**, not defaulted off — a dead setting is
  worse than none, because someone will set it and expect a monthly refresh that
  never happens. `skipped_fresh` is renamed `skipped_has_photo`, since "fresh"
  described a clock that no longer exists.
- **Both surviving gates are unchanged.** The negative cache
  (`instagram_profile_photo_retry_days`, 7) still skips a venue with no photo and
  a recent failed attempt — it is now the *only* recurring spend, so it matters
  more, not less. And the **handle-change re-scrape** still forces a re-fetch of
  a row whose stored handle differs from the venue's current one. That is not a
  refresh: it is another business's logo currently on this venue's card, a wrong
  answer being served to a user, and with no refresh window left nothing else
  would ever dislodge it. Normalization (case-insensitive, `@`-stripped) and the
  "stored handle absent = unknown, not mismatched = still skip" rule are kept.
- **New `refresh_all` mode, manual only.** Ignores the existing photo row and
  re-scrapes every venue with a confirmed handle. Unreachable from the
  scheduler: `main.py` passes `mode=backfill` explicitly, so a later change to
  the service default cannot turn the cron into a catalog-wide re-scrape.
  **`refresh_all` does NOT bypass the negative cache** — decision recorded
  because the opposite is defensible. It exists to replace photos the catalog
  *has*, and a negative-cached venue has none, so bypassing would buy a scrape
  the next backfill makes anyway while inflating the very bill the operator is
  approving. The dedicated lever for that intent already exists and is
  deliberately a settings change rather than a dialog click:
  `instagram_profile_photo_retry_days = 0`. The estimate reports the suppressed
  count so the choice is visible rather than silent.
- **New estimate endpoint that spends nothing.**
  `POST /admin/trigger/instagram_profile_photos/estimate` (hyphenated alias
  registered too), modelled on the proven
  `POST /trigger/venue_photo_archive/estimate` — a *separate* endpoint rather
  than a flag on the trigger, so an estimate can never accidentally start a run.
  Returns the mode, the venue count that would actually be scraped, the unit
  cost, the total USD, and — for `refresh_all` — a `warning` string for the
  admin UI's warning sign.
  **Critical invariant:** estimate and run share one selection function,
  `VenueProfilePhotoService.select(mode)`. Proven two ways: a unit test spying
  on `select` shows both paths call it, and a BDD scenario asserts the
  estimate's count equals the run's billed-call count over the same fixture.
- **Cost profile after this change:** one-time backfill ≈$4.50 at ~1,500 handled
  venues; steady state ≈$0/month (see the Cost bullet above).

**vibes_bot admin panel:** the estimate *call* already works with no change —
`JobsList.tsx` renders the job registry generically, `JobRunOptionsDialog.tsx`
builds `/api/enrichment/estimate/{job.name}`, and vibes_bot's
`app/admin/routes.py` proxies that straight to
`/admin/trigger/{job_name}/estimate`. The estimate *rendering* is field-specific
(`EstimateResult` reads `venues_after_skip`/`venues_selected`/`est_cost_usd`/
`caveat`), so this endpoint returns those names as aliases of the same
selection — the panel therefore shows the right count and cost today. Only the
visible **warning sign** needs a vibes_bot change (`warning` is not read
anywhere), plus an optional `mode` dropdown; that is a separate PR in a separate
repo and is **not** done here.

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
