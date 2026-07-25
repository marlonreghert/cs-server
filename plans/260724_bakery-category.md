# Add BAKERY ("Padaria") venue category and map Google "bakery" into it

## Branch
feature/bakery-category

## Goal
Introduce a new VibeSense display category `BAKERY` (label "Padaria", emoji 🥐)
and route Google Places `primaryType: "bakery"` venues into it. Today those
venues resolve to `OTHER`. cs-server owns the category set; mobile and vibes_bot
render label/emoji/color as pure passthrough, so this is a cs-server-only change.

## Non-goals
- No mobile or vibes_bot changes — category label/emoji/color flow through
  untouched (verified: `vibe_sense_mobile/src/api/venues/transform.ts` copies the
  fields 1:1; vibes_bot `venue_type_service` prefers cs-server's values).
- No new vibe-mode membership. BAKERY appears under "Todas as vibes"
  automatically (empty `allowed_types` = all-pass). Adding it to a specific mode
  (e.g. Família) is a separate, optional vibes_bot follow-up.
- No eligibility/block-list change. BestTime `BAKERY` stays in the venue-type
  block-list (out of scope); only the Google-side `bakery` mapping is added.
- The category set stays hardcoded (not admin-configurable). The admin
  type→category JSON map can point any type at BAKERY once it exists.

## Evidence
- `app/models/venue_category.py:26-44` — `CATEGORIES`, the single hardcoded
  category set (label/emoji/color); `get_category_info` (`:249`) +
  `resolve_venue_display` (`:261`) return these.
- `app/models/venue_category.py:119-188` — `_GOOGLE_TO_CATEGORY`; `bakery` is
  absent (→ OTHER today). `:48-116` — `GRANULAR_LABELS` (no `bakery`).
- `app/models/venue_category.py:333-360` — `validate_category_map_config` rejects
  any category value not in `CATEGORIES`; adding BAKERY makes it a valid target
  for the admin JSON map.
- `app/services/venue_eligibility.py` — `bakery` is NOT in the Google block-list
  (bakery venues are served); BestTime `BAKERY` IS blocked (unchanged here).
- `migrations/versions/0009_eligibility_serving_view.py:52-60,118-133` —
  `admin.category_good_type (token, kind)` + `_seed_good_types` seeds it from the
  resolve_category maps (google lowercase, besttime uppercase, `ON CONFLICT DO
  NOTHING`); `serving.eligible_venue` uses it for the `good_category` check.
- `migrations/versions/0016_hot_like_event_idempotency.py:72` — current head
  `revision = "0016_hot_like_event_idempotency"` (down_revision for 0017).
- `tests/test_eligibility_serving_view_parity.py::test_seeded_good_type_table_matches_maps`
  — real-Postgres-only parity check (skipped without `RDS_TEST_URL`) that expects
  `admin.category_good_type` to match the maps.

## Current Behavior
A venue with Google `primaryType: "bakery"` resolves to category `OTHER` (label
"Outro", 📍). It is served (not eligibility-blocked) but uncategorized. The admin
type→category map cannot target a "BAKERY" category — the validator 400s on it.

## Desired Behavior
- A `bakery` venue resolves to category `BAKERY`, label "Padaria", emoji 🥐,
  served on the next `/v1/venues/nearby` request (serve-time resolution; no
  re-fetch).
- The admin category-map validator accepts `BAKERY` as a category value.
- The SQL eligibility view's `good_category` set includes `('bakery','google')`
  so a `bakery` venue is treated consistently with Python (protected from
  name-keyword blocking), once the migration is applied.

## Implementation Approach
1. `app/models/venue_category.py`:
   - Add `"BAKERY": {"label": "Padaria", "emoji": "🥐", "color": "#B7791F"}` to
     `CATEGORIES` (warm golden-brown; distinct from BREWERY `#B45309` /
     COFFEE_SHOP `#78350F` / BAR `#D97706`).
   - Add `"bakery": "BAKERY"` to `_GOOGLE_TO_CATEGORY`.
   - Add `"bakery": "Padaria"` to `GRANULAR_LABELS` (detail-page subtitle).
2. New Alembic migration `0017_bakery_good_type` (down_revision
   `0016_hot_like_event_idempotency`):
   - upgrade: `INSERT INTO admin.category_good_type (token, kind) VALUES
     ('bakery','google') ON CONFLICT DO NOTHING` (idempotent — a fresh DB already
     seeds it via 0009's re-read of the updated map; existing prod gets the row
     here).
   - downgrade: `DELETE FROM admin.category_good_type WHERE token='bakery' AND
     kind='google'`.

## Data, Config, And API Impact
- **API**: `/v1/venues/nearby` now serves `category: "BAKERY"`, `label:
  "Padaria"`, `emoji: "🥐"` for `bakery` venues. Additive — no DTO/schema change,
  no removed fields.
- **Admin**: `POST /admin/venues/category-map` now accepts `BAKERY` as a value.
- **DB migration**: one row into `admin.category_good_type`. No schema change, no
  new column, no CHECK. Category remains unpersisted (serve-time).
- **Migration application (prod)**: the deploy (`vibes_bot` main.yml
  `sync_cs_server`) rebuilds cs-server from `main` but does NOT run alembic —
  migrations are applied manually on EC2 (`docker exec vibes_bot-cs-server-1
  alembic upgrade head`). The category + mapping work from the code deploy alone;
  the migration only closes the eligibility-view edge case (a `bakery` venue
  whose name contains a blocked keyword).

## Error Handling And Observability
No new runtime path. Resolution already falls open to `OTHER` for unknown types;
BAKERY is just a new known key. No new metric or log.

## Test Plan
Feature file: `tests/bdd/api/bakery-category.feature`

Scenarios:
- A venue whose Google primary type is `bakery` is served with category
  `BAKERY`, label "Padaria", and emoji "🥐".
- `resolve_venue_display` for a `bakery` venue returns category `BAKERY`
  (previously `OTHER`).
- The admin category-map validator accepts a mapping whose value is `BAKERY`
  (previously rejected with 400).

Pytest unit tests (`tests/test_venue_category.py`):
- `CATEGORIES["BAKERY"]` has label "Padaria" and emoji "🥐".
- `resolve_category(google_type="bakery") == "BAKERY"`; `get_granular_label(
  "bakery") == "Padaria"`.
- `validate_category_map_config({"google": {"bakery": "BAKERY"}})` succeeds and
  normalizes.

Manual or integration checks:
- After merge + deploy, run the migration on EC2 (`docker exec
  vibes_bot-cs-server-1 alembic upgrade head`) and confirm
  `admin.category_good_type` has `('bakery','google')`. The real-Postgres parity
  test `test_seeded_good_type_table_matches_maps` covers map↔table consistency
  when run with `RDS_TEST_URL`.
- Hit `/v1/venues/nearby` for a bakery venue; confirm served `category: BAKERY`.

## Acceptance Criteria
- `bakery` venues serve `category: "BAKERY"` / label "Padaria" / emoji "🥐".
- The admin category-map accepts `BAKERY` as a value.
- Migration `0017` inserts `('bakery','google')` idempotently and downgrades
  cleanly.
- No mobile/vibes_bot change required; existing categories unchanged.

## Open Questions
- None.
