# Venue-Add Job Tracking On RDS (Not Redis)

## Branch
fix/venue-add-job-rds-tracking

## Goal
Move venue-add job tracking (both `AddVenueJobService`'s single-add jobs and
`BatchAddService`'s batch-add jobs) off Redis-only storage and onto the RDS
system of record, so job history persists durably for audit purposes and a
job that was mid-flight when its owning process restarts is deterministically
reconciled to a terminal state instead of being stuck showing `"running"`
forever.

## Non-goals
- No change to `AddVenueHandler.add()` itself, BestTime pacing, Google
  enrichment, or Instagram discovery.
- No change to the in-memory `BATCH_ADD_LOCK` single-flight guard in
  `app/services/job_lock.py` — it is explicitly documented as correct for
  this single-process deployment and already self-heals on restart (a fresh
  process boots with an empty lock set).
- No general backfill of historical job records already sitting in Redis at
  the time this ships — RDS tracking starts from this feature's rollout
  forward, not retroactively. The one exception is the specific stuck
  record from the 2026-08-14 incident, corrected out-of-band per
  "Incident Remediation" below — that is a one-time, scripted correction of
  one known-bad record, not a general migration mechanism, and does not
  wait for this plan's code changes to ship.
- No change to the public API/response contract of `POST /venues/add-job`,
  `GET /venues/add-job/{job_id}`, `GET /venues/add-jobs/recent`,
  `POST /venues/batch-add`, `GET /venues/batch-add/{job_id}` — vibes_bot's
  admin-ui (`AddVenue.tsx`, `RecentAddJobs.tsx`) and the `batch-add-venues`
  skill both consume these response shapes today; this plan changes only the
  storage layer underneath them.
- No retention/archival policy for old job rows — at this operation's volume
  (dozens to low hundreds of jobs a month), an unbounded
  `admin.venue_add_job_run` table is not a real storage concern; pruning can
  be a separate future plan if it ever becomes one.

## Evidence
- `app/services/batch_add_service.py`: `_save()` writes the full job dict
  (including the growing `results` list) to Redis via `setex(JOB_KEY_FMT,
  JOB_TTL_SECONDS=7d, ...)` on every processed row. `_on_done`'s crash
  handler runs via `task.add_done_callback` — in-process only; it never
  fires if the whole process dies, so the job's last-written Redis doc is
  left however it was, `"status": "running"` if the process died mid-run.
- `app/services/add_venue_job_service.py`: identical pattern —
  `JOB_KEY_FMT`/`JOB_TTL_SECONDS=24h`, plus `admin:add_venue_job_recent_v1`,
  a capped Redis LIST for `list_recent()`. Same in-process-only crash
  handling in `_run_job`'s except block.
- 2026-08-14 incident (this session): submitted a 58-row batch-add retry
  (`job_id=2d07cf4450ea466ea6a7b8b95fc11211`). An unrelated deploy on the
  shared prod stack recreated both the `vibes_bot-cs-server-1` and
  `vibes_bot-admin-1` containers mid-run (confirmed via `docker inspect
  --format {{.State.StartedAt}}` jumping to a new boot time, `/tmp` wiped
  on both). The job's Redis record was left at `"status": "running",
  "processed": 26` permanently — nothing was left alive to advance or fail
  it. Recovery required treating live RDS inventory as ground truth and
  resubmitting the actual gap, rather than trusting the job record.
- `app/services/job_lock.py`: `BATCH_ADD_LOCK`'s single-flight guard is
  explicitly documented as "In-process only... a plain module-level set is
  sufficient" — confirmed self-healing during the incident (a fresh
  process's empty `_running` set let new batch-add submissions through
  immediately after the restart, with no stuck lock).
- `app/dao/rds_venue_store.py`: the established RDS-write convention in this
  repo — synchronous SQLAlchemy (`create_engine(..., pool_pre_ping=True,
  future=True)`, `with self.engine.begin() as conn: conn.execute(text(...),
  ...)`), interface-matched to a `tests.rds_fake.InMemoryRdsVenueStore` fake
  for BDD/unit coverage without a live Postgres.
- `main.py` `startup_essential()`: already runs one blocking RDS read off
  the event loop on every boot (`await loop.run_in_executor(None,
  container.eligibility_rule_service.rehydrate_mirror)`) specifically so a
  blocking SQLAlchemy call cannot stall `/health` — the established pattern
  this plan's write path and startup reconciliation both need to follow.
  Its docstring also documents the hard "no pipeline runs on startup"
  policy from the 2026-07-01 incident (a restart must never re-trigger paid
  work); this plan's startup step is a pure status-metadata `UPDATE`, not a
  resumed run, so it does not conflict with that policy.
- `migrations/versions/0030_crawl_target.py`: nearest existing precedent for
  an "operational tracking" table in this schema style —
  `last_run_at`/`last_run_results`/`last_run_cost_usd`/
  `consecutive_failures` columns, raw-SQL `CREATE TABLE IF NOT EXISTS
  <schema>.<table>` migrations, `timestamptz NOT NULL DEFAULT now()` audit
  columns. `admin` schema already exists (`0001_baseline_schemas.py`; holds
  `admin.geo_fence`, `admin.geo_fence_city`, `admin.admin_config`).
- vibes_bot: `admin-ui/src/views/Venues/AddVenue.tsx` polls `GET
  /api/admin/venues/add-job/{id}` expecting `{status, started_at,
  finished_at, http_status, result}`; `RecentAddJobs.tsx` polls `GET
  /api/admin/venues/add-jobs/recent` expecting `{jobs: [...]}` with
  `outcome` present only once `status: "done"`.
  `.claude/skills/batch-add-venues/SKILL.md` polls `GET
  /venues/batch-add/{job_id}` expecting `{status, processed, total,
  summary, results, budget_before, budget_after}`. Both are hard contracts
  this plan must not change.

## Current Behavior
Both `BatchAddService` and `AddVenueJobService` persist their entire job
document as a single JSON blob in Redis, keyed by job id, with a TTL (7 days
/ 24 hours respectively). A crash inside the owning process's `_run_job`
coroutine is caught and recorded (`status: "failed"`), but this handling
lives entirely in-process — a `task.add_done_callback`/`except` block that
never runs if the process itself is killed or replaced (container restart,
redeploy, OOM). When that happens, the job's last-written Redis document is
left exactly as it was, typically `"status": "running"`, forever (or until
its TTL expires and the record silently disappears with no trace it ever
existed). There is no reconciliation step anywhere in the codebase that
notices "a job is marked running but nothing is actually running it." Job
history is also inherently ephemeral: once the TTL lapses, there is no
durable trace an add-venue run ever happened, what it did, or why it
failed — a real gap for a system whose stated architecture (`CLAUDE.md`)
makes RDS the system of record and treats Redis as a cache/projection
layer, not a store of record.

## Desired Behavior
Both job services persist their job documents to a new RDS table
(`admin.venue_add_job_run`), synchronously written off the event loop via
the same `run_in_executor` pattern `rebuild_redis_offloop`/
`rehydrate_mirror` already use elsewhere in this codebase. Redis is no
longer used for job state at all (see Data/Config/API Impact — the polling
API response shapes are unchanged, only what backs them). Job rows are
never deleted by a TTL; they accumulate as a durable, queryable audit
trail. On every cs-server boot, `startup_essential()` runs one additional
off-loop step that reconciles any row still marked `"running"` from a
previous process's lifetime to a new terminal status, `"interrupted"`, with
`stopped_reason` set to a fixed, clear explanation and `finished_at` set to
the reconciliation time — deterministically, because in this single-process
deployment (`job_lock.py`'s own documented assumption: one event loop, no
worker fan-out) any row still `"running"` at boot time cannot belong to the
process now starting. No job is ever resumed or re-run automatically — the
reconciliation only marks the row as interrupted, exactly matching the "no
pipeline runs on startup" policy for spend-worthy work.

## Incident Remediation
Independent of the code changes below, and not blocked on them: the specific
Redis record left by the 2026-08-14 incident
(`admin:batch_add_job:2d07cf4450ea466ea6a7b8b95fc11211`, stuck at
`"status": "running", "processed": 26` after an unrelated deploy recycled
the container mid-run) is corrected directly in prod Redis, once, by hand —
setting `status` to `"interrupted"`, `stopped_reason` to a factual note that
the process restarted mid-run and the remaining rows were completed via the
manual follow-up job `f9d717036dae4cb590bb52ebfdcf5f56`, and `finished_at`
to the correction time. This is a targeted fix for one known-bad record so
it does not read as permanently "running" for the remainder of its 7-day
TTL — not a general-purpose backfill tool, and not a precedent for
recovering any other historical Redis job record.

## Implementation Approach
- **Migration** (`migrations/versions/00NN_venue_add_job_run.py`): `CREATE
  TABLE IF NOT EXISTS admin.venue_add_job_run` in the raw-SQL
  `op.execute(UPGRADE)` / `op.execute(DOWNGRADE)` style
  `0030_crawl_target.py` uses. Columns: `job_id text PRIMARY KEY` (reuses
  the existing `uuid.uuid4().hex` ids unchanged — no format migration
  needed on the producing side), `job_type text NOT NULL CHECK (job_type IN
  ('single','batch'))`, `label text` (batch's campaign label; null for
  single), `status text NOT NULL` (`running`/`done`/`stopped`/`failed`/
  `interrupted`), `total integer NOT NULL DEFAULT 1`, `processed integer
  NOT NULL DEFAULT 0`, `venue_name text`, `venue_address text` (single-add's
  top-level identity fields, used by `RecentAddJobs.tsx`; null for batch),
  `http_status integer`, `result jsonb` (single-add's one-shot outcome
  body), `error text` (single-add's crash message), `resolve_coords
  boolean` (batch-only), `summary jsonb NOT NULL DEFAULT '{}'::jsonb`,
  `results jsonb NOT NULL DEFAULT '[]'::jsonb` (batch's per-row array),
  `stopped_reason text`, `budget_before jsonb`, `budget_after jsonb`,
  `started_at timestamptz NOT NULL DEFAULT now()`, `finished_at
  timestamptz`, `created_at timestamptz NOT NULL DEFAULT now()`,
  `updated_at timestamptz NOT NULL DEFAULT now()`. Index on `status` (the
  boot-time reconciliation's `WHERE status = 'running'` scan) and on
  `started_at DESC` (the `list_recent` query).
- **New shared DAO** `app/dao/venue_add_job_store.py` (mirrors
  `rds_venue_store.py`'s conventions: `create_engine`, `with
  self.engine.begin()/.connect()`): `save(job: dict) -> None` (`INSERT ...
  ON CONFLICT (job_id) DO UPDATE`, an upsert so both the initial insert and
  every subsequent progress write go through one method), `get(job_id:
  str) -> Optional[dict]`, `list_recent(limit: int, job_type:
  Optional[str] = None) -> list[dict]` (replaces the Redis
  `admin:add_venue_job_recent_v1` LIST entirely — `ORDER BY started_at DESC
  LIMIT :limit` makes the separate capped-list index structure
  unnecessary), `reconcile_orphaned(reason: str) -> int` (the boot-time
  sweep; returns the row count fixed, for a startup log line). An
  in-memory fake (`InMemoryVenueAddJobStore`, matching the existing
  `InMemoryRdsVenueStore` fake pattern) backs BDD/unit coverage without a
  live Postgres, per this repo's established testing convention for
  RDS-backed stores.
- **`BatchAddService`** (`app/services/batch_add_service.py`): constructor
  takes `job_store` instead of `redis_client`; `_save()` becomes `await
  loop.run_in_executor(None, self.job_store.save, job)`; `get_job()`
  becomes the equivalent off-loop `self.job_store.get(job_id)` (the poll
  route is already async, so this is a straightforward swap, not a new
  pattern). `_on_done`'s in-process crash handling is unchanged — it is
  still the fast path when the process survives.
- **`AddVenueJobService`** (`app/services/add_venue_job_service.py`): same
  `job_store` swap. `list_recent()` becomes `job_store.list_recent(limit,
  job_type="single")` — the `_push_recent`/`RECENT_JOBS_KEY` machinery is
  deleted entirely, since `ORDER BY started_at DESC` on the durable table
  replaces it outright.
- **`app/container.py`**: wire one `VenueAddJobStore` instance (constructed
  once from `settings.rds_sqlalchemy_url`, alongside `self.rds_store`),
  pass it into both `BatchAddService` and `AddVenueJobService` in place of
  the raw Redis client each currently receives.
- **`main.py`**: add one call inside `startup_essential()`, immediately
  after container init and following the exact `run_in_executor` shape
  `rehydrate_mirror` already uses: `fixed = await
  loop.run_in_executor(None, container.venue_add_job_store.reconcile_orphaned,
  "process restarted while job was running")`, then `logger.info(f"[Main]
  Reconciled {fixed} orphaned venue-add job(s) from a previous process")`.
  Runs before the server starts accepting requests, so no window exists
  where a poller can read a stale "running" row post-restart.

## Data, Config, And API Impact
- New table `admin.venue_add_job_run` (migration above). No changes to any
  existing table.
- Redis keys `admin:batch_add_job:{job_id}`, `admin:add_venue_job:{job_id}`,
  `admin:add_venue_job_recent_v1` are retired — nothing writes them after
  this ships. Any record already sitting in Redis at deploy time simply
  ages out via its existing TTL and is not migrated (Non-goals).
- `settings.rds_sqlalchemy_url` (already used by `RdsVenueStore`) is reused
  for the new store — no new config/secret.
- **No response-shape change** on any of the five job-related HTTP
  endpoints (`POST`/`GET /venues/add-job...`, `POST`/`GET
  /venues/batch-add...`) — vibes_bot's admin-ui and the batch-add-venues
  skill's polling contract are unaffected. This is verified by the BDD
  scenarios below re-running unmodified against the new storage backend.
- `job_type` is a new internal field on the stored row (`single`/`batch`);
  it is not surfaced in any API response, purely a storage-layer
  discriminator for the shared table.

## Error Handling And Observability
- A `job_store.save()` failure (RDS unreachable mid-job) logs and continues
  today's existing pattern (best-effort, matches the current Redis
  `_save`'s `except Exception: logger.error(...)` — a persistence hiccup
  must not crash the run itself), but is now a materially bigger deal than
  a missed Redis write since RDS is the only copy; add a dedicated
  Prometheus counter (`venue_add_job_persist_failures_total`, labeled
  `job_type`) so a sustained RDS outage during a batch run is visible
  rather than silently degrading to "job ran, nothing got recorded."
- The boot-time `reconcile_orphaned` call logs its fixed-row count at
  `INFO` (0 is the expected/healthy case on a clean restart with no jobs in
  flight) and must never raise past `startup_essential` — a reconciliation
  failure degrades to "some old rows stay stuck a bit longer," not "the
  server fails to boot." Wrap in the same defensive pattern
  `rehydrate_mirror`'s caller already uses.
- No new metrics needed for the steady-state save/get path beyond the one
  counter above — `ADMIN_CS_PROXY_TOTAL` and the existing add-venue
  metrics on the vibes_bot side are unaffected since the API contract
  doesn't change.

## Test Plan
Feature file: `tests/bdd/persistence/venue-add-job-rds-tracking.feature`

Scenarios:
- A single-add job's progress and terminal result are readable via `GET
  /venues/add-job/{id}` when backed by the RDS store (unchanged response
  shape).
- A batch-add job's per-row results and summary are readable via `GET
  /venues/batch-add/{id}` when backed by the RDS store (unchanged response
  shape).
- `list_recent` returns jobs newest-first from the RDS store with no
  separate index structure.
- A job row left `"running"` by a prior process is reconciled to
  `"interrupted"` on the next startup, with `stopped_reason` set and
  `finished_at` populated, and does not block a poller from getting a
  terminal response.
- A startup with zero `"running"` rows reconciles zero rows and logs that
  cleanly (the common case).
- An in-process crash during `_run_job` (process survives) still reaches
  `"failed"` via the existing in-process handler, unchanged from today.

Pytest unit tests:
- `VenueAddJobStore.save`/`get`/`list_recent`/`reconcile_orphaned` against
  the in-memory fake: upsert semantics (a second `save` for the same
  `job_id` updates, not duplicates), `list_recent` ordering and `job_type`
  filtering, `reconcile_orphaned` only touches `status='running'` rows and
  leaves `done`/`stopped`/`failed`/`interrupted` rows untouched.
- `BatchAddService`/`AddVenueJobService` updated to construct with the fake
  store in place of a fake Redis client (existing test files already do
  this for Redis — same shape, new fake).

Manual or integration checks:
- After deploy, confirm `admin.venue_add_job_run` exists via the RDS
  post-provisioning smoke-test pattern this repo already uses for new
  tables (per `rds_venue_store.py`'s own docstring: no local Postgres in
  CI, so real-Postgres SQL is validated there, not in the offline suite).
- Trigger a real single-add and a real batch-add against a
  staging/prod-like environment, restart the container mid-batch, and
  confirm the row reconciles to `"interrupted"` on the next boot rather
  than staying `"running"`.

## Acceptance Criteria
- No venue-add job record can be left showing `"status": "running"` once
  its owning process is gone — the next boot deterministically reconciles
  it.
- Job history survives indefinitely (no TTL), queryable directly in RDS
  for audit purposes.
- `POST`/`GET /venues/add-job...` and `POST`/`GET /venues/batch-add...`
  response shapes are byte-for-byte unchanged from today.
- Redis is no longer written for venue-add job state.

## Open Questions
None. The storage-layer swap (RDS-only, no Redis dual-write) is a direct
application of this repo's own stated architecture (RDS = system of
record, `CLAUDE.md`) and the user's explicit framing — writing job state
directly to Redis was the anti-pattern to correct, not a design choice
still open for debate.
