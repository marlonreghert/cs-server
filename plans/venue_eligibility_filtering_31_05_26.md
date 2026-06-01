# Venue Eligibility Filtering

## Branch
feature/venue-eligibility-filtering

## Goal
Stop ineligible venues (drugstores, markets, churches, empty-named places, and
other non-nightlife/non-food places) from entering the active inventory,
reaching users, or consuming crawl credits.

Specifically:
1. Make every venue that enters Redis pass a single, centralized eligibility
   evaluation — including venues pulled by `inventory_sync`, which today bypass
   all type/name filtering.
2. Soft-delete the clearly-ineligible venues with a rejection reason so the
   expensive enrichment jobs (photos, live busyness, Instagram, menu) never run
   on them, and so operators can inspect *why* each was vetoed.
3. Apply the same eligibility rules at serving time, and additionally exclude
   empty-named venues, so churches and blank venues are never returned to users.
4. Make the eligibility lists tunable from the vibes_bot admin panel via
   `admin_config`, since they are currently hardcoded despite a stale comment
   that claims otherwise.

## Non-goals
- Do not hard-delete any venue. All vetoing uses the existing soft-delete
  (`lifecycle_status="deprecated"`) lifecycle. See
  `plans/venue_soft_deletion_27_05_26.md`.
- Do not change the BestTime day-index or Recife timezone behavior.
- Do not migrate or rename Redis keys, and do not require a flush, reset, or
  reprocess of production Redis.
- Do not adopt a positive allow-list that auto-soft-deletes unclassified
  venues. The chosen policy is **block-list only**: serve everything except
  known-bad, and only auto-soft-delete *high-confidence* junk. (BestTime types
  ~60% of venues as `OTHER`, many of which are real bars; auto-deleting
  unknowns would permanently hide real venues because soft-delete is one-way.)
- Do not build a restore/reactivation workflow. Because the block-list policy
  soft-deletes only high-confidence junk, V1 ships without restore; a later
  plan may add it.
- Do not add photo-based eligibility gating. (See "Photos" under Evidence — the
  vibe classifier already analyzes photos and saves tags; gating on them would
  require spending photo credits on junk, which contradicts the credit-saving
  goal.)
- Do not move user-facing exclusion to vibes_bot. cs-server is the source of
  truth and already filters at serve time; that stays in cs-server.
- Do not build the vibes_bot admin UI here. This plan only provides the
  cs-server config contract vibes_bot consumes.

## Evidence

### Root cause: inventory-synced venues are type-blind and bypass filters
- `app/models/new_venue.py:51` — `AccountInventoryVenue` (the `/api/v1/venues`
  row) has **no `venue_type` field**. The endpoint cannot supply a type.
- `app/services/venues_refresher_service.py:853` — `sync_account_inventory_to_redis`
  constructs `Venue(...)` with **no `venue_type`** and **no eligibility check**;
  every inventory row is upserted as active.
- `app/handlers/venue_handler.py:137` — `_is_blocked` check #2 only fires when
  `v.venue_type` is set, and check #3 only fires when `v.venue_type == "OTHER"`.
  With `venue_type=None`, inventory-synced venues skip both checks; only the
  Google-type check (`:140`) can catch them, and only after Google enrichment
  has already spent a lookup. `_is_blocked` also **never rejects empty names**.
- Net effect: the ~1300 inventory-synced venues (drugstores, markets, churches,
  blank names) reach users unless/until Google enrichment happens to tag them.

### The filters exist but only at serve time, split across files
- `app/services/venues_refresher_service.py:83` `DEFAULT_BLOCKED_VENUE_TYPES`,
  `:125` `BLOCKED_GOOGLE_TYPES`, `:156` `BLOCKED_NAME_KEYWORDS` are hardcoded
  constants consumed by `venue_handler._is_blocked`. There is no write-time
  enforcement and no shared module.

### Soft-delete + observability already exist (reuse, do not rebuild)
- `app/dao/redis_venue_dao.py:94` `soft_delete_venue(venue_id, reason, source,
  google_business_status)` sets lifecycle metadata and preserves all keys.
- `app/dao/redis_venue_dao.py:57` `upsert_venue` preserves an existing
  `deprecated` status across re-upserts and does not implicitly reactivate.
- `app/metrics.py:156` `venues_soft_deleted_total{reason,source}` and `:163`
  `venues_deprecated_total` already exist. `:396` `VENUES_BY_TYPE` gauge exists.
- `app/routers/admin_trigger_router.py:316` `GET /admin/venues/inventory`
  already returns `lifecycle_status`, `deprecated_reason`, `deprecated_source`,
  and `deprecated_at` per venue. **This is the "intelligence over vetoed
  venues" surface the user asked about — it exists today.**
- Enrichment/refresh jobs already enumerate `list_active_venue_ids()`
  (`google_places_enrichment_service.py:281`, and per
  `plans/venue_soft_deletion_27_05_26.md` the photo/IG/menu/vibe jobs too), so a
  soft-deleted venue automatically stops consuming crawl credits.

### Admin configurability: claimed but not wired
- `app/services/venues_refresher_service.py:81` comments that
  `DEFAULT_BLOCKED_VENUE_TYPES` "can be overridden via admin panel
  (admin_config:blocked_venue_types)", but **no code reads that key** (verified
  by repo-wide grep — only the comment references it). The blocked lists are
  effectively hardcoded.
- Genuinely-wired `admin_config:` keys (read live by code) are only:
  `venue_photos_cache_ttl_days` (`redis_venue_dao.py:29`),
  `venue_monthly_budget` (`venue_budget_service.py:27`), and
  `discovery_points` (`venues_refresher_service.py:186`). The budget/TTL pattern
  (read-with-fallback on each call) is the template to follow for a real
  eligibility-config reader.

### Photos (third quest) — answered, no action required
- `app/services/photo_enrichment_service.py` fetches Google Places photos and
  caches URL + author only; it does **not** analyze them at fetch time.
- `app/services/vibe_classifier_service.py` **does** analyze photos (Stage A/B
  GPT) and persists per-photo tags `photo_type`
  (interior/exterior/crowd/food/drink/event/menu/selfie/other) and `vibe_appeal`
  inside the vibe profile's `evidence_photos`.
- Those tags are consumed for **display sorting** in
  `app/handlers/venue_handler.py:380`, **not** for eligibility.
- Conclusion: photo tags are already saved. Per the user's own conditional, and
  because gating on them means spending photo credits on junk, this plan adds no
  photo-based eligibility. No code change for the third quest.

## Current Behavior
- `inventory_sync` upserts every BestTime account-inventory venue as active with
  no type and no filtering.
- Discovery (`/venues/filter`) upserts every returned venue as active with no
  write-time eligibility filtering (it does carry a `venue_type`).
- Only serve time filters venues, via `venue_handler._is_blocked`, which misses
  empty names and misses type/name rules for type-less inventory venues.
- The blocked lists are hardcoded; the admin-config override is documented but
  not implemented.
- Crawl-credit jobs run on all active venues, including ineligible inventory
  junk that has not yet been Google-tagged.

## Desired Behavior
- A single eligibility module evaluates a venue from its name, BestTime type,
  and (optional) Google type, returning eligible/ineligible plus a stable reason
  and a confidence level.
- Inventory sync and discovery upserts apply the **cheap, free-signal** part of
  the evaluation at write time. A venue that fails a high-confidence free signal
  (empty name; hard blocked name keyword; blocked BestTime type when a type is
  present) is **persisted as deprecated** with its reason and
  `source="eligibility_filter"`, never as active.
- An eligibility sweep job evaluates active venues in cost order:
  1. Free signals first (empty name, name keyword, blocked BestTime type) →
     soft-delete high-confidence junk **without** a Google lookup.
  2. Google-label only the survivors (reuse a cached `google_primary_type` when
     present; fetch one lookup otherwise).
  3. Soft-delete venues whose Google type is in the blocked Google set.
- **Block-list policy:** a venue that is neither empty-named, nor keyword/type
  blocked, nor Google-blocked stays active — including BestTime `OTHER` with no
  Google type and unknown Google types. A name-keyword match on a venue that
  Google positively classifies as nightlife/food is **not** soft-deleted (avoid
  false positives like "Parque Bar").
- Public serving uses the same eligibility module and additionally excludes
  empty-named venues, so churches and blank venues never reach users.
- The blocked lists are read live from `admin_config:venue_eligibility` (with
  the current hardcoded constants as defaults), and GET/POST admin endpoints let
  vibes_bot read and update them without a redeploy. A config change affects
  serve-time filtering and the next sweep immediately.
- Soft-delete reasons are reason-labelled in the existing metrics, and the
  existing `GET /admin/venues/inventory` exposes per-venue reasons.
- Existing deprecated venues are never reactivated or re-deprecated by the
  sweep; legacy records without lifecycle metadata remain active.

## Implementation Approach

### 1. Centralize eligibility into one module
Create `app/services/venue_eligibility.py` owning the moved constants
(`BLOCKED_VENUE_TYPES`, `BLOCKED_GOOGLE_TYPES`) and a **split** of today's
`BLOCKED_NAME_KEYWORDS` into two lists:

- `HARD_BLOCKED_NAME_KEYWORDS` — unambiguous non-nightlife tokens that never
  appear in a legitimate bar/restaurant name (e.g. `drogaria`, `farmácia`,
  `igreja`, `catedral`, `hospital`, `clínica`, `escola`, `colégio`,
  `universidade`, `banco`, `correios`, `cartório`, `delegacia`, `posto`,
  `farmacia`). These are high-confidence and may be soft-deleted **pre-label**.
- `AMBIGUOUS_NAME_KEYWORDS` — tokens that often appear in real bar names
  (e.g. `mercado`, `parque`, `park`, `praça`, `plaza`, `shopping`, `mall`).
  These are **never** soft-deleted before labeling. They only cause exclusion at
  serve time (reversible), or a soft-delete **after** Google labeling confirms a
  non-good category. An ambiguous-only match on an unlabeled venue keeps it
  active.

The split is the core protection against irreversibly nuking "Bar do Mercado"
at inventory-sync time, where a Google type is never available. Every existing
entry in today's `BLOCKED_NAME_KEYWORDS` must be sorted into exactly one of the
two lists; when in doubt a token goes to `AMBIGUOUS_NAME_KEYWORDS`.

Then add:

- `EligibilityConfig` — the lists (`blocked_venue_types`, `blocked_google_types`,
  `hard_blocked_name_keywords`, `ambiguous_name_keywords`), loadable from
  defaults or a dict.
- An evaluation function returning `eligible: bool`, `reason: Optional[str]`, and
  `confidence` (high vs low), given `venue_name`, `besttime_type`, `google_type`.
  Reasons: `ineligible_empty_name`, `ineligible_name_keyword`,
  `ineligible_besttime_type`, `ineligible_google_type`.
  - High-confidence (sweep- and write-time-soft-deletable, no lookup needed):
    empty name; blocked Google type; blocked BestTime type; **hard** name
    keyword.
  - Low-confidence (serve-time exclusion only, or soft-delete **only after**
    Google labeling confirms a non-good category): **ambiguous** name keyword.
  - Everything else (unknown/unlabeled, including BestTime `OTHER`) → eligible
    under the block-list policy.
- Keep the positive category map in `app/models/venue_category.py` as the source
  of "is this a good category" used to suppress ambiguous-keyword false positives
  after labeling.

Re-export the moved constants from `venues_refresher_service` (or update its
imports) so existing imports in `venue_handler` keep working. Update
`venue_handler._is_blocked` to delegate to the module and to reject empty names.
Delete the stale admin-config comment.

### 2. Admin-tunable config (`admin_config:venue_eligibility`)
- Add a live reader (budget/TTL pattern: read Redis on each pass, fall back to
  hardcoded defaults, tolerate malformed JSON by logging and using defaults).
- Add `GET /admin/venues/eligibility-config` returning the active lists and
  whether they come from Redis or defaults.
- Add `POST /admin/venues/eligibility-config` validating that each field is a
  list of strings, persisting to `admin_config:venue_eligibility`, and returning
  the stored config. Reject malformed bodies with HTTP 4xx and leave the active
  config unchanged.
- Wire the reader through the container so the handler, refresher, and sweep all
  read the same effective config.

### 3. Write-time pre-filter in sync and discovery
- In `sync_account_inventory_to_redis`, evaluate each row's **high-confidence
  free signals only** before upsert (empty name; hard name keyword; blocked
  BestTime type — though inventory rows carry no type, so in practice empty name
  + hard keyword). Ambiguous keywords do **not** soft-delete here. If
  high-confidence ineligible, upsert the `Venue` with
  `lifecycle_status="deprecated"`, `deprecated_reason=<reason>`,
  `deprecated_source="eligibility_filter"`, `deprecated_at=now`, and increment
  the soft-deleted metric; otherwise upsert active as today. Preserve existing
  deprecated metadata for venue IDs already deprecated (do not reactivate).
- In `refresh_venues_data_by_venues_filter`, apply the same free-signal
  pre-filter (discovery rows carry `venue_type`, so the BestTime-type rule is
  effective here). Keep the monthly-budget accounting unchanged for venues that
  are actually persisted active.

### 4. Eligibility sweep job
- Add `VenueEligibilityService` (or a method on the refresher) implementing the
  cost-ordered sweep over `list_active_venue_ids()`:
  1. High-confidence free signals (empty name, **hard** keyword, blocked
     BestTime type) → soft-delete, no Google lookup.
  2. Survivors (including ambiguous-keyword matches) → **cache-first labeling**:
     read the already-cached `google_primary_type` from vibe attributes (the
     nightly `google_places` enrichment populates this for every active venue,
     so in steady state this step makes **zero** Google calls). Only when a
     survivor has no cached type at all, optionally fetch a **minimal**
     `id,primaryType,businessStatus` Place Details lookup (Place Details Pro
     SKU — far cheaper than the full `VIBE_FIELDS_MASK` Enterprise+Atmosphere
     call), gated by a config flag; otherwise defer that venue to the next
     sweep after nightly enrichment has labeled it.
  3. After labeling: soft-delete blocked Google types; soft-delete an
     ambiguous-keyword match **only if** the confirmed Google category is not a
     good category; keep everything else active.
- Reuse `venue_dao.soft_delete_venue(reason=<reason>, source="eligibility_filter")`.
- Register a `venue_eligibility` admin job in
  `admin_trigger_router.JOB_REGISTRY` and `_run_job`, available only when the
  Google Places client is configured (the labeling step needs it; the cheap
  pass can run without it and should still soft-delete free-signal junk).
- Update `update_data_quality_metrics` so active/deprecated gauges and the
  by-reason breakdown stay current after a sweep.

### 5. Serving
- `venue_handler.get_venues_nearby` filters via the centralized module
  (blocked types/keywords/google-type) and excludes empty names, before merge
  and transform. Behavior for eligible venues is unchanged.
- **Serve-time hides only HIGH-confidence ineligibility** (`result.soft_deletable`):
  empty name, blocked BestTime/Google type, hard keyword. Ambiguous-keyword
  names with no label yet ("Bar da Praça", "Boteco do Mercado") stay **visible**
  until Google labeling resolves them — honoring the block-list "unknowns reach
  users" decision. Once labeled, a confirmed non-good category flips them to
  high-confidence → hidden and soft-deleted by the sweep (self-converging within
  ~a day via nightly enrichment). Trade-off: an unlabeled actual "Mercado
  Central" can show briefly during that window (accepted "lower exposed quality").

## Data, Config, And API Impact
- **Redis keys:** unchanged. Venue JSON gains lifecycle fields only when a venue
  is born-deprecated, soft-deleted, or re-upserted — all already-optional fields
  from the soft-deletion plan. No migration, flush, rename, or reprocess.
- **New admin_config key:** `admin_config:venue_eligibility` (JSON object with
  `blocked_venue_types`, `blocked_google_types`, `blocked_name_keywords`). Absent
  key ⇒ hardcoded defaults (matches current behavior).
- **New admin API:** `GET` and `POST /admin/venues/eligibility-config`.
- **New admin job:** `venue_eligibility` in the trigger registry.
- **Serving response shape:** unchanged for eligible venues; empty-named and
  ineligible venues are now reliably omitted.
- **Soft-delete reasons (new values):** `ineligible_empty_name`,
  `ineligible_name_keyword`, `ineligible_besttime_type`,
  `ineligible_google_type`, all with `source="eligibility_filter"`.

## Cost (Google labeling)
- Google "evaluation" is the existing `google_places` enrichment job (cron
  default `0 3 * * *` plus optional on-startup). It makes 2 Google calls per
  venue — Text Search (Pro SKU) + Place Details (full `VIBE_FIELDS_MASK`,
  Enterprise+Atmosphere SKU) — caches the result permanently, caches empty
  attributes for not-found venues, and skips already-enriched venues on re-runs.
- The eligibility sweep is **cache-first**: it reads the cached
  `google_primary_type`, so in steady state it makes **no** new Google calls.
  Net Google cost of this feature ≈ 0 for already-enriched venues; only
  never-enriched survivors of the cheap filters can cost a lookup, and only the
  minimal Pro-SKU `primaryType` call when the config flag enables it.
- Worst case (cold cache, full ~1300 inventory, flag on): ~1300 minimal
  Place Details Pro lookups, a one-time order of a few cents per venue. Confirm
  exact figures against the current Google rate card and per-SKU free monthly
  tier before enabling the fallback.
- The real saving is downstream: soft-deleting ineligible venues stops the
  expensive photos, live-busyness, Instagram, and menu crawls from ever running
  on them. That is where the credits go, not the one-time type lookup.

## Error Handling And Observability
- Malformed `admin_config:venue_eligibility` JSON: log once and fall back to
  defaults; never crash a sweep or a request.
- A Google Places lookup failure during the sweep leaves the venue **active**
  (do not soft-delete on uncertainty) and is logged; it is retried next sweep.
- `soft_delete_venue` returning false (venue vanished) is logged with venue ID,
  reason, and source; the sweep continues.
- Admin config endpoints return sanitized validation errors and never echo raw
  Redis payloads.
- Reuse `venues_soft_deleted_total{reason,source}` and `venues_deprecated_total`.
  Add one gauge `venues_deprecated_by_reason{reason}` (snapshot) so Grafana can
  break down *why* venues were vetoed — this answers the "expose intelligence /
  I didn't see it in Grafana" ask. Document the panel addition; the per-venue
  reason is already in `GET /admin/venues/inventory`.
- The sweep logs a summary: seen, free-signal soft-deletes, labeled, Google-type
  soft-deletes, kept-active, errors.
- **Operational caveat (conscious V1 trade-off):** because the sweep soft-deletes
  on the admin-tunable lists and there is no restore in V1, an operator who
  *tightens* `blocked_venue_types`/`blocked_google_types`/`hard_blocked_name_keywords`
  causes irreversible soft-deletes on the next sweep. Mitigations: ambiguous
  keywords never auto-delete pre-label; the POST endpoint should surface this in
  its response/docs; document that broadening the hard lists is a one-way action
  in V1. A restore workflow is deferred to a later plan.

## Test Plan
Feature file: `tests/bdd/refresh/venue_eligibility_filtering.feature`

Scenarios (see feature file):
- Inventory sync births an empty-named venue as deprecated with the right reason
  and excludes it from serving.
- Inventory sync keeps an unclassified named venue active (block-list policy).
- Sweep soft-deletes a hard-keyword venue ("Drogaria") with no Google lookup.
- Sweep Google-labels only survivors of the cheap filters.
- Sweep soft-deletes a Google-confirmed pharmacy/supermarket; keeps a
  Google-confirmed bar.
- Ambiguous keyword ("Bar do Mercado") stays active pre-label and is not deleted
  without a lookup; "Parque Bar" labeled as bar stays active; "Mercado Central"
  labeled as supermarket is soft-deleted as `ineligible_google_type`.
- Inventory sync keeps an ambiguous-keyword venue active (no type available).
- Unknown/unlabeled venues stay active.
- Soft-deleted ineligible venues are skipped by enrichment jobs (no crawl work).
- Serving excludes empty-named and CHURCH-typed venues, includes the real bar.
- Operators read and update eligibility config; an added keyword takes effect
  without redeploy; invalid config is rejected and leaves the active config
  unchanged.
- Soft-delete emits the reason/source-labelled metric and updates the gauge.
- Re-running the sweep does not reactivate or re-deprecate venues.

Pytest unit tests:
- `venue_eligibility` module: each reason fires for its input; hard keyword is
  high-confidence; ambiguous keyword is never high-confidence and is suppressed
  when Google positively classifies a good category; empty/whitespace names
  rejected; unknown/unlabeled types eligible; every legacy
  `BLOCKED_NAME_KEYWORDS` token lands in exactly one of the two new lists.
- Refresher: `sync_account_inventory_to_redis` and
  `refresh_venues_data_by_venues_filter` born-deprecate free-signal junk,
  preserve existing deprecated metadata, and keep active venues active.
- Sweep service: cost ordering (no Google lookup for cheap-rejected venues),
  Google-type soft-delete, lookup-failure leaves venue active, idempotency.
- Handler: empty-named and ineligible venues omitted; eligible venues unchanged.
- Admin router: GET returns active lists; POST validates and persists; malformed
  body rejected with the active config unchanged.
- Config reader: defaults when key absent; live override; malformed JSON falls
  back to defaults.
- Metrics: soft-delete increments the reason/source counter; by-reason gauge set.

Manual or integration checks:
- `make test-feature FEATURE=tests/bdd/refresh/venue_eligibility_filtering.feature`.
- Targeted unit tests for the module, refresher, sweep, handler, admin router.
- Redis integration only against the documented test database 15. Do not run any
  command against production Redis.

## Acceptance Criteria
- Inventory-synced and discovered venues that are high-confidence ineligible are
  persisted as deprecated with a reason and `source="eligibility_filter"`, never
  active.
- The eligibility sweep performs no Google lookup for venues rejected by cheap
  signals, soft-deletes Google-confirmed ineligible venues, and leaves unknown
  and positively-classified venues active.
- Soft-deleted ineligible venues are excluded from all enrichment/refresh jobs
  (no crawl credits) and from public nearby responses.
- Public nearby responses never include empty-named or blocked-type venues.
- `GET`/`POST /admin/venues/eligibility-config` read and update the blocked
  lists, validation rejects malformed input, and changes take effect without a
  redeploy.
- Per-venue veto reasons are visible via `GET /admin/venues/inventory`, and a
  by-reason Prometheus gauge plus the reason-labelled soft-delete counter expose
  the breakdown.
- No Redis flush, reset, rename, migration, or reprocess; legacy records without
  lifecycle metadata remain active.
- BDD and targeted pytest coverage pass.

## Open Questions
None.

## vibes_bot Integration Prompt
Use after the cs-server change is merged and deployed:

Add an admin-only "Venue eligibility" config panel to vibes_bot that reads
cs-server `GET /admin/venues/eligibility-config` and writes
`POST /admin/venues/eligibility-config` through the existing cs-server admin
proxy/auth utilities (do not call cs-server from browser code). Let operators
view and edit three string lists — blocked BestTime types, blocked Google
types, and blocked name keywords — and show whether the active config is the
Redis override or the built-in defaults. Surface the existing
`GET /admin/venues/inventory` `deprecated_reason`/`deprecated_source` so an
operator can see which venues the eligibility filter vetoed and why. Treat this
as a tuning + inspection surface only: do not add any UI action that
hard-deletes, restores, or reprocesses venues in V1. Keep user-facing exclusion
on the cs-server side; vibes_bot must not re-implement the filter.

## Implementation Notes (built)
Delivered on branch `feature/venue-eligibility-filtering`:
- `app/services/venue_eligibility.py` — owns the block-lists (hard/ambiguous
  keyword split), `evaluate()`, `EligibilityConfig`, and the live
  `load_eligibility_config()` reader.
- `venues_refresher_service.py` — re-exports the constants, born-deprecates
  high-confidence junk in `sync_account_inventory_to_redis` and
  `refresh_venues_data_by_venues_filter`, adds `run_eligibility_sweep()`
  (cache-first; no live Google calls in V1), and fills `venues_deprecated_by_reason`.
- `venue_handler.py` — serve-time filtering now delegates to `evaluate()` and
  excludes empty-named venues.
- `admin_trigger_router.py` — new `venue_eligibility` job and
  `GET`/`POST /admin/venues/eligibility-config`.
- `app/metrics.py` — `venues_deprecated_by_reason{reason}` gauge.
- Tests: `tests/bdd/refresh/venue_eligibility_filtering.feature` (+ steps) and
  `tests/test_venue_eligibility.py`. Full suite green (200 unit, 46 BDD).

### Rejected-venue visibility — cs-server surfaces (ready for vibes_bot)
Everything needed to "inspect and validate the rejected ones and their reasons"
is exposed by cs-server now:
1. **Per-venue inspection (HTTP):**
   `GET /admin/venues/inventory?status=deprecated` returns each vetoed venue with
   `venue_id`, `venue_name`, `venue_address`, `venue_lat/lng`, `lifecycle_status`,
   `deprecated_reason` (e.g. `ineligible_name_keyword`, `ineligible_google_type`,
   `ineligible_empty_name`, `ineligible_besttime_type`), `deprecated_source`
   (`eligibility_filter`), `deprecated_at`, `google_business_status`, and
   `cache_flags`. Supports `q=` search and `limit`/`cursor` pagination. (Endpoint
   pre-existed from PR #19; eligibility now populates it with new reasons.)
2. **Aggregate counts (Prometheus, `/metrics`):**
   `venues_deprecated_by_reason{reason}` (snapshot per reason),
   `venues_soft_deleted_total{reason,source}` (running counter),
   `venues_deprecated_total`, `venues_active_total`.
3. **Filter tuning (HTTP):** `GET`/`POST /admin/venues/eligibility-config`.

### What vibes_bot must do to get this visibility (if not already)
- **Admin panel:** add a "Rejected venues" view that calls
  `GET /admin/venues/inventory?status=deprecated` through the existing cs-server
  admin proxy/auth (not from browser code), and renders the per-venue metadata +
  `deprecated_reason`/`deprecated_source` above. Reuse the same proxy the
  soft-deletion inventory view already uses — if that view exists, this is a
  filter/columns addition, not a new integration. Default the operator to
  `status=deprecated` when troubleshooting; allow `q` search and pagination.
- **Grafana/Prometheus:** add a panel grouping `venues_deprecated_by_reason` by
  `reason` (bar/stacked) plus a `rate(venues_soft_deleted_total[1h])` panel by
  `reason,source`. cs-server already exports these on `/metrics`; vibes_bot only
  needs the dashboard if the Prometheus scrape of cs-server is already wired.
- **Config editor (optional but recommended):** the eligibility-config panel
  described in the Integration Prompt above, so operators can tighten/loosen
  lists after inspecting the rejected set.
- **Do NOT** re-implement filtering or hide/restore venues in vibes_bot; this is
  a read-only inspection + tuning surface in V1 (soft-delete is one-way).
