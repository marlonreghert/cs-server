# Admin Config → RDS (system of record, Redis mirror)

## Branch
feature/admin-config-rds

## Goal
Make AWS RDS (`admin.admin_config`, already created by the baseline migration but
currently **empty and unused**) the **system of record** for admin configuration,
with Redis demoted to a **mirror**. A new cs-server admin API writes the value to
RDS first, then **synchronously mirrors** the existing Redis `admin_config:*` key
in the same request, so the durable source survives Redis loss while **every
current runtime reader keeps reading the Redis mirror unchanged**.

This completes **Phase 2** of `plans/rds_system_of_record_01_06_26.md` (the data
plane — venues/enrichment/live — was cut over and backfilled on 2026-06-01; admin
config is the remaining durable-state gap, the one behind "DBeaver shows no admin
config").

Config is a **synchronous RDS-write-then-Redis-mirror carve-out** (the same shape
as the engagement API), **not** the venue projector — config writes are rare, need
immediate read-back, are global (not venue-keyed, so the venue projector can't
surface them), and are a single-row write plus a few Redis SETs (none of the
event-loop-blocking risk that bit the backfill).

## Non-goals
- Do not change runtime config **readers**. cs-server's `venue_eligibility`,
  `venue_budget_service`, `venues_refresher` (discovery points), photos-TTL
  reader, and vibes_bot's runtime readers keep reading the Redis `admin_config:*`
  mirror, in the same JSON format. No reader churn in this plan.
- Do not edit vibes_bot. This plan **defines the admin config API contract**
  vibes_bot will consume; repointing the vibes_bot admin panel to write through
  that API is a **companion** under vibes_bot's own lifecycle (see Open Questions).
- Do not move the monthly **budget counter** (the atomic INCR/DECR reservation
  primitive) into RDS in this plan — only the budget **config** (quota/reserve).
  See Open Questions; the counter is a concurrency primitive, not config.
- Do not change the public serving contract or any venue/enrichment path.
- Do not require a Redis flush or key-format change — the mirror reuses the exact
  existing `admin_config:{key}` JSON-string representation.

## Evidence
- `migrations/versions/0001_baseline_schemas.py` — `admin.admin_config(key PK,
  value jsonb, updated_by, updated_at)` exists; **no code reads/writes it** today
  (verified: `grep "admin.admin_config"` in `app/` is empty).
- Existing config write pattern to generalize:
  `app/routers/admin_trigger_router.py:332-358` (`POST /venues/eligibility-config`)
  does `client.set(ADMIN_CONFIG_ELIGIBILITY_KEY, json.dumps(config))` — Redis value
  is a **JSON string** under `admin_config:venue_eligibility`. `GET` at `:320` reads
  via `load_eligibility_config(client)`.
- Current Redis `admin_config:*` readers (all read Redis, parse JSON, fall back to
  defaults): `app/services/venue_eligibility.py:36` (`venue_eligibility`),
  `app/services/venue_budget_service.py:27,66-97` (`venue_monthly_budget` →
  `get_quota_settings`), `app/services/venues_refresher_service.py:100`
  (`discovery_points`), `app/dao/redis_venue_dao.py:29` (`venue_photos_cache_ttl_days`).
- **Budget counter vs config (key finding):** `venue_budget_service.py` reads the
  *config* (quota/reserve) from `admin_config:venue_monthly_budget` (config-shaped),
  but the monthly **count** is an atomic Redis INCR/DECR via `VenueBudgetDao`
  (`reserve_manual_slot` = INCR-then-validate-then-rollback; `:130-167`). The
  counter is a token-bucket concurrency primitive, **not** config.
- Carve-out pattern to mirror: `app/services/engagement_service.py:39-56` —
  RDS-first then synchronous Redis projection in the same call; non-success on
  partial failure so the caller retries.
- `app/dao/rds_venue_store.py` — has venue/enrichment/live/engagement methods; **no
  admin_config methods yet** (net-new: `get_admin_config`/`upsert_admin_config`/
  `delete_admin_config`/`list_admin_config`).
- vibes_bot `app/admin/config_dao.py` + `app/admin/routes.py` — the admin panel
  writes `admin_config:*` **directly to Redis** today (feature flags, scoring
  weights, busyness, venue_types, blacklist, discovery_points, budget, …). These
  are the vibes_bot-owned keys; making them RDS-authoritative needs the companion.

## Current Behavior
Admin config lives **only in Redis** (`admin_config:*` JSON strings), written
directly by cs-server's eligibility-config endpoint and by the vibes_bot admin
panel, read live by both services. `admin.admin_config` in RDS is empty. If Redis
is lost, all admin config is gone (no durable copy).

## Desired Behavior
- **RDS owns admin config.** A write goes to `admin.admin_config` (truth) then
  mirrors `admin_config:{key}` in Redis (same JSON), in one request.
- **Generic admin config API:** `GET/PUT/DELETE /admin/config/{key}` plus a list
  endpoint, backing both cs-server's own config and (via the companion) the
  vibes_bot panel. The existing `/venues/eligibility-config` endpoints become thin
  wrappers over (or are superseded by) the generic path, preserving validation.
- **Readers unchanged:** every current reader keeps reading the Redis mirror; the
  mirror format is byte-compatible, so no reader changes.
- **One-time backfill:** an admin job imports all existing `admin_config:*` Redis
  keys into `admin.admin_config` so nothing is lost at switchover. Idempotent.
- **Budget config** (quota/reserve) migrates as a normal config key; the **budget
  counter stays Redis** (see Open Questions).
- **Partial-failure:** RDS commit then mirror; if the mirror fails after commit,
  return a non-success status so the caller retries (idempotent upsert).
- **Outage:** if RDS is unavailable, a config write fails loudly and the existing
  Redis mirror is left intact, so runtime readers keep serving the last config.

## Implementation Approach
1. **`RdsVenueStore` admin-config methods** — `get_admin_config(key)`,
   `upsert_admin_config(key, value, updated_by)`, `delete_admin_config(key)`,
   `list_admin_config()`. JSONB `value` column; trivial single-row SQL.
2. **`AdminConfigService`** (new, mirrors `EngagementService` shape) — holds
   `rds_store` + the raw Redis client. `set(key, value, updated_by)`:
   `rds_store.upsert_admin_config(...)` (truth) → `redis.set(f"admin_config:{key}",
   json.dumps(value))` (mirror). `delete(key)`: RDS delete → `redis.delete(...)`.
   Gated like engagement: when `rds_store is None`, behave as today (Redis-only)
   so flag-off is unchanged.
   - **`get(key)` reads the Redis MIRROR as the live value** (RDS is the durable
     source). Rationale: for cs-server-owned keys the API keeps RDS and mirror
     identical, so reading the mirror == reading RDS; for not-yet-owned vibes_bot
     keys (still written directly to Redis until the companion), the mirror is the
     fresh authoritative value while the RDS snapshot may be stale — reading the
     mirror avoids the "DBeaver/RDS shows X, app shows Y" divergence. After the
     companion makes all keys API-written, RDS == mirror and it's moot. The list
     endpoint may surface the RDS `updated_at` for durability transparency.
   - **Caveat (state in docs):** because config has no projector, a **direct
     DBeaver edit to `admin.admin_config` does NOT reach the app** — config changes
     must go through the API (which mirrors Redis). Same shape as the venue smoke.
3. **Router** — generic `GET/PUT/DELETE /admin/config/{key}` + `GET /admin/config`
   (list) in `admin_trigger_router` (or a new `admin_config_router`).
   - **Validation dispatch (correctness requirement, not optional):** the generic
     `PUT` MUST dispatch to a per-key validator **before** persisting — superseding
     `/venues/eligibility-config` without it would let malformed eligibility config
     land in RDS+Redis and break the next eligibility sweep. Use a validator
     registry: `venue_eligibility` → `EligibilityConfig.from_dict(...,
     from_admin_override=True)`; unknown keys accept arbitrary JSON (vibes_bot keys
     are opaque to cs-server). **Persist the byte-exact shape the reader parses** —
     today the eligibility endpoint stores the **raw body** (`json.dumps(config)`)
     while returning a *normalized* `to_public_dict()`; confirm `load_eligibility_config`
     consumes the raw body and persist that exact shape (the "mirror format
     byte-compatibility" pytest below guards this).
   - Reconcile the existing `/venues/eligibility-config` GET/POST to delegate to
     this service (so eligibility lands in RDS too) without dropping its validation.
4. **Backfill** — `admin_config_backfill` admin job: **generic `SCAN
   admin_config:*`** in Redis → upsert each into `admin.admin_config`. Because it
   scans by prefix, it **covers every key automatically** (no hardcoded list).
   Idempotent; run as an **off-loop one-off** (`docker exec` building a `Container`,
   like the venue backfill) so it never blocks serving. Wire into the admin jobs
   registry too. Known keys it will pick up (cs-server + vibes_bot, enumerated from
   `vibes_bot/app/admin/config_dao.py`): `venue_eligibility`, `discovery_points`,
   `venue_monthly_budget`, `venue_photos_cache_ttl_days`, `feature_flags`,
   `busyness_labels`, `vibe_translations`, `venue_blacklist`, `scoring_weights`,
   `insights_thresholds`, `venue_types`, `similar_venues`, `hot_likes`, `weather`,
   `busyness_prediction`, `vibe_modes`, `onboarding_timing`. (Whatever exists under
   the prefix at run time is what migrates — self-adjusting.)
   - **Backfill execution — who does what:**
     - *Me (cs-server code):* implement `AdminConfigService` + endpoints + the
       generic backfill, drive BDD/pytest green, and run the off-loop backfill via
       `docker exec` (no serving impact); verify `count(admin.admin_config)` vs the
       `admin_config:*` key count.
     - *You (ops):* deploy the new cs-server code (it's a new SHA on
       `marlonreghert/cs-server` main → a vibes_bot `[FULL-RESTART]` deploy picks
       it up, same path as the cutover); then I trigger/run the backfill.
     - *vibes_bot (companion, later):* repoint the admin panel to write config
       through `/admin/config` so vibes_bot-owned keys become authoritative. Until
       then those keys are a point-in-time snapshot (accepted — see Resolved #2).
5. **Budget config → RDS + Redis sync (Resolved #1).** The budget *config*
   (`monthly_quota`/`manual_reserve`, key `venue_monthly_budget`) is written
   through the config API → RDS truth + Redis mirror, like every other key;
   `VenueBudgetService.get_quota_settings()` keeps reading the Redis mirror
   unchanged. The budget **counter** (atomic monthly INCR/DECR token-bucket) stays
   in Redis — it is an operational concurrency primitive, not config, and self-heals
   monthly.
6. **Metrics/observability** — `admin_config_writes_total{key,result}`, reuse
   `rds_writes_total`; log RDS-write and mirror-write failures with key context
   (never log secret-ish values verbatim if any are added later).
7. **Decoupling-plan reconciliation — ✅ DONE** (PR #25):
   `plans/redis_projection_decoupling_01_06_26.md` §C now states "config = synchronous
   RDS-write-then-mirror carve-out (like engagement), owned by this plan, not the
   projector"; `plans/rds_system_of_record_01_06_26.md` Phase 2 now points here.

## Data, Config, And API Impact
- **DDL:** none — `admin.admin_config` already exists. (If delete should be
  recoverable, a follow-up could add `deleted_at`; this plan uses hard DELETE —
  config is admin-set and reproducible, not an expensive label.)
- **New API:** `GET/PUT/DELETE /admin/config/{key}`, `GET /admin/config`; new
  admin job `admin_config_backfill`. `/venues/eligibility-config` preserved
  (delegates to the new path).
- **Config flag:** reuses `rds_enabled` (already true in prod). Flag-off →
  Redis-only (today's behavior).
- **Redis:** key formats and JSON representation **unchanged** (mirror target).
- **vibes_bot:** no change in this plan; companion repoints its panel writes.

## Error Handling And Observability
- RDS-first write: on RDS failure, raise + log with key/op context, increment a
  failure metric, and **do not** touch the Redis mirror (no divergence from a
  failed truth-write).
- Mirror failure after RDS commit: return non-success (e.g. 502) so the caller
  retries; upsert is idempotent so a retry converges.
- Readers never depend on RDS at request time (they read the Redis mirror) — an
  RDS outage cannot break eligibility/budget/discovery/photo-TTL resolution.
- Backfill logs seen/written/skipped/errors; idempotent on re-run.

## Test Plan
Feature file: `tests/bdd/persistence/admin_config_rds.feature`
(reuses/supersedes the `@wip` "Admin configuration is stored in RDS and mirrored
to Redis" scenario in `rds_system_of_record.feature` — remove that `@wip`
scenario or leave it pointing here).

Scenarios: write-through (RDS truth + Redis mirror); a running reader reflects the
update via the mirror with no reader change; delete removes RDS + mirror →
default fallback; one-time backfill imports existing keys; mirror-failure returns
a retryable status (idempotent retry restores mirror); RDS-outage write fails
loudly without changing the mirror and readers keep working.

`# bdd-exempt: infrastructure` for the `admin.admin_config` migration (already
applied) and any RDS provisioning.

Pytest unit tests:
- `RdsVenueStore` admin-config round-trip (upsert/get/delete/list; JSONB fidelity).
- `AdminConfigService`: RDS-first then mirror ordering; mirror-failure surfaces
  non-success after the RDS commit; `rds_store=None` degrades to Redis-only.
- Mirror format byte-compatibility: a value written via the API is readable by the
  existing readers (e.g. `load_eligibility_config`, `get_quota_settings`).
- Backfill idempotency + that it imports every `admin_config:*` key.
- Eligibility endpoint still validates and now lands in RDS.

Manual / integration checks:
- `make test-feature FEATURE=tests/bdd/persistence/admin_config_rds.feature`.
- Post-deploy: run `admin_config_backfill`, then in DBeaver confirm
  `select count(*) from admin.admin_config` matches the `admin_config:*` key count
  in Redis.

## Acceptance Criteria
- A config write via `/admin/config/{key}` lands in `admin.admin_config` (truth)
  and mirrors `admin_config:{key}` in Redis with the identical JSON; existing
  readers reflect it with no reader change.
- Delete removes the RDS row and the Redis mirror; readers fall back to defaults.
- The one-time backfill imports every existing `admin_config:*` key into RDS,
  idempotently, with the Redis mirror unchanged.
- A failed mirror after the RDS commit returns a retryable status; retry converges.
- An RDS outage fails the write loudly and leaves the Redis mirror (and readers)
  working.
- `rds_enabled=false` reproduces today's Redis-only behavior.
- Budget quota/reserve is settable via the config API and lands in RDS; the budget
  counter is unaffected.

## Open Questions
**All resolved (2026-06-02, by user) — this plan is ready to `/execute-feature`.**
1. **Budget — config to RDS, counter stays Redis.** The budget *config*
   (quota/reserve, `venue_monthly_budget`) goes to RDS with Redis sync like every
   other key (Impl §5). The monthly *counter* (atomic INCR/DECR token-bucket) stays
   in Redis — operational concurrency primitive, not config, self-heals monthly.
2. **Snapshot is acceptable.** vibes_bot-owned keys are backfilled as a
   point-in-time snapshot (durability/visibility) and become authoritative only
   when the vibes_bot companion repoints its panel through the API. A slightly
   stale snapshot — and losing self-healing/minimal data (e.g. ephemeral cache
   config that refills) — is explicitly OK.
3. **Backfill is generic + autonomous.** It `SCAN admin_config:*` and upserts each
   row, so it covers all keys automatically (full enumerated list in Impl §4 for
   reference). I implement + run it off-loop; **you** deploy the new cs-server code
   via a vibes_bot `[FULL-RESTART]`; the **vibes_bot** authoritative-write companion
   is separate (Impl §4 "who does what").
4. **Hard delete.** A config DELETE removes the RDS row + Redis mirror; verified
   safe — `venue_eligibility`, `discovery_points`, `venue_photos_cache_ttl_days`,
   and `venue_monthly_budget` readers all default cleanly on a missing key.
