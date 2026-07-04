# PARK category: admit praças/urban parks into resolution + eligibility

## Branch
feature/park-category-eligibility

## Goal
Venues whose Google type is `plaza`, `city_park`, `park`, or
`historical_landmark` (or BestTime type `PLAZA`/`CITY_PARK`/`PARK`) must
resolve to a new first-class `PARK` display category ("Ao Ar Livre" 🌳
`#16A34A`) and must be eligible for serving, so vibes_bot's vibe modes can
target them. Today they resolve to `OTHER` and (except for a handful of
legacy rows) are blocked at write-time/sweep.

## Non-goals
- No vibe-mode changes (vibes_bot owns those — companion plan
  `vibes_bot/plans/260704_vibe-modes-park-familia.md`).
- No unblocking of `garden`/`national_park` Google types or
  `HISTORICAL`/`TOURIST_DESTINATION` BestTime types — they stay blocked.
- No changes to the ambiguous name-keyword lists (`praça`, `parque`, `plaza`,
  `park` stay listed; positive category resolution already suppresses their
  false positives via the `good_category` check).
- No new venue ingestion (Marco Zero / Parque da Jaqueira are a separate
  batch-add effort).

## Evidence
- `app/models/venue_category.py` — `_GOOGLE_TO_CATEGORY` /
  `_BESTTIME_TO_CATEGORY` have no park/plaza/landmark mappings; unmapped
  types fall to `OTHER` in `resolve_category()`.
- `app/services/venue_eligibility.py` — `DEFAULT_BLOCKED_GOOGLE_TYPES`
  contains `park`, `city_park`, `plaza` (and `garden`, `national_park`);
  `DEFAULT_BLOCKED_VENUE_TYPES` contains `PARK`, `CITY_PARK`, `PLAZA`.
  `evaluate()` order: blocked google type → blocked besttime type →
  `good_category` suppression of name keywords → keyword rejection.
- Live production check (2026-07-04, read-only `POST /venues`): Jardim do
  Baobá, Pier dos Ciclistas, The square Barriguda (all `google_places_type:
  "park"`) and Pátio de São Pedro (`historical_landmark`) are already served
  with `category: "OTHER"` and `mode_eligibility: ["explorar"]` only —
  proving the mapping gap is what hides them from vibe modes, and that
  category resolution happens at serve time (fix takes effect on next serve,
  no backfill).
- `resolve_venue_display` is called at serve time from
  `app/handlers/venue_handler.py` — category is computed live, not persisted.

## Current Behavior
- `resolve_category(google_type="park"|"plaza"|"city_park"|"historical_landmark")`
  → `"OTHER"`.
- `evaluate()` rejects new `park`/`plaza`/`city_park`-typed venues with
  `ineligible_google_type` (high confidence, soft-deletable) at write-time
  and during the sweep; equivalent BestTime types are likewise blocked.
- A praça-named venue without a mapped category also trips the ambiguous
  name-keyword rejection because `good_category` is false.

## Desired Behavior
- The four Google types and three BestTime types above must resolve to
  `PARK` with display `{"label": "Ao Ar Livre", "emoji": "🌳", "color": "#16A34A"}`.
- Granular labels: `plaza` → "Praça", `city_park` → "Parque Urbano",
  `park` → "Parque", `historical_landmark` → "Marco Histórico".
- `evaluate()` must return eligible for venues typed `plaza`/`city_park`/
  `park` (Google) or `PLAZA`/`CITY_PARK`/`PARK` (BestTime).
- A PARK-resolving venue with `praça`/`parque` in its name must NOT be
  rejected by the ambiguous-keyword rule (good-category suppression).
- `garden`/`national_park` must still resolve to `OTHER` and stay blocked.

## Implementation Approach
- `app/models/venue_category.py`: add `PARK` to the category dict; add the
  four Google-type and three BestTime-type mappings; add the four granular
  labels.
- `app/services/venue_eligibility.py`: remove `"plaza"`, `"city_park"`,
  `"park"` from `DEFAULT_BLOCKED_GOOGLE_TYPES`; remove `"PLAZA"`,
  `"CITY_PARK"`, `"PARK"` from `DEFAULT_BLOCKED_VENUE_TYPES`. Leave keyword
  lists and all other blocked types untouched.
- Operational (deploy-time, not code): check `admin.eligibility_rule` for
  override rows for these values (`SELECT * FROM admin.eligibility_rule
  WHERE rule_type IN ('blocked_google_type','blocked_venue_type') AND value
  IN ('plaza','city_park','park','PLAZA','CITY_PARK','PARK')`). If rows
  exist they shadow the code defaults and must be removed via
  `EligibilityRuleService.remove_rule(...)`; if the table has no rows for
  these values, the code-default edit is the operative lever. Doing both is
  safe (removing absent rows is a no-op).

## Data, Config, And API Impact
- No RDS schema change, no migration, no Redis key change.
- Served venue payloads gain `category: "PARK"` + new label/emoji/color for
  affected venues at next serve (computed live; no backfill).
- Cross-repo contract: vibes_bot mode configs will reference `"PARK"` in
  `allowed_types`/`always_pass_types` — this plan must deploy first.
- Possible `admin.eligibility_rule` row removals (see above) — data-only,
  reversible by re-adding rows.

## Error Handling And Observability
No new runtime path. Existing eligibility rejection metrics
(`ineligible_google_type` etc.) simply stop counting these types; the
existing Grafana rejection-reason panel makes the drop observable.

## Test Plan
Feature file: `tests/bdd/refresh/park-category-eligibility.feature`

Scenarios:
- Plaza-typed venue resolves to PARK and is eligible for serving.
- Park-typed venue with an ambiguous name keyword ("praça"/"parque") is not
  rejected (good-category suppression).
- Garden/national_park-typed venue still resolves to OTHER and remains
  blocked.
- CITY_PARK BestTime-typed venue with no Google type resolves to PARK and is
  eligible.
- historical_landmark-typed venue resolves to PARK (it already passed
  eligibility; resolution is the change).

Pytest unit tests:
- `resolve_category` for: `park`, `plaza`, `city_park`,
  `historical_landmark` → `PARK`; `garden`, `national_park` → `OTHER`;
  BestTime `PLAZA`/`CITY_PARK`/`PARK` → `PARK`.
- Display tokens for `PARK` (label/emoji/color) and the four granular labels.

Manual or integration checks:
- Post-deploy: read-only `POST /venues` re-query of "Jardim do Baobá" shows
  `category: "PARK"`, `venue_type_label: "Ao Ar Livre"`.
- Post-deploy: `admin.eligibility_rule` override-row check performed and its
  outcome recorded in the PR description.

## Acceptance Criteria
- The five BDD scenarios pass; the full offline suite stays green.
- The four already-served Recife venues (Jardim do Baobá, Pier dos
  Ciclistas, The square Barriguda, Pátio de São Pedro) serve with
  `category: "PARK"` after deploy, verified by the manual check above.
- `garden`/`national_park` venues remain blocked and `OTHER`.

## Open Questions
- None.
