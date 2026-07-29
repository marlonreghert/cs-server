# Photo Resolve Cost Controls

## Branch
fix/photo-resolve-cost-controls

## Goal

Cut the Google photo bill without changing what any screen shows, by letting a
caller ask for **only the photos it will actually display**, and by keeping a
resolved URL usable for a day instead of six hours.

Three changes, all inside the on-demand resolve path:

1. `POST /internal/venues/{venue_id}/photos/resolve` accepts `max_photos`, so
   the venue list can ask for the single hero it renders instead of five.
2. The fresh cache **upgrades in place**: a cached entry that holds fewer photos
   than the caller needs is re-resolved and overwritten, so a hero-only entry
   never truncates the detail carousel.
3. The fresh-cache TTL default moves 6h → 24h.

## Non-goals

- Reviving the catalogue-wide photo pre-bake. `refresh_photos_for_venues` stays
  dormant with no scheduled, startup, or admin trigger.
- Serving photos from the S3 `media/` archive. Every displayed image still comes
  from Google's CDN.
- Reducing how many photos the **venue detail** shows. It keeps all five
  (`photos_per_venue`); only *when* they are fetched changes, and that is a
  vibes_bot/mobile concern.
- Storing photo resource names. Still forbidden, still not done.
- Any change to the legacy `venue_photos_v1:*` key, its format, or its TTL.

## Evidence

**The bill is one SKU, and it is per photo.** Cloud Console, 1–28 July 2026,
project VibesenseProdDev: R$128.19 total, attributed entirely to
`Places API Place Details Photos` — down 31% from June. The Place Details call
that returns the photo names carries the field mask
`photos.name,photos.authorAttributions`
(`app/api/google_places_client.py:22`), and `photos` is an **Essentials
(IDs Only)** field: $0, unlimited. The billed unit is the media fetch, at
$7.00/1,000 with 1,000 free per month.

**We buy five photos and the list shows one.** `get_place_photos`
(`app/api/google_places_client.py:509`) loops `photos[:max_photos]` calling
`_resolve_photo_media_uri` once per photo — one billed call each — with
`max_photos` fixed at `settings.photos_per_venue` (5) by
`resolve_and_cache_fresh_photos` (`app/services/photo_enrichment_service.py:176`).
vibes_bot's list card then serves `photos[0]` only (`hero_photo`,
`vibes_bot/app/services/photo_resolver.py:36`). **Four of every five photos
bought for a list row are discarded.**

**Nothing pre-warms the cache.** The only writer of `venue_photos_fresh_v1:*` is
`set_venue_photos_fresh`, reached solely from `resolve_and_cache_fresh_photos`
via `POST /internal/venues/{id}/photos/resolve`
(`app/routers/internal_router.py:46`). Verified by grepping every call site. So
every resolve is user-driven and every TTL expiry costs the photos again.

**The TTL is already admin-tunable.**
`_resolve_fresh_photos_cache_ttl_seconds` (`app/dao/redis_venue_dao.py:729`)
reads `admin_config:photo_fresh_cache_ttl_hours`, falling back to
`settings.photo_fresh_cache_ttl_hours` (6). Raising it in production is a
vibesadmin edit; this plan moves the code default so a fresh environment agrees.

**Failure handling is already correct and must be preserved.**
`resolve_and_cache_fresh_photos` caches `[]` when there is no `google_place_id`
or when Google returns zero photos (both stable facts, worth caching), and
returns `[]` **without writing** on any Google exception, so a transient outage
never poisons the cache.

## Current Behavior

1. Every resolve fetches `photos_per_venue` (5) media URLs and caches the list
   under `venue_photos_fresh_v1:{venue_id}` for 6 hours.
2. A caller that needs one photo pays for five.
3. A cached entry is served as-is; there is no notion of it being "not enough".
4. After 6 hours the entry expires and the next viewer pays for five again.

## Desired Behavior

1. `POST /internal/venues/{venue_id}/photos/resolve` accepts an optional
   `max_photos` (1..`photos_per_venue`, default `photos_per_venue`). Exactly
   that many media calls are made. Omitting it is byte-for-byte today's
   behaviour.
2. **Partial-entry upgrade.** On a resolve for `max_photos = N`:
   - cached list has `>= N` entries → serve the first `N`, **no Google call**;
   - cached list is empty (`[]`) → serve `[]`, **no Google call** (empty is a
     definitive answer, not a shortfall);
   - cached list is non-empty but has `< N` entries → re-resolve `N` and
     overwrite the key, so the richer list replaces the poorer one;
   - no cached entry → resolve `N` and write.
3. The TTL default becomes 24 hours. A resolved URL survives a full day, so a
   venue is re-purchased at most once per day instead of up to four times.

   **Where that saving actually comes from.** The billed event is the *resolve*
   (`_resolve_photo_media_uri` → `places.googleapis.com/.../media`), not the
   display — the app fetches the image itself from `lh3.googleusercontent.com`,
   keyless and free, however many times it renders. So a longer TTL saves
   nothing on a venue seen once and everything on a venue seen across several
   windows. Because the list is ranked identically for every user, the head of
   the ranking is re-resolved in nearly every active window, and after the
   mobile paging change most sessions never leave that head. The saving is
   therefore up to 4× and it concentrates on precisely the venues that dominate
   the bill. It is monotonic: a longer TTL can never cost more.
4. A forced re-resolve (vibes_bot's dead-URL retry) always calls Google and
   always overwrites, ignoring the cache — this is the existing behaviour of the
   endpoint and must not regress, because it is the only way a dead URL is
   replaced before the TTL rolls.

## Implementation Approach

**`app/api/google_places_client.py`** — no change. `get_place_photos` already
takes `max_photos`; it is the caller that hardcodes 5.

**`app/services/photo_enrichment_service.py`** — `resolve_and_cache_fresh_photos`
gains `max_photos: Optional[int] = None` (clamped to
`1..settings.photos_per_venue`) and the cache-read/upgrade decision above. The
method currently never reads the cache — the read-through lives in vibes_bot —
so the upgrade check is new here and must be cheap: one `get_venue_photos_fresh`
before deciding to call Google.

**`app/routers/internal_router.py`** — `max_photos` as an optional query
parameter, validated by FastAPI (`ge=1`, `le=photos_per_venue`). Absent → today's
behaviour. The response shape is unchanged.

**`app/config.py`** — `photo_fresh_cache_ttl_hours: int = 6` → `24`. The
docstring's rationale ("a few hours amortizes repeated opens without serving a
rotated/dead URL") is updated to record why a day is now acceptable: vibes_bot
gains a dead-URL retry that forces a re-resolve and overwrites this key, so a
rotated URL is repaired on first sighting instead of waiting out the TTL. **That
retry must ship before the TTL is raised in production** — see the rollout note.

**`app/metrics.py`** — `VENUE_PHOTO_RESOLVE_TOTAL` gains the results
`cache_hit` (served from the cached entry, no Google call) and `upgraded`
(re-resolved because the entry held fewer photos than requested), so the saving
is measurable rather than assumed. A new histogram is not needed; photos
actually fetched per resolve is worth a counter — `VENUE_PHOTOS_FETCHED_TOTAL` —
because that counter *is* the bill.

## Data, Config, And API Impact

| Boundary | Change | Compatibility |
|---|---|---|
| `POST /internal/venues/{id}/photos/resolve` | new optional `max_photos` query param | Additive; omitting it preserves current behaviour exactly |
| Response body | unchanged (`{"venue_photos": [...]}`) | — |
| `settings.photo_fresh_cache_ttl_hours` | 6 → 24 | `admin_config:photo_fresh_cache_ttl_hours` still overrides at runtime |
| `venue_photos_fresh_v1:{id}` | same key, same JSON shape; may now hold 1 entry instead of 5 | Readers already handle any length; vibes_bot's `hero_photo` takes `[0]` |
| Legacy `venue_photos_v1:*` | untouched | — |
| RDS / migrations | None | — |

**Cost effect.** A list-driven resolve drops from 5 billed photos to 1 (−80% on
that path). The 24h TTL divides repeat purchases by 4. Combined, the same
browsing behaviour costs roughly 1/20th. The detail screen still buys 5, but
only when a venue is actually opened.

## Error Handling And Observability

- Every existing degradation is preserved: no `google_place_id` → cache `[]`;
  zero photos → cache `[]`; any Google exception → return `[]` **without**
  writing.
- A Redis read failure during the upgrade check is treated as a cache miss (
  resolve and write), never as a hard error — the endpoint must not start
  failing because Redis hiccuped.
- An out-of-range `max_photos` is a 422 from FastAPI validation, not a silent
  clamp, so a caller bug is visible.
- Metrics: `VENUE_PHOTO_RESOLVE_TOTAL{result="cache_hit"|"upgraded"}` and
  `VENUE_PHOTOS_FETCHED_TOTAL`. The latter is the only number that tracks the
  invoice; watch it, not the resolve count.

## Test Plan

Feature file: `tests/bdd/enrichment/photo-resolve-cost-controls.feature`

Scenarios:

- **A hero-only resolve buys one photo** — `max_photos=1` on an uncached venue
  makes exactly one media call and caches one entry.
- **Omitting max_photos is unchanged** — a resolve without the parameter buys
  `photos_per_venue` photos, exactly as today.
- **A sufficient cached entry costs nothing** — requesting 1 when 5 are cached
  serves the first and makes no Google call.
- **A partial entry upgrades** — requesting 5 when 1 is cached re-resolves 5 and
  overwrites the key.
- **An empty cached entry is definitive** — requesting 5 when `[]` is cached
  serves `[]` and makes no Google call.
- **A Google failure never poisons the cache** — the key is left untouched and
  the caller receives an empty list.
- **A venue with no google_place_id caches empty** — unchanged.
- **The written TTL is the resolved 24h default** — and an
  `admin_config:photo_fresh_cache_ttl_hours` override still wins.
- **An out-of-range max_photos is rejected** — 0 and `photos_per_venue + 1` both
  return 422.
- **The photos-fetched counter matches the media calls made** — for a hero-only
  resolve, a full resolve, and an upgrade.

Pytest unit tests:

- `resolve_and_cache_fresh_photos` clamp/upgrade decision table (cached length ×
  requested length → call Google or not).
- `_resolve_fresh_photos_cache_ttl_seconds` returns 86400 by default and honours
  the admin override.
- Redis read failure during the upgrade check degrades to a resolve.

## Acceptance Criteria

1. A caller asking for one photo is billed for one photo.
2. A caller asking for five never receives fewer than five because a hero-only
   resolve got there first.
3. A resolve with no `max_photos` behaves exactly as it does today.
4. Cached URLs live 24 hours by default and the admin override still works.
5. Every existing failure path (no place id, zero photos, Google error) behaves
   exactly as before.
6. `VENUE_PHOTOS_FETCHED_TOTAL` reflects the true number of billed media calls.
7. No pre-bake is revived, no photo names are stored, no S3 serving.

## Rollout Note

**The 24h TTL must not be enabled in production until vibes_bot's dead-URL
retry is deployed.** Today a URL that dies inside the TTL window is served to
every user until the key expires; lengthening the window without the repair path
multiplies that exposure by four. The code default may ship first — production
keeps 6h via `admin_config:photo_fresh_cache_ttl_hours` until the retry is live,
then the override is removed.

## Open Questions

1. **How long do the keyless `googleusercontent.com` URLs actually live?**
   Undocumented. The only datapoint on record is that re-resolving a photo name
   returns a byte-identical URI 100 minutes later. 24h is a deliberate bet, made
   safe by the retry rather than by evidence. If dead-URL retries turn out to be
   frequent after the change, lower the admin override — no redeploy needed.
2. **Does a 24h cache of a resolved Google photo URL sit within Places API
   terms?** The 6h value was chosen as a deliberate compliance window. Raising
   it is a policy judgement, not an engineering one, and is the one item in this
   plan that is not ours alone to decide.
