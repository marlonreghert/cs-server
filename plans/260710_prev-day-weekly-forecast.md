# Attach Previous Business Day's Weekly Forecast For Nighttime Serving

## Branch
fix/prev-day-weekly-forecast

## Goal
`/v1/venues/nearby` must give readers what they need to resolve busyness
correctly between 00:00 and 05:59 Recife time: alongside the requested
calendar day's `weekly_forecast` entry, attach the previous day's entry as a
new optional `weekly_forecast_prev` field, so the reader (vibes_bot) can apply
the BestTime 6 AM day anchor. Flag-gated for instant rollback.

## Non-goals
- Changing which entry `weekly_forecast` itself carries (backward
  compatibility with the current reader is preserved verbatim).
- Fixing vibes_bot's selection logic (its own plan, sequenced after this).
- Accepting a `target_hour` parameter or computing "effective day" server-side
  — cs-server never learns the client's hour; the reader owns that decision.
- Batching the per-venue weekly reads (tracked separately in the refactor
  assessment, projector/nearby N+1).

## Evidence
Proof that the current behavior is wrong (required by CLAUDE.md's "preserve
BestTime day-index behavior unless a plan proves the current behavior is
wrong"):

- BestTime `day_raw` semantics: index 0 = 6 AM of `day_int`'s day, indices
  18–23 = 00:00–05:00 of the **following** calendar day. This is pinned by the
  consumer's own code and tests: vibes_bot
  `app/services/forecast_busyness_service.py:58-67` ("day_raw[18] = 12 AM …
  day_raw[23] = 5 AM") and
  `tests/unit_test/test_busyness_prediction_service.py:153-164`
  (`test_overnight_scanning`: from Monday 23:00, 2 AM lives at index 20 of
  Monday's array). The `RawWindow` metadata BestTime returns
  (`app/models/week_raw.py:19-29`, `time_window_start`) carries the same 6 AM
  anchor.
- cs-server attaches the **calendar** day's entry with no anchor shift:
  `app/handlers/venue_handler.py:214-221` (`besttime_day_int =
  (python_weekday + (target_day_offset or 0)) % 7`), fetched at `:242-244`,
  serialized at `:496` into `MinifiedVenue.weekly_forecast`
  (`app/models/venue.py:192`).
- Consequence: at Recife hour h < 6, the operative data lives in **yesterday's**
  entry (indices h+18), but only today's entry is attached; the reader indexes
  `(h − 6) % 24` of it, which is tomorrow morning's value — ~24 h in the
  future. Cross-verified independently from both sides of the boundary
  (wrapper `plans/260710_bug-assessment.md`, findings vibes_bot H1 and
  cross-repo 1.1, both CONFIRMED).
- Arrays are stored verbatim from BestTime
  (`app/services/venues_refresher_service.py:399-404`), so the stored data is
  correct — only the attachment day is incomplete for nighttime reads.

## Current Behavior
Each venue in the `/v1/venues/nearby` response carries `weekly_forecast`: the
single `WeekRawDay` for the current Recife calendar weekday plus
`target_day_offset` (mod 7). Between 00:00 and 05:59 no attached entry covers
the current moment under the 6 AM anchor; readers either misread tomorrow's
early-morning values as "now" (vibes_bot today) or would find no matching day
at all.

## Desired Behavior
- When the flag is enabled, each venue additionally carries
  `weekly_forecast_prev`: the `WeekRawDay` for
  `(besttime_day_int − 1) % 7`, fetched the same way as the main entry and
  `None` when that day has no stored forecast.
- `weekly_forecast` is unchanged in meaning, shape, and day selection.
- When the flag is disabled, the field is absent/None — the response is
  byte-equivalent to today's (rollback path).
- Both verbose and minified responses carry the field.

## Implementation Approach
- `app/models/venue.py`: add `weekly_forecast_prev: Optional[Any] = None` to
  `VenueWithLive` and `MinifiedVenue`, mirroring the existing
  `weekly_forecast` field.
- `app/handlers/venue_handler.py` `_merge`: when
  `settings.weekly_forecast_prev_day_enabled`, fetch
  `get_week_raw_forecast(venue_id, (besttime_day_int - 1) % 7)` with the same
  per-venue try/except-debug-log pattern as the main fetch, and attach it.
  `_transform` passes it through to `MinifiedVenue` like `weekly_forecast`.
- `app/config.py`: `weekly_forecast_prev_day_enabled: bool = True`
  (env `WEEKLY_FORECAST_PREV_DAY_ENABLED`), following the existing
  `*_enabled` convention. Default on: the field is additive and ignored by the
  current reader, so enabling is safe before vibes_bot changes; the flag
  exists purely as the rollback lever.
- No change to `besttime_day_int` computation, sorting, or any other response
  field.

## Data, Config, And API Impact
- API: one new optional response field `weekly_forecast_prev` on each venue in
  `/v1/venues/nearby` (verbose and minified). Additive; existing readers are
  unaffected (vibes_bot treats venues as dicts and passes unknown keys through
  its pipeline; its outbound Pydantic models drop them before mobile).
- Config: new `WEEKLY_FORECAST_PREV_DAY_ENABLED` (default true). Document in
  `.env.example` / `config.example.json` if those list serving flags.
- Persistence: none — read-only reuse of `get_week_raw_forecast` with a
  different `day_int`. No Redis key or RDS schema change.
- Cross-repo contract (pinned in wrapper coordination plan): reader rule is
  "use `weekly_forecast_prev` when check-hour < 6, else `weekly_forecast`;
  fall back to `weekly_forecast` when `_prev` is absent". Deploy/rollback safe
  in any order on either side.

## Error Handling And Observability
- The prev-day fetch uses the same swallow-and-debug-log pattern as the main
  weekly fetch: a missing/failed prev-day read attaches `None` and never
  degrades the rest of the venue.
- Reuse of the existing DAO path means existing read metrics cover the new
  fetch. Add a debug-level log of the attached prev day_int alongside the
  existing `_merge` day log line (extend the existing INFO log, no new line
  per venue).
- No new failure mode: flag off = current behavior exactly.

## Test Plan
Feature file: `tests/bdd/api/prev-day-weekly-forecast.feature`

Scenarios:
- Nearby venues carry the previous business day's weekly entry: with weekly
  forecasts stored for Friday (day_int 4) and Saturday (day_int 5) and the
  current Recife day being Saturday, each venue's `weekly_forecast` has
  `day_int` 5 and `weekly_forecast_prev` has `day_int` 4 with Friday's stored
  `day_raw` verbatim.
- Day offset shifts both entries: with `target_day_offset=1` on a Saturday,
  `weekly_forecast.day_int` is 6 and `weekly_forecast_prev.day_int` is 5.
- Week wraparound: on a Monday (day_int 0), `weekly_forecast_prev.day_int` is
  6 (Sunday).
- Missing previous day degrades gracefully: when only the current day's
  forecast is stored, `weekly_forecast` is present and
  `weekly_forecast_prev` is null, and the venue is otherwise fully served.
- Flag off restores the legacy shape: with
  `WEEKLY_FORECAST_PREV_DAY_ENABLED=false`, no venue carries a
  `weekly_forecast_prev` value.

Pytest unit tests:
- `_merge` attaches prev-day entry with correct `(day_int − 1) % 7` for
  day_int 0 (wraps to 6) and non-zero days; attaches None when the DAO raises;
  flag-off skips the fetch entirely (DAO not called for prev day).
- Minified `_transform` passes `weekly_forecast_prev` through unchanged.

Manual or integration checks:
- After deploy (AWS SSO session): sample `GET /v1/venues/nearby` in prod and
  confirm `weekly_forecast_prev.day_int == (weekly_forecast.day_int - 1) % 7`;
  confirm response size growth is acceptable (~one day_raw array per venue).

## Acceptance Criteria
- Every venue served with the flag on carries `weekly_forecast_prev` for the
  previous business day, or null when unavailable, in both verbose and
  minified modes.
- `weekly_forecast` (day selection, shape, values) is unchanged in all modes.
- Flag off reproduces today's response shape exactly.
- BDD scenarios and targeted pytest pass; existing suites stay green.

## Open Questions
- None.
