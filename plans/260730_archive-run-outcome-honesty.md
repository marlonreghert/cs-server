# Archive Run Outcomes Must Describe What Actually Happened

## Branch
fix/archive-run-outcome-honesty

## Goal
An archive run's reported outcome must match its real result. A run that lost
most of its venues must not be recorded as a clean success, a run that was
stopped must be recorded as an error, and the two very different reasons
currently sharing the `no_match` label must be told apart.

## Non-goals
- **Persisting run records** (`self._records` is in-memory, so a restart discards
  every past run's config). Needs RDS schema + Alembic; its own plan.
- **Deploy drain** — a deploy still destroys an in-flight run. Spans vibes_bot's
  workflow and cs-server shutdown; its own plan.
- Any change to fetch, retry, poll-budget, or S3 layout behavior.
- The Grafana panels for these series (vibes_bot repo, shipped alongside).

## Evidence

**`media_archive_runs_total` cannot express failure.** It is declared
`["source", "status"]` with `# status: success, error` (`app/metrics.py:451`), but
there is exactly **one** call site — `app/services/venue_photo_archive_service.py:848`
— and it is hardcoded:

```
MEDIA_ARCHIVE_RUNS_TOTAL.labels(source=source, status="success").inc()
MEDIA_ARCHIVE_LAST_SUCCESS_TIMESTAMP.set_to_current_time()
```

`status="error"` is dead. Every run that reaches the end of `run()` is a success
regardless of what happened inside, and `LAST_SUCCESS_TIMESTAMP` — the freshness
signal an alert would watch — moves with it.

**Observed 2026-07-30.** An 8-venue run archived **1** venue and timed out on 7.
It recorded `media_archive_runs_total{status="success"} = 1` and bumped
`LAST_SUCCESS_TIMESTAMP`. A second run archived 0 of 1 and did the same. Nothing
in the run-level metrics distinguishes either from a perfect run; only the
per-venue `media_archive_venues_total` breakdown reveals it, and no alert reads
that.

**`no_match` means two unrelated things** (`venue_photo_archive_service.py:990`
and `:1005`):
- **:990** — the venue has no `search_query`, so it could never be addressed. A
  catalog-data problem. Costs nothing; re-running will never fix it.
- **:1005** — the source was called, was paid, and returned nothing. A
  source/coverage problem. Worth re-running.

Same label, opposite operator responses, and the expensive one is invisible
inside the cheap one. This is the same defect class as #123, where a timeout was
also filed as `no_match` — the label absorbs anything that isn't a hard failure.

**A run stopped by credit exhaustion still reports success.** `_guarded` records
`summary["credit_exhausted"] = True` and lets the run finish reporting
(`venue_photo_archive_service.py:~812`), which is right — but it then flows into
the same unconditional success counter. #123 already stopped such a run from
writing `_latest.json`; the run counter was left alone and is now inconsistent
with that.

## Current Behavior
1. Any run reaching the end of `run()` increments
   `media_archive_runs_total{status="success"}` and moves
   `MEDIA_ARCHIVE_LAST_SUCCESS_TIMESTAMP`, regardless of per-venue outcomes.
2. `status="error"` is never emitted by any path.
3. A venue with no search query and a venue the source could not find both
   increment `media_archive_venues_total{result="no_match"}` and the same
   `summary["no_match"]` counter.

## Desired Behavior
1. A run must report a status reflecting its outcome:
   - `success` — the run completed and every selected venue reached a terminal
     good state (archived, info-only, or deliberately skipped).
   - `partial` — the run completed but at least one venue failed, timed out, or
     produced no result.
   - `error` — the run did not complete: stopped by credit exhaustion, aborted,
     or ended by an exception.
2. `MEDIA_ARCHIVE_LAST_SUCCESS_TIMESTAMP` must advance **only** on `success`, so
   it remains usable as a freshness signal.
3. A venue with no addressable query must be reported `no_query`; a venue the
   source called and could not find must be reported `no_result`. `no_match` must
   no longer be emitted.
4. The run summary must carry the same split (`no_query`, `no_result`) and the
   run status, so the admin JSON tells the operator what the metrics do.
5. Per-venue outcome accounting must stay exhaustive: every selected venue lands
   in exactly one bucket, and the buckets must sum to the number considered.

## Implementation Approach

- **`app/services/venue_photo_archive_service.py`** — derive the run status from
  the summary once, at the end of `run()`, rather than asserting it. A single
  helper reading `summary` keeps the rule in one place and makes the
  "buckets must be exhaustive" property testable. Guard
  `LAST_SUCCESS_TIMESTAMP` behind `success`. Split the two `no_match` sites into
  `no_query` (:990) and `no_result` (:1005), and initialise both in the summary
  dict alongside the existing counters.
- **`app/metrics.py`** — widen the documented `status` values to
  `success|partial|error` and correct the `result` comment, which is stale again
  after #123.
- The status derivation must not itself raise: a metric must never fail a run
  whose photos are already stored, matching the existing `_save_run_record`
  posture.

## Data, Config, And API Impact
- **Metrics (breaking for queries, not for collection):** `result="no_match"` is
  replaced by `no_query`/`no_result`; `media_archive_runs_total` gains `partial`.
  Any dashboard panel or alert filtering `no_match` returns empty after this —
  the vibes_bot dashboard is updated in the same change set. No metric is renamed
  or removed.
- **Run summary / admin JSON:** `no_match` key replaced by `no_query` and
  `no_result`; new `status` key. Consumers reading `no_match` must be updated —
  grep shows the admin panel renders the summary generically, so this is
  additive in practice.
- **Config / persistence / HTTP API:** none.

## Error Handling And Observability
- The run-status derivation is pure and total: unknown or unexpected summary
  shapes must resolve to `error` rather than raising, because an unclassifiable
  run is exactly the case an operator needs to see.
- The end-of-run log line must state the status and the new buckets so the logs
  and the metrics agree.
- No new external calls, so no new failure modes.

## Test Plan
Feature file: `tests/bdd/observability/archive-run-outcome-honesty.feature`

Placed in `observability/` — unlike #123 this changes no fetch or storage
behavior; the deliverable is that metrics, summary, and logs describe the run
truthfully.

Scenarios:
- A run where every venue is archived reports `success` and advances the
  last-success timestamp.
- A run where some venues time out reports `partial` and must NOT advance the
  last-success timestamp.
- A run where every venue fails reports `partial`, not `success`.
- A run stopped by credit exhaustion reports `error`.
- A venue with no search query is reported `no_query`, not `no_match`.
- A venue the source cannot find is reported `no_result`, not `no_match`.
- `no_match` is never emitted by any path.
- The per-venue buckets sum to the number of venues considered.
- A run that archives nothing because everything was skipped as already-archived
  still reports `success` (a no-op run is not a failure).

Pytest unit tests:
- `tests/test_archive_run_status.py` — the status derivation across summary
  shapes, including the total/never-raises property and the exhaustiveness of the
  bucket sum.

Manual or integration checks:
- After deploy, confirm `media_archive_runs_total{status="partial"}` appears for
  the next imperfect run and that `no_query`/`no_result` replace `no_match`.

## Acceptance Criteria
- A run with any failed, timed-out, or empty venue reports `partial`; a stopped
  run reports `error`; only a clean run reports `success`.
- `MEDIA_ARCHIVE_LAST_SUCCESS_TIMESTAMP` advances only on `success`.
- `no_match` is emitted nowhere; `no_query` and `no_result` carry its two
  meanings.
- The summary exposes `status`, `no_query`, and `no_result`.
- Per-venue buckets sum to venues considered.
- Full BDD and unit suites pass; no new lint or type errors versus `main`.

## Open Questions
None.
