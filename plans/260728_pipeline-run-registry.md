# Pipeline Run Registry

## Branch
feature/pipeline-run-registry

## Goal
Give every pipeline run an identity that an operator can select, so a dashboard
can answer "show me that run" instead of "show me this pipeline in aggregate".

Concretely: each scheduled and admin-triggered job mints a time-ordered run id,
logs it in the `job=<id>` shape the photo archive already uses, and registers
itself in a **bounded** Prometheus registry that Grafana can turn into a
dropdown.

## Non-goals
- **Dashboards.** They are vibes_bot's plan
  (`plans/260728_pipeline-dashboards.md`); this repo only publishes the metric
  and the log shape they consume.
- **Durable run history.** The registry keeps the last N runs per pipeline on
  purpose. History that must survive beyond that belongs in Postgres — the photo
  archive's existing run-record store is the seed — not in more metric labels.
- **Per-run business metrics.** Only run identity is labelled by `job_id`;
  outcome counters stay unlabelled by run, which is what keeps cardinality flat.
- **Reworking the photo archive.** It already does all of this correctly; it
  becomes the reference implementation and adopts the shared helper.

## Evidence
- **`job_id` cannot be a Loki label here.** Loki is `grafana/loki:2.9.8` on
  `schema: v11` (`vibes_bot/docker-compose.yml:281`, `config/loki/*.yml`).
  Structured metadata — the feature built for exactly this kind of
  high-cardinality field — needs Loki 3.x on schema v13. A `job_id` label would
  therefore create **one stream per run**; `redis_projection` alone runs every
  2 minutes (`redis_projection_minutes: int = 2`), i.e. ~720 new streams a day,
  indefinitely.
- **Loki does not need it.** The photo archive dashboard already filters runs
  with a plain substring match — `{service="cs-server"} |= "job=$job_id"` — and
  works. Selection is the missing half, not filtering.
- **Prometheus cardinality can be bounded by construction.** `prometheus_client`
  exposes `.remove(*labelvalues)`; verified live that removing one label set
  drops that series. So a ring of the last N runs per pipeline has a hard
  ceiling rather than a growth curve.
- **The insertion points already exist.** `make_job()` (`main.py:69`) wraps every
  scheduled job and *already mints* `uuid.uuid4().hex` per run for the data lake
  context (`main.py:121`). `_run_job()`
  (`app/routers/admin_trigger_router.py`) wraps every admin trigger. Two places
  cover every pipeline; no per-service edits.
- **The id convention exists.** `new_run_id()`
  (`app/services/venue_photo_archive_service.py:168`) mints a **ULID** —
  48-bit millisecond timestamp then 80 bits of randomness — deliberately
  time-ordered so lexicographic sort is chronological. Generalise it rather than
  introducing a second scheme.
- Only `venue_photo_archive_service` (36 refs) and `batch_add_service` carry a
  run id today. Instagram (old and cascade), Google Places, menu extraction and
  vibe classifier emit none, so there is currently nothing for a dashboard to
  key on.

## Current Behavior
A run is anonymous. Logs from concurrent or successive runs of the same pipeline
interleave with nothing distinguishing them, and metrics are aggregate-only. An
operator watching a triggered enrichment can see *that* the job is running (the
admin card polls `running`) but cannot scope anything to *this* run. The photo
archive is the sole exception, and its dashboard's `$job_id` is a **textbox** —
you must already know the id to type it.

## Desired Behavior
1. Every scheduled and admin-triggered pipeline run mints a **ULID** run id.
2. Every log line a pipeline emits during that run carries `job=<id>`, matching
   the shape the photo archive dashboard already greps for.
3. The run is published as `pipeline_run_info{pipeline, job_id, status}` whose
   **value is the run's start time** (unix seconds), so `sort_desc` orders runs
   newest-first without parsing anything.
4. `status` moves `running` → `success` | `error` as the run ends.
5. Only the **last N runs per pipeline** remain registered; older label sets are
   removed, so the series count has a fixed ceiling.
6. A pipeline that raises still finishes registered as `error` — a crashed run
   must not vanish from the dropdown.

## Implementation Approach

### A. Shared run context
A small module owning the id, the contextvar, and the registry:

- `new_run_id()` moves here (ULID, unchanged semantics); the photo archive
  imports it from the new home so there is one implementation.
- `current_run()` exposes `(pipeline, job_id)` for anything that wants to stamp
  its own output — the data lake's existing job context folds into this rather
  than staying a parallel mechanism.
- `run_scope(pipeline)` — a context manager that mints the id, registers
  `running`, yields, and registers the terminal status on the way out including
  on exception.

### B. Two insertion points
- `make_job()` in `main.py`: wrap the existing body in `run_scope(job_name)`.
  It already mints a uuid here for the data lake; that call is **replaced**, not
  duplicated, so a run has exactly one identity.
- `_run_job()` in `admin_trigger_router.py`: the same wrap, so an admin trigger
  and its scheduled twin are indistinguishable to a dashboard.

### C. Log correlation
A logging filter injects `job=<id>` into records emitted inside a run scope, so
existing pipelines gain correlation **without touching their log statements**.
Pipelines that already write `job=<id>` themselves (the photo archive) must not
end up with it twice — the filter only adds it when absent.

### D. Bounded registry
The ring is the whole cardinality argument, so it is explicit:

```
pipeline_run_info{pipeline, job_id, status}   value = start unix seconds
```

Per pipeline, keep at most `pipeline_run_registry_size` (default 10) entries;
on overflow `.remove()` the oldest by start time. Ceiling ≈ pipelines × N ≈ 80
series, flat. When a run's status changes the old `(pipeline, job_id, status)`
label set is removed and re-registered, so a run never appears twice under two
statuses.

### E. Concurrency
Registration is guarded so two runs finishing at once cannot corrupt the ring,
and the whole registry is best-effort: a registry failure logs and continues.
**A bookkeeping bug must never fail a pipeline.**

## Data, Config, And API Impact
- **API:** none. **Persistence:** none — no RDS, no Redis, no migration.
- **New settings:** `pipeline_run_registry_enabled` (bool, true),
  `pipeline_run_registry_size` (int, 10).
- **Cross-repo contract** (vibes_bot's dashboards depend on exactly this):

| Thing | Shape |
|---|---|
| Metric | `pipeline_run_info{pipeline, job_id, status}` |
| Value | run start, unix seconds |
| `status` | `running` \| `success` \| `error` |
| `pipeline` | the existing `job_name` label values, unchanged |
| Log correlation | `job=<ulid>` present in every line of the run |
| Dropdown query | `label_values(pipeline_run_info{pipeline="$pipeline"}, job_id)` |
| Recent runs | `sort_desc(pipeline_run_info{pipeline="$pipeline"})` |

## Error Handling And Observability
The registry is instrumentation; it may never change pipeline behavior.

| Failure | Behavior |
|---|---|
| Registry raises while registering | logged, run continues |
| Ring eviction fails | logged, run continues |
| Pipeline raises | status recorded `error`, exception re-raised unchanged |
| Registry disabled by setting | ids and log correlation still work; no metric |

`pipeline_run_info` is the new metric. The existing `background_job_*` metrics
are untouched — this adds run identity beside them, it does not replace
aggregate job telemetry.

## Test Plan
Feature file: `tests/bdd/observability/pipeline-run-registry.feature`

Scenarios:
- Register a scheduled run as running, then success.
- Register an admin-triggered run identically to a scheduled one.
- Record a failed run as error and still re-raise to the caller.
- Keep only the most recent runs per pipeline.
- Never let one pipeline's runs evict another's.
- Report a run's start time so runs order newest-first.
- Show a run under exactly one status at a time.
- Stamp every log line emitted during a run with its id.
- Leave a pipeline that already stamps its own id unchanged.
- Continue the run when the registry itself fails.
- Keep run ids time-ordered so a lexicographic sort is chronological.

Pytest unit tests:
- `tests/test_pipeline_run_registry.py` — ULID monotonicity across rapid calls;
  ring eviction by start time; per-pipeline isolation; status transition removes
  the prior label set (no duplicate run rows); **series count stays at the
  ceiling after many runs** — the cardinality guarantee, asserted not assumed;
  registry exceptions swallowed; disabled mode still yields ids.
- `tests/test_pipeline_run_logging.py` — the filter injects `job=<id>` inside a
  scope, adds nothing outside one, and does not double-stamp a line that already
  carries it.

Manual or integration checks:
- Trigger a job from vibesadmin; confirm `pipeline_run_info` appears with
  `status="running"` then flips to `success`, and that
  `{service="cs-server"} |= "job=<id>"` returns that run's lines in Loki.
- Confirm the series count for `pipeline_run_info` stops growing after more than
  N runs of one pipeline.

## Acceptance Criteria
- Every scheduled and admin-triggered pipeline publishes
  `pipeline_run_info` with a ULID `job_id` and a start-time value.
- `label_values(pipeline_run_info{pipeline="X"}, job_id)` returns recent runs of
  X — the dropdown vibes_bot needs.
- Series count is bounded: more than N runs of a pipeline leaves exactly N.
- A failed run is registered `error` and the exception still propagates.
- Log lines inside a run carry `job=<id>`; the photo archive's lines are not
  double-stamped.
- Disabling the registry does not break any pipeline.
- `make test-bdd` and `make test-unit` pass; the `@wip` tag is removed.

## Open Questions
- None. The Loki-label option is rejected on evidence (2.9.8 / schema v11,
  ~720 streams/day), the bounded-Prometheus design is verified, and both
  insertion points already exist.
