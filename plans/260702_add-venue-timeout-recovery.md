# Add-Venue Resilience: 60s Timeout + Timeout Self-Recovery + Honest Error Payloads

## Branch
feature/add-venue-timeout-recovery

## Goal
Stop losing successful BestTime creates to timeouts, and make every add-venue
failure legible to the admin panel. Three changes:

1. Raise the add-venue BestTime timeout default from 30s to **60s**
   (`besttime_add_venue_timeout_seconds`).
2. **Timeout self-recovery:** when the create still times out, check BestTime's
   account inventory (free read) for the venue — its `venue_id` is
   deterministic on name+address — and, if present, continue as a normal
   success (persist, ledger, enrich). The 2026-07-01/02 incidents showed
   timeouts routinely leave a **created and charged** venue behind
   (Beijupirá, Laça Pina) that we then drop.
3. Every error response carries an actionable `detail` (and BestTime's own
   message where available) so the admin UI can show the real reason.

## Non-goals
- The admin-panel UI work and the vibes_bot proxy timeout (planned separately in
  vibes_bot — see wrapper coordination plan `260702_add-venue-feedback`).
- Retrying the BestTime create itself (each POST re-forecasts and re-charges;
  recovery must use the free inventory read, never a second create).
- Changing live/weekly refresh timeouts (stay at the tight client default).

## Evidence
- Timeout knob exists: `besttime_add_venue_timeout_seconds` = 30.0
  (`app/config.py`, PR #60) wired into `add_venue_to_account`
  (`app/api/besttime_client.py`).
- Prod incidents: 2026-07-01 10:35 Beijupirá — 10s timeout, venue created
  server-side; 2026-07-02 13:42+13:43 Laça Pina — **30s** timeouts during a
  window where light reads were ~90% healthy, venue created server-side
  (`ven_7744656f…`, `venue_forecasted=True`, account 2017→2018). BestTime's
  create computes a fresh forecast and runs far slower than reads when they
  are degraded (docs say "a few seconds" normally).
- Deterministic id: BestTime docs — "the venue_id is generated based on the
  venue name + address geocoding result… forecasting the same venue again
  results in the same venue id." This makes inventory-lookup recovery exact.
- Free lookup exists: `list_account_inventory()` (`besttime_client.py`) pages
  `GET /venues` at zero credit cost; `AccountInventoryVenue` carries
  venue_id/name/address/lat/lng/forecasted.
- Current timeout path: `add_venue_handler.py:138-147` — generic except →
  release slot → 502 `"BestTime is unavailable: ReadTimeout"`; nothing
  persisted; the paid venue is stranded (recovered manually twice now).
- Error bodies today: monthly-cap 429 carries `besttime_status`/`besttime_message`;
  geo-fallback/transport/bad-response 502s carry `detail` only.

## Current Behavior
- Create timeout (>30s) → 502 "BestTime is unavailable", slot released, venue
  stranded on BestTime (created + charged), operator retries and risks paying
  again or gives up.

## Desired Behavior
- Create waits up to 60s.
- On timeout, the handler runs a bounded, free **inventory reconcile**: search
  the account inventory for the submitted venue (normalized name+address
  match; a short grace delay before the check is an execution decision). If
  found: proceed exactly as a successful create — persist, `mark_touched`,
  inline Google enrichment, week-raw/live best-effort — and return 201 with a
  `recovered_from_timeout: true` marker in the body. If not found (or the
  reconcile read itself fails): release the slot and return the existing 502
  with a detail that says the create timed out, the venue was not confirmed,
  and a later retry maps to the same venue id (no duplicate).
- All failure bodies include BestTime's own `message` when one exists
  (`besttime_message`), alongside the human `detail`.

## Implementation Approach
- **Config:** bump `besttime_add_venue_timeout_seconds` default 30.0 → 60.0
  (`config.py`, `.env.example`, `config.example.json`).
- **Handler (`add_venue_handler.py`):** catch the create timeout specifically
  (httpx.TimeoutException, distinct from other transport errors); run
  `_reconcile_timed_out_create(request)`: iterate `list_account_inventory()`
  with accent-folded, case-insensitive name(+address) matching; on hit, build
  the same success path used today (reuse `_persist_new_venue`-equivalent
  construction from the inventory row + place_id enrichment) and add
  `recovered_from_timeout: true` to the 201 body; on miss, current 502 with the
  honest timeout detail. Reconcile is Google/BestTime-read-only — never a
  second POST /forecasts.
- **Error payloads:** extend the transport/bad-response/geo-fallback outcomes to
  include `besttime_message` when the parsed response carried one (the
  monthly-cap path already does this; unify).
- **Metrics:** new `ADD_VENUE_BY_ADDRESS_TOTAL` result labels
  `created_recovered_timeout` and `timeout_unconfirmed`; reuse existing
  BestTime error counters otherwise.

## Data, Config, And API Impact
- Config default change (30→60) — env override still wins.
- 201 body gains optional `recovered_from_timeout: true`; error bodies gain
  optional `besttime_message`. Additive only — the admin panel (and its proxy)
  treat both as optional. No RDS/Redis shape changes; no migration.

## Error Handling And Observability
- Reconcile failures never mask the original timeout: any exception inside the
  reconcile logs a WARNING and falls through to the timeout 502.
- WARNING log when a timed-out create is recovered (venue_id + elapsed), so
  operators can see BestTime slowness trends.
- New metric labels above; no new endpoints.

## Test Plan
Feature file: `tests/bdd/api/add-venue-timeout-recovery.feature`

Scenarios:
- A create that times out but exists in the account inventory is persisted,
  ledger-marked, enriched, and returns created with the recovered marker.
- A create that times out and is absent from the inventory returns the honest
  timeout error, releases the quota slot, and tells the operator a retry is
  duplicate-safe.
- The reconcile read failing (inventory also down) degrades to the timeout
  error — never a second create call.
- Recovery never issues a second POST /forecasts (no double charge).
- Error responses carry BestTime's message when BestTime provided one.
- The add-venue call waits up to the configured 60s before timing out.

Pytest unit tests:
- Timeout classified separately from other transport errors; reconcile
  matching (accent folding, name/address normalization); metric labels;
  config default = 60.

Manual or integration checks:
- Prod after deploy: add a venue during BestTime slowness and observe either a
  60s-window success or a recovered 201.

## Acceptance Criteria
- No path exists where a create confirmed on BestTime's side is dropped by a
  timeout: it is either persisted inline or recovered by the reconcile.
- A timed-out, unconfirmed create returns an honest, actionable error and a
  released quota slot.
- The timeout default is 60s, env-overridable; reads keep their tight timeout.
- Recovery spends zero BestTime credits.

## Open Questions
- None.
