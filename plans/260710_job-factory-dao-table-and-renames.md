# Job Factory, Table-Driven DAO, Retry/Instrumentation Helpers, And Renames

## Branch
chore/job-factory-dao-table-and-renames

## Goal
Collapse the copy-paste scaffolding and fix the lying names identified by the
2026-07-09 refactor assessment (cs-server A1-A5, S6-S8, N1-N4) with zero
behavior change: ~700 LOC of near-identical wrappers/accessors/retry blocks
become small factories and table-driven specs; the two DAO container names
that plausibly caused a broken admin endpoint are renamed to what they hold.

## Non-goals
- Any behavior, key-format, metric-name, or API change — this batch is
  structure and naming only.
- The dead-code sweep, bug fixes, and N+1 performance work (planned
  separately in `plans/260710_dead-code-sweep.md`,
  `plans/260710_admin-breakdown-and-addvenue-fold.md`, and
  `plans/260710_projector-and-serving-bulk-reads.md`). This batch is
  sequenced LAST: the factories should not absorb code those plans delete or
  restructure.
- A shared "enrichment-pass runner" across the six `*_all_venues` services —
  explicitly rejected by the assessment (bespoke budget/credit semantics per
  loop; would trade visible duplication for parameter soup).
- Renaming anything in HTTP routes, Redis keys, RDS schema, metric names, or
  APScheduler job ids.

## Evidence
Verified against the code on 2026-07-10:

### Scaffolding (abstractions)
- **A1 — scheduler-job wrappers:** `main.py:54-343` defines eleven
  `run_*_job` coroutines (54, 75, 94, 113, 147, 175, 202, 229, 256, 283,
  310), each repeating the same ~25-line template: log start / perf timer /
  optional service-None guard / await the service call / the three job
  metrics (`BACKGROUND_JOB_DURATION_SECONDS`, `BACKGROUND_JOB_RUNS_TOTAL`,
  `BACKGROUND_JOB_LAST_RUN_TIMESTAMP`) in try+except. (`run_photo_enrichment_job`
  at 147-172 is deleted by the dead-code plan, leaving ten.)
  `start_background_jobs` (`main.py:401-571`) repeats an
  add-or-log-disabled block ~8 times.
- **A2 — Redis DAO accessors:** `app/dao/redis_venue_dao.py:277-1128` (file
  is 1,128 lines): ~10 entity types × {set/get/delete/list/count} ≈ 50
  structurally identical methods (format key → JSON (de)serialize →
  try/except → log). The projector's `_REBUILD_MODELS` spec table
  (`app/services/redis_projection_service.py:58-67`) models the table-driven
  approach. `VenueRepository` (`app/dao/venue_repository.py`) overrides the
  public methods individually, so the public method names must survive as
  one-line delegates onto private generics.
- **A3 — job registry re-encoded twice + pasted 503 preamble:**
  `app/routers/admin_trigger_router.py:119-177` — `_run_job` is a 12-branch
  if/elif mapping job names to service calls; `187-203` re-encodes
  service-availability knowledge as a second if/elif chain; the
  container-None 503 preamble is pasted into ~13 endpoints (lines 184, 230,
  273, 297, 310, 334, 350, 370, 503, 604, 763, 775, 793). `JOB_REGISTRY`
  (line 65) already exists but holds only labels/locks.
- **A4 — duplicated retry machinery in the BestTime client (riskiest item —
  credit-spending code):** `app/api/besttime_client.py:465-601`
  (`add_venue_to_account`) re-implements `_request`'s bounded-429-retry loop
  (`198-300`) with three deltas: a per-call timeout, the terminal
  monthly-cap 429 (stop retrying and surface the cap), and
  4xx-body-is-result parsing. The two loops have already drifted once (the
  timeout-recovery plans of 2026-07-01/02).
- **A5 — instrumentation boilerplate in the Google Places client:**
  `app/api/google_places_client.py` — `search_place_id` (107),
  `get_place_location` (191), `get_place_details` (257), `get_place_photos`
  (501), and the photo-URL fetch each repeat 15-30 lines of
  duration/calls/errors metric + log boilerplate with an inconsistent
  error-type taxonomy (`http_error`/`timeout`/`connection_error` emitted
  unevenly per method; `get_place_location` skips `ERRORS_TOTAL` entirely).

### Twin implementations (simplification)
- **S6 — twin TTL resolvers:** `app/dao/redis_venue_dao.py:499-540`
  (`_resolve_photos_cache_ttl_seconds`, admin key
  `venue_photos_cache_ttl_days`, unit days) vs `607-649`
  (`_resolve_fresh_photos_cache_ttl_seconds`, admin key
  `photo_fresh_cache_ttl_hours`, unit hours) — identical resolve-override/
  validate/fallback logic differing only in key, settings field, and unit.
- **S7 — twin refresh loops:**
  `app/services/venues_refresher_service.py:717-797`
  (`_refresh_with_discovery_points`) vs `799-859` (`_refresh_with_locations`)
  — ~80% identical budget-spending loop; both call
  `refresh_venues_data_by_venues_filter` (771, 840) with differently-shaped
  inputs (discovery points vs bare locations).
- **S8 — pending-enrichment re-implementation:**
  `app/services/google_places_enrichment_service.py:369-478`
  (`enrich_all_venues`) vs `480-559` (`enrich_pending_venues`) — the pending
  variant re-implements the per-venue enrich/pace/mark body instead of
  sharing a `_enrich_one_pending(...)` helper; pacing/marker policy can
  drift between them.

### Names that lie (naming)
- **N1:** `Container.redis_venue_dao` (`app/container.py:108-111`) holds a
  `VenueRepository` whose contract is *read RDS / write RDS-only*; the actual
  serving Redis DAO is `redis_only_dao` (`container.py:92`). This inversion
  forced the fuzzy getattr in
  `admin_trigger_router.py:371-373` (`_get_venue_dao_from_container`) and
  plausibly caused the broken `venue-type-breakdown` endpoint (fixed by the
  bug-fix plan). Rename `redis_venue_dao` → `pipeline_repository` and
  `redis_only_dao` → `serving_redis_dao` (~15 production call sites in
  `app/container.py`, `main.py`, and routers, plus test fixtures).
- **N2:** `refresh_venues_data_by_venues_filter`
  (`app/services/venues_refresher_service.py:453`) discovers venues, creates
  catalog rows, and spends new-venue budget — none of which "refresh data"
  conveys. The 2026-07-01 "restart ran discovery" incident is the failure
  mode this name enables. Rename →
  `discover_and_upsert_venues_via_filter`. Callers: the two S7 loops
  (771, 840) and `tests/test_services.py` (204, 253, 292, 387).
- **N3:** `_backfill_venue_review_signal`
  (`app/services/google_places_enrichment_service.py:78-161`) also derives
  and persists the served price tier. Rename →
  `_backfill_rating_reviews_and_price`.
- **N4:** `search_for_lgbtq_indicators`
  (`app/api/google_places_client.py:668-700`) is a local keyword scan —
  no awaits, no I/O, not a "search" against Google. Rename →
  `contains_lgbtq_keywords`, drop `async`, and move it to `app/services/`
  (it is business logic, not an API client concern). Callers:
  `google_places_enrichment_service.py:15` (import) and `263` (await —
  becomes a plain call).

Line-number drift vs the assessment: `_request` ends at 300 and
`add_venue_to_account` at 601 (cited 465-600); the availability if/elif
starts at 189 (cited 187); both BestTime/Google client files live under
`app/api/` (the assessment's short names resolve there). All immaterial.

## Current Behavior
Behavior is correct but encoded 11×/50×/13×/2× instead of once: adding a
scheduler job, DAO entity, or admin trigger requires copying a template and
remembering every metric/guard; the BestTime create retry has already drifted
from `_request` once; `container.redis_venue_dao` misleads every new reader
about which DAO writes Redis.

## Desired Behavior
Identical runtime behavior — same endpoints, same metrics (names, labels,
values), same logs' semantics, same Redis/RDS access — with:

- One `make_job(...)` factory producing the scheduler wrappers and one
  `schedule(...)` helper for the add-or-log-disabled block; new jobs cannot
  forget a metric. APScheduler job ids and `job_name` metric label values
  stay byte-identical.
- `RedisVenueDAO` public accessors preserved as one-line delegates onto
  private spec-table-driven generics (~1,128 → ~600 LOC), keeping
  `VenueRepository`'s per-method overrides working unchanged.
- `JOB_REGISTRY` entries gain `runner` / `service_attr` fields so `_run_job`
  and the availability listing derive from the registry; a `require(attr)`
  FastAPI dependency replaces the ~13 pasted 503 preambles (same 503
  status + detail strings).
- `BestTimeAPIClient._request` grows `timeout`, `stop_retry_on`, and
  `allow_4xx_body` parameters; `add_venue_to_account` delegates to it and
  keeps its exact externally observable semantics (per-call timeout raises
  the same `httpx.TimeoutException` the handler's recovery path expects;
  monthly-cap 429 remains terminal, never retried; 4xx bodies still parse to
  `NewVenueResponse`).
- An `_instrumented(endpoint)` async context manager in the Google Places
  client emits duration/calls/errors uniformly with one error taxonomy
  (`http_error`, `timeout`, `connection_error`) across all five methods.
- One `_resolve_admin_ttl_seconds(admin_key, settings_value, unit)` behind
  both TTL resolvers; one normalized loop behind S7 (inputs normalized to
  tuples); one `_enrich_one_pending(...)` behind S8.
- The four renames applied everywhere (production + tests), with
  `_get_venue_dao_from_container` reading the renamed attribute directly —
  the fuzzy `venue_dao` getattr fallback dies with the rename.

## Implementation Approach
Sequenced inside the branch as independent, individually revertible commits:
A1 (main.py factory) → A2 (DAO table) → A3 (registry + require dependency) →
S6/S7/S8 (twin merges) → A5 (instrumentation helper) → A4 (retry fold,
last and most carefully reviewed) → N1-N4 (mechanical renames, one commit).
No public module API changes except the four renames; every collapsed
implementation keeps its current log messages and metric emissions.

## Data, Config, And API Impact
None — behavior-preserving. No HTTP contract, Redis key, RDS schema, config,
or metric-name change. The renames touch Python identifiers only.

## Error Handling And Observability
- No new runtime path. The factories must preserve existing error handling
  exactly: job wrappers keep the same try/except + error log + error-status
  metric; `require(attr)` returns the same 503s; `_instrumented` emits the
  same metric families with a now-consistent `error_type` label taxonomy
  (values stay within the existing set so no dashboard breaks).
- A metrics-snapshot test (below) pins that the Prometheus registry exposes
  identical metric names/labels before and after — especially the job
  metrics' `job_name` values and the API-client families.
- A4 must preserve the BestTime credit-safety invariants: never retry the
  create itself on timeout (the timeout surfaces to the handler's
  reconcile-based recovery), never retry the monthly-cap 429, keep bounded
  Retry-After-aware 429 retries otherwise.

## Test Plan
# bdd-exempt: behavior-preserving refactor and renames — no user-visible or externally observable behavior change

Pytest unit tests:
- Full suite green (`make test-unit`) with only mechanical rename updates in
  tests (e.g. `tests/test_services.py` call sites of the N2 rename).
- Metrics-name snapshot: capture the registry's metric families + label
  names (and the `job_name` label values emitted by one run of each
  scheduler job against fakes) before the refactor; assert identical after
  the factory lands.
- A1: each factory-produced job records the three metrics with the same
  `job_name` on success and error, and skips with the same warning when its
  service is absent.
- A2: delegate parity — for every entity family, set/get/delete/list/count
  through `RedisVenueDAO` produce the same keys and values on fakeredis as
  today (existing `tests/test_redis_dao*.py` remain the guard);
  `VenueRepository` overrides still intercept (existing
  `tests/test_rds_repository.py`).
- A3: `_run_job` dispatch parity for every registry entry, unknown-job and
  unavailable-service errors unchanged; `require(attr)` yields 503 with the
  current detail strings.
- A4: `_request`-level tests for the three new parameters — per-call timeout
  raises `httpx.TimeoutException` untouched; a `stop_retry_on` 429 is
  terminal (no sleep/retry, response surfaced); `allow_4xx_body` returns the
  parsed body; and `add_venue_to_account` behavior tests (success, cap
  rejection, geocoder rejection, timeout) pass unchanged
  (`tests/test_besttime_client.py`, add-venue handler suites).
- A5: each Google client method emits duration/calls on success and the
  correct `error_type` on HTTP error, timeout, and connection error.
- S6/S7/S8: existing TTL-resolver, refresher, and enrichment tests pass
  unchanged; add a case pinning that both TTL resolvers honor their admin
  override and fall back on invalid values.

Manual or integration checks:
- Full BDD suite green (`make test-bdd`) — the add-venue features
  (timeout recovery, monthly cap, geo fallback) are the acceptance net for
  A4.
- One docker-compose boot: scheduler registers the same job ids, `/metrics`
  exposes the same families.

## Acceptance Criteria
- `make test-unit` and `make test-bdd` fully green.
- Prometheus registry snapshot (metric names + label names + `job_name`/
  `endpoint` label values) is identical before/after.
- `grep -rn "redis_venue_dao\|redis_only_dao"` over `app/ main.py tests/`
  returns no hits (both names fully replaced by
  `pipeline_repository`/`serving_redis_dao`); same for the other three old
  names.
- `add_venue_to_account` has no private retry loop; its timeout, monthly-cap,
  and 4xx-body semantics are proven unchanged by the existing BDD scenarios.
- `RedisVenueDAO` public method set is unchanged (delegates), and
  `VenueRepository` behavior tests pass without modification.
- Scheduler job ids and admin `JOB_REGISTRY` job names are byte-identical to
  today's.
- Net LOC reduction in the touched files of roughly 600-700 lines.

## Rollback
Revert the PR (or the individual commit for one concern — the branch is
sequenced as independently revertible commits). No persisted state, schema,
config, or Redis-key impact; the serving projection is unaffected. A4 is the
only credit-adjacent change — if any add-venue anomaly appears in
production, revert just the A4 commit and the previous hand-rolled loop is
restored verbatim.

## Open Questions
- **A4 shipping shape:** fold `add_venue_to_account`'s retry into `_request`
  inside this batch (as planned), or split it into its own PR for isolated
  review/rollback given it is live credit-spending code? Plan default:
  keep it in this batch as the last, isolated commit.
- **N1 test fixtures:** the rename touches ~30/~50 references including test
  fixtures — confirm there is no out-of-repo consumer (scripts, runbooks,
  vibes_bot admin panel) that reflects on the container attribute names.
  (Code search inside this repo found only in-repo references.)
