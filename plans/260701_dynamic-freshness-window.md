# Dynamic Live-Busyness Freshness Window

## Branch
feature/dynamic-freshness-window

## Goal
Stop treating the serve-time stale window as an independent knob. Derive it from
the live refresh cadence — `window = live_freshness_refresh_factor × effective
refresh interval`, floored at `live_freshness_min_minutes` — so the two can never
desync. A slower refresh (chosen to spare the EC2 / cut BestTime call volume)
automatically widens the window, keeping on-schedule venues "live" instead of
mass-suppressing them. Also expose the age distribution of served vs suppressed
venues so "really stale" can be told apart from normal refresh desync.

## Non-goals
- No change to the suppression mechanism itself (stale → omit `venue_live_busyness`
  → vibes_bot forecast fallback with `is_forecast=true`).
- No vibes_bot / mobile change.
- Does not change the refresh interval itself (that stays an admin/panel value,
  `admin_config:live_refresh_minutes`); this only makes the window track it.

## Evidence
- Prior gate (static window): `app/services/live_freshness.py`,
  `plans/260701_live-busyness-freshness-gate.md`.
- Refresh cadence is admin-tunable via `admin_config:live_refresh_minutes`
  (`app/services/refresh_interval_watch.py`, bounds [1,120], default
  `settings.venues_live_refresh_minutes`=5).
- Production observation: at refresh==window (15==15) venues sat at the boundary
  and ~94% suppressed; dropping refresh to 5 (window stayed 15) recovered the
  real-live ratio 0.046→0.349. The desync, not genuine staleness, drove most
  suppression — motivating the coupling.

## Current vs desired behavior
- **Current:** window = `settings.live_freshness_max_age_minutes` (static 15),
  admin-overridable via `admin_config:live_freshness_max_age_minutes`.
- **Desired:** window = `round(factor × resolve_refresh_minutes())`, floored at
  `live_freshness_min_minutes`. `factor` default 2.0, floor default 5. The window
  reads the SAME `admin_config:live_refresh_minutes` the refresher uses.

## Implementation approach
- `app/config.py`: remove `live_freshness_max_age_minutes`; add
  `live_freshness_refresh_factor: float = 2.0` and `live_freshness_min_minutes: int = 5`.
- `app/services/live_freshness.py`:
  - `resolve_refresh_minutes(admin_config_service)` — reads
    `admin_config:live_refresh_minutes` (shape `{"minutes": int}` or bare int),
    bounds [1,120] (kept in sync with refresh_interval_watch), else settings
    default. Never raises.
  - `resolve_max_age_minutes()` now returns `max(min_minutes, round(factor × interval))`.
  - `classify_live_freshness()` returns `(verdict, age_minutes)` so the caller can
    record the age distribution.
- `app/handlers/venue_handler.py`: unpack `(verdict, age)`; observe the age on the
  new histogram for served / suppressed_stale.
- `app/metrics.py`: add `venue_serve_live_forecast_age_minutes{outcome}` histogram
  (buckets 1..240 min).

## Data, config, API impact
- **Config:** setting rename (`live_freshness_max_age_minutes` →
  `live_freshness_refresh_factor` + `live_freshness_min_minutes`). The
  `admin_config:live_freshness_max_age_minutes` override is no longer consulted
  (it was never wired into the admin panel, so nothing in prod sets it).
- **API:** unchanged (`venue_live_busyness` still nulled when stale).
- **Metrics:** new histogram; existing `venue_serve_live_busyness_total` unchanged.

## Error handling and observability
- Invalid/out-of-bounds/absent refresh override → settings default; never raises.
- The age histogram lets Grafana split suppressed_stale into "just past the
  window" (pipeline desync, absorbed once window = 2× interval) vs a long tail of
  hours-old payloads (a genuinely stalled/failing refresh).

## Test plan
Feature file: `tests/bdd/api/live-busyness-freshness-gate.feature`

Scenarios:
- Default 5-min cadence → 10-min window: 8-min-old served; 12-min-old suppressed.
- Slower 15-min cadence → 30-min window: 25-min-old still served; 40-min-old stale.
- Floor: 1-min cadence → window clamped to the 5-min minimum (4-min-old served).
- Boundary (age == window) is stale; unparseable gmttime suppressed.

Pytest (`tests/test_live_freshness.py`): gmttime parsing; `classify` returns
`(verdict, age)`; `resolve_refresh_minutes` parsing/bounds/default;
`resolve_max_age_minutes` = factor × interval, floored.

## Acceptance criteria
- With refresh N, a cached value younger than `2N` min (≥ 5) serves as live; older
  is suppressed to forecast.
- Changing `admin_config:live_refresh_minutes` moves the window with it, no redeploy.
- `venue_serve_live_forecast_age_minutes{outcome}` reports the served/suppressed age
  distribution.

## Open questions
- None. Factor 2.0 and floor 5 confirmed with the user (window = 2× interval to
  absorb desync; keep freshness high while running a slower, lower-load refresh).
