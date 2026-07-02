# Geo-fence as capital-city circles inside eligibility

## Branch
feature/geofence-city-circles

## Goal
Replace the single Recife bounding box with a list of **state-capital circles**
(capital + radius km) that an admin can add, remove, and edit — while keeping
geo restriction enforced exactly where the rest of eligibility lives: in the
`serving.eligible_venue` view, reversibly, fail-open, never soft-deleting.

## Non-goals
- Arbitrary (non-capital) cities. The catalog is the 26 Brazilian state
  capitals + Brasília; adding other municipalities is a one-line catalog edit
  later, not a schema change.
- Per-city enable flags (the global `enabled` plus add/remove covers it).
- Moving the `/admin/config/geofence` URLs under an "eligibility" path — the
  admin-panel regrouping is vibes_bot's side of this feature; the cs-server
  contract keeps its endpoints.
- Deriving vibes_bot's add-venue search-city list from the fence (noted as a
  wrapper-level follow-up; today it derives from deprecated discovery points).
- Any change to the eligibility rules CRUD or the name/type predicates.

## Evidence
- `migrations/versions/0014_geofence_eligible_venue.py` — box table + the view
  with the geo term as a top-level fail-open predicate.
- `app/services/venue_eligibility.py:62-72,438-531` — `DEFAULT_GEO_FENCE` box,
  `validate_geo_fence`, `geo_excluded`, `load_geo_fence`, Redis mirror key
  `admin_config:venue_geofence`; `REASON_GEO` is deliberately serve-time-only.
- `app/routers/admin_trigger_router.py:467-531` — GET/PUT `/config/geofence`
  (declared **before** the generic `/config/{key}` route — order matters).
- `app/dao/rds_venue_store.py:489-542` — `get_geo_fence`, `set_geo_fence`,
  `count_geo_excluded_active_venues` (box predicate duplicated for the gauge).
- `app/services/redis_projection_service.py:97-99` — `venues_geo_excluded`
  gauge from the store count.
- Tests today: `tests/test_venue_eligibility.py`, `tests/rds_fake.py`,
  `tests/test_rds_store_contract.py`,
  `tests/test_eligibility_serving_view_parity.py`,
  `tests/bdd/steps/discovery_hardening_geofence_steps.py`.
- Prod baseline (2026-07-02): fence box lat −8.30..−7.85 / lng −35.10..−34.80,
  `serving_view_venues` 456, `venues_geo_excluded` 1112.

## Current Behavior
The fence is one axis-aligned box in the singleton `admin.geo_fence` row. The
view, the Python parity predicate, and the gauge count all test
`lat/lng BETWEEN` the box edges. The admin contract is the raw box
(`min_lat/max_lat/min_lng/max_lng/enabled`), which is hard to reason about and
supports exactly one region.

## Desired Behavior
- The fence is a set of circles: each row = one state capital (slug, name,
  center lat/lng from a server-side catalog) + an admin-chosen `radius_km`.
- A venue is geo-OK when the fence is disabled, OR it has no coordinates, OR it
  falls inside **any** configured circle (haversine distance ≤ radius). Same
  fail-open semantics as 0014; still applied only in `serving.eligible_venue`
  at view level; still never a soft-delete reason.
- Admins manage the fence with capital + radius only; the server owns all
  coordinates. Changes apply on the next projection (≤2 min), fully reversible.
- The current prod serving set must not shrink at migration time.

## Implementation Approach
1. **Catalog** (`app/services/venue_eligibility.py`): constant
   `STATE_CAPITALS` — 27 entries `{slug, name, lat, lng}` (city-center
   coordinates, accurate to ≤0.05°). Recife pinned at `(-8.0476, -34.8770)`.
2. **Schema — migration 0015**:
   - `CREATE TABLE admin.geo_fence_city (slug text PRIMARY KEY, name text NOT
     NULL, lat/lng double precision NOT NULL with range CHECKs, radius_km
     double precision NOT NULL CHECK (radius_km BETWEEN 1 AND 200),
     updated_by text, updated_at timestamptz NOT NULL DEFAULT now())`.
   - Seed one row: `recife`, radius **40 km**. Rationale: the farthest corner
     of the 0014 box from the Recife center is ≈37.3 km, so the 40 km circle
     strictly contains the old box — **zero venues lose serving**; venues newly
     inside (e.g. Igarassu/Itapissuma, genuinely metro) are re-included and the
     count is reported at verification.
   - `admin.geo_fence` keeps only `id`/`enabled`/`updated_by`/`updated_at`
     (box columns dropped), preserving the current `enabled` value.
   - Redefine `serving.eligible_venue`: identical name/type predicates; geo
     term becomes `fence.enabled IS NOT TRUE OR lat/lng IS NULL OR EXISTS
     (circle within radius)` using core-Postgres haversine (no extension):

     ```sql
     2 * 6371.0088 * asin(sqrt(
       pow(sin(radians(c.lat - g.lat) / 2), 2)
       + cos(radians(g.lat)) * cos(radians(c.lat))
         * pow(sin(radians(c.lng - g.lng) / 2), 2))) <= c.radius_km
     ```

   - Empty city table while enabled = fail-open in the view (unreachable via
     the API — see validation — but consistent with 0014 if reached by hand).
   - `downgrade()` restores the 0014 table shape (re-seeding the original box)
     and the 0014 view.
3. **Python parity** (`venue_eligibility.py`): `DEFAULT_GEO_FENCE` becomes
   `{enabled: true, cities: [recife@40km]}`; `geo_excluded(lat, lng, fence)`
   applies the same haversine union, fail-open on disabled/missing
   coords/empty cities/malformed shape; `validate_geo_fence` validates the new
   payload; `load_geo_fence` returns the new shape and degrades a legacy box
   JSON (or any invalid mirror) to the defaults with a warning.
4. **Store** (`rds_venue_store.py`): `get_geo_fence` returns
   `{enabled, cities: [...]}` (defensive: missing tables → defaults, logged —
   covers the deploy-before-migration window); `set_geo_fence` transactionally
   replaces `admin.geo_fence_city` and upserts `enabled`;
   `count_geo_excluded_active_venues` switches to the circle predicate,
   staying in lockstep with the view. `tests/rds_fake.py` mirrors the contract.
5. **API** (`admin_trigger_router.py`, all before `/config/{key}`):
   - `GET /admin/config/geofence` → `{"enabled": bool, "cities": [{"slug",
     "name", "lat", "lng", "radius_km"}]}`.
   - `PUT /admin/config/geofence` ← `{"enabled": bool, "cities": [{"slug",
     "radius_km"}]}` — full-list replace; the server resolves slug → catalog
     coords. 400 (fence unchanged) on: unknown slug, duplicate slug,
     non-numeric or out-of-[1,200] radius, or `enabled: true` with zero cities
     (prevents both the serve-everything and serve-nothing cliffs);
     `enabled: false` with zero cities is allowed. Legacy box payloads → 400
     with a message naming the new shape. On success, mirror the GET shape to
     `admin_config:venue_geofence` (best-effort, as today) and return it.
   - `GET /admin/config/geofence/capitals` → `{"capitals": [{slug, name, lat,
     lng}]}` sorted by name, for the panel's select.

## Data, Config, And API Impact
- Migration 0015 (manual on RDS, snapshot first — deploy never runs alembic).
- `admin.geo_fence_city` new; `admin.geo_fence` reduced to the enabled flag.
- Redis mirror `admin_config:venue_geofence` changes shape (readers:
  `load_geo_fence` only, which tolerates the old shape by falling back).
- GET/PUT `/admin/config/geofence` change shape — **breaking** for the panel;
  vibes_bot ships its matching UI in the same integrated deploy. The
  batch-add-venues skill's fence pre-check reads this contract and must follow
  (wrapper-level doc touch after merge).
- New endpoint `/admin/config/geofence/capitals`.

## Error Handling And Observability
- PUT validation failures → 400 with the specific field problem; RDS write
  failure → 502, fence unchanged; mirror failure → warning log, write still
  succeeds (same as today).
- `get_geo_fence` on a pre-migration DB logs an error and serves defaults so
  the router never 500s during the deploy→migrate window.
- Gauges `serving_view_venues` / `venues_geo_excluded` keep their meaning under
  the new predicate; no new metric needed. Fence writes log
  `updated_by`, `enabled`, and the city/radius list (no PII, no secrets).

## Test Plan
Feature file: `tests/bdd/api/geofence-city-circles.feature`

Scenarios:
- The capitals catalog endpoint lists all 27 capitals with slug, name, coords.
- GET returns the enabled flag and configured circles with resolved coords.
- PUT replaces the city list, returns resolved circles, and mirrors to Redis.
- PUT with an unknown capital slug is rejected and the fence is unchanged.
- PUT with a duplicate slug or an out-of-range radius is rejected.
- PUT enabling the fence with no cities is rejected.
- PUT disabling the fence with no cities is accepted.
- A venue inside any one configured circle is served.
- A venue outside every circle is excluded from serving on projection.
- A venue without coordinates is always served (fail-open).
- Disabling the fence re-serves previously geo-excluded venues.

Pytest unit tests:
- `tests/test_venue_eligibility.py`: haversine `geo_excluded` (inside /
  outside / boundary / multi-circle / disabled / empty / malformed),
  `validate_geo_fence` new payload matrix, `load_geo_fence` legacy-shape
  fallback, catalog integrity (27 unique slugs, lat/lng in range).
- `tests/test_rds_store_contract.py` + `tests/rds_fake.py`: new
  get/set contract, transactional replace, `count_geo_excluded_active_venues`
  circle parity.
- `tests/test_eligibility_serving_view_parity.py`: Python predicate ↔ SQL view
  parity for circles (real Postgres when available).

Manual or integration checks:
- Prod, after snapshot + deploy + `alembic upgrade head`: `SELECT` comparing
  old-box vs new-circle membership over active venues — expect 0 newly
  excluded; record newly included count. `serving_view_venues` must not drop
  below 456; `venues_geo_excluded` decreases only by the newly included count.

## Acceptance Criteria
- Admin can add/remove capitals and edit radii via PUT with capital+radius
  only; the change is visible in GET, the Redis mirror, and serving membership
  on the next projection.
- Geo restriction is enforced **only** in `serving.eligible_venue` (view
  level); no code path soft-deletes for geo.
- Migration preserves the prod serving set (no venue newly geo-excluded).
- Invalid PUTs never partially apply.
- Full suite green: `make test-unit`, `make test-bdd`.

## Open Questions
- None. Decisions recorded: capitals-only catalog (27), Recife seeded at 40 km
  (strictly contains the 0014 box), radius bounds 1–200 km, global `enabled`
  kept on the singleton row, `enabled:true` requires ≥1 city.
