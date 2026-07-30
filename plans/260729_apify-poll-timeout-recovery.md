# Apify Archive Poll Timeouts: Recover Them, And Stop Calling Them `no_match`

## Branch
fix/apify-poll-timeout-recovery

## Goal
An Apify archive fetch whose actor run has not finished inside the poll budget
must be **recovered by continuing to poll the run already paid for**, and when it
still cannot be recovered it must be reported as a `timeout` — not as
`no_match`, which claims the venue does not exist on Google Maps.

The run's last observed non-terminal Apify status (`READY` vs `RUNNING`) must be
logged and exposed as a metric label, because that single field decides whether
the remaining fix is a larger poll budget or lower concurrency, and today the
code discards it.

## Non-goals
- **Raising `MAX_POLL_ATTEMPTS` or lowering `media_archive_concurrency` (4).**
  Both are sizing decisions and neither can be sized until the `READY`/`RUNNING`
  split from this change exists. An 8-venue probe from `rerun_missing_29.txt`
  decides it afterwards: if the timeout rate collapses it was concurrency
  contention, if it holds near 39% it is per-venue slowness.
- **Starting a replacement actor run on timeout.** See Implementation Approach —
  this double-bills and is the reason the retry is a resumed poll instead.
- **Re-running the 35 missing venues.** Operational follow-up, not this change.
- **The Grafana panel for `apify_api_errors_total`.** Lives in vibes_bot
  (`config/grafana/provisioning/dashboards/pipelines/`), so it is a separate
  repo and sequenced as a follow-up after this merges.
- Any change to the `retrieved/` S3 layout, run-id encoding, or skip semantics.

## Evidence

Production Prometheus (`prometheus.apivibesensemiddleware.click`), for the
77-venue run — job `8cfd4ef90825409bb0c537360664f3c8`, run
`01KYQPQPE9QX3ZN9XKW38R1HTE`, which archived 42 of 77:

| series | value |
|---|---|
| `apify_api_errors_total{endpoint="gmaps_archive_photos", error_type="timeout"}` | **35** |
| `media_archive_venues_total{source="apify_gmaps_extractor", result="archived"}` | 54 |
| `media_archive_venues_total{source="apify_gmaps_extractor", result="no_match"}` | **35** |
| `media_archive_throttled_total` | **0** |
| `apify_api_call_duration_seconds_count{endpoint="gmaps_archive_photos"}` | 54 |

Every failure is a poll timeout, and all 35 are reported as `no_match`.

**The timeout counter proves the runs were still alive.**
`app/api/apify_gmaps_extractor_client.py:462` returns immediately when the status
is `SUCCEEDED`/`FAILED`/`ABORTED`/`TIMED-OUT`, so a run that died on Apify's side
never reaches the `error_type="timeout"` increment at line 474 — that only fires
after all 60 attempts are exhausted. 35 timeouts therefore means 35 runs were
**still non-terminal (`READY` or `RUNNING`) at the 300s mark.** They did not
crash, and Apify did not reject them. The status that would say which is read
into a local at line 460 and then discarded.

**The budget is 300s and the success distribution sits against it.**
`MAX_POLL_ATTEMPTS = 60` x `POLL_INTERVAL_SECONDS = 5.0`
(`apify_gmaps_extractor_client.py:43-44`). Duration histogram, successes only:
`le=30` 7, `le=60` 18, `le=120` 33, `le=300` 54, mean **117.7s**. 21 of 54
successes landed in the 120-300s band — the tail is already against the ceiling,
which is why a large fraction crosses it.

**Three different outcomes collapse into one bare `None`.**
`fetch_venue_photos` returns `None` for a non-`SUCCEEDED` run (line ~957), for an
empty dataset, and for a start-run failure. At
`app/services/venue_photo_archive_service.py:975` the service does
`if not result: summary["no_match"] += 1`. The service cannot distinguish a
timeout from a genuine no-match **even in principle** — the information is gone
before it is asked for. (`app/metrics.py:431` documents the `result` label as
`archived, skipped_existing, no_place_id, google_error, failed` and does not even
list `no_match`; the comment is already stale.)

**A timeout is retried zero times.** `_fetch_photos`
(`venue_photo_archive_service.py:~910-936`) retries only when `is_throttled(e)`
is true for a raised exception. A timeout raises nothing — it returns `None` —
so it bypasses the ladder entirely and the venue is dropped permanently.
`media_archive_throttled_total = 0` confirms the ladder never fired once.

**A simulation against the real client** (virtual clock, real `_poll_run` and
`fetch_venue_photos`) reproduces the cliff exactly and emits the same prod log
line (`Run … timed out after 300.0s`): a run finishing at 295s succeeds after 59
polls; at 305s it is lost at poll 60; the same 360s run recovers when the budget
is larger. This confirms the mechanism, but it *assumes* the run eventually
reaches `SUCCEEDED` — which is precisely the assumption item 1 exists to test.

**Cost guarantee at risk.** `docs/venue-retrieval-storage.md` §3 states both
sources bill per venue and that orderings exist so money is never spent twice.

## Current Behavior
1. `_poll_run` polls for 300s, discards the non-terminal status it last saw,
   logs a timeout, increments `apify_api_errors_total{error_type="timeout"}`,
   and returns `"TIMED-OUT"`.
2. `fetch_venue_photos` sees a non-`SUCCEEDED` status, increments
   `apify_api_calls_total{status="error"}`, and returns `None`. The run id is
   dropped. No duration is observed.
3. `_archive_venue` receives a falsy result and records `no_match` — reporting a
   venue Apify was mid-way through scraping as one that does not exist.
4. The venue is never retried and is absent from the archive. On the 77-venue
   run this silently lost 35 venues, including `Bar do Cuscuz`, the #1
   recommended venue.

## Desired Behavior
1. `_poll_run` must retain the last non-terminal status it observed, include it
   in the timeout log line, and label the timeout metric with it, so `READY`
   (queued behind an Apify concurrency cap) is distinguishable from `RUNNING`
   (genuinely slow).
2. On exhausting the poll budget, the client must **continue polling the same
   run** for a bounded, configurable continuation window rather than abandoning
   it. If the run reaches `SUCCEEDED` during that window, its dataset must be
   fetched and the venue archived normally.
3. The client must report a timeout to its caller **distinguishably from a
   genuine no-match**, so a timeout that outlives the continuation window is
   recorded as `result="timeout"` in `media_archive_venues_total` and counted in
   its own summary bucket, while a real empty result stays `no_match`.
4. A recovered venue must be archived exactly as a first-pass success — same S3
   layout, same classification path, same `place.json`-before-photos ordering.
5. The archive run must not start a replacement actor run for a timed-out venue.

## Implementation Approach

**Continue the paid run; never buy a second one.** The natural-looking fix — let
the existing `_fetch_photos` ladder retry — calls `descriptor.fetch` again, which
calls `fetch_venue_photos`, which calls `_start_run` and **starts a brand-new
actor run.** The original run was proven alive and goes on to complete and bill,
so that approach pays Apify twice for one venue, on exactly the venues that were
about to succeed, and breaks the §3 cost guarantee. The retry is therefore a
**resumed poll on the retained run id**, not a re-fetch: poll GETs are not billed
per place, so recovery reuses a scrape already paid for and costs nothing extra.
This also needs no guess about actor duration, which is why it can ship before
the probe.

Changes, by boundary:

- **`app/api/apify_gmaps_extractor_client.py`** — `_poll_run` tracks the last
  non-terminal status and returns it alongside the terminal status so the caller
  and the metric can both see it. On budget exhaustion, `fetch_venue_photos`
  enters a bounded continuation that keeps polling the **same run id**; on
  `SUCCEEDED` it proceeds to `_fetch_dataset` down the normal path. The
  continuation must observe the duration histogram when it resolves, so recovered
  venues stop being invisible in the latency data (today the non-`SUCCEEDED`
  early return skips `observe()` — which is why the histogram had 54 observations
  against 89 calls).
- **Distinguishable timeout signal.** A bare `None` cannot carry the reason, so
  the client must signal a timeout distinctly — mirroring the existing
  `FETCH_FAILED` sentinel already used between these two layers
  (`venue_photo_archive_service.py:98`) rather than inventing a second
  convention. `ApifyCreditExhaustedError` stays exactly as it is: propagated,
  never swallowed.
- **`app/services/venue_photo_archive_service.py`** — `_archive_venue` maps the
  timeout signal to a `timeout` summary bucket and
  `MEDIA_ARCHIVE_VENUES_TOTAL{result="timeout"}`. `no_match` keeps its real
  meaning. The new bucket must be initialised in the summary dict alongside
  `no_match` (line ~755) so the run record and admin JSON always carry the key.
- **`app/metrics.py`** — add the non-terminal status label to the Apify timeout
  path and refresh the stale `result` label comment to list every value actually
  emitted, `no_match` and `timeout` included.
- **`app/config.py`** — one setting for the continuation window, defaulting
  conservatively so the change is inert until deliberately raised, plus
  `.env.example` / `config.example.json` if they enumerate archive settings.

Ordering constraints that must not move: the skip check stays before any spend;
`place.json` is still written before photos; the run-id prefix is still resolved
before fetching.

## Data, Config, And API Impact
- **Config:** one new archive setting (continuation window) with a conservative
  default. No existing default changes; `MAX_POLL_ATTEMPTS` and
  `media_archive_concurrency` are untouched.
- **Metrics:** new `result="timeout"` value on `media_archive_venues_total`; new
  status label on the Apify timeout error path. Both additive — no series is
  renamed or removed, so existing dashboards keep working. The Pipelines
  dashboard gains a panel in a follow-up vibes_bot PR.
- **Run summary / admin JSON:** a new `timeout` key. Additive; consumers that
  ignore unknown keys are unaffected.
- **Persistence:** none. No S3 layout, key-format, Redis, or schema change, so
  no migration.
- **HTTP API:** none.

## Error Handling And Observability
- A timeout must log at error level with the venue id, job id, the last
  non-terminal Apify status, and the elapsed budget — enough to troubleshoot
  without a rerun, per the background-jobs rule in `CLAUDE.md`.
- A recovered venue must log that it was recovered, and after how long, so the
  probe can measure how much continuation window was actually needed.
- The continuation must be bounded and must never block a run indefinitely: a run
  stuck in `READY` has to end as a reported `timeout`, not a hang.
- One venue's timeout must never end the run — the existing
  "one venue must not end a run" behavior is preserved.
- Duration must be observed on the recovery path so the histogram's count
  matches the call count.
- No API keys, tokens, or raw Apify payloads in any new log line.

## Test Plan
Feature file: `tests/bdd/enrichment/apify-poll-timeout-recovery.feature`

Placed in `enrichment/` alongside `venue-photo-archive.feature` and
`photo-archive-pipeline-v2.feature` because the archive pipeline is the subsystem
under change; the metric and log assertions ride along in the same scenarios.

Scenarios:
- A venue whose actor run finishes inside the poll budget is archived normally
  and reported `archived` (no regression).
- A venue whose actor run finishes during the continuation window is recovered,
  archived with the same S3 layout, and reported `archived` — not `timeout`.
- A venue whose actor run never leaves a non-terminal state is reported
  `timeout`, and **not** `no_match`.
- A venue that genuinely returns an empty dataset is still reported `no_match`,
  proving the two are no longer conflated.
- A timed-out venue must not cause a second actor run to be started (the cost
  guarantee, asserted on start-run call count).
- The timeout log line and metric must carry the last non-terminal status, and
  `READY` and `RUNNING` must be distinguishable.
- A recovered venue observes the call-duration histogram.
- A timeout on one venue must not abort the run; the remaining venues are still
  processed.
- Credit exhaustion during a continuation still aborts the whole run.

Pytest unit tests:
- `tests/test_apify_poll_timeout.py` — `_poll_run` returns the last non-terminal
  status; the continuation polls the same run id and starts no new run; the
  continuation is bounded and terminates on a permanently-`READY` run; a
  terminal `FAILED` still returns immediately without touching the timeout
  counter.
- Extend the archive-service tests — the timeout signal maps to the `timeout`
  bucket and metric label; `no_match` keeps its meaning; the summary dict always
  carries the `timeout` key.

Manual or integration checks:
- After merge, the 8-venue probe from `rerun_missing_29.txt`, reading
  `apify_api_errors_total` and the new status label to decide the sizing fix.
  Deterministic fakes only in BDD — no live Apify calls, per `CLAUDE.md`.

## Acceptance Criteria
- A run whose actor completes during the continuation window archives the venue
  and reports `archived`.
- A poll timeout is reported as `result="timeout"`; `no_match` is emitted only
  for a genuinely empty or unaddressable result.
- The timeout log line and metric carry the last non-terminal Apify status, and
  `READY` is distinguishable from `RUNNING`.
- No timed-out venue triggers a second `_start_run` for the same venue in the
  same attempt.
- `apify_api_call_duration_seconds_count` accounts for recovered calls.
- `MAX_POLL_ATTEMPTS` and `media_archive_concurrency` are unchanged.
- The new continuation setting defaults conservatively; existing behavior is
  otherwise unchanged.
- Every scenario in the feature file passes, `@wip` is removed, and
  flake8/black/mypy are clean.

## Open Questions
None.
