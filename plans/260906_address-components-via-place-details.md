# Address components backfill — swap the dead Geocoding rung for Place Details

## Branch
feature/address-components-via-place-details

## Execution corrections (post-approval, applied by `/execute-feature`)

The operator reviewed this plan after approval and mandated three changes
before implementation. All three were applied; this plan's body below is
otherwise unchanged from what was approved, so the two corrected sections
(marked **[CORRECTED]** inline below) reflect what was actually built, not
what was originally proposed.

**(A) Distinct metrics endpoint label — `place_details_address`, not a reuse
of `"place_details"`.** This plan originally recommended reusing the
`"place_details"` endpoint label in `fetch_address_components`'s
`_instrumented(...)` call (mirroring `_recheck_business_status`'s own
precedent). Overridden: `fetch_address_components` uses its own distinct
label, `"place_details_address"`. Reason: this whole feature exists because
the new call sits in the Place Details **Essentials** SKU (10,000 free
calls/month), while the daily vibe-enrichment cron's full-mask call to the
same URL is billed at **Enterprise + Atmosphere** — a different, paid SKU.
Sharing one label would make free-tier consumption unobservable via
`GOOGLE_PLACES_API_CALLS_TOTAL{endpoint=...}` and a cost regression
invisible. `_recheck_business_status`'s own reuse of `"place_details"` is
left untouched — it is an existing gap, not a pattern to repeat here.

**(B) `geocode_by_place_id`, `GoogleGeocodingError`, `GOOGLE_GEOCODING_API_BASE`,
and their dedicated test file are KEPT, not removed.** This plan originally
recommended deleting all of them once `VenueAddressBackfillService` no
longer called `geocode_by_place_id`. Overridden: all four stay in the
codebase, unremoved. `geocode_by_place_id` gained a docstring marker
recording that it is confirmed DEAD — the legacy Geocoding API returned
`REQUEST_DENIED` ("This API is not activated on your API project") for
12/12 production place_ids probed on 2026-09-06 — and must not be wired to
anything without first enabling that API in the GCP Console. Reason: this
PR changes production data-write behavior; the operator wants a small,
easily revertable blast radius. Deleting a client method + exception +
module constant + ~12 tests in the same PR that also flips a production
data path widens that blast radius for zero functional gain. Removing the
now-confirmed-dead code is left as a deliberate, separate follow-up.

**(C) Non-vacuity of the already-merged BDD suite's spend-gate guard —
mandatory, proven during execution.** The already-merged
`tests/bdd/steps/address_components_backfill_steps.py` mocked
`geocode_by_place_id` at 4 call sites, two of which are guard assertions
(`side_effect=AssertionError(...)` for "switch is off", and
`assert_not_called()` for "no Geocoding request was made"). Once
`_maybe_fetch_google_address` stopped calling `geocode_by_place_id`, those
two assertions would have gone vacuously true regardless of whether the
spend gate actually worked. All 4 sites were rewired to
`fetch_address_components`, and a fresh regression guard was added (an
`AsyncMock(wraps=...)` spy wrapped onto `geocode_by_place_id` for every BDD
scenario in `tests/bdd/environment.py`, backing a new
`Then the backfill never calls the legacy geocode_by_place_id method` step)
so the dead path staying in the codebase per correction (B) cannot silently
come back into service. Non-vacuity was then proven by deliberately
disabling the spend-gate's early return in
`VenueAddressBackfillService._maybe_fetch_google_address`, confirming the
guarded scenario failed for real (`ASSERT FAILED: Expected 'mock' to not
have been called. Called 1 times.` against `fetch_address_components`),
then restoring the gate and re-confirming green.

## Goal
Replace the address backfill's Google rung: stop calling the legacy Geocoding
API (`GooglePlacesAPIClient.geocode_by_place_id`), which is confirmed
non-functional against this project's GCP key (12/12 real place_ids probed
returned `REQUEST_DENIED`, the API simply isn't enabled), and call Place
Details (New) with the minimal `id,addressComponents` field mask instead —
already probed 15/15 successful against real residual servable venues,
including venues whose `raw_text` is useless for the parser. Every downstream
piece (mapping, precedence-guarded write, metrics, the spend gate) stays
exactly as it is; only the source of the raw `addressComponents` list changes.

## Non-goals
- Enabling `address_backfill_geocoding_enabled` or triggering the backfill in
  production. This plan ships the code path with the switch left off; turning
  it on and running the sweep is a separate, user-gated rollout step.
- Any change to the parser fallback, the city vocabulary, the precedence rule
  (`operator > google > parsed`), the migration, or the events serving
  projection. All of that is already merged (commit `7722bd9`) and untouched
  here.
- Any change to `enrich_venue`'s own opportunistic `addressComponents` request
  (it already uses `get_place_details`'s full `VIBE_FIELDS_MASK`, billed at
  Enterprise+Atmosphere regardless of this field — see Evidence). This plan
  only touches the backfill's dedicated, minimal-mask Google rung.
- Re-deriving the 10,000-free-requests/month Essentials-tier ceiling from
  Google's docs. That figure is external and was already the basis for the
  original (now-dead) Phase 3 design; nothing here changes the mechanism that
  makes the spend gate default-off and reversible.

## Evidence

**The dead rung and why.**
`GooglePlacesAPIClient.geocode_by_place_id` (`app/api/google_places_client.py:420-482`)
calls `GOOGLE_GEOCODING_API_BASE = "https://maps.googleapis.com/maps/api/geocode/json"`
(`:31`) — the legacy Geocoding API, a different REST family (query-string
`key=` auth, `long_name`/`types` component shape) from every other method on
this client, which all use Places API (New) (`GOOGLE_PLACES_API_BASE =
"https://places.googleapis.com/v1"`, `X-Goog-Api-Key` header). Probed against
12 real production place_ids (2026-09-06): 12/12 `REQUEST_DENIED — "This API
is not activated on your API project"`. It fails safe — raises
`GoogleGeocodingError`, writes nothing (`:454-459`, `:465-468`) — but its
measured yield is zero and there is no reason to expect it will change without
a separate GCP Console action nobody has taken.

Its only caller is `VenueAddressBackfillService._maybe_geocode`
(`app/services/venue_address_backfill_service.py:59-98`), confirmed by
`grep -rn "geocode_by_place_id"` across `app/` — no other production code
calls it. Its own dedicated test file, `tests/test_google_places_geocode_by_place_id.py`
(12 tests), and 5 call sites in `tests/test_venue_address_backfill_service.py`
(`:99,114,119,130,140`) are the only other references.

**The replacement, already probed.** Place Details (New),
`GET https://places.googleapis.com/v1/places/{place_id}` with
`X-Goog-FieldMask: id,addressComponents` — the SAME base URL and auth header
every other method on this client already uses (`get_place_details`,
`get_place_location`, `get_place_photos` all hit `GOOGLE_PLACES_API_BASE`).
Probed 15/15 successful against real residual servable venues (2026-09-06),
including venues whose stored `raw_text` is blank or useless
(`' '`, `'Aracaju Brazil'`) — cases no text parser can ever fix, so this is
real incremental coverage, not a resurfacing of what the parser already gets.
Response shape is the SAME camelCase `longText`/`types` per component that
`app/services/venue_address_components.py::map_address_components` already
consumes (confirmed: `_parse_place_details`, `google_places_client.py:618-621`,
already does `address_components=data.get("addressComponents")` verbatim, no
normalization, for the full-mask opportunistic path) — unlike the Geocoding
API's `long_name`/`short_name` shape, which `geocode_by_place_id` had to
normalize field-by-field (`:474-481`). **No normalization layer is needed for
the new method.**

**Cost tier.** `addressComponents` is billed at the Place Details Essentials
SKU ($5/1000, 10,000 free/month) when requested alone or with only
Essentials-tier fields. The existing full-mask call
(`VIBE_FIELDS_MASK`, `:41-97`) already requests it too, but that call is
billed at the top Enterprise+Atmosphere tier regardless (the comment at
`:47-52` states this explicitly: reviews/priceLevel/priceRange/generativeSummary
already push it there, so one more field changes nothing for that call). The
NEW minimal-mask call this plan adds is a structurally different, cheaper
request — Essentials only — and does not draw against the daily enrichment
cron's Enterprise+Atmosphere spend at all. ~556 backfill calls (see below)
against a 10,000/month Essentials allowance ≈ $0, and the daily vibe
enrichment cron does not compete with this allowance since it is billed under
a different SKU.

**The gate stays a single early-return, unchanged in shape.**
`_maybe_geocode` (`venue_address_backfill_service.py:66-71`) checks
`address_backfill_geocoding_enabled` via `self.admin_config_service.get(...)`
and returns `"disabled"` **before** any client call is reachable — this
early-return is unconditional on which client method the `try` block below it
calls. `AdminConfigService.get()` (`app/services/admin_config_service.py:55-68`)
returns `None` for a key that was never set, and `bool(None)` is `False`, so
the switch is default-off with no separate default-registration step —
confirmed by `grep` finding no entry for this key in `app/container.py`'s
validator wiring (unlike `address_city_vocabulary`, which does have one).
Rewiring only the line inside the `try` block that calls the client
(`:84`) leaves this gate structurally identical: a paid call stays
categorically unreachable while the switch is off.

**Cost ceiling per run, confirmed from code.** `settings.address_backfill_batch_size
= 200` (`app/config.py:293`). The admin-trigger runner,
`_run_address_components_backfill` (`app/routers/admin_trigger_router.py:105-115`),
calls `backfill_batch(limit=cfg.get("limit"))` — ONE bounded batch per
trigger, no auto-repeat loop (docstring at `:106-113` states this explicitly:
"the same shape as every other batch job here... there is no auto-repeat loop
inside a single call"). `backfill_batch` (`venue_address_backfill_service.py:170`)
falls back to `settings.address_backfill_batch_size` when no explicit `limit`
is given. So one trigger call processes at most 200 rows, and only the subset
of those 200 that both have a null field, have a stored `google_place_id`,
and the switch is on ever reach a paid HTTP call — an upper bound, not a
typical count. `address_components_backfill` is in `job_lock.LOCKED_JOB_NAMES`
(`app/services/job_lock.py:34-38`) already, so an admin trigger cannot race a
scheduled run — no change needed there, this plan adds no new job.

**The re-sweep mechanism, already designed for, not yet documented anywhere
operator-visible.** `ADMIN_CONFIG_CURSOR_KEY = "address_backfill_cursor"`
(`venue_address_backfill_service.py:49`) is a small JSON blob
(`{"last_venue_id", "processed_total", "written_total"}`) read at
`backfill_batch`'s start (`:172-174`) and written once per batch (`:198-199`).
`backfill_batch` only ever selects rows with `venue_id > cursor`
(`list_address_backfill_candidates(after_id, effective_limit)`, `:176`), so
once a full sweep completes, the cursor sits at the catalog's last `venue_id`
and a fresh trigger selects nothing new — confirmed by the docstring at
`:44-48` itself: "an operator can still reset it by hand (e.g.
`{"last_venue_id": null}`... via the existing generic
`PUT /admin/config/{key}`)". That generic endpoint is
`PUT /admin/config/{key}` (`admin_trigger_router.py:1297-1315`, router prefix
`/admin` at `:39`) — so the concrete operator action is
`PUT /admin/config/address_backfill_cursor` with body
`{"last_venue_id": null}`. **This is not currently written down anywhere an
operator would see it** — `grep` across `docs/`, `DEPLOYMENT.md`, `README.md`
found no mention of either `address_backfill_geocoding_enabled` or
`address_backfill_cursor`. The one place operators already look for a job's
own operational quirks is the `JOB_REGISTRY["address_components_backfill"]["description"]`
string (`admin_trigger_router.py:200-208`) — every other job in this registry
already documents its own gotchas there (e.g. `instagram_profile_photos`'s
entry tells the operator to price a mode first via a specific endpoint,
`:304-309`), and that string is what `GET /admin/jobs` surfaces to the admin
panel. This plan's Implementation Approach appends the reset instructions
there rather than inventing a new doc location.

**`enrich_all_venues` is confirmed unaffected — the cron is not the vehicle,
and this plan does not change that.** `enrich_all_venues`
(`app/services/google_places_enrichment_service.py:567-629+`) skips any venue
whose `vibe_attributes` row already exists (`:613-615`), running only a
`businessStatus`-only recheck (`_recheck_business_status`, `:343-376`,
`fields_mask="businessStatus"` at `:360`) — never `addressComponents` — for
those. This is exactly why the 547 null-address, already-enriched venues never
got Google's answer through the opportunistic path, and it is precisely why
the backfill job (not the cron) is the vehicle for the residual. This plan
touches only `GooglePlacesAPIClient` (new method) and
`VenueAddressBackfillService` (rewired call); `enrich_all_venues` is not
imported or referenced by either. **Consequence, unchanged by this plan**: a
venue that is already enriched and gets its address filled ONLY via a
backfill batch trigger, never automatically by the daily cron — that is the
existing, already-documented design (the prior plan's own Evidence section:
"Opportunistic only... a one-off backfill... is a separate, costed decision"),
not a new gap this plan introduces or is expected to close.

**No validator/config change needed for the gate itself.** `grep` for
`ADMIN_CONFIG_GEOCODING_ENABLED_KEY`/`address_backfill_geocoding_enabled` in
`app/container.py` finds no registered validator (unlike
`address_city_vocabulary`) — the `bool(...)` coercion happens inline in
`_maybe_geocode` itself. Unaffected by this plan.

**The existing, already-merged BDD suite mocks the method being replaced —
this is a lockstep-update hazard, not optional cleanup.**
`tests/bdd/steps/address_components_backfill_steps.py` (backs the
already-merged, non-`@wip` `tests/bdd/enrichment/address-components-backfill.feature`)
sets `context.google_places_client.geocode_by_place_id = AsyncMock(...)` at
four call sites (`:33-40` "Geocoding returns..." step, `:43-48` "switch is
off" step, `~178` the rerun/"unchanged" step, `~363-364` the
"no Geocoding request was made" then-step). Once `_maybe_geocode` is rewired
to call the new method, these mocks target an attribute production code never
touches again: the "switch is off" guard and the "no Geocoding request was
made" assertion become **vacuously true** regardless of whether a real paid
call happened, and the "Geocoding returns..." / rerun steps feed a
plain, un-mocked `AsyncMock` return value (not the programmed component list)
into the real code path, which no longer receives the response the scenario
intends to test. This must be fixed in the SAME change that rewires the
client call, or the merged suite silently stops testing what it claims to.

## Current Behavior
`VenueAddressBackfillService._maybe_geocode` calls
`GooglePlacesAPIClient.geocode_by_place_id(place_id)`, which always fails with
`GoogleGeocodingError` in production (API not enabled for this project),
recorded as `VENUE_GEOCODING_REQUESTS_TOTAL{outcome="api_error"}`. The row's
nulls are left untouched (never poisoned) and the parser fallback still runs
unconditionally — so the backfill is not broken, it simply never gets a
`google`-sourced answer. `neighborhood_source` is 3,053 `parsed` / 0 `google`
across 3,600 rows; 547 rows still have no filled address at all.

## Desired Behavior
`VenueAddressBackfillService`'s Google rung calls a new
`GooglePlacesAPIClient.fetch_address_components(place_id)` method that hits
Place Details (New) with the minimal `id,addressComponents` mask. On a
genuine "Google has nothing for this place" it returns `None`/an empty
answer, exactly like today's contract; on any transport/quota/API failure it
raises, exactly like today's contract — so `write_mapped_components(...,
source="google")`, the precedence rule, and every metric outcome label behave
identically to today, just fed by a working Google response instead of a
guaranteed failure. The spend gate (`address_backfill_geocoding_enabled`,
default off) still makes the new call categorically unreachable when off.
`geocode_by_place_id` and its now-fully-unreachable Geocoding API integration
are **[CORRECTED — see "Execution corrections" above] kept, not removed** —
`geocode_by_place_id`, `GoogleGeocodingError`, and `GOOGLE_GEOCODING_API_BASE`
stay in the codebase (dead, unwired, docstring-marked), a deliberate,
separate follow-up. The re-sweep mechanism (cursor reset) is documented in
the one place an operator already looks for this job's operational notes.

## Implementation Approach

**New client method: `GooglePlacesAPIClient.fetch_address_components(place_id)`**
(`app/api/google_places_client.py`, alongside `get_place_details`/
`get_place_location`). A new field-mask constant,
`ADDRESS_COMPONENTS_FIELDS_MASK = "id,addressComponents"`, sibling to the
existing `PHOTOS_FIELDS_MASK`/`VIBE_FIELDS_MASK`. Accepts either a bare place
id or the `"places/..."` resource name, mirroring `get_place_details`'s own
handling (`:372-376`).

This is a bespoke implementation, not a call into `get_place_details` with a
custom mask, because `get_place_details`'s existing exception handling
(`:400-418`) swallows every failure mode — 403 (permission/quota), any other
HTTP error, timeout, and connection error — into a plain `return None`,
identical to how it treats a genuine 404. That is the exact poisoning risk
`GoogleGeocodingError`'s own docstring exists to prevent (`:110-120`): a
transient outage must never look like a real "Google answered: nothing here."
`get_place_details`'s contract is correct for its own callers (the
opportunistic enrich path and the business-status recheck, both of which
already treat "no details" as "skip this venue, try again later" at their own
call sites) and is out of scope to change here.

The new method's error handling mirrors `get_place_details`'s STRUCTURE
(the HTTP call and `raise_for_status()` inside `self._instrumented("place_details")`,
so a 404 raises `httpx.HTTPStatusError` there and is caught by an outer
`except` clause) but inverts the OUTCOME for everything except 404: a 404
still means "Google has nothing for this place_id" and returns `None` (same
semantics `get_place_details:402-403` already gives it); every other
`httpx.HTTPStatusError`, `httpx.TimeoutException`, and `httpx.RequestError`
raises a new `GoogleAddressLookupError` (mirroring `GoogleGeocodingError`'s
own docstring contract almost verbatim, updated for Place Details) instead of
returning `None`. On a clean 200, the method returns `data.get("addressComponents")`
directly — `None` or a list, both already exactly what
`map_address_components` expects, no normalization. Endpoint label passed to
`_instrumented(...)`: **[CORRECTED — see "Execution corrections" above] a
DISTINCT label, `"place_details_address"`, NOT a reuse of `"place_details"`.**
This plan originally proposed reusing `"place_details"`, matching the
precedent `_recheck_business_status`'s minimal `fields_mask="businessStatus"`
call (`google_places_enrichment_service.py:359-361`) already sets — it is
genuinely the same REST endpoint (`GET /v1/places/{id}`), just a different
mask. That precedent is left unchanged. But this NEW call is different from
both of those in one way that matters for cost observability: it is billed
at the Essentials SKU (10,000 free/month), while both `get_place_details`'s
own full-mask call and `_recheck_business_status`'s reuse of its label are
billed at Enterprise+Atmosphere — a different, paid SKU. Reusing the label
would mix a free-tier call's volume into a paid-tier metric series,
making free-tier consumption unobservable via
`GOOGLE_PLACES_API_CALLS_TOTAL{endpoint=...}` and any cost regression
invisible. The dedicated, already-existing `VENUE_GEOCODING_REQUESTS_TOTAL{outcome}`
counter (kept, see below) remains the PRIMARY metric an operator watches
against the Essentials free-tier ceiling — it counts backfill attempts, not
raw HTTP calls — but the distinct `"place_details_address"` label is now
also available as a secondary, client-level corroborating signal, at zero
cost.

**Rewire `VenueAddressBackfillService`.** `_maybe_geocode` is renamed to
`_maybe_fetch_google_address` (confirmed by `grep` to have zero external
callers — only referenced from within this same file — so the rename is
purely internal and touches no test or other caller) so its name stops
describing a call it no longer makes. The single line that calls the client
(`components = await self.google_places_client.geocode_by_place_id(place_id)`)
becomes `await self.google_places_client.fetch_address_components(place_id)`.
Everything else in the method — the `enabled` gate check, the `no_place_id`
check, the `try`/`except Exception`/`VENUE_GEOCODING_REQUESTS_TOTAL` labeling,
the call into `write_mapped_components(..., source="google")` — is
byte-for-byte unchanged. The returned dict key `result["geocoding_outcome"]`
(read by `backfill_batch:183` and by every existing test) is **deliberately
left unrenamed** — it has multiple call sites and renaming it buys no
behavior change, only diff noise; `_maybe_geocode` had zero callers and
`result["geocoding_outcome"]` has several, which is exactly why one gets
renamed and the other doesn't.

**Metrics: keep names and outcome labels, update only the prose.**
`VENUE_GEOCODING_REQUESTS_TOTAL`'s name and its `{success, no_place_id,
api_error, disabled}` outcome labels stay exactly as they are (task
requirement, and this repo's own conservative posture toward renaming
anything a dashboard could already reference) — only its HELP text and code
comment (`app/metrics.py:2056-2074`) are updated to describe "the
address-components Place Details lookup" instead of "Geocoding-by-place_id."
`address_backfill_geocoding_enabled` (the admin-config key string) is
UNCHANGED, per the task's own reasoning: renaming it risks a silent
default-off on any code path that still reads the old key name.

**Keep `geocode_by_place_id` and its dead integration surface — do NOT
remove it. [CORRECTED — see "Execution corrections" above; this whole
section is superseded by that override.]** This plan originally recommended
**removing** `geocode_by_place_id` (`google_places_client.py:420-482`),
`GoogleGeocodingError` (`:110-120`), `GOOGLE_GEOCODING_API_BASE` (`:31`), the
comment block introducing it (`:23-30`), and its dedicated test file,
`tests/test_google_places_geocode_by_place_id.py` (12 tests) — reasoning
that it is confirmed non-functional (12/12 `REQUEST_DENIED`), its only
caller is being rewired by this very plan, and a reachable-looking,
permanently-dead method is an attractive nuisance.

Overridden by the operator post-approval: all four are **kept**, unremoved.
`geocode_by_place_id` instead gained a docstring marker — "DEAD — DO NOT
WIRE THIS TO ANYTHING", recording the 12/12 `REQUEST_DENIED` probe result
and the date, and stating that re-enabling the Geocoding API SKU in the GCP
Console is a prerequisite before ever wiring it back into a caller. Its
dedicated test file is untouched (still exercises its own contract; it was
never testing anything about `fetch_address_components`, so nothing about
this feature's own correctness depends on that file either way). Reason for
the override: this PR already changes production data-write behavior (the
address backfill's Google rung), and the operator wants a small, easily
revertable blast radius for that change — deleting a client method + an
exception class + a module constant + ~12 tests in the SAME PR widens the
blast radius for zero functional gain. If the Geocoding API SKU is ever
enabled for this project later, `geocode_by_place_id` is already sitting
there, tested, ready to re-verify against real place_ids — cheaper than
recovering it from git history. Removing it now that it is confirmed dead
remains a reasonable idea; it is just a separate, deliberate follow-up PR,
not bundled with this one.

**Fix the existing merged BDD suite in lockstep (not optional).**
`tests/bdd/steps/address_components_backfill_steps.py`'s four
`context.google_places_client.geocode_by_place_id = AsyncMock(...)`
assignments (Evidence, above) are updated to
`context.google_places_client.fetch_address_components = AsyncMock(...)`,
and the module docstring's "Google's answer travels through
`geocode_by_place_id`... a DIFFERENT client method with its own
already-normalized shape" is corrected — the new method also returns the
already-normalized `longText`/`types` shape, so that specific contrast no
longer holds (both `fetch_address_components` and `get_place_details` now
return the identical wire shape; they still differ in field mask/cost tier,
which is the distinction worth keeping). This must land in the same PR as the
client/service rewire — otherwise the existing, currently-green scenarios
either silently stop testing anything real or start failing outright.

**Terminology sweep (comments/docstrings only, no behavior change).**
Update "Geocoding"/"geocode" language that now describes the wrong mechanism:
`venue_address_backfill_service.py` module docstring (`:1-19`) and
`ADMIN_CONFIG_GEOCODING_ENABLED_KEY`'s comment (`:39-42`, "Every Geocoding
call site checks this before calling geocode_by_place_id"); `app/config.py`'s
`address_backfill_*` settings block comment (`:287-292`); `app/container.py`'s
`VenueAddressBackfillService` construction comment (`:1031-1034`). None of
these are load-bearing for behavior — they are flagged because a stale
comment claiming "Geocoding" would actively mislead the next person who reads
this service.

**Document the re-sweep operationally, at the one place operators already
look.** Append to `JOB_REGISTRY["address_components_backfill"]["description"]`
(`admin_trigger_router.py:200-208`): replace "Google (Geocoding by the
venue's already-stored place_id)" with "Google (Place Details
address-components lookup by the venue's already-stored place_id, minimal
id,addressComponents mask — Essentials SKU)", and add a sentence stating the
exact reset action: `PUT /admin/config/address_backfill_cursor` with body
`{"last_venue_id": null}` to re-sweep the whole catalog from the start.

## Data, Config, And API Impact
- **No new settings, no new admin-config keys, no new migration, no new API
  endpoints.** This plan reuses every piece of config/persistence/routing
  infrastructure the prior plan already built.
- `GooglePlacesAPIClient` gains one public method (`fetch_address_components`)
  and one new exception class (`GoogleAddressLookupError`).
  **[CORRECTED — see "Execution corrections" above]** does NOT lose
  `geocode_by_place_id`, `GoogleGeocodingError`, or `GOOGLE_GEOCODING_API_BASE`
  — all three are kept (dead, unwired, docstring-marked) per the operator's
  override.
- `VenueAddressBackfillService._maybe_geocode` renamed to
  `_maybe_fetch_google_address` (private, zero external callers). Its
  returned outcome-label contract and the public `process_one`/`backfill_batch`
  surface are unchanged.
- `admin_trigger_router.JOB_REGISTRY["address_components_backfill"]["description"]`
  text updated (operator-facing string only, not a schema change).
- No change to `venues.address` schema, `update_venue_address_components`'s
  precedence SQL, `map_address_components`, or any Redis key.

## Error Handling And Observability
- `fetch_address_components` raises `GoogleAddressLookupError` on any
  transport/quota/API failure (HTTP error other than 404, timeout, connection
  error) and returns `None` only for a genuine 404 ("Google has nothing for
  this place_id") — mirrors `geocode_by_place_id`'s contract precisely, so
  `_maybe_fetch_google_address`'s existing `except Exception` /
  `VENUE_GEOCODING_REQUESTS_TOTAL{outcome="api_error"}` handling needs no
  change to keep working correctly.
- `VENUE_GEOCODING_REQUESTS_TOTAL{outcome}` keeps its exact name and label
  set (`success`/`no_place_id`/`api_error`/`disabled`) — an operator watching
  Grafana sees the same series continue, now with real `success` counts
  instead of only `api_error`/`disabled`.
- **[CORRECTED — see "Execution corrections" above]**
  `GOOGLE_PLACES_API_CALLS_TOTAL`/`_DURATION_SECONDS`/`_ERRORS_TOTAL` use a
  DISTINCT `endpoint="place_details_address"` label for this new call, kept
  separate from `endpoint="place_details"` — so the cheap Essentials-tier
  call's volume/latency/error signal never mixes into the existing
  Enterprise+Atmosphere-tier series (see Implementation Approach).
- The spend gate (`address_backfill_geocoding_enabled`, default off) makes
  the new call categorically unreachable while off, by construction (same
  early-return the current code already has) — no new runtime path exists
  while the switch stays off, which this plan does not change.
- A venue whose already-enriched `vibe_attributes` row predates this change
  gets its address filled only via a backfill batch trigger, never
  automatically by the daily `enrich_all_venues` cron (unchanged,
  pre-existing design — see Evidence).

## Test Plan
Feature file: `tests/bdd/enrichment/address-components-via-place-details.feature`

Scenarios:
- Google's Place Details answer fills the bairro for a venue with a stored
  place id and no stored address components (main path, `source="google"`).
- A response with no address components is a normal outcome, not an error —
  the parser still fills what it can.
- A Place Details transport/quota failure leaves the row's nulls untouched;
  it is never recorded as a real answer.
- The backfill makes no Place Details address lookups while the free-tier
  switch is off, and the parser still fills what it can (spend-gate
  unreachability, renamed from the existing "no Geocoding request" scenario's
  intent).
- Google's Place Details answer still outranks a previously parsed bairro,
  and an operator-entered bairro is still never overwritten (precedence
  preserved through the swap).
- One backfill trigger never processes more than the configured batch size
  (cost ceiling).
- An operator resets the backfill cursor and the catalog becomes selectable
  again from the start (the re-sweep mechanism).

Pytest unit tests:
- `tests/test_google_places_fetch_address_components.py` (new; **[CORRECTED]**
  a SIBLING to `tests/test_google_places_geocode_by_place_id.py`, which is
  KEPT, not deleted, per correction (B)): success with components; success
  with a 200 response carrying no `addressComponents` key (returns `None`,
  not an error); 404 → `None`; 403/429/500/503 → raises
  `GoogleAddressLookupError`; timeout → raises; connection error → raises;
  header/field-mask/URL shape assertions; both bare and `"places/"`-prefixed
  place ids accepted.
- `tests/test_venue_address_backfill_service.py` (updated): every
  `client.geocode_by_place_id` reference becomes `client.fetch_address_components`
  (6 call sites found during execution, not the 5 estimated here); the
  existing `success`/`api_error`/`disabled`/`no_place_id` scenarios keep
  their current assertions unchanged — only the mocked method name changes,
  since the contract is identical.
- `tests/bdd/steps/address_components_backfill_steps.py` (updated, backs the
  ALREADY-MERGED, non-`@wip` feature): all four
  `geocode_by_place_id`-mocking call sites become
  `fetch_address_components` — required so the existing suite keeps testing
  the real code path instead of a mock nothing calls anymore (see Evidence).
  **[Added during execution, correction (C):]** `tests/bdd/environment.py`
  now wraps every scenario's `context.google_places_client.geocode_by_place_id`
  in an `AsyncMock(wraps=...)` spy (harmless if ever genuinely reached), and
  a new `Then the backfill never calls the legacy geocode_by_place_id method`
  step asserts against it — a regression guard proving the dead path stays
  dead, verified non-vacuous by deliberately breaking the spend gate and
  confirming the guarded scenario failed for real before restoring it (see
  "Execution corrections" above).

Manual or integration checks:
- None required to merge this plan's code changes (the switch ships off).
  The Phase-3-style free-tier sample run (enable the switch, trigger with a
  small `{"limit": N}`, watch `VENUE_GEOCODING_REQUESTS_TOTAL{success}` and
  the events UI's bairro field for a handful of real venues) is a separate,
  user-gated rollout step, not part of this plan's acceptance criteria.

## Acceptance Criteria
- `GooglePlacesAPIClient.fetch_address_components` exists, hits Place Details
  with the `id,addressComponents` mask, returns `None` only for a genuine
  404, and raises `GoogleAddressLookupError` on every other transport/quota/
  API failure.
- **[CORRECTED — see "Execution corrections" above]** `geocode_by_place_id`,
  `GoogleGeocodingError`, and `GOOGLE_GEOCODING_API_BASE` are KEPT (not
  removed), with `geocode_by_place_id` carrying a docstring marking it DEAD;
  no PRODUCTION code calls it anywhere (confirmed by `grep`) — only its own
  dedicated test file and the BDD regression-guard spy reference it.
- `VenueAddressBackfillService`'s Google rung calls
  `fetch_address_components`; `map_address_components`,
  `write_mapped_components(..., source="google")`, the precedence rule, and
  `VENUE_GEOCODING_REQUESTS_TOTAL`'s name/outcome labels are unchanged.
- `address_backfill_geocoding_enabled` still gates every reachable path to
  the new client call, by the same early-return structure as today, and
  ships **off**.
- The already-merged `tests/bdd/enrichment/address-components-backfill.feature`
  suite still passes, exercising the real `fetch_address_components` mock —
  not a vacuously-unused `geocode_by_place_id` stub.
- The new `@wip` feature file's scenarios pass once `/execute-feature` wires
  its steps, then the `@wip` tag is removed.
- `JOB_REGISTRY["address_components_backfill"]`'s description states both
  the new mechanism (Place Details, not Geocoding) and the exact cursor-reset
  action.
- No production trigger of the backfill job and no flip of
  `address_backfill_geocoding_enabled` happens as part of this plan's
  execution.

## Open Questions
None block implementation — the switch ships off, so this plan is fully
buildable, testable, and mergeable without any production rollout decision.
The following are rollout-gate items for a later, explicit, user-approved
step, not design questions:
- Whether the Essentials SKU's 10,000-free-requests/month figure holds in
  practice for this GCP project is an external claim (Google's public docs),
  not verifiable from this repo. The dedicated `VENUE_GEOCODING_REQUESTS_TOTAL{success}`
  counter is the in-repo corroborating signal an operator watches during the
  rollout's own small sample run — this plan does not perform that rollout.
- Whether to also rename `VENUE_GEOCODING_REQUESTS_TOTAL` (the metric name
  itself, not its labels) for accuracy is left to the executor's judgment;
  this plan recommends keeping it unchanged to avoid any dashboard drift,
  since nothing in production has ever recorded a real `success` under this
  name yet (0 `google`-sourced rows today) and the label semantics already
  say everything a dashboard needs.
