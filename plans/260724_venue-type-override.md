# Per-venue type override (cs-server)

## Branch
feature/venue-type-override

## Goal
Let an operator correct a single mis-typed venue — e.g. "IRAQ", enriched by
Google as `art_museum` but actually a nightclub — by storing a corrected,
**locked** `google_primary_type`. Because category and eligibility both key off
`google_primary_type`, flipping it (`art_museum` → `night_club`) simultaneously
**un-blocks** the venue (the live eligibility view stops excluding it) and
**recategorizes** it (`night_club` → NIGHTCLUB); the projector restores it to
serving on the next rebuild. The lock protects the correction from being
clobbered by re-enrichment. cs-server owns storage + the enrichment guard + the
admin endpoints; vibes_bot (separate plan) adds the per-venue editor UI + proxy.
Mobile is unaffected (category label/emoji/color are pure passthrough).

## Non-goals
- No serving-view, projector, or serve-path changes — they already key off
  `google_primary_type`; storing a corrected value is sufficient.
- No mobile or vibes_bot logic here (vibes_bot only proxies + renders the editor).
- Not a general per-venue field editor — only the type/category correction.
- Restoring a venue deprecated by **permanent closure** or **admin geo-link-undo**
  (those set `lifecycle_status='deprecated'`) is out of scope: the eligibility
  view filters `lifecycle_status='active'`, so a type override alone won't
  resurface a deprecated venue.

## Evidence
- `app/services/google_places_enrichment_service.py:345` — unconditional
  `vibe_attrs.google_primary_type = details.primary_type`, then `set_vibe_attributes`
  (`:353`). `force_refresh=False` runs skip already-enriched venues (`:507`); only
  `force_refresh=True` reaches `:345` (`:534`).
- `app/dao/venue_repository.py:155-163` — `set_vibe_attributes` upserts the JSON
  `payload` AND the promoted columns; `app/dao/rds_venue_store.py:36-39` promoted
  set `["google_primary_type","google_place_id"]`; `:380-396` upsert overwrites
  both `payload` and promoted columns on conflict.
- `app/models/vibe_attributes.py:16-24` — `VibeAttributes` (has `venue_id`,
  `google_place_id`, `google_primary_type`); serialized to `payload`.
- `migrations/versions/0009_eligibility_serving_view.py:62-111` —
  `serving.eligible_venue` view: blocked-google-type + good_category both key off
  `lower(vibe_attributes.google_primary_type)`; filters `lifecycle_status='active'`.
- `app/services/redis_projection_service.py:99,146,189-205` — projector re-asserts
  the serving set from the view each cycle and reconciles Redis in both directions
  (a now-eligible active venue reappears). `tests/test_redis_projection.py:115-132`
  proves block/unblock reversibility.
- `app/routers/admin_trigger_router.py` — `POST /trigger/{job_name}` with
  `rebuild_redis` (job `:186-190`, runner `:96-102` → `rebuild_redis_from_rds`);
  existing per-type admin endpoints (`/venues/eligibility-config`,
  `/venues/category-map`) are the endpoint pattern.
- `app/models/venue_category.py` — `_GOOGLE_TO_CATEGORY` (forward map) and
  `CATEGORIES`; no reverse map exists yet.
- `migrations/versions/0017_bakery_good_type.py` — current alembic head.

## Current Behavior
A venue's `google_primary_type` is set only by Google enrichment. If Google types
a nightclub as `art_museum`, the venue (a) is excluded by the eligibility view
(`art_museum` is a default `blocked_google_type`) and (b) would resolve to OTHER.
There is no per-venue correction — every admin lever (eligibility, category-map)
is type-level, and the per-venue editor was removed (returns 410).

## Desired Behavior
- An operator sets a per-venue type correction by choosing a target category; the
  venue is stored with the representative `google_primary_type` for that category
  and marked locked, then re-projected — it serves in the chosen category and is
  no longer blocked (if it was blocked only by type and is still `active`).
- Re-enrichment (including `force_refresh=True`) does not overwrite a locked
  `google_primary_type`.
- Clearing the correction unlocks the venue; a subsequent `force_refresh`
  enrichment restores Google's value.

## Implementation Approach
1. **Migration `0018_vibe_attributes_primary_type_locked`** (down_revision
   `0017_bakery_good_type`): `ALTER TABLE google_places.vibe_attributes ADD COLUMN
   IF NOT EXISTS primary_type_locked boolean NOT NULL DEFAULT false;` (reversible
   downgrade drops it).
2. **Model + round-trip**: add `primary_type_locked: bool = False` to
   `VibeAttributes` (so it serializes into `payload`); add `primary_type_locked`
   to the promoted set for `google_places.vibe_attributes` in `rds_venue_store`
   (`:36-39`) and ensure `get_enrichment`/reconstruction returns it. It round-trips
   as both a promoted column and a payload field.
3. **Enrichment guard** (`google_places_enrichment_service.py` around `:345`):
   before overwriting `google_primary_type`, load the existing stored row for the
   venue; if `primary_type_locked` is true, keep the existing `google_primary_type`
   and keep the lock (skip the overwrite) — even under `force_refresh`. Other vibe
   attributes may still update. `force_refresh=False` already skips enriched
   venues, so this guard specifically defends the `force_refresh=True` path.
4. **Reverse map** in `venue_category.py`: `REPRESENTATIVE_GOOGLE_TYPE`
   (category → representative google type) + `representative_google_type(category)`
   returning the type or None on unknown. Mapping: BAR→bar, PUB→pub,
   NIGHTCLUB→night_club, COCKTAIL_BAR→cocktail_bar, KARAOKE→karaoke,
   BREWERY→brewery, WINERY→winery, COFFEE_SHOP→coffee_shop, RESTAURANT→restaurant,
   BUFFET→buffet_restaurant, FOOD_DRINK→bistro, EVENT_VENUE→event_venue,
   LIVE_MUSIC→performing_arts_theater, CASINO→casino, ENTERTAINMENT→video_arcade,
   PARK→park, BAKERY→bakery. Every value is a key in `_GOOGLE_TO_CATEGORY`, not in
   the default block-list, and a seeded good type. (OTHER has no representative →
   rejected: you cannot "correct" a venue to OTHER.)
5. **Admin endpoints** (`admin_trigger_router`, prefix `/admin`):
   - `POST /venues/{venue_id}/type-override` body `{"category":"<CATEGORY>"}` →
     validate category has a representative type (else 400); load existing
     `vibe_attributes` (404 if the venue has none); set
     `google_primary_type = representative`, `primary_type_locked = true` via
     `set_vibe_attributes`; trigger `rebuild_redis` off-loop (non-fatal on
     failure — the periodic projector still applies it); return
     `{venue_id, google_primary_type, category}`.
   - `DELETE /venues/{venue_id}/type-override` → set `primary_type_locked = false`
     (404 if missing); return `{venue_id, status:"unlocked"}`. Revert to Google's
     value happens on the next `force_refresh` enrichment.

## Data, Config, And API Impact
- **Migration**: one new column `google_places.vibe_attributes.primary_type_locked`
  (boolean, default false). **Hard prerequisite for the endpoints** — until it's
  applied, code reading `primary_type_locked` errors. Applied MANUALLY on EC2
  (`docker exec vibes_bot-cs-server-1 alembic upgrade head`); the deploy does not
  run alembic.
- **New endpoints**: `POST` / `DELETE /admin/venues/{venue_id}/type-override`.
- **No serving DTO change**; category/label/emoji already flow from
  `google_primary_type`.
- **Design note**: overriding stores a *representative* google type, so the
  venue's granular label reflects that type (e.g. NIGHTCLUB → "Casa Noturna").
  Acceptable — the correction is category-level by intent.

## Error Handling And Observability
- Validation: unknown/representative-less category → 400; missing venue
  `vibe_attributes` → 404. The RDS write must succeed or 5xx.
- `rebuild_redis` trigger failure is non-fatal (logged); the ~2-min periodic
  projector applies the change as fallback.
- Log override set/clear with `venue_id` + category. No new Prometheus metric
  (rare, admin-initiated action).

## Test Plan
Feature file: `tests/bdd/persistence/venue-type-override.feature`

Scenarios:
- Setting a type override stores the representative google type + lock, and the
  venue resolves to the chosen category.
- A `force_refresh` re-enrichment does NOT overwrite a locked `google_primary_type`
  (the override survives).
- A normal (non-force) enrichment also preserves the override.
- Clearing the override unlocks the venue so a later `force_refresh` restores
  Google's value.
- Overriding to an unknown category (or OTHER) → 400.
- Overriding/clearing a venue with no `vibe_attributes` → 404.

Pytest unit tests:
- Enrichment guard: locked row preserves `google_primary_type` on `force_refresh`;
  unlocked row is overwritten.
- `REPRESENTATIVE_GOOGLE_TYPE`: every non-OTHER `CATEGORIES` key has a
  representative that is present in `_GOOGLE_TO_CATEGORY` and not in
  `DEFAULT_BLOCKED_GOOGLE_TYPES`; `representative_google_type` returns None for
  OTHER/unknown.
- `primary_type_locked` round-trips through `rds_venue_store` (promoted column +
  payload).

Manual or integration checks:
- After merge + deploy, apply the migration on EC2, then `POST
  /admin/venues/{IRAQ}/type-override {"category":"NIGHTCLUB"}` and confirm IRAQ
  serves as NIGHTCLUB via `/v1/venues/nearby` after the projector cycle.

## Acceptance Criteria
- A blocked, mis-typed, still-active venue becomes served in the corrected
  category after a type override (no re-fetch, no manual un-delete).
- A locked override survives `force_refresh` re-enrichment.
- Clearing the override unlocks it (Google's value returns on next force refresh).
- Unknown category → 400; missing venue → 404.
- `primary_type_locked` round-trips (column + payload); existing behavior for
  unlocked venues unchanged.

## Open Questions
- None.
