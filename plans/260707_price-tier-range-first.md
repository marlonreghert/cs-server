# Price tier — prefer the objective range over the coarse Google enum

## Branch
fix/price-tier-range-first

## Goal
Make the served `price_level` (1..4) reflect a venue's actual price by deriving it
from the objective Google `priceRange` (bucketed by locally-tuned BRL thresholds)
**in preference to** the coarse `priceLevel` enum. Backfill existing venues so the
correction reaches the serving projection, not just newly-enriched venues.

## Non-goals
- No 5th tier — stay 1..4 (RDS `CHECK price_level BETWEEN 1 AND 4` and the mobile
  `PriceIndicator` renderer are unchanged). 5-tier is a possible future cross-repo
  upgrade, not needed to fix this.
- No vibes_bot or mobile changes (both already pass `price_level` + `price_range`
  through / render 1..4).
- Not fixing the separate mis-geocoded foreign-venue data-quality issue
  (e.g. "Ruin Bars Budapest", "Szimpla Kert" in the Recife catalog).

## Evidence
- `app/services/price_signal.py:116-122` — `derive_price_signal` checks
  `price_level_from_enum` **first** (google_enum), then `bucket_price_range`
  (google_range), then besttime. Module docstring states "priceLevel enum PRIMARY".
- `app/config.py` — `price_range_tier_thresholds = {"BRL": [40.0, 80.0, 160.0]}`.
- `tests/bdd/enrichment/price-signal-google-source.feature` — documents the current
  enum-primary order (Background table + scenario "…the enum wins and the range is
  kept raw"). This plan inverts that behavior.
- Prod-data simulation over 907 live Recife venues (604 with a range): current
  served dist `L1 72 / L2 465 / L3 113 / L4 4 / none 253` → **~55% pile at $$**.
  Range midpoints cluster at **R$30–110**, so thresholds `[40,80,160]` are too high.
  Range-first + tuned `[40,70,110]` gives `58/282/141/185` and separates
  **Giro Praia (R$40–120) → $$$** from **Tasquinha do Tio (R$80–160) → $$$$**.
- Prod metric `venues_by_price_level_source`: `google_enum 819 · google_range 137
  · besttime 83 · none 1211` (of 2250) — the coarse enum drives ~79% of priced venues.
- Full analysis: `docs/price-score-analysis.md` in the wrapper repo.

## Current Behavior
`derive_price_signal` order is enum → range → besttime → null. When a venue has both
a Google enum and a range, the enum wins and the range is stored but unused for the
tier. Thresholds `[40,80,160]` (BRL). Existing venues keep whatever `price_level`
was computed at their last enrichment.

## Desired Behavior
Derivation order is **range → enum → besttime → null**: bucket the objective
`priceRange` first when a usable range exists (record `price_level_source` =
`google_range`); otherwise use the enum (`google_enum`); otherwise BestTime; else
null. Thresholds are tuned to the Recife BRL distribution so venues spread across
tiers. Existing venues are backfilled by re-deriving from their already-stored
`google_price_level` / `price_range` / `besttime_price_level`, and the off-loop
projector re-asserts the corrected tiers to Redis.

## Implementation Approach
- **Reorder `derive_price_signal`** (`app/services/price_signal.py`): call
  `bucket_price_range` first; on a hit return `(tier, google_range)`. Else
  `price_level_from_enum` → `(tier, google_enum)`. Else besttime → `(tier, besttime)`.
  Else `(None, None)`. Keep the never-0 contract and `GOOGLE_SOURCES` membership
  (google_range is already a Google source, so BestTime-clobber protection is
  unaffected). Update the module docstring + inline comments (currently "enum PRIMARY").
- **Re-tune `price_range_tier_thresholds`** (`app/config.py`) to the Recife BRL
  distribution. Start `[40, 70, 110]`; finalize the exact cut points from the full
  catalog's range-midpoint distribution (query RDS during execute — the 907-venue
  serving sample is a subset). `bucket_price_range` uses strict `<` cuts, so
  `[40,70,110]` yields: <40 → 1, <70 → 2, <110 → 3, ≥110 → 4.
- **Backfill** (new Alembic data migration under `migrations/versions/`, or a
  one-off invoked once): iterate venues that carry any price signal, recompute
  `(price_level, price_level_source)` via `derive_price_signal`, and UPDATE RDS.
  Idempotent and safe to re-run; no DDL/schema change (columns + CHECK unchanged).
  The projector pushes corrected tiers to the Redis serving projection on its next
  `REDIS_PROJECTION_MINUTES` cycle — no Redis key/shape change.
- **Reconcile the existing feature** `price-signal-google-source.feature`: flip the
  Background order table (google_range rank 1, google_enum rank 2) and rewrite the
  "enum wins" scenario to "range wins", adjusting tier expectations to the tuned
  thresholds. Done in execute alongside the code so it never asserts stale behavior.

## Data, Config, And API Impact
- Config: `price_range_tier_thresholds` values change.
- Persistence: one new Alembic **data** migration (recompute `price_level` +
  `price_level_source` for existing rows). No column/DDL change; `CHECK 1..4` stays.
  Redis projection key/shape unchanged.
- API: none — `price_level` + `price_range` are already in the DTO. `price_level_source`
  distribution shifts toward `google_range`.
- Mobile: none (renders 1..4).

## Error Handling And Observability
No new serving runtime path (derivation is pure). The backfill migration must log
the count of venues re-derived and the before/after `price_level_source` breakdown,
and must not fail the deploy on a single bad row (skip + log). The existing gauges
`venues_by_price_level` and `venues_by_price_level_source` will reflect the shift —
no new metric required, but confirm they re-emit after the backfill/projection.

## Test Plan
Feature file: `tests/bdd/enrichment/price-tier-range-first.feature`

Scenarios:
- When a venue has both a Google enum and a range, the objective range wins and sets
  source `google_range` (enum MODERATE + range BRL 80–160 → range-derived tier).
- Tuned thresholds separate a cheaper from a pricier venue (BRL 40–120 tiers below
  BRL 80–160).
- An enum-only venue (no usable range) still tiers from the enum (source `google_enum`).
- A BestTime-only venue still falls back to BestTime.
- A free / unpriceable venue stays null (never 0).
- An unbounded range (startPrice only) buckets from the lower bound, source `google_range`.
- Backfill re-derivation: existing venues with a stored enum+range are recomputed
  range-first (price_level + price_level_source updated); no-signal venues stay null;
  re-running the backfill changes nothing (idempotent).
- Reconcile: the existing `price-signal-google-source.feature` order table and
  enum-vs-range scenario now assert range-first.

Pytest unit tests:
- `derive_price_signal`: range wins when a usable range exists; enum fallback when no
  range; besttime fallback; null; never returns 0.
- `bucket_price_range`: boundary behavior under the tuned thresholds.
- backfill routine: recompute correctness, only-changed updates, idempotency, bad-row skip.

Manual or integration checks:
- Finalize thresholds from the full-catalog RDS range-midpoint distribution before
  committing the config value.
- After deploy + backfill, confirm via the prod `/venues` sample and the
  `venues_by_price_level` metric that the $$ pile-up drops and Giro/Tasquinha separate.

## Acceptance Criteria
- A venue with enum MODERATE and range BRL 80–160 serves the range-derived tier with
  `price_level_source = "google_range"`.
- Under the tuned thresholds, Giro Praia (BRL 40–120) and Tasquinha do Tio (BRL 80–160)
  serve different tiers.
- Existing venues are backfilled — the served `price_level` distribution no longer
  piles ~55% at $$.
- `price_level` stays 1..4 or null (never 0); Redis projection, API DTO, and mobile
  are unchanged.

## Open Questions
None. (Threshold cut points start at `[40,70,110]` and are finalized against the
full-catalog RDS distribution during execute — a tuning step, not a blocker.)
