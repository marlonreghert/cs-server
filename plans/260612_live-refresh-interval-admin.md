# Admin-tunable live forecast refresh interval

## Branch
feature/live-refresh-interval-admin

## Goal
The `live_forecast_refresh` job's interval must be tunable at runtime through
the existing `admin_config:*` Redis path (key written by the vibesadmin
panel), applying within about one minute without a cs-server restart, with
validation and a settings fallback. This lets the operator slow the BestTime
live busyness cadence (cost/CPU relief) or speed it up, live.

## Non-goals
- Changing the default cadence (stays `settings.venues_live_refresh_minutes`
  = 5).
- Admin tunability for any other job (catalog, weekly, enrichment, projector)
  — same mechanism could be extended later, deliberately out of scope now.
- The vibesadmin HTML form field itself — that is vibes_bot work, recorded in
  vibes_bot `plans/260611_venues-warm-serving.md`; this plan only consumes
  the key.
- Changing what the refresh job does per run.

## Evidence
- `main.py:371-374` — `live_forecast_refresh` registered once at startup with
  `IntervalTrigger(minutes=settings.venues_live_refresh_minutes)`; nothing
  re-reads the interval afterwards.
- `app/config.py:128` — `venues_live_refresh_minutes: int = 5`
  (env/JSON-file backed).
- Hot-tunable admin key precedent: `app/dao/redis_venue_dao.py:487-495` reads
  `admin_config:venue_photos_cache_ttl_days` from Redis per use, falling back
  to `settings.photo_cache_ttl_days` when absent/invalid — the key is written
  by the vibesadmin panel (vibes_bot `app/admin/config_dao.py`), Redis-only.
- `app/services/admin_config_service.py` — the RDS-truth mirror service is
  for cs-server-owned write paths; the photo-TTL pattern (Redis-only,
  vibesadmin-written, reader falls back) is the one to follow here.
- APScheduler supports `scheduler.reschedule_job(job_id, trigger=...)` on a
  running scheduler — the apply mechanism.
- Background-job observability pattern: `BACKGROUND_JOB_*` metrics in
  `app/metrics.py`, used by every job wrapper in `main.py`.

## Current Behavior
The live refresh interval is fixed at scheduler startup from settings.
Changing it requires editing env/compose and restarting the cs-server
container.

## Desired Behavior
- A lightweight `refresh_interval_watch` job (every 60 s) must read
  `admin_config:live_refresh_minutes` from Redis, validate it as an integer
  within [1, 120], and when the effective value differs from the currently
  scheduled interval, reschedule `live_forecast_refresh` to the new interval.
- Key absent → effective value is `settings.venues_live_refresh_minutes`
  (deleting the key reverts to the default within a minute).
- Invalid value (non-integer, ≤0, >120) → keep the current interval, log a
  warning once per change (not every tick).
- The watcher must never die: Redis errors are caught, logged, and counted;
  the current schedule stays in place.
- The currently effective interval must be observable (gauge metric + an info
  log line on every applied change with old → new).

## Implementation Approach
- New module-level constant for the key (`admin_config:live_refresh_minutes`)
  next to the photo-TTL pattern, and a small reader with the validation/
  clamping rules above.
- Register `refresh_interval_watch` in `main.py` alongside the other jobs
  (60 s interval, no jitter needed); its body compares the effective value to
  the live job's current trigger interval and calls `reschedule_job` when
  different. Rescheduling resets the next-run time — acceptable (worst case
  one refresh shifts by the new interval).
- Keep the job's run wrapper consistent with the existing
  `BACKGROUND_JOB_*` metric pattern.

## Data, Config, And API Impact
- New Redis key consumed: `admin_config:live_refresh_minutes` (JSON integer,
  minutes, bounds [1, 120]; Redis-only, written by vibesadmin). No RDS
  schema change. No API change. `settings.venues_live_refresh_minutes`
  semantics unchanged (now "the default").

## Error Handling And Observability
- New gauge `live_refresh_interval_minutes` (currently effective interval).
- Watcher failures: `BACKGROUND_JOB_RUNS_TOTAL{job_name="refresh_interval_watch",status="error"}`
  + error log with the raw value; never raises out of the job.
- Info log on applied change: old interval, new interval, source
  (admin|default).

## Test Plan
Feature file: `tests/bdd/refresh/live-refresh-interval-admin.feature`

Scenarios:
- Setting the admin key reschedules the live refresh to the new interval and
  updates the gauge.
- Deleting the admin key reverts the schedule to the settings default.
- An invalid or out-of-range value keeps the current interval and logs a
  warning.
- A Redis read failure keeps the current schedule and counts a watcher error.
- The watcher applies a change within one watch cycle.

Pytest unit tests:
- Reader validation: valid, absent, non-integer, ≤0, >120, Redis exception.
- Reschedule decision: equal value → no-op; changed value → reschedule called
  with the new interval.

Manual or integration checks:
- In prod (after vibesadmin exposes the field): set 15, watch
  `background_job_runs_total{job_name="live_forecast_refresh"}` cadence drop
  and `live_refresh_interval_minutes` gauge move within a minute; BestTime
  call volume falls accordingly.

## Acceptance Criteria
- Changing `admin_config:live_refresh_minutes` retunes the live refresh
  cadence within ~1 minute, no restart.
- Absent/invalid values can never stall or kill the refresh job; defaults
  always win on bad input.
- The effective interval is visible in Prometheus.

## Open Questions
- None. Bounds [1, 120] minutes are proposed; retunable in code review if a
  wider range is wanted.
