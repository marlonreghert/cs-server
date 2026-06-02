# RDS As System Of Record With Redis Serving Projection

> **STATUS (2026-06-02) — data plane DONE; config plane + engagement activation NOT.**
>
> **Phase map (incremental, independent, non-conflicting):**
> 1. ✅ **Delivered** — data-plane system of record (THIS plan, live in prod).
> 2. ▶ **Next** — admin config → RDS: **`plans/admin_config_rds_02_06_26.md`**
>    (planned, ready to `/execute-feature`).
> 3. ⏭ **Later** — projection decoupling (pipelines stop writing Redis):
>    **`plans/redis_projection_decoupling_01_06_26.md`** (planned, `@wip`).
> 4. 🔀 **vibes_bot companions** — engagement flag flip + admin-panel-via-API
>    (vibes_bot lifecycle).
>
> Config (2) is a synchronous-mirror carve-out the decoupling projector (3) never
> touches, so 2 and 3 are independent and can land in either order; this plan (1)
> is the as-built record they build on.
>
> - **Phase 0 (provision RDS):** ✅ done — RDS up, Alembic baseline applied,
>   dual-store contract test green.
> - **Phase 1 (dual-write + cutover + backfill):** ✅ **DONE.** Deployed
>   `rds_enabled=true` (2026-06-01, container healthy, "RDS system-of-record
>   enabled"); `backfill_rds` ran clean — **1331 venues, 12299 enrichment,
>   0 errors** (vibe_attributes 612, weekly 9274, live 371). RDS is now the
>   populated system of record for venue/enrichment/live data. *(Not separately
>   done: the staging "flush Redis + rebuild" drill — rebuild is exercised by the
>   off-loop one-off / DBeaver smoke instead; no prod Redis flush.)*
> - **Phase 2 (config→RDS + vibes_bot admin via API):** ❌ **NOT started.** Admin
>   config (eligibility, discovery_points, budget, photos TTL + all vibes_bot
>   admin-panel config) still lives **only in Redis**; `admin.admin_config` table
>   is empty/unused; no config CRUD or venue-edit endpoints; budget counters still
>   Redis. The `@wip` admin-config scenario is superseded by the dedicated plan
>   **`plans/admin_config_rds_02_06_26.md`** (sync-mirror carve-out, NOT the
>   decoupling projector); the vibes_bot admin-panel-via-API is a separate companion.
> - **Engagement (favorites/hot_likes):** API is durable-capable now (flag on),
>   but **vibes_bot `ENGAGEMENT_WRITE_THROUGH` is still OFF** → vibes_bot still
>   writes Redis directly, so **nothing is persisted to `engagement.*` yet** (those
>   tables are empty; backfill does not cover engagement). Activation pending the
>   flag flip + the vibes_bot `http_requests_total` monitoring gap.
> - **Phase 3 (engineer DBeaver/SSM access):** ✅ available — connection + SSM
>   shell documented in `README.md`.
> - **Superseded design:** the **synchronous write-through projection** described
>   below (RDS-first → then project Redis in the same call) is **as-built and
>   live**, but is to be replaced by an asynchronous, RDS-fed **projector**
>   (pipelines write RDS only; pipelines read RDS; Redis fed only by the
>   projector). See **`plans/redis_projection_decoupling_01_06_26.md`** (unbuilt).
> - As-built engagement endpoints, key formats, and the contract test are
>   captured in `VIBES_BOT_HANDOFF.md` (the as-built source of truth).

## Branch
feature/rds-system-of-record

## Goal
Make an AWS RDS Postgres database the **system of record** for all venue
pipeline data, with Redis demoted to a **serving projection** that is rebuilt
from RDS. Every cs-server pipeline write persists to RDS first, then projects
the serving-relevant subset into the existing Redis keys, so public venue
serving (and vibes_bot's HTTP retrieval) is byte-for-byte unchanged. Admin
configuration moves into RDS and the vibes_bot admin panel becomes a thin
interface over the DB (via cs-server's API). Live busyness stays Redis-only.

Deliver: the cs-server runtime change, the DDL for five schemas
(`venues`, `besttime`, `google_places`, `instagram`, `admin`), the rejection-
reason model, a Redis-rebuild/backfill path, the vibes_bot changes, and an
infra runbook (Terraform via AWS SSO, SSM access, DBeaver) so the EC2 and both
engineers can reach RDS without a VPN.

## Non-goals
- Do not change the public venue-serving contract. `GET /v1/venues/nearby`
  keeps reading Redis; vibes_bot's `CrowdSenseClient` is untouched.
- **All data is persisted to RDS — including live busyness and vibes_bot's
  favorites/hot_likes.** Redis is purely the user-facing read interface; there is
  no Redis-only data category. End-user personal data is not excluded; personal
  identifiers are protected (pseudonymized/encrypted — see Security / LGPD).
  (Short-lived vibes_bot-only caches like weather/pricing are out of scope, not
  "Redis-only venue data".)
- Do not couple RDS lifecycle to the cs-server deploy. RDS is standalone AWS
  infra provisioned independently (Terraform), reachable by DBeaver.
- Do not let vibes_bot open a direct RDS connection. cs-server is the sole
  application owner of the schema and migrations; vibes_bot reaches RDS only
  through cs-server's admin API. (Humans may still use DBeaver via SSM.)
- Do not adopt PostGIS-based serving. The geo nearest-neighbour query stays in
  Redis; RDS stores plain lat/lng columns used to rebuild the Redis geo index.
- Do not require a Redis flush, rename, or key-format migration. Existing Redis
  key formats are preserved as the projection target.

## Evidence
### cs-server (this repo)
- `app/dao/redis_venue_dao.py:21-36` defines every Redis key format (venues geo +
  `venues_geo_place_v1:{id}`, `live_forecast_v1`, `weekly_forecast_v1:{id}_{day}`,
  `vibe_attributes_v1`, `venue_photos_v1`, `opening_hours_v1`,
  `venue_instagram_v1`, `venue_reviews_v1`, `venue_menu_photos_v1`,
  `venue_menu_raw_data_v1`, `venue_ig_posts_v1`, `venue_vibe_profile_v2`,
  `admin_config:*`). This DAO is the single persistence boundary all pipelines
  use — the natural seam for write-through.
- `app/handlers/venue_handler.py:_transform` enumerates the exact read-set the
  nearby response needs (core venue, live busyness, weekly forecast, vibe
  attributes, photos, opening hours, instagram, reviews, vibe profile, menu).
  This read-set is the **projection contract**.
- `app/services/*` pipelines all write through the DAO: refresh (besttime
  catalog/inventory/live/weekly), google places enrichment, photo, instagram,
  instagram posts, menu photo, menu extraction, vibe classifier, eligibility
  sweep.
- `app/services/venue_eligibility.py` + `redis_venue_dao.soft_delete_venue`
  produce the rejection metadata (`lifecycle_status`, `deprecated_reason`,
  `deprecated_source`, `deprecated_at`) — the rejection-reason model to persist.
- `requirements.txt` has **no DB layer** (only `redis`, `boto3`). Postgres
  driver + migrations are net-new.
- Admin config is read live from Redis: `redis_venue_dao` photos TTL,
  `venue_budget_service` monthly budget, `venues_refresher_service` discovery
  points, `venue_eligibility` eligibility config.

### vibes_bot (/home/mario/projects/vibes_bot)
- `app/admin/config_dao.py` — `AdminConfigDao` connects **directly to the shared
  Redis** and read/writes `admin_config:*` (feature flags, blacklist, scoring
  weights, venue_types, discovery_points, venue_monthly_budget, etc.).
- `app/admin/routes.py` — the admin panel reads/writes **venue data and config
  directly in Redis**: `GET/PUT/DELETE /api/config/{key}`,
  `GET /api/venues/{id}` (reads all `*_v1:{id}` keys), `PUT /api/venues/{id}`
  and `PUT /api/venues/{id}/weekly/{day}` (write `venues_geo_place_v1`, weekly,
  photos, etc. directly).
- `app/services/crowd_sense_client.py` — venue **serving** retrieval is HTTP to
  cs-server `GET /v1/venues/nearby`. This path stays unchanged.
- `app/daos/favorites_dao.py`, `app/daos/hot_likes_dao.py` — end-user data keyed
  by `user_id` in Redis. Out of scope; must not enter RDS.
- `discovery_points` and `venue_monthly_budget` appear in **both** repos'
  config readers — confirms config is shared state today (via Redis), and must
  have a single owner tomorrow (RDS, owned by cs-server).

### PII / LGPD
- cs-server stores **no end-user PII**. The only personal-data field is
  `app/models/venue_review.py:VenueReview.author_name` (Google reviewer display
  names — third-party personal data under LGPD). End-user identifiers live only
  in vibes_bot's Redis (favorites/hot_likes) and stay out of RDS.

## Current Behavior
- All durable venue state lives in Redis only. There is no system of record;
  if Redis is lost, the data is gone (it can only be re-derived by re-running
  paid pipelines).
- Admin config is shared between the two repos by both writing/reading the same
  Redis `admin_config:*` keys. vibes_bot's admin panel mutates venue and config
  state directly in Redis.
- Serving reads Redis (geo + JSON). Live busyness is refreshed every ~30 min for
  all venues into Redis.

## Desired Behavior
- **RDS is the system of record.** Every pipeline write (venue core, besttime
  weekly forecast, google places enrichment, instagram, photos, reviews,
  opening hours, menu, vibe profile, lifecycle/rejection) is durably persisted
  to RDS, then projected into the existing Redis keys.
- **Redis is a projection fully rebuildable from RDS.** A rebuild job
  reconstructs every serving key **including the geo index** from RDS — now
  including live busyness (`besttime.live_forecast`), which is persisted as a
  current-state snapshot on each live refresh. Redis holds no durable-only data;
  it is the read interface. (Live busyness is high-churn and self-healing, so its
  RDS row is current-state-only — not append-only-historied, like photos.)
- **Serving is unchanged.** `GET /v1/venues/nearby` and vibes_bot retrieval keep
  reading Redis.
- **RDS owns config.** `admin.admin_config` is the source of truth. Admin writes
  go through cs-server's API → RDS → mirrored into the existing Redis
  `admin_config:*` keys, so cs-server's current config readers and vibes_bot's
  runtime config reads keep working unchanged off the Redis mirror.
- **vibes_bot admin panel becomes a DB interface.** Its config/venue read+write
  endpoints proxy to cs-server's admin API (which reads/writes RDS) instead of
  touching Redis directly. vibes_bot runtime and serving are unchanged.
- **Decoupling:** an RDS outage pauses pipeline persistence (writes fail loudly,
  are logged/metered, and must not corrupt the Redis projection) while serving
  continues from Redis.
- **User engagement is durable in RDS, written through the API.** vibes_bot
  **writes** favorites and hot_likes by calling a cs-server engagement API; that
  endpoint persists to RDS (truth, `user_id` pseudonymized) and projects to the
  existing Redis keys. vibes_bot **reads** favorites/hot_likes from Redis as
  today — a few seconds of projection staleness is explicitly acceptable. This is
  the same write-through-API / read-Redis pattern as venue data, so cs-server
  stays the sole RDS owner and vibes_bot never opens a DB connection.
- **Enrichment data and labels are never lost.** Every output of an
  enrichment/pipeline service — especially the *labels* (Google Places
  `vibe_attributes` incl. `google_primary_type`, AI `vibe_profile` photo tags,
  menu extraction, reviews, opening hours, instagram) — is durable in RDS and is
  **never hard-deleted**, because re-deriving it is expensive (paid Google/Apify/
  OpenAI calls). Removals are **soft-deletes with a timestamp** (`deleted_at`),
  recoverable by clearing the flag. Overwrites on re-enrichment of expensive
  derived labels are captured in an append-only history so a prior label set can
  be recovered (photos are excluded — their URLs expire, so old URL sets are dead
  links; see DDL note). Redis is only the
  *access* layer — a Redis cache eviction or `delete_*` never removes the durable
  RDS record. (The future internal-S3 photo-analysis store that replaces
  reprocessing Google will follow the same RDS-truth + Redis-projection pattern;
  its labels persist in RDS too.)
- **Google photos freshness is preserved (non-regression).** The recent
  TTL-eviction fix for stale Google photo URLs must keep working: the
  `venue_photos_v1` Redis projection keeps its `setex` TTL, and the photo-refetch
  trigger (`list_cached_venue_photos_ids` = Redis key absent) stays **Redis-only**
  — RDS must never be consulted for the refetch decision, or expired URLs would
  return. RDS holds a durable copy (with `fetched_at`). On **rebuild**, photos
  must NOT be projected with a fresh full TTL (that would re-serve already-expired
  URLs for a whole TTL window); project with **remaining freshness**
  (`TTL − age(fetched_at)`) so already-stale photos project as absent and refetch
  fires immediately — or skip photos on rebuild and let the cron refill (like
  live busyness).

## Implementation Approach

### A. cs-server data layer (the core)
Introduce a Postgres layer without disturbing the Redis DAO's interface:

- Add `psycopg[binary]` (or `asyncpg`) + `SQLAlchemy` + `alembic` to
  `requirements.txt`. Add DB settings to `app/config.py`
  (`rds_enabled` flag, host, port, db, user, password via env/secret, sslmode).
- New `app/db/rds_client.py` — connection pool + session management; reads
  credentials from env (injected from AWS Secrets Manager / env on the EC2).
- New `app/dao/rds_venue_store.py` (`RdsVenueStore`) — the **system-of-record
  writer/reader**: one upsert method per data type, persisting the Pydantic
  model's JSON into a JSONB payload column plus a few promoted query columns
  (see DDL). Mirrors the existing DAO method names. Every write also appends an
  `audit.enrichment_history` row.
- **Delete semantics:** the existing `RedisVenueDAO.delete_*` methods (and
  `delete_venue`) must NOT hard-delete from RDS. Routed through the repository,
  a "delete" becomes an RDS **soft-delete** (`deleted_at = now()` + history row)
  while it may still drop the Redis cache key (Redis is the ephemeral access
  layer). Labels/enrichment are thus always recoverable from RDS.
- New `app/dao/venue_repository.py` (`VenueRepository`) — the thin orchestration
  seam used by all pipelines and handlers:
  - **write(x):** `RdsVenueStore.upsert(x)` (truth) → `RedisVenueDAO.project(x)`
    (cache). RDS first; if RDS write fails, raise/log and **do not** write the
    stale projection.
  - **read for serving:** delegate to `RedisVenueDAO` (unchanged).
  - Keep `RedisVenueDAO` as the **projection writer** (its existing `set_*`/
    `upsert_venue` methods become the project step) so the rebuild job reuses
    them verbatim.
- Wire `VenueRepository` through `app/container.py` so every pipeline/service and
  the handler depend on it. When `rds_enabled` is false, `VenueRepository`
  degrades to Redis-only (today's behavior) — this is the rollout flag.
- **Live busyness is persisted too:** `set_live_forecast` writes RDS
  (`besttime.live_forecast`, current-state upsert) **then** projects to Redis.
  It is excluded only from the append-only history (high churn, self-healing) —
  it still lives durably in RDS so Redis holds no durable-only data.
- **FK ordering (behavior change vs Redis):** every enrichment table FK-references
  `venues.venue`, so an enrichment upsert for an unknown `venue_id` now *fails*
  where Redis silently accepted it. Guarantee core-row-first: the backfill inserts
  all `venues.venue` rows before any enrichment, and `VenueRepository` enrichment
  writes ensure the core venue row exists first (upsert a stub from the known
  `venue_id` if absent) so a stray enrichment write can never error on the FK.

### B. Rebuild + backfill (makes "projection" real)
- `app/services/redis_projection_service.py`:
  - `rebuild_redis_from_rds()` — read all active venues + enrichment from RDS and
    call the `RedisVenueDAO` project methods, **including `GEOADD` per venue** to
    repopulate `venues_geo_v1`. This is both the disaster-recovery tool and the
    Redis-warm path. Excludes live busyness.
  - `backfill_rds_from_redis()` — **one-time** import of the current Redis dataset
    into RDS to enable the switch-over. It does not need live/continuous data — a
    point-in-time snapshot is fine (dual-write keeps RDS current afterward). It
    may be implemented simply: scan each `*_v1:{id}` key group, **dump to one CSV
    per table, then bulk-load with Postgres `COPY`** (fast, simple, restartable),
    or upsert directly via `RdsVenueStore`. Must insert `venues.venue` rows before
    any enrichment rows (FK ordering). Idempotent on re-run.
- Expose both as admin jobs in `admin_trigger_router` (`rebuild_redis`,
  `backfill_rds`). The CSV export can also be run as a standalone one-off script
  for the initial migration.

### C. Config in RDS, mirrored to Redis
> **Delegated — see `plans/admin_config_rds_02_06_26.md`** (the owning plan for
> this work). The design below is correct and unchanged (synchronous
> RDS-write-then-Redis-mirror); it has been carved into that dedicated plan with
> the budget-counter scope boundary and the validation-dispatch detail resolved.
- `admin.admin_config(key, value jsonb, updated_by, updated_at)` is truth.
- cs-server admin config writes (eligibility-config, budget, discovery points,
  photos TTL, and the generic key CRUD vibes_bot needs) write RDS then mirror
  the JSON into the existing `admin_config:*` Redis key. cs-server's runtime
  readers and vibes_bot's runtime readers keep reading the Redis mirror — **no
  runtime reader changes required** in phase 1.
- Add cs-server admin endpoints to back the vibes_bot panel:
  - generic `GET/PUT/DELETE /admin/config/{key}` (RDS-backed, mirrors Redis);
  - venue-section read/edit endpoints to replace vibes_bot's direct
    `GET/PUT /api/venues/{id}` Redis access (write RDS → project Redis).

### D. Rejection reason
- Promoted columns on `venues.venue`: `lifecycle_status`, `deprecated_reason`,
  `deprecated_source`, `deprecated_at`, `google_business_status`.
- Lookup table `admin.rejection_reason(code, description, category)` seeded with
  the known reasons (`ineligible_empty_name`, `ineligible_name_keyword`,
  `ineligible_besttime_type`, `ineligible_google_type`,
  `google_places_closed_permanently`, …) so the admin panel can render
  human-friendly descriptions. `deprecated_reason` is a soft reference (text),
  not a hard FK, so new reasons never block a write.

### E. vibes_bot changes (companion plan — see note)
- `app/admin/routes.py` + `app/admin/config_dao.py`: replace direct-Redis config
  and venue mutations with calls to cs-server admin API (config CRUD + venue
  edits). Admin **reads** also proxy to cs-server so the panel reflects RDS truth.
- vibes_bot **runtime** config reads (scoring weights, vibe translations, etc.)
  remain Redis-mirror reads — unchanged.
- `CrowdSenseClient` serving path — unchanged.
- favorites/hot_likes — **writes** move to the cs-server engagement API
  (section F); **reads** stay on Redis (seconds staleness OK). This is the one
  user-facing vibes_bot path that changes (write only).

### F. User-engagement API (favorites / hot_likes write-through)
- New cs-server engagement endpoints, e.g. `POST/DELETE /v1/favorites` and
  `POST/DELETE /v1/hot-likes` (using the user identity vibes_bot already holds).
  Each call upserts/soft-deletes the RDS row (`user_pseudo = HMAC(user_id)`; raw
  id never stored) **then** projects the change into the existing Redis keys
  (`user_favorites:{user_id}`, `hot_likes:{venue_id}`) that vibes_bot reads.
- vibes_bot change: its favorites/hot_likes **write** DAOs call this API instead
  of writing Redis directly; **reads** stay on Redis (seconds of staleness OK).
- Because writes go through the API, removals (un-favorite) propagate to RDS
  naturally — no batch reconciliation needed. An optional periodic Redis→RDS
  reconciliation is belt-and-suspenders, not required.
- **Orphan guard:** for a `venue_id` absent from `venues.venue`, accept the like
  into Redis but ensure the core venue row exists before the RDS write (same
  core-row-first rule as pipelines) so the FK never drops a like.
- **Partial-failure:** the API commits RDS then projects Redis. If the Redis
  projection fails after the RDS commit, the API returns a non-success so
  vibes_bot retries (idempotent upsert), so the user's favorite isn't silently
  missing from their read path. (The venue rebuild job is venue-scoped and does
  not re-project engagement; a small periodic engagement reconcile is optional.)
- The HMAC key lives in the same secret store as the DB creds. LGPD access/
  erasure is served by hashing the requester's `user_id` and matching
  `user_pseudo`. Raw `user_id` remains only in the ephemeral Redis cache (as
  today); the durable RDS store is pseudonymized.

### G. Photos freshness non-regression (recent deliverable)
- Keep `set_venue_photos` projecting to Redis with `setex` TTL, and keep the
  refetch trigger `list_cached_venue_photos_ids()` reading **Redis only**. The
  `RdsVenueStore` photos write is a durable copy and must not be consulted by the
  refetch decision. On rebuild, project photos with **remaining** TTL
  (`TTL − age(fetched_at)`), so photos older than the TTL project as absent and
  refetch immediately (projecting expired URLs with a fresh full TTL would
  resurrect the bug for a whole window). Guard with tests on **both** paths: the
  refetch decision reads Redis only, and a day-old-RDS rebuild does not serve
  expired URLs.

## DDL (schema design decision — to be created via Alembic baseline)
Five Postgres schemas. Enrichment tables store the Pydantic model JSON in a
`payload jsonb` column (mirrors what is already serialized into Redis, minimizing
mapping code) plus promoted columns for querying. `venue_id` is the shared key.

```sql
-- ── venues ────────────────────────────────────────────────────────────────
CREATE SCHEMA venues;
CREATE TABLE venues.venue (
  venue_id              text PRIMARY KEY,
  venue_name            text NOT NULL DEFAULT '',
  venue_address         text NOT NULL DEFAULT '',
  venue_lat             double precision NOT NULL,
  venue_lng             double precision NOT NULL,
  venue_type            text,                 -- BestTime primary type
  price_level           int,
  rating                double precision,
  reviews               int,
  forecast              boolean NOT NULL DEFAULT false,
  processed             boolean NOT NULL DEFAULT false,
  lifecycle_status      text NOT NULL DEFAULT 'active',  -- active | deprecated
  deprecated_reason     text,                 -- soft ref to admin.rejection_reason.code
  deprecated_source     text,
  deprecated_at         timestamptz,
  google_business_status text,
  payload               jsonb NOT NULL,       -- full Venue model
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON venues.venue (lifecycle_status);
CREATE INDEX ON venues.venue (deprecated_reason);

CREATE TABLE venues.vibe_profile (venue_id text PRIMARY KEY REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE venues.menu_data   (venue_id text PRIMARY KEY REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE venues.menu_photos (venue_id text PRIMARY KEY REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());

-- ── besttime ──────────────────────────────────────────────────────────────
CREATE SCHEMA besttime;
-- One row per (venue, day_int 0..6). Live busyness is intentionally NOT stored.
CREATE TABLE besttime.weekly_forecast (
  venue_id text REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  day_int  int NOT NULL CHECK (day_int BETWEEN 0 AND 6),
  payload  jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (venue_id, day_int)
);

-- ── google_places ───────────────────────────────────────────────────────────
CREATE SCHEMA google_places;
CREATE TABLE google_places.vibe_attributes (
  venue_id text PRIMARY KEY REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  google_place_id     text,
  google_primary_type text,                   -- promoted for eligibility queries
  payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON google_places.vibe_attributes (google_primary_type);
CREATE TABLE google_places.opening_hours (venue_id text PRIMARY KEY REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE google_places.photos        (venue_id text PRIMARY KEY REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
-- reviews.payload retains author_name (kept per "all data in RDS"); it is
-- third-party personal data protected by at-rest encryption + SSM-only access.
CREATE TABLE google_places.reviews       (venue_id text PRIMARY KEY REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());

-- NOTE: google_places.photos.updated_at is the durable "fetched_at". Photo
-- freshness is still governed by the Redis venue_photos_v1 TTL (not by RDS).

-- ── instagram ───────────────────────────────────────────────────────────────
CREATE SCHEMA instagram;
CREATE TABLE instagram.handle (venue_id text PRIMARY KEY REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  instagram_handle text, payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE instagram.posts  (venue_id text PRIMARY KEY REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());

-- ── admin ─────────────────────────────────────────────────────────────────
CREATE SCHEMA admin;
CREATE TABLE admin.admin_config (
  key        text PRIMARY KEY,               -- e.g. venue_eligibility, discovery_points, venue_monthly_budget
  value      jsonb NOT NULL,
  updated_by text,
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE admin.rejection_reason (
  code        text PRIMARY KEY,
  description text NOT NULL,
  category    text);                          -- e.g. eligibility | closure

-- ── engagement (vibes_bot user data, durably persisted, pseudonymized) ──────
CREATE SCHEMA engagement;
-- user_pseudo = keyed HMAC of the raw user_id; the raw id is NEVER stored.
-- We can still serve LGPD access/erasure by hashing the requester's id and matching.
CREATE TABLE engagement.favorite (
  user_pseudo text NOT NULL,                  -- HMAC(user_id)
  venue_id    text NOT NULL REFERENCES venues.venue(venue_id)  -- RESTRICT: never cascade-delete labels,
  created_at  timestamptz NOT NULL DEFAULT now(),
  synced_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_pseudo, venue_id));
CREATE INDEX ON engagement.favorite (venue_id);
-- hot_likes are an ephemeral "trending" signal in Redis (TTL'd). For durable
-- metrics we record each like as an append-only EVENT (the Redis TTL counter
-- stays the live signal). Append-only → no soft-delete needed.
CREATE TABLE engagement.hot_like_event (
  id          bigserial PRIMARY KEY,
  user_pseudo text NOT NULL,                  -- HMAC(user_id)
  venue_id    text NOT NULL REFERENCES venues.venue(venue_id),
  created_at  timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON engagement.hot_like_event (venue_id, created_at);
```

Design notes:
- JSONB payload + promoted columns keeps the write-through cheap (serialize the
  model once) and avoids a brittle per-field ORM that must change every time a
  Pydantic model gains a field. Promote a column only when we need to filter on
  it (`lifecycle_status`, `deprecated_reason`, `google_primary_type`).
- `venue_monthly_budget` counters currently live in Redis; they become an
  `admin.admin_config` row (or a small dedicated table) so the budget survives
  Redis loss.
- **Retention / never-lose-labels (convention on every enrichment table):**
  - Add `deleted_at timestamptz NULL` to each enrichment table (vibe_attributes,
    opening_hours, photos, reviews, instagram.*, menus, weekly_forecast,
    vibe_profile). Removal = set `deleted_at` (soft); recovery = clear it. Rows
    are **never hard-deleted**.
  - No FK is `ON DELETE CASCADE` (RESTRICT default): venues are soft-deleted
    (`lifecycle_status`), so a venue is never hard-removed; and even if one were,
    its expensive labels must survive rather than cascade away.
  - Append-only history for recoverability of overwritten labels:
    ```sql
    CREATE SCHEMA audit;
    CREATE TABLE audit.enrichment_history (
      id          bigserial PRIMARY KEY,
      schema_name text NOT NULL,            -- e.g. google_places
      table_name  text NOT NULL,            -- e.g. vibe_attributes
      venue_id    text NOT NULL,
      payload     jsonb NOT NULL,           -- the value as written
      operation   text NOT NULL,            -- upsert | soft_delete
      written_at  timestamptz NOT NULL DEFAULT now());
    CREATE INDEX ON audit.enrichment_history (schema_name, table_name, venue_id, written_at);
    ```
    Each write/soft-delete of an **expensive derived label** (vibe_attributes,
    vibe_profile, menu extraction, reviews, opening_hours, instagram) appends a
    history row, so any prior label set is recoverable. **Photos are excluded
    from history**: Google photo URLs expire by design (that's the whole reason
    for the TTL-refetch deliverable), so historical URL sets are dead links with
    near-zero recovery value, and photos are the highest-churn enrichment
    (refetched every TTL window) — they would dominate history volume. Photos
    keep only their current-value row in RDS (saves a refetch on rebuild); they
    are not deep-historied. Derived labels are small text/JSON; the history cost
    is acceptable for the recoverability guarantee.

## Data Centralization Map (what else belongs in RDS)
Goal: centralize durable data in RDS; keep only ephemeral/self-healing data in
Redis. Mapped candidates across both repos:

**Centralize now (this plan):**
- All venue pipeline data + **labels** (venues, besttime weekly, google_places
  vibe_attributes/`google_primary_type`, opening_hours, reviews, photos,
  instagram, menus, AI vibe_profile photo tags) — never hard-deleted.
- Rejection reasons (lifecycle on `venues.venue` + `admin.rejection_reason`).
- Admin config — **both repos'** `admin_config:*` keys (cs-server: eligibility,
  discovery_points, photos TTL, budget; vibes_bot: scoring_weights,
  vibe_translations, busyness_labels, feature_flags, vibe_modes,
  onboarding_timing, venue_types, similar_venues, blacklist) → `admin.admin_config`.
- Monthly venue **budget counters** (today Redis) → survive Redis loss.
- **Engagement (explicit user actions, pseudonymized) via the write-through API:**
  favorites as current-state (soft-deletable); hot_likes as append-only events
  (`engagement.hot_like_event`) for durable metrics, with the Redis TTL counter
  staying the live "trending" signal.

**Centralize next (high value, low volume — small follow-up):**
- User profile/preferences/onboarding state (pseudonymized) — if user-level
  analytics is wanted. Confirm what vibes_bot persists for users today
  (`app/models/user.py`, firebase) before modeling.
- **External-API cost/usage telemetry** — per-pipeline BestTime/Google/Apify/
  OpenAI call counts + cost, so spend is queryable in the DB (high value given
  the cost-driven decisions in this product). New `telemetry` schema.
- Pipeline run history (job, started/finished, counts, outcome).

**Centralize later (high volume PASSIVE telemetry — separate analytics path):**
- Passive event streams (searches, venue impressions, clicks, dwell time) —
  distinct from the explicit user actions above; these are high-volume append-only
  streams; size them before adding (batch insert or a separate analytics store),
  do not co-mingle with the operational tables.

**Now also in RDS (per "all data in RDS"):**
- Live busyness — `besttime.live_forecast`, current-state upsert on each live
  refresh. High-churn + self-healing, so current-state only (no append-only
  history). Redis stays the read interface.

**Out of scope (vibes_bot-only short-lived caches):**
- weather, pricing, busyness predictions — vibes_bot's own ephemeral caches, not
  venue pipeline data; left as-is unless a specific analytics need arises.

## Data, Config, And API Impact
- **New infra:** standalone RDS Postgres (independent of cs-server deploy).
- **cs-server deps:** psycopg/SQLAlchemy/alembic added.
- **Migration execution model:** migrations are **run by a human via SSM**
  (`alembic upgrade head` from the EC2 or an SSM session), **not** on container
  startup — so a bad migration cannot block the app from booting/serving from
  Redis. cs-server is the sole schema owner, so there is no cross-repo ambiguity
  about who runs them. Document the command in the runbook and gate deploys that
  need a new migration on running it first.
- **cs-server config:** `rds_enabled`, `rds_host/port/db/user/password`,
  `rds_sslmode=require`. Missing/disabled ⇒ Redis-only (today's behavior).
- **New cs-server admin API:** `GET/PUT/DELETE /admin/config/{key}`, venue-section
  read/edit endpoints, and `rebuild_redis` / `backfill_rds` jobs.
- **Redis:** key formats unchanged (projection target). No flush/rename.
- **vibes_bot:** admin panel config/venue endpoints proxy to cs-server; runtime
  + serving + favorites/hot_likes unchanged.
- **Public serving contract:** unchanged.

## Error Handling And Observability
- RDS-first write: on RDS failure, raise + log with venue/op context, increment a
  failure metric, and **skip** the Redis projection so the cache never diverges
  from a failed truth-write. Pipelines continue to the next item (don't abort the
  whole job on one row).
- Serving never depends on RDS at request time — an RDS outage cannot break
  `GET /v1/venues/nearby`.
- Metrics: `rds_writes_total{table,result}`, `rds_write_duration_seconds`,
  `redis_projection_writes_total{result}`, `rebuild_redis_*` and `backfill_rds_*`
  summaries, `rds_up` gauge. Reuse existing soft-delete/deprecated metrics.
- Connection-pool exhaustion and SSL failures must log clearly and fail the
  write (not silently fall back to Redis-only, which would lose durability).
- Backfill/rebuild jobs log seen/written/skipped/errors and never partially
  corrupt serving (rebuild writes are idempotent upserts + GEOADD).

## Security / LGPD
- RDS: encryption at rest (KMS) + forced SSL in transit (`rds.force_ssl=1`),
  in a private subnet; **no public endpoint**. EC2→RDS via SG-to-SG reference
  (same VPC/region — co-locate RDS with the EC2 to keep the write-through round
  trip ~1-3ms). Engineers→RDS via **SSM Session Manager port-forward** through
  the EC2 (DBeaver connects to `localhost:<forwarded>`); auth is each engineer's
  AWS SSO login, IAM-scoped; no inbound DB rules, no IP allowlist to maintain.
- Credentials in AWS Secrets Manager (or SSM Parameter Store); injected to the
  EC2 as env. Never commit secrets; if one leaks, rotate.
- **LGPD — all data persisted, personal data protected** (per the directive to
  keep everything in RDS):
  - **End-user identifiers** (favorites/hot_likes `user_id`) are
    **pseudonymized**: stored only as `HMAC(user_id, secret)` (`user_pseudo`),
    never raw. Irreversible from the DB alone, still queryable, and supports LGPD
    access/erasure by hashing the requester's id. This is stronger than reversible
    encryption and is the recommended default for identifiers we never need to
    read back.
  - **Google review `author_name`** is third-party personal data that the app
    displays. It is **kept** in RDS, protected by encryption-at-rest (KMS) +
    SSM-only access (no public endpoint). Optional hardening: reversible
    app-level column encryption (decrypt when projecting to Redis) — recommended
    only if review author names need to be opaque to DBeaver operators; default
    is plaintext-at-rest given the data is already public on Google.
  - **At rest + in transit:** KMS encryption at rest on the whole instance +
    forced SSL. The HMAC/encryption keys live in the same secret store as the DB
    credentials.
  - Confirm at execution: HMAC-pseudonymize `user_id` (recommended) is assumed;
    if you instead need to recover raw user_ids from RDS, switch to reversible
    encryption — say so and the store changes accordingly.

## Test Plan
Feature file: `tests/bdd/persistence/rds_system_of_record.feature`

Scenarios (cs-server runtime contract): write-through (RDS truth + Redis
projection), enrichment persistence + projection completeness, live-busyness is
Redis-only, rejection reason persisted in RDS, admin config in RDS mirrored to
Redis, rebuild-Redis-from-RDS (incl. geo index, excl. live busyness),
serving-survives-RDS-outage, one-time Redis→RDS backfill (venues before
enrichment), **favorites/hot_likes synced to RDS pseudonymized (hot path stays
Redis)**, and **photos freshness non-regression** (TTL eviction + refetch trigger
stay Redis-only).

`# bdd-exempt: infrastructure` for RDS provisioning, Terraform, AWS SSO, SSM
tunnels, networking, and Alembic DDL — these are provisioning/IaC, validated by
the runbook + manual checks, not by Behave.

**BDD harness (required, do not skip):** the current harness
(`tests/bdd/environment.py`) wires only fakeredis + a programmable BestTime and
AGENTS.md forbids live external calls in BDD. Add a deterministic **in-memory
`RdsVenueStore` fake** (dict-backed, with a toggle to raise for the RDS-outage
scenario), injected into the harness exactly like `_ProgrammableBestTime`. All
behavior scenarios (write-through ordering, outage-skips-projection, rebuild,
backfill) run against this fake; **real Postgres fidelity** (JSONB round-trip,
promoted columns, FK ordering) is covered by the `pytest-postgresql` unit tests,
not by Behave. Without this fake the scenarios cannot reach a clean true-red.

Pytest unit tests:
- `RdsVenueStore`: upsert/select round-trips per data type against a Postgres
  test DB (or `testcontainers`/`pytest-postgresql`); JSONB payload fidelity;
  promoted columns populated; live busyness has no store method.
- `VenueRepository`: RDS-first ordering; RDS failure skips the Redis projection
  and surfaces the error; `rds_enabled=false` degrades to Redis-only.
- Projection completeness: the project step writes every key
  `venue_handler._transform` reads (guard test enumerating the read-set).
- `redis_projection_service`: rebuild repopulates JSON + geo index, excludes live
  busyness; backfill is idempotent.
- Admin config: write goes to RDS and mirrors Redis; generic config CRUD endpoint.
- Metrics on RDS write failure paths.
- Engagement API: a favorite/like write upserts RDS (`user_id` HMAC-pseudonymized,
  raw id never persisted) and projects Redis; an un-favorite soft-deletes the RDS
  row and removes it from Redis; reads come from Redis.
- **Never-hard-delete:** a `delete_*` soft-deletes the RDS row (`deleted_at` set,
  not dropped), appends an `audit.enrichment_history` row, and the prior value is
  recoverable; re-enrichment overwrites the current row but the old value survives
  in history.
- Photos non-regression guard (two paths): the refetch decision reads Redis only;
  and a rebuild from day-old RDS projects expired photos as absent (remaining-TTL)
  rather than serving stale URLs.
- Backfill: FK ordering (venue rows before enrichment) and idempotency on re-run.

Manual / integration checks:
- `make test-feature FEATURE=tests/bdd/persistence/rds_system_of_record.feature`.
- Targeted unit tests above. Postgres integration only against a disposable test
  DB — never production RDS.
- Infra runbook validation (below) in a staging RDS.

## Acceptance Criteria
- Every pipeline write persists to RDS as truth and projects to Redis; serving
  reads Redis unchanged.
- Live busyness is written only to Redis; RDS has no live-busyness table.
- Rejection reasons are durable in RDS and visible to the admin panel.
- Admin config is owned by RDS and mirrored to Redis; cs-server and vibes_bot
  runtime readers work unchanged off the mirror.
- `rebuild_redis_from_rds` reconstructs serving (incl. geo index) from RDS;
  `backfill_rds_from_redis` imports the existing dataset idempotently.
- An RDS outage does not break serving; failed truth-writes do not corrupt the
  Redis projection.
- vibes_bot admin mutations go through cs-server API (no direct Redis/RDS);
  vibes_bot serving + favorites/hot_likes hot path unchanged.
- **All data is in RDS** — including live busyness and favorites/hot_likes
  (written via the cs-server API, read from Redis) — with `user_id` stored only as
  a pseudonymized HMAC (raw id never persisted) and review `author_name` protected
  by at-rest encryption + SSM-only access. Redis holds no durable-only data; it is
  purely the read interface.
- **Enrichment data and labels are never hard-deleted** from RDS: removals are
  soft (`deleted_at`), overwrites keep an append-only history, and labels are
  recoverable without re-paying Google/Apify/OpenAI. Redis cache eviction never
  removes the durable RDS record.
- **Photos non-regression:** stale Google photo URLs still refresh on the Redis
  TTL after migration; the refetch trigger never consults RDS.
- A one-time backfill (CSV/`COPY` or direct upsert) loads the existing Redis
  dataset into RDS so the platform can switch to the DB-backed version.
- Engineers connect from home via SSM port-forward + DBeaver; RDS has no public
  endpoint.

## Open Questions
Resolved by the user: engagement **writes via cs-server API, reads from Redis**
(seconds staleness OK); favorites **and** hot_likes are persisted to RDS; all
enrichment labels live in RDS and are never hard-deleted (soft-delete + history).

Decisions made for you (object if wrong):
- **`user_id` pseudonymized via HMAC** (irreversible, queryable) rather than
  reversible encryption — assumes we never need raw user_ids back from RDS.
- **All data is in RDS, including live busyness** (`besttime.live_forecast`,
  current-state). Redis is purely the read interface; no Redis-only data.
- High-volume engagement **events** (searches/impressions/clicks) are mapped as
  "later / separate analytics path", not built in this plan.

Defaults to confirm during execution: Postgres 16, `db.t4g.small` single-AZ to
start (Multi-AZ later), `gp3` storage; RDS in the **same region/VPC as the EC2**
(confirm before `terraform apply`).

## Phased Rollout / Step-By-Step Runbook
**Phase 0 — Provision RDS (infra, bdd-exempt):**
1. Enable AWS IAM Identity Center (SSO); configure a profile so `aws sso login`
   opens the browser. Confirm `aws sts get-caller-identity` works.
2. Terraform module (`infra/rds/`): RDS Postgres in the EC2's VPC/private
   subnets, encrypted, `rds.force_ssl=1`, SG allowing ingress only from the
   cs-server EC2 SG; Secrets Manager secret for the master creds; **no** public
   access. `terraform apply` using the SSO profile (no long-lived keys).
3. Ensure the EC2 has the SSM agent + an instance profile with SSM permissions
   (for the port-forward path).
4. Create app DB role(s); run the Alembic baseline migration **manually via SSM**
   (`alembic upgrade head`) to create the five schemas + tables + seed
   `admin.rejection_reason`. Migrations are always human-run via SSM (never on
   container startup); re-run this command before any deploy that ships a new
   migration.

**Phase 1 — cs-server dual-write + backfill (behind `rds_enabled`):**
5. Add the DB layer (`rds_client`, `RdsVenueStore`, `VenueRepository`), wire the
   container, route all pipeline writes through the repository (RDS→Redis), and
   make `delete_*` soft-delete in RDS (never hard-delete labels; append history).
   Add the engagement write-through API (section F) and keep the photos refetch
   trigger Redis-only (section G). Store the HMAC pseudonymization key alongside
   the DB creds in Secrets Manager.
6. Deploy with `rds_enabled=true`; run the one-time `backfill_rds` (CSV/`COPY` or
   direct) to import the current Redis dataset; verify counts match. Reads still
   serve from Redis.
7. Validate the rebuild path on staging: flush a staging Redis, run
   `rebuild_redis`, confirm nearby serving + geo index are restored.

**Phase 2 — Config to RDS + vibes_bot admin via API:**
8. Move `admin_config:*` into `admin.admin_config`; cs-server admin writes go
   RDS→mirror Redis; add generic config CRUD + venue-edit admin endpoints.
9. Point vibes_bot's admin panel (config + venue read/write) at cs-server's
   admin API; remove its direct-Redis mutations. Runtime/serving unchanged.

**Phase 3 — Engineer access (ongoing):**
10. Each engineer: `aws sso login`; `aws ssm start-session --target <ec2-id>
    --document-name AWS-StartPortForwardingSessionToRemoteHost` forwarding the
    RDS endpoint:5432 to `localhost:5432`; connect DBeaver to `localhost:5432`
    with SSL. No VPN, no public endpoint, no IP allowlist.

## vibes_bot companion plan (separate lifecycle)
vibes_bot has its own `AGENTS.md` and `plans/`. The vibes_bot edits in section E
+ step 9 must be planned/executed under **vibes_bot's** lifecycle (a companion
`vibes_bot/plans/admin_via_cs_server_api_*.md`), not driven silently from this
cs-server plan. This plan defines the cs-server admin API contract vibes_bot
will consume; it does not itself edit vibes_bot.
