# On-Demand Venue Photos (fresh keyless CDN URLs, short-TTL cache)

## Branch
feature/on-demand-venue-photos

## Goal
Resolve a single venue's Google Places photos **on demand** and cache **fresh,
keyless CDN URLs** for a few hours, instead of pre-baking key-bearing
`/media?...&key=` URLs for the whole ~2000-venue catalog with a ~5-day TTL.
Google rotates the photo token faster than the 5-day TTL, so the pre-baked URL
returns `400 INVALID_ARGUMENT` and the app shows a blank photo for days.

Deliver, in cs-server (the sole writer of the shared Redis projection):

1. An on-demand resolver that, for one venue, calls Google Place Details for the
   photo resource `name`s, then calls the Google photo **media** endpoint with
   `skipHttpRedirect=true` to obtain the **keyless** `photoUri`
   (`https://lh3.googleusercontent.com/...`) for up to `photos_per_venue` (5)
   photos at `maxWidthPx=800`.
2. A short-TTL Redis cache the resolver writes: key
   `venue_photos_fresh_v1:{venue_id}`, value JSON `[{"url","author_name"}]`, TTL
   from a new admin-tunable setting `photo_fresh_cache_ttl_hours` (default 6).
3. An internal, auth-restricted endpoint
   `POST /internal/venues/{venue_id}/photos/resolve` →
   `200 {"venue_photos": [{"url","author_name"}]}` that resolves, writes the
   cache, and returns the list. Empty list when there is no `google_place_id` or
   Google returns no photos.
4. Retirement/scope-down of the catalog-wide `PhotoEnrichmentService` cron
   pre-bake (the `venue_photos_v1:*` 5-day path), while keeping the resolution
   logic reusable by the on-demand path, and never serving a stale/dead URL.

## Non-goals
- No new RDS columns or long-term photo storage. `google_place_id` is already
  persisted (`app/models/vibe_attributes.py`, `app/dao/venue_repository.py`,
  `app/dao/rds_venue_store.py`); the fresh keyless URLs live **only** in the
  short-TTL Redis key — never in RDS.
- No change to vibes_bot or mobile in this plan. Downstream behavior
  (`GET /venues/{id}` reads the fresh key or calls resolve on miss; `GET /venues`
  list drops photos; mobile renders keyless URLs behind `venuePhotosEnabled`) is
  contract context only and is sequenced in those repos' own plans.
- No removal of the legacy `venue_photos_v1:*` key, its DAO methods, or the
  projector's `_project_photos` path — they stay intact (dormant) for Redis key
  compatibility.
- No hard delete of `PhotoEnrichmentService.refresh_photos_for_venues` — it is
  scoped down to dormant (no scheduled/startup/admin trigger), mirroring the
  already-dormant venue-discovery pattern documented in `admin_trigger_router.py`.

## Evidence
- `app/services/photo_enrichment_service.py` — `fetch_and_cache_photos` and the
  catalog loop `refresh_photos_for_venues` (writes the legacy key with a ~5-day
  TTL); `get_place_photos` is the reusable per-venue resolution call.
- `app/api/google_places_client.py`:
  - Constants `GOOGLE_PLACES_API_BASE = "https://places.googleapis.com/v1"` (L18)
    and `PHOTOS_FIELDS_MASK = "photos.name,photos.authorAttributions"` (L21).
  - `get_place_photos` (L501-577) currently **constructs and stores**
    `.../{photo_name}/media?maxWidthPx=800&key={api_key}` (L553) — a key-bearing
    URL that dies when Google rotates the token. It never calls the media
    endpoint; it only builds the redirect URL.
- `app/dao/redis_venue_dao.py`:
  - `VENUE_PHOTOS_KEY_FORMAT = "venue_photos_v1:{}"`, `set_venue_photos` /
    `get_venue_photos` / `delete_venue_photos` (L533-592), and legacy-format
    tolerance (bare URL strings) in `get_venue_photos` (L586-588).
  - `_resolve_photos_cache_ttl_seconds` (L490-531) resolves the legacy TTL:
    `admin_config:venue_photos_cache_ttl_days` live override → `photo_cache_ttl_days`
    setting, guarding non-positive/invalid writes. This is the template for the
    new fresh-TTL resolver.
- `app/config.py` — photo settings (L200-211): `photos_per_venue = 5`,
  `photo_cache_ttl_days = 5`, `photo_enrichment_enabled/on_startup/limit`.
- `app/services/redis_projection_service.py` — `_project_photos` (L163-179)
  re-asserts the **legacy** key from the RDS `google_places.photos` enrichment
  record with remaining TTL. Since the pre-bake stops writing that RDS record,
  this path naturally goes dormant; it is left intact for compatibility.
- `main.py` — Job 5 photo-enrichment scheduler registration (L447-464), gated on
  `photo_enrichment_enabled`; `run_photo_enrichment_job` (L147).
- `app/routers/admin_trigger_router.py` — `photos` entry in `JOB_REGISTRY`
  (L79-82) and its `_run_job` branch (L149-155); container-injection router
  pattern (`set_container`, module-level `router`).
- `app/routers/venue_router.py` — router + `set_*`/`get_handler` injection
  pattern; routers carry no in-app auth. `main.py` mounts routers (L698-701).
- Internal/admin surface is gated at the **network layer** (Caddy does not expose
  `/admin`; `README.md` L182 "private subnet reachable only from the EC2's VPC";
  `DEPLOYMENT.md` internal-network guidance), not by an app-level token. vibes_bot
  and cs-server share the integrated compose network, so vibes_bot reaches
  `/internal/...` over that network. No `admin_token`/`internal_api_key` exists in
  `app/config.py`.
- `app/api/google_places_client.py` metric labels
  `GOOGLE_PLACES_API_CALLS_TOTAL{endpoint=...}` etc. are the pattern for the new
  media-call metric.

## Current Behavior
- A daily cron (`photo_enrichment`, when `PHOTO_ENRICHMENT_ENABLED=true`) and an
  admin `photos` trigger walk the servable catalog and, per venue, build a
  key-bearing `/media?maxWidthPx=800&key=<api_key>` URL and store it under
  `venue_photos_v1:{venue_id}` with a ~5-day TTL (`photo_cache_ttl_days`).
- The projector re-asserts those legacy entries from RDS with the remaining TTL.
- Google rotates the photo `name` token faster than 5 days; once rotated, the
  stored URL returns `400` and the app shows a blank photo until the next cron.
- Photos are only ever shown when a user opens a venue, yet the whole catalog is
  pre-baked.

## Desired Behavior
- cs-server resolves photos for a single venue **on demand** and caches **fresh,
  keyless** CDN URLs with a short TTL:
  - `POST /internal/venues/{venue_id}/photos/resolve` looks up the venue's stored
    `google_place_id`, fetches photo resource `name`s via Google Place Details
    (reusing `GooglePlacesAPIClient`), then for each `name` calls the media
    endpoint with `skipHttpRedirect=true` and reads the keyless `photoUri`.
  - It caps at `photos_per_venue` (5), requests `maxWidthPx=800`, preserves the
    first author attribution as `author_name` (or null), writes the list to
    `venue_photos_fresh_v1:{venue_id}` with TTL `photo_fresh_cache_ttl_hours`
    (default 6, admin-tunable), and returns
    `{"venue_photos": [{"url","author_name"}]}`.
  - No `google_place_id` → return `{"venue_photos": []}` (cache an empty list so
    repeated opens don't re-hit Google within the short window).
  - Google returns zero photos → return `{"venue_photos": []}` and cache the
    empty list.
  - Any Google/resolution error → return `{"venue_photos": []}` and **do not**
    write a URL-bearing entry, so the next open can retry and a dead URL is never
    served (graceful degradation).
- Every served `url` is a keyless `googleusercontent.com` URL — no `key=` query
  parameter, no `places.googleapis.com/.../media` redirect URL.
- The catalog-wide pre-bake is retired: the scheduled photo cron, the
  `*_on_startup` path, and the admin `photos` trigger no longer pre-bake the
  catalog. The legacy `venue_photos_v1:*` key, its DAO methods, and
  `_project_photos` remain intact (dormant) for Redis key compatibility.

## Implementation Approach

### 1. Google keyless photo resolution (reusable core)
Change `GooglePlacesAPIClient.get_place_photos` so that, for each photo resource
`name` returned by Place Details, it **calls** the media endpoint with
`skipHttpRedirect=true` and reads the keyless `photoUri`, instead of building a
key-bearing redirect URL:

```
GET {GOOGLE_PLACES_API_BASE}/{photo_name}/media?maxWidthPx=800&skipHttpRedirect=true
Header: X-Goog-Api-Key: <api_key>
200 -> { "name": "...", "photoUri": "https://lh3.googleusercontent.com/..." }
```

- Return shape is unchanged: `[{"url": <keyless photoUri>, "author_name": str|null}, ...]`,
  capped at `max_photos`, `author_name` from the first `authorAttributions` entry.
- The API key travels only in the request header, never in the stored/returned
  URL.
- This adds one media call per photo (up to `photos_per_venue` extra calls per
  venue). Keep the existing intra-request pacing sensibility; emit a metric per
  media call (see Observability). On a per-photo media failure, skip that photo
  rather than failing the whole venue.

### 2. Short-TTL fresh cache (DAO, cs-server sole writer)
In `app/dao/redis_venue_dao.py`:
- Add `VENUE_PHOTOS_FRESH_KEY_FORMAT = "venue_photos_fresh_v1:{}"`.
- Add `_resolve_fresh_photos_cache_ttl_seconds()` modeled on
  `_resolve_photos_cache_ttl_seconds`: live override
  `admin_config:photo_fresh_cache_ttl_hours` (int hours) → `settings.photo_fresh_cache_ttl_hours`,
  rejecting non-positive/invalid values back to the setting default; return
  seconds.
- Add `set_venue_photos_fresh(venue_id, photos)` (uses the fresh TTL resolver via
  `setex`) and `get_venue_photos_fresh(venue_id)` (JSON decode; tolerate empty
  list). Keep them separate from the legacy `set_venue_photos`/`get_venue_photos`
  so the legacy key and format are untouched.

### 3. On-demand resolver path (reuse `PhotoEnrichmentService`)
Add `PhotoEnrichmentService.resolve_and_cache_fresh_photos(venue_id) -> list[dict]`:
- Look up the venue's stored `google_place_id` from the system of record
  (RdsVenueStore / venue vibe attributes). If absent → cache empty list to the
  fresh key and return `[]`.
- Else call `google_places_client.get_place_photos(place_id, max_photos=photos_per_venue, max_width=800)`.
- On success (including the empty result) → write the list to the fresh key via
  `set_venue_photos_fresh` and return it.
- On exception → log with `venue_id` context and return `[]` **without** writing
  the fresh key (retry-friendly; never serve a dead URL).
- `fetch_and_cache_photos`/`refresh_photos_for_venues` (legacy-key writers) are
  left in place but no longer reachable from cron/startup/admin.

### 4. Internal resolve endpoint
Add `app/routers/internal_router.py`:
- `router = APIRouter(prefix="/internal", tags=["internal"])`, container-injection
  pattern (`set_internal_container`) like the admin router.
- `POST /internal/venues/{venue_id}/photos/resolve` → calls
  `resolve_and_cache_fresh_photos`, returns `{"venue_photos": [...]}` (a typed
  Pydantic response model with `url: str`, `author_name: Optional[str]`).
- 503 if the photo service is unconfigured (no Google key), consistent with the
  admin router's "not configured" handling; graceful empty list on resolution
  failure (never 5xx for a Google outage).
- Mount in `main.py` via `include_router(internal_router)` and inject the
  container at startup. Auth is the existing internal-surface model: Caddy does
  not expose `/internal` publicly; it is reachable only over the integrated
  compose network / VPC (same gating as `/admin`). See Open Questions on whether
  a shared-secret header is additionally required.

### 5. Retire / scope down the catalog pre-bake
- `main.py`: stop scheduling Job 5 (`photo_enrichment`) and drop the
  `photo_enrichment_on_startup` pre-bake. Keep `run_photo_enrichment_job` and the
  service method present but unreferenced by the scheduler.
- `admin_trigger_router.py`: remove the `photos` job from `JOB_REGISTRY` (and its
  `_run_job` branch) so triggering it returns the standard 404 "Unknown job",
  mirroring the documented dormant-discovery pattern; add an explanatory comment.
- Leave `_project_photos`, the legacy DAO methods, and `venue_photos_v1:*`
  untouched for compatibility.

### 6. Config
`app/config.py`: add `photo_fresh_cache_ttl_hours: int = 6` with a comment
explaining it TTLs the `venue_photos_fresh_v1:*` keyless-URL cache and is
admin-tunable via `admin_config:photo_fresh_cache_ttl_hours`. Retain
`photos_per_venue = 5`. `photo_cache_ttl_days`/`photo_enrichment_*` remain for the
now-dormant legacy path.

## Data, Config, And API Impact
- **API (new):** `POST /internal/venues/{venue_id}/photos/resolve` →
  `200 {"venue_photos": [{"url": str, "author_name": str|null}]}`. Internal
  surface (network-gated like `/admin`).
- **Redis (new key, cs-server sole writer):** `venue_photos_fresh_v1:{venue_id}`
  = JSON `[{"url","author_name"}]`, keyless URLs, TTL `photo_fresh_cache_ttl_hours`
  (default 6h, live override `admin_config:photo_fresh_cache_ttl_hours`).
- **Redis (legacy, preserved):** `venue_photos_v1:{venue_id}` and its
  format/TTL/projection are unchanged and remain read-compatible; the pre-bake
  simply stops populating it.
- **Config (new):** `photo_fresh_cache_ttl_hours` (default 6).
- **RDS:** none. No new columns; no long-term photo storage.
- **Scheduler/admin:** photo cron + `photos` admin trigger retired (scoped to
  dormant).

## Error Handling And Observability
- Graceful degradation: no `google_place_id`, zero photos, or any Google/media
  error → `{"venue_photos": []}`. Deterministic empty (no id / zero photos) is
  cached empty; exceptions are **not** cached, so a later open can retry. A
  stale/dead or key-bearing URL is never served.
- Metrics:
  - New per-media-call label on the existing Google metrics
    (`endpoint="place_photo_media"`) for success/error/duration.
  - New resolve counter (e.g. `VENUE_PHOTO_RESOLVE_TOTAL{result=resolved|empty|error}`)
    and a duration histogram for the endpoint.
- Logs include `venue_id` and photo counts; never the API key or raw Google
  payloads. Confirm the existing secret-masking in `main.py` (L42) still covers
  the media URL (the key is now header-only, so it should not appear in URLs).

## Test Plan
Feature file: `tests/bdd/api/on-demand-venue-photos.feature`

Scenarios (imperative; deterministic Google/Redis fakes, no live network):
- **Resolve returns fresh keyless URLs and caches them** — venue with a
  `google_place_id`, Google returns 3 photos → `200` with 3
  `{url, author_name}` items; every `url` is a keyless `googleusercontent.com`
  URL with no `key=` parameter and is not a `places.googleapis.com/.../media`
  URL; `venue_photos_fresh_v1:{id}` holds the same list.
- **Author attribution preserved** — the first author attribution surfaces as
  `author_name`; a photo without attribution yields `author_name` null.
- **Caps at photos_per_venue** — Google returns 8 photos, `photos_per_venue` 5 →
  exactly 5 items returned and cached.
- **Fresh TTL follows the setting** — with `photo_fresh_cache_ttl_hours` 6, the
  fresh key TTL is at most 6 hours (and positive).
- **Writes the fresh key, not the legacy key** — the resolve path writes
  `venue_photos_fresh_v1:{id}` and does **not** write `venue_photos_v1:{id}`.
- **No google_place_id → empty list** — venue without a stored `google_place_id`
  → `200 {"venue_photos": []}`; no URL-bearing entry is written.
- **Google returns zero photos → empty list cached** — `200 {"venue_photos": []}`
  and the fresh key holds an empty list.
- **Google failure degrades to empty without caching a dead URL** — Google
  resolution raises → `200 {"venue_photos": []}`, no URL-bearing fresh entry is
  written, and a subsequent successful resolve returns photos.
- **Retired catalog trigger** — triggering the `photos` admin job returns the
  standard 404 "Unknown job".

Pytest unit tests (critical internal logic):
- `GooglePlacesAPIClient.get_place_photos` calls the media endpoint with
  `skipHttpRedirect=true`, returns the keyless `photoUri`, keeps the key in the
  header (never in the URL), caps at `max_photos`, and skips a photo whose media
  call fails without failing the venue.
- `RedisVenueDAO._resolve_fresh_photos_cache_ttl_seconds` precedence and
  guard-rails (live override → setting default; non-positive/invalid rejected).
- `RedisVenueDAO.set_venue_photos_fresh`/`get_venue_photos_fresh` round-trip and
  key isolation from the legacy key.
- `PhotoEnrichmentService.resolve_and_cache_fresh_photos` branches: happy path,
  no `google_place_id`, zero photos, exception (no write).

Manual or integration checks:
- Redis integration: after a resolve call, `venue_photos_fresh_v1:{id}` exists
  with a TTL ≤ configured hours and holds keyless URLs; `venue_photos_v1:{id}` is
  untouched.

## Acceptance Criteria
- `POST /internal/venues/{venue_id}/photos/resolve` returns
  `200 {"venue_photos": [{"url","author_name"}]}` with keyless
  `googleusercontent.com` URLs (no `key=`), capped at `photos_per_venue`.
- The resolver writes `venue_photos_fresh_v1:{venue_id}` (cs-server sole writer)
  with TTL from `photo_fresh_cache_ttl_hours` (default 6h) and writes no other
  photo key.
- Missing `google_place_id`, zero photos, and Google errors all yield an empty
  list; errors never cache or serve a stale/dead URL, and a later retry can
  still succeed.
- The catalog-wide photo pre-bake (scheduled cron, startup, admin `photos`
  trigger) no longer runs; the legacy `venue_photos_v1:*` key, its DAO methods,
  and `_project_photos` remain intact.
- No new RDS columns and no long-term photo storage.

## Open Questions
- **Internal auth mechanism.** cs-server has no app-level auth token; `/admin`
  and `/internal` are gated by network topology (Caddy not exposing them; VPC /
  integrated compose network). Default: mount `/internal` the same way and rely
  on that network isolation, matching `/admin`. If the pinned contract requires
  an explicit shared-secret header between vibes_bot and cs-server, that header +
  a config setting must be added on both sides — flagged for the coordination
  plan; proceeding with the network-isolation default.
- **`google_place_id` read source at resolve time.** Default: read from the
  system of record (RdsVenueStore / venue vibe attributes), since that is where
  `google_place_id` is persisted. If a resolve must succeed while RDS is degraded
  but the Redis venue record carries `google_place_id`, add a Redis fallback.
  Proceeding with the RDS/system-of-record read as the default.
- **Cache-empty vs. no-cache for the "no photos / no place id" case.** Default:
  cache an empty list within the short window to avoid re-hitting Google on every
  open, while exceptions are never cached. Proceeding with this default.
