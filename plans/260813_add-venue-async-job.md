# Add-venue async job (single add)

## Branch
fix/add-venue-async-job

## Goal
`POST /venues/by-address` (the admin single add-venue call) must return quickly
and let the caller poll for the outcome, instead of blocking the HTTP request
for the full BestTime-create + enrichment + Instagram-discovery duration. An
operator must also be able to see recent add attempts and their outcomes
(pending, done with what result, or failed with why) after the fact — not only
by holding one poll open for the job they just started.

## Non-goals
- Any change to `AddVenueHandler.add()`'s own logic, BestTime pacing, Google
  enrichment, or Instagram discovery. This plan changes only how the call is
  invoked and how its result is delivered, not what it does.
- Any change to `POST /venues/batch-add` / `BatchAddService`. It already solved
  this problem for a curated list; this plan gives the single-venue path the
  same treatment via its own service, not by routing through batch's.
- The separate, already-tracked defect where a re-add of an existing venue can
  miss both the address-hash and geo short-circuits and spend a paid BestTime
  create. Unrelated failure mode; do not fold in.
- vibes_bot's proxy/UI changes — planned separately in that repo against the
  contract this plan fixes below.

## Evidence
- `app/routers/admin_trigger_router.py:540` — `add_venue_by_address` calls
  `await handler.add(request)` inline and returns only once it resolves. The
  request holds the HTTP connection open for the handler's full duration.
- `app/handlers/add_venue_handler.py` — that duration is effectively unbounded:
  up to `besttime_add_venue_timeout_seconds` (60s, `app/config.py:195`) for the
  BestTime create, + `timeout_recovery_grace_seconds` (~2s) + an
  account-inventory reconcile read on timeout (`add_venue_handler.py:460-501`),
  + Google Places enrichment (no fixed deadline), + up to
  `add_venue_instagram_deadline_seconds` (25s, `app/config.py:375`,
  `add_venue_handler.py:1286-1299`) for Instagram discovery.
- `app/api/besttime_client.py:76-134` (`_SearchRateLimiter.acquire`) — BOTH the
  create call (`besttime_client.py:589`) and the post-timeout inventory-reconcile
  read independently pace through the SAME shared BestTime venue-search window
  (30/min, 300/hour), each able to wait up to `besttime_rate_max_wait_seconds`
  (75.0, `app/config.py:206`) before proceeding. This wait is not included in
  any of the "worst realistic path" estimates that sized vibes_bot's proxy
  timeout, and it can occur twice in one request.
- vibes_bot has raised its client-side timeout around this exact call three
  times chasing the growing worst case (90s → 120s → 180s, most recently
  vibes_bot commit `b158d74` on 2026-08-12) and the false-timeout warning to the
  operator recurred the very next day — confirming the ceiling, not the
  request, is the wrong thing to keep changing.
- `app/services/batch_add_service.py` — already solves exactly this class of
  problem for a curated list: runs each row through this same
  `AddVenueHandler.add()` inside an `asyncio.Task`, persists a pollable job
  document to Redis (`JOB_KEY_FMT`, 7-day TTL) under a `job_id`, and is exposed
  via `POST /venues/batch-add` (202 + job_id) / `GET /venues/batch-add/{job_id}`
  (poll). `app/routers/admin_trigger_router.py:555-586` wires it.
- `app/services/job_lock.py` — the single-flight lock `BatchAddService` uses
  (`BATCH_ADD_LOCK = "batch_add"`) is a plain in-process `set[str]` keyed by an
  arbitrary name; it is not specific to batch and nothing requires a single-add
  job to share it.
- No existing "recent items" capped-list pattern exists in this repo to
  mirror (checked `app/services/`, `app/dao/` for an LPUSH/LTRIM or similar
  history idiom — none found); the small index this plan adds is a new but
  minimal pattern, not a divergence from one already established.
- `admin-ui/src/views/Venues/recentAdds.ts` in vibes_bot is a *different*,
  narrower thing this plan must not be confused with or repurposed into: a
  client-side, localStorage-backed duplicate-credit guard keyed on
  `place_id`/coordinates with a ~2-minute TTL, unrelated to job-outcome
  visibility. The monitoring surface this plan adds is server-side and
  outcome-oriented; it does not replace or touch that guard.
- `app/handlers/add_venue_handler.py:207-236` (`add()`) — the per-venue
  correctness guard already exists independently of any job/batch wrapper:
  `VENUE_ADD_LOCK_KEY_V1` (a short-TTL Redis single-flight lock keyed by the
  folded name+address hash) serializes concurrent reserve→create→persist calls
  for the SAME venue, and a loser waits for the winner via
  `_await_inflight_add` rather than double-spending. This guard is what makes
  concurrent calls to `.add()` for different venues already safe today — two
  operators (or an operator and a running batch job) adding different venues
  concurrently is the normal case the sync endpoint already allows; only the
  same venue submitted twice needs serialization, and that is already handled
  inside the handler, not by any caller-side lock.

## Current Behavior
An operator adds a venue in the admin panel. The panel POSTs the candidate to
vibes_bot, which proxies to `POST /venues/by-address` on cs-server and blocks
until `AddVenueHandler.add()` returns. When the handler is slow (BestTime
timeout + rate-limit pacing + discovery), the proxy call times out before
cs-server responds and the operator sees a false failure for an add that is
often still running, or already succeeded.

## Desired Behavior
`POST /venues/add-job` accepts the same body as `POST /venues/by-address`
(`AddVenueByAddressRequest`), starts `AddVenueHandler.add()` as a background
`asyncio.Task`, and returns immediately with `{job_id, status: "running"}`.
`GET /venues/add-job/{job_id}` returns the job's current state; once the task
finishes, the job carries the exact same `status_code` + `body` that
`AddVenueOutcome` has always produced (`already_exists`,
`matched_via_geo_fallback` with `newly_linked`/`match_reason`, `created`,
`created_google_only`, `recovered_from_timeout`, and the 429/502 error shapes
with `besttime_message` where present) — unchanged, just delivered a poll away
instead of synchronously. The existing `POST /venues/by-address` route and
`AddVenueHandler.add()` itself are untouched: any other caller of the sync
route keeps working exactly as today.

No new single-flight lock. Concurrent single-add jobs (different venues), and
a single-add job running alongside a batch-add job, are allowed to run at the
same time — the per-venue `VENUE_ADD_LOCK_KEY_V1` guard inside `add()` is what
prevents a real double-spend, and it is keyed per-venue, not per-caller, so it
already covers this regardless of how many job wrappers call `add()`
concurrently. Serializing single adds behind a lock (the way two batch runs are
serialized) would reintroduce a regression: an operator adding several
different venues back-to-back from search results — the normal admin workflow
— would start seeing spurious "already running" rejections.

A small monitoring surface exposes recent add jobs — venue, status, and
outcome or failure reason — so an operator can check on an add after
navigating away or having a poll die client-side, and so more than one
operator's admin session can see the same history. `GET
/venues/add-jobs/recent` lists the last `ADD_VENUE_RECENT_JOBS_CAP` (50) job
docs, newest first, each annotated with a short outcome label reusing the same
vocabulary `BatchAddService` already reports (`created`, `already_exists`,
`geo_linked`, `quota_exhausted`, `besttime_error`, etc.) so a failure's
*reason* — a BestTime rejection message, a quota block, or a job-runner crash
— is visible at a glance, not just a bare "failed".

## Implementation Approach
- `app/services/add_venue_job_service.py` (new, sibling to
  `batch_add_service.py`, not layered on top of it — the batch service's
  single-flight lock and multi-row `_classify()` summary shape don't fit a
  single add's concurrency model or its callers' expected response shape):
  - `AddVenueJobService(handler, redis_client)`.
  - `start_job(request: AddVenueByAddressRequest) -> dict`: mint a `job_id`
    (`uuid.uuid4().hex`), persist an initial `{job_id, status: "running",
    started_at, venue_name: request.venue_name, venue_address:
    request.venue_address}` doc to Redis under `admin:add_venue_job:{job_id}`
    (mirroring `batch_add_service.JOB_KEY_FMT`'s pattern in its own
    namespace; `venue_name`/`venue_address` are captured up front purely so
    the recent-jobs list below is self-describing without a second lookup),
    `LPUSH` the `job_id` onto `admin:add_venue_job_recent_v1` and `LTRIM` it
    to the last `ADD_VENUE_RECENT_JOBS_CAP` (50) entries, launch
    `asyncio.create_task(self._run_job(job_id, request))`, keep a task ref in
    an instance dict (`batch_add_service.py`'s GC-safety pattern), return
    `{job_id, status: "running"}`.
  - `_run_job`: `outcome = await self.handler.add(request)`; persist
    `status="done"`, `finished_at`, `http_status=outcome.status_code`,
    `result=outcome.body`. On an unexpected exception, persist `status="failed"`,
    `finished_at`, and a truncated `error` string — mirroring
    `BatchAddService._on_done`'s crash handling — and log it with the venue
    name/address for troubleshooting (CLAUDE.md: background jobs must not fail
    silently).
  - `get_job(job_id) -> Optional[dict]`: same read-and-decode shape as
    `BatchAddService.get_job`.
  - `list_recent(limit: int = 20) -> list[dict]`: `LRANGE` the recent-jobs
    list (capped at `ADD_VENUE_RECENT_JOBS_CAP` regardless of `limit`), read
    each job doc via `get_job`, silently drop any that come back `None`
    (aged out past the 24h TTL, or trimmed — both expected and harmless for a
    best-effort recency list), and annotate each `status="done"` doc with a
    short `outcome` label by running its `http_status`/`result` through the
    same classification `BatchAddService._classify` already computes.
    Extract `_classify` from `app/services/batch_add_service.py` into a new
    `app/services/add_venue_outcome_classify.py` (pure function, no batch-
    specific state) so both services share one outcome vocabulary instead of
    two copies of the same status-code/body mapping; update
    `batch_add_service.py`'s import accordingly with no behavior change.
  - TTL: 24 hours (`ADD_VENUE_JOB_TTL_SECONDS`) — long enough to survive a
    refreshed admin tab, short enough that this doesn't need batch's 7-day
    campaign-review window.
- `app/routers/admin_trigger_router.py`: add `POST /venues/add-job` (body
  `AddVenueByAddressRequest`, 202, `{job_id, status}`),
  `GET /venues/add-job/{job_id}` (404 if unknown), and
  `GET /venues/add-jobs/recent?limit=20` (`{"jobs": [...]}`), mirroring the
  existing by-address/batch-add routes' `require("add_venue_job_service",
  ...)` wiring. Leave `add_venue_by_address` and `batch_add_venues`
  unchanged.
- `app/container.py`: construct `self.add_venue_job_service =
  AddVenueJobService(handler=self.add_venue_handler,
  redis_client=redis_internal_client)` next to the existing
  `self.batch_add_service` construction.
- Metrics: no new Prometheus series. `AddVenueHandler.add()` already emits
  `ADD_VENUE_BY_ADDRESS_TOTAL` and `ADD_VENUE_INSTAGRAM_TOTAL` for every
  outcome regardless of caller, so business-outcome observability is unchanged.
  A job-runner crash (the `status="failed"` path) is logged with context, same
  as `BatchAddService` does today with no dedicated counter.

## Data, Config, And API Impact
New endpoints only; no request/response change to any existing route.
- `POST /venues/add-job` — request: `AddVenueByAddressRequest` (unchanged
  model). Response `202`: `{"job_id": str, "status": "running"}`.
- `GET /venues/add-job/{job_id}` — response `200` while running:
  `{"job_id": str, "status": "running", "started_at": float, "venue_name":
  str, "venue_address": str}`; response `200` once done: adds
  `"finished_at": float, "http_status": int, "result": dict` (`result` is
  exactly `AddVenueOutcome.body`); response `200` on a runner crash:
  `{"job_id", "status": "failed", "started_at", "finished_at", "venue_name",
  "venue_address", "error": str}`; response `404` for an unknown/expired
  `job_id`.
- `GET /venues/add-jobs/recent?limit=20` — response `200`: `{"jobs": [ <same
  shape as a single job's GET response, plus "outcome": str | null (present
  only once status is "done", using the shared classification vocabulary) >
  ]}`, newest first, capped at `ADD_VENUE_RECENT_JOBS_CAP` (50) regardless of
  `limit`.
New Redis key namespaces: `admin:add_venue_job:{job_id}` (JSON, 24h TTL) and
`admin:add_venue_job_recent_v1` (a capped LIST of job ids, no TTL — self-
bounding via `LTRIM` on every push) — no migration, purely additive.

## Error Handling And Observability
A crashed job runner is caught in `_run_job`, logged with the venue name and
address plus the exception type/message, and persisted as `status="failed"`
so a poller gets a terminal state instead of polling forever. `get_job`
returns `None` (→ 404) for an unknown or expired `job_id` rather than raising.
Every business-outcome metric the synchronous path already emits
(`ADD_VENUE_BY_ADDRESS_TOTAL`, `ADD_VENUE_INSTAGRAM_TOTAL`) fires identically
here, since both paths call the same unmodified `AddVenueHandler.add()`.

## Test Plan
Feature file: `tests/bdd/api/add-venue-async-job.feature`

Scenarios:
- Starting an add job returns 202 with a job id immediately, without waiting
  for BestTime.
- Polling a running job's id before it finishes reports status "running".
- Polling after the underlying add completes returns the same outcome body a
  synchronous add would have returned, for each of: created, already_exists,
  matched_via_geo_fallback (newly_linked), recovered_from_timeout.
- Polling after the underlying add fails (BestTime rejects with a message)
  returns the failure outcome including `besttime_message`, not a generic
  error.
- Polling an unknown job id returns 404.
- Two add jobs for two different venues started back to back both complete
  successfully (no spurious single-flight rejection).
- An add job started while a batch-add job is running completes successfully
  (no cross-lock rejection).
- The recent-jobs list includes a just-started job immediately, showing it as
  running.
- Once that job finishes created, the recent-jobs list shows it done with
  outcome "created" — without a second poll of the individual job.
- Once a job finishes rejected by BestTime with a message, the recent-jobs
  list surfaces that message as the failure reason, not a bare "failed".
- Once a job's runner crashes, the recent-jobs list shows it as failed with
  the error text.
- The recent-jobs list never exceeds its cap even after starting more jobs
  than the cap.

Pytest unit tests:
- `tests/test_add_venue_job_service.py` (new, mirrors
  `tests/test_batch_add_service.py`'s structure): `start_job` returns
  immediately and persists a running doc; `_run_job` persists the outcome
  body verbatim on success; an injected exception in `handler.add` is caught
  and persisted as `status="failed"` with a logged error, never raised out of
  the task; `get_job` round-trips through Redis JSON exactly like
  `BatchAddService.get_job`; `list_recent` returns newest-first, skips an
  expired/missing job id without raising, respects the cap under `LTRIM`, and
  annotates a done job with the same outcome label `_classify` would produce.
- `tests/test_batch_add_service.py` — unchanged behavior after `_classify`
  moves to the shared module (import-only change, same assertions pass).

Manual or integration checks:
- None required beyond the BDD/pytest coverage above — no live BestTime,
  Google, or Instagram calls are exercised differently than the existing
  by-address tests already fake.

## Acceptance Criteria
- `POST /venues/add-job` never blocks on BestTime, Google enrichment, or
  Instagram discovery; it returns as soon as the job is persisted and the
  background task is launched.
- The terminal job result for every existing `AddVenueOutcome` status/body
  shape is byte-for-byte the same as what the synchronous `POST
  /venues/by-address` returns for the same inputs today.
- `POST /venues/by-address` and `AddVenueHandler.add()`'s public behavior are
  unchanged.
- No single-flight lock blocks a single-add job against another single-add
  job or against a running batch-add job.
- A crashed job resolves to a polled `status="failed"` rather than hanging a
  poller forever.
- `GET /venues/add-jobs/recent` shows an operator the outcome (or failure
  reason) of any add started in the last `ADD_VENUE_RECENT_JOBS_CAP` jobs /
  24h, without needing to have kept a poll open on it.

## Open Questions
None.
