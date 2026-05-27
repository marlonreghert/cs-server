# Add Venue To BestTime Account Inventory By Brazilian Address

> Terminology note: "BestTime collection" in BestTime's own docs refers to a
> labelled subgroup of venues. In this plan, the relevant concept is the
> **BestTime account inventory** — the set of venues already registered to our
> private API key, which BestTime exposes through `GET /api/v1/venues` and
> which we can query for live and weekly forecasts at no monthly cost. The
> +500 unique-new-venue/month allowance applies only to growing this
> inventory, not to querying venues already in it.

## Branch
feature/add-venue-by-address

## Goal
Expose an authenticated cs-server endpoint that registers a specific venue in
our BestTime account inventory by `venue_name` + `venue_address`, persists it
in our Redis geo index, and protects a configurable monthly quota of unique
new venues so automated discovery never starves the manual-add path.

Additionally, sync the **full BestTime account inventory** into Redis at the
start of every monthly crawler run so the upstream pipeline can serve live
busyness for every venue already free to query, with no credit cost. This
captures the ~755 inventory venues that are not currently in Redis (today's
state: 1330 in BestTime account inventory, 575 in Redis geo index).

This change must work alongside a vibes_bot admin flow (planned in a separate
prompt at the bottom of this file) that lets operators search Google Places for
candidates in a target city, pick the correct one, and send the chosen address
to cs-server.

## Non-goals
- Do not redesign the existing discovery (`/venues/filter`) pipeline beyond
  adding the monthly cap coordination and ordering it after the inventory
  sync step.
- Do not change `venues_geo_v1` key formats or migrate existing venue records.
- Do not implement Google Places search in cs-server; the search-and-pick UI
  is vibes_bot's responsibility (see the vibes_bot prompt at the end).
- Do not introduce a new public, unauthenticated POST. The new endpoint lives
  under `/admin/...`, mirroring the existing `/admin/trigger/*` and
  `/admin/recount-discovery-points` surface.
- Do not change BestTime billing semantics beyond tracking unique new venue
  additions to our account inventory. Live and weekly refreshes for venues
  already in the inventory remain free.
- Do not call the deprecated BestTime `/venues/search` background flow.
- Do not store BestTime's "collection" (subgroup label) field; we have no
  product need for that concept.

## Evidence
- BestTime client today supports `/venues/filter`, `/forecasts/live`, and
  `/forecasts/week/raw2`, but has no add-new helper and no inventory list
  helper: see `app/api/besttime_client.py:128`, `:155`, `:194`.
- BestTime "register a new venue" endpoint per
  https://documentation.besttime.app/: `POST
  https://besttime.app/api/v1/forecasts?api_key_private=...&venue_name=...&venue_address=...`.
  Documented response shape includes `venue_info.venue_id`, `venue_name`,
  `venue_address`, `venue_lat`, `venue_lon`, plus a 7-day `analysis` array.
  Synchronous when forecast data exists.
- BestTime "list account inventory" endpoint per
  `/home/mario/projects/exp/besttime.py`: `GET
  https://besttime.app/api/v1/venues?api_key_private=...&limit=...&page=...`.
  Returns a JSON list with `venue_id`, `venue_name`, `venue_address`,
  `venue_lat`, `venue_lng`, `venue_forecasted`, and `forecast_updated_on`.
  Calling this endpoint does not consume credits — it merely enumerates
  venues already registered to the API key.
- **Schema-naming inconsistency observed:** `/api/v1/venues` returns
  `venue_lng` (the inventory shape), while the docs for `POST /forecasts`
  return `venue_lon` inside `venue_info`. Our client must accept both
  spellings via a Pydantic alias and normalise internally to `venue_lng` to
  match the rest of the repo (`Venue.venue_lng` in `app/models/venue.py`).
- **Live probe finding (Probe A, 2026-05-26):** posting an inventory
  venue's own stored address verbatim back to `POST /forecasts` returned
  HTTP 400 with `{"status": "Error", "venue_info": null}` and consumed no
  credit slot in the rate-limit window (`x-ratelimit-remaining: 299`).
  Probe used `venue_name="Casas Bahia"`,
  `venue_address="Av. Gov. Agamenon Magalhães 153 - Santo Amaro Recife - PE
  50110-000 Brazil"`. Conclusion: BestTime's geocoder is one-way — the
  address it emits in `/api/v1/venues` is the **normalised** form and is not
  necessarily a valid **input** to `/forecasts`. We cannot rely on
  round-tripping inventory addresses. The canonical input format must come
  from a real geocoding source (Google Places `formatted_address`), and we
  need a lat/lng fallback for cases where BestTime's geocoder still fails.
- **Probe B (deliberate fake address, follow-up to Probe A) was declined
  by the operator;** no data was collected for the not-found shape. The
  contract for non-OK BestTime responses is treated as "any HTTP non-2xx OR
  any body with `status != "OK"` OR missing `venue_id`" until probed.
- `POST /forecasts` per the BestTime docs does **not** accept `lat`/`lng`
  inputs — only `venue_name`+`venue_address` or `venue_id`. So the lat/lng
  fallback must go through `/venues/filter` with a tight radius around the
  target coordinate, then match on venue name in the response.
- The BestTime account inventory today (probed via the exp script): 1330
  venues total, 575 forecasted, 755 not. Live busyness queries for any of the
  1330 are free; only adding new venues to the inventory counts against the
  +500/month allowance.
- Existing refresh service caps and budgets:
  - `Settings.fetch_venue_total_limit` is a per-run cap and is wired through
    `VenuesRefresherService.__init__`
    (`app/services/venues_refresher_service.py:182`).
  - `_refresh_with_discovery_points` already respects per-point `current/limit`
    counters and a `remaining_budget`
    (`app/services/venues_refresher_service.py:637`).
  - Discovery upserts via `RedisVenueDAO.upsert_venue`
    (`app/dao/redis_venue_dao.py:45`); we can detect "venue_id never seen
    before in the Redis geo index" by checking key existence before each
    upsert.
- Admin config is already centralised in Redis with `admin_config:` prefix;
  cs-server already reads `admin_config:discovery_points`
  (`app/services/venues_refresher_service.py:580`) and vibes_bot already
  manages these keys through `app/admin/config_dao.py` and
  `app/admin/static/admin.html`.
- Admin trigger / control endpoints live under `/admin` in
  `app/routers/admin_trigger_router.py`, with the container injected via
  `set_container`. Adding new admin routes follows that pattern.
- The existing `venue_catalog` admin job entry-point
  (`app/routers/admin_trigger_router.py:93`) calls
  `refresh_venues_by_filter_for_default_locations`, which is the monthly
  crawler hook the new inventory sync must run inside.
- vibes_bot already proxies cs-server admin endpoints (see
  `vibes_bot/app/admin/routes.py:567` `list_enrichment_jobs` and `:606`
  `recount_discovery_points`). Adding a new proxy route for venue-add follows
  the same shape.
- vibes_bot already uses `googlemaps.Client` for pricing
  (`vibes_bot/app/services/pricing/pricing_service.py:23`), so it can reuse
  the same `GOOGLE_MAPS_API_KEY` for text search candidates.
- vibes_bot's admin defaults list discovery points grouped by city prefix:
  Recife, Brasilia, SP, Salvador, JP (`vibes_bot/app/admin/routes.py:110`).
  Those city groupings are the natural source for the "select city" dropdown
  in the new flow.

## Current Behavior
- Operators can only seed new venues into the BestTime account inventory
  through the scheduled discovery refresh, which fans out from hardcoded or
  admin-configured lat/lng/radius points and uses `/venues/filter`.
- The discovery refresh respects per-point `current/limit` counters and a
  per-run `fetch_venue_total_limit`, but neither is calendar-month-aware nor
  reserves headroom for manual additions.
- BestTime's monthly +500 unique-new-venue allowance is enforced only by
  BestTime; cs-server has no internal counter.
- cs-server never reads `GET /api/v1/venues`, so ~755 venues that are already
  in our BestTime account inventory (and free to query for live busyness) are
  invisible to our pipeline because they have never come back through
  `/venues/filter` (e.g., venues without forecast data yet).
- There is no way to add a specific venue (e.g., a club without a strong
  signal that pulled it in via `/venues/filter`) without standing up new
  discovery points and rerunning the whole pipeline.

## Desired Behavior

### Inventory sync (monthly crawler, step 1)
- At the start of every `refresh_venues_by_filter_for_default_locations`
  invocation (i.e., the monthly venue catalog refresh and the manual
  `venue_catalog` admin trigger), call BestTime
  `GET /api/v1/venues` (paginated) to enumerate every venue currently in our
  BestTime account inventory.
- For each inventory entry not already present in the Redis geo index
  (`venues_geo_place_v1:<venue_id>`), upsert it via
  `RedisVenueDAO.upsert_venue` using `venue_id`, `venue_name`,
  `venue_address`, `venue_lat`, `venue_lng`. Forecast fields can be empty;
  later live/weekly refresh cycles will populate them.
- Inventory-sync upserts must not increment the monthly new-venue counter —
  these venues are already in the BestTime account inventory and do not
  consume the +500/month allowance.
- Inventory sync failures must log clearly and must not abort the rest of the
  monthly crawler. Discovery still runs, still respects the monthly counter,
  and still drives `/venues/filter` against the cached (possibly stale)
  Redis state.
- Emit metrics for inventory venues seen, inventory venues upserted as new
  (i.e., not previously in Redis), inventory venues skipped (already present),
  and inventory sync errors.

### Add-by-address endpoint
- Request body is a structured object, not a bare string, so vibes_bot can
  pass everything cs-server needs to make the BestTime call succeed and to
  fall back when it doesn't. Pydantic shape
  `AddVenueByAddressRequest`:

  ```
  {
    "venue_name": string,                  # required, 1..256
    "venue_address": string,               # required, 1..1024 — Google Places `formatted_address`
    "venue_lat": float,                    # required, -90..90  — Google Places geometry.location.lat
    "venue_lng": float,                    # required, -180..180 — Google Places geometry.location.lng
    "place_id": string | null,             # optional, for traceability/logging only
    "fallback_radius_meters": int | null   # optional, default 200, capped at 500
  }
  ```

  `venue_lat`/`venue_lng` are **required** because the live probe (Probe A)
  proved BestTime's geocoder cannot always parse an address verbatim, so we
  need the coordinates for the fallback path. vibes_bot already has them
  from the Google Places candidate the operator picked.

- New endpoint `POST /admin/venues/by-address` validates input and resolves
  the request:
  1. If a venue matching the submitted name+address is already present in
     cs-server's Redis (looked up via a deterministic name+address hash
     cache; on miss, fall back to a tight geo lookup around
     `(venue_lat, venue_lng)` within `fallback_radius_meters` and match
     candidates by case-folded name), return 200 with
     `{status: "already_exists", venue_id}` and do not call BestTime. After
     the inventory sync runs, this short-circuit covers any address already
     in the BestTime account inventory.
  2. Otherwise consult the monthly counter. If
     `counter >= monthly_new_venue_quota`, return 429 with a clear message.
  3. Atomically reserve one slot (Redis `INCR` of the monthly counter,
     released on failure), call BestTime
     `POST /forecasts?venue_name=...&venue_address=...&api_key_private=...`
     **with `venue_address` set to the Google Places `formatted_address`
     received from vibes_bot** (not whatever cs-server has in inventory; the
     inventory address is normalised output, not valid input). On success:
     - Upsert the returned venue (id, name, address, lat, lng) into the geo
       index via `RedisVenueDAO.upsert_venue`. Accept `venue_lon` from the
       `/forecasts` body via Pydantic alias; persist as `venue_lng`.
     - Cache the returned weekly analysis days via
       `RedisVenueDAO.set_week_raw_forecast` for each day in `analysis`.
     - Fetch live forecast inline via the existing
       `BestTimeAPIClient.get_live_forecast(venue_id=...)` and persist when
       BestTime returns one; tolerate empty/closed responses gracefully.
     - Promote the reservation into the permanent monthly counter
       increment.
     - Save the deterministic name+address → venue_id mapping so the next
       identical submission short-circuits at step 1.
     - Return 201 with `{status: "created", venue_id, venue_name,
       venue_address, venue_lat, venue_lng, source: "besttime_new"}`.
  4. **Fallback when BestTime's `/forecasts` rejects the address** (HTTP
     4xx, or 200 with `status != "OK"`, or 200/OK with missing `venue_id`):
     release the reservation and try one rescue path before failing:
     - Call `BestTimeAPIClient.venue_filter(...)` with
       `lat=venue_lat, lng=venue_lng, radius=fallback_radius_meters,
        foot_traffic="both"`, limited result count (e.g., 25).
     - If any returned venue's case-folded name contains (or is contained
       in) the submitted `venue_name`, treat that as a hit:
       - Upsert it via the existing refresher dedupe path (counts toward
         the monthly counter only if it's new to Redis — same rule as
         discovery).
       - Return 200 with `{status: "matched_via_geo_fallback",
         venue_id, source: "venues_filter_radius"}`.
     - If the fallback finds no name match, do not upsert anything, return
       502 with `{detail: "BestTime rejected the address and the geo
       fallback found no matching venue near (lat,lng) within
       <radius>m", besttime_status, candidates_seen}`.
  5. On non-recoverable BestTime errors (network, 5xx, etc.): release the
     reservation, do not upsert, and return 502 with a clear message.

### Discovery cap coordination
- Discovery refresh (`/venues/filter` path) must additionally respect the
  monthly counter and reserve. The effective discovery budget per run is
  `max(0, (monthly_new_venue_quota - manual_add_reserve) - month_counter)`.
- The counter increments only when a returned venue_id was **not already
  present in our Redis geo index** before the upsert. Because step 1
  (inventory sync) populates Redis with the full BestTime account inventory
  before discovery runs, "not in Redis before this batch" is a reliable proxy
  for "new to the BestTime account inventory."
- Both quota values (`monthly_new_venue_quota`, default 500; and
  `manual_add_reserve`, default 10) live in a new admin config key
  `admin_config:venue_monthly_budget`, mirroring the existing
  `admin_config:discovery_points` pattern so vibes_bot can edit them from the
  admin panel.
- The counter is per calendar month in UTC. The counter key suffix changes on
  rollover, so the previous month's counter is preserved and the new month
  starts at zero implicitly.

## Implementation Approach

### 1. BestTime client extensions
Add two methods to `app/api/besttime_client.py`:

- `add_venue_to_account(venue_name: str, venue_address: str) -> NewVenueResponse`
  - `POST /forecasts` with `api_key_private`, `venue_name`, `venue_address`
    as query parameters (matches BestTime doc'd shape and the existing
    live/weekly clients in this repo, which already pass keys via query
    string).
  - Reuse the existing httpx client and metrics
    (`BESTTIME_API_CALLS_TOTAL`, `BESTTIME_API_CALL_DURATION_SECONDS`,
    `BESTTIME_API_ERRORS_TOTAL`) with endpoint label `/forecasts`.
  - Parse into a Pydantic `NewVenueResponse` exposing `status`, `venue_info`
    (id, name, address, lat, lng — accept BestTime's `venue_lon` via
    Pydantic alias and expose as `venue_lng` for parity with the inventory
    list and with `app/models/venue.py`), `venue_timezone`, `rating`,
    `reviews`, `price_level`, and a list of `week_raw_days` when the
    response includes the 7-day analysis. The model should reuse
    `WeekRawDay` where possible.
  - Treat any of the following as a recoverable failure (caller may try the
    geo fallback): HTTP 4xx, body `status != "OK"`, or body OK without
    `venue_info.venue_id`. Surface the BestTime `message` field when
    present so the response 502 carries a meaningful explanation.
  - Treat HTTP 5xx and transport errors as non-recoverable; the caller
    must release the reservation and return 502 without attempting the geo
    fallback (BestTime is unhealthy, not the address).

- `list_account_inventory(page_size: int = 1000) -> Iterator[AccountInventoryVenue]`
  - Paginate `GET /api/v1/venues?api_key_private=...&limit=<page_size>&page=<n>`
    until a page returns fewer than `page_size` results (matches the exp
    script's loop pattern).
  - Use the same metrics convention (endpoint label `/venues`).
  - Parse into a small `AccountInventoryVenue` Pydantic model with the
    fields needed for upsert: `venue_id`, `venue_name`, `venue_address`,
    `venue_lat`, `venue_lng`, `venue_forecasted`, `forecast_updated_on`.

### 2. Monthly venue budget DAO
Add `app/dao/venue_budget_dao.py` (small focused DAO; no need to bloat
`RedisVenueDAO`):

- Key format: `venue_add_counter_v1:YYYY-MM` (integer stored as string).
- Methods:
  - `get_month_count(year_month: str) -> int`
  - `increment_month(year_month: str, n: int = 1) -> int` — returns post-INCR
    value. Use atomic `INCR`/`INCRBY`.
  - `decrement_month(year_month: str, n: int = 1) -> int` — used for
    reservation release on BestTime failure. Use `DECRBY`, clamped at zero.
  - `current_year_month_utc() -> str` helper.
- The DAO does not own quota policy; that lives in the service.

### 3. Quota config + service
Add `app/services/venue_budget_service.py`:

- Reads `admin_config:venue_monthly_budget` from Redis on each call (no
  in-memory caching in V1; admin changes take effect immediately).
- Default shape (when key missing): `{"monthly_quota": 500, "manual_reserve": 10}`.
- Exposes:
  - `get_quota_settings() -> QuotaSettings`
  - `discovery_effective_cap_remaining() -> int` —
    `max(0, (quota - reserve) - month_counter)`.
  - `can_manual_add() -> bool` — `month_counter < quota`.
  - `reserve_manual_slot()` / `release_manual_slot()` — wraps INCR/DECR with
    rollback semantics.
  - `record_new_venue(source: str)` — increments counter, labels metric for
    `source in {"discovery", "manual_add"}`.

### 4. Inventory sync method on the refresher service
Add `VenuesRefresherService.sync_account_inventory_to_redis() -> InventorySyncResult`:

- Calls `besttime_api.list_account_inventory()` and iterates results.
- For each entry, checks Redis for `venues_geo_place_v1:<venue_id>` existence.
- If missing, constructs a `Venue` from the inventory fields (with empty
  forecast data) and calls `venue_dao.upsert_venue(...)`.
- Tracks counts: `seen`, `newly_upserted`, `skipped_already_present`, `errors`.
- Logs a final summary and emits metrics (`inventory_sync_venues_total{result}`).
- Never increments the monthly new-venue counter — these are existing
  inventory venues.
- Exceptions are caught at the per-venue level (a bad row should not abort
  the whole sync) and at the per-page level (a transient BestTime error
  should be logged and may abort sync, but must not abort the caller).

### 5. Monthly crawler ordering
- Change `refresh_venues_by_filter_for_default_locations` to call
  `sync_account_inventory_to_redis()` first, then run the existing discovery
  logic (`_refresh_with_discovery_points` or `_refresh_with_locations`).
- Inventory sync failures are logged but do not abort the discovery step.
- After the inventory sync completes, recompute the discovery
  `remaining_budget` using `VenueBudgetService.discovery_effective_cap_remaining()`
  combined (`min(...)`) with the existing `fetch_venue_total_limit`. The
  per-point `current/limit` counters remain in play for their original
  purpose (distributing budget across points).
- The admin `venue_catalog` trigger inherits the new ordering for free
  because it calls the same method.

### 6. Discovery counter integration
- In `refresh_venues_data_by_venues_filter`, before each
  `venue_dao.upsert_venue`, detect whether the venue_id already exists in
  the geo index using a single Redis `EXISTS` check on the venue key. If
  not, call `VenueBudgetService.record_new_venue(source="discovery")` after
  a successful upsert.
- Existing per-point `current` counters and per-run `remaining_budget`
  arithmetic stay intact, but the source of the initial `remaining_budget`
  shifts to the monthly-budget service.
- Log when the discovery loop bails out because of the monthly cap,
  including `year_month`, current counter, quota, reserve, and remaining
  budget.

### 7. Add-by-address handler + router
- New handler `app/handlers/add_venue_handler.py`:
  - Validates the request via a Pydantic `AddVenueByAddressRequest` model
    with bounded `venue_name` (1..256) and `venue_address` (1..1024).
  - Looks up a deterministic name+address cache (Redis key
    `venue_lookup_by_address_v1:<sha1(lower(name)|lower(address))>` →
    venue_id) to short-circuit obvious duplicates.
  - Calls `VenueBudgetService.can_manual_add()`. If false, returns
    `quota_exhausted` (HTTP 429).
  - Reserves a slot via `reserve_manual_slot()`, calls
    `BestTimeAPIClient.add_venue_to_account(...)`. On non-OK or missing
    `venue_id`: releases the slot, returns 502.
  - On success: writes the venue via `RedisVenueDAO.upsert_venue`, caches
    any weekly forecast days returned, calls
    `BestTimeAPIClient.get_live_forecast(venue_id=...)` and stores it via
    `RedisVenueDAO.set_live_forecast` when status is OK and
    `venue_live_busyness_available` is true (otherwise skips silently).
  - Saves the `venue_lookup_by_address_v1` cache mapping for future
    requests.
  - Returns 201 with `{status: "created", venue_id, venue_name,
    venue_address, venue_lat, venue_lng}`.

- Extend `app/routers/admin_trigger_router.py` (keeps the admin surface in
  one place; no new module):
  - `POST /admin/venues/by-address` → handler call.
  - `GET /admin/venues/monthly-budget` → returns
    `{quota, manual_reserve, month_counter, year_month,
      discovery_effective_cap_remaining, manual_add_available}`. vibes_bot
    will read this for the admin UI badge.
  - Optional: expose `POST /admin/trigger/inventory_sync` (or add
    `inventory_sync` to `JOB_REGISTRY`) so operators can re-run the
    inventory sync on demand without waiting for the next monthly crawler.

### 8. Wiring
- `app/container.py`: instantiate `VenueBudgetDao` and `VenueBudgetService`
  and pass them into `VenuesRefresherService` and the new add-venue handler.
- `main.py`: no new router file is needed because we extend the existing
  `admin_trigger_router`. Container setter is already invoked.
- No new env vars or settings: the quota and reserve are admin-config-only.

### 9. Idempotency and atomicity decisions
- Concurrent add-by-address requests are guarded by `INCR` on the month
  counter as the reservation primitive: if `INCR` returns a value > quota,
  immediately `DECR` and return 429. Same pattern as a token bucket; avoids
  check-then-set races.
- Concurrent discovery + manual add: discovery rechecks the remaining cap
  before each batch (`refresh_venues_data_by_venues_filter` is called once
  per point), and manual add uses the `INCR`-then-validate pattern. The
  reserve guarantees that if discovery already hit `quota - reserve`, manual
  add can still INCR up to `quota` because discovery stops itself short.
- The deterministic name+address cache avoids burning the reservation on
  obvious duplicates before any BestTime call.
- Inventory sync runs serially in the existing async refresh task; it does
  not need to coordinate with discovery beyond ordering (sync first, then
  discovery).

## Data, Config, And API Impact
- **API impact (cs-server):** New routes under `/admin/venues/...`. No
  existing routes change. Response payloads use the same naming convention
  as current admin endpoints. Optional new admin job
  `inventory_sync`.
- **Redis impact:** New keys with explicit V1 prefixes — no existing key
  format changes, no migrations:
  - `venue_add_counter_v1:YYYY-MM` (integer; never deleted; resets by month
    rollover producing a fresh key).
  - `venue_lookup_by_address_v1:<sha1>` (string venue_id; long-lived, no
    TTL in V1).
  - `admin_config:venue_monthly_budget` (JSON, follows existing
    `admin_config:*` pattern).
- **Config impact:** No new settings in `app/config.py`. Quota and reserve
  are admin-config-only. If the admin key is missing, code falls back to
  `{monthly_quota: 500, manual_reserve: 10}`.
- **Deployment impact:** Python code change only. No Redis schema migration,
  no volume removal, no `[FULL-RESTART]` commit message. vibes_bot ships
  independently (see prompt below).

## Error Handling And Observability
- Inventory sync errors at the BestTime call level: log at `error` with the
  failing page number; do not abort the monthly crawler. Emit
  `inventory_sync_errors_total`.
- Inventory sync per-venue parse/upsert failures: log at `warning` with the
  failing venue_id (no other PII), continue with the next venue. Emit
  `inventory_sync_venues_total{result="error"}`.
- BestTime add-new failure: log at `error` with sanitised request context
  (`venue_name`, masked address — never log keys or full street address).
  Release reservation. Return 502.
- BestTime returns `status="OK"` but missing `venue_id`: treat as failure,
  release reservation, log, return 502.
- Live forecast fetch failure after successful add: log at `warning` with
  venue_id, keep the venue persisted, do not fail the response.
- Redis failures on the budget DAO: log, fail-closed for `can_manual_add` so
  we don't accidentally over-spend BestTime credits.
- New Prometheus metrics in `app/metrics.py`:
  - Counter `add_venue_by_address_total{result}` with
    `result in {"created","already_exists","quota_exhausted","besttime_error",
    "validation_error"}`.
  - Counter `inventory_sync_venues_total{result}` with
    `result in {"seen","upserted","skipped","error"}`.
  - Counter `inventory_sync_runs_total{outcome}` with
    `outcome in {"ok","partial","failed"}`.
  - Counter `discovery_skipped_due_to_monthly_cap_total`.
  - Gauge `venue_monthly_new_count` (current month counter).
- Logs that bail discovery must include `year_month`, current counter,
  quota, reserve, and remaining budget so operators understand why no
  venues were fetched without grepping Redis.

## Pre-Implementation Verification (locked into `/execute-feature`)
Before writing production code, `/execute-feature` must run a short probe
script at `scripts/probe_besttime_forecasts.py` (added as part of this
feature) and check in the captured JSON to `tests/fixtures/besttime/`
**redacted of API keys**. The script covers four cases and records the
full response body, status code, and credit-related headers for each:

1. Probe A — **known inventory venue (idempotent re-add)**:
   `POST /forecasts` for a venue already in our BestTime account inventory,
   using the Google-Places-style formatted address (not the inventory's
   normalised form). Operator picks a low-stakes venue (e.g., a chain store
   such as Casas Bahia) and supplies the formatted address from Google
   Places Text Search. Expected outcome: `status="OK"` with a populated
   `venue_info.venue_id` equal to the existing inventory id. If credits
   are spent, the response or rate-limit headers should make it
   observable.

2. Probe B — **deliberately fake address**:
   `POST /forecasts` for an address that BestTime cannot geocode.
   Expected outcome: HTTP 400 or 200 with `status="Error"`. Record the
   exact failure shape so the client parses `message`, `status`, and
   absence of `venue_info.venue_id` correctly.

3. Probe C — **`/venues/filter` radius hit**:
   `GET /venues/filter` with `lat`, `lng`, and `radius=200` (m) around the
   Probe A venue's coordinate. Expected outcome: the response includes
   the inventory venue. Confirms the geo fallback path can match an
   existing inventory venue from coordinates alone.

4. Probe D — **valid venue NOT in inventory (first-time create)**.
   This is the expensive probe — it spends 1 of our +500 monthly venue
   slots and the associated BestTime credits. It must be run **exactly
   once, supervised, never in CI**, because Probes A and B alone never
   show the response shape for a fresh BestTime venue creation (where
   BestTime is computing the analysis array for the first time and may
   return partial or pending data). Without it, we'd be locking in the
   client model from idempotent-re-add evidence only and would discover
   the fresh-create differences in production.

   Constraints for Probe D:
   - Operator manually picks a real venue we **actually want** to add to
     the catalog so the spent quota is not wasted (e.g., a club a user
     has requested but that has not surfaced through `/venues/filter`).
   - The operator confirms in person before the script issues the call;
     the script must require an interactive confirmation prompt, not just
     run on import.
   - The script must verify the chosen `(venue_name + venue_address)` is
     NOT in the current inventory (call `/api/v1/venues` first and check).
     If found, abort without spending the slot.
   - On success, save the redacted response to
     `tests/fixtures/besttime/forecasts_post_fresh_create_ok.json` and
     immediately re-call Probe A on the same venue to capture the
     follow-up "now in inventory" idempotent shape into
     `tests/fixtures/besttime/forecasts_post_fresh_then_reread_ok.json`
     — that comparison locks down whether BestTime returns the same
     payload on first-create vs subsequent re-add.
   - On failure (BestTime error despite the address being valid),
     surface the body to the operator and stop — do not retry with
     variations.

   Probe D **MUST NOT** be re-run by automation. The fixtures it
   produces are the canonical evidence for the fresh-create branch
   forever; later changes to the BestTime contract are caught at
   production time via the existing client metrics, not by re-probing.

The captured fixtures become the basis for `respx`/`httpx` mocks in the
unit tests below. **Do not write the BestTime client model classes from
docs alone — model field names, optionality, and the recoverable-vs-fatal
error mapping must be derived from the captured fixtures.**

Probe rules:
- Run probes against the production BestTime account; the user has already
  authorised spending the credits required for Probes A, B, and C, and
  the single fresh-venue slot required for Probe D.
- Save fixtures under
  `tests/fixtures/besttime/forecasts_post_known_ok.json`,
  `forecasts_post_unknown_error.json`,
  `venues_filter_radius_200m.json`,
  `forecasts_post_fresh_create_ok.json`,
  `forecasts_post_fresh_then_reread_ok.json`.
- Redact `api_key_private` from any captured request/response artifact
  before commit.
- If Probe A or Probe D returns an unexpected shape (e.g., Probe A
  charged credits for an idempotent re-add, or Probe D returned a shape
  that differs structurally from Probe A's re-read), `/execute-feature`
  must stop and surface the finding to the user before continuing.

## Test Plan
Feature file: `tests/bdd/api/add_venue_by_address.feature`

Scenarios (already drafted in the Gherkin file):
- Add a new venue by address when budget is available.
- Return existing venue without spending the monthly budget when the address
  is already in the account inventory.
- Reject manual add when the monthly quota is exhausted.
- Allow manual add to consume the reserved budget when discovery has filled
  the discovery cap.
- Validate the submitted address payload.
- Surface BestTime errors clearly without spending the monthly counter.
- Discovery refresh must stop short of the manual add reserve.
- Increment monthly counter only for venues new to the BestTime account
  inventory.
- Reload monthly quota and reserve from admin config on each request.
- Reset the monthly counter when the calendar month rolls over.
- Sync the full BestTime account inventory into Redis at the start of the
  monthly crawler.
- Inventory sync persists venues even when BestTime has no forecast for
  them yet.
- Inventory sync failure must not abort the monthly crawler.

Pytest unit tests:
- `tests/test_venue_budget_dao.py`: month key formatting, INCR/DECR
  roundtrips, decrement clamps at zero, year_month rollover produces a
  separate counter.
- `tests/test_venue_budget_service.py`: quota loading from admin config,
  default fallback, discovery effective cap math (including
  `quota - reserve - counter` clamping at zero), reservation roundtrip,
  fail-closed behaviour on Redis errors.
- `tests/test_besttime_client.py` (extend):
  - `add_venue_to_account` builds the correct query string, parses both
    the **fresh-create success fixture (Probe D)** and the
    **idempotent-re-add fixture (Probe A)** — both must round-trip into
    the same Pydantic model with identical field semantics, including
    the `venue_lon`→`venue_lng` alias. If Probe D's analysis array is
    partial or missing, the model must tolerate that without raising.
  - Parses the captured error fixture (Probe B) into a recoverable error
    indicator (no exception).
  - Raises on transport errors / HTTP 5xx.
  - `list_account_inventory` paginates correctly and stops on a short
    final page.
  - Use respx with the JSON fixtures captured in the Pre-Implementation
    Verification step. Do not hand-write expected JSON.

- `tests/test_add_venue_handler.py` (extend success-path coverage):
  - The "fresh BestTime create" branch must be mocked with Probe D's
    fixture, not Probe A's. This catches regressions where the handler
    assumes a fully-populated analysis array that fresh creates may not
    have.
  - The "already in inventory, re-submitted" branch is mocked with Probe
    A's fixture to assert idempotent behaviour and zero monthly-counter
    impact.
- `tests/test_add_venue_handler.py`:
  - Validates input including rejecting requests with missing lat/lng.
  - Short-circuits on the address-hash cache hit (no BestTime call).
  - Short-circuits on the geo-lookup cache hit (matching inventory venue
    within `fallback_radius_meters`, name-fold compared).
  - Calls BestTime exactly once on the success path, releases reservation
    on failure, caches the address mapping on success.
  - On `/forecasts` recoverable failure (Probe B fixture), the handler
    invokes the `/venues/filter` geo fallback exactly once and treats a
    name-matched result as `matched_via_geo_fallback` (HTTP 200).
  - On `/forecasts` recoverable failure with no geo-fallback match,
    returns 502 with the BestTime `message` echoed in `detail`.
  - On `/forecasts` non-recoverable failure (5xx / transport), does **not**
    invoke the geo fallback and returns 502.
  - Returns expected status codes for every branch above.
- `tests/test_venues_refresher_service.py` (extend):
  `sync_account_inventory_to_redis` upserts only venues missing from Redis
  and never increments the monthly counter; the monthly crawler runs the
  inventory sync before discovery; inventory sync failures do not abort
  discovery; discovery cap reflects monthly counter; counter increments
  only for venue_ids not previously in Redis; discovery stops short of the
  reserve; log/metric emitted when the cap is hit mid-run.

Manual or integration checks:
- `make test-feature FEATURE=tests/bdd/api/add_venue_by_address.feature`
  once steps are written.
- `make test-bdd`.
- `make test-unit`.
- Smoke test against a disposable BestTime account or recorded fixtures
  only; do not consume the production +500 monthly allowance during
  automated tests.
- After deploy, manually trigger `inventory_sync` (or the next
  `venue_catalog` job) and verify the Redis venue count rises toward the
  account inventory total (~1330) without the monthly counter changing.
- Verify the new `/admin/venues/monthly-budget` endpoint returns expected
  values after manually setting `admin_config:venue_monthly_budget`.

## Acceptance Criteria
- The monthly crawler's first action is to sync the full BestTime account
  inventory into Redis; venues already in Redis are skipped; missing
  venues are upserted with id, name, address, lat, lng.
- Inventory-sync upserts do not increment the monthly new-venue counter.
- Inventory sync failures are logged but do not abort the discovery refresh.
- `POST /admin/venues/by-address` returns 201 with the persisted venue
  payload for a fresh address when monthly budget is available.
- A second call with the same name+address returns 200 `already_exists`
  without calling BestTime and without touching the monthly counter; after
  the inventory sync runs, addresses already in the BestTime account
  inventory also short-circuit.
- The monthly counter increments only when a venue_id is genuinely new to
  cs-server's Redis state (post-inventory-sync), regardless of whether it
  came in through the add-by-address path or discovery.
- Discovery never causes the monthly counter to exceed
  `quota - manual_reserve`, even when admin shrinks the reserve mid-run
  (the next batch picks up the new value).
- Manual add can use the reserved slots when discovery has hit its
  effective cap, but never exceeds the total quota.
- Admin updates to `admin_config:venue_monthly_budget` are observed on the
  next request without restarting cs-server.
- The implementation does not delete, migrate, or rewrite any existing
  Redis venue data and uses only new V1-prefixed keys for new state.
- Logs and Prometheus metrics make quota-related rejections, discovery
  bail-outs, and inventory sync outcomes visible without grepping Redis.
- Deployment does not require Redis restart, Redis volume removal, or any
  `[FULL-RESTART]` commit-message trigger.

## Open Questions
None — the credit model (per unique new venue added to the BestTime account
inventory, +500/month) and inventory-sync expectations were confirmed by the
user. Idempotency, atomicity, ordering, and admin-config wiring are decided
above.

---

## Companion Prompt For vibes_bot Engineering Agent

Send the block below to the vibes_bot engineering agent verbatim. It assumes
the cs-server side described above ships first (or in parallel) but does not
require it to be live to start work; the proxy can be wired against the
agreed contract.

```
# vibes_bot: Admin flow for adding a venue to the BestTime account inventory by address

## Context
cs-server is gaining a new admin endpoint that registers a specific venue in
our BestTime account inventory by name + address, persists it in Redis, and
respects a per-month budget of +500 unique new venues (10 reserved for manual
adds so discovery never starves the manual path). cs-server is also gaining a
monthly inventory-sync step that pulls every venue already in our BestTime
account into Redis at no credit cost, so vibes_bot's pipeline can serve live
busyness for the full account inventory (not just the venues that have come
back through /venues/filter discovery).

vibes_bot needs an admin UI to drive the manual add flow end-to-end: search
Google Places for a candidate in a chosen city, let the operator pick the
exact match, and POST the chosen address to cs-server.

## Terminology
"BestTime account inventory" = the venues registered to our BestTime API key
(listed by GET /api/v1/venues). Live busyness queries for these venues are
free. The +500/month budget applies only to growing the inventory.

Do not use the word "collection" — in BestTime's docs that means a labelled
subgroup, which we do not use.

## Scope
1. Add an "Add Venue" tab/section in the existing admin portal
   (`app/admin/static/admin.html`) next to "Discovery Points" / "Enrichment
   Services".
2. Add backend routes in `app/admin/routes.py` and reuse the existing
   `AdminConfigDao` for any persistence needs.
3. Proxy the new cs-server admin endpoints for the add operation and budget
   readout.
4. Surface the current monthly budget so operators see the remaining manual
   adds before they search.
5. Match the existing admin auth pattern (`Depends(verify_session)`).

## Backend changes

### Google Places candidate search
Add `GET /api/admin/venues/places-search` (admin-gated):
- Query params: `q` (text query, required), `city_slug` (required, e.g.
  "recife", "sao-paulo", "brasilia", "salvador", "joao-pessoa").
- Resolve `city_slug` to a `(lat, lng, radius_meters)` triple. Source of
  truth for the dropdown should be derived from the existing
  `admin_config:discovery_points` Redis key (group by the city prefix in
  `id` — same grouping the Discovery Points tab already does in
  `loadDiscoveryPoints()`), so adding a new city in Discovery Points
  automatically exposes it here.
- Call Google Maps Text Search via the already-imported `googlemaps` SDK
  with `location=(lat, lng)` and `radius=radius_meters`, asking for
  `place_id`, `name`, `formatted_address`, `geometry.location`, `rating`,
  `user_ratings_total`, and `types`.
- Return up to 10 candidates as
  `[{place_id, name, formatted_address, lat, lng, rating, total_ratings,
     types}]`. Cache results in Redis under
  `admin_places_search_cache:<sha1(q|city_slug)>` for ~24h to avoid burning
  Google Places quota on retries.

### Monthly budget readout (proxy)
Add `GET /api/admin/venues/monthly-budget` that proxies to cs-server's
`GET /admin/venues/monthly-budget` (similar to how
`recount_discovery_points` proxies cs-server today in
`app/admin/routes.py`). Pass through the JSON unchanged.

### Add venue (proxy)
Add `POST /api/admin/venues/add` that:
- Accepts body
  `{venue_name, venue_address, venue_lat, venue_lng, place_id?,
    fallback_radius_meters?}`.
  All of `venue_name`, `venue_address`, `venue_lat`, `venue_lng` are
  required. `venue_address` MUST be Google Places' `formatted_address`
  for the candidate the operator selected — not a free-text rewrite, not
  the user's typed search query, and not any address you got back from
  cs-server's inventory list. BestTime's geocoder rejects its own
  normalised output when fed back to `/forecasts`, so sending anything
  other than a real geocoded address risks a 400 from BestTime.
  `venue_lat`/`venue_lng` MUST be the Google Places candidate's
  `geometry.location.lat`/`lng`; cs-server uses them for its lat/lng
  fallback when BestTime's geocoder still fails.
- Calls cs-server `POST /admin/venues/by-address` forwarding the same
  body fields.
- Returns whatever cs-server returns (201 / 200 already_exists / 200
  matched_via_geo_fallback / 429 / 502 / 422), passing through status
  codes and bodies.
- Logs the operator session and `place_id` (if provided) at info level.
- The frontend must therefore send the selected Place's `place_id`,
  `formatted_address`, and `geometry.location.lat`/`lng` verbatim — do
  not edit any of those before submitting.

### Inventory-sync trigger (proxy, optional but recommended)
Add `POST /api/admin/venues/inventory-sync` that proxies cs-server's
`POST /admin/trigger/inventory_sync` so the admin UI can re-run the sync
on demand without waiting for the next monthly crawler.

### City list helper
Add `GET /api/admin/venues/available-cities` that reads
`admin_config:discovery_points`, groups by the part before the first hyphen
in each point's `id`, and returns
`[{slug, label, center_lat, center_lng, radius_meters}]` for each city. Use
the largest of the city's discovery point radii as the search bias radius
(or a sane minimum of 5000 m). This becomes the source for the city
dropdown.

## Frontend changes (single-file `admin.html`)

Add a new tab `"Add Venue"` to the existing tab bar in `admin.html` (around
the same place as `Discovery Points` and `Enrichment Services`). Layout:

1. Header strip with a live "Monthly budget" badge that calls
   `/api/admin/venues/monthly-budget` on tab open and after every successful
   add: show `month_counter / quota` and `manual_add_available` count
   prominently. Colour the badge red when `manual_add_available <= 0`.
   Include a small secondary "Run Inventory Sync" button that calls
   `/api/admin/venues/inventory-sync` and refreshes the badge on completion.
2. Two side-by-side controls:
   - City dropdown populated from `/api/admin/venues/available-cities`.
   - Free-text search box for the venue name.
3. "Search" button calls `/api/admin/venues/places-search`.
4. Results list renders each candidate with name, formatted_address, rating,
   total_ratings, types, and a primary "Add to inventory" button.
5. Clicking "Add to inventory":
   - Confirms in a small dialog showing the exact `venue_name` and
     `venue_address` that will be sent.
   - Calls `/api/admin/venues/add`.
   - On 201: success toast, refresh the badge, mark the candidate row as
     added.
   - On 200 already_exists: info toast with the existing `venue_id`.
   - On 429 quota_exhausted: error toast, disable the "Add" buttons.
   - On 502 / 5xx: error toast with the message from the cs-server response.

Follow the existing patterns in `admin.html` for fetch helpers (`api(...)`),
toast/status elements, and table layout. Keep the styling consistent with
the existing tabs (no new CSS framework). Read the Enrichment Services tab
as a structural example of polling + button states.

## Editable budget config

The admin config key `admin_config:venue_monthly_budget` (JSON, shape
`{"monthly_quota": 500, "manual_reserve": 10}`) is read by cs-server. Wire
it into the existing App Config tab the same way
`admin_config:discovery_points` is wired:
- Add `KEY_VENUE_MONTHLY_BUDGET = "venue_monthly_budget"` to
  `app/admin/config_dao.py` and include it in `ALL_KEYS`.
- Add a default `_DEFAULT_VENUE_MONTHLY_BUDGET = {"monthly_quota": 500,
  "manual_reserve": 10}` in `app/admin/routes.py` `_get_defaults`.
- Add a config section in `admin.html` (under Pipeline Config or a new
  "Budget" sub-section) with two number inputs and Save/Reset buttons that
  POST to `/api/config/venue_monthly_budget` exactly like other config keys.

## Tests
- Backend unit tests under `tests/` for the new admin routes:
  candidate search response shaping, proxy passthrough for the monthly
  budget endpoint, proxy passthrough for the add endpoint (mock cs-server
  with `httpx` mock), proxy passthrough for inventory sync, available-cities
  derivation from a stub `admin_config:discovery_points` payload.
- A BDD scenario under `tests/bdd/admin/` covering the happy path:
  operator searches → picks candidate → submits → sees a success toast and
  an updated badge.

## Out of scope
- Do not implement the cs-server-side endpoints here — vibes_bot only
  proxies.
- Do not bypass the existing admin session auth.
- Do not add a "delete venue" path; that already exists at the cs-server
  level if needed.
- Do not change vibes_bot's pipeline scoring or filters.

## References (cs-server, read-only)
- New endpoints: `POST /admin/venues/by-address`,
  `GET /admin/venues/monthly-budget`, optional
  `POST /admin/trigger/inventory_sync`.
- Request body (add):
  ```
  {
    "venue_name": string (1..256),
    "venue_address": string (1..1024),     # Google Places formatted_address
    "venue_lat": float (-90..90),          # Google Places geometry.location.lat
    "venue_lng": float (-180..180),        # Google Places geometry.location.lng
    "place_id": string | null,
    "fallback_radius_meters": int | null   # default 200, max 500
  }
  ```
- Response 201 created:
  `{status: "created", venue_id, venue_name, venue_address, venue_lat,
    venue_lng, source: "besttime_new"}`.
- Response 200 already_exists:
  `{status: "already_exists", venue_id, ...}`.
- Response 200 matched_via_geo_fallback:
  `{status: "matched_via_geo_fallback", venue_id, ...,
    source: "venues_filter_radius"}`.
- Response 429:
  `{detail: "Monthly venue quota exhausted", year_month, month_counter,
    quota}`.
- Response 502 (BestTime address rejected, no geo fallback hit):
  `{detail: "BestTime rejected the address and the geo fallback found no
    matching venue near (lat,lng) within <radius>m", besttime_status,
    besttime_message, candidates_seen}`.
- Response 502 (BestTime transport/5xx):
  `{detail: "BestTime is unavailable: <message>"}`.
- Response 422: standard FastAPI validation error (e.g., missing lat/lng).
- Budget readout: `{quota, manual_reserve, month_counter, year_month,
  discovery_effective_cap_remaining, manual_add_available}`.

## Observed BestTime quirks (from cs-server's live probe)
1. `POST /forecasts` may reject an address even when that address is
   already in our account inventory. The address BestTime stores and
   returns in `/api/v1/venues` is the geocoder's **output**, not a valid
   geocoder **input**. Always send the Google Places `formatted_address`,
   never recycle inventory-list addresses.
2. The lat/lng field name differs between endpoints:
   `/api/v1/venues` returns `venue_lng`, `/forecasts` returns
   `venue_lon`. cs-server normalises everything to `venue_lng`.
3. The response status field is the top-level `status`. A 200 HTTP code
   with `status="Error"` and `venue_info: null` is the documented failure
   shape for an unparseable address.
```
