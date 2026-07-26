# Venue List Hero Photo

## Branch
feature/venue-list-hero-photo

## Goal
Give every servable venue a **pre-baked hero thumbnail URL** that reaches the
venue LIST without any serve-time Google call, so the mobile list card can show
a photo per row.

cs-server's slice is the whole supply side:

1. A `google_places.hero_photo` RDS row per venue holding the **durable** Google
   photo resource name plus the **ephemeral** resolved keyless CDN URL.
2. A scheduled job that discovers missing rows and refreshes stale URLs, under a
   daily Google spend ledger.
3. A projector stage that publishes the URL into the shared Redis serving
   projection with a **remaining TTL**, so a stalled job degrades to "no photo",
   never to a dead image.
4. `venue_photo_url` on the `MinifiedVenue` serving DTO that vibes_bot already
   consumes via `GET /v1/venues/nearby`.

**This plan is gated on Phase 0 (§Implementation Approach). If a keyless photo
URI turns out to live less than ~6 hours, the correct outcome is to abandon the
feature, not to build it anyway.**

## Non-goals
- **Serving venue imagery from S3.** The data-lake `media/` archive added by
  `plans/260726_venue-photo-archive.md` is **internal use only**. Every image
  displayed in the app must be served from Google's own CDN
  (`lh3.googleusercontent.com`) for Places compliance. This is a product
  constraint, not an implementation preference — do not re-propose an S3 or
  CloudFront serving path in review. It is independently blocked anyway: the
  bucket sets `restrict_public_buckets = true` (`infra/datalake/main.tf:29-35`)
  and the writer role is deliberately denied `s3:GetObject`
  (`infra/datalake/iam.tf:9`).
- **Resolving photos on demand for the list.** Rejected on cost and latency; see
  Current Behavior.
- **Reviving Job 5 / the legacy `venue_photos_v1` pre-bake.** It stays retired
  and its 404 stays pinned.
- **Changing the venue detail photo path.** `venue_photos_fresh_v1`,
  `POST /internal/venues/{id}/photos/resolve`, and `PhotoEnrichmentService`
  keep their current behavior.
- **Photo quality ranking.** The hero is `photos[0]` positionally. See Open
  Questions.
- **Touching `venues.venue`, `app/models/venue.py::Venue`, or
  `app/dao/venue_row.py`.** See Implementation Approach step 1.

## Evidence
- `GooglePlacesAPIClient.get_place_photos(place_id, max_photos=5, max_width=800)`
  (`app/api/google_places_client.py:509`) is two-step: Place Details with
  `PHOTOS_FIELDS_MASK = "photos.name,photos.authorAttributions"` (`:22`) returns
  photo resource **names**, then `_resolve_photo_media_uri(photo_name, max_width)`
  (`:608`) calls the media endpoint with `skipHttpRedirect=true` and the key in
  the `X-Goog-Api-Key` **header**, returning a **keyless** `photoUri` (`:619`).
  The loop is sequential over `photos[:max_photos]` (`:589`).
- The photo resource **name** is durable; the resolved URI is not. The client's
  own docstring states the keyless form "does not die when Google rotates the
  photo token" (`app/api/google_places_client.py:522-527`) — but no code in this
  repo has ever measured how long it does live. `photo_fresh_cache_ttl_hours = 6`
  (`app/config.py:246`) is an unvalidated conservative guess.
- The failure that retired the catalog-wide pre-bake involved **key-bearing**
  `/media?...&key=` redirect URLs, a different artifact
  (`plans/260707_on-demand-venue-photos.md:9-11`). The keyless form was never
  tried at catalog scale.
- The projector `RedisProjectionService.rebuild_redis_from_rds`
  (`app/services/redis_projection_service.py:87`) is the sole Redis writer for
  pipeline data. It bulk-prefetches enrichment (`:127`) and runs a per-venue
  stage block with `stage = "venue" | "enrichment" | "photos" | "weekly" |
  "live"` (`:142-174`) where a single venue's failure cannot abort the run.
- `_project_photos` (`:217-237`) already implements exactly the **remaining-TTL**
  arithmetic this feature needs: `remaining = full_ttl - age(updated_at)`, write
  when `> 0`, otherwise delete. It is the pattern to copy.
- `_ENRICHMENT` (`app/dao/rds_venue_store.py:37`) maps
  `table_key -> (schema, table, [promoted columns])`; `"google_places.photos"`
  is registered at `:42` with no promoted columns.
- `MinifiedVenue` (`app/models/venue.py:185`) is the non-verbose serving DTO;
  `venue_photos` sits at `:217`. It is constructed at
  `app/handlers/venue_handler.py:554`, inside `_transform` (`:296`), which
  already bulk-prefetches five per-key-family enrichment maps at `:327-331`.
- Migration head is `0019_venue_closure_signal`
  (`migrations/versions/0019_venue_closure_signal.py:37`), so the new migration
  is **0020**.
- `JOB_REGISTRY` (`app/routers/admin_trigger_router.py:110`) currently holds
  `live_forecast, weekly_forecast, google_places, google_places_backfill,
  instagram, instagram_posts, venue_photo_archive, menu_photos, menu_extraction,
  vibe_classifier, instagram_validate, inventory_sync, rebuild_redis`. The name
  `hero_photo` is free. The retired `photos` job must keep 404ing —
  `tests/bdd/api/on-demand-venue-photos.feature` pins it.
- There is **no Google spend guard anywhere in this repo**. `VenueBudgetService`
  (`app/services/venue_budget_service.py`) is BestTime-only. The on-demand
  resolve path has no rate limiter, no ledger, and `/internal` has no app-level
  auth (`app/routers/internal_router.py`).
- Serving set size: `serving_view_venues = 456`
  (wrapper `plans/260702_geofence-city-circles.md:41`), against `active = 1255`,
  `servable = 849` recorded 2026-06-17
  (wrapper `plans/260617_eligible-priority-live-refresh.md:41`). These disagree
  and both are stale — see Phase 0 / S3.

## Current Behavior
Venue photos are resolved **on demand, per venue, at detail-open only**. A
resolve is 1 Place Details + up to `photos_per_venue = 5` sequential media calls
(`app/config.py:232`), cached in `venue_photos_fresh_v1:{venue_id}` for
`photo_fresh_cache_ttl_hours = 6` (`app/config.py:246`) via `setex` with no
read-extension.

Nothing writes a photo onto the list payload. `MinifiedVenue.venue_photos` is
populated from the legacy `venue_photos_v1` key, which is now **data-dead**:
nothing has written `google_places.photos` since Job 5 was retired
(`main.py`, job unscheduled and absent from `JOB_REGISTRY`), so
`_project_photos`' remaining-TTL arithmetic evaluates `<= 0` and **deletes**.
vibes_bot strips the field to `[]` on the list regardless.

Doing this on demand for the list is not viable. The mobile client hydrates the
**entire servable set** per cold open in 50-item chunks
(`vibe_sense_mobile/src/services/venueCatalog.ts:90-143`), and the 6h `setex`
makes spend traffic-independent: 4 x S resolves/day. At S=456 that is 1,824
resolves/day — **$657–$2,189/month** and 5.5x over Google's free tier on both
SKUs — with p95 pinned at the resolve budget.

## Desired Behavior
1. Every servable venue with a `google_place_id` and at least one Google photo
   has a `google_places.hero_photo` row holding its durable `photo_name` and a
   recently-resolved keyless `photo_url`.
2. A venue with no place id or no photos is **negatively cached in RDS**
   (`state = 'no_place_id' | 'no_photo'`) and is never re-resolved by the
   scheduled job.
3. A scheduled job discovers missing rows and re-resolves URLs older than
   `hero_photo_refresh_hours`, oldest first, and **stops spending** when the
   daily Google call ledger is exhausted.
4. The projector publishes `venue_hero_photo_v1:{venue_id}` with
   `remaining = hero_photo_max_age_hours - age(url_resolved_at)`, and **deletes**
   the key when that is `<= 0`.
5. `GET /v1/venues/nearby` (non-verbose) returns `venue_photo_url` per venue:
   the projected URL, or `null`.
6. `null` is a normal value at every layer — no place id, no photo, aged past the
   ceiling, or the job is behind. It is never an error and never a 500.
7. No serving path makes a Google call. Ever.

## Implementation Approach

### Phase 0 — Blocking spike (no feature code until it returns)

**S1 — keyless photo URI lifetime. This is the go/no-go gate.**
Resolve heroes for ~20 distinct venues, record the returned
`lh3.googleusercontent.com` URIs, then `HEAD`-poll each hourly for 7 days and
record time-to-first-4xx. Deliverable: p05 and p50 lifetime. Cost ~40 Google
calls, ~$0.30.

This one number sets `hero_photo_refresh_hours` and therefore the entire
recurring bill. Using hero-only resolves (1 Place Details + 1 media = $0.012)
at S=456:

| p05 lifetime | Cadence | Google calls/SKU/month | Approx. cost |
|---|---|---|---|
| >= 7 days | weekly | ~2,000 | **$0** — inside the 10k/SKU free tier |
| ~24 h | daily | ~13,700 | ~$26/mo |
| ~6 h | 4x/day | ~54,700 | ~$313/mo |
| < ~4 h | — | — | **Do not build. Ship no list photo.** |

**S2 — CDN width suffix rewrite.** `curl -I` a resolved URI with its `=w800`
suffix mutated to `=w400`. If Google honors the directive, the refresh job can
derive a list hero for free from any already-warm `venue_photos_fresh_v1` key,
and the 400px list and 800px detail widths can eventually collapse to one
resolve per venue. **Not a dependency** — the job falls back to its own media
call. ~30 minutes.

**S3 — real serving-set size.** Read `SERVING_VIEW_VENUES` and
`REDIS_PROJECTION_VENUES` off prod Prometheus. Every cost figure above assumes
456, from a plan dated 2026-07-02, while another dated 2026-06-17 says 849 and
the active catalog is ~1255. If the real number is 849, multiply every figure by
1.9x; at the full catalog, by ~2.8x.

### Phase 1 — Persistence

1. **Migration `0020_venue_hero_photo.py`**, `down_revision =
   "0019_venue_closure_signal"`. New table `google_places.hero_photo` keyed by
   `venue_id`, holding: `photo_name` (durable resource name), `author_name`,
   `photo_url` (resolved keyless URI), `photo_width_px`, `name_resolved_at`,
   `url_resolved_at`, `state` (`ok | no_photo | no_place_id | failed`),
   `failure_count`, `last_error`, `payload` jsonb, `deleted_at`, `updated_at`.
   Partial indexes on `url_resolved_at` (for the refresh queue) and on
   `(state, updated_at)`, both `WHERE deleted_at IS NULL`.
2. Register in `_ENRICHMENT` (`app/dao/rds_venue_store.py:37`) as
   `"google_places.hero_photo"` with the scalar columns promoted, and exclude it
   from `audit.enrichment_history` the same way `google_places.photos` is.

**Hard constraint.** Do **not** add a hero field to `venues.venue`,
`app/models/venue.py::Venue`, or `app/dao/venue_row.py`. `Venue` is round-trip
bound to RDS via `COLUMN_FIELDS ∪ RESIDUAL_FIELDS`, and `split_venue_for_storage`
dumps the whole model on every catalog-refresh upsert — a hero column there would
be clobbered with `None` every cycle. Someone will propose this in review; the
answer is no. The hero lives in its own enrichment table, exactly like photos.

### Phase 2 — Google client (no behavior change to the detail path)

Decompose `get_place_photos` (`app/api/google_places_client.py:509`) without
changing what it does:

- new `get_place_photo_names(place_id, max_photos)` — the Place Details half,
  reusing `PHOTOS_FIELDS_MASK` and the existing instrumentation;
- promote `_resolve_photo_media_uri` (`:608`) to public
  `resolve_photo_media_uri(photo_name, max_width)`;
- recompose `get_place_photos` from the two.

`tests/test_photo_resolve.py` pins `maxWidthPx == "800"` and
`max_photos == photos_per_venue` on the existing path. Those assertions must stay
green — that is the regression gate for this refactor.

### Phase 3 — Spend ledger (new, and overdue)

`GooglePhotoBudgetService` over a Redis daily counter keyed by date, checked
against an admin-tunable ceiling. `try_spend(n) -> bool` is called **inside** the
resolve loop, not once at batch entry, so an exhausted budget stops the run
mid-flight rather than after it.

Wire it into the **existing** `resolve_and_cache_fresh_photos` path too. That
path has been unguarded since the on-demand cutover and `/internal` has no
app-level auth — it is a latent unbounded-bill surface independent of this
feature.

### Phase 4 — Hero photo service

`HeroPhotoService` with three entry points:

- `discover_missing(limit)` — venues in `serving.eligible_venue` with a
  `google_place_id` and no `hero_photo` row. One Place Details call for
  `photos[0].name` + author attribution, insert the row, immediately resolve the
  URI at `hero_photo_width_px`. No place id → `state='no_place_id'`; zero photos
  → `state='no_photo'`. Both are terminal for the scheduled job.
- `refresh_stale(limit)` — rows with `state='ok'` and `url_resolved_at` older
  than `hero_photo_refresh_hours`, oldest first. **Check
  `venue_photos_fresh_v1:{id}` first**: on a hit, derive the hero by width-suffix
  rewrite (pending S2) at zero Google cost. Otherwise one media call from the
  stored `photo_name`. A 4xx on media means the resource name itself rotated →
  increment `failure_count` and requeue for re-discovery; soft-delete after 3
  consecutive failures.
- `canary_probe()` — `HEAD` a handful of already-projected URLs each run and
  emit a failure counter. This is the early warning that the configured refresh
  interval has drifted past the real URI lifetime, which is the one failure mode
  that would otherwise be silent and global.

Reuse the existing Google pacing constant rather than introducing a new one.
Gate construction on `settings.google_places_api_key`.

### Phase 5 — Projection and serving

- Redis DAO: `venue_hero_photo_v1:{venue_id}` holding `{url, w, author_name,
  resolved_at}`, with `set_/get_bulk/delete_` accessors, and the delete wired
  into the existing `delete_venue` sweep alongside the two other photo caches.
- Projector: one more bulk prefetch and a `stage = "hero_photo"` entry in the
  per-venue isolation block, using remaining-TTL arithmetic **byte-identical to
  `_project_photos`** (`:217-237`). Zero Google calls in the projector; one extra
  Redis SET per venue.
- Serving: one more bulk map alongside the five at `venue_handler.py:327-331`,
  and `venue_photo_url=...` in the `MinifiedVenue` construction at `:554`.
  Populated on the **non-verbose branch only** — vibes_bot calls with
  `verbose=False`. The verbose branch is untouched.

`author_name` is carried through RDS **and** Redis so attribution can be
surfaced later without a re-projection, but is deliberately **not** added to the
serving DTO — see Open Questions.

### Phase 6 — Job, schedule, backfill

Register `hero_photo` in `JOB_REGISTRY` and schedule it on a cron, off-loop via
`run_in_executor` under its own job lock, exactly like the existing projection
job. **The job must be named `hero_photo`, never `photos`** — the retired
`photos` job's 404 is pinned by `tests/bdd/api/on-demand-venue-photos.feature`
and that assertion stays green.

Backfill is the same job triggered once with a large discover limit. At S=456 and
2 calls per venue with existing Google pacing, that is roughly 5 minutes and ~$5.

## Data, Config, And API Impact
- **API:** `GET /v1/venues/nearby` non-verbose items gain
  `venue_photo_url: Optional[str] = None`. Additive and nullable; no existing
  field changes. No new endpoint. The verbose branch is unchanged.
- **Persistence:** new table `google_places.hero_photo` (migration 0020). New
  Redis key `venue_hero_photo_v1:{venue_id}`, cs-server sole writer. No change to
  `venues.venue`, to `venue_photos_v1`, or to `venue_photos_fresh_v1`.
- **New settings:** `hero_photo_cron`, `hero_photo_refresh_hours`,
  `hero_photo_max_age_hours`, `hero_photo_width_px`, `hero_photo_discover_limit`,
  `hero_photo_refresh_limit`, `hero_photo_daily_call_budget`. All admin-tunable
  through the existing `admin_config:` mechanism with a registered validator.
  `hero_photo_refresh_hours` **must not be set until S1 reports**; ship the job
  disabled rather than guess it.
- **Feature flag:** none in this repo. Serving the field is unconditional; the
  user-visible gate is vibes_bot's `FEATURE_LIST_HERO_PHOTO`. This is deliberate
  — it lets the data land and be verified before anything renders.
- **Infrastructure:** none. No S3, no CDN, no new IAM.

## Error Handling And Observability
Every failure degrades to "no hero photo", never to a broken image and never to a
5xx:

| Failure | Behavior |
|---|---|
| No `google_place_id` | `state='no_place_id'`, terminal, no Google call ever |
| Google returns zero photos | `state='no_photo'`, terminal |
| Place Details / media call fails | count, leave the row for the next run, continue to the next venue |
| Media call returns 4xx | resource name rotated → `failure_count += 1`, requeue for discovery; soft-delete after 3 |
| Daily ledger exhausted | stop spending mid-run, emit the outcome, exit cleanly |
| Projector runs with a stale row | remaining TTL `<= 0` → **delete** the key → serving returns `null` |
| Redis unavailable at serve time | `venue_photo_url` is `null`; the venue still serves |

New metrics: hero rows by `state`; refresh outcomes labelled
`derived|resolved|no_photo|no_place_id|failed|budget_exceeded`; projected URL age;
canary probe failures; projected hero count; and the daily Google photo call
counter. The canary counter is the alert that matters — it is the only signal
that the configured cadence has drifted past the real URI lifetime.

## Test Plan
Feature file: `tests/bdd/enrichment/venue-list-hero-photo.feature`

Scenarios:
- Discovery stores the durable photo name and a resolved URL for a venue with photos.
- A venue with no `google_place_id` is recorded terminally and never costs a Google call.
- A venue whose Place Details returns zero photos is recorded terminally and is not retried by the scheduled job.
- Refreshing a stale row from its stored photo name costs exactly one media call — no second Place Details.
- A warm `venue_photos_fresh_v1` entry yields the hero with zero Google calls.
- A 4xx on the media call increments the failure count and requeues the venue for re-discovery.
- Three consecutive failures soft-delete the row.
- The daily call ledger short-circuits a run mid-flight and the outcome is counted.
- The projector publishes the hero with the remaining TTL, not a fresh full TTL.
- The projector deletes the key once the URL has aged past the max-age ceiling.
- A venue with no hero row serves `venue_photo_url: null` and does not fail.
- The non-verbose nearby response carries `venue_photo_url`; the verbose branch is unchanged.
- No serving request makes a Google call under any of the above.
- The retired `photos` job still returns 404.

Pytest unit tests:
- Remaining-TTL arithmetic, including the boundary where remaining is exactly zero and the negative case.
- The refresh-queue query: ordering (oldest first), the `state='ok'` filter, the `deleted_at IS NULL` filter, and limit clamping.
- `GooglePhotoBudgetService.try_spend` — the counter, its expiry, the ceiling, and that an exhausted budget stops mid-loop rather than at entry.
- The Google client decomposition: `get_place_photos` must still issue the same calls with the same parameters (the existing `tests/test_photo_resolve.py` assertions are the gate).
- Width-suffix rewrite: a correct rewrite when the suffix matches, and a safe no-op when it does not.
- `state` transitions, including that terminal states are never re-queued.

Manual or integration checks:
- **S1, S2 and S3 from Phase 0 — blocking, before any of the above.**
- After backfill, confirm the projected hero count against the serving-view count and confirm the daily call counter matches the expected spend.

## Acceptance Criteria
- S1 has reported a p05 keyless-URI lifetime and `hero_photo_refresh_hours` is set from that measurement, not guessed.
- Every servable venue with a place id and at least one Google photo has a hero row; venues without are recorded terminally and cost no repeat calls.
- `GET /v1/venues/nearby` non-verbose returns `venue_photo_url` for a projected venue and `null` otherwise, and never 500s on a photo failure.
- No Google call is made on any serving path — provable by a scenario asserting zero client invocations across a nearby request.
- The projector writes remaining TTL and deletes past the ceiling; a stalled job produces "no photo", never a dead URL.
- Daily Google spend is bounded by the ledger, and the ledger also covers the pre-existing on-demand resolve path.
- The retired `photos` job still 404s, and the existing photo-resolve tests stay green.
- No change to `venues.venue`, `Venue`, or `venue_row.py`.

## Open Questions
- **Q1 (BLOCKING, go/no-go).** What is the real p05 lifetime of a keyless
  `lh3.googleusercontent.com` photo URI? Run S1. If it is under ~6 hours this
  design costs the same as the on-demand path it replaces and only fixes latency
  — in that case ship no list photo. **Nothing else in this plan may start until
  this number exists.**
- **Q2.** Does the `=w800` → `=w400` CDN suffix rewrite work? Run S2. If not, a
  venue that is both listed and opened costs two independent media calls forever.
- **Q3.** What is the live `SERVING_VIEW_VENUES`? Repo plans disagree (456 vs
  849 vs 1255 active). Every cost figure here scales linearly with it.
- **Q4 (product/legal, blocks the flag flip — not the code).** Google's Places
  terms require displaying `authorAttributions` alongside a photo, and a
  101x76dp thumbnail has nowhere to put it. Separately, retaining a resolved
  photo URI in RDS for days is a caching posture nobody has cleared with Google;
  it may cap the refresh interval regardless of what S1 finds. This plan carries
  `author_name` through RDS and Redis so a later answer needs no re-projection,
  but renders nothing. **Must be answered before `FEATURE_LIST_HERO_PHOTO` is
  turned on, not before this code is written.**
- **Q5.** `hero_photo_width_px` — the spec asked for "~300px"; 101dp x 3 = 303px,
  so 400 covers 3x with headroom and a plausible 4x. Confirm 400. The width is
  baked into the returned URL, so changing it later is a re-bake, not a
  re-parametrization.
- **Q6.** The hero is `photos[0]` positionally — no quality ranking, so a card's
  thumbnail may be a menu shot. Accept for v1, or scope ranking in? Note the
  vibe-profile photo categorisation that could rank it matches on exact URL
  equality against evidence photos captured as key-bearing URLs, so `category` is
  almost certainly absent on every photo today.
- **Q7.** Coverage gate before vibes_bot flips its flag. Proposal: >= 95% of the
  serving view carrying a non-null hero. A half-populated projection shows the
  fallback on a random subset of cards, which reads as broken in a way that
  uniformly-no-photos does not.
