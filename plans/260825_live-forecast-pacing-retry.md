# Live-forecast refresh: client-side pacing + bounded transient retry

## Branch
fix/live-forecast-pacing-retry

## Goal
Give `BestTimeAPIClient.get_live_forecast()` (`app/api/besttime_client.py:500-537`)
the same category of client-side protection its sibling BestTime calls in the
same file already have — pacing before send and bounded retry on the failure
modes actually observed for this endpoint — so the live-forecast refresh cycle
stops burning most of its wall-clock time on unretried timeouts and recovers
some fraction of the transient BestTime-side failures that currently produce
zero data.

## Non-goals
- Do not change `venues_refresher_service.py:693-720`'s `deleted_not_ok`
  cache-wipe-on-rejection behavior (a BestTime `status != "OK"` business
  rejection still deletes any previously-cached live forecast for that venue,
  unretried). Whether stale-but-real data should ever outlive a rejection is a
  separate product decision. Noted below as a follow-up recommendation only.
- Do not add HTTP 429 handling to `get_live_forecast`. 429 was never observed
  on this endpoint in the diagnosed window (see Evidence); adding a defensive
  429 path here would be speculative, not evidence-driven.
- Do not change `venue_filter()`, `add_venue_to_account()`, or the existing
  `_SearchRateLimiter` / `besttime_search_rate_per_minute` /
  `besttime_search_rate_per_hour` behavior for the venue-search family.
- Do not change `get_week_raw_forecast()` (the weekly refresh path). It has the
  same missing-pacing/retry shape as `get_live_forecast` did, but no evidence
  was gathered for it in this investigation; changing it now would be
  unevidenced scope creep. Noted below as a follow-up recommendation.
- Do not change the 5-minute `IntervalTrigger` cadence for the `live_forecast_
  refresh` job (`main.py:406-410`) or the priority-bounded venue-selection
  logic (`_select_refresh_venue_ids`). This plan only changes what happens
  inside each individual `get_live_forecast` call.

## Evidence

### Live 44-hour prod observation (this investigation, verified against
`/metrics` on `vibes_bot-cs-server-1` and prod logs — not simulated)
- ~38,700 total calls to `POST /forecasts/live`.
- Only 5.8% produced usable cached data
  (`live_forecast_fetch_results_total{result="cached"}` = 2,229).
- 53.4% were transport failures: 34.3% client-side 10-second timeouts (no
  retry existed), 19.1% HTTP 503/504 from BestTime. Bursts of 503/504 were
  observed in tight ~250ms-apart clusters in the logs.
- 40.8% were clean BestTime responses with `status != "OK"` — a real
  "can't forecast this venue" rejection, out of scope (see Non-goals).
- 5.8 + 53.4 + 40.8 = 100.0%; 34.3 + 19.1 = 53.4% — the two failure-mode splits
  are internally consistent.
- Not a billing/credit issue: no 402/403, no credit/balance/payment text in
  logs. Confirmed uniform across old and new venues via a same-day
  catalog-growth campaign (~927 new venues) — the new cities show *better*
  live-forecast coverage than the old dominant city, ruling out "too many
  venues" as cause. This is a chronic client-side reliability gap, not a
  capacity ceiling being newly hit.
- Consequence: the job is scheduled every 5 minutes
  (`venues_live_refresh_minutes=5`, `main.py:406-410`) but a real cycle takes
  ~2.1 hours on average
  (`background_job_duration_seconds_sum / count` for
  `job_name="live_forecast_refresh"`), because ~37 of every ~40 cumulative
  cycle-hours are spent blocked on unretried 10-second timeouts. Overlapping
  cycles queue behind each other rather than actually refreshing every 5
  minutes.

### Root cause (code comparison, `app/api/besttime_client.py`)
- `venue_filter()` (:456-462) calls `await self._search_limiter.acquire(...)`
  before sending, and `_request(..., retry_429=True)`.
- `add_venue_to_account()` (:590-606) calls the same limiter, plus
  `_send_with_retry(..., retry_429=True, stop_retry_on=_looks_like_monthly_
  cap_body, ...)`.
- `get_live_forecast()` (:500-537) does neither: it calls `self._request("POST",
  "/forecasts/live", params=query_params)` directly, with no limiter
  `.acquire()` call and `retry_429` left at its default `False`, and `_request`
  passes no `retry_...` kwargs to `_send_with_retry` at all in that call.
- `venues_refresher_service.py::_fetch_and_cache_live_forecasts` (:665-758)
  calls `get_live_forecast` once per venue_id, sequentially, in a plain `for`
  loop with no concurrency and no delay between iterations — so nothing
  between the client and the loop was pacing these ~2,360-per-cycle calls.

### BestTime documentation (fetched live from documentation.besttime.app
during this investigation)
- General account default: "the API is limited to 300 API requests per
  minute."
- Venue Search family (create `/forecasts`, `/venues/filter`, `/venues/
  search`, `/venues/progress`): "30 request per minute and 300 requests per
  hour" — already implemented by `_SearchRateLimiter` /
  `besttime_search_rate_per_minute` / `besttime_search_rate_per_hour`
  (`app/config.py:196-206`).
- **"New Forecast & Live Endpoints"** (this heading appears to cover both
  `POST /forecasts` create and `POST /forecasts/live`): "By default the API is
  limited to 10 requests per second. Contact us for higher limits." — a
  different, more generous, per-second-granularity limit than the
  venue-search family's per-minute/per-hour figures.
- No documented guidance on retry/backoff, or on the cause of 503/504, for any
  endpoint.
- Worth flagging, informational only, not resolved here: the existing comment
  at `besttime_client.py:588-589` says the create call "shares its 30/min +
  300/hour rate limits" with the search family, while the docs describe create
  and live together under the separate "10 requests/second" heading. These are
  plausibly two different things (a monthly "Venue Search" quota-draw
  classification vs. the raw per-second HTTP rate limit). Existing create/
  filter behavior is unchanged by this plan (see Non-goals); flagged as a
  follow-up worth a human look, not a bug.

### Interpretation this plan is built on
Our own call volume already runs far below even a conservative reading of any
documented limit — calls are sequential/awaited one at a time with a 10s
timeout, so realized throughput is well under 1 req/sec on average, and the
observed ~250ms-apart burst clustering of 503/504 tops out far short of 10
req/sec too. So the 53.4% transport failure rate is not us blowing through a
documented BestTime cap; it reads as BestTime-side reliability/overload for
this endpoint (or an unrelated transient network condition) that our own
unthrottled, unretried call pattern does nothing to absorb. Client-side pacing
here is therefore a protective, considerate-citizen measure that also happens
to smooth the observed burst clustering — not a fix for "we are exceeding
BestTime's limit" — and bounded retry is a bet that some fraction of these
transient failures resolve on a second try shortly after. Both are phrased as
explicit trade-offs below, not guaranteed wins, with an instant per-setting
rollback lever.

## Current Behavior
`get_live_forecast()` issues one unpaced `POST /forecasts/live` per call, with
the client-wide 10-second timeout and no retry of any kind. A timeout or
503/504 immediately propagates as `httpx.TimeoutException` /
`httpx.HTTPStatusError` out of `_request`, is logged and counted once under
`BESTTIME_API_CALLS_TOTAL{endpoint="/forecasts/live",status="error"}` /
`BESTTIME_API_ERRORS_TOTAL{endpoint="/forecasts/live",error_type=...}`, and is
caught by `_fetch_and_cache_live_forecasts`'s `except Exception` (:686-691),
which logs and increments `LIVE_FORECAST_FETCH_RESULTS.labels(result="error")`
then moves on to the next venue. Nothing paces or retries before that point.

## Desired Behavior
- Before each `POST /forecasts/live`, `get_live_forecast()` waits, if needed,
  for a minimum spacing interval since the previous live-forecast call, so
  consecutive calls in a refresh cycle are never fired back-to-back.
- A client-side timeout on `POST /forecasts/live` is retried a small bounded
  number of times before giving up.
- An HTTP 503 or 504 response to `POST /forecasts/live` is retried the same
  bounded number of times (with the existing `Retry-After`-aware-else-
  exponential backoff already used for 429s) before giving up.
- A BestTime `status != "OK"` business rejection (a normal 2xx response) is
  never retried — unchanged from today; retrying it would only waste calls
  against a definite "no forecast for you" answer.
- An HTTP 429 is never retried by this change (see Non-goals) — unchanged from
  today (already-observed-never on this endpoint).
- When retries are exhausted, the call fails exactly as it does today: the
  same exception type propagates, the same existing
  `BESTTIME_API_CALLS_TOTAL` / `BESTTIME_API_ERRORS_TOTAL` counters fire once
  for the final outcome (not once per attempt), and
  `_fetch_and_cache_live_forecasts` continues to the next venue exactly as
  today. No new terminal-failure code path is introduced — only some fraction
  of previously-terminal failures now succeed on a retried attempt instead.

## Implementation Approach

### Pacing
Add a new, minimal pacer — deliberately simpler than `_SearchRateLimiter`,
since the live-forecast family's documented limit (10 req/s) is a single
per-second-granularity figure, not the search family's dual per-minute/
per-hour windows. It tracks only the monotonic time of the last live-forecast
call and enforces a minimum interval before the next one, sleeping the
(small, sub-second) shortfall when needed. Unlike `_SearchRateLimiter`, it
never fail-fast-rejects: because the enforced interval is always small, the
worst-case wait is bounded by the interval itself, so there is no realistic
"wait budget exhausted" case worth a `BestTimeRateLimitedError` path. Clock and
sleep are injectable (same pattern as `_SearchRateLimiter`) so unit tests never
sleep for real. `get_live_forecast()` calls `.acquire()` once per invocation,
before building/sending the request — retries inside a single
`get_live_forecast()` call are governed only by the retry backoff below, not
by a second pacer acquisition.

A new `BestTimeAPIClient.__init__` parameter (default from
`settings.besttime_live_min_interval_seconds`, see Config below) wires the
interval in, following the constructor-parameter pattern `search_rate_per_
minute` / `search_rate_per_hour` already use. `<= 0` disables pacing entirely,
mirroring the search limiter's own disable convention — an instant rollback
lever.

### Retry
`_send_with_retry` (`besttime_client.py:254-329`) already generalizes 429
handling behind two parameters (`retry_429`, `stop_retry_on`) and already
computes Retry-After-or-exponential backoff via `_retry_after_seconds`. Extend
it with two new, orthogonal, opt-in parameters used only by `get_live_forecast`
(existing callers' behavior is unchanged by their absence):
- A set of additional HTTP status codes that trigger the same retry loop 429
  does (503 and 504 for this call), sharing the same attempt counter and
  backoff computation as 429 would — bounded by the same existing
  `attempt >= 2` cap and `self.rate_max_wait_seconds` budget already used for
  429 (worst case here — 1 retry, i.e. attempts capped below the hardcoded
  429 loop's `attempt >= 2` bound, see Config — needs at most ~1-2s of backoff
  total, nowhere near the 75s budget, so no new wait-budget setting is added;
  see the Open Questions / judgment call below on why 429's `attempt >= 2` cap
  is reused as the mechanism's ceiling rather than introducing a second
  hardcoded number).
- A flag that also retries a client-side `httpx.TimeoutException` raised by
  the transport call itself (today that exception propagates immediately,
  bypassing the retry loop) — caught, backed off (no response object to read
  `Retry-After` from, so backoff falls back to the same exponential formula
  `_retry_after_seconds` already uses when the header is absent), and retried
  within the same bounded attempt/wait accounting as the status-code case.

Both new triggers are wired through `_request` (which already exposes
`retry_429` as a passthrough) so `get_live_forecast` keeps using `_request` —
and therefore keeps its existing metrics/archive/logging on the call's final
outcome — rather than hand-rolling a parallel request path. Only the FINAL
attempt's outcome is allowed to raise/return out of `_send_with_retry` in the
normal way; every intermediate retried attempt is swallowed inside the loop
(this mirrors exactly how an intermediate 429 never reaches `_request`'s own
except blocks today).

`get_live_forecast` calls `self._request("POST", "/forecasts/live", params=
query_params, retry_on_timeout=True, retry_transient_status=frozenset({503,
504}))`, after its `await self._live_forecast_pacer.acquire(...)` call.

### Retry attempt bound — a deliberate, evidence-aware trade-off
Each retried attempt on this endpoint can itself cost a full ~10-second
timeout, unlike a 429 retry on the search family (typically fast). Retrying a
timeout is therefore a real bet: it converts some fraction of transient
failures into successes (the goal), but for a venue whose timeout is not
transient but persistent, it makes that one venue's cycle cost roughly double
(or triple) instead of helping. The diagnosed evidence (tight ~250ms-apart
503/504 clusters, i.e., bursty rather than uniformly-distributed failure)
supports "some meaningful fraction of these are transient," but there is no
data in this investigation distinguishing which timeouts are transient vs.
persistent-per-venue. Given that uncertainty, the default retry bound is kept
small — one retry (two attempts total) — and made a named, tunable setting
(see Config) specifically so the operator has an instant, code-change-free
rollback if post-deploy metrics show cycle duration got worse rather than
better (see the "how to verify" section below for the exact comparison).

## Data, Config, And API Impact
- New `Settings` fields in `app/config.py`, adjacent to the existing
  `besttime_search_rate_per_minute` / `besttime_search_rate_per_hour` /
  `besttime_rate_max_wait_seconds` block (:196-206), documented the same way:
  - `besttime_live_min_interval_seconds: float = 0.5` — minimum spacing
    between consecutive `POST /forecasts/live` calls (2 req/sec), a
    conservative default well under BestTime's documented 10 req/sec ceiling
    for this endpoint family (5x margin), chosen to smooth the observed
    ~250ms-apart burst clustering rather than to approach the documented
    limit. `<= 0` disables pacing.
  - `besttime_live_retry_max_attempts: int = 2` — total attempts (1 retry) for
    a `POST /forecasts/live` call that times out or gets 503/504. `<= 1`
    disables retry (first-failure-is-final, today's behavior) — the rollback
    lever referenced above.
- `app/container.py`: pass both new settings into the `BestTimeAPIClient(...)`
  construction (:227-236), alongside the existing search-family settings.
- `config.example.json`: add both new keys for documentation parity (the
  existing search-family settings are not currently mirrored there, but
  `besttime_add_venue_timeout_seconds` is — this follows that precedent for
  discoverability).
- No Redis key, RDS schema, or HTTP request/response contract changes. No
  change to what mobile or vibes_bot observe from any endpoint — this is
  entirely internal to one outbound BestTime call's reliability.

## Error Handling And Observability
- New Prometheus counter `besttime_live_forecast_resilience_total` (labels
  `endpoint`, `event`; `event` ∈ `paced` | `retry_timeout` | `retry_http_5xx`),
  defined in `app/metrics.py` next to `BESTTIME_SEARCH_RATE_LIMIT_TOTAL`,
  mirroring its label shape (`endpoint` kept even though only one value exists
  today, for consistency with its sibling metric and in case
  `get_week_raw_forecast` gains the same treatment later — see Non-goals
  follow-up). Deliberately scoped to its own metric rather than folded into
  `BESTTIME_SEARCH_RATE_LIMIT_TOTAL`, whose name and docstring are explicitly
  about the venue-search family — reusing it here would conflate two
  different BestTime endpoint families under one metric's semantics, exactly
  the kind of same-family assumption this investigation found to be wrong.
  - `paced`: incremented whenever the pacer actually sleeps before sending.
  - `retry_timeout` / `retry_http_5xx`: incremented once per retried attempt
    (not per call), immediately before that attempt's backoff sleep.
  - No new "exhausted" event: a call that exhausts its retry budget still
    raises/returns the same way an unretried call does today, so the
    existing `besttime_api_calls_total{endpoint="/forecasts/live",
    status="error"}` and `besttime_api_errors_total{endpoint="/forecasts/
    live",error_type=...}` counters already correctly represent "ultimately
    failed" without a new metric — this is a deliberate simplification (see
    the "how to verify" section for how paced/retried vs. ultimately-failed
    are read together post-deploy).
- Existing background-job failure handling in `venues_refresher_service.py`
  is unchanged: `_fetch_and_cache_live_forecasts`'s per-venue `try`/`except`
  still logs and records `result="error"`, then continues to the next venue —
  no change needed there, and this plan does not touch that file.
- Logging: the pacer logs at INFO when it waits (mirroring
  `_SearchRateLimiter.acquire`'s existing log line); each retried attempt logs
  at WARNING with the failure reason and attempt number (mirroring the
  existing 429-retry WARNING log in `_send_with_retry`).

## Test Plan
Feature file: `tests/bdd/refresh/live-forecast-pacing-retry.feature`

This is a background refresh path with no new HTTP endpoint and no change to
any served API contract, but this repo's own convention (`tests/bdd/refresh/`
already covers non-HTTP background refresh behavior — see
`eligible-priority-live-refresh.feature`, `priority_bounded_besttime_refresh.
feature`) treats this class of change as BDD-eligible, and the prior sibling
plan for the venue-search family (`plans/260703_add-venue-no-live-besttime-
rate-limit.md`) wrote a BDD scenario for its analogous "transient failure is
retried and the call still succeeds" behavior. This plan follows the same
split that prior plan made explicit: the *observable outcome* of a retry
(does the venue end up with a cached live forecast, or a recorded error, or
neither) is BDD material; the *internal timing/window math* of the pacer and
backoff is pytest-only with an injected fake clock (real BDD sleeping would
make the suite wall-clock dependent and flaky).

Test-harness note: this repo's existing `tests/bdd/refresh/` scenarios drive
`VenuesRefresherService` against `context.besttime`, a `_ProgrammableBestTime`
stand-in (`tests/bdd/environment.py:28-88`) that replaces the *entire*
`BestTimeAPIClient` — it has no retry logic of its own, so it structurally
cannot exercise retry/pacing behavior that lives inside the real client. This
feature instead wires a real `BestTimeAPIClient` with its `.client` attribute
replaced by an `httpx.AsyncClient(transport=httpx.MockTransport(handler))`
(the same technique this repo's own unit tests already use to reach into
`.client` — see `tests/test_besttime_client.py`), scripted per-venue by
`venue_id`, into a real `VenuesRefresherService` — the same "real client over
a scripted transport, real handler/service" pattern the prior sibling plan
used for the create-429 BDD scenario, applied here to the refresh domain.
`MockTransport` handlers can raise `httpx.ReadTimeout` synchronously with no
real wait, and any 503/504 response is scripted with `Retry-After: 0` so the
one allowed retry's backoff sleep is ~0s — no scenario needs real wall-clock
delay.

Scenarios:
- Cache the live forecast after a transient timeout is retried — the first
  `POST /forecasts/live` for a venue raises a timeout, the retried call
  returns `status: OK` with busyness available; the venue's live forecast
  ends up cached.
- Cache the live forecast after a transient BestTime error is retried
  (Scenario Outline, examples 503 and 504) — same shape, first response is
  503/504, retried response is 200 OK.
- Record an error and continue to the next venue when retries are exhausted —
  one venue times out on every attempt; its live forecast is not cached and
  an error is recorded for it; a second venue in the same cycle still gets
  its live forecast cached normally (pins that one venue's exhausted retry
  never aborts the cycle).
- A BestTime business rejection is never retried and still clears stale cache
  — regression guard for the explicitly out-of-scope `deleted_not_ok` path:
  a venue with a previously-cached live forecast gets a clean `status: "Error"`
  response (not a transport failure); only one call is made (no retry
  attempted) and the previously-cached forecast is deleted, exactly as today.

Pytest unit tests (`tests/test_besttime_client.py`, new test classes mirroring
the existing `TestSearchRateLimiter` / `TestCreate429Retry` conventions):
- `TestLiveForecastPacing` (injected fake clock/sleep, no real sleeping,
  same fixture shape as `TestSearchRateLimiter._limiter`):
  - Two calls within the configured interval sleep for the remaining
    shortfall; the sleep amount is exact.
  - A call after the interval has already elapsed never sleeps.
  - `besttime_live_min_interval_seconds <= 0` disables pacing (never sleeps).
  - The default `BestTimeAPIClient()` constructs with the settings-derived
    interval (mirrors `TestAddVenueTimeout.test_default_add_venue_timeout_
    is_60_and_base_timeout_unchanged`'s settings-default-wiring pattern).
- `TestLiveForecastRetry` (mocks `api_client.client.request` with an
  `AsyncMock(side_effect=[...])` list mixing a raised exception and/or an
  `httpx.Response`, exactly like `TestCreate429Retry`):
  - A single timeout followed by a 200 OK is retried once and returns the
    parsed `LiveForecastResponse`; `request` is awaited exactly twice.
  - A single 503 followed by a 200 OK is retried once (parametrized with 504).
  - Timeouts on every attempt (bounded by
    `besttime_live_retry_max_attempts`) still raise `httpx.TimeoutException`
    after the configured attempt count — not more, not fewer — and
    `besttime_api_errors_total{endpoint="/forecasts/live",error_type=
    "timeout"}` increments by exactly 1 (not once per attempt).
  - A 429 response is not retried by `get_live_forecast` (returns/raises on
    the first 429 exactly as today) — pins the explicit Non-goal.
  - A clean `status != "OK"` 200 response is not retried (only one `request`
    call) — pins the explicit Non-goal and protects the `deleted_not_ok` path
    this plan does not touch.
  - `besttime_live_retry_max_attempts <= 1` disables retry (first failure is
    final, `request` awaited exactly once).
  - `besttime_live_forecast_resilience_total{endpoint="/forecasts/live",
    event="retry_timeout"}` / `{event="retry_http_5xx"}` / `{event="paced"}`
    each increment by exactly 1 per occurrence, read via
    `prometheus_client.REGISTRY.get_sample_value`, matching this file's
    existing `TestAddVenueResponseParsing._errors_metric()` pattern.

Manual or integration checks: None (no live BestTime call in any test, no
Redis/RDS dependency for the new logic itself — pacing/retry live entirely
inside `BestTimeAPIClient`).

## Acceptance Criteria
- `get_live_forecast()` waits at least `besttime_live_min_interval_seconds`
  since the previous live-forecast call before sending, proven by a unit test
  that fails if the pacer is bypassed.
- A single client timeout or a single HTTP 503/504 on `POST /forecasts/live`
  is retried and a subsequent success is returned as a normal
  `LiveForecastResponse`, proven by unit tests for all three trigger cases.
- Retries are bounded by `besttime_live_retry_max_attempts`; exhausting them
  raises/returns exactly what an unretried call raises/returns today (same
  exception type, same existing metrics fire once, not once per attempt),
  proven by a unit test asserting both the exception type and the metric
  delta.
- HTTP 429 and a clean `status != "OK"` response are never retried by this
  change, proven by unit tests that fail if either is added.
- The refresh cycle continues to the next venue after one venue exhausts its
  retries, and a business rejection still clears that venue's stale cache,
  proven by the BDD feature's exhaustion and rejection scenarios.
- `besttime_live_forecast_resilience_total` increments correctly for `paced`,
  `retry_timeout`, and `retry_http_5xx` events.
- Full suite green: `pytest tests/ -q` and `make test-bdd` (new feature's
  `@wip` tag removed once its steps pass).

## Open Questions
None.

## How To Verify In Prod After Deploy
Compare against the 44-hour baseline in Evidence above, using the same
`/metrics` scrape on `vibes_bot-cs-server-1` and the same query shapes, over a
comparable post-deploy window (recommend at least 24-44h to average out
BestTime-side variability the way the baseline window did):

1. **Transport-failure rate should drop.** Re-run the same breakdown of
   `besttime_api_calls_total{endpoint="/forecasts/live"}` (success vs. error)
   and `besttime_api_errors_total{endpoint="/forecasts/live",error_type=
   "timeout"}` vs. the baseline's 34.3% timeout / 19.1% 503-504 shares (503 vs
   504 aren't separately labeled today — `error_type="http_error"` covers
   both; cross-reference with logs if the split matters). Improvement looks
   like a lower error share than baseline, not necessarily zero — the fix
   recovers some fraction of transient failures, not all of them.
2. **Cached fraction should rise.** Re-run
   `live_forecast_fetch_results_total{result="cached"}` as a share of total
   calls; baseline was 5.8%. Any increase is direct evidence retries are
   recovering real data; `result="deleted_not_ok"` (40.8% baseline) should
   stay roughly flat since that path is untouched — a large *drop* in that
   share alongside a rise in `cached` would suggest something else changed
   and is worth a second look before crediting this fix.
3. **New resilience counters give direct before/after-impossible visibility.**
   `besttime_live_forecast_resilience_total{endpoint="/forecasts/live",
   event="retry_timeout"}` and `{event="retry_http_5xx"}` did not exist in the
   baseline (nothing was retried), so there is no baseline delta for them —
   but their post-deploy values, read alongside
   `besttime_api_errors_total{endpoint="/forecasts/live",...}`, tell the
   recovery rate directly: `(retry_timeout + retry_http_5xx attempts) -
   (final timeout/5xx errors after deploy)` is an estimate of calls the retry
   saved. `event="paced"` shows how often the pacer actually had to wait —
   near-zero would mean the interval is set looser than the natural call
   cadence and isn't doing much; a high count is expected and fine (it means
   pacing is actively smoothing the sequential loop).
4. **Cycle duration should drop toward the 5-minute schedule.** Re-run
   `background_job_duration_seconds_sum / background_job_duration_seconds_
   count` for `job_name="live_forecast_refresh"`; baseline was ~2.1 hours.
   **Caveat, stated plainly per the retry trade-off above**: if a large share
   of the observed timeouts turn out to be persistent-per-venue rather than
   transient, retrying them will push this number *up*, not down, since each
   retried failure now costs ~2x its former wall-clock time. If this metric
   gets worse rather than better after a full comparable window, the
   recommended first response is to set `besttime_live_retry_max_attempts=1`
   (disables retry, keeps pacing) via config/env and redeploy — no code
   change needed — and re-observe; if cycle duration is still not improving,
   set `besttime_live_min_interval_seconds<=0` too to fully revert to
   pre-change behavior.
5. **No new error class.** `besttime_api_calls_total{endpoint="/forecasts/
   live",status="error"}`'s total volume (summed, not the internal timeout/5xx
   split) should not increase versus a volume-normalized baseline share —
   this change should only ever convert some errors into successes or leave
   them as errors after a bounded delay, never introduce a new failure mode.

## Follow-Up Recommendations (not part of this fix)
- Revisit whether `deleted_not_ok` (`venues_refresher_service.py:693-720`)
  should keep serving stale-but-real data instead of wiping cache on a
  business rejection — a deliberate product/design call, not a bug.
- `get_week_raw_forecast()` (`besttime_client.py:539-563`) has the same
  missing-pacing/retry shape `get_live_forecast` had; no evidence was
  gathered on its failure rate in this investigation, so it was left
  untouched here — worth the same diagnostic pass if weekly-refresh
  reliability is ever in question.
- The `besttime_client.py:588-589` comment classifying the create call under
  the search family's 30/min+300/hour limit sits oddly next to BestTime's docs
  bundling create together with live under a separate "10 requests/second"
  heading — worth a human/BestTime-support confirmation of which figure
  actually governs create, independent of this fix.
