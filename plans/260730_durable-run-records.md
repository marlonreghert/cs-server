# Archive Run Records Must Survive A Restart

## Branch
feature/durable-run-records

## Goal
An archive run's record — its config, its outcome counts, and its status — must
still be readable after cs-server restarts, so an operator can answer "what did
that run actually do, and what settings produced it?" without the process that
ran it still being alive.

## Non-goals
- Streaming live progress of an in-flight run (the existing summary already
  covers this while the process lives).
- Persisting per-venue detail. The aggregate summary plus the S3 partition is
  enough to reconstruct a run; a row per venue is a different, much larger thing.
- Retention/archival policy beyond a simple bounded window.
- The deploy-drain problem — see `260730_archive-deploy-drain.md`. That plan
  depends on this one for its "unfinished run" record.

## Evidence

**Run records are in-memory only.** `_save_run_record`
(`app/services/venue_photo_archive_service.py:701`) is:

```
def _save_run_record(self, job_id: str, record: dict) -> None:
    try:
        self._records[job_id] = record
```

`self._records` is a plain dict on the service instance. Every restart discards
every past run.

**This has already cost a real investigation.** While diagnosing why a 77-venue
run archived 42, the run's own config was unrecoverable — the container had been
recreated by a deploy — so the settings had to be inferred from S3 key shapes and
Prometheus counters instead of simply being read. The same restart also reset the
Prometheus counters (they are per-process), so both records of the run were gone
at once.

**The `pipeline_run_registry` is not a substitute.** It is also in-memory
(`app/services/pipeline_run_registry.py:106`, `self._runs: dict[str, list[list]]`),
a bounded ring whose purpose is to publish `pipeline_run_info` for the Grafana run
picker. It holds ids and statuses, not configs or outcomes, and dies with the
process too.

**The cancellation path already assumes durability it does not have.**
`venue_photo_archive_service.py:833` catches `CancelledError`, sets
`summary["aborted"] = True`, and calls `_save_run_record` specifically so a
cancelled run "is still accounted for rather than vanishing — it already spent
money on the venues it finished". That intent is correct and currently
unrealised: the record vanishes with the process, and a cancelled run is exactly
the case where the process is most likely to be going away.

**RDS is already the system of record** with Alembic in place, so this is a new
table rather than new infrastructure.

## Current Behavior
1. A run's summary is written to `self._records[job_id]` and served from there by
   `GET /admin/jobs/runs/{job_id}`.
2. A restart — including every deploy — empties it.
3. An operator asking what a past run did gets nothing, and must reconstruct it
   from S3 layout and Prometheus counters, both of which are lossy.

## Desired Behavior
1. A run record must be persisted to RDS when the run finishes, is cancelled, or
   is stopped, and must survive a restart.
2. The record must carry at least: job id, run id, source, status, the resolved
   config, the outcome counts, start/end time, and the S3 prefix written to.
3. `GET /admin/jobs/runs/{job_id}` must serve the persisted record, so an
   operator sees the same answer before and after a restart.
4. Recent runs must be listable, so an operator can find a run without already
   knowing its job id.
5. Persistence must never fail a run: a write error is logged and the run still
   returns its summary, matching the current `_save_run_record` posture ("a lost
   record must never fail a run whose photos are already stored").
6. Records must be bounded — old rows pruned or aged out — so the table cannot
   grow without limit.

## Implementation Approach
- **New RDS table + Alembic migration** for archive run records, keyed by job id,
  with the config and counts stored as JSON so a new summary field does not
  require a migration.
- **A DAO** owning the table, per the repository's DAO boundary rule; the service
  must not bind to SQL directly.
- **`_save_run_record` becomes write-through**: persist, and keep the in-memory
  dict as a read cache for the live run. Its existing swallow-and-log behavior is
  the required posture, not a compromise.
- **`GET /admin/jobs/runs/{job_id}`** reads through to the DAO on a miss, plus a
  list endpoint for recent runs.
- Writes happen at the same three points that already call `_save_run_record`, so
  no new lifecycle is introduced.

## Data, Config, And API Impact
- **Persistence:** one new table + migration. No existing schema touched.
- **API:** `GET /admin/jobs/runs/{job_id}` starts returning records it previously
  lost; one new list endpoint. Additive.
- **Config:** a retention bound (row count or age).
- **Metrics:** none required; a counter for persistence failures is worthwhile
  since the write is deliberately non-fatal and would otherwise be silent.

## Error Handling And Observability
- A persistence failure must log with the job id and increment a failure counter,
  never propagate. A silent swallow would recreate the current problem in a new
  place.
- Reads must degrade to the in-memory record if the DAO is unavailable.

## Test Plan
Feature file: `tests/bdd/observability/durable-run-records.feature`

Scenarios:
- A completed run's record is readable after the service is rebuilt.
- A cancelled run's record is readable after the service is rebuilt, and reports
  `aborted`.
- A run stopped by credit exhaustion persists with status `error`.
- The record carries the resolved config that produced the run.
- Recent runs are listable newest-first without knowing a job id.
- A persistence failure does not fail the run: photos stay archived and the
  summary is still returned.
- Records are bounded: beyond the retention limit the oldest are dropped.

Pytest unit tests:
- DAO round-trip including JSON config fidelity; retention pruning; the
  write-through path when the DAO raises.

Manual or integration checks:
- Run the migration against a scratch DB; confirm a record written before a
  container restart is readable after it.

## Acceptance Criteria
- A run record survives a restart and is served by the admin endpoint.
- Cancelled and credit-exhausted runs persist with the right status.
- A DAO failure never fails a run.
- Records are bounded by the configured retention.
- Migration applies and rolls back cleanly.

## Open Questions
- Retention bound: row count or age? Runs are operator-triggered and rare (no
  cron), so even a generous bound is small — suggest a row count, decided at
  implementation.
