# Price Signal: Google priceLevel Primary, priceRange Fallback

## Branch
fix/price-signal-google-source

## Goal
Stop serving a misleading price tier. Today `venues.venue.price_level` is a single
0–4 int where `0` means "unknown" but is rendered as the cheapest tier, and
565/1255 active venues sit at `0` — including clearly-expensive venues like *Vasto
Restaurante Recife* (Google `priceRange` BRL 80–200, which Google returns with NO
`priceLevel` enum). Re-source price from Google, **trust Google's locale-normalized
`priceLevel` enum where present (PRIMARY), and fall back to bucketing the
objective `priceRange` (real money) only to fill enum-less venues like Vasto
(FALLBACK)**, then derive a clean 1–4 tier (NULL = unknown, never 0). Persist the
raw signals so the served tier is auditable, project both the tier and the new
structured range to the Redis Venue projection, and backfill the price-relevant
active inventory.

This is the **originating** repo of an end-to-end fix sequenced cs-server →
vibes_bot → mobile. This plan covers cs-server only (RDS schema + projection +
ingestion/enrichment). vibes_bot exposes `price_range` on the venue **detail** view
and mobile renders pips (list) + range (detail) in later, separate plans.

## Non-goals
- vibes_bot serving/DTO changes and mobile rendering (separate, downstream plans).
  This plan only **produces** the corrected `price_level` tier + the new
  `price_range` in the Redis Venue projection that vibes_bot reads.
- Changing the BestTime live/weekly busyness pipeline, scoring, or any non-price
  field.
- Re-sourcing every non-price field. Enrichment re-enablement here is scoped to the
  price backfill + ongoing price freshness, not a full enrichment overhaul.
- A Google billing-tier change. We flag the cost (ENTERPRISE SKU) but do not
  negotiate or alter billing.
- History purge of committed secrets, and any other repo hygiene unrelated to
  price.

## Evidence
Verified, data-backed root cause (do not re-investigate):

- **Schema.** `migrations/versions/0001_baseline_schemas.py` defines
  `venues.venue.price_level int` (nullable). It is the system-of-record column.
- **Single 0–4 int, 0 served as cheapest.** `app/models/venue.py` (`Venue` and
  `MinifiedVenue`) carry `price_level: Optional[int]`. 565/1255 active venues are
  `0`. *Vasto Restaurante Recife* — Google `priceRange` BRL 80–200, unambiguously
  expensive — is one of them.
- **Non-zero 1–4 values are correct and came from Google's enum.**
  `_PRICE_LEVEL_ENUM_TO_INT` in
  `app/services/google_places_enrichment_service.py` maps
  `PRICE_LEVEL_INEXPENSIVE=1 … PRICE_LEVEL_VERY_EXPENSIVE=4` (and only those;
  `_FREE`/`_UNSPECIFIED` → None).
- **Mask requests only the enum.** `VIBE_FIELDS_MASK` in
  `app/api/google_places_client.py` requests `priceLevel`, never `priceRange`.
  Google now returns ONLY `priceRange` for ~19% of price-relevant venues (incl.
  Vasto) → enum is None → `_backfill_venue_review_signal` skips the price write →
  the stale `0` persists.
- **Nothing re-sources in prod.** `app/config.py`
  `google_places_enrichment_enabled: bool = False` (disabled by default), so no run
  ever corrects the stale `0`.
- **Observed coverage** (n=32 price-relevant venues): `priceLevel` present 75%,
  `priceRange` present 78%, at least one signal 94%, neither 6%.
- **Promoted-column path that must move as one set:** `app/dao/rds_venue_store.py`
  (`_VENUE_SELECT`, INSERT/ON CONFLICT, params), `app/dao/venue_row.py`
  (`COLUMN_FIELDS` / `RESIDUAL_FIELDS` / `ALL_VENUE_FIELDS` — invariant guarded by
  `tests/test_venue_row.py`), `app/models/venue.py`. `venue_from_row` →
  `VenueRepository` → projector projects the reconstructed `Venue` to Redis.
- **BestTime price path.** `app/models/venue_filter.py` (`price_level:
  Optional[int]`, already an int from BestTime) → `app/services/
  venues_refresher_service.py` builds the `Venue` with `price_level=vf.price_level`.
- **Third price write path (found during planning, not in the original RCA):**
  add-venue-by-address. `app/handlers/add_venue_handler.py:309,429` and
  `app/models/new_venue.py` set `price_level` from a Google match. The "never 0"
  rule implicates this path too — brought into scope below.
- **Observability.** `VENUES_BY_PRICE_LEVEL` (`app/metrics.py`, labels 1/2/3/4/
  unknown) and the `with_price_level` rollup in `venues_refresher_service.py` count
  `0` as a real tier today; after migration there is no `0` bucket.
- **Projector / manual migrations.** Deploy & startup do NOT run alembic (CI-only);
  migrations are applied manually via SSM `docker exec … alembic upgrade head`
  (see `migrations/versions/0012_engagement_app_session_day.py` header). Latest
  revision is `0012_engagement_app_session_day`.

## Current Behavior
- `venues.venue.price_level` is a single nullable int. Enrichment and BestTime write
  `0` (and the enum path writes None which leaves a pre-existing `0`), and serving
  renders `0` as the cheapest tier.
- Only Google's `priceLevel` enum is requested/parsed; the objective `priceRange`
  (currency + start/end money) is never fetched or stored, so enum-less venues
  (Vasto) get no Google price at all.
- No record of which source produced the served tier; no structured money range is
  served anywhere.
- `google_places_enrichment_enabled=False` in prod, so price is never corrected.

## Desired Behavior
Per the pinned cross-repo contract (field names/types MUST match vibes_bot +
mobile):

- **Served tier `price_level`** = int **1..4** or **NULL = unknown**. `0` is
  eliminated; there are 4 tiers, not 5 and not 1..5. NULL is never rendered as a
  tier.
- **Served range `price_range`** (new) = `{ "currency": <ISO code string, e.g.
  "BRL" — Google `currencyCode`, NOT a symbol>, "min": <number>, "max": <number or
  null> }` or NULL. From Google `priceRange.startPrice`/`endPrice` units.
  `endPrice` may be unbounded ("more than X") → `max` nullable/omitted. Projected so
  vibes_bot can expose it on the venue **detail** view (list = pips only).
- **Derivation order for the tier (priceLevel PRIMARY — trust Google's
  locale-normalized tier; range fills the gaps):**
  1. Google `priceLevel` enum → 1..4 (PRIMARY — Google already normalizes the tier
     per-locale, for free, where the enum is present).
  2. else bucket Google `price_range` by per-currency thresholds → 1..4 (FALLBACK —
     fills enum-less venues like Vasto, which Google returns with a range but no
     enum).
  3. else BestTime price → 1..4.
  4. else NULL.
  **Never write 0.**
- **`PRICE_LEVEL_FREE` → NULL.** The enum `PRICE_LEVEL_FREE` (and
  `PRICE_LEVEL_UNSPECIFIED`) maps to NULL, not a tier — a deliberate choice given
  the contract renders nothing for unknown price (we do not show a "free" pip).
- **"Store both" — raw signals persisted as DISTINCT, auditable columns:**
  - `google_price_level text` — raw Google enum string (e.g.
    `PRICE_LEVEL_VERY_EXPENSIVE`).
  - `price_range jsonb` — raw structured Google range (`{currency,min,max}`); see
    Open Questions for jsonb-vs-discrete-columns.
  - `besttime_price_level int` — raw BestTime price.
  - `price_level_source text` — `'google_enum' | 'google_range' | 'besttime' |
    null`, recording which rule produced the served `price_level`.
- Rationale: Google's `priceLevel` is already locale-normalized at no cost, so we
  trust it where present rather than re-deriving from money. Its exact derivation is
  undisclosed (we do NOT assert it is "subjective"). The objective `priceRange` is
  needed only to BACKFILL the ~19% of price-relevant venues (incl. Vasto) that
  Google returns with a range but no enum — it fills gaps, it never overrides a
  present enum. Per-currency thresholds are therefore required ONLY for the
  enum-absent fallback path, not for every currency in the DB.

## Implementation Approach

### 1. Migration (new Alembic revision `0013`)
New revision `0013_price_level_objective_source`, `down_revision =
"0012_engagement_app_session_day"`. Applied **manually** via SSM `docker exec …
alembic upgrade head` (deploy/startup do not run alembic). Migration header MUST
state: take an RDS snapshot BEFORE applying; slightly-stale serving data is
acceptable because the busyness pipelines overwrite live data on the next runs.

DDL (described, not written here):
- Redefine `venues.venue.price_level` to the 1..4/NULL contract: **data-migrate
  existing `0 → NULL` in the same revision** (`UPDATE venues.venue SET price_level =
  NULL WHERE price_level = 0`). Keep the column nullable int. (Optional CHECK
  `price_level IS NULL OR price_level BETWEEN 1 AND 4` — see Open Questions; if
  added it must run AFTER the 0→NULL update.)
- Add `price_range jsonb NULL`, `google_price_level text NULL`,
  `besttime_price_level int NULL`, `price_level_source text NULL`.
- Preserve the **projector-rebuildable invariant**: every new column is a promoted
  scalar/structured source-of-truth column the projector reads, so a full
  re-projection from RDS reconstructs the served Venue exactly (no field lives only
  in Redis).
- `downgrade()` drops the four new columns. The `0 → NULL` data step is NOT
  reversible (originals are lost); the downgrade body documents this and relies on
  the pre-migration RDS snapshot for true rollback. See Rollback section.

### 2. Ingestion / enrichment
- **Mask:** add `priceRange` to `VIBE_FIELDS_MASK` in
  `app/api/google_places_client.py` (keep `priceLevel`).
- **Parse:** in `_parse_place_details`, read `priceRange` (`currencyCode`,
  `startPrice.units`, `endPrice.units`; units are string-encoded integers in
  Google's `Money`). Handle unbounded `endPrice` (absent → `max = None`). Surface
  the structured range on `GooglePlacesDetailsResponse`
  (`app/models/vibe_attributes.py`) alongside the existing raw enum `price_level`.
- **Keep** the enum→1..4 map (`_PRICE_LEVEL_ENUM_TO_INT`) as the PRIMARY tier
  source. Confirm `PRICE_LEVEL_FREE` / `PRICE_LEVEL_UNSPECIFIED` → NULL (already the
  case — the map omits them), so an unknown/free venue shows no price.
- **Bucket priceRange → tier (FALLBACK, configurable per-currency thresholds).**
  This path runs ONLY when the enum is absent, so thresholds are needed only for the
  currencies of enum-less venues — not every currency in the DB. Start with BRL
  anchored to observed data. The Google enum bands OVERLAP in BRL — approximately:
  INEXPENSIVE ≈ BRL 20–40, MODERATE ≈ BRL 40–120, EXPENSIVE ≈ BRL 60–160,
  VERY_EXPENSIVE ≈ BRL 80–180 — so we cannot bucket on raw endpoints. **Bucket on a
  robust single statistic** (proposed: the range **midpoint** `(min+max)/2`, or
  `startPrice`/`min` when `endPrice` is unbounded) against monotone BRL thresholds,
  and document the exact rule in the migration/runbook. Thresholds are config-driven
  (per-currency table in `app/config.py`); a venue in a currency with no configured
  table → range path yields no tier and we fall through to besttime/NULL. Final
  numbers are an Open Question (gate execute).
- **Derive + write (single source of truth in the enrichment write path).** Replace
  the price half of `_backfill_venue_review_signal` so it: sets `google_price_level`
  (raw enum), `price_range` (structured), derives `price_level` by the order
  enum→range→besttime→NULL (**never 0**), and sets `price_level_source`. Continue to
  preserve a pre-existing non-null `price_level` only when Google returns NO price
  signal at all (don't blank out a BestTime-sourced tier); when Google returns any
  signal, Google wins per the derivation order. (`besttime_price_level` is set on
  the refresh path, below; the enricher reads it as the step-3 fallback.)
- **Add-venue-by-address path (third write path).** Route
  `app/handlers/add_venue_handler.py` (lines ~309, ~429) and `app/models/
  new_venue.py` through the same derivation helper so a manually-added venue also
  gets a 1..4/NULL tier (never 0), the raw signals, and the source. Extract the
  derivation into one shared helper (service-level) so all three write paths
  (enrichment, BestTime refresh, add-venue) agree and the "never 0" rule is enforced
  in exactly one place.
- **BestTime refresh path.** In `app/services/venues_refresher_service.py`, persist
  the raw BestTime price into `besttime_price_level` (its own column) and feed it
  into the shared derivation as step 3. Do not let BestTime overwrite a Google-range
  or Google-enum-derived tier.

### 3. RDS row mapping + projection (move as one coordinated set)
The `ALL_VENUE_FIELDS == Venue field set` invariant is asserted by
`tests/test_venue_row.py`; all of the following must change together or the suite
goes red:
- `app/models/venue.py`: add `price_range: Optional[dict]` (or a typed
  `PriceRange` model), `google_price_level: Optional[str]`, `besttime_price_level:
  Optional[int]`, `price_level_source: Optional[str]` to `Venue`. Add `price_range`
  to `MinifiedVenue` (the served projection shape vibes_bot reads); the raw audit
  fields need not appear on `MinifiedVenue` unless serving needs them (default: do
  not serve raw enum/besttime/source — they are audit-only).
- `app/dao/venue_row.py`: add the new scalar/structured fields to `COLUMN_FIELDS`
  (new promoted columns go in `COLUMN_FIELDS`, NOT residual `extra`), so
  `ALL_VENUE_FIELDS` still equals the full `Venue` field set.
- `app/dao/rds_venue_store.py`: extend `_VENUE_SELECT`, the INSERT column list, the
  `VALUES`/`ON CONFLICT DO UPDATE SET`, and the params dict with the four new
  columns (jsonb cast for `price_range`).
- Projector: no new logic — it already projects whatever `venue_from_row`
  reconstructs; confirm `price_range` + the 1..4/NULL `price_level` reach the Redis
  Venue projection and round-trip.

### 4. Backfill + ops
- **One-off re-source of price** for all PRICE-RELEVANT active venues. Select by
  Google `primaryType`/displayName heuristics — NOT the stored `venue_type` (Vasto
  is typed OTHER). Non-priceable venues (malls, parks, theatres) resolve to NULL.
- **Re-enable enrichment.** `google_places_enrichment_enabled` must be flipped on
  for the backfill and for ongoing price freshness. FLAG (billing): both `priceLevel`
  AND `priceRange` trigger the Google **Place Details ENTERPRISE SKU** — a real
  per-call cost. Scope the backfill (price-relevant only) and decide one-off vs
  monthly-cron cadence (Open Question).

### 5. Observability
- Update `VENUES_BY_PRICE_LEVEL` for the NULL-vs-tier reality: the `0` bucket is
  gone; `unknown` now means NULL (the `with_price_level` rollup in
  `venues_refresher_service.py` already maps `None → "unknown"`, so post-migration
  the old "0" simply collapses into "unknown"). Confirm no label still emits "0".
- Add a metric + log of **tier source distribution**: count venues by
  `price_level_source` (`google_enum` / `google_range` / `besttime` / `null`) so we
  can watch the enum-vs-range-fallback mix (expect enum to dominate, range to fill
  the ~19% enum-less tail) and detect regressions.

## Data, Config, And API Impact
- **Persistence (migration `0013`, manual SSM apply):** redefine
  `venues.venue.price_level` to 1..4/NULL with `0 → NULL` data migration; add
  `price_range jsonb`, `google_price_level text`, `besttime_price_level int`,
  `price_level_source text`. Promoted columns → projector-rebuildable.
- **Redis projection:** `MinifiedVenue` gains `price_range`; `price_level` is now
  1..4/NULL (no 0). Round-trips through `venue_from_row` → projector.
- **Config:** new per-currency price-range→tier threshold table (start with BRL);
  `google_places_enrichment_enabled` must be enabled for backfill + ongoing price.
- **Cross-repo contract (pinned):** `price_level` int 1..4|null; `price_range`
  `{currency:ISO string, min:number, max:number|null}|null` on the detail view.
  vibes_bot + mobile follow in sequenced downstream plans.
- **API (cs-server internal):** `GooglePlacesDetailsResponse` and the BestTime
  `Venue` build gain price-range/raw-signal handling.

## Error Handling And Observability
- Missing/partial `priceRange` (no currency, no `endPrice`, unparsable units) must
  not raise: fall through the derivation order, leaving `price_range = None` and/or
  letting enum/besttime produce the tier; log at debug with venue context, never log
  raw payloads with secrets.
- Unknown currency (no configured threshold table) → range path yields no tier; fall
  through. Log once per unknown currency for threshold-coverage visibility.
- Backfill is a background job: log per-venue failures with venue id + source
  decision, never fail the whole run on one venue; emit the source-distribution
  metric at run end.
- Metrics: update `VENUES_BY_PRICE_LEVEL` (NULL-vs-tier); add the
  `price_level_source` distribution metric/log.

## Rollback
Migration `0013` is applied manually via SSM (`docker exec … alembic upgrade
head`) — deploy/startup do not run alembic — so rollback is a deliberate, manual
operation, not an automatic one.

- **Snapshot first (hard requirement).** Take an RDS snapshot BEFORE applying
  `0013`. This is the only true rollback path, because the `0 → NULL` data step is
  irreversible (the original `0` values are lost and cannot be reconstructed).
- **Stale-data tolerance.** Slightly-stale serving data after a restore is
  acceptable: the BestTime live/weekly busyness pipelines overwrite live data on
  their next runs, and the projector re-asserts the Redis serving projection from
  RDS, so a restore self-heals within a pipeline cycle.
- **`downgrade()` scope.** `alembic downgrade` for `0013` drops the four new columns
  (`price_range`, `google_price_level`, `besttime_price_level`,
  `price_level_source`). It does NOT and cannot restore the migrated `0` values —
  the downgrade body documents this explicitly.
- **Full rollback procedure.** To fully revert: restore the pre-migration RDS
  snapshot (recovers the original `price_level` integers and removes the new
  columns), then let the next projection re-assert Redis. Do not rely on
  `downgrade()` alone for a true revert.
- **Re-enablement is also reversible.** Setting `google_places_enrichment_enabled`
  back to `False` halts ongoing price re-sourcing without any schema change.

## Test Plan
Feature file: `tests/bdd/enrichment/price-signal-google-source.feature`

Primary BDD domain is **enrichment** (the derivation + persistence of the price
signal at enrichment time). The legacy `0 → NULL` migration scenario is persistence
behavior but is co-located in this one feature file so there is a single feature for
the fix; the plan notes persistence is also touched (`venue_row`, `rds_venue_store`,
migration `0013`).

Scenarios (imperative; observable assertions on persisted/projected state):
- **Enum present (primary):** a venue whose Google details carry a `priceLevel` enum
  (e.g. `PRICE_LEVEL_VERY_EXPENSIVE`) derives the tier **from the enum** (tier 4),
  sets `price_level_source = "google_enum"`.
- **Both signals present — enum wins:** a venue with both `priceLevel` and
  `priceRange` derives the tier **from the enum** (primary), still persists the raw
  `price_range`, and sets `price_level_source = "google_enum"`.
- **Vasto-like, priceRange only (fallback):** a venue with Google `priceRange`
  (BRL, expensive) and NO enum resolves `price_level` to an expensive tier (3–4)
  **bucketed from the range as the fallback**, persists `price_range`
  `{currency:"BRL", min, max}`, and records `price_level_source = "google_range"`.
- **PRICE_LEVEL_FREE → unknown:** a venue whose enum is `PRICE_LEVEL_FREE` and which
  has no usable range yields `price_level = NULL` (no "free" tier rendered).
- **Neither signal:** a price-relevant venue with no Google price signal and no
  BestTime price yields `price_level = NULL` (**never 0**), `price_range = NULL`,
  `price_level_source = NULL`.
- **Legacy data migration:** applying `0013` converts every existing
  `price_level = 0` to `NULL` and leaves 1..4 untouched.
- **Non-priceable venue:** a mall/park/theatre (selected by Google
  primaryType/name) resolves to `price_level = NULL`, not a tier.
- (Optional) **BestTime fallback:** no Google signal but a BestTime price → tier
  from BestTime, `price_level_source = "besttime"`, raw kept in
  `besttime_price_level`.

Pytest unit tests (critical internal logic):
- The shared derivation helper: enum→range→besttime→NULL order (enum wins over a
  present range); never returns 0; `PRICE_LEVEL_FREE`/`_UNSPECIFIED` → NULL;
  per-currency bucketing on the robust statistic (midpoint / startPrice on unbounded
  end) for the fallback path; unknown-currency fall-through; unbounded `endPrice`
  handling.
- `priceRange` parsing in `_parse_place_details` (currency, units, missing
  endPrice).
- `venue_row` invariant: `ALL_VENUE_FIELDS == Venue` field set after adding the four
  fields (guards `tests/test_venue_row.py`).
- `rds_venue_store` round-trip: upsert→select preserves the four new columns +
  1..4/NULL `price_level`.

Manual or integration checks:
- Apply `0013` on a staging RDS copy; verify `0 → NULL`, no served `price_level = 0`,
  `price_range` populated for a Vasto-like venue, projector re-projection reproduces
  the served Venue.
- Confirm `make test-bdd` excludes `@wip` so the stepless feature does not break the
  suite.

## Acceptance Criteria
- A venue with a Google `priceLevel` enum resolves to the tier **from the enum**
  (e.g. `PRICE_LEVEL_VERY_EXPENSIVE` → 4), `price_level_source = "google_enum"`,
  even when a `priceRange` is also present.
- An enum-less expensive restaurant with a Google `priceRange` (e.g. Vasto, BRL
  80–200) resolves to tier **3–4 bucketed from the RANGE as the fallback** — not 0
  and not 1 — with `price_level_source = "google_range"`.
- `price_range` `{currency, min, max}` is persisted and reaches the Redis Venue
  projection (detail view).
- `price_level_source` records which rule produced the served tier.
- **No served `price_level` is ever 0**, across enrichment, BestTime refresh, AND
  add-venue-by-address.
- `priceRange` bucketing is used only when the `priceLevel` enum is absent;
  `PRICE_LEVEL_FREE`/`_UNSPECIFIED` resolve to NULL (no tier shown).
- Migration `0013` converts all legacy `0 → NULL`; 1..4 values are unchanged.
- The `venue_row` field-set invariant still holds (suite stays green).
- The price-relevant active inventory is backfilled with the corrected tier + range.

## Open Questions
(Must be resolved before `/execute-feature`; they gate execute, not this plan.)
- **Final per-currency bucket thresholds and the exact robust statistic — for the
  enum-less FALLBACK path only.** Because the range bucket runs only when the enum
  is absent, thresholds are needed only for the currencies of enum-less venues, not
  every currency in the DB (narrower scope than the original plan). Proposed: bucket
  on the range midpoint (or `startPrice`/`min` when `endPrice` is unbounded) against
  monotone BRL thresholds; the enum bands overlap so raw endpoints can't be used
  directly. Numbers are unresolved.
- **Unbounded `endPrice` handling.** Confirm `max = NULL` semantics end-to-end and
  the bucketing rule when only `startPrice` is known ("more than X").
- **Backfill cadence.** One-off re-source now vs folding price re-source into the
  existing monthly enrichment cron (and the ENTERPRISE-SKU billing impact of each).
- **`price_range` storage shape.** Single `jsonb` column vs discrete
  `price_currency text` / `price_min numeric` / `price_max numeric` columns
  (jsonb is simpler for the projection; discrete columns are queryable). Default
  assumption for this plan: `jsonb`.
- **Optional CHECK constraint** `price_level IS NULL OR price_level BETWEEN 1 AND 4`
  — adopt as a hard guard, or rely on the derivation helper alone?
