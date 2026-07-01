# BestTime Add-Venue Timeout

## Branch
fix/besttime-add-venue-timeout

## Goal
Give the BestTime "create venue" call (`POST /forecasts`, exposed as
`BestTimeAPIClient.add_venue_to_account`) its own, longer, configurable HTTP
timeout so the manual add-venue flow survives BestTime's slow-but-healthy
forecast-generation latency, instead of failing at the client-wide 10s default.

## Non-goals
- Do not change the timeout used by the high-frequency read paths
  (`get_live_forecast`, `venue_filter`, `get_week_raw_forecast`,
  `list_account_inventory`) — those must keep the tight default so a slow live
  refresh cannot pin a worker.
- Do not add retries, circuit-breaking, or timeout-then-created reconciliation
  (a venue BestTime creates after we time out). Worth doing, but separate.
- Do not touch the handler's release-slot / 502 behavior on transport error.
- No change to Redis keys, RDS schema, or the add-venue API contract.

## Evidence
- Prod RCA (2026-07-01): a single `add_venue` attempt logged
  `app.handlers.add_venue_handler - ERROR - [AddVenueHandler] BestTime transport
  error: ReadTimeout` at 10:35:19 while BestTime was degraded (5,272 `502 Bad
  Gateway` responses across the retained ~96h of cs-server logs; a live
  `GET /venues` probe from the prod host timed out at 60s). The venue never
  reached the store (0 rows match "beiju" across active+deprecated).
- `app/container.py:113` constructs `BestTimeAPIClient(...)` with **no** `timeout`
  argument, so every call uses the constructor default `timeout=10.0`
  (`app/api/besttime_client.py:34`).
- `add_venue_to_account` (`app/api/besttime_client.py:262`) issues
  `self.client.request("POST", "/forecasts", ...)` with no per-request timeout,
  inheriting the 10s client default.
- BestTime docs: `POST /forecasts` is synchronous and forecasts are "usually
  created within a few seconds"; POST "costs more credits and takes longer to
  respond" than GET. So the create endpoint is the slow one and 10s is too tight
  under load, while reads are fast and should stay at 10s.
- Existing client tests use `respx` and construct the client directly:
  `tests/test_besttime_inventory_sync.py:74-148`.

## Current Behavior
All BestTime HTTP calls — the rare, slow `POST /forecasts` create and the
frequent, fast live/read calls — share one 10s client-wide timeout. When
BestTime is slow (degraded, or simply slow to build a fresh forecast), the
create call raises `httpx.ReadTimeout`; the handler releases the reserved
monthly slot and returns 502, persisting nothing.

## Desired Behavior
`add_venue_to_account` must apply an independent, configurable timeout
(`besttime_add_venue_timeout_seconds`, default 30s) to its `POST /forecasts`
request. All other BestTime calls must continue to use the existing client-wide
default (10s). The timeout must be overridable via env/JSON config without a code
change.

## Implementation Approach
- `app/config.py`: add `besttime_add_venue_timeout_seconds: float = 30.0` in the
  BestTime configuration block (env-overridable via the existing `.env`/JSON
  flattening mechanism).
- `app/api/besttime_client.py`:
  - `__init__`: add `add_venue_timeout: float = 30.0`; store `self.add_venue_timeout`.
    Leave the client-wide `timeout` (10s) and the `httpx.AsyncClient(timeout=...)`
    construction unchanged.
  - `add_venue_to_account`: pass `timeout=self.add_venue_timeout` to the
    `self.client.request(...)` call. No other method changes.
- `app/container.py:113`: pass
  `add_venue_timeout=settings.besttime_add_venue_timeout_seconds`.
- `config.example.json` + `.env.example`: document the new key under the
  BestTime section.

## Data, Config, And API Impact
- Config: new setting `besttime_add_venue_timeout_seconds` (float, default 30.0),
  documented in `config.example.json` and `.env.example`. Absent config →
  30s default. No request/response, persistence, migration, or feature-flag
  changes.

## Error Handling And Observability
No new runtime path. The existing `add_venue_to_account` timeout/transport
handling (metrics `BESTTIME_API_CALLS_TOTAL{status="error"}`,
`BESTTIME_API_ERRORS_TOTAL{error_type="timeout"}`, ERROR log, handler slot
release + 502) is unchanged; only the point at which the read times out moves
from 10s to 30s for this one call. A longer timeout can hold the request-handler
coroutine open longer under a real outage — bounded (30s) and acceptable for a
manual admin action.

## Test Plan
# bdd-exempt: Internal HTTP-client timeout hardening. The only externally
# observable difference (an add that previously 502'd now succeeds) depends on
# real BestTime response latency, which repo policy forbids exercising in BDD
# (no live BestTime; deterministic fakes only). A Gherkin scenario would have to
# assert the client's per-request timeout internals, i.e. a unit test in
# disguise. Covered deterministically by the pytest cases below.

Pytest unit tests (`tests/test_besttime_inventory_sync.py`, alongside the
existing add_venue cases):
- `add_venue_to_account` issues its request with `timeout` equal to the
  configured `add_venue_timeout` (construct with `timeout=5.0,
  add_venue_timeout=30.0`; assert the timeout kwarg passed to the underlying
  httpx client is 30.0).
- A read call (`_request`, exercised via the account-inventory/read path) passes
  no per-request `timeout`, so it inherits the client-wide default.
- Constructor default `add_venue_timeout` is 30.0 and the client-wide `timeout`
  default remains 10.0; an explicit `add_venue_timeout` overrides the default.
- Config default: `Settings().besttime_add_venue_timeout_seconds == 30.0`.

Manual or integration checks:
- None required (live BestTime currently degraded). Optional post-merge: confirm
  a real add succeeds once BestTime recovers.

## Acceptance Criteria
- `add_venue_to_account` sends `POST /forecasts` with a 30s (configurable)
  timeout; all other BestTime calls still use the 10s client default.
- `besttime_add_venue_timeout_seconds` is settable via env/JSON and defaults to
  30.0.
- Targeted pytest for the above passes; no other BestTime behavior changes.

## Open Questions
- None.
