# Redis Projection Decoupling (Pipelines Write Only RDS)

## Branch
feature/redis-projection-decoupling

## Goal
Complete the RDS-as-system-of-record migration by **removing Redis from every
write path**. Pipelines and admin writes persist **only** to RDS; a scheduled
**projector** is the sole writer of Redis and feeds it exclusively from RDS;
pipelines read their **inputs** from RDS; end users keep reading Redis unchanged.

Target data flow:
- Users read from Redis (unchanged serving contract).
- Redis is fed from RDS (by the projector — Redis never written by a pipeline).
- Pipelines write to RDS.
- Admin panel writes to RDS.
- Pipelines read from RDS when they need data.

This **supersedes the synchronous write-through projection** shipped in
`plans/rds_system_of_record_01_06_26.md` (RDS-first → then project Redis in the
same call). That plan's Phase 0 (provision) and Phase 1 code (flag-gated
write-through) are **as-built**; this plan replaces the *synchronous projection
step* with an asynchronous, RDS-fed projector and moves pipeline reads to RDS.

## Non-goals
- Do not change the public serving contract. `GET /v1/venues/nearby` and
  vibes_bot's `CrowdSenseClient` keep reading Redis.
- Do not move **cache-freshness bookkeeping** out of Redis. The TTL-based
  "is-this-fresh / already-done" gating (photos refetch trigger, instagram cache
  TTL + not-found negative cache, the `list_cached_*` "done" sets) is a cache
  concern and **stays Redis-only** (decision: "pipes read *data* from DB;
  freshness bookkeeping stays in Redis"). Pipelines still touch Redis for these
  specific gating reads only — never for data writes.
- Do not adopt PostGIS serving. The geo nearest-neighbour query stays in Redis;
  the projector rebuilds the Redis geo index from RDS lat/lng (as today).
- Do not require a Redis flush/rename/key-format migration. Existing Redis key
  formats stay the projection target.
- Do not build an incremental/outbox change-data-capture projector in this plan.
  A scheduled full reprojection (~1k venues) is the v1; dirty-tracking is a later
  optimization only if sub-cadence latency is needed.
- Do not do the cutover here. `rds_enabled=true` + `backfill_rds` is a
  precondition owned by `rds_system_of_record_01_06_26.md` (see Open Questions).

## Evidence
### Single shared DAO is the thing being split
- `app/container.py:108` builds **one** `self.redis_venue_dao =
  VenueRepository(redis_client, rds_store=...)` and injects it into **both** the
  serving handler (`VenueHandler`, `container.py:314`) **and** all 9 pipeline
  services (`container.py:131,138,165,184,231,260,280,300,302,338`). Reads are
  inherited Redis reads; writes are write-through.
- `app/dao/venue_repository.py:36-178` — every write does `rds_store.upsert/...`
  (truth) **then** `super().set_*` (the **synchronous Redis projection** this
  plan removes). Reads are inherited from `RedisVenueDAO` (Redis).
- `app/container.py:91` already builds `self.redis_only_dao = RedisVenueDAO(...)`
  (Redis-only) used by the projection service — this is the serving/projection
  Redis writer the projector reuses.

### Pipeline reads form a cross-stage DAG (reads + writes must move together)
Verified pipeline reads against the shared DAO (`grep venue_dao.get_/list_`):
- photo_enrichment writes photos → vibe_classifier reads `get_venue_photos`
  (`vibe_classifier_service.py:95`).
- instagram writes handle → read by vibe_classifier (`:123`), menu_photo
  (`menu_photo_enrichment_service.py:103`), ig_posts
  (`instagram_posts_enrichment_service.py:51`), google_places
  (`google_places_enrichment_service.py:407,477`).
- ig_posts → vibe_classifier `get_venue_ig_posts` (`:128`).
- menu_photo → menu_extraction `get_venue_menu_photos`
  (`menu_extraction_service.py:69`).
- google_places `vibe_attributes`/`reviews` → vibe_classifier `get_venue_reviews`
  (`:136`), refresher `get_vibe_attributes`
  (`venues_refresher_service.py:204`).
- Nearly every service calls `list_active_venue_ids()` + `get_venue()`.

Consequence: if writes go RDS-only but reads stay on Redis, stage N+1 reads a
**stale Redis** for stage N's output until the projector ticks. So pipeline
reads must move to RDS **in the same change** as the write decoupling.

### Cache-freshness gating reads that STAY Redis-only (the carve-out)
- `photo_enrichment_service.py:121` `list_cached_venue_photos_ids()` — the
  TTL-eviction refetch trigger (section G of the prior plan; **must** stay Redis;
  RDS has no TTL, so photos would look permanently present and never refetch).
- `vibe_classifier_service.py:236-237`, `menu_extraction_service.py:180`,
  `instagram_posts_enrichment_service.py:44` — `list_cached_*` "already-done /
  ready" sets.
- Instagram cache TTL + not-found negative caching inside
  `instagram_enrichment_service.py` (Redis TTL keys).

### RDS read layer is a thin adapter
`app/dao/rds_venue_store.py` already exposes `get_venue` (payload),
`get_enrichment(table_key, venue_id)` (payload + `deleted_at`),
`get_live_forecast`, `list_active_venue_ids`. Typed reads
(`get_vibe_attributes → VibeAttributes`, etc.) are model reconstruction over
those getters — the exact pattern `redis_projection_service.rebuild_redis_from_rds`
(`:107-127`) already uses. Net-new generic reads still needed: a venue-payload
list (for `list_all_venues`-equivalent) and any promoted-column count helpers.

### Scheduler + admin jobs already exist
- `main.py:301-345` `AsyncIOScheduler` with `IntervalTrigger`/`CronTrigger`
  jobs (venue catalog, live forecast, weekly). The projector is a new interval
  job here.
- `app/routers/admin_trigger_router.py` already has `rebuild_redis` (RDS→Redis)
  and `backfill_rds` jobs, both guarded on `rds_store` not None
  (`:129-133` region). `rebuild_redis_from_rds()` is ~90% of the projector.

## Current Behavior
- One `VenueRepository` serves both serving and pipelines. Pipeline writes are
  synchronous write-through: RDS first, then Redis projection in the same call.
  Pipelines read from Redis. With `rds_enabled=false` (prod today) it degrades to
  pure Redis — RDS is neither read nor written and is empty.
- Photos/instagram/menu/vibe "done" gating + photo refetch are driven by Redis
  keys written as a side-effect of the synchronous projection.

## Desired Behavior
- **Pipelines write only RDS.** No pipeline write touches Redis. A write that
  fails against RDS fails loudly (logged + metered) and is not silently dropped.
- **The projector is the sole Redis writer for pipeline/venue/admin data.** A
  scheduled job reads active venues + enrichment + live busyness from RDS and
  projects them into the existing Redis keys (incl. `GEOADD` geo index). It is
  idempotent; it **removes venues deprecated in RDS** from serving (B1) but does
  **not** prune orphans that have no RDS row at all. Serving freshness for this
  data = projector cadence (eventual consistency is acceptable here).
- **Engagement is the one deliberate Redis-write exception — DB-first then
  IMMEDIATE projection (latency-critical).** Favorites/hot_likes are explicit user
  actions: a favorite or hot-like MUST appear "very quickly", so engagement does
  **not** go through the slow projector. The engagement API writes **RDS first**
  (including every `add_hot_like`), then **synchronously projects the change into
  Redis in the same request** so the user sees it immediately on their next read.
  Engagement history stays durable in RDS; users read engagement from Redis. This
  is a documented carve-out from "pipes don't write Redis" — engagement is a user
  action, not a pipeline. (hot_likes' TTL'd Redis trending counter is therefore
  the live signal as today; it is not reconstructed from append-only RDS events.)
- **Pipelines read data inputs from RDS.** Cross-stage inputs (venue,
  vibe_attributes, instagram, ig_posts, photos content, reviews, opening_hours,
  menu_photos, menu_data, vibe_profile, weekly, live) are read from RDS, so a
  later stage sees an earlier stage's output without waiting for projection.
- **Cache-freshness gating stays Redis-only.** The photo refetch trigger and the
  `list_cached_*` / TTL / not-found gating reads continue to read Redis. RDS is
  never consulted for a refetch/TTL decision (preserves the photos-TTL deliverable
  and instagram negative cache). These Redis cache sets are populated by the
  projector (their producer), so the projector cadence must stay **tighter than**
  any enrichment cadence to avoid duplicate paid fetches (see Error Handling).
- **Admin writes go to RDS only.** Admin venue edits + config writes persist to
  RDS and surface in serving via the projector (cs-server side; vibes_bot panel
  proxying is the companion plan).
- **Serving is unchanged.** The handler reads a Redis-only DAO; an RDS outage
  cannot break `GET /v1/venues/nearby`.

## Implementation Approach
### A. Split the single DAO into three roles (container rewiring)
- **serving_dao** = `RedisVenueDAO` (Redis-only). Inject into `VenueHandler`
  (and the read side of `AddVenueHandler`/`VenueService` nearby). Reads Redis;
  never writes RDS.
- **pipeline_repo** = a new RDS-backed repository exposing the same typed
  interface the 9 services use, but **reading and writing RDS** (writes: the
  existing `VenueRepository` RDS branch, minus the `super().set_*` Redis
  projection; reads: typed model reconstruction over `rds_store.get_*`). Inject
  into all enrichment/refresh services. It holds a Redis handle **only** for the
  carve-out gating reads (`list_cached_*`, photo refetch trigger), which it
  delegates to a Redis-only DAO unchanged.
- **projector** = `RedisProjectionService` promoted: `rebuild_redis_from_rds()`
  becomes the projection body, run on a schedule (and still on-demand via admin).

Cleanest mechanics: keep `VenueRepository` as the RDS writer but **drop the
`super().set_*()` projection calls** from its write methods (writes become
RDS-only); add typed RDS read methods (override the inherited Redis getters that
return *data* to read RDS, while explicitly delegating the carve-out gating
reads to Redis). Serving uses a separate plain `RedisVenueDAO` so it is unaffected
by the read override.

### B. The scheduled projector
- **B0 — MUST run OFF the serving event loop (correctness constraint, learned in
  prod during cutover).** `rebuild_redis_from_rds()` / `backfill_rds_from_redis()`
  are **synchronous, blocking** (blocking SQLAlchemy + Redis, no `await`). Today
  the admin trigger runs them inline in an `async` task and
  `AsyncIOScheduler` runs jobs **on the serving loop** — so a sync projector there
  **stalls `GET /v1/venues/nearby` and `/health` for the entire run** (observed:
  the one-time backfill blocked serving and timed out HTTP calls; "unhealthy"
  flapped). The projector MUST therefore execute off the serving loop:
  `loop.run_in_executor(...)` (thread), a dedicated worker thread/process, or a
  separate sidecar container/cron. Do **not** schedule the sync body directly on
  `AsyncIOScheduler`. Same fix applies to the admin `backfill_rds`/`rebuild_redis`
  triggers (wrap in a thread executor) so a manual run can't stall serving either.
- Add a `redis_projection` interval job in `main.py` (new
  `redis_projection_minutes` setting) that runs the projection **via the executor
  per B0** (not a bare sync call). Keep the existing `rebuild_redis` admin trigger
  for manual runs (also executor-wrapped).
- Cadence must be tighter than enrichment cadence (default a small interval,
  e.g. 1–2 min; ~1k venues is cheap on write volume — the risk is loop-blocking,
  not throughput, hence B0).
- Live busyness flows the same way: live refresh writes RDS only; the projector
  reflects it to Redis on the next tick.

**`rebuild_redis_from_rds` was built as a RARE recovery tool. Running it
continuously as the projector turns two of its assumptions into correctness
bugs — both MUST be fixed here, they are design requirements not execute-time
details:**

- **B1 — Deprecation removal (serious; protects the PR #21 eligibility
  feature).** Today `soft_delete_venue` removes the venue from Redis via the
  synchronous `super()` call. In the decoupled model pipelines write RDS only,
  and `rebuild_redis_from_rds` iterates `list_active_venue_ids()` and never
  removes anything. So a venue that goes **active → deprecated** would never be
  re-projected and **never removed from Redis → served forever**, silently
  defeating eligibility filtering (drugstores/markets/junk). Fix — split the two
  notions of removal:
  - **Deprecated in RDS = a positive removal signal** → the projector MUST remove
    that venue from the Redis serving keys + geo index. Add a
    `list_deprecated_venue_ids()` (or `updated_at`-windowed sweep) and a
    Redis-removal path in the projector.
  - **No RDS row at all = absence of signal** (partial-read safety) → the
    projector still does NOT prune it. Additive for orphans; pruning only on the
    explicit deprecate signal.

- **B2 — Photo TTL must count down, not reset.** `redis_projection_service.py:113-118`
  is commented "remaining TTL" but actually calls `set_venue_photos(...)`, which
  applies the **full configured setex TTL**. Acceptable for a rare manual rebuild;
  run every 1–2 min it **re-stamps a fresh full TTL on every photo every run →
  photos never expire in Redis → `list_cached_venue_photos_ids` always sees them
  present → refetch never fires → stale Google URLs served indefinitely** (the
  exact section-G bug). Note `get_enrichment` for photos currently returns only
  `(payload, deleted_at)` — it does not return `updated_at`, so remaining-TTL is
  not even computable today. Fix:
  - Plumb `updated_at` (the durable `fetched_at`) through `get_enrichment` for
    photos.
  - Project photos with `remaining = configured_ttl − age(updated_at)`; if
    `remaining ≤ 0`, **skip** (project absent) so the refetch trigger fires.

- **Read-cost note → incremental projection sooner than "later".** A full
  reprojection re-reads, per venue, `get_venue` + ~8 enrichment getters +
  weekly×7 + live ≈ 16 round-trips × ~1k venues per run. At a 1–2 min cadence on
  `db.t4g.small`, verify this read cost. Combined with B1's need to also sweep
  deprecated venues, this — not the (cheap) write volume — is the real argument
  for moving to incremental/dirty-tracking projection earlier. v1 stays the
  scheduled full reprojection; size the read cost before committing the cadence.

### C. Admin writes to RDS (cs-server side)
- Admin venue-edit + generic config endpoints write **RDS only**; the projector
  surfaces them to Redis. (This merges the old plan's Phase 2 "config in RDS,
  mirrored to Redis" into the projector model — config mirror becomes part of
  projection, not a synchronous mirror write.) vibes_bot admin panel proxying is
  the separate companion plan.

### D. Pipeline read carve-out wiring (explicit per-call disposition)
- **Read from RDS (data):** `get_venue`, `get_vibe_attributes`,
  `get_venue_instagram`, `get_venue_ig_posts`, `get_venue_photos` (content),
  `get_venue_reviews`, `get_opening_hours`, `get_venue_menu_photos`,
  `get_venue_menu_data`, `get_venue_vibe_profile`, `get_week_raw_forecast`,
  `get_live_forecast`, `list_active_venue_ids`, `list_all_venues`.
- **Stay Redis-only (cache-freshness gating):** `list_cached_venue_photos_ids`
  (photo refetch trigger), `list_cached_ig_posts_venue_ids`,
  `list_cached_menu_photos_venue_ids`, `list_cached_vibe_profile_venue_ids`,
  instagram cache-TTL + not-found negative-cache reads.
- **Serving-only (Redis):** `get_nearby_venues` and the handler's read-set —
  on the serving_dao, untouched.

### E. As-built gaps to fold in
- `set_google_business_status`, `delete_live_forecast` are not RDS-aware on the
  repository today — route them through RDS (status as a venue promoted-column
  update; live delete as an RDS current-state delete) so no write escapes to
  Redis-only.
- `count_*` analytics reads can move to RDS (or stay Redis-derived) — low
  priority; specify during execution.

### F. Engagement carve-out (DB-first, immediate synchronous projection)
- Engagement is explicitly **excluded** from the slow projector. The
  `EngagementService` keeps its as-built behavior: on `add_favorite` /
  `remove_favorite` / `add_hot_like` / `remove_hot_like` it writes **RDS first**
  (truth), then **synchronously projects to the existing Redis keys in the same
  request** (`user_favorites:{user_id}`, `hot_likes:v1:{venue_id}` with EXPIRE).
  Users keep reading engagement from Redis; the durable history is in RDS.
- **Latency requirement (hard):** the round trip must stay fast — a favorite/like
  must be visible on the user's next read, NOT after a 1–2 min projector tick.
  So engagement is the documented exception to "no pipeline writes Redis"
  (engagement is a user action, not a pipeline). This is already how
  `EngagementService` is wired; the decoupling work must **not** reroute it
  through the projector. Add a unit test asserting an engagement write projects
  Redis in-band (synchronously), independent of the projector.
- Partial-failure stays as-built: RDS commit then Redis projection; if Redis
  projection fails after the RDS commit, the API returns non-success so vibes_bot
  retries (idempotent upsert) — the user's action is never silently lost.

### G. Rollout / transition — Redis is NEVER emptied
The user's hard constraint: never empty Redis; populate the DB first, get the
projector in sync, then decouple. Ordered transition (all reversible):
1. **Precondition (old plan cutover):** deploy `rds_enabled=true` + RDS
   env/secrets (synchronous write-through still ON), run `backfill_rds`, verify
   counts. Redis stays exactly as-is throughout — backfill only writes RDS.
2. **Start the projector alongside write-through** (`redis_projection_enabled=true`
   while write-through is still active). The projector now re-asserts the same
   Redis from RDS; confirm it runs green and `redis_projection_lag_seconds` is
   low. Redis is being fed by BOTH paths and never goes empty.
3. **Only then flip pipelines to RDS-only writes** (remove the synchronous
   projection from the write path). Because the projector is already feeding Redis
   from a populated, in-sync RDS, serving never sees an empty/stale Redis.
4. **Rollback:** `redis_projection_enabled=false` restores synchronous
   write-through; the projector stops; no data is lost (RDS is the truth, Redis
   is intact). The DBeaver prod-data smoke can run from step 1 onward.

## Data, Config, And API Impact
- **Config (new):** `redis_projection_enabled` (default false → today's
  synchronous write-through behavior), `redis_projection_minutes` (interval).
  Gating the decoupling behind a flag lets the cutover and decoupling roll
  separately and roll back.
- **No DDL change.** Schema from `0001_baseline_schemas` is sufficient (venue,
  enrichment, `besttime.live_forecast`, audit history). Admin venue-edit reuses
  existing tables.
- **Redis:** key formats unchanged. Now written *only* by the projector +
  engagement (carve-out) — never by a pipeline.
- **API:** internal admin venue-edit/config endpoints write RDS only (no
  behavior change to public serving). vibes_bot companion handles the panel.
- **Precondition:** `rds_enabled=true` + `backfill_rds` complete (old plan).

## Error Handling And Observability
- **RDS-only write failure:** raise + log with venue/op context, increment
  `rds_writes_total{table,result="error"}`; pipeline continues to the next item.
  No Redis fallback (would lose durability).
- **Projector failure / RDS outage:** the projector run is a safe no-op (logs +
  `redis_projection_runs_total{result="skipped"}`); it never flushes Redis, so
  serving keeps running on the last good projection. Pipeline writes fail loudly
  during the outage; nothing corrupts Redis.
- **Duplicate-paid-fetch guard:** because the carve-out gating sets are produced
  by the projector, emit `redis_projection_lag_seconds` (now − oldest
  unprojected `updated_at`) and **alert if lag approaches enrichment cadence**;
  document that `redis_projection_minutes` ≪ enrichment job cadence is an
  invariant. (Stage-internal candidate lists are iterated once per run, so a
  single run never double-fetches.)
- **Metrics:** `redis_projection_runs_total{result}`,
  `redis_projection_duration_seconds`, `redis_projection_venues`,
  `redis_projection_lag_seconds`; reuse `rds_writes_total` /
  `rds_write_duration_seconds`; add `pipeline_rds_reads_total{type,result}`.

## Test Plan
Feature file: `tests/bdd/persistence/redis_projection_decoupling.feature`

Scenarios (cs-server runtime contract):
- Pipeline venue upsert writes RDS only, Redis untouched until projection; not
  yet served.
- Projector reflects RDS → Redis (incl. geo index); then served.
- Enrichment persists to RDS; projector completes the serving read-set.
- A later pipeline stage reads a prior stage's output from RDS (not stale Redis).
- Photo refetch trigger still reads Redis only; RDS never consulted (carve-out).
- **Repeated projector runs let the photo TTL count down to expiry (B2)** — they
  do not re-stamp a fresh full TTL; once aged past TTL the refetch fires.
- Skip-already-done gating derives from the Redis cache set; pipeline does not
  write that set itself.
- Admin venue edit writes RDS only; surfaces after projection.
- RDS outage: serving continues from Redis; pipeline write fails loudly;
  projector is a safe no-op.
- **A venue deprecated in RDS is removed from serving by the projector (B1).**
- Projector is idempotent; prunes on the deprecate signal but leaves orphans
  (no RDS row) untouched.
- **Engagement is immediate:** a favorite/hot-like writes RDS then projects Redis
  in the same request and is visible on the next read without a projector tick.

`# bdd-exempt: infrastructure` for the scheduler cadence wiring, deploy, and the
cutover (flag-on + backfill) — provisioning/IaC validated by runbook + manual
checks.

Pytest unit tests:
- `pipeline_repo` writes: RDS upsert occurs and **no Redis projection write**
  happens (assert the Redis-only DAO is untouched by a pipeline write).
- `pipeline_repo` reads: typed getters reconstruct models from RDS payloads;
  carve-out gating reads (`list_cached_*`, photo refetch trigger) still hit
  Redis and never query RDS.
- Projector: rebuilds JSON + geo index + live busyness from RDS; idempotent;
  safe no-op when `rds_store` raises.
- **B1 deprecation removal:** a venue flipped active→deprecated in RDS is removed
  from the Redis serving keys + geo index by the next projector run; a venue with
  no RDS row at all is NOT pruned (orphan-safe).
- **B2 photo TTL countdown:** repeated projections of an aging photo set project
  decreasing remaining TTL (not a fresh full TTL); once `age ≥ ttl` the photos
  project as absent so the refetch trigger fires. (Distinct from the existing
  day-old-RDS rebuild test, which misses the fresh-but-aging case.)
- Serving DAO unaffected: handler reads Redis and never reaches RDS.
- **Engagement immediacy (§F):** an engagement write projects Redis in-band
  (synchronously) and is NOT routed through the projector; assert the favorite/
  hot-like key is present in Redis right after the API call, with no projector run.
- `redis_projection_enabled=false` preserves today's synchronous write-through
  (no regression path for rollback).
- Metrics on RDS write-failure and projector skip paths.
- As-built gaps: `set_google_business_status` / `delete_live_forecast` go to RDS
  only.

Manual or integration checks:
- `make test-feature FEATURE=tests/bdd/persistence/redis_projection_decoupling.feature`.
- Post-provision: against the scratch/staging RDS, run a pipeline write → confirm
  Redis unchanged → run projector → confirm served (proves decoupling end-to-end).
- DBeaver smoke (works on the as-built system pre-decoupling; see runbook note).

## Acceptance Criteria
- No pipeline or admin write path writes Redis; the projector is the sole Redis
  writer for pipeline/venue/admin data. (Engagement is the documented exception.)
- **Engagement is DB-first + immediately projected:** a favorite/hot-like writes
  RDS then projects Redis in the same request, visible on the next read without a
  projector tick; it is never routed through the slow projector.
- **The transition never empties Redis:** the projector runs in sync alongside
  write-through before pipelines flip to RDS-only writes; rollback restores
  write-through with no data loss.
- The scheduled projector reconstructs serving (incl. geo index + live busyness)
  from RDS; idempotent; a safe no-op during an RDS outage.
- **(B1)** A venue deprecated in RDS is removed from Redis serving + geo index by
  the projector; orphans with no RDS row are left untouched. Eligibility
  filtering (PR #21) keeps working after decoupling.
- **(B2)** The projector projects photos with remaining TTL (not a fresh full
  TTL), so repeated runs let photos expire and the Redis-only refetch trigger
  keeps firing — stale Google URLs still refresh after decoupling.
- Pipelines read data inputs from RDS; cross-stage read-after-write works without
  waiting for projection.
- The photo refetch trigger and `list_cached_*` / TTL / not-found gating stay
  Redis-only; RDS is never consulted for a freshness decision (photos-TTL and
  instagram negative-cache deliverables preserved).
- Serving (`GET /v1/venues/nearby`) is byte-for-byte unchanged and independent of
  RDS at request time.
- `redis_projection_enabled=false` reproduces today's synchronous write-through
  exactly (rollback path).
- Observability: projection runs/duration/venues/lag + RDS write-result metrics
  emitted; lag alert documented against enrichment cadence.

## Open Questions
### Resolved (2026-06-01, by user)
- **Sequencing — never empty Redis.** Populate RDS first (`backfill_rds`), bring
  the projector into sync alongside write-through, **then** flip pipelines to
  RDS-only writes (see Implementation §G). Redis is never emptied; the DBeaver
  prod-data smoke runs from the cutover step onward.
- **Engagement carve-out — confirmed DB-first + immediate projection.** Favorites
  and hot_likes (incl. every `add_hot_like`) write **RDS first**, then project
  Redis **synchronously in the same request** (NOT via the slow projector), so a
  user's action appears "very quickly". History stays durable in RDS; users read
  from Redis. hot_likes' TTL'd Redis counter stays the live signal (see §F).
- **Projector cadence for venue/pipeline data — eventual consistency OK** at the
  1–2 min cadence; only *user interactions* require immediacy (handled by §F, not
  the projector).

### Remaining (lighter — measure/decide during execution, not blocking design)
1. **Projector read cost at the chosen cadence.** A full reprojection is ~16 RDS
   round-trips × ~1k venues per run — **measure on `db.t4g.small`** before locking
   `redis_projection_minutes`. If uncomfortable, move to incremental/dirty-tracking
   projection earlier than this plan assumes (v1 stays scheduled full).
2. **`count_*` analytics reads.** Move to RDS or leave Redis-derived? Low impact;
   defaulting to leave on Redis (admin/metrics only) unless you want DB-queryable.
3. **Off-loop execution mechanism (per B0).** Choose: thread executor
   (`run_in_executor`, simplest), a worker thread, or a sidecar process/cron.
   Also note the DBeaver-smoke step (`rebuild_redis`) and any manual admin trigger
   stall serving until they're executor-wrapped — tolerable as a rare one-off, but
   wrap them as part of this work so an operator can't accidentally take serving
   down. (Discovered live: the cutover backfill, run via the HTTP trigger, blocked
   `/v1/venues/nearby` and timed out the trigger call.)

## vibes_bot companion — what's needed on their side
The cs-server engagement contract is already implemented and the vibes_bot
write-through code is **merged and deployed but dormant**
(`ENGAGEMENT_WRITE_THROUGH=false`; post-merge sanity check confirmed zero
functional drift, favorites/hot_likes still byte-for-byte on Redis, no 5xx).
Needed on the vibes_bot side, in order:
1. **Do NOT flip the engagement flag until the cs-server cutover is done**
   (`rds_enabled=true` + `backfill_rds` verified). Then set
   `ENGAGEMENT_WRITE_THROUGH=true` and re-run the same sanity check to watch the
   `engagement_write` metric come alive.
2. **Confirm low write→read latency after the flip.** Per §F, a hot-like/favorite
   must appear "very quickly"; verify the cs-server engagement API projects Redis
   in-band so vibes_bot's Redis read reflects it on the next request (no
   projector-tick delay).
3. **Admin panel (later, separate companion):** when Phase 2 admin endpoints land,
   point vibes_bot's config/venue panel writes at cs-server's RDS-backed admin API
   (writes go to RDS, surface via the projector) instead of writing Redis directly.
4. **Monitoring-gap follow-up (pre-existing, flag before activation):**
   `http_requests_total{job="vibesbot"}` has 0 series — vibes_bot HTTP
   request-rate / 5xx / p95 latency are **not scraped**, so today's "5xx = 0" is
   really "no data". Add real serving latency/error visibility on vibes_bot before
   flipping the engagement flag, so an activation regression is actually observable.

## Next Steps — who does what (based on what is already done)

### ✅ Already done
- **cs-server (code):** RDS provisioned + Alembic baseline applied (Phase 0);
  engagement API (`POST/DELETE /v1/favorites`, `POST/DELETE /v1/hot-likes`,
  pseudonymization, RDS-first + in-band Redis projection), write-through
  repository, projection/backfill/rebuild jobs — all merged in PR #22 and
  deployed to prod **flag-OFF** (`rds_enabled=false`). Dual-store contract test
  green. **Engagement is code-complete; nothing to build here to activate it.**
- **vibes_bot:** engagement write-through merged + deployed **dormant**
  (`ENGAGEMENT_WRITE_THROUGH=false`); post-merge sanity check = zero drift.
- **Plans:** this plan + the annotated old plan written.

### 👤 You (Mario) — ops + decisions (no coding)
1. **Run the cutover** (this is the single gate for engagement durability AND the
   DBeaver smoke; bdd-exempt ops): add `RDS_HOST/PORT/DB/USER/PASSWORD`,
   `RDS_SSLMODE=require`, `ENGAGEMENT_PSEUDONYMIZATION_KEY` + set `RDS_ENABLED=true`
   on the cs-server container (via vibes_bot compose/CI + GitHub secrets) →
   deploy → run the `backfill_rds` admin job once → verify counts (DBeaver
   `select count(*) from venues.venue` vs Redis). *(I can hand you the exact
   commands — just ask when you're at this step.)*
2. **DBeaver prod-data smoke** (after step 1): SSM port-forward → connect
   `localhost:5432` → edit a venue's **`payload` JSONB** → trigger `rebuild_redis`
   → see it in `GET /v1/venues/nearby` (transient; fine).
3. **Tell vibes_bot to flip** `ENGAGEMENT_WRITE_THROUGH=true` only after step 1.
4. **Decide when to start the decoupling** (this plan): it is gated on the §G
   transition (projector in sync alongside write-through *before* pipelines go
   RDS-only). Resolve the two lighter remaining items (projector read-cost
   measurement, `count_*`) and give the go for `/execute-feature`.

### 🤖 vibes_bot — (mostly done; activation + hygiene)
1. **Wait for the cutover**, then set `ENGAGEMENT_WRITE_THROUGH=true` and re-run
   the sanity check to watch the `engagement_write` metric come alive.
2. **Verify write→read latency** after the flip (favorite/hot-like appears
   immediately — §F).
3. **Close the monitoring gap** (`http_requests_total{job="vibesbot"}` = 0 series)
   before flipping, so an activation regression is observable.
4. **Later (separate companion):** point the admin config/venue panel at
   cs-server's RDS-backed admin API once Phase 2 endpoints land.

### 🤖 Me (cs-server) — only after your go on `/execute-feature` (decoupling)
*Nothing is required from me to activate engagement — it already ships.* For the
decoupling itself, on your go:
1. Split the single `VenueRepository` → serving-DAO (Redis) + pipeline-repo (RDS
   read+write) + scheduled projector; rewire the container (§A).
2. Pipelines write RDS only + read data from RDS; keep the cache-freshness gating
   reads Redis-only (§D carve-out).
3. Promote the projector to a scheduled job **with B1 (remove deprecated venues)
   + B2 (photo remaining-TTL)** — the two correctness fixes; behind
   `redis_projection_enabled`.
4. Preserve engagement immediacy (§F) and add the as-built gap writes
   (`set_google_business_status`, `delete_live_forecast`) to RDS.
5. Drive the BDD feature red→green + the unit tests; metrics + lag alert.
6. *(On request)* hand you the exact cutover commands, and/or add a
   `POST /admin/venues/{id}/reproject` single-venue projection helper.
