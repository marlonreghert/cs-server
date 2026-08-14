# Deep review corpus — capture 6 months of reviews for selected venues

## Branch
feature/deep-review-corpus

## Goal
Let an operator select a subset of catalog venues and capture **every Google
review newer than a configurable window** (default 180 days) for them — well
past the 5 the Places API will ever return — storing the full corpus in RDS as
the system of record, projecting a bounded slice to Redis for serving, and
never spending a cent without an estimate first and a budget gate in front of
every paid call.

## Non-goals
- **Touching `google_places.reviews` or `venue_reviews_v1` in any way.** The
  mobile app's venue-detail review list must serve exactly what it serves
  today. Released binaries cannot be updated in lockstep, and a detail response
  that grew from 5 reviews to 300 would be a payload and rendering regression
  on every installed client.
- Serving deep reviews to the app at all. This plan lands the corpus; consuming
  it is `vibes_bot/plans/260813_bot-review-evidence.md`.
- Crawling the whole catalog, or crawling on a schedule. Selection is explicit
  and operator-driven; no cron, no automatic enrolment. A scheduled refresh is
  a deliberate follow-up once the one-off cost is known in practice.
- Replacing Google Places review enrichment. The 5 relevance-ranked reviews
  stay: they are a *different* signal (Google's notion of most-relevant), they
  arrive free with an enrichment call we already make, and they are the
  fallback for any venue never selected for a deep crawl.
- Translating reviews, or deriving structured facets from them.

## Evidence
- **Google is the cap, not us.** The Places API `Place.reviews` field is
  documented as "A maximum of 5 reviews can be returned", with no pagination.
  `app/api/google_places_client.py:450`'s `raw_reviews[:5]` is redundant
  defensive code. Prod bears this out: median 5, max 5 reviews per venue.
- **The corpus today**: 1420 of 1437 venues have reviews, 7050 total, mean 367
  characters, 2.59 MB (measured on prod 2026-08-13, read-only SCAN + MGET).
- **Every venue is reachable.** All **1438** venues with a
  `vibe_attributes_v1` key carry a non-empty `google_place_id` (measured
  2026-08-13). It is a promoted column on `google_places.vibe_attributes`
  (`app/dao/rds_venue_store.py:39-41`), so selection can always resolve the
  actor's required input and no venue is silently unreachable.
- **The actor**: `compass/google-maps-reviews-scraper` — from **$0.30 per 1,000
  scraped reviews**, accepts place IDs directly, exposes `maxReviews` per
  location, a `language` parameter, and sort options; date filtering works only
  under "Newest" sort. The actor we already run,
  `compass~google-maps-extractor`, explicitly cannot do this — its own docs say
  it "does not extract ... Images, Reviews".
- **The Apify client pattern already exists**:
  `app/api/apify_gmaps_extractor_client.py` — `_start_run` / `_poll_run` /
  `_fetch_dataset`, `POLL_INTERVAL_SECONDS = 5.0`, `MAX_POLL_ATTEMPTS = 60`, and
  a local `POLL_BUDGET_EXHAUSTED` sentinel kept distinct from the actor's own
  terminal statuses.
- **Per-unit cost constants already exist**: `app/config.py:472-493`
  (`apify_place_scraped_cost_usd`, `apify_place_details_cost_usd`,
  `apify_instagram_post_cost_usd`).
- **A budget gate already exists to copy**: `app/dao/crawl_budget_dao.py` — an
  atomic monthly Redis counter, checked **before** every actor call ("a gate
  that runs after the spend is not a gate") and decremented by each run's
  **actual** billed count, not the requested cap.
- **A background-job pattern already exists**: `app/routers/admin_trigger_router.py:440-465`
  mints a `job_id`, guards with `job_lock`, launches an asyncio task and returns
  immediately; `GET /admin/jobs/runs/{job_id}` reads the run record; and
  `POST .../estimate` (photo archive, `:520-527`) is the precedent for
  *costing a run before authorising it*.
- **A selection surface already exists**: `_venue_cache_flags_bulk`
  (`app/routers/admin_trigger_router.py:1040-1090`) already computes a
  per-venue `reviews` presence flag for the venue-inventory panel via
  `get_venue_reviews_bulk`.
- **Adding an enrichment family is a known three-place change**: the
  `_ENRICHMENT` registry (`app/dao/rds_venue_store.py:37-50`), the Redis DAO
  setter/getter/deleter (`app/dao/redis_venue_dao.py`), and `_REBUILD_MODELS`
  (`app/services/redis_projection_service.py:60-71`), plus a migration.
  Migration head is `0039_event_merge_suggestions`.
- **Redis is the constraint that matters.** Measured on prod 2026-08-13:
  `used_memory` **20.2 MB** across **18,382 keys**, `maxmemory` **0** with
  policy **noeviction**, on a box with 3.9 GB total and 2.1 GB available. There
  is headroom, but there is no ceiling and no eviction — Redis grows until the
  host does something about it. An unbounded review projection is the single
  way this feature could hurt production.

## Current Behavior
`GooglePlacesEnrichmentService` writes up to 5 reviews per venue to the
`google_places.reviews` enrichment table; the projector re-asserts them to
`venue_reviews_v1:{venue_id}`; vibes_bot's `ReviewsResolver` serves them on
`GET /venues/{id}`. There is no way to obtain more, no operator-selectable
review crawl, and no review-specific spend accounting.

## Desired Behavior
1. An operator must be able to select venues explicitly by id list, and
   optionally narrow a candidate set by filter (city, venue type, "has no deep
   reviews yet"). When both are given the **id list is authoritative** — a
   filter may never widen a run beyond what was explicitly selected.
2. A run must be **estimated before it is authorised**: a dedicated estimate
   call returns the selected venue count, the venues skipped and why, the
   worst-case review count and the worst-case USD cost, and spends nothing.
   The estimate is an upper bound, not a prediction — how many reviews fall
   inside the window is unknowable without crawling, and it must be labelled
   as a bound.
3. A run must fetch, per selected venue, reviews sorted newest-first, stopping
   at the **window edge** (`reviews_deep_window_days`, default 180) or the
   **per-venue cap** (`reviews_deep_max_per_venue`), whichever comes first.
4. A re-run for a venue that already has deep reviews must fetch only what is
   **newer than the newest stored review**, so a refresh costs a handful of
   reviews rather than the whole window again.
5. Captured reviews must be **merged and deduped**, never replaced. A review
   that ages out of the window is kept: evidence that a venue has a covered
   play area does not expire the way busyness does, and re-fetching what we
   already paid for is pure waste.
6. The monthly review budget must be checked **before** every actor call. A run
   that would cross the ceiling must stop at the boundary, persist everything
   already fetched, and report exactly which venues it did not reach — never
   silently truncate the selection.
7. RDS must hold the **full** captured corpus. Redis must carry a **bounded
   per-venue slice** (`reviews_deep_projection_max`, newest first).
8. Per-venue truncation — hitting the cap before reaching the window edge —
   must be recorded on the stored record and surfaced in the run summary, so
   "we have 6 months for this venue" is never assumed when we have 300 reviews
   covering three weeks.
9. A failure on one venue must not abort the run: it is isolated, counted, and
   named in the summary, mirroring the projector's per-venue isolation.
10. Nothing about the existing Places review path, the projection of
    `venue_reviews_v1`, or the app's detail response may change.

## Implementation Approach
**Models.** `VenueReview` gains two optional fields — `review_id` (the actor's
own id, when present) and `source` (`google_places` | `apify_gmaps`) — both
optional so every existing stored payload still validates. A new
`VenueReviewsDeep` wraps `venue_id`, the review list, and the provenance the
run summary and the truncation rule need: `window_days`, `fetched_at`,
`oldest_publish_time`, `newest_publish_time`, and `truncated`.

**Client.** New `app/api/apify_gmaps_reviews_client.py`, mirroring the existing
extractor client's structure verbatim — same start/poll/fetch shape, same
`POLL_BUDGET_EXHAUSTED` sentinel distinction, same error type. Input is a place
id list with `maxReviews`, newest sort, and `language` from the existing
`LANGUAGE_CODE`.

**Amended during execution (2026-08-13): one actor run per venue, not batched
place ids.** The original text called for batching several place ids per run to
amortise Apify's per-run overhead. That is incompatible with this plan's own
approved scenario *"One venue's failure does not abort the run"*: a single Apify
run returns ONE terminal status covering every place id it was given, so it
cannot fail for one venue and succeed for the others. The Gherkin wins — the
actor bills per review event, so per-venue runs cost no more in event terms, and
one bad place id destroying fifty venues' worth of paid work is a far worse
failure than slower throughput on a one-off backfill. `reviews_deep_batch_size`
consequently means "how many venues share one account-headroom lookup", not
"place ids per run"; see `deep_review_crawl_service.py`'s module docstring.

**Service.** New `app/services/deep_review_crawl_service.py` owning: candidate
resolution (id list ∩ filter, place-id lookup, skip-and-report for anything
unresolvable), estimation, batched execution, the incremental cursor, merge and
dedup, and persistence. Dedup key is `review_id` when the actor supplies one,
else `(author_name, publish_time, first 64 chars of text)` — the fallback
exists because a scraped payload without a stable id must not duplicate on
every re-run.

**Persistence.** New enrichment family `venues.reviews_deep`: migration `0040`
chaining from `0039_event_merge_suggestions` and creating the table in the
existing `venues` schema alongside `menu_data`/`vibe_profile`; a `_ENRICHMENT`
registry entry with no promoted columns; `set/get/delete/get_..._bulk` on
`RedisVenueDao`, keyed `venue_reviews_deep_v1:{venue_id}`; and a `_REBUILD_MODELS`
entry so the projector re-asserts and soft-delete-propagates it exactly like
every other family.

**The projection slice is a deliberate, measured decision.** At the 300-review
cap, 1438 venues × ~367 characters is roughly **158 MB** — about eight times
today's entire Redis, on an instance with no `maxmemory` and `noeviction`. So
RDS holds everything and the projector writes only the newest
`reviews_deep_projection_max` (default 40) per venue: ~21 MB, which roughly
doubles Redis and leaves the box comfortable. Raising that knob is a deliberate
act with a memory number attached, not a default anybody inherits by accident.

**Budget.** New `ReviewCrawlBudgetDao` mirroring `CrawlBudgetDao` exactly —
atomic monthly counter, distinct key namespace `review_crawl_budget_v1:{year_month}`
so it can never be conflated with the Instagram result budget, checked before
each batch and incremented by the actual returned count. Ceiling:
`reviews_deep_monthly_review_budget = 30000` (~$9/month at
`apify_review_cost_usd`).

**The review budget is not additive — it competes with production.** Measured
2026-08-13: the prod Apify account (`STARTER`) has
`maxMonthlyUsageUsd: 29`, a **hard cap** above which Apify refuses runs rather
than billing extra, and the current cycle (2026-07-29 → 2026-08-28) has already
consumed **$12.45**, essentially all of it `PAID_ACTORS_PER_EVENT` from the
Instagram/events crawl and the photo scrapers. At ~$0.80/day that cycle projects
to ~$24.5 unaided, leaving roughly **$4.5 of headroom**. A review crawl that
spent its full $9 allowance this cycle would therefore push the account into the
cap and **start failing the production events crawl** — a serving regression
caused by a background job, which is the worst shape this feature could take.

So the local review counter is necessary but **not sufficient**. The gate must
also refuse on the SHARED account limit:

- Before each batch, read Apify's own `/v2/users/me` (`maxMonthlyUsageUsd`) and
  `/v2/users/me/usage/monthly` (`totalUsageCreditsUsdBeforeVolumeDiscount`) and
  compute remaining account headroom.
- Refuse the batch unless
  `headroom_usd − batch_cost_usd ≥ reviews_deep_reserved_headroom_usd`
  (a new config, default **$8**) — the reserve that keeps the Instagram/events
  crawl alive for the rest of the cycle. The reserve is the point: the review
  crawl must yield to production, never the other way round.
- A lookup failure is a **refusal**, not a pass. An unknown headroom is treated
  as zero headroom.
- Surface both numbers on the estimate response so an operator sees the real
  constraint before authorising, not after.

**Admin surface.** Register `reviews_deep_crawl` in the existing `JOB_REGISTRY`
so it inherits `job_lock`, the minted `job_id`, the background task and the run
record with no new control flow. Add `POST /admin/reviews-deep/estimate`
(spends nothing) beside it. Extend `_venue_cache_flags_bulk` with a
`reviews_deep` flag so the venue-inventory panel can show, and let the operator
select on, what has already been captured.

## Data, Config, And API Impact
- **Migration `0040_reviews_deep`** — creates `venues.reviews_deep` with the
  same shape as the other enrichment tables (`venue_id` PK, `payload` jsonb,
  `updated_at`, `deleted_at`), chaining from `0039_event_merge_suggestions`.
  Additive; nothing existing is altered.
- **New Redis key** `venue_reviews_deep_v1:{venue_id}`, written only by
  cs-server's projector — the sole-writer invariant is preserved.
- **New config** in `app/config.py` (+ `config.example.json`, `.env.example`):
  `apify_reviews_actor` (`compass~google-maps-reviews-scraper`),
  `apify_review_cost_usd` (`0.0003`), `reviews_deep_window_days` (`180`),
  `reviews_deep_max_per_venue` (`300`), `reviews_deep_projection_max` (`40`),
  `reviews_deep_batch_size`, `reviews_deep_monthly_review_budget` (**30000**,
  ~$9/month), and `reviews_deep_reserved_headroom_usd` (**8.0**) — the shared
  Apify quota this job must always leave for the production crawls.
- **New admin endpoints**, behind the existing admin auth:
  `POST /admin/reviews-deep/estimate` and the `reviews_deep_crawl` entry under
  the existing `POST /admin/trigger/{job_name}`. `GET /admin/jobs/runs/{job_id}`
  needs no change.
- **`VenueReview` gains two optional fields.** Both optional, so every payload
  already stored keeps validating and the app — which reads
  `google_places.reviews`, untouched by this plan — sees no difference.
- **No app-facing API change. No change to `google_places.reviews`.**

## Error Handling And Observability
- Per-venue isolation: an actor failure, an unparseable item, or a persistence
  error for one venue is caught, counted, named in the run summary, and the run
  continues — mirroring `RedisProjectionService`'s per-venue try.
- A budget refusal is a **first-class outcome**, not an error: the run ends
  `partial`, reports the venues not reached, and everything already fetched is
  persisted.
- Poll-budget exhaustion keeps the existing client's distinction between "the
  actor gave up" and "we stopped waiting".
- New metrics in `app/metrics.py`:
  - `deep_reviews_fetched_total{outcome}` — `stored` | `duplicate` |
    `out_of_window`.
  - `deep_review_crawl_venues_total{outcome}` — `ok` | `skipped_no_place_id` |
    `truncated` | `error` | `budget_stopped`.
  - `deep_review_crawl_cost_usd_total` — counter, actual billed reviews ×
    `apify_review_cost_usd`.
  - `deep_review_budget_remaining` — gauge, so the ceiling is visible before it
    bites rather than after.
  - `deep_review_crawl_duration_seconds` — histogram.
  - `deep_reviews_projected_venues` — gauge, set by the projector, alongside a
    log line carrying the projected byte total so Redis growth is observable
    from the same place the memory risk lives.
- Every log line carries `job_id`. Review text is never logged.

## Test Plan
Feature file: `tests/bdd/enrichment/deep-review-corpus.feature`

Scenarios:
- An operator selects three venues and the run captures every review inside the
  window for each.
- Reviews older than the window are not stored.
- A venue with more in-window reviews than the per-venue cap is stored
  truncated, and the run summary says so.
- A re-run fetches only reviews newer than the newest stored review.
- A re-run does not duplicate reviews already stored.
- Reviews that have aged out of the window since capture are retained, not
  pruned.
- A venue with no `google_place_id` is skipped, reported, and never billed.
- The estimate call returns a bound and spends nothing.
- The monthly review budget is checked before the actor call, not after.
- A batch is refused when it would leave less than the reserved Apify headroom,
  even though the local review budget still allows it.
- An Apify usage lookup failure refuses the batch rather than letting it pass.
- A run that would cross the budget stops at the boundary, persists what it
  fetched, and names the venues it did not reach.
- One venue's actor failure does not abort the run.
- The existing `google_places.reviews` payload and `venue_reviews_v1` are
  byte-identical after a deep run.
- The projector writes only the newest `reviews_deep_projection_max` reviews
  per venue while RDS keeps the full set.
- A soft-deleted deep-review row propagates its absence to Redis on the next
  projection cycle.
- A filter may narrow but never widen an explicit id selection.

Pytest unit tests:
- `tests/unit/services/test_deep_review_crawl_service.py` — window boundary
  (inclusive/exclusive at exactly N days); the incremental cursor; merge/dedup
  with and without `review_id`; the truncation flag; candidate resolution and
  skip reporting; the id-list-is-authoritative rule.
- `tests/unit/dao/test_review_crawl_budget_dao.py` — atomic increment, month
  rollover in UTC, check-before-spend ordering, a failed read never reading as
  "budget available".
- `tests/unit/api/test_apify_gmaps_reviews_client.py` — input construction,
  poll-budget exhaustion vs actor terminal status, malformed dataset items
  skipped.
- `tests/unit/services/test_redis_projection_reviews_deep.py` — the projection
  slice cap, newest-first ordering, and deletion propagation.
- Migration test in the existing style, asserting `0040` chains from `0039` and
  is reversible.

Manual or integration checks:
- **A one-venue trial run before any batch.** Confirm the actor's real output
  field names, the effect of newest-sort plus the date filter, and the actual
  billed count against `apify_review_cost_usd` — the cost model is only as good
  as that first invoice line.
- After the trial, read `deep_review_crawl_cost_usd_total` and Redis
  `used_memory` before and after, and compare against the 21 MB projection.

## Acceptance Criteria
- For a selected venue, every review newer than the window is captured, up to
  the per-venue cap, and truncation is reported when the cap binds.
- A second run over the same venue bills only for reviews published since the
  first run.
- `GET /venues/{id}`'s `venue_reviews` is unchanged before and after a deep
  crawl, on the same venue.
- No paid call is ever issued without the budget check preceding it, and a
  budget stop leaves every already-fetched review persisted.
- Redis growth after a full-catalog run matches the projection-slice estimate
  within an order of magnitude, and `used_memory` stays far below the host's
  available memory.
- Venues without a `google_place_id` are reported, not silently dropped.
- `make test` passes and the feature file's `@wip` tag is removed.

## Open Questions
None. Both were answered on 2026-08-13:

1. **Monthly review budget: 30,000 reviews (~$9).** Encoded as
   `reviews_deep_monthly_review_budget`. A cautious start — roughly a third of
   the catalog per month at depth 50, or the whole catalog at depth 20 — chosen
   deliberately so the first invoice is seen before committing to a full corpus.
2. **Prod runs Apify `STARTER`, $29/month, `maxMonthlyUsageUsd: 29` — a hard
   cap, shared with production.** $12.45 of the 2026-07-29 → 2026-08-28 cycle
   was already consumed by the Instagram/events crawl and photo scrapers. This
   is why the budget design above gained a second, account-level gate and the
   `reviews_deep_reserved_headroom_usd` reserve: the review crawl must never be
   the reason a production crawl is refused.

**Scheduling consequence (not a blocker for 2a/2b).** With ~$4.5 of projected
headroom left in the current cycle, the 2c backfill must not run before the
next cycle opens on **2026-08-29** — or must be sized to the measured headroom
at the time. 2a (build) and 2b (the one-venue trial, ~50 reviews ≈ $0.015) are
unaffected and can proceed immediately.
