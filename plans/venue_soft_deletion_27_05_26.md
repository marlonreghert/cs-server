# Venue Soft Deletion And Admin Inventory

## Branch
feature/venue-soft-deletion

## Goal
Replace the Google Places permanently closed hard-delete path with a
soft-delete lifecycle state that preserves existing Redis venue data for
troubleshooting, while keeping deprecated venues out of public nearby
responses and enrichment jobs.

Expose deprecated venues through an admin API surface that vibes_bot can call
to inspect permanently closed venues and their cache state.

The deployment must not flush, reset, rename, rebuild, or reprocess the current
Redis venue dataset.

## Non-goals
- Do not hard-delete any venue because Google Places marks it permanently or
  temporarily closed.
- Do not soft-delete temporarily closed venues. They must remain active so
  live busyness refreshes can keep running and public nearby responses can
  show them when data is available.
- Do not migrate Redis keys, rename `venues_geo_v1`, rename
  `venues_geo_place_v1:<venue_id>`, or require a backfill before serving
  existing data.
- Do not reset production Redis, run a production Redis flush, or replay the
  full venue inventory during deployment.
- Do not add a public closed-venue endpoint. The access surface belongs under
  `/admin` for the vibes_bot admin panel.
- Do not build the vibes_bot UI in this cs-server change. This plan only
  provides the cs-server API contract vibes_bot can consume.
- Do not implement manual restore/reactivation in V1. Deprecated venues remain
  deprecated unless a later plan explicitly defines a restore workflow.

## Evidence
- `app/services/google_places_enrichment_service.py:114` checks Google Places
  permanent closure, and `:121` calls `self.venue_dao.delete_venue(venue_id)`
  when `settings.remove_permanently_closed_venues` is enabled.
- `app/services/google_places_enrichment_service.py:133` checks temporary
  closure, and `:140` calls `self.venue_dao.delete_venue(venue_id)` when
  `settings.remove_temporarily_closed_venues` is enabled.
- `app/dao/redis_venue_dao.py:79` implements `delete_venue`; `:104` removes
  the venue from `venues_geo_v1`, `:108` deletes the venue JSON key, and
  `:111` through `:138` delete live forecast, vibe attributes, weekly
  forecasts, photos, opening hours, Instagram, reviews, menu data, and vibe
  profile records.
- `app/config.py:115` through `:123` document the current closure settings as
  removing closed venues, with both permanent and temporary removal defaults
  set to true when enrichment runs.
- `app/metrics.py:129` through `:149` expose "removed" closed-venue metrics,
  but there is no soft-delete/deprecated metric.
- `app/dao/redis_venue_dao.py:251` and `:265` list every venue key without a
  lifecycle filter. Current enrichment paths use these lists directly.
- `app/services/photo_enrichment_service.py:118`, `app/services/instagram_enrichment_service.py:209`,
  `app/services/instagram_posts_enrichment_service.py:43`,
  `app/services/menu_photo_enrichment_service.py:287`,
  `app/services/vibe_classifier_service.py:235`, and
  `app/services/venues_refresher_service.py:978` and `:998` all enumerate
  all venue IDs for enrichment or forecast refresh work today.
- `app/handlers/venue_handler.py:117` loads nearby venues from the geo index
  and filters blocked types and names, but does not filter deprecated venues.
- `app/routers/admin_trigger_router.py:87` already exposes an admin
  `inventory_sync` job, and `:300` exposes an admin venue type breakdown,
  so a new admin inventory inspection endpoint fits the existing router.
- `main.py:102` describes Google Places enrichment as removing permanently
  closed venues, and `main.py:550` force-refreshes on startup when
  `remove_permanently_closed_venues` is true. The startup path must stop being
  capable of hard-deleting venue data.
- `config.example.json:27` through `:33` still documents permanent closure
  removal and sets `remove_permanently_closed_venues` to true.

## Current Behavior
- Google Places enrichment can permanently remove a venue from Redis when
  Google reports `CLOSED_PERMANENTLY`.
- Google Places enrichment can also remove a venue from Redis when Google
  reports `CLOSED_TEMPORARILY`.
- The hard-delete removes the venue's geo member, venue JSON, live forecast,
  weekly forecasts, vibe attributes, photos, opening hours, Instagram cache,
  reviews, menu photos, menu data, and vibe profile.
- Public nearby responses and enrichment jobs have no lifecycle status concept.
  A venue is either present in the normal Redis keys or gone.
- Admin endpoints can inspect aggregate venue type data, but there is no
  admin endpoint for listing deprecated venues or seeing why a venue stopped
  being used.

## Desired Behavior
- Google Places closure handling must mark permanently closed venues as
  deprecated instead of deleting them.
- Google Places temporary closure handling must not mark venues as deprecated.
  `CLOSED_TEMPORARILY` venues remain active, keep their Redis data, stay
  eligible for live busyness refresh, and remain visible in
  `GET /v1/venues/nearby` when live busyness is available.
- Soft-deleting a venue must preserve the existing venue key, geo member, and
  all associated cache keys.
- Missing lifecycle metadata on legacy Redis records must mean active.
- Deprecated venues must be excluded from:
  - `GET /v1/venues/nearby`
  - live forecast refresh
  - weekly forecast refresh
  - Google Places enrichment
  - photo enrichment
  - Instagram discovery
  - Instagram posts scraping
  - menu photo enrichment
  - menu extraction
  - vibe classification
  - Instagram handle validation
- Deprecated venues must remain inspectable through an admin inventory endpoint
  that vibes_bot can call.
- Inventory sync and venue discovery upserts must preserve existing deprecated
  lifecycle metadata for a venue ID and must not reactivate it implicitly.
- Deployment must be read-compatible with existing Redis data and must require
  no data reset or migration.

## Implementation Approach
- Extend `app/models/venue.py` with optional lifecycle fields on `Venue`:
  `lifecycle_status` defaulting to `active`, `deprecated_at`,
  `deprecated_reason`, `deprecated_source`, and `google_business_status`.
  Existing Redis venue JSON without these fields will validate as active.
- Add a small helper on `Venue`, or a local utility near the DAO boundary, for
  active/deprecated checks so all services use the same rule.
- Add DAO methods in `app/dao/redis_venue_dao.py`:
  - `soft_delete_venue(...)`: load the existing venue JSON, set deprecated
    lifecycle fields, and write it back through the existing venue key and
    geo member. It must not call `delete_venue`, `zrem`, or cache delete
    helpers.
  - `list_active_venue_ids()` and `list_active_venues()` for background jobs.
  - `list_deprecated_venue_ids()` and an admin-oriented listing method that
    supports `status=active|deprecated|all`, a text query, pagination limit,
    and cursor.
  - `get_nearby_venues(..., include_deprecated=False)` so public nearby
    serving remains active-only while admin/debug code can opt in.
- Keep the existing `delete_venue` method for explicit future maintenance use,
  but remove it from Google Places closure handling. Add tests to prove the
  closure path does not call it.
- Change `GooglePlacesEnrichmentService.enrich_venue` so
  `CLOSED_PERMANENTLY` calls `soft_delete_venue` when permanent closure
  handling is enabled. Preserve the current permanent-closure setting as a
  backward-compatible closure-handling toggle, but update comments and
  example config so it no longer promises hard removal.
- Change the `CLOSED_TEMPORARILY` path so it records Google business status
  for troubleshooting, does not call `soft_delete_venue`, does not call
  `delete_venue`, and continues the normal enrichment/cache path. Keep
  `remove_temporarily_closed_venues` accepted for config compatibility, but
  make it deletion/deprecation inert.
- Update every all-venue enrichment and refresh path to enumerate active IDs
  only. Each job should log the number of deprecated venues skipped when that
  count is nonzero.
- Update public venue serving in `VenueHandler` so deprecated venues are
  filtered before merge, sorting, and transformation.
- Add admin endpoint `GET /admin/venues/inventory` in
  `app/routers/admin_trigger_router.py` or a new admin venues router wired
  through the existing container:
  - Query parameters: `status=active|deprecated|all` default `active`,
    optional `q`, `limit` capped to a safe value, and optional `cursor`.
  - Response: `items`, `next_cursor`, and `counts`.
  - Each item includes venue id, name, address, latitude, longitude,
    lifecycle status, deprecated reason, deprecated source, deprecated
    timestamp, Google business status, and cache-presence flags for live,
    weekly, vibe attributes, photos, opening hours, Instagram, reviews, menu
    data, and vibe profile.
- Add Prometheus metrics:
  - `venues_soft_deleted_total{reason,source}` counter.
  - `venues_deprecated_total` gauge.
  - Optional job-level skipped counts through existing background job logs, or
    a `venues_enrichment_skipped_total{job,reason}` counter if the existing
    metrics style fits cleanly.
- Update startup/job log text so it no longer says Google Places removes
  closed venues.
- Update `config.example.json` comments and `app/config.py` comments to make
  clear that permanent closure handling soft-deprecates, while temporary
  closure handling must not delete or deprecate.

## Data, Config, And API Impact
- Existing Redis venue keys remain unchanged:
  - `venues_geo_v1`
  - `venues_geo_place_v1:<venue_id>`
  - all associated live, weekly, vibe, photo, opening-hours, Instagram,
    review, menu, and vibe-profile key formats.
- Existing venue JSON gains optional lifecycle fields only when a venue is
  soft-deleted, newly upserted after the model change, or refreshed with a
  Google business status. Missing lifecycle fields are treated as active.
- No Redis flush, no data migration, no key rename, no deployment-time
  reprocessing, and no mandatory inventory rebuild.
- `GET /v1/venues/nearby` response shape does not change for active venues;
  deprecated venues are omitted.
- New admin API: `GET /admin/venues/inventory`.
- The existing `remove_permanently_closed_venues` setting name remains
  accepted for compatibility, but its behavior becomes soft-deprecation.
- The existing `remove_temporarily_closed_venues` setting name remains
  accepted for compatibility, but it must no longer delete or deprecate
  temporary closures.

## Error Handling And Observability
- If Google Places returns no details or an error, keep the current behavior:
  log the error, increment the existing fetch error metric, and leave venue
  lifecycle unchanged.
- If soft-delete cannot load or write the venue record, log the venue ID,
  reason, and source; increment an error result metric; do not attempt a
  hard-delete fallback.
- Admin inventory failures must return sanitized HTTP errors and must not
  expose raw Redis payloads or external API payloads.
- Background jobs must log active, deprecated-skipped, and processed counts
  where they switch to active-only enumeration.
- Data quality metrics must distinguish active and deprecated totals so a
  drop in active venues is observable without losing the retained inventory.

## Test Plan
Feature file: `tests/bdd/persistence/venue_soft_deletion.feature`

Scenarios:
- Permanently closed Google Places venues are soft-deleted and all Redis cache
  records are retained.
- Temporarily closed Google Places venues remain active, eligible for live
  busyness refresh, and visible in nearby results when data is available.
- Deprecated venues are hidden from public nearby results while still
  available for admin lookup.
- Enrichment and refresh jobs skip deprecated venues.
- Admin inventory exposes deprecated venues and cache flags for vibes_bot.
- Legacy Redis records without lifecycle metadata remain active after deploy
  without a reset or migration.
- Inventory sync and discovery upserts preserve deprecated metadata.

Pytest unit tests:
- `tests/test_redis_dao_unit.py`: `soft_delete_venue` marks lifecycle metadata,
  keeps the geo member, and never deletes associated keys.
- `tests/test_redis_dao.py`: real Redis integration for active/deprecated
  nearby filtering in test Redis database 15 only.
- `tests/test_services.py` or focused service tests: live and weekly refresh
  enumerate active IDs only.
- Google Places enrichment tests: `CLOSED_PERMANENTLY` calls
  `soft_delete_venue`, never `delete_venue`, and updates soft-delete metrics.
  `CLOSED_TEMPORARILY` calls neither `soft_delete_venue` nor `delete_venue`,
  continues enrichment, and remains active.
- Handler tests: deprecated venues are omitted from public nearby responses.
- Admin router tests: `GET /admin/venues/inventory` filters by status and
  returns lifecycle metadata plus cache flags.
- Enrichment service tests for photo, Instagram, Instagram posts, menu photo,
  menu extraction, and vibe classifier skip deprecated venues.

Manual or integration checks:
- Run `make test-feature FEATURE=tests/bdd/persistence/venue_soft_deletion.feature`.
- Run targeted unit tests for DAO, Google Places enrichment, handler, admin
  router, and affected enrichment services.
- Do not run any command against production Redis. Redis integration tests may
  use only the documented test database 15 target.

## Acceptance Criteria
- Google Places closure handling cannot hard-delete a venue or associated
  cache records.
- Deprecated venues stay in Redis with reason/source/timestamp metadata.
- Temporarily closed venues remain active, continue through live busyness
  refresh, and remain eligible for public nearby responses.
- Public venue serving and all enrichment/refresh jobs ignore deprecated
  venues by default.
- vibes_bot can fetch deprecated venue inventory through the new admin API.
- Legacy Redis venue records remain active without any migration.
- Deployment instructions and code paths do not flush, reset, rename, rebuild,
  or reprocess the production Redis venue dataset.
- BDD and targeted pytest coverage pass.

## Open Questions
None

## vibes_bot Integration Prompt
Use this prompt after the cs-server change is merged and deployed:

Integrate vibes_bot's admin panel with cs-server's deprecated venue inventory.
Add an admin-only view that calls `GET /admin/venues/inventory` through the
existing cs-server admin proxy pattern. The UI must support `status=active`,
`status=deprecated`, and `status=all`, default to deprecated when the operator
is troubleshooting closed venues, and pass through optional search text and
pagination cursor/limit parameters.

Display each returned venue with id, name, address, latitude, longitude,
lifecycle status, deprecated reason, deprecated source, deprecated timestamp,
Google business status, and cache flags for live forecast, weekly forecast,
vibe attributes, photos, opening hours, Instagram, reviews, menu data, and vibe
profile. Do not expose this as a public user-facing page. Do not create any UI
action that deletes, restores, reprocesses, or mutates Redis venue records in
V1; this is a read-only troubleshooting surface.

Treat `CLOSED_TEMPORARILY` venues as active. They should continue to appear in
normal admin inventory and public nearby flows when cs-server has live busyness
available. Only `CLOSED_PERMANENTLY` venues should appear as deprecated because
of Google Places closure handling.

Keep the existing vibes_bot admin style and configuration conventions. Reuse
the existing cs-server base URL/auth/proxy utilities rather than calling
cs-server directly from browser code.
