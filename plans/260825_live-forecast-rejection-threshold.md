# Live forecast rejection threshold: wipe cache after N consecutive rejections

## Branch
fix/live-forecast-rejection-threshold

## Goal
`_fetch_and_cache_live_forecasts` must stop wiping a venue's previously-cached
good live forecast on the very first clean BestTime business rejection
(`status != "OK"`). It must instead track consecutive rejections per venue and
only delete the cached forecast once a configurable threshold `N` of
back-to-back rejections is reached, so a single flaky/transient rejection no
longer discards good data a durably-unforecastable venue would have lost
anyway a cycle later.

## Non-goals
- Do not change the sibling `deleted_not_available` branch (`status == "OK"`
  but `venue_live_busyness_available == False` — e.g. the venue is currently
  closed). That is a different, unambiguous signal (BestTime successfully
  classified the venue and says it has no live data right now) and must keep
  deleting immediately, every time. Only the `status != "OK"` business
  rejection path is gated by the new threshold.
- Do not change `venue_filter()`, `add_venue_to_account()`, or
  `get_week_raw_forecast()` behavior.
- Do not change the `venues_live_refresh_minutes` cadence, the priority-bounded
  venue-selection logic (`_select_refresh_venue_ids`), or the ledger/monthly-
  cap gate (`_ledger_allows_read`).
- Do not deploy this to prod as part of this change. This plan and its
  execution stop at PR creation (see Open Questions — explicit deployment-order
  dependency on the already-merged pacing/retry fix).
- Do not attempt to retrofit `get_week_raw_forecast`'s cache-wipe-on-rejection
  behavior (it has no equivalent "delete stale cache" branch today) — out of
  scope, not evidenced.

## Evidence

### Code path (re-read fresh on `main` @ `d99796b`, this session)
- `app/services/venues_refresher_service.py:665-758` (`_fetch_and_cache_live_forecasts`):
  the `status != "OK"` branch (`:698-703`) logs a WARNING
  (`"Error LiveForecast status=%r for %s, removing cache"`), increments
  `LIVE_FORECAST_FETCH_RESULTS.labels(result="deleted_not_ok")`, then always
  calls `self.venue_dao.delete_live_forecast(vid)` (`:713-719`) — no state is
  consulted before deleting; every rejection deletes.
- No per-venue consecutive-rejection counter exists anywhere in the codebase
  today. `app/dao/redis_venue_dao.py` has no such key family — this requires
  NEW persistent Redis state. The closest existing key is
  `LIVE_FORECAST_KEY_FORMAT = "live_forecast_v1:{}"` (`redis_venue_dao.py:27`),
  which the new streak key sits alongside.
- `_fetch_and_cache_live_forecasts` is reachable from TWO scheduled jobs:
  - `live_forecast_refresh` (`main.py:195-202`), the dedicated job, guarded by
    `job_lock.LIVE_FORECAST` (`app/services/job_lock.py`) so only one instance
    runs at a time. Cadence: `venues_live_refresh_minutes` (default 5 min
    `IntervalTrigger`), but real cycles took ~2.1h mean pre-fix (see prod
    evidence below).
  - `venue_catalog_refresh` (`main.py:184-192`,
    `refresh_venues_by_filter_for_default_locations(fetch_and_cache_live=True)`),
    which is **unlocked** (`venue_catalog_refresh` is not in
    `job_lock.LOCKED_JOB_NAMES`) and runs on `venues_catalog_refresh_minutes`
    (default 43200 min = 30 days).
  These two jobs can in principle call `_fetch_and_cache_live_forecasts`
  concurrently for overlapping venue sets. Confirmed empirically (see below)
  that the catalog-refresh job did not run at all in the 46.5h prod window
  observed, so this is a real but rare edge case, not exercised in the
  observed data — the new counter must still use an atomic Redis primitive
  (`INCR`, never a Redis GET-then-SET round trip) specifically because of this
  cross-job exposure.
- `GeoRedisClient` (`app/db/geo_redis_client.py`) has no `incr` method today;
  it has `set`, `get`, `mget`, `setex`, `del_`, `zrem`. A minimal `incr(key) ->
  int` wrapper (mirroring `del_`'s style) is needed.
- Existing unit tests in `tests/test_services.py`:
  - `test_live_forecast_delete_when_status_not_ok` (:371-388) asserts a
    SINGLE not-OK response deletes on the very first call — must change to
    assert the new below-threshold/at-threshold behavior.
  - `test_live_forecast_delete_when_not_available` (:391-408) asserts the
    closed-venue branch deletes immediately — must NOT change.
- `app/metrics.py:948-956` (`LIVE_FORECAST_FETCH_RESULTS`): today
  `result="deleted_not_ok"` fires on every rejection (meaning "deleted, no
  gate"). Once deletion is gated on N consecutive rejections, that label must
  fire ONLY when an actual delete happens (streak reached N), to keep its
  existing meaning ("cache was deleted") intact for any dashboard/alert
  already keyed on it.

### Prior investigation this session — pacing/retry fix, deployment status
- Commit `d99796b` ("fix: pace and retry POST /forecasts/live to cut refresh
  timeouts", #216, already merged to `main`) added client-side pacing +
  bounded retry for TRANSPORT failures (timeout/503/504) on
  `get_live_forecast`. It explicitly left the `deleted_not_ok` behavior
  unchanged as an out-of-scope follow-up
  (`plans/260825_live-forecast-pacing-retry.md`, "Follow-Up Recommendations":
  *"Revisit whether `deleted_not_ok` ... should keep serving stale-but-real
  data instead of wiping cache on a business rejection — a deliberate
  product/design call, not a bug."*). This plan is that follow-up.
- **Prod is confirmed NOT running that fix yet.** `docker inspect
  vibes_bot-cs-server-1` on `i-0893fb6d283243480` shows label
  `cs_server_main_sha:1b610e21f925c62318b8f105e58e553ec5f9ba70` — one commit
  behind `d99796b` (`1b610e2` is `d99796b`'s direct parent on `main`). Zero
  occurrences of the pacing fix's new `"live-forecast spacing"` log line in
  prod logs, confirming the same thing independently. **All prod log evidence
  below reflects the OLD unpaced/unretried code.**

### Live prod evidence (this session, via AWS SSM on `i-0893fb6d283243480` /
`vibes_bot-cs-server-1`, real prod logs over the container's full uptime — not
simulated, not from the earlier 44h pacing/retry investigation window)
- Container uptime observed: ~46.5h (started `2026-08-23T20:16:38Z`, pulled
  `2026-08-25T17:50Z`).
- 20 `live_forecast_refresh` cycles observed (`[Scheduler] Running
  LiveForecastRefreshJob"` start lines; 19 completed, 1 in-flight at pull
  time). Cross-validated via 20 distinct structured-log `job=<ULID>` run ids
  appearing across every start/completion/rejection/error line in the window
  — exact match confirms clean per-cycle attribution with no cross-job
  contamination. Independently confirmed the catalog-refresh job
  (`"Running VenueFilterMultiLocationJob"`) logged **zero** runs in this
  window (its 30-day default cadence did not fire), so none of this data is
  contaminated by the second, unlocked call site.
- Cycle duration (pre-fix, unpaced): min 4274s (~71min), median 6835s
  (~114min), mean 7628s (~127min), max 11927s (~199min). Cadence between
  cycle starts ≈2.1-2.24h average — this is the real-world unit for "how long
  does a venue keep showing stale-but-wrong good data" under any given N.
- 16,066 total clean-rejection events (`status='Error'` — the ONLY non-OK
  status value observed in the entire window, 100% of rejections) across
  1,973 distinct venues rejected at least once. 21,027 transport-error events
  (`"GetLiveForecast failed for ..."`, timeout/5xx) in the same window —
  consistent in proportion with the earlier pacing/retry investigation's
  ~41%/53%/5.8% (rejection/transport-failure/cached) split.
- **Consecutiveness** (each rejection bucketed into its cycle by wall-clock
  cycle-start boundaries, cross-validated against the `job=` ULID grouping —
  both methods agree on 20 cycles with zero ambiguity):
  - Only 65/1973 (3.3%) of ever-rejected venues were rejected in EXACTLY one
    cycle total across up to 20 cycles observed — a genuinely isolated,
    one-off rejection is rare.
  - 1,860/1973 (94.3%) reached a max consecutive-cycle run ≥2.
  - 1,623/1973 (82.3%) reached a max consecutive-cycle run ≥3.
  - Only 48/1973 (2.4%) were rejected in multiple cycles but NEVER
    back-to-back (repeated-but-not-consecutive — the one pattern an
    N-threshold does not protect against losing cache, however briefly, on
    each isolated recurrence).
  - The run-length histogram is heavily right-skewed with a long tail up to
    18-19 consecutive cycles — i.e. most rejected venues are durably, not
    transiently, unforecastable: rejected in essentially every cycle of the
    whole observed window.
- Of 3,636 "gap" intervals (a broken streak: rejected, then not-rejected for
  ≥1 cycle, then rejected again for the same venue), 3,522 (96.9%) coincide
  with a transport-error log line for that same venue during the gap —
  meaning most apparent streak breaks are very likely transport-failure
  artifacts of the OLD unpaced code, not genuine BestTime successes. The true
  underlying business-rejection consecutiveness is therefore probably a
  *lower* bound here, and should go up (making N=2/3 look more, not less,
  supported) once the already-merged pacing/retry fix is actually deployed
  and converts more of those transport failures into completed calls.
- **Caveat/limitation**: prod runs `LOG_LEVEL=INFO`; the success (`cached`)
  log line is DEBUG-only and invisible in prod logs, so a genuine per-cycle
  success for a given venue cannot be directly distinguished from "venue
  simply wasn't selected that cycle" in this dataset — only positive
  rejection/error evidence is directly observable. This does not undermine
  the *consecutive-rejection* counts themselves (two adjacent-cycle rejection
  log lines for the same venue is unambiguous positive evidence, immune to
  this gap), only the explanation for why some streaks break.

### Verdict (reached this session, drives the design below)
Proceed. Evidence supports **N=2**, not N=3: the isolated-rejection
population this change protects (the "exactly 1 cycle" 3.3% bucket) is
already fully covered at N=2. Durably-broken venues cross N=2 within one
extra cycle regardless of N (a one-time ~2.1-2.24h delay per venue the first
time it turns durably unforecastable — not a recurring cost, since once wiped
there is nothing left to protect). N=3's extra cycle of delay (~4.2-4.5h)
only additionally protects the small "repeated-but-not-consecutive" 2.4%
bucket, most of whose gaps are themselves transport-failure artifacts that
the separately-deployed pacing/retry fix should reduce anyway. `N` is a
config setting, not a hardcoded constant, so it can be retuned after
observing behavior post-deployment without a code change (mirrors the
pacing/retry fix's own "instant rollback lever" design philosophy).

## Current Behavior
On any single BestTime response with `status != "OK"` for a venue,
`_fetch_and_cache_live_forecasts` immediately deletes that venue's cached live
forecast (if any) via `venue_dao.delete_live_forecast`, logs a WARNING, and
increments `live_forecast_fetch_results_total{result="deleted_not_ok"}`. There
is no memory of prior rejections across refresh cycles.

## Desired Behavior
The system must track, per venue, the number of consecutive `status != "OK"`
rejections seen across refresh cycles. On a rejection:
- If the venue's consecutive-rejection count (after incrementing for this
  rejection) is below the configured threshold `N`
  (`live_forecast_rejection_streak_threshold`), the system must NOT delete the
  cached live forecast. It must log at WARNING (existing text, unchanged) and
  increment a new metric outcome distinguishing "rejected but cache
  preserved" from "rejected and cache deleted".
- If the count reaches `N`, the system must delete the cached live forecast
  (existing behavior), keep incrementing
  `live_forecast_fetch_results_total{result="deleted_not_ok"}` for this case
  only, and reset the consecutive-rejection count back to 0.
- Any `status == "OK"` outcome for the venue (whether cached successfully,
  skipped as venue-absent, or the sibling `deleted_not_available` closed-venue
  branch) must reset the consecutive-rejection count to 0 — an OK status is
  BestTime successfully classifying the venue, which is evidence against "this
  venue can't be forecast," even when the closed-venue branch still deletes
  the cache immediately for its own, unrelated reason.
- A transport error (`GetLiveForecast` exception, or a `SetLiveForecast`
  persistence failure) must leave the consecutive-rejection count unchanged —
  it carries no information about whether BestTime would have accepted or
  rejected the venue.
- `N=1` must reproduce today's exact behavior (delete on the first rejection)
  — an instant rollback lever via config, no code change, consistent with the
  pacing/retry fix's own rollback-lever pattern.

## Implementation Approach
- **Config**: add `live_forecast_rejection_streak_threshold: int = 2` to
  `app/config.py`, near the existing `venues_live_refresh_minutes` /
  `live_freshness_*` refresher settings, with a comment documenting the
  evidence-based default and the `N=1` rollback lever. Add the matching entry
  to `.env.example` and `config.example.json` following existing conventions.
- **Persistence** (`app/dao/redis_venue_dao.py`): add
  `LIVE_FORECAST_REJECTION_STREAK_KEY_FORMAT = "live_forecast_rejection_streak_v1:{}"`
  alongside `LIVE_FORECAST_KEY_FORMAT`. Add two `RedisVenueDAO` methods —
  `increment_live_forecast_rejection_streak(venue_id: str) -> int` (atomic
  `INCR`, returns the new count) and
  `reset_live_forecast_rejection_streak(venue_id: str) -> None` (delete the
  key; idempotent) — following the file's existing per-venue key-format /
  method-pair pattern (e.g. `set_live_forecast` / `delete_live_forecast`).
  This keeps the new state co-located with the live-forecast persistence it
  gates, per `CLAUDE.md`'s "DAOs own Redis persistence" guardrail, rather than
  introducing a second, disconnected budget-style DAO for what is really part
  of the live-forecast cache's own lifecycle.
- **Redis client primitive** (`app/db/geo_redis_client.py`): add a minimal
  `incr(key: str) -> int` method wrapping `self.client.incr(key)`, matching
  `del_`'s existing wrapping style (one-line docstring, direct passthrough,
  atomic by construction — no read-modify-write).
- **Service** (`app/services/venues_refresher_service.py`,
  `_fetch_and_cache_live_forecasts`): in the `status != "OK"` branch, call
  `increment_live_forecast_rejection_streak` before deciding whether to
  delete; compare the returned count against
  `settings.live_forecast_rejection_streak_threshold`; only call
  `delete_live_forecast` (and only increment
  `result="deleted_not_ok"`) when the count has reached the threshold,
  resetting the streak afterward; otherwise increment the new "preserved"
  metric outcome and skip the delete. In every `status == "OK"` outcome
  (`cached`, `skipped_venue_absent`, and the `deleted_not_available` branch),
  call `reset_live_forecast_rejection_streak`. Leave the `error` outcome
  (transport failure / `SetLiveForecast` exception) untouched — no
  increment, no reset. Wrap the new DAO calls in their own try/except,
  logging and continuing (never raising) on a Redis failure, matching this
  function's existing defensive style around every other DAO call.
- **Metric** (`app/metrics.py`): add a new `LIVE_FORECAST_FETCH_RESULTS`
  label value, `result="rejected_streak_below_threshold"`, and update the
  comment enumerating the label's possible values. `deleted_not_ok` keeps its
  existing meaning ("cache was actually deleted").
- **Existing BDD scenario needs reconciling**:
  `tests/bdd/refresh/live-forecast-pacing-retry.feature`'s scenario "A
  BestTime business rejection is never retried and still clears stale cache"
  (from the already-merged pacing/retry PR) asserts that a SINGLE `status:
  "Error"` response deletes an already-cached forecast — the exact `N=1`
  assumption this plan changes the default away from. That scenario's title
  and rejection-is-never-retried assertion (`exactly 1 live-forecast call was
  made`) stay true and out of scope; only its cache-clearing assertion is now
  default-threshold-dependent. `/execute-feature` must update that scenario
  (e.g. set the streak threshold to 1 in its Background/Given, or otherwise
  make the scenario explicit about which threshold it exercises) so it keeps
  passing under the new default without silently asserting the old N=1
  behavior as if it were still the default.

## Data, Config, And API Impact
- New Redis key family: `live_forecast_rejection_streak_v1:{venue_id}`,
  purely additive — no existing key format changes, no migration. Bounded in
  count by the servable-venue set (thousands, not millions); negligible
  against prod Redis's ~20MB used / no memory ceiling (`maxmemory 0`,
  `noeviction`, 3.9GB box).
- New config setting `live_forecast_rejection_streak_threshold` (default 2).
  This is a deliberate default *behavior* change (today's effective threshold
  is 1) — that is the entire point of this plan, not an accidental default
  drift.
- No HTTP request/response contract changes; no changes to what
  `/v1/venues/nearby` or venue-detail responses expose.

## Error Handling And Observability
- A Redis failure on the new increment/reset calls must not raise out of
  `_fetch_and_cache_live_forecasts` — log at ERROR with venue context and
  continue to the next venue, matching every other DAO-call error path in
  this function.
- New metric outcome `result="rejected_streak_below_threshold"` on
  `live_forecast_fetch_results_total` gives direct observability into how
  often the gate is doing its job (rejections that did NOT wipe cache) versus
  `deleted_not_ok` (rejections that DID). The ratio between the two, watched
  post-deployment, is the concrete signal for whether `N=2` needs retuning.
- No new background job, no new endpoint.

## Test Plan
Feature file: `tests/bdd/refresh/live-forecast-rejection-threshold.feature`

Scenarios:
- A single business rejection does not delete a venue's cached live forecast
  (streak below threshold; cache untouched; "preserved" outcome recorded).
- The Nth consecutive business rejection deletes the cached live forecast
  (streak reaches threshold; cache deleted; `deleted_not_ok` recorded; streak
  resets).
- A successful live forecast resets an in-progress rejection streak (reject
  once, then succeed, then reject again — the third rejection must NOT delete,
  because the intervening success reset the count to 0).
- A closed venue (`status == "OK"`, busyness unavailable) still deletes the
  cached forecast immediately, unaffected by the rejection-streak threshold,
  and resets any in-progress rejection streak.
- A transport failure between two rejections does not advance or reset the
  rejection streak (reject, transport-error, reject again — the second
  rejection must delete under `N=2`, proving the transport error didn't
  reset progress toward the threshold).

Pytest unit tests (`tests/test_services.py`,
`TestVenuesRefresherService`):
- Update `test_live_forecast_delete_when_status_not_ok` for the new
  below-threshold-does-not-delete / at-threshold-deletes semantics (covering
  both sides with `live_forecast_rejection_streak_threshold=2` in the test
  fixture).
- New: rejection below threshold does not call `delete_live_forecast` and
  increments the new metric outcome, not `deleted_not_ok`.
- New: rejection at threshold calls `delete_live_forecast`, increments
  `deleted_not_ok`, and resets the streak (assert the DAO reset method is
  called).
- New: a successful cache (`test_live_forecast_caching_success`'s scenario)
  resets the streak (assert the DAO reset method is called even on success).
- New: `test_live_forecast_delete_when_not_available` (closed venue) still
  deletes immediately regardless of streak state, and also resets the streak.
- New: a transport error / `GetLiveForecast` exception does not call either
  the increment or the reset DAO method.
- New DAO-level tests in `tests/test_redis_dao_unit.py` (or the file housing
  `RedisVenueDAO` unit coverage) for
  `increment_live_forecast_rejection_streak` (atomic increment, correct key)
  and `reset_live_forecast_rejection_streak` (delete, idempotent on an absent
  key).

Existing coverage to reconcile (not new, but will break under the new default
if left untouched):
- `tests/bdd/refresh/live-forecast-pacing-retry.feature`'s "A BestTime
  business rejection is never retried and still clears stale cache" scenario
  — see Implementation Approach.
- `tests/test_services.py::test_live_forecast_delete_when_status_not_ok` —
  see above.

Manual or integration checks:
- None required beyond `make test-unit` and `make test-bdd` passing. No live
  BestTime call is required or permitted per this repo's BDD policy.

## Acceptance Criteria
- `make test-unit` and `make test-bdd` pass locally.
- A single `status != "OK"` rejection for a venue with
  `live_forecast_rejection_streak_threshold=2` (the shipped default) leaves
  its previously-cached live forecast in place.
- The 2nd consecutive rejection for that same venue deletes the cached live
  forecast and the streak resets to 0.
- The `deleted_not_available` (closed-venue) branch is provably unaffected —
  its existing test continues to pass unmodified in its assertions (only
  fixture/threshold plumbing, if any, may change).
- `live_forecast_fetch_results_total` gains the
  `result="rejected_streak_below_threshold"` outcome and `deleted_not_ok`'s
  existing meaning ("actually deleted") is preserved.
- `N=1` (set via config) reproduces today's exact immediate-delete behavior,
  verified by at least one test.

## Open Questions
- **Deployment-order dependency (must be resolved before this is deployed,
  not before `/execute-feature` — the PR itself should stay open pending
  this):** this plan's consecutiveness evidence was gathered against the OLD
  unpaced/unretried code (prod has not yet deployed `d99796b`). This change
  should NOT be deployed to prod before, or without, also deploying the
  already-merged pacing/retry fix — consecutive-rejection rates may shift
  (evidence above suggests they would shift toward MORE consecutiveness, i.e.
  further supporting N=2, but this has not been observed against the fixed
  code and should be re-checked post-deployment of both fixes together).
