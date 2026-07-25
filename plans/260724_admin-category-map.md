# Admin-configurable venue type → category mapping (cs-server)

## Branch
feature/admin-category-map

## Goal
Let an operator remap any Google Places `primaryType` or BestTime `venue_type`
to a VibeSense display category from the admin panel, taking effect without a
code deploy and without re-fetching venues from BestTime/Google. cs-server owns
the mapping, the effective-map reader, the validated config endpoints, and
persistence. This plan is the cs-server half of a cross-repo feature; vibes_bot
(separate plan) adds the proxy + admin UI. Mobile is unaffected.

## Non-goals
- No new VibeSense categories (the `CATEGORIES` set stays fixed; adding one would
  need mobile render changes).
- No change to the `CATEGORIES → (label/emoji/color)` display dict or
  `GRANULAR_LABELS`.
- The special override rule (Google=`restaurant` but BestTime=`BAR`/`PUB` → keep
  `BAR`/`PUB`) stays hardcoded, not exposed.
- No venue backfill, no re-projection, no snapshot-cache-bust (category is
  serve-time; the vibes_bot snapshot self-heals within ~15min — that is the
  vibes_bot plan's concern, save-only by decision).

## Evidence
- `app/models/venue_category.py` — hardcoded `_GOOGLE_TO_CATEGORY` (line 105) and
  `_BESTTIME_TO_CATEGORY` (line 177); `CATEGORIES` (line 12); `resolve_category`
  (line 198) and `resolve_venue_display` (line 239) consume the two dicts.
- `app/handlers/venue_handler.py` — `_transform()` (from line ~299) is the sole
  production call site; category is computed per request, never stored. The
  handler already carries `self.admin_config_service` (wired at
  `app/container.py:365`) and reads the raw Redis client via
  `self.venue_dao.client`.
- `app/services/venue_eligibility.py:385` — `load_eligibility_config(redis_like)`
  is the reader pattern to mirror: read the Redis key, per-field fall back to
  hardcoded defaults on any error / missing / malformed.
- `app/routers/admin_trigger_router.py:404-469` — `GET/POST
  /venues/eligibility-config` is the endpoint pattern; `PUT /config/{key}`
  (line 637) shows `AdminConfigService.set` raising `ValueError/TypeError` → 400.
- `app/services/admin_config_service.py` — `set(key, value, updated_by)` runs the
  registered validator, writes RDS (`admin.admin_config`) then mirrors Redis
  `admin_config:{key}`; `get(key)` reads the mirror (RDS fallback).
- `app/container.py:353-357` — `AdminConfigService(..., validators={
  "venue_eligibility": _validate_eligibility_config})` is where a new validator
  is registered.

## Current Behavior
The Google/BestTime → category maps are hardcoded Python dicts. Changing how a
type maps to a category requires editing `venue_category.py` and redeploying
cs-server. `resolve_venue_display(google_type, besttime_type, venue_name)` reads
the module-level dicts directly.

## Desired Behavior
- An operator can read the effective mapping and write an override; cs-server
  serves the new category on the next `GET /v1/venues/nearby` request.
- `GET /admin/venues/category-map` returns the EFFECTIVE map (hardcoded defaults
  merged with any stored override) so the admin UI pre-populates with the live
  mapping.
- `POST /admin/venues/category-map` validates the body, normalizes key casing,
  persists via `AdminConfigService` (RDS truth + Redis mirror
  `admin_config:venue_category_map`), and returns the resulting effective map.
  An unknown category value is rejected with HTTP 400 and nothing is written.
- If no override is stored, or the stored value is unreadable/malformed, serving
  falls open to the hardcoded defaults — category resolution never breaks on bad
  config.

## Implementation Approach
1. **Reader** (`app/models/venue_category.py`, or a small sibling): add
   `load_category_map(redis_like)` returning a `(google_map, besttime_map)` pair
   of effective dicts. Read the raw Redis key `admin_config:venue_category_map`;
   on any error / missing key / malformed JSON / wrong shape, fall open to the
   hardcoded defaults (mirror `load_eligibility_config` defensiveness, incl. its
   debug logging). Merge semantics: `effective = {**default, **override}` per
   side, so an override entry shadows a default and unknown/extra keys are kept
   only if they pass validation on write.
2. **Resolver refactor**: give `resolve_category` and `resolve_venue_display`
   optional `google_map`/`besttime_map` params defaulting to the module dicts
   (`_GOOGLE_TO_CATEGORY` / `_BESTTIME_TO_CATEGORY`). Keep the special
   Google-restaurant-vs-BestTime-bar override rule hardcoded inside
   `resolve_category`. No behavior change when params are omitted.
3. **Hot serve path** (`venue_handler._transform`): once per batch (verbose=False
   branch), call `load_category_map(getattr(self.venue_dao, "client", None))` and
   pass the two maps into each `resolve_venue_display(...)` call. One Redis read
   per request, never per venue.
4. **Validator** (`app/container.py` + a `_validate_category_map` fn): body must
   be a dict with optional `"google"` and `"besttime"` keys, each a
   `dict[str, str]`; every value must be an existing `CATEGORIES` key (else
   `ValueError`); normalize google keys to lowercase, besttime keys to uppercase;
   return the normalized dict. Register under key `venue_category_map` in the
   `AdminConfigService` validators dict.
5. **Endpoints** (`app/routers/admin_trigger_router.py`, mirroring eligibility):
   - `GET /venues/category-map` → merge stored override
     (`admin_config_service.get("venue_category_map")`, may be `None`) over the
     hardcoded defaults and return `{"google": {...}, "besttime": {...}}`.
   - `POST /venues/category-map` → `admin_config_service.set("venue_category_map",
     body, updated_by="admin")`; `ValueError/TypeError` → 400, other exceptions →
     502 (retryable); return the resulting effective map.

## Data, Config, And API Impact
- **New Redis key + RDS row**: `admin_config:venue_category_map` (mirror) and an
  `admin.admin_config` row (truth), both managed by `AdminConfigService`. Shape:
  `{"google": {"<primaryType lowercase>": "<CATEGORY>"}, "besttime":
  {"<VENUE_TYPE UPPERCASE>": "<CATEGORY>"}}`.
- **New endpoints**: `GET /admin/venues/category-map`, `POST
  /admin/venues/category-map` (admin router prefix).
- **No RDS migration** — reuses the existing `admin.admin_config` table.
- **Design decision (matches eligibility)**: a saved override is authoritative
  for the keys it contains and shadows later code-default changes for those keys;
  to adopt a new code default, delete the config key. To unmap a type back toward
  `OTHER`, set it explicitly to `OTHER` (merge cannot delete a default entry).

## Error Handling And Observability
- Reader fails open to hardcoded defaults on any fault, with a debug log
  (`[category_map] ... using defaults`), exactly like eligibility. Category
  resolution must never raise on bad config.
- Write validation happens before any persistence; malformed input → 400, nothing
  written. RDS/mirror failure after validation → 502 (idempotent retry).
- No new Prometheus metric (the write path is `AdminConfigService`, already
  covered; the read path is a single guarded Redis GET per request).

## Test Plan
Feature file: `tests/bdd/api/admin-category-map.feature`

Scenarios:
- GET returns the effective map = hardcoded defaults when no override is stored.
- POST a valid override, then GET reflects it merged over the defaults.
- POST with an unknown category value (e.g. `"FOO"`) → 400 and the effective map
  is unchanged.
- POST normalizes key casing (google key uppercased in → stored lowercase;
  besttime key lowercased in → stored uppercase).
- Serving effect without re-fetch: with an override remapping google `bakery` →
  `FOOD_DRINK`, a venue whose `primaryType` is `bakery` serves `category:
  FOOD_DRINK` from `GET /v1/venues/nearby` (previously `OTHER`), proving the map
  applies at serve time with no venue re-fetch.

Pytest unit tests:
- `load_category_map`: returns hardcoded defaults when redis is `None`, key
  missing, JSON malformed, or shape invalid; returns merged maps on a valid
  override.
- `resolve_category` / `resolve_venue_display` with injected `google_map` /
  `besttime_map`: an injected mapping wins; the special restaurant-vs-bar rule
  still holds; omitting the params preserves current behavior.
- `_validate_category_map`: rejects unknown category values and non-dict shapes;
  normalizes key casing; accepts an empty/partial map.

Manual or integration checks:
- `POST /admin/venues/category-map` then `GET /v1/venues/nearby` against the local
  demo stack; confirm a remapped type's served `category` changes without a
  refresh job.

## Acceptance Criteria
- `GET /admin/venues/category-map` returns defaults with no override and the
  merged map after a POST.
- `POST` with a valid map persists to RDS + Redis mirror and is reflected on the
  next GET and on the next `/v1/venues/nearby` serve.
- `POST` with an invalid category value returns 400 and changes nothing.
- With no/malformed config, serving uses the hardcoded defaults (no error).
- Omitting the new resolver params reproduces today's category output exactly.

## Open Questions
- None.
