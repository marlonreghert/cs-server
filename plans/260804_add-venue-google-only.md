# Add Venue From Google Metadata Only

## Branch
feature/add-venue-google-only

## Goal
Let an operator catalog a venue that Google knows about but BestTime cannot
forecast. Today that add ends in a terminal 502 and nothing is persisted. After
this change the venue is persisted under a **venue id we mint ourselves**,
populated entirely from Google Places metadata, with `forecast=false` and no
BestTime id anywhere. It enters `serving.eligible_venue`, is projected to
`venues_geo_v1` like any other venue, and reaches the app — which already labels
a venue with no busyness data **"Sem Informação"**.

Zero BestTime credit is spent on this path, and such venues are permanently
excluded from BestTime live/weekly refresh selection because there is no
BestTime id to query.

## Non-goals
- **Google Places discovery.** No crawl job, no `places:searchNearby`. The only
  entry point is the operator add (`POST /admin/venues/by-address`, and the batch
  runner that wraps it). A discovery pipeline is a separate plan with its own
  Google cost review.
- **Any vibes_bot or mobile change.** The `unknown` busyness state, the
  `sem informação` label, the ghost chip, the sort rank, and the client-side
  filter gates all shipped on 2026-07-28, and `GET /config` reports both
  `show_venues_without_busyness_enabled` and `show_closed_venues_enabled` as
  `true` in production today. This plan only supplies the venues.
- **Promotion to BestTime.** If such a venue later becomes forecastable, this
  plan does not relink it. The minted id is permanent — 14 tables FK to
  `venues.venue(venue_id)` and the app navigates by it, so rewriting the PK is
  never an option. A future promotion plan would record the BestTime id in a new
  column and leave the PK alone.
- **Any change to the projector, the geo-fence, the closure detector, the
  eligibility view, or `venues_geo_v1`.** A `forecast=false` venue is already a
  supported, projected, served state.
- Resurrecting `feature/add-venue-without-forecast`. That branch is abandoned;
  see Evidence.

## Evidence
- `plans/260728_add-venue-without-forecast.md` — **ABANDONED (2026-07-28)**. A
  prod probe ("Praça do Arsenal") proved BestTime's *"Venue found, but could not
  forecast"* means found in **their** database, not registered in **ours**: the
  venue is absent from a full `GET /venues` inventory listing. Its
  "Replacement direction" section is the design this plan implements.
- `app/models/new_venue.py:22-30` — `NewVenueInfo` already documents that a 4xx
  rejection carries a `venue_info` block **without** a `venue_id`. That block
  carries `venue_name`, `venue_address`, `venue_lat`/`venue_lon`, `rating`,
  `reviews`, `price_level` — everything but an id.
- `app/handlers/add_venue_handler.py:274-298` — the `not _response_ok(response)`
  tail releases the manual slot and falls through to `_geo_fallback`.
- `app/handlers/add_venue_handler.py:608-660` — `_geo_fallback` returns a
  terminal 502 `besttime_rejected_no_geo_match` when no candidate name-matches.
  `/venues/filter` only returns venues BestTime has foot-traffic data for, so an
  unforecastable venue can never match here. **This is the only outcome this plan
  replaces.**
- `app/handlers/add_venue_handler.py:140-180` — `add()` already short-circuits on
  the address-hash cache (step 1) and a 50 m Redis geo lookup (step 2) before any
  create, so duplicate protection for this path already exists upstream.
- `migrations/versions/0001_baseline_schemas.py:26` — `venue_id text PRIMARY KEY`.
  There is **no format constraint** on venue ids anywhere in the codebase; a
  grep for prefix/length assumptions finds only test fixtures. Minting our own id
  needs no PK change.
- `migrations/versions/0001_baseline_schemas.py` + later revisions — 14 tables
  carry `REFERENCES venues.venue(venue_id)`. The PK must never be rewritten.
- `app/dao/rds_venue_store.py:310-326` —
  `list_servable_venue_ids_by_priority` selects from `serving.eligible_venue`
  joined to `venues.venue` ordered by `priority`, and backs the bounded live and
  weekly BestTime refresh (`venues_refresher_service.py:143-157`, `:1043`,
  `:1065`). **Without an exclusion, every refresh cycle would send a synthetic id
  to BestTime.** This is the single largest hazard in the change.
- `app/services/venues_refresher_service.py:875-940` —
  `sync_account_inventory_to_redis` is **additive only**: it upserts inventory
  venues our store lacks and never deprecates a venue missing from BestTime's
  inventory (its `deprecated` counter is written but never incremented). Minted
  venues therefore survive every inventory sync untouched.
- `app/dao/venue_row.py:60-104` — `COLUMN_FIELDS` / `RESIDUAL_FIELDS` split:
  scalars are real columns, everything else round-trips through the `extra`
  jsonb. `geo_linked` / `geo_linked_year_month` (`app/models/venue.py:138-149`)
  are the precedent for a provenance flag.
- `migrations/versions/0009_eligibility_serving_view.py` (`CREATE_VIEW`) — the
  blocked-type predicates are guarded by `g.gtype IS NOT NULL` and
  `g.btype IS NOT NULL`, and `good_category` is an `EXISTS`. A venue with a NULL
  BestTime `venue_type` is therefore **eligible**, not silently excluded. Verified
  by reading the predicate; pinned by a scenario below.
- `app/api/google_places_client.py:149` (`search_place_id`) and `:289`
  (`get_place_details`) — both are already called on the add path by
  `_enrich_from_google` (`add_venue_handler.py:917-958`). No new Google endpoint
  and no new Google SKU is introduced.
- `app/services/admin_config_service.py:55` — `AdminConfigService.get(key)` reads
  RDS truth at request time (Redis is a mirror). This is the kill switch: a flag
  read here is reversible in seconds with no redeploy, unlike a `Settings` class
  attribute, which is evaluated at import time.
- `tests/bdd/api/besttime-rejection-venue-info.feature` — pins today's terminal
  rejection ("A rejection with idless venue info surfaces BestTime's message").
  Its two no-geo-match scenarios **must be updated** to state that they hold while
  the Google-only path is disabled; otherwise they contradict this feature.
- Prod counts (2026-07-28): BestTime account inventory 2,340 venues, 1,848 (79%)
  `venue_forecasted=false`; `serving.eligible_venue` 1,728, of which 202 have
  `forecast=false` and 1,102 (64%) carry Google opening hours.

## Current Behavior
`POST /admin/venues/by-address` reserves a monthly slot, calls BestTime
`POST /forecasts`, and on a non-OK, non-monthly-cap response releases the slot
and runs the `/venues/filter` geo fallback. When the venue exists on Google but
has no BestTime foot traffic, the filter returns no name match and the endpoint
answers `502 besttime_rejected_no_geo_match`. Nothing is persisted and the
operator has no way to catalog the venue.

The serving stack downstream is already ready for such a venue: with no live and
no weekly forecast in Redis, vibes_bot resolves `busyness_state="unknown"` and
labels it `sem informação`, and the app renders the ghost chip.

## Desired Behavior
When the geo fallback finds no match — and **only** then — cs-server must catalog
the venue from Google metadata instead of returning 502:

- The venue must be persisted under a **minted** venue id in a namespace that can
  never collide with BestTime's (`vsg_` + the folded name+address hash), with
  `forecast=false`, `processed=true`, and `venue_source='google_only'`.
- Google enrichment must **succeed** for the venue to be created. A Google-only
  venue whose place cannot be resolved carries almost no usable metadata (no
  type, no hours, no photos), so a failed resolution must fall back to today's
  502 and persist nothing. This is the quality gate for the path.
- Scalars must be taken from Google Places details, with BestTime's rejection
  `venue_info` block used only where Google is silent (`rating`, `reviews`,
  `price_level` — it carries these even on a rejection).
- The response must be `201` with `status="created_google_only"` and
  `source="google_places"`.
- The monthly BestTime new-venue ledger must **not** be incremented and the venue
  must **not** be marked touched — no BestTime venue was created and no credit was
  drawn. The manual slot release stays exactly as today.
- The venue must never be selected for BestTime live or weekly refresh.
- The whole path must be gated by a request-time admin-config flag, default
  **off**, so enabling and rolling back are both flag flips.
- Every other outcome of the endpoint — the `already_exists` short circuits, the
  successful create, the timeout recovery, the monthly-cap 429, the geo-fallback
  match, the transport-error 502s — must be byte-for-byte unchanged.

## Implementation Approach

**1. The discriminator column.** Add `venue_source` to `venues.venue`
(migration `0021_venue_source`, `down_revision = "0020_instagram_handle_source"`).
An `ADD COLUMN` with a constant default is a catalog-only operation on
PostgreSQL 11+ — no table rewrite, no lock beyond a brief `ACCESS EXCLUSIVE`, and
no risk to the 2.3k existing rows.

```sql
ALTER TABLE venues.venue
  ADD COLUMN IF NOT EXISTS venue_source text NOT NULL DEFAULT 'besttime';
CREATE INDEX IF NOT EXISTS ix_venue_source_non_besttime
  ON venues.venue (venue_source) WHERE venue_source <> 'besttime';
```

The column is chosen over a key in the `extra` jsonb deliberately: the
refresh-selection exclusion below is a SQL predicate on a join that runs every
refresh cycle, and operators need to be able to count, audit, and bulk-soft-delete
these venues from plain SQL. The partial index keeps the cost proportional to the
minority class.

`venue_source` must be added to `COLUMN_FIELDS` in `app/dao/venue_row.py`, to the
`Venue` model (`venue_source: str = "besttime"`), and to both the column list and
the `ON CONFLICT DO UPDATE SET` clause of `RdsVenueStore.upsert_venue`. Missing
any of the three silently routes the field into `extra` and the SQL exclusion
never sees it — this is the failure mode to test for first.

**2. Refresh exclusion — the cost guarantee.** Add
`AND v.venue_source <> 'google_only'` to `list_servable_venue_ids_by_priority`
and to its sibling `list_active_venue_ids_by_priority` in
`app/dao/rds_venue_store.py`. These two queries are the sole selection source for
bounded live and weekly BestTime refresh. Nothing else in the codebase feeds a
venue id to `besttime_client`: discovery goes through `/venues/filter` (which
returns BestTime's own ids), and inventory sync iterates BestTime's inventory.

**3. The minted id.** Reuse the existing `_address_hash(venue_name, venue_address)`
helper — the same SHA-1 of the folded `name|address` that already keys the address
cache and the single-flight lock — and prefix it: `vsg_<hash[:24]>`. Deterministic
minting means a re-add of the same submission is an idempotent upsert onto the same
row rather than a duplicate venue. The `vsg_` prefix cannot collide with BestTime's
`ven_` namespace.

**4. Where the branch goes.** In `_geo_fallback`, only the `match is None` branch
routes onward to a new `_create_from_google_metadata`. Every other exit is
untouched — in particular the `/venues/filter` transport-failure 502 stays
terminal, because a failed filter call is not evidence that BestTime lacks the
venue. Preferring the geo fallback first is deliberate: linking to a real BestTime
venue is strictly better than minting our own id, so the new path is the last
resort, never a shortcut.

**5. `_create_from_google_metadata`.** Reads the admin flag; when disabled,
returns today's 502 unchanged. When enabled: resolve the place id (from
`request.place_id` when supplied, else `search_place_id`), fetch details, build
the `Venue` from Google's fields with BestTime's rejection `venue_info` filling
`rating`/`reviews`/`price_level`, derive the price tier through the existing
`_derive_and_set_price`, upsert, then reuse `_finalize_created_venue`'s tail for
the inline Google enrichment (vibe attributes, opening hours), the address cache
write, and the metric — **minus** `budget.mark_touched` and the discovery counter,
neither of which applies when no BestTime venue exists. A failed place resolution
or details fetch logs a warning and returns today's 502.

**6. The batch runner.** `app/services/batch_add_service.py:53-95` `_classify`
must map `created_google_only` to a 201 success alongside `created` and
`created_recovered_timeout`, and it must **not** join `_STOP_OUTCOMES` — it is not
a spend-stopping state.

## Data, Config, And API Impact

**Migration:** `0021_venue_source` — one additive column plus a partial index, as
above. Expand-only. The downgrade drops both, and **must never be run once
`google_only` rows exist**: without the column those venues become
indistinguishable from BestTime venues and would be fed to BestTime refresh. See
Rollback.

**Persistence:** new rows may carry `venue_source='google_only'`, `forecast=false`,
`venue_type=NULL`, and no row in `besttime.weekly_forecast` or
`besttime.live_forecast`. All are already-supported states — 202 served venues
have `forecast=false` today.

**API (additive):** `POST /admin/venues/by-address` gains one 201 body shape,
`status="created_google_only"`, `source="google_places"`, plus the existing venue
fields. No existing field changes meaning or type. The only in-repo caller that
branches on `status` is `batch_add_service._classify`.

**Config:** new admin-config key `add_venue_google_only`, shape
`{"enabled": <bool>}`, **default `false`**, read through `AdminConfigService.get`
on every request. A `Settings` fallback (`ADD_VENUE_GOOGLE_ONLY_ENABLED`, default
`false`) applies only when the admin read fails or the key is absent; the admin
value must always win. An unreadable or malformed override must degrade to
disabled, never to enabled.

**Downstream contracts:** none change. `GET /v1/venues/nearby` already serves
`forecast` and the Google fields; vibes_bot and mobile need no release.

## Error Handling And Observability
- A failed Google place resolution or details fetch on this path must log a
  warning naming the submitted venue and return today's 502 body unchanged. It
  must never turn a 502 into a 500, and must never persist a partial venue.
- An `AdminConfigService.get` failure must be caught and treated as **disabled**,
  logged once at warning level. Failing open would create venues during a config
  outage.
- The monthly-cap 429 branch keeps priority and never reaches this path.
- `ADD_VENUE_BY_ADDRESS_TOTAL` gains three result labels: `created_google_only`,
  `google_only_disabled` (flag off, so the 502 is intentional), and
  `google_only_enrichment_failed` (flag on but Google could not resolve the
  place). The three labels must be distinguishable — an operator has to be able to
  tell "we chose not to" from "we tried and could not".
- A new gauge for the count of `venue_source='google_only'` active venues,
  refreshed by the same stats pass that sets `VENUES_WITH_ATTRIBUTE`, so the
  minority class is visible on the dashboards and its growth rate is watchable.
- Log at INFO on each mint, including the minted id, the resolved Google place id,
  and that no BestTime credit was drawn.

## Test Plan
Feature file: `tests/bdd/api/add-venue-google-only.feature`

The refresh-exclusion scenarios live in this file rather than `tests/bdd/refresh/`
because they are this feature's cost guarantee, not a change to refresh behavior.

Scenarios:
- BestTime rejects the create as unforecastable, the geo fallback finds no match,
  and the flag is enabled — the response must be 201 `created_google_only`, the
  venue must be persisted with a `vsg_`-prefixed id, `forecast` false, and
  `venue_source` `google_only`.
- The same request with the flag disabled must return today's 502
  `besttime_rejected_no_geo_match` byte-for-byte and persist nothing.
- The Google-only path must never call `POST /forecasts` a second time and must
  never call any paid BestTime endpoint.
- The monthly BestTime new-venue counter must not increase, and the venue must not
  be recorded as touched in the ledger.
- A venue created this way must never appear in the bounded live-refresh
  selection, and never in the weekly-refresh selection, even when it is the only
  active venue.
- A venue created this way must appear in `serving.eligible_venue` and in
  `GET /v1/venues/nearby` despite carrying a NULL BestTime `venue_type` — the
  eligibility view's NULL-guard must hold.
- Google place resolution failing while the flag is enabled must return today's
  502 and persist nothing.
- An admin-config read failure must be treated as disabled, returning today's 502.
- Re-adding the identical name and address must return `already_exists` for the
  minted venue, not a second minted row.
- A BestTime rejection whose geo fallback **does** find a name match must still
  complete as `matched_via_geo_fallback` — the Google-only path must not preempt
  a real BestTime link.
- A `/venues/filter` transport failure must stay a terminal 502 and must not fall
  through to the Google-only path.
- A batch-add row resolving to `created_google_only` must count as a success in
  the job summary and must not stop the job.

Also update `tests/bdd/api/besttime-rejection-venue-info.feature`: its two
no-geo-match scenarios must state that the terminal rejection holds **while the
Google-only path is disabled**, so the two feature files stop contradicting each
other.

Pytest unit tests:
- `tests/test_venue_row.py` — `venue_source` round-trips as a **column**, not
  through `extra`; a legacy row without the field reconstructs as `besttime`.
- `tests/test_add_venue_handler.py` — the branch is reached only from
  `match is None`; slot release, ledger, and touch behaviour on the new path;
  flag off / flag on / config-read-failure; enrichment failure persists nothing;
  the minted id is deterministic and `vsg_`-prefixed.
- `tests/test_rds_venue_store.py` — both priority-selection queries exclude
  `google_only` and are otherwise unchanged in ordering.
- `tests/test_batch_add_service.py` — `_classify` maps the new 201 and it is
  absent from `_STOP_OUTCOMES`.
- `tests/test_eligibility_serving_view_parity.py` — extend with a NULL
  `venue_type` + present `google_primary_type` case if not already covered.

Manual or integration checks:
- After the migration, `alembic current` must report `0021_venue_source` and a
  `SELECT count(*) FROM venues.venue WHERE venue_source <> 'besttime'` must return
  0 before the flag is enabled.
- One real operator add of a known-unforecastable venue in prod with the flag on,
  then confirm within one projection cycle (`REDIS_PROJECTION_MINUTES=2`) that it
  is a `venues_geo_v1` member and reaches the app labelled `Sem Informação`.

## Rollout And Rollback

The ordering matters: the migration is expand-only and lands **before** any code
that writes the new value, and the code lands **inert** behind a flag that is off.

1. **Take a manual RDS snapshot and record its identifier in the PR.** The
   migration carries no rewrite and no backfill, so this is insurance against an
   unrelated incident inside the change window, not the rollback path.
2. **Apply `0021_venue_source`.** Verify `alembic current` and that the
   `google_only` count is 0.
3. **Deploy cs-server with the flag off.** This must be a no-op: the endpoint's
   every outcome is unchanged, and `google_only_disabled` is the only new label
   that can increment.
4. **Flip `add_venue_google_only.enabled` to true in vibesadmin.** Add one venue.
   Verify the 201, the minted id, the projection membership, and the app label.
5. **Soak, watching** the new gauge's growth rate, the three new result labels,
   and the `unknown` share of a typical venue-list response in vibes_bot's
   `/metrics`. Because the gate is automatic on every rejection, the growth rate
   is the number to watch: a batch-add job of mostly-unforecastable venues will
   now catalog all of them.

**Rollback levers, in the order to reach for them:**

| Symptom | Lever | Cost |
|---|---|---|
| Too many / low-quality venues being cataloged | Flip the admin flag off | Seconds, no redeploy. Stops new mints; existing rows stay. |
| Existing minted venues must leave serving | `UPDATE venues.venue SET lifecycle_status='deprecated', deprecated_reason='google_only_rollback', deprecated_source='admin_rollback' WHERE venue_source='google_only'` | One projection cycle (≤2 min). **Reversible** — the venue-restore path reactivates them. No data is destroyed. |
| The code path itself is broken | Redeploy the previous cs-server main | One CI run. The column is inert to old code. |
| Genuine data corruption | Restore the snapshot from step 1 | Last resort. Loses engagement writes since the snapshot; slightly stale data accepted per the feature owner. |

**Never run the `0021` downgrade** while `google_only` rows exist. Dropping
`venue_source` makes those venues indistinguishable, and the very next refresh
cycle would feed synthetic ids to BestTime. If the column truly must go, deprecate
the rows first (row 2 above), then drop.

**Two deploy couplings to respect.** vibes_bot's CI (`.github/workflows/main.yml`
→ `sync_cs_server`) git-clones cs-server **main** and builds it on the EC2, so
merging this PR means the next vibes_bot deploy ships it whether or not that was
intended — merge it either well before or immediately before a watched vibes_bot
deploy. And the projector re-asserts the serving projection from RDS every
`REDIS_PROJECTION_MINUTES`, so a manual Redis edit is never a valid rollback lever:
roll back at the flag or at RDS, never at the projection.

## Acceptance Criteria
- An operator add of a Google-known, BestTime-unforecastable venue returns 201
  `created_google_only` with the flag on, and the venue is queryable through
  `GET /v1/venues/nearby` with `forecast=false`.
- The same add returns today's exact 502 with the flag off.
- No BestTime credit and no additional Google SKU is spent on the path: no second
  `POST /forecasts`, no new Google endpoint beyond the `search_place_id` /
  `get_place_details` calls the add path already makes.
- A `google_only` venue never appears in live- or weekly-refresh selection.
- Every pre-existing outcome of `POST /admin/venues/by-address` is unchanged, and
  `tests/bdd/api/add_venue_by_address.feature` stays green untouched except where
  the rejection feature file is explicitly amended.
- `alembic current` reports `0021_venue_source`; no table rewrite occurred.
- The end-to-end path is demonstrated once in prod: mint → projection → app card
  reading **Sem Informação**.

## Open Questions
None. The three decisions that shaped this plan were settled by the feature owner
on 2026-08-04: the entry point is the operator add only (no Google discovery
crawl); creation is automatic on every rejection rather than per-venue opt-in,
with the admin flag as the single gate; and the rollout is snapshot + reversible
flag, with slightly stale data accepted on a restore.

**One consequence worth stating rather than asking about:** a Google-only venue
that *does* carry Google opening hours will render **`Fechado`**, not
`Sem Informação`, whenever it is outside those hours — vibes_bot's shipped
precedence puts trusted-closed above no-data, and 64% of served venues have hours.
That is the correct, already-approved behavior, not a defect. `Sem Informação` is
what these venues show while open.
