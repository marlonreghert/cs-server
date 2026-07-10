# Projector And Serving Bulk Reads (Refactor Assessment P1-P5)

## Branch
feature/projector-and-serving-bulk-reads

(`feature/` because the work adds new runtime capabilities — bulk per-table
RDS readers, pipelined Redis read paths, SCAN-based key iteration — rather
than tooling/lifecycle changes, even though externally observable payloads
are preserved byte-for-byte.)

## Goal
Eliminate the N+1 access patterns identified by the 2026-07-09 refactor
assessment (cs-server P1-P5) while keeping the Redis projection content/shape
and the `/v1/venues/nearby` response body **byte-equivalent** before and
after:

- P1: projector rebuild goes from ~18 SQL queries **per venue** per cycle to
  ~12 queries **total** per cycle.
- P2: `/v1/venues/nearby` goes from 7-14 sequential Redis GETs per venue to a
  handful of pipelined/MGET round-trips per request.
- P3: `update_data_quality_metrics` goes from 2 SQL queries per venue to 2
  set-queries per refresh cycle.
- P4: blocking admin/engagement handlers stop stalling the event loop
  (declare plain `def`; bulk presence flags for the inventory listing).
- P5: `KEYS`-based scans + GET-per-key loops against the shared serving Redis
  become SCAN + MGET.

## Non-goals
- Any change to projection key formats, DTO shapes, sort orders, freshness
  gating, or eligibility semantics — this plan is throughput/latency only.
- The dead-code sweep, bug fixes, scaffolding abstractions, and renames from
  the same assessment (planned separately; see
  `plans/260710_admin-breakdown-and-addvenue-fold.md` and
  `plans/260710_dead-code-sweep.md`). The `venue-type-breakdown` endpoint's
  broken DAO reference is fixed in the bug-fix plan; this plan only touches
  its `async`/`def` declaration and assumes that plan lands first.
- Caching layers, concurrency (asyncio.gather) rework, or changing the
  projector cadence.
- Rewriting the seven DAO list/count methods beyond routing them through
  SCAN/MGET (their table-driven consolidation is A2, planned separately).

## Evidence
Verified against the code on 2026-07-10:

- **P1 — projector N+1:** `app/services/redis_projection_service.py:102-133`
  loops per servable venue: `rds_store.get_venue` (103), 8 enrichment reads
  via `_REBUILD_MODELS` (112-117; table at 58-67), `_project_photos` (118 →
  one more `get_enrichment` at 168), 7 weekly-day reads (120-127), and a live
  read (128) — ~18 queries per venue per cycle. Each read opens its own
  pooled connection: `app/dao/rds_venue_store.py:352-368` (`get_enrichment`
  does `engine.connect()` per call). At 500 venues this is ~9,000 serialized
  queries every `redis_projection_minutes` (default 2,
  `app/config.py:114`; scheduled at `main.py:558-560`).
- **P2 — nearby serving N+1:** after GEORADIUS,
  `app/db/geo_redis_client.py:187-198` GETs each member sequentially (1
  GET/venue). Then `app/handlers/venue_handler.py:229-257` (`_merge`) reads
  live + weekly-day per venue (2 GETs), `339-411` (`_transform` minified
  loop) reads vibe attributes (340), photos (351), opening hours (363),
  Instagram (384), and vibe profile (405) per venue (5 GETs), and venues
  without Google hours pay 7 more GETs in `_derive_hours_from_forecast`
  (58-98, loop at 70-71). ~200 venues ⇒ 1,400-2,800 sequential round-trips
  per request on the product's primary endpoint.
- **P3 — data-quality metrics N+1:**
  `app/services/venues_refresher_service.py:208` (`update_data_quality_metrics`)
  ends with a per-venue loop at 349-368 reading live forecast + Monday weekly
  forecast per venue (2 SQL queries each via `VenueRepository`), called after
  every refresh cycle at 994, 1020, 1042, and 1113.
- **P4 — blocking `async def` handlers:**
  `app/routers/admin_trigger_router.py:682-683` — `async def
  list_venue_inventory` calls `venue_dao.list_all_venues()` (bulk RDS read)
  then `_venue_cache_flags` (658-679) per page venue: 7 weekly reads + live +
  vibe + photos + hours + instagram + reviews + menu photos + menu data +
  vibe profile ≈ 16 synchronous queries per venue **on the event loop** (up
  to 250 venues per page ⇒ ~4,000 blocking queries). Same pattern:
  `789-790` (`async def venue_type_breakdown`) and
  `app/routers/engagement_router.py:48,60,72,84,100` — five `async def`
  handlers performing synchronous RDS writes. While these run, `/health` and
  `/v1/venues/nearby` cannot be served. `app/routers/venue_router.py:38`
  already models the correct pattern (plain `def` → FastAPI threadpool).
- **P5 — `KEYS` + GET loops on the shared serving Redis:**
  `app/db/geo_redis_client.py:76-85` (`keys`) wraps blocking O(N) `KEYS`,
  used by 14 DAO call sites (`app/dao/redis_venue_dao.py` lines 323, 352,
  366, 478, 492, 697, 711, 824, 878, 948, 1004, 1015, 1116, 1127);
  `redis_venue_dao.py:359-379` (`list_all_venues`) and `871-889`
  (`count_venues_with_instagram`) additionally GET each key one by one.

Line-number drift vs the assessment: `_merge`'s def is at 183 (loop at
229-257 as cited); the GEORADIUS GET loop starts at 187 (cited 188);
`update_data_quality_metrics` is defined at 208 with the cited loop at
349-368; `list_venue_inventory`/`venue_type_breakdown` decorator lines are
682/789 with the `def`s at 683/790. All immaterial.

## Current Behavior
Projection cycles issue ~18 sequential SQL queries per venue; nearby requests
issue 7-14 sequential Redis GETs per venue; data-quality metrics re-query
per venue after every refresh; admin inventory/breakdown and engagement
writes run synchronously on the event loop, stalling `/health` and public
serving while they execute; catalog-wide DAO list/count helpers block Redis
with `KEYS` and then GET each key individually.

## Desired Behavior
Identical externally observable outputs with bounded round-trips:

- The projector must read its inputs in ~12 bulk queries per cycle (serving
  view listing; one bulk read per enrichment table including photos; one bulk
  weekly-forecast read; one bulk live read; venue rows in one query) and
  write the same projection it writes today. Optionally batch Redis writes
  through a pipeline. The projected Redis content — key names, values,
  TTL behavior (including the photos remaining-TTL rule), removals, and the
  summary dict — must be equivalent to today's output for the same RDS state.
- `/v1/venues/nearby` must serve a byte-identical response body for the same
  Redis state using MGET for the geo members and per-key-family pipelines /
  MGETs for live, weekly-day, vibe attributes, photos, opening hours,
  Instagram, vibe profile, and the 7-day hours-derivation fallback (~6
  round-trips per request instead of N×k).
- `update_data_quality_metrics` must compute `VENUES_WITH_LIVE_FORECAST` and
  `VENUES_WITH_WEEKLY_FORECAST` from two set-queries (venue-ids-with-live,
  venue-ids-with-weekly) intersected with the active set, producing the same
  gauge values.
- The admin inventory, venue-type-breakdown, and engagement handlers must be
  declared plain `def` so FastAPI runs them in the threadpool;
  `list_venue_inventory` must compute its cache flags from bulk per-family
  presence sets (one round-trip per key family per page) instead of ~16
  reads per venue. `/health` must stay responsive while an inventory listing
  runs.
- `GeoRedisClient.keys` must iterate with SCAN (same return contract);
  `list_all_venues` and `count_venues_with_instagram` must MGET the scanned
  keys. All seven list/count DAO helpers stop blocking Redis via `KEYS`.

## Implementation Approach
- `app/dao/rds_venue_store.py`: add bulk per-table readers — venue rows for
  an id set, all non-deleted enrichment rows per `table_key` for an id set
  (payload + updated_at + deleted_at, same columns as `get_enrichment`), all
  weekly rows, all live rows. Existing single-row readers stay (other call
  sites keep using them).
- `app/services/redis_projection_service.py`: restructure
  `rebuild_redis_from_rds` to prefetch the bulk maps once, then run the same
  per-venue projection logic (including `_project_photos`'s remaining-TTL
  math and the fail-safe/reconcile semantics, which must not change) from
  the in-memory maps. Per-venue error isolation (109-111) is preserved.
  Optional: wrap the Redis writes for each venue in a `pipeline()` — only if
  the equivalence tests stay green, since the serving DAO setters own key
  formats.
- `app/db/geo_redis_client.py`: `get_nearby_locations` uses MGET for member
  payloads; `keys()` switches to `scan_iter` internally.
- `app/handlers/venue_handler.py` + `app/dao/redis_venue_dao.py`: add bulk
  getters per key family (MGET on formatted keys, parse with the exact same
  model validation and per-item error tolerance as the single getters).
  `_merge`/`_transform`/`_derive_hours_from_forecast` consume prefetched
  maps; per-venue fallbacks, logging semantics, and freshness gating stay
  identical.
- `app/services/venues_refresher_service.py`: replace the 349-368 loop with
  two set-queries (ids with live forecast; ids with weekly day present) and
  set-intersection with active venue ids.
- `app/routers/admin_trigger_router.py` + `app/routers/engagement_router.py`:
  change the listed handlers from `async def` to `def` (no awaits inside —
  verified for engagement handlers and the two admin endpoints);
  `_venue_cache_flags` is replaced by bulk presence lookups for the page's
  venue ids.

## Data, Config, And API Impact
None — behavior-preserving. No request/response schema change, no Redis key
change, no config or migration impact. Redis projection content and
`/v1/venues/nearby` bodies must be byte-equivalent (see Acceptance Criteria).

## Error Handling And Observability
- Bulk readers must preserve the current per-venue error isolation: one bad
  venue/enrichment row skips that venue/enrichment with the same warning
  logs and `summary["errors"]` accounting, never aborting the cycle
  (fail-safe semantics at `redis_projection_service.py:85-90,108-111` are
  unchanged).
- On the serving path, a failed MGET/pipeline degrades exactly like today's
  failed GET: the venue is served without that enrichment (debug log), never
  a 500.
- Existing metrics keep their names, labels, and values:
  `REDIS_PROJECTION_VENUES`, `SERVING_VIEW_VENUES`,
  `REDIS_PROJECTION_REMOVED_TOTAL`, `VENUES_WITH_LIVE_FORECAST`,
  `VENUES_WITH_WEEKLY_FORECAST`, and the serve-time freshness counters in
  `_transform`. No new metrics are required; the projector's existing
  duration metric (`BACKGROUND_JOB_DURATION_SECONDS{job_name="redis_projection"}`)
  is the before/after evidence.

## Test Plan
Feature file: `tests/bdd/persistence/projector-and-serving-bulk-reads.feature`

Scenarios:
- Projection equivalence — for a seeded RDS state (venues + all enrichment
  families + weekly + live, including a soft-deleted enrichment and an
  ineligible venue), a rebuild with bulk reads writes the same Redis keys and
  values, and the same removals, as the RDS state dictates today.
- Bounded projector queries — a rebuild over many venues issues a fixed
  number of RDS queries (not proportional to venue count), observed via the
  counting RDS fake.
- Nearby response equivalence — `/v1/venues/nearby` returns the same response
  body for the same seeded Redis state, including the BestTime hours
  fallback for a venue without Google hours and suppression of a stale live
  value.
- Bounded nearby round-trips — a nearby request issues a bounded number of
  Redis round-trips regardless of venue count, observed via the counting
  Redis fake.
- Health stays responsive during a blocking admin listing — while a slow
  inventory listing executes, `/health` must answer (threadpool handler, not
  event-loop-blocking).

Pytest unit tests:
- Bulk RDS readers: shape parity with the single-row readers (payload /
  deleted_at / updated_at), weekly `venue_id#day_int` key handling, empty-set
  behavior.
- Bulk Redis getters: MGET parse parity with single getters, per-item error
  tolerance (one corrupt JSON skips one item only).
- `update_data_quality_metrics`: same gauge values as the per-venue loop for
  mixed live/weekly presence; 2 set-queries observed.
- `GeoRedisClient.keys`: SCAN-based result parity with `KEYS` (fakeredis).
- Handler declaration check: the listed admin/engagement endpoints are plain
  functions (no coroutine), locking the threadpool behavior.

Manual or integration checks:
- Against the local docker-compose stack: run one projector cycle before and
  after; diff a dump of `venue*`/`live_forecast*`/`week_raw*` keys for
  byte-equality; compare `/v1/venues/nearby` bodies for byte-equality; check
  the projector-cycle duration drop in the job metrics.

## Acceptance Criteria
- For an identical RDS state, the Redis projection produced after the change
  is byte-equivalent to the one produced before (same keys, same serialized
  values, same TTL semantics for photos, same removals).
- For an identical Redis state, the `/v1/venues/nearby` response body is
  byte-equivalent before/after, for verbose and minified, including the
  hours-derivation fallback and live-freshness suppression paths.
- Projector RDS queries per cycle are O(1) in venue count (~12), verified by
  the counting fake; the per-venue GET loops on the nearby path are gone
  (bounded round-trips, ~6 per request).
- `update_data_quality_metrics` issues 2 data queries per invocation and
  reports unchanged gauge values.
- `/health` responds while an admin inventory listing is in flight (no
  event-loop stall); the converted handlers are plain `def`.
- No `KEYS` command is issued by `GeoRedisClient.keys` (SCAN only).
- Full pytest + BDD suites green.

## Rollback
Revert the PR. The projector re-asserts the serving projection from RDS every
`redis_projection_minutes` (default 2, `app/config.py:114`), so one projector
cycle after the revert restores the previous serving state end-to-end; no
Redis or RDS cleanup is needed. The handler `def`/`async def` flips and read
paths carry no persisted state.

## Open Questions
- None. (Sequencing note, not a question: this plan assumes
  `fix/admin-breakdown-and-addvenue-fold` merges first, since it repairs the
  `venue-type-breakdown` DAO reference that P4 then converts to `def`.)
