# Add-venue: no live-busyness spend + BestTime search rate-limit hardening

Date: 2026-07-03 · Branch: `feature/besttime-rate-limit-no-inline-live`

## Why

Operator evidence (BestTime dashboard, 2026-07-03): venue **adds** draw from the
"Venue Search (Normal)" monthly quota (96/7000 used) and do **not** burn
forecast credits — credits are spent by **live busyness retrieval**. Two
consequences for the add flow:

1. `AddVenueHandler._finalize_created_venue` does a best-effort **inline live
   forecast fetch** (`GET /forecasts/live`) right after a create. That is the
   only credit spend in the add path and it duplicates the live-busyness
   pipeline, which refreshes venues from the serving view on its own cadence.
   → Remove it. Live data must arrive only through the live pipeline once the
   venue is prioritized through the venues view.
2. BestTime's Venue Search endpoints are rate limited to **30 requests/minute
   and 300 requests/hour** (documentation.besttime.app, "Venue Search"). The
   client has no pacing and no 429 handling; batch adds would trip the limit.
   → Add a resilient client-side limiter + bounded 429 retry for the
   search-family calls.

## Behavior spec

- Adding a venue never issues `GET /forecasts/live`. The create response's
  `analysis` week data is still cached (comes free with the create).
- Search-family BestTime calls (`POST /forecasts` create, `GET /venues/filter`,
  `GET /venues/search`, `GET /venues/progress`) pass through a shared sliding
  window limiter (per-minute + per-hour, defaults 30/300, configurable). If a
  slot requires waiting, the client sleeps (bounded); if the wait would exceed
  `besttime_rate_max_wait_seconds` it raises `BestTimeRateLimitedError`
  immediately (fail fast, nothing sent, no quota drawn).
- An HTTP 429 from BestTime on a search-family call is retried up to 2 times,
  honoring `Retry-After` when present (else exponential backoff), all within
  the same max-wait budget; exhaustion raises `BestTimeRateLimitedError`.
- The add handler maps `BestTimeRateLimitedError` to the existing 502
  `besttime_error` envelope (slot released, retryable later) — no new HTTP
  contract.
- Reads outside the search family (live/week-raw for the refresh pipeline) are
  untouched.

## Feature file

`tests/bdd/api/add-venue-no-live-rate-limit.feature`

- Scenario: a successful add never calls the live-busyness endpoint
- Scenario: a transient BestTime 429 on create is retried and the add succeeds
- (limiter window pacing itself is internal timing behavior — unit-tested with
  an injected fake clock in `tests/test_besttime_client.py`; scenario-level
  sleeping would make BDD wall-clock dependent)

## Implementation

- `app/api/besttime_client.py`: `BestTimeRateLimitedError`; `_SearchRateLimiter`
  (async-lock, monotonic deques for 60 s / 3600 s windows, injectable
  clock/sleep for tests); acquire before the four search-family methods;
  429-aware bounded retry wrapping create and `_request` for search endpoints;
  metric events.
- `app/handlers/add_venue_handler.py`: drop `_inline_live_forecast` + its call;
  map `BestTimeRateLimitedError` → 502 `besttime_error` with message.
- `app/config.py`: `besttime_search_rate_per_minute=30`,
  `besttime_search_rate_per_hour=300`, `besttime_rate_max_wait_seconds=75.0`
  (covers a full minute window so per-minute pacing always waits; hour-window
  waits fail fast).
- `app/container.py`: pass the three settings to the client.
- `app/metrics.py`: `BESTTIME_SEARCH_RATE_LIMIT_TOTAL{endpoint,event}`
  (event ∈ waited | rejected | retry_429).

## Test plan

- BDD (real client over MockTransport, real handler): scenarios above; the
  transport records every request path so "no /forecasts/live call" is pinned.
- Unit: limiter window math with fake clock (no real sleeps); 429 retry honors
  Retry-After; max-wait rejection raises before sending; handler test flips
  `get_live_forecast.await_count == 1` → `== 0` on the happy path.

## Ops notes

- No schema/Redis changes; no deploy-order constraints. Internal monthly budget
  ledger semantics intentionally untouched (product decision — the operator can
  raise `admin_config:venue_monthly_budget` to reflect the real 7000/month
  search quota via the admin panel).
