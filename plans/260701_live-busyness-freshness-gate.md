# Live Busyness Freshness Gate (serve-time)

## Branch
feature/live-busyness-freshness-gate

## Goal
At serve time, suppress a live busyness value whose underlying BestTime forecast
is older than a configurable freshness window, so the `venue_live_busyness`
field is omitted (null) from the minified venue response. vibes_bot's existing
absence→forecast fallback then serves the forecast estimate (`is_forecast=true`)
instead of a stale live number. The gate lives exactly where the
`venue_live_busyness_available` check already lives, and requires **no vibes_bot
code change**.

## Non-goals
- No vibes_bot change. We rely on its existing `None`→forecast substitution
  (`_enrich_missing_live_busyness_with_forecast`) and `is_forecast=true` flag.
- No change to vibes_bot's admin `/api/live-coverage` dashboard (stays at its own
  24h classifier — decision: keep cs-server-only).
- No change to the live refresh cadence, the delete-on-not-OK/unavailable path,
  or the exception-path "retain last value" behavior in
  `venues_refresher_service`. This change gates at read/serve time, not refresh.
- No new RDS column and no migration: freshness is derived from the
  `venue_current_gmttime` already inside the cached live-forecast payload.
- Verbose-mode (`verbose=true`) full `live_forecast` payload is left intact
  (it already exposes `venue_current_gmttime`; consumers can judge age
  themselves). The gate applies to the minified serving list vibes_bot consumes.

## Evidence
- Gate point — live value is populated only when available, no age check:
  `app/handlers/venue_handler.py:260-266` (`_transform`).
- Pre-minify sort also treats any present `live_forecast` as "has live":
  `app/handlers/venue_handler.py:227-235` (`sort_key`).
- Stale value is retained across a BestTime outage (exception path neither
  writes nor deletes): `app/services/venues_refresher_service.py:606-612`.
- Projector re-asserts live RDS→Redis with **no age gate** (contrast photos'
  remaining-TTL): `app/services/redis_projection_service.py:118-123` vs
  `:153-169`.
- Redis live key is a plain `SET`, **no TTL**: `app/dao/redis_venue_dao.py:274-276`.
- RDS `besttime.live_forecast.updated_at` exists but is never SELECTed, so age is
  invisible downstream: `app/dao/rds_venue_store.py:359-372`.
- The timestamp we will use is already carried in the payload:
  `LiveForecastResponse.venue_info.venue_current_gmttime`
  (`app/models/live_forecast.py:14-24`).
- Admin config read pattern (serve-time): `app/services/admin_config_service.py`
  (`get()` reads `admin_config:<key>` from Redis).
- Precedent for an admin-tunable, bounds-checked minutes setting:
  `app/services/refresh_interval_watch.py:29-34` (`admin_config:live_refresh_minutes`,
  MIN=1/MAX=120) and `app/config.py:128` (`venues_live_refresh_minutes: int = 5`).
- Downstream behavior this depends on (vibes_bot, not changed here):
  forecast substitution `vibes_bot/app/services/live_busyness_service.py:99-114`;
  wired at `vibes_bot/app/services/venue_pipeline.py:192`; existing 24h freshness
  classifier used only by the admin dashboard
  `vibes_bot/app/admin/routes.py:357,386-397`.

## Current Behavior
A live busyness value that stops refreshing — because a BestTime fetch raised
(timeout / 5xx / network / rate-limit) or the whole `live_forecast_refresh` job
stalled — is retained in RDS, re-asserted into Redis every projection cycle, and
served by `/v1/venues/nearby` as genuine live (`is_forecast` never set) with no
upper bound on age. Forecast substitution only ever happens when the live value
is **absent**, which upstream only produces on a *successful* fetch that reports
status not-OK or not-available.

## Desired Behavior
When building the minified serving list, treat a live forecast as usable only if
it is both available AND fresh:

- Compute `age = now_utc - parse(venue_current_gmttime)`.
- Define **fresh** as `age < max_age` (strictly less; boundary age == max_age is
  stale), matching the admin dashboard's `(now - generated) < _LIVE_FRESH_MAX_AGE`.
- If `venue_current_gmttime` is missing or unparseable, treat the live value as
  **stale** (fail toward forecast) so a payload/format drift degrades to the
  estimate rather than serving an un-datable "live" number. This is made
  observable via a metric so drift is not silent.
- When stale, do not populate `venue_live_busyness` (leave it `None`) and group
  the venue with "no live" in the pre-minify sort. `weekly_forecast` is still
  returned so vibes_bot can compute and substitute the forecast.

The effective window is resolved per request:
`admin_config:live_freshness_max_age_minutes` (integer minutes) if present and
in-bounds, else `settings.live_freshness_max_age_minutes`. **Default 1440
minutes (24h)** — admin-tunable, no redeploy required.

## Implementation Approach
- **Settings:** add `live_freshness_max_age_minutes: int = 1440` to
  `app/config.py`. Document it in `config.example.json` / `.env.example` if those
  enumerate settings.
- **Threshold resolution:** read `admin_config:live_freshness_max_age_minutes`
  via `AdminConfigService` once per request; validate as a positive int within
  bounds (reuse the `refresh_interval_watch` MIN/MAX convention, MIN=1); on any
  missing/invalid value fall back to the settings default. Never raise on a bad
  admin value.
- **gmttime parser:** add a tolerant parser (own to cs-server; the vibes_bot one
  is not importable across repos) accepting ISO 8601 (with optional `Z`) then the
  BestTime display formats `"%A %Y-%m-%d %I:%M%p"`, `"%A %Y-%m-%d %H:%M:%S"`,
  `"%A %Y-%m-%d %H:%M"`, returning a UTC-aware `datetime` or `None`. Never raise.
- **Freshness predicate:** `_live_forecast_is_fresh(lf, now_utc, max_age) -> bool`
  returning `False` when `lf` is `None`, gmttime is missing/unparseable, or
  `age >= max_age`.
- **Wire into `VenueHandler`:** inject the admin-config reader (via
  `app/container.py`), compute `now_utc` and the effective `max_age` once in the
  serve entry, and apply the predicate in two places for internal consistency:
  the `_transform` minified extraction (the field vibes_bot consumes) and the
  `sort_key` grouping. Final product ordering is owned by vibes_bot's scoring, so
  the sort change is for cs-server-internal consistency only.
- **No key/format change:** the Redis live key, RDS schema, and projection are
  untouched.

## Data, Config, And API Impact
- **API:** `/v1/venues/nearby` minified `venue_live_busyness` becomes `null` for
  venues whose live payload is stale. Field is already `Optional[int]`
  (`app/models/venue.py:191`); the wire contract is unchanged. Verbose payload
  unchanged.
- **Config:** new setting `live_freshness_max_age_minutes` (default 1440) + new
  admin key `admin_config:live_freshness_max_age_minutes`.
- **Persistence / migration:** None.

## Error Handling And Observability
- Unparseable/missing `venue_current_gmttime` → treated stale (safe) and counted,
  never raises.
- Invalid/out-of-bounds admin threshold → settings default, logged once.
- **Metric:** add a Prometheus counter for serve-time live outcomes, e.g.
  `venue_serve_live_busyness_total{outcome="served|suppressed_stale|suppressed_unparseable"}`
  (final name/labels chosen in execution against `app/metrics.py` conventions),
  so the stale-suppression rate and any gmttime drift are visible in Grafana.
- Debug log per suppression with `venue_id` and computed age; no PII, no payload
  dump.

## Test Plan
Feature file: `tests/bdd/api/live-busyness-freshness-gate.feature`

Scenarios:
- Fresh live within the window → response includes `venue_live_busyness` from the
  live value.
- Stale live beyond the window → response omits the live value (`null`) and the
  `suppressed_stale` metric is incremented; `weekly_forecast` is still present.
- Boundary: a live payload exactly at `max_age` old is treated as stale.
- Missing/unparseable `venue_current_gmttime` → live value suppressed and the
  `suppressed_unparseable` metric is incremented.
- Admin override of `admin_config:live_freshness_max_age_minutes` changes the
  effective window within the same request cycle (a value fresh under the default
  but stale under a tighter override is suppressed).
- Invalid admin override value → the settings default window is applied.

Pytest unit tests:
- gmttime parser: ISO 8601 (with/without `Z`), each BestTime display format, and
  garbage → `None`.
- `_live_forecast_is_fresh`: `None` forecast, age just under / equal / over
  `max_age`, missing gmttime.
- Threshold resolution: admin key valid, invalid, out-of-bounds, absent → default.
- `_transform` / `sort_key`: a present-but-stale live forecast is suppressed and
  grouped as "no live".

Manual or integration checks:
- Against a real Redis, write a `live_forecast_v1:<id>` whose `venue_current_gmttime`
  is > 24h old, GET `/v1/venues/nearby`, and assert `venue_live_busyness` is null
  while `weekly_forecast` remains.

## Acceptance Criteria
- A stale (older than the effective window) cached live forecast is served with
  `venue_live_busyness = null` from `/v1/venues/nearby`, and vibes_bot renders the
  forecast estimate with `is_forecast=true`.
- A fresh cached live forecast is served with its live `venue_live_busyness`
  unchanged from today.
- The window is 1440 minutes by default and overridable via
  `admin_config:live_freshness_max_age_minutes` without redeploy.
- Missing/unparseable gmttime degrades to forecast and is counted, never 500s.
- A Prometheus counter exposes served vs stale-suppressed vs unparseable outcomes.

## Open Questions
- None. (Threshold default 24h, admin-tunable with settings default, cs-server-only
  with the vibes_bot dashboard left unchanged — all confirmed with the user.)
