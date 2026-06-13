# Eligibility as a gold-layer serving view (true SQL view)

## Branch
feature/eligibility-serving-view

## Goal
Make venue eligibility a **dynamic SQL view** (the "gold" serving layer) that the
Redis projector reads, instead of a destructive soft-delete sweep. A venue is
served iff it is active AND passes the eligibility block-list, evaluated live in
SQL from the `admin.eligibility_rule` rows. Editing the block-list then takes
effect on the next projection in **both directions** — block a type and its
venues leave serving; unblock it and they return — with no soft-delete, no
reactivation, and full reversibility.

**Scope is the gold view only.** Build a single true SQL view over the *existing*
schema. Do **not** restructure RDS into bronze/silver/gold tables now — that is a
deliberate follow-up. This plan also carries a **heavy validation** workstream
(pre-change snapshots + Redis/RDS pre/post-migration metric comparison) because
it changes the serving source and migrates prod lifecycle data.

## Non-goals
- **No bronze/silver/gold table restructuring** — one SQL view over today's
  schema; the medallion table redesign is a separate follow-up plan.
- **No venue-restore / per-venue reactivate / reconcile** — superseded; this view
  makes eligibility reversible by design (`plans/260613_venue-restore.md` dropped).
- **Google "permanently closed" stays a real soft-delete.** It is a true removal,
  not a policy filter; the view selects only `lifecycle_status='active'`, so
  closed/deprecated venues are excluded regardless. Eligibility never soft-deletes.
- No change to the eligibility *rules* model/admin API (the merged
  eligibility-admin-panel keeps editing `admin.eligibility_rule`).
- No mobile or public-DTO change — serving shape is unchanged; only *which*
  venues are projected changes.

## Evidence
- `app/services/redis_projection_service.py` `rebuild_redis_from_rds` — iterates
  `list_active_venue_ids()`, upserts each (venue + enrichments + photos + weekly +
  live), and deletes `list_deprecated_venue_ids()` from Redis. This is where the
  serving set is built; it must instead reconcile Redis to the **view's** set.
- `app/dao/rds_venue_store.py` — `list_active_venue_ids()`/`list_deprecated_venue_ids()`
  (SELECTs on `lifecycle_status`); `soft_delete_venue` (the eligibility sweep's
  current mechanism, to be retired for eligibility); `_VENUE_SELECT` join shape;
  `admin.eligibility_rule` accessors (`list_eligibility_rules` etc.); the
  `google_places.vibe_attributes` promoted column `google_primary_type`.
- `app/services/venue_eligibility.py` — `evaluate()` order (empty name → blocked
  google type → blocked besttime type → good-category-suppressed hard keyword →
  good-category-suppressed ambiguous keyword → eligible) and `RULE_TYPE_TO_BLOB_KEY`.
  `resolve_category(google_type, besttime_type)` (`app/models/venue_category.py`)
  is a large static map; **"good category" = resolves to non-`OTHER`** and is the
  only non-trivial part to express in SQL.
- `app/services/venues_refresher_service.py` `run_eligibility_sweep` — the
  soft-delete path to retire (its serving role moves into the view); also the
  enrichment jobs select active venues to enrich (budget).
- `app/handlers/venue_handler.py` — cs-server's serve-time Python eligibility
  filter; becomes redundant once Redis is pre-filtered.
- Live prod facts (verified via SSM): the override `blocked_google_type` rows
  omit `book_store`/`library` (so those serve through); ~159 venues are
  `deprecated_source='eligibility_filter'`; `REDIS_PROJECTION_MINUTES=2`.

## Current Behavior
Eligibility is applied destructively: the sweep soft-deletes ineligible active
venues in RDS, the projector drops deprecated venues from Redis, and unblocking a
type cannot bring venues back (no reactivation). The serving set is "active AND
not soft-deleted-by-anything".

## Desired Behavior
- A SQL view (e.g. `serving.eligible_venue`) returns the `venue_id`s of venues
  that are `lifecycle_status='active'` **and** eligible under the live rules:
  > name non-empty AND `google_primary_type` ∉ blocked-google-types AND
  > `venue_type` ∉ blocked-besttime-types AND NOT (matches a hard/ambiguous name
  > keyword AND the venue is not a "good category").
  Block-lists and keywords come from `admin.eligibility_rule`; "good category"
  comes from a seeded classification (below). Unlabeled venues (no Google type,
  non-good BestTime type, no keyword hit) are eligible — unknowns still reach
  serving, matching today's block-list policy.
- The projector **reconciles Redis to exactly the view's set**: project every
  view venue (with its enrichments/photos/forecasts as today), and remove from
  Redis any member not in the view (deprecated OR active-but-ineligible). Rule
  edits are reflected on the next cycle, both directions.
- **Enrichment gates on the view**: enrichment jobs skip view-excluded venues so
  known junk does not burn Google/Apify/OpenAI/photo budget — but unlabeled
  venues are in the view (eligible until labeled), so first-time enrichment still
  happens and we still learn types.
- The eligibility **sweep no longer soft-deletes**; eligibility never touches
  `lifecycle_status`. The cs-server serve-time Python filter is removed (Redis is
  pre-filtered). The Redis `admin_config:venue_eligibility` mirror leaves the
  serving path (the view reads the RDS rows directly).
- **Migration:** the ~159 `eligibility_filter`-deprecated venues are reactivated
  (`lifecycle_status='active'`, `deprecated_*` cleared) so the view governs them;
  ones still ineligible simply stay out of the view (not served, not deleted).
- Google permanently-closed venues remain `deprecated` and excluded.

## Implementation Approach
1. **Rules + category inputs the view can read in pure SQL:**
   - **Seed default rules as rows** (one-time migration) so `admin.eligibility_rule`
     is the complete source and the view needs no SQL-side defaults. (The merged
     admin panel already materializes defaults on save; this guarantees it.)
   - **Seed a good-category classification** from Python `resolve_category`: a
     migration computes, for every known Google type and BestTime type token,
     whether it resolves to non-`OTHER`, into a small table (e.g.
     `admin.category_good_type(token, kind)`), so the view's keyword suppression
     is a simple `EXISTS` lookup rather than re-encoding the mapping. A **parity
     test** regenerates this from the Python map and asserts no drift.
2. **The view** (Alembic migration): `CREATE VIEW serving.eligible_venue` joining
   `venues.venue` + `google_places.vibe_attributes` (for `google_primary_type`) +
   the rule rows + the good-type table, encoding the eligibility predicate above,
   filtered to `lifecycle_status='active'`. Add a thin DAO method
   `list_servable_venue_ids()` reading the view.
3. **Projector** (`redis_projection_service`): drive the project loop from
   `list_servable_venue_ids()`; replace the "delete deprecated" pass with
   "reconcile": delete any Redis geo member / venue key not in the view's set.
4. **Enrichment**: gate the enrichment jobs' venue selection on view membership
   (skip ineligible); keep unlabeled venues in scope.
5. **Retire**: the sweep's eligibility soft-delete; `venue_handler`'s Python
   eligibility filter; remove the eligibility mirror from the serving read path.
   Keep `evaluate()` as the parity reference (and category source) — do not delete
   it; the parity test pins SQL-vs-Python equivalence.
6. **Migration**: reactivate `eligibility_filter`-deprecated venues; seed default
   rules + good-type table. Sequence for safety (see Validation): build view +
   parity → snapshot → cut projector to the view (served set ~unchanged) →
   reactivate deprecated → re-validate.
7. **Near-real-time (optional, flagged):** optionally trigger a projection pass on
   eligibility-config save so edits apply in seconds rather than ≤2 min; default
   is the existing 2-min cadence. (Admin-config Redis mirror writes are already
   synchronous; the projection cadence is the only lag.)

## Data, Config, And API Impact
- **New:** a SQL view + a small seeded good-type table + an Alembic migration; a
  one-time data migration reactivating `eligibility_filter`-deprecated venues +
  seeding default rules. No change to `venues.venue` columns or the public DTO.
- **Behavioral:** the projector's source becomes the view; enrichment selection
  gains an eligibility gate; the sweep stops soft-deleting for eligibility.
- **Config:** none new. The Redis eligibility mirror is no longer read for serving.

## Error Handling And Observability
- View/DB read failure in the projector must fail safe: log + abort the cycle
  without corrupting Redis (never blanket-delete on a failed read). Keep the
  existing per-venue error isolation.
- Metrics: `SERVING_VIEW_VENUES` (gauge: view size), `PROJECTION_REMOVED_TOTAL`
  (venues reconciled out), `VENUES_REACTIVATED_TOTAL{source="eligibility_filter"}`
  for the migration, and keep the active/deprecated gauges. Log projection summary
  with view size + removed count.

## Test Plan
Feature file: `tests/bdd/persistence/eligibility-serving-view.feature`

Scenarios (behave, deterministic fakes):
- The view returns only active, eligible venues; a blocked Google type is absent
  and an unlabeled venue is present.
- Unblocking a Google type makes its (active) venues enter the view; blocking it
  removes them — no `lifecycle_status` change either way.
- A hard/ambiguous name keyword is suppressed for a good-category venue and
  applied for a non-good-category one (the SQL good-type lookup).
- The projector reconciles Redis to exactly the view set: a venue that leaves the
  view is removed from the geo index + venue key; one that enters is projected.
- Enrichment skips a view-excluded venue but still enriches an unlabeled one.
- A Google permanently-closed (deprecated) venue is excluded from the view.

Pytest unit tests:
- The view predicate via the DAO over an in-memory/SQLite-or-fixture fixture:
  each eligibility branch (empty name, blocked google, blocked besttime, keyword
  ± good-category) matches expectations.
- `list_servable_venue_ids` and the projector reconcile logic (add/remove).

**Parity test (critical — guards SQL↔Python drift):** run the documented
eligibility fixtures through BOTH `evaluate()` and the SQL view and assert the
eligible/ineligible verdict matches for every case; regenerate the good-type
table from `resolve_category` and assert it is unchanged.

**Heavy migration validation (pre/post, Redis + RDS) — required:**
- A read-only `validate_eligibility_view` script/report capturing, **before** and
  **after** the cutover+migration:
  - RDS: counts of `active` vs `deprecated`; `deprecated_source` breakdown
    (`eligibility_filter` count must go to 0 post-migration); active-venue count
    per `google_primary_type`.
  - Redis: `ZCARD venues_geo_v1`; the served `venue_id` set; served count per
    `google_primary_type` (blocked types must be 0).
  - The view: size, and per-`google_primary_type` counts.
- Assertions:
  1. **Pre-cutover parity:** when the projector first reads the view (before the
     reactivation migration), the served set equals the pre-change Redis snapshot
     within an explained delta (the view replicates current serving for active
     venues).
  2. **Post-migration delta is explainable:** the served set grows by exactly the
     reactivated venues that the view now deems eligible (corrected false
     positives, e.g. `book_store` once unblocked) and by nothing else; venues of
     still-blocked types remain absent.
  3. **Redis↔RDS reconciliation:** every Redis served member is active AND
     view-eligible in RDS, and every view-eligible venue is in Redis (no orphans,
     no leaks).
- Run the snapshot→migrate→snapshot→compare loop against a non-prod copy first;
  on prod, snapshot immediately before and validate immediately after. Never run
  destructive steps without the before-snapshot.

## Acceptance Criteria
- Redis serves exactly the view's venues; blocking/unblocking a type changes
  serving on the next projection in both directions, with no `lifecycle_status`
  change and no soft-delete.
- The parity test passes (SQL view == `evaluate()` across the documented cases);
  the good-type table matches `resolve_category`.
- Post-migration: `eligibility_filter`-deprecated count is 0; the served-set
  pre/post delta matches the validation's expected (corrected-false-positive) set
  exactly; Redis↔RDS reconciliation is clean.
- Enrichment no longer processes view-excluded venues (budget preserved) but still
  enriches unlabeled ones.
- `make test-unit` and `make test-bdd` pass incl. the new feature + parity tests.

## Open Questions
- **Good-category representation:** seeded `admin.category_good_type` table
  materialized from `resolve_category` (chosen, lowest SQL complexity, parity-guarded)
  vs encoding the mapping logic directly in the view. Confirm at execution.
- **Near-real-time:** trigger a projection on eligibility save (seconds) vs rely
  on the 2-min cadence (default). Decide at execution.
- **Migration timing:** reactivation of the ~159 venues is a prod data change; run
  it in the same window as the cutover, gated by the before-snapshot. Confirm the
  maintenance window.
