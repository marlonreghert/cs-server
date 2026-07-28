# Add Venue Without Forecast

## Branch
feature/add-venue-without-forecast

## Goal
Let an operator add a venue that BestTime registers in our account inventory but
cannot build a forecast for. Today that add fails with a 502 and nothing is
persisted. After this change the venue is persisted with `forecast=false`, enters
the serving projection like any other venue, and reaches vibes_bot — which will
label it "sem informação" once its own change lands.

## Non-goals
- Serving behavior for venues without busyness data. That is entirely
  vibes_bot's `filter_no_busyness` step; cs-server already serves these venues.
- Any change to the eligibility view, the geo-fence, the closure detector, or the
  Redis projection. A venue with `forecast=false` is already eligible and already
  projected — the monthly inventory sync has been creating them since PR #21.
- Any change to live/weekly refresh selection. Refresh is bounded by `priority`,
  not by `forecast`, so a non-forecasted venue is already in the rotation. Whether
  that wastes BestTime credits is a real question, but a pre-existing one — see
  Open Questions.
- Spending any additional paid BestTime call. The reconcile added here uses the
  free account-inventory read only.

## Evidence
- `app/handlers/add_venue_handler.py:274-298` — a non-OK `POST /forecasts`
  releases the manual slot and falls through to `_geo_fallback`.
- `app/handlers/add_venue_handler.py:608-714` — `_geo_fallback` calls
  `/venues/filter` (`busy_min=0`, radius ≤ 50 m) and 502s with
  `besttime_rejected_no_geo_match` when no candidate name-matches. `/venues/filter`
  only returns venues BestTime has foot-traffic data for, so a registered but
  unforecastable venue can never match here. This is the failure the operator sees.
- `app/handlers/add_venue_handler.py:368-500` — `_recover_timed_out_create` already
  solves the shape of this problem for the timeout case: it reconciles against the
  account inventory with `_find_in_account_inventory`, a free paged read guarded by
  exact-folded-name match, a `MIN_CONTAINMENT_MATCH_LEN` floor, and an
  address-token-overlap requirement for containment matches.
- `app/services/venues_refresher_service.py:911` — inventory sync already persists
  `forecast=bool(inv.venue_forecasted)`, so `forecast=false` venues are an
  established, supported state in RDS and in serving.
- `app/models/new_venue.py:134` — `venue_forecasted: bool = False` is already
  parsed off BestTime's venue payloads.
- `migrations/versions/0001_baseline_schemas.py:34` —
  `forecast boolean NOT NULL DEFAULT false` exists in `venues.venue` from the
  baseline. **No migration is required by this plan.**
- `tests/bdd/api/add_venue_by_address.feature:171` — "Inventory sync persists venues
  even when BestTime has no forecast for them yet" pins the equivalent guarantee for
  the crawler path. This plan gives the manual-add path the same guarantee.
- `app/services/batch_add_service.py:53-95` — `_classify` maps handler outcomes to
  batch row results; a new outcome must be added there or batch rows report
  `besttime_rejected_no_geo_match` for what is now a success.

## Current Behavior
`POST /admin/venues/by-address` reserves a monthly slot, calls BestTime
`POST /forecasts`, and on a non-OK, non-monthly-cap response releases the slot and
tries the `/venues/filter` geo fallback. When the submitted venue exists on
BestTime's side but has no foot-traffic forecast, the filter returns no match and
the endpoint answers 502 `besttime_rejected_no_geo_match`. Nothing is persisted, and
the operator has no way to catalog the venue.

## Desired Behavior
When `POST /forecasts` returns a non-OK response that is not the monthly-cap
rejection, cs-server must reconcile the submission against the BestTime account
inventory **before** attempting the geo fallback:

- On an inventory match, the venue must be persisted with `forecast` taken from the
  inventory row's `venue_forecasted`, Google-enriched inline exactly like a normal
  create, address-cached, and returned as `201` with
  `status="created_without_forecast"` and `source="besttime_inventory"`. The monthly
  slot reservation must be kept (not released) and the venue marked as touched — the
  create really did register a venue on BestTime's side.
- On no inventory match, behavior must be unchanged: release the slot, run the geo
  fallback, and return today's outcomes including the 502.
- The reconcile must never issue a second `POST /forecasts` and must never call any
  paid endpoint.
- Membership in the account inventory — not the wording of BestTime's error message
  — must be the discriminator between "registered but unforecastable" and "rejected".

## Implementation Approach
Restructure the failure tail of `_reserve_create_persist` so the slot release moves
below a new reconcile step, then reuse the two helpers that already exist.

1. **Reconcile before release.** On `not _response_ok(response)` and not a monthly-cap
   rejection, call `_find_in_account_inventory(venue_name, venue_address)` — unchanged,
   including its exact/containment guards. A hit routes into a new
   `_finalize_inventory_venue` that builds the `Venue` from the inventory row with
   `forecast=bool(row.venue_forecasted)`, derives the price through
   `_derive_and_set_price`, upserts, and then hands off to the existing
   `_finalize_created_venue` tail (touch ledger, inline Google enrichment, address
   cache, gauge) with `result_label="created_without_forecast"`. A miss falls through
   to `self.budget.release_manual_slot()` and `_geo_fallback` exactly as today.

2. **Bound the inventory read.** `list_account_inventory` is a paged read over the
   whole account (1330+ venues in the pinned BDD fixture). Running it per rejected row
   would make a long batch-add job pathologically slow. Cache the listing in-process
   for `add_venue_inventory_cache_seconds` (new setting, default 300, `0` disables the
   cache) so one batch performs one listing. The cache is keyed by nothing — it is the
   whole inventory — and is invalidated by TTL only. A cache read must never mask a
   listing failure: a failed listing logs, skips the reconcile, and falls through to
   the geo fallback, preserving today's outcome.

3. **Batch triage.** Add `created_without_forecast` to `_classify` in
   `batch_add_service.py` as a 201 outcome alongside `created` and
   `created_recovered_timeout`, so batch summaries count it as a success rather than a
   rejection. It must not be added to `_STOP_OUTCOMES` — it is not a spend-stopping
   state.

4. **Timeout path.** `_recover_timed_out_create` currently hardcodes `forecast=True`
   when building the recovered venue. Take `forecast` from the matched inventory row
   there too, so the two reconcile paths agree.

## Data, Config, And API Impact
- **Migration: none.** `venues.venue.forecast` exists from `0001_baseline_schemas`.
  The serving projection, the eligibility view, and `venues_geo_v1` are untouched.
- **Response contract (additive).** `POST /admin/venues/by-address` gains one new
  201 body shape: `status="created_without_forecast"`, `source="besttime_inventory"`,
  plus the existing venue fields. No existing field changes meaning. Callers that
  branch on `status` must tolerate the new value — inside this repo that is
  `batch_add_service._classify` only.
- **New setting.** `add_venue_inventory_cache_seconds` (int, default 300). Absent or
  `0` disables the cache and reconciles read the inventory live.
- **Persistence.** New venue rows may now carry `forecast=false` from the manual-add
  path. This is not a new state — inventory sync already writes it.

## Error Handling And Observability
- A `list_account_inventory` failure during reconcile must log a warning with the
  submitted venue name and fall through to the geo fallback. It must never turn a
  would-be 502 into a 500.
- The monthly-cap branch keeps priority over the reconcile — a capped account has not
  registered anything and must still return 429 with BestTime's status/message.
- `ADD_VENUE_BY_ADDRESS_TOTAL` gains the `created_without_forecast` result label.
- New counter for reconcile outcomes (`hit` / `miss` / `error`) so an operator can see
  whether the reconcile is earning its latency, and a gauge or counter for cache
  hits vs live listings.
- Log at INFO on a reconcile hit, including the venue id and that the venue was
  persisted without a forecast, so the batch-add operator can find these rows later.

## Test Plan
Feature file: `tests/bdd/api/add-venue-without-forecast.feature`

Scenarios:
- BestTime rejects the create but the venue is in the account inventory without a
  forecast — the response must be 201 `created_without_forecast`, the venue must be
  persisted with `forecast` false, and the monthly slot must stay reserved.
- The reconcile must not call the BestTime add-venue endpoint a second time and must
  not call `/venues/filter`.
- BestTime rejects the create and the venue is absent from the inventory — the
  monthly slot must be released, the geo fallback must run, and today's outcomes
  (`matched_via_geo_fallback` or the 502) must be unchanged.
- A monthly-cap rejection must return 429 without attempting the reconcile.
- A failing inventory listing during reconcile must fall through to the geo fallback
  and must not change the response the operator would have received before.
- An inventory row whose folded name only containment-matches a short submitted name
  must not be linked, preserving the `MIN_CONTAINMENT_MATCH_LEN` guard.
- A batch-add row that resolves to `created_without_forecast` must be counted as a
  success in the job summary and must not stop the job.
- The venue persisted without a forecast must appear in `GET /v1/venues/nearby`.

Pytest unit tests:
- `tests/test_add_venue_handler.py` — reconcile-before-release ordering, slot
  retention on hit vs release on miss, `forecast` sourced from the inventory row,
  and the unchanged geo-fallback path.
- Inventory-listing cache: one listing across N reconciles inside the TTL, a fresh
  listing after expiry, and no caching when the setting is `0`.
- `tests/test_batch_add_service.py` — `_classify` maps the new 201 to
  `created_without_forecast` and it is absent from `_STOP_OUTCOMES`.

Manual or integration checks:
- **Supervised BestTime probe, run exactly once, never by automation.** Submit one
  real venue known to be on Google but with no BestTime foot traffic and capture:
  (a) the exact `POST /forecasts` response body and HTTP status, and (b) whether the
  venue subsequently appears in `GET /venues` with `venue_forecasted=false`. The
  captured body becomes the fixture for the scenarios above. Follows the Probe D
  precedent in `add_venue_by_address.feature`.

## Acceptance Criteria
- An operator adding a registered-but-unforecastable venue receives 201 and the venue
  is queryable through `GET /v1/venues/nearby`.
- No additional paid BestTime call is made on this path; the reconcile uses the free
  inventory read only.
- A rejected add that is genuinely not on BestTime still returns exactly today's
  status code and body.
- A 400-row batch-add performs at most one inventory listing per cache TTL.
- `alembic current` is unchanged by this feature.

## Open Questions
- **Probe outcome.** The design assumes BestTime registers the venue in the account
  inventory even when it answers non-OK on `POST /forecasts`. The supervised probe
  must confirm this before `/execute-feature` runs. If the venue is *not* registered,
  the reconcile can never hit and this approach must be replaced (most likely by
  BestTime's venue-search endpoint, which is paid and would need its own cost review).
- **Does a `forecast=false` venue waste live-refresh credits?** Refresh selection is
  by `priority` only, so these venues are already fetched today. Confirm against the
  BestTime ledger whether a live call on an unforecastable venue draws credit; if it
  does, excluding them from live selection is a follow-up worth its own plan, not part
  of this one.
