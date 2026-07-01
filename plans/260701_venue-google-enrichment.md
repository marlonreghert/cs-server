# Google-Only Venue Enrichment: Add-Time, Re-Enabled Cron, Pending Backfill

## Branch
feature/venue-google-enrichment

## Goal
Make venues carry real Google metadata (`google_primary_type`/vibe attributes,
opening hours, reviews, business status, rating, price) instead of NULLs. Today a
manually-added venue gets **price only** at add time and the background Google
enrichment is **disabled**, so venues like "Restaurante Beijupirá Olinda" sit with
no `google_places.vibe_attributes` row and NULL type/price/rating. Three coupled
changes, all **Google-only**:

1. **Add-time enrichment** — a manual add is fully Google-enriched inline.
2. **Re-enable background enrichment** — the Google cron runs again, enriching only
   venues that need it.
3. **One-time backfill** — a careful, idempotent pass fills existing *pending*
   venues.

## Non-goals
- **Any BestTime call or credit spend.** No enrichment path may call BestTime
  (`/forecasts`, `/forecasts/live`, `/venues/filter`, `add_venue_to_account`).
  Verified: `google_places_enrichment_service.py` imports/calls no BestTime API.
- vibes_bot / mobile changes. vibes_bot already forwards the selected candidate's
  `place_id` (admin.html:1926 → `POST /api/admin/venues/add` →
  `POST /admin/venues/by-address`), and no new field is surfaced end-to-end — we
  only populate fields the app already consumes. This is **cs-server only**.
- Re-adding venues or changing the BestTime add path (the venue is already created
  in BestTime; enrichment is purely Google on top).

## Evidence
- Add-time is price-only: `app/handlers/add_venue_handler.py` `_persist_new_venue`
  (:378) → `_derive_and_set_price(venue, place_id)` (:406) fetches Google
  `priceLevel`/`priceRange` only; never writes `vibe_attributes`/`google_primary_type`,
  hours, reviews, or business status, and does not persist `place_id`.
- Per-venue enrich already exists and is Google-only:
  `app/services/google_places_enrichment_service.py:153` `enrich_venue(venue_id,
  google_place_id, force_refresh=False)` → `get_place_details` → writes
  `set_vibe_attributes` (incl `google_primary_type`), `set_opening_hours`,
  `set_venue_reviews`, `set_google_business_status`, and price backfill. Returns
  early (`skipped_no_place_id`) if no `place_id`; skips when already cached unless
  `force_refresh`.
- `place_id` resolution exists: `app/api/google_places_client.py`
  `search_place_id(name, address, lat, lng)` (Text Search New).
- Background enrichment disabled in prod: `google_places_enrichment_enabled=False`,
  cron `0 0 1 * *`; job wiring in `main.py:113-144` + `main.py:429-439`; admin
  trigger `JOB_REGISTRY["google_places"]` (`admin_trigger_router.py`).
- Price fallback: `_backfill_venue_review_signal` (:78) calls
  `derive_price_signal(google_enum, google_range, venue.besttime_price_level)`
  (:121) — a *stored* BestTime value (no API call, no credit spend), but not
  strictly "Google-only" (see Open Questions).
- No enrichment-attempt marker exists (`grep` for last_attempt/attempted found
  nothing); `enrich_venue` returns `None` on no-Google-match without recording it,
  so a pending venue with no Google match is re-attempted every run.
- Startup pipelines are a no-op (`main.py` `startup_background_pipelines`), so
  enrichment runs only via cron + admin trigger + the new add-time path.

## Current Behavior
- Manual add persists a venue with Google **price only** (when `place_id` present);
  no type/vibe/hours/reviews/status. `place_id` is used then discarded.
- The Google enrichment cron is disabled; nothing backfills.
- Many active venues have no `vibe_attributes` row and NULL Google fields.

## Desired Behavior
- **Add-time:** after persisting a manual add, resolve `place_id` (via
  `search_place_id` when the request carries none), persist it, and call
  `enrich_venue(venue_id, place_id)` inline so the venue is fully Google-enriched
  immediately. Enrichment failure logs and degrades — the add still succeeds.
- **Cron:** background Google enrichment runs on schedule, `force_refresh=False`
  (skips already-enriched), Google-only, zero BestTime calls.
- **Backfill:** a bounded, idempotent, Google-only pass enriches ONLY *pending*
  venues (active, no `google_primary_type` / no `vibe_attributes` row) that have not
  already been attempted. It never reprocesses enriched venues and does not
  re-attempt venues that had no Google match on a prior run (attempt marker). Logs
  `seen/enriched/skipped_cached/no_google_match/error`.
- All three paths are Google-only: if Google has no data for a venue, its Google
  fields stay NULL — never fall back to BestTime.

## Implementation Approach
**1. Add-time enrichment (`add_venue_handler.py`, `container.py`).**
- Inject `google_places_enrichment_service` into `AddVenueHandler`.
- In `_persist_new_venue` (or right after it), once the venue is persisted:
  resolve `place_id = request.place_id or search_place_id(name,address,lat,lng)`;
  persist it (e.g. on the venue/vibe row); then
  `await google_places_enrichment_service.enrich_venue(venue_id, place_id)`.
- Wrap in try/except: log `[AddVenueHandler] google enrichment failed …` and
  continue (never fail the add). Keep the existing price derivation OR let
  `enrich_venue`'s price backfill own it (avoid double work) — decide during exec.

**2. Re-enable background enrichment (`config.py` / ops).**
- Make the Google enrichment cron active (flip the default of
  `google_places_enrichment_enabled`, or document the prod env flip). Keep
  `force_refresh=False`. Confirm `enrich_all_venues(force_refresh=False)` skips
  already-enriched and makes zero BestTime calls.

**3. One-time backfill of pending venues (new service/trigger + migration).**
- New migration: add an attempt marker — `google_enrich_attempted_at timestamptz`
  (nullable) on `venues.venue` (or a small `google_places.enrich_attempt` row).
- Backfill routine (admin-trigger and/or management entrypoint): select active
  venues where **no `vibe_attributes` row / `google_primary_type IS NULL` AND
  `google_enrich_attempted_at IS NULL`**; for each, `search_place_id` →
  `enrich_venue`; on success the `vibe_attributes` row makes it non-pending; on
  no-Google-match set `google_enrich_attempted_at = now()` so re-runs skip it.
  Bounded batch size + inter-call pacing to respect Google limits; log counts.
- Idempotent: re-running enriches only still-pending, never-attempted venues.

**Google-only price (all three paths).** Change the enrichment price backfill to
derive from Google `priceLevel`/`priceRange` only (pass no `besttime_price_level`
fallback) so an enriched venue's price is NULL when Google has none. (See Open
Questions — this changes shared `_backfill_venue_review_signal` behavior.)

## Data, Config, And API Impact
- **Migration:** add `google_enrich_attempted_at` (nullable) marker.
- **Config:** enable Google enrichment cron (`google_places_enrichment_enabled`).
- **API:** a backfill admin trigger (new `JOB_REGISTRY` entry, e.g.
  `google_places_backfill`, pending-only) or a management entrypoint. `place_id`
  now persisted on add.
- **Serving projection / DTOs:** unchanged keys/shapes — only more rows get
  populated. No vibes_bot/mobile change.

## Error Handling And Observability
- Add-time enrichment failure: WARNING log + graceful continue (add succeeds).
- Reuse `VIBE_ATTRIBUTES_FETCH_RESULTS` labels (`skipped_no_place_id`,
  `skipped_cached`, `error`, success) and add a `no_google_match` / add-time label.
- Backfill emits a summary log (seen/enriched/skipped_cached/no_google_match/error)
  and a runs metric. Assert (in tests) that no BestTime metric/counter moves.

## Test Plan
Feature file: `tests/bdd/enrichment/venue-google-enrichment.feature`

Scenarios:
- A manual add with a `place_id` is fully Google-enriched inline (type/vibe, hours,
  reviews, business status, rating, Google price persisted).
- A manual add without a `place_id` resolves one via Google search, then enriches.
- When Google returns no match / details fail, the add still succeeds and Google
  fields stay NULL (no BestTime fallback).
- The backfill enriches only pending venues (active, no `google_primary_type`),
  skips already-enriched, and on re-run does not re-attempt a no-Google-match
  venue (attempt marker honored).
- No enrichment path issues any BestTime call.
- Google-only price: a venue with no Google price ends with NULL price, not a
  BestTime-derived tier.

Pytest unit tests:
- Add handler wires `enrich_venue` and degrades gracefully on failure.
- Backfill pending-selection query + attempt-marker idempotency.
- Price derivation Google-only in the enrichment path.

Manual or integration checks:
- On a scratch/migrated DB, backfill a fixture set; confirm pending-only + marker.
- Verify against prod after deploy: Beijupirá gains `vibe_attributes`/type.

## Acceptance Criteria
- A newly manually-added venue has a `vibe_attributes` row with
  `google_primary_type` and Google-derived fields immediately after add (when
  Google knows it), without any BestTime call.
- The Google enrichment cron runs and enriches only venues needing it, Google-only.
- The backfill fills pending venues, is idempotent, never reprocesses enriched or
  no-match venues, and makes no BestTime calls.
- Enrichment price is Google-only; NULL when Google has none.

## Open Questions
- **Google-only price fallback:** drop the stored `besttime_price_level` fallback in
  `_backfill_venue_review_signal` entirely (making ALL enrichment price Google-only,
  which can null out price for venues that only had a BestTime tier), or restrict
  Google-only to the new add/backfill paths and leave the shared cron behavior as
  is? User directive says Google-only — confirm the blast radius before executing.
- **Cron enablement:** flip `google_places_enrichment_enabled` default in code, or
  keep code default False and enable via prod env only (ops)?
- **Backfill trigger:** admin endpoint (`google_places_backfill`) vs one-off
  management entrypoint; batch size + Google rate-limit pacing.
- **Attempt marker location:** `venues.venue.google_enrich_attempted_at` vs a
  dedicated `google_places` table.
