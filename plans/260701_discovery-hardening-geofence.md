# Discovery/Startup Hardening + Recife-Metro Geo-Fence Eligibility

## Branch
feature/discovery-hardening-geofence

## Goal
Stop uncontrolled venue ingestion and out-of-region catalog pollution, and add a
location dimension to eligibility so only venues inside the Recife/Olinda metro
are served. Three coupled changes, driven by the 2026-07-01 incident:

1. **No pipeline runs on application startup.** Pipelines run only via scheduled
   cron (`register_refresh_jobs`) or explicit admin-panel triggers
   (`admin_trigger_router` / `JOB_REGISTRY`).
2. **Discovery is fully dormant.** The venue-filter catalog discovery keeps its
   code but has no trigger path at all (no startup, no scheduled job, no admin
   trigger), and its discovery-point config is deprecated.
3. **Recife-metro geo-fence eligibility.** A lat/lng bounding box excludes
   out-of-region venues from `serving.eligible_venue` (and therefore from the
   Redis serving projection and the priority refresh budget). The box is
   admin-editable; enforcement mirrors `venue_eligibility.evaluate()` and the
   serving view in parity. This retroactively drops the existing 826
   out-of-region active venues from serving without deleting data.

## Non-goals
- Freeing the July BestTime unique-venue quota ledger (`besttime_touched_v1:2026-07`
  = 500/500). That is a separate operational action, not this change.
- Soft-deleting or hard-deleting the out-of-region venues. Geo-fence exclusion is
  a **reversible serve-time filter** (consistent with the module's "never
  irreversibly hide a real bar" policy); rows stay in `venues.venue`.
- The vibes_bot admin-panel editor UI for the geo-fence. That is a separate,
  sequenced per-repo plan (see coordination plan). This plan defines the
  cs-server admin **endpoint contract** vibes_bot will consume.
- Removing the `*_on_startup` settings/env keys. They may remain as dead config;
  the startup code path that reads them is what is removed. (Optional cleanup
  noted below.)

## Evidence
- Startup pipelines: `main.py:621-747` (`startup_background_pipelines`) runs the
  catalog/discovery refresh when `refresh_on_startup=True` (`main.py:630-654`)
  plus 7 enrichment pipelines behind their own `*_on_startup` flags. It is
  launched as a task in the lifespan at `main.py:788`.
- Root cause: the startup refresh is gated **only** by `refresh_on_startup`
  (`main.py:630`), bypassing `discovery_enabled=False` which only guards the
  *scheduled* Job 1 (`main.py:356`, log "Job 1 not scheduled"). Prod ran discovery
  on the 2026-07-01 11:47 restart: 7× `GET /venues/filter` (11:49:59–11:52:09),
  0× `POST /forecasts`, stopped at "Global budget reached".
- Discovery admin trigger: `JOB_REGISTRY["venue_catalog"]`
  (`app/routers/admin_trigger_router.py:50`) → "Fetch venues from BestTime API for
  all default locations".
- Eligibility is name/type only: `app/services/venue_eligibility.py:261`
  (`evaluate(venue_name, besttime_type, google_type, config)`) — no location
  input. That is why out-of-city bars/restaurants (legit types) pass.
- Serving view: `migrations/versions/0009_eligibility_serving_view.py`
  (`serving.eligible_venue`) selects `lifecycle_status='active'` venues not
  high-confidence-ineligible under `admin.eligibility_rule` rows. Parity guard:
  `tests/test_eligibility_serving_view_parity.py`. Projector reads
  `list_servable_venue_ids()` (`app/services/redis_projection_service.py:81-92`).
- Rule storage/mirror: `admin.eligibility_rule` rows ↔ `admin_config:venue_eligibility`
  blob via `assemble/decompose_eligibility_blob` and `eligibility_rule_service`
  (`venue_eligibility.py:347-415`).
- Data reality (prod, 2026-07-01): `venues.address.city` and `postal_code` are
  100% empty; `lat`/`lng` present for all; state parseable from `raw_text` only
  ~88%. → coordinates are the only reliable location signal (bbox chosen).
- Damage: 1648 active venues, **826 outside the Recife bbox, 750 of them
  eligible**; only 459 of 1209 eligible venues are actually in Recife.

## Current Behavior
- On every startup, if `refresh_on_startup=True`, discovery + live + weekly
  refresh run, followed by enrichment pipelines whose `*_on_startup` flags are set.
- Discovery (`refresh_venues_by_filter_for_default_locations`) can be triggered by
  startup, the scheduled Job 1 (when `discovery_enabled=True`), or
  `POST /admin/trigger/venue_catalog`.
- `serving.eligible_venue` and `evaluate()` ignore location. Out-of-region venues
  with legit/blank types are served and consume the priority refresh budget
  (capped at ~400/cycle), crowding out real Recife venues.

## Desired Behavior
- `startup_background_pipelines` runs **no** pipeline. Startup only serves existing
  data; refresh/enrichment happen via cron or admin trigger. A stray
  `*_on_startup=true` must not re-trigger anything.
- The venue-filter catalog discovery has **no** reachable trigger: startup path
  removed, scheduled Job 1 stays gated off, `venue_catalog` removed from the admin
  `JOB_REGISTRY`. The function code remains for future reuse. Deprecate
  `admin_config:discovery_points` and any `DEFAULT_DISCOVERY_POINTS` fallback so
  even a manual call has no points.
- A venue is eligible only if its coordinates fall inside the configured allowed
  bounding box (default Recife/Olinda metro). Enforced identically in
  `serving.eligible_venue` (SQL) and `evaluate()` (Python), kept in parity. The
  box is stored in Postgres (so the SQL view can read it) and editable through a
  cs-server admin endpoint. Missing coordinates do **not** geo-exclude (fail-open,
  reversible policy). The projector's next cycle drops the 826 out-of-region
  venues from serving and frees the refresh budget.

## Implementation Approach
**1. No startup pipelines (`main.py`).**
- Remove the pipeline invocations from `startup_background_pipelines` (or reduce it
  to a log-only no-op) and stop scheduling it in the lifespan (`main.py:788`).
  Keep all pipeline service methods intact. Leave a single INFO log documenting
  that startup runs no pipelines by design.

**2. Deprecate discovery (`app/routers/admin_trigger_router.py`, config).**
- Remove the `venue_catalog` entry from `JOB_REGISTRY` so it cannot be admin-
  triggered; return the standard "Unknown job" 404 for it. Keep
  `refresh_venues_by_filter_for_default_locations` and `discovery_enabled`
  (already default False) untouched. Empty/deprecate `DEFAULT_DISCOVERY_POINTS`
  and treat a missing/empty `admin_config:discovery_points` as "no discovery".

**3. Recife-metro geo-fence eligibility.**
- **Storage:** add the allowed box to Postgres so the SQL view can read it — a new
  single-row `admin.geo_fence` table (`min_lat, max_lat, min_lng, max_lng, enabled`)
  seeded with the Recife/Olinda default, plus a Redis mirror
  (`admin_config:venue_geofence`) for `evaluate()`/admin reads. Keep the two in
  sync through the existing eligibility-mirror rehydration path.
- **Serving view (new migration, e.g. `0011_geofence_eligible_venue`):** redefine
  `serving.eligible_venue` to LEFT JOIN `venues.address` and additionally require,
  when the box is enabled and coordinates are present, that `lat`/`lng` fall inside
  the box. Missing address/coords → not geo-excluded. Down-migration restores 0009.
- **`evaluate()` parity:** extend with optional `lat`/`lng` (and the loaded box) and
  add the same bbox predicate as a reversible (`low`/serve-time) exclusion, never
  soft-deletable. Update callers (serving, inventory sync, discovery sweep) to pass
  coordinates. Extend `tests/test_eligibility_serving_view_parity.py` to cover the
  geo dimension.
- **Admin endpoint:** `GET`/`PUT /admin/config/geofence` returning/accepting
  `{min_lat, max_lat, min_lng, max_lng, enabled}`, validating ranges
  (lat −90..90, lng −180..180, min<max), writing the table + mirror. This is the
  contract the vibes_bot admin UI consumes.

## Data, Config, And API Impact
- **Migration:** new `admin.geo_fence` table (seeded Recife default) + redefinition
  of `serving.eligible_venue` (reversible down-migration to 0009).
- **Config:** new Redis mirror key `admin_config:venue_geofence`. Deprecate
  `admin_config:discovery_points` + `DEFAULT_DISCOVERY_POINTS`. `*_on_startup`
  settings become dead (documented).
- **API:** new `GET`/`PUT /admin/config/geofence`. `POST /admin/trigger/venue_catalog`
  now returns 404 (removed from `JOB_REGISTRY`).
- **Serving projection:** unchanged key/shape; the servable *set* shrinks by ~826.

## Error Handling And Observability
- Geo-fence config read failures fall back to the seeded default box (never break
  filtering), mirroring `load_eligibility_config`.
- Admin `PUT` validates the box and rejects invalid payloads (400) leaving the
  active box unchanged.
- Add a metric/gauge for venues excluded by geo-fence (e.g. reuse the eligibility
  reason labels with a new `ineligible_geo` reason) and log the servable-set size
  delta on projector rebuild.
- Startup logs one line stating no pipelines run on startup.

## Test Plan
Feature file: `tests/bdd/refresh/discovery-hardening-geofence.feature`

Scenarios:
- On startup, no discovery/enrichment pipeline runs even when every `*_on_startup`
  flag is true; the server still serves existing venues.
- The scheduled cron jobs and admin triggers still run their pipelines normally.
- `POST /admin/trigger/venue_catalog` returns 404 (discovery not triggerable);
  other admin triggers still work.
- A venue with coordinates outside the Recife box is excluded from
  `serving.eligible_venue` and from the serving projection.
- A venue inside the box (e.g. Olinda) remains eligible/served.
- A venue with no coordinates is not geo-excluded (fail-open).
- `PUT /admin/config/geofence` updates the box; a previously-excluded venue that
  now falls inside becomes eligible after the next projection (reversibility).
- Invalid geo-fence payload is rejected (400) and the active box is unchanged.

Pytest unit tests:
- `evaluate()` geo predicate: inside/outside/missing-coords, box disabled, and
  parity of reason/confidence (reversible, never soft-deletable).
- Serving-view ↔ `evaluate()` parity extended for the geo dimension
  (`tests/test_eligibility_serving_view_parity.py`).
- `JOB_REGISTRY` no longer contains `venue_catalog`; unknown-job path returns 404.
- Geo-fence config load/validate/fallback; admin PUT validation.

Manual or integration checks:
- Apply the migration on a scratch DB; confirm `serving.eligible_venue` drops the
  826 out-of-region venues and keeps Recife/Olinda venues.
- Confirm projector rebuild shrinks the servable set and frees the refresh budget.

## Acceptance Criteria
- No pipeline executes on startup under any `*_on_startup` setting.
- Discovery has no reachable trigger (startup/scheduled/admin); its code remains.
- Out-of-region venues are excluded from `serving.eligible_venue`, the serving
  projection, and the refresh budget; Recife/Olinda venues remain.
- The geo-fence box is admin-editable via the cs-server endpoint, validated, with a
  safe default fallback; changes are reversible on the next projection.
- Serving-view and `evaluate()` parity holds for the geo dimension.

## Open Questions (resolved at execution)
- vibes_bot preflight: the vibes_bot admin-UI editor is a **non-goal** of this
  cs-server plan (it only defines the endpoint contract). The vibes_bot side is a
  separate, later per-repo plan and does not block this cs-server execution.
- Exact default Recife/Olinda box coordinates — **confirmed** at execution as the
  proposed box: lat −8.30..−7.85, lng −35.10..−34.80 (seeded in migration 0014 and
  `DEFAULT_GEO_FENCE`).

## Execution notes (as-built)
- **Discovery points already dormant:** there is no `DEFAULT_DISCOVERY_POINTS`
  constant, and `_get_discovery_points()` already returns `[]` when the admin
  config is empty/absent. "Discovery keeps no configured points" needed no new
  code — it is a BDD assertion of existing behavior.
- **No `evaluate()` production callers:** serving membership is enforced solely by
  the `serving.eligible_venue` SQL view. The geo predicate is therefore a
  **separate** function (`geo_excluded`, a reversible third state — never folded
  into `evaluate()`/`soft_deletable`); the Python side exists only to keep the fake
  store + parity test honest. No serving/inventory-sync/discovery callers needed
  coordinate threading.
- **Route ordering:** `GET`/`PUT /admin/config/geofence` are declared **before** the
  generic `/config/{key}` handler so the write lands in the typed `admin.geo_fence`
  table (read by the view), not `admin.admin_config`.
- **Observability:** added a `venues_geo_excluded` gauge set on each projector
  rebuild (active venues outside the enabled box) alongside the existing
  `serving_view_venues` gauge; `REASON_GEO = "ineligible_geo"` added to
  `ALL_REASONS` for serve-time labeling.
- **Migration id:** `0014_geofence_eligible_venue` (down_revision `0013…`; the
  plan's `0011` was illustrative).
