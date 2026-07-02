# Future-Time Forecast Day For /v1/venues/nearby

## Branch
fix/future-time-forecast

## Goal
Let `/v1/venues/nearby` return the weekly-forecast day the caller asks for, not
only "today". When a caller passes `target_day_offset`, each venue's
`weekly_forecast` must carry that future day's `WeekRawDay`, so downstream
consumers (vibes_bot) can serve future-time venue requests instead of returning
an empty list.

## Non-goals
- No change to the `weekly_forecast` response shape (stays a single
  `WeekRawDay`, not a list). Returning all 7 days was rejected: 7x Redis reads
  per venue and ~7x payload would break the serving latency budget and change
  the response contract for every caller.
- No change to live-busyness serving or the live-busyness freshness gate.
- No hour selection here — the hour within the day is chosen by vibes_bot from
  the returned 24-value `day_raw`. This endpoint only selects the day.
- No mobile change.

## Evidence
- `app/handlers/venue_handler.py:204-231` — `besttime_day_int` is derived from
  `datetime.now(recife_tz).weekday()` only, then
  `venue_dao.get_week_raw_forecast(v.venue_id, besttime_day_int)` fetches a
  single day. There is no way to request another day.
- `app/routers/venue_router.py:38-46` — `get_venues_nearby` accepts only
  `lat, lon, radius, verbose`; no time parameter.
- `app/dao/redis_venue_dao.py` — key `weekly_forecast_v1:{venue_id}_{day_int}`;
  `get_week_raw_forecast(venue_id, day_int)` already reads any single day. The
  refresher already writes all 7 days, so every day is present in Redis.
- `app/models/venue.py:192` — `weekly_forecast: Optional[WeekRawDay]`
  (single-day contract preserved).
- Migration note: this "today-only" behavior was ported verbatim from the
  original Go implementation (`MIGRATION_SUMMARY.md`); it was never a
  regression here. The empty-list symptom surfaced only after the July 1
  live-busyness freshness gate (PRs #62/#64) started nulling stale live
  busyness, removing the value that had been masking the missing future
  forecast downstream.

## Current Behavior
`/v1/venues/nearby` always computes today's `besttime_day_int` and returns
today's `weekly_forecast` for every venue, regardless of any future day the
caller wants.

## Desired Behavior
- `/v1/venues/nearby` accepts an optional `target_day_offset` query parameter
  (integer, `>= 0`; interpreted modulo 7 because the weekly forecast is
  weekly-periodic).
- When `target_day_offset` is omitted or `0`, behavior is byte-for-byte the same
  as today (today's forecast) — full backward compatibility.
- When `target_day_offset = N`, the handler computes
  `besttime_day_int = (recife_today_weekday + N) % 7` and each venue's
  `weekly_forecast` carries that day's `WeekRawDay`.
- Live-forecast serving, freshness gating, ordering, and all other response
  fields are unchanged.

## Implementation Approach
- `app/routers/venue_router.py`: add
  `target_day_offset: Optional[int] = Query(None, ge=0)` to `get_venues_nearby`
  and pass it to the handler. (No upper bound in validation; the handler applies
  `% 7`, so callers cannot trigger a 422 by exceeding the window.)
- `app/handlers/venue_handler.py`: add `target_day_offset: Optional[int] = None`
  to `get_venues_nearby`; replace the hardcoded
  `besttime_day_int = recife_time.weekday()` with
  `besttime_day_int = (recife_time.weekday() + (target_day_offset or 0)) % 7`.
  Everything else (live merge, freshness gate, transform, sort) is untouched.
- No DAO change: `get_week_raw_forecast(venue_id, besttime_day_int)` already
  reads the requested day.
- No model/response-shape change.

## Data, Config, And API Impact
- **API:** `/v1/venues/nearby` gains an optional `target_day_offset` query param.
  Additive and backward-compatible; absent param == today.
- **Response:** unchanged shape; `weekly_forecast` now reflects the requested
  day when the param is set.
- **Persistence/config/migration:** None. All 7 days already exist under the
  existing Redis keys.

## Error Handling And Observability
- Invalid `target_day_offset` (negative / non-int) is rejected by the Pydantic
  Query boundary (422), consistent with existing param validation.
- No new external call or background job. Existing request logging/metrics for
  `/v1/venues/nearby` cover this path; no new metric required. Include
  `target_day_offset` in the request log context for the endpoint.

## Test Plan
Feature file: `tests/bdd/api/future-time-forecast.feature`

Scenarios:
- Omitting `target_day_offset` returns today's `weekly_forecast` (unchanged
  behavior).
- `target_day_offset=0` returns today's `weekly_forecast` (explicit-zero parity
  with omission).
- `target_day_offset=N` (a future day) returns that day's `WeekRawDay`
  (`day_int` equals `(today + N) % 7`, `day_raw` matches the projected day).
- `target_day_offset` beyond the week (e.g. 8) wraps modulo 7 rather than
  erroring.
- Negative `target_day_offset` is rejected with a 422.

Pytest unit tests:
- `venue_handler.get_venues_nearby` day-index math: for a fixed Recife
  "today", assert the selected `besttime_day_int` for offsets 0, 1, 6, 7, and
  that the DAO is queried with that day for each venue.
- Backward-compat: omitted param queries the same day as `offset=0`.

Manual or integration checks:
- Against a Redis fixture with distinct per-day `day_raw`, hit
  `/v1/venues/nearby?...&target_day_offset=5` and confirm the response's
  `weekly_forecast.day_int` and `day_raw` match day `(today+5)%7`.

## Acceptance Criteria
- `/v1/venues/nearby?...&target_day_offset=N` returns, per venue, the
  `weekly_forecast` for day `(today+N)%7`.
- Requests without the param are unchanged (same day, same payload shape).
- Response shape (`weekly_forecast: Optional[WeekRawDay]`) is unchanged.
- New + existing BDD and unit tests pass; no live external calls in tests.

## Open Questions
- None.
