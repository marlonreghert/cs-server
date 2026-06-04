# Priority-bounded BestTime refresh within the monthly unique-venue cap

## Branch
feature/besttime-refresh-priority-budget

## Goal
Spend BestTime's scarce monthly allowance (500 unique venues interacted with per
calendar month, counting every live/forecast read — see
`plans/besttime_cap_and_bounded_refresh_intent_04_06_26.md`) on a **prioritized
subset** of venues instead of all ~1331 active venues. Concretely:
1. Add a `priority` column to venues (0 = most important … 5 = least).
2. Live + weekly refresh select the **same** top-X active venues by priority,
   where `X = monthly_quota − manual_reserve`, and touch no other venue.
3. Turn venue discovery **off** (except the manual add-venue flow).
4. Reduce the add-venue geo-fallback radius to **50m** to avoid clutter.
5. Enforce a hard ceiling: the system must **never** ask BestTime for more than
   `monthly_quota` unique venues in a calendar month.

## Non-goals
- Not raising BestTime's real cap. **This plan controls OUR consumption; it does
  not make BestTime accept more.** BestTime's window is independently full for
  the current month, so live data / adds will only actually succeed **next
  calendar month** (per user + the no-usage-endpoint finding). Verification
  against BestTime must wait for the reset; this month proves only our own
  selection/ledger behavior.
- Not reconciling our ledger to BestTime's counter (no usage endpoint exists);
  BestTime's `POST /forecasts` rejection stays authoritative.
- Not building an admin UI for priority. Priorities are set by the one-time
  mitigation UPDATE (below) and by future manual edits.
- Not changing the public serving path (`/v1/venues/nearby` reads Redis, never
  BestTime) — it does not consume the cap and is untouched.
- Not deleting venues. Low-priority venues simply stop being refreshed; their
  existing (possibly stale) data is handled per the Open Question on "set
  forecast/live busyness as agreed".

## Evidence
- Cap semantics + 1331-venue inventory + replication: `/tmp/besttime_troubleshoot/`
  and `plans/besttime_cap_and_bounded_refresh_intent_04_06_26.md`.
- Refresh reads every active venue today:
  `app/services/venues_refresher_service.py:1018` (`refresh_live_forecasts_for_all_venues`
  → `list_active_venue_ids()`), `:1038` (`refresh_weekly_forecasts_for_all_venues`).
- Schedules: `main.py` Job1 `venue_catalog_refresh` (discovery), Job2
  `live_forecast_refresh` (every `venues_live_refresh_minutes`=5), Job3
  `weekly_forecast_refresh` (cron). Settings in `app/config.py:127-129`.
- Budget: `app/services/venue_budget_service.py` (`DEFAULT_MONTHLY_QUOTA=500`,
  `DEFAULT_MANUAL_RESERVE=10`; admin-config key `venue_monthly_budget`); counter
  in `app/dao/venue_budget_dao.py` (`venue_add_counter_v1:YYYY-MM`, counts ADDS
  only — not the reads that actually consume the cap; this conflation was the
  original bug).
- RDS schema: `migrations/versions/0001_baseline_schemas.py` — `venues.venue`
  (promoted cols + JSONB payload, `ix_venue_lifecycle`), `besttime.live_forecast`,
  `besttime.weekly_forecast`, `google_places.vibe_attributes(google_primary_type)`.
- RDS writer/reader: `app/dao/rds_venue_store.py` (`upsert_venue`,
  `list_active_venue_ids`). Repository facade: `app/dao/venue_repository.py`.
- Add-venue radius: `app/handlers/add_venue_handler.py:31-32`
  (`DEFAULT_FALLBACK_RADIUS_M=200`, `MAX_FALLBACK_RADIUS_M=500`); geo fallback at
  `:222-267`.

## Current Behavior
Every scheduled refresh reads **all** active venues from RDS and calls BestTime
per venue. With 1331 active venues vs a 500 unique/month cap, the allowance is
exhausted within days of the 1st and BestTime then errors for the rest of the
month — so most venues lack fresh live busyness, and a new manual add (a 501st
unique venue) is rejected. Discovery (catalog refresh + venue-filter) also
consumes the cap. The local add-counter shows budget remaining even when BestTime
is full.

## Desired Behavior
- Refresh must select at most `X = monthly_quota − manual_reserve` active venues,
  ordered by `priority` ascending (tie-break reviews desc, rating desc), and live
  + weekly must use the **same** set so they touch the same unique venues.
- Discovery must be off (not scheduled; manual trigger rejected).
- The add-venue geo fallback must use a 50m radius.
- A monthly unique-venue ledger must refuse any BestTime read once the calendar
  month's distinct-venue count reaches `monthly_quota`; repeated reads of an
  already-touched venue must not increase that count.

## Implementation Approach
**Budget accounting (named concept).** The 500 monthly cap is consumed by two
**disjoint slices**: `refresh(X)` + `manual_adds(≤ manual_reserve)`. Define a
single source of truth `refresh_budget = monthly_quota − manual_reserve` on
`VenueBudgetService`. `month_counter` (adds) is **not** the cap and must not be
treated as such.

**1. Schema + model.** Alembic migration: `ALTER TABLE venues.venue ADD COLUMN
priority SMALLINT NOT NULL DEFAULT 5` + partial index
`ix_venue_priority ON venues.venue (priority) WHERE lifecycle_status='active'`.
Add `priority: int = 5` to the `Venue` model and to the promoted-column list in
`RdsVenueStore.upsert_venue` (so adds/edits persist it). **Scope the projector
OUT**: priority is a refresh-selection concern read from RDS; do not project it
to Redis (serving does not need it).

**2. Priority-bounded selection.** New `RdsVenueStore.list_active_venue_ids_by_priority(limit)`
→ `SELECT venue_id FROM venues.venue WHERE lifecycle_status='active' ORDER BY
priority ASC, reviews DESC NULLS LAST, rating DESC NULLS LAST LIMIT :limit`,
surfaced through `VenueRepository`. `refresh_live_forecasts_for_all_venues` and
`refresh_weekly_forecasts_for_all_venues` both call it with `limit =
refresh_budget`, so they select the identical set.

**3. Discovery off.** Add `discovery_enabled: bool = False` to settings. In
`main.py`, schedule Job1 only when enabled. Guard the manual `venue_catalog`
trigger in `admin_trigger_router.py` to return a clear "discovery disabled"
response when off.

**4. Add-venue radius.** Set `DEFAULT_FALLBACK_RADIUS_M = 50` and
`MAX_FALLBACK_RADIUS_M = 50`. (Bounds the geo-fallback `venue_filter` blast
radius; see the named edge below.)

**5. Monthly unique-venue ledger (hard ceiling).** A Redis set
`besttime_touched_v1:YYYY-MM` of venue_ids touched this calendar month, written
by a small gate in the BestTime read paths (live, weekly, add, geo-fallback).
Before a read for a not-yet-touched venue, if `SCARD >= monthly_quota`, refuse
(skip + metric) instead of calling BestTime; otherwise `SADD` and proceed.
Already-touched venues pass freely (no new unique). The ledger is the backstop
for the two leaks the priority-limit alone cannot close:
(a) **geo-fallback** touching out-of-set venues — additionally bound it to match
only owned/in-set venues at 50m; (b) **mid-month priority edits** churning the
selected set. **Limitation to state loudly:** a fresh ledger starts at 0 and will
authorize up to `monthly_quota` touches that BestTime may still reject, because
BestTime's window is independently full right now. The ledger enforces *our*
budget; it does not guarantee BestTime accepts until BestTime's window resets.

## Data, Config, And API Impact
- **Migration:** new `priority` column + partial index on `venues.venue`.
- **Model/persistence:** `Venue.priority`; promoted in `RdsVenueStore.upsert_venue`.
- **Config:** new `discovery_enabled` (default False); `refresh_budget` derived
  from existing admin-config `venue_monthly_budget` (`monthly_quota`,
  `manual_reserve`). Confirm production `manual_reserve` (the user temporarily set
  it to 500 to choke discovery; with discovery now flag-off it should return to a
  small value, e.g. 10, so `X≈490`).
- **Redis:** new ephemeral key `besttime_touched_v1:YYYY-MM` (monthly TTL).
- **API:** add-venue request bound `le` drops 500→50. No response shape change.
- **One-time data:** a prioritization UPDATE on `venues.venue.priority` (below).

## Error Handling And Observability
- Metrics: gauge `besttime_unique_venues_touched{year_month}`; counter
  `besttime_read_skipped_total{reason="monthly_cap"}`; counter
  `refresh_selected_total{job}`. Log the selected set size and the
  refresh_budget each run.
- Surface a real BestTime cap rejection clearly (its own status/message),
  mirroring the existing `quota_exhausted` 429 path, instead of laundering it
  through `_geo_fallback` into "rejected the address" (carried from the intent
  doc; keep in scope so cap state is legible during the over-cap month).
- Background jobs must log skips with venue counts; never fail silently.

## Test Plan
Feature file: `tests/bdd/refresh/priority_bounded_besttime_refresh.feature`

Scenarios:
- Live refresh selects ≤ X active venues ordered by priority asc; nothing outside
  the set is requested.
- Weekly refresh reuses the same set as live; union ≤ X distinct venues.
- Deterministic tie-break (priority asc → reviews desc → rating desc), stable.
- Discovery disabled: catalog job not scheduled, manual trigger rejected, no
  venue-filter discovery call.
- Monthly ledger refuses a new-venue read once the month hits `monthly_quota`;
  skip metric increments.
- Already-touched venue re-read does not increase the unique count.
- Add-venue geo fallback uses a 50m radius (candidate >50m not matched).

Pytest unit tests:
- `RdsVenueStore.list_active_venue_ids_by_priority` ordering + LIMIT (against the
  in-memory RDS fake / interface contract).
- `refresh_budget = monthly_quota − manual_reserve` derivation + clamps.
- Ledger gate: admit until cap, refuse the (cap+1)th distinct venue, re-admit a
  touched venue, monthly key rollover.
- Both refresh jobs request the identical selected set.
- Add-venue radius constants and `_geo_fallback` 50m matching.

Manual or integration checks:
- **Effective next cycle only.** Against real BestTime, success can be verified
  **only after the monthly reset** (this month's 500 is already spent). Before
  then, verify our behavior: selection size, ledger refusals, discovery not
  scheduled, 50m radius, metrics — all without requiring BestTime to accept.

## One-Time Pernambuco Prioritization (mitigation — separate deliverable)
Code lands first; data is set by a one-time UPDATE. **Sequence (must hold):**
migrate (priority default 5) → run the prioritization UPDATE → only then enable
discovery-off + bounded refresh. Enabling bounding on the all-default-5 state
would tie-break by reviews and refresh high-review **supermarkets/churches** —
exactly the venues to exclude.

**Dependency to confirm before ranking:** `X = monthly_quota − manual_reserve`
must be ≥ P0+P1 minimums (≥100 + ≥200 = **≥300**). With reserve=10, X≈490 ✓.
Also the SELECT must return **≥300 active Pernambuco venues** for the minimums to
be satisfiable — report the row count.

**Step 1 — run this SELECT and paste the full result back** (I rank one-by-one;
I do not run it). `google_primary_type` is expected to be sparse, so ranking
leans on `venue_name` + type signals:

```sql
SELECT
  v.venue_id,
  v.venue_name,
  v.venue_address,
  v.venue_type                         AS besttime_type,
  va.google_primary_type               AS google_type,
  v.rating,
  v.reviews,
  v.price_level,
  (lf.venue_id IS NOT NULL)            AS has_live_busyness,
  EXISTS (SELECT 1 FROM besttime.weekly_forecast w WHERE w.venue_id = v.venue_id)
                                       AS has_weekly_forecast,
  lf.updated_at                        AS live_updated_at
FROM venues.venue v
LEFT JOIN google_places.vibe_attributes va ON va.venue_id = v.venue_id
LEFT JOIN besttime.live_forecast        lf ON lf.venue_id = v.venue_id
WHERE v.lifecycle_status = 'active'
  AND (
        v.venue_address ILIKE '%- PE %'
     OR v.venue_address ILIKE '%- PE,%'
     OR v.venue_address ILIKE '%- PE'
     OR v.venue_address ILIKE '%pernambuco%'
  )
ORDER BY has_live_busyness DESC, v.reviews DESC NULLS LAST, v.rating DESC NULLS LAST;
```
Fallback if address filtering is unreliable: bound by lat/lng box (approx
Pernambuco: lat −7.0…−10.0, lng −34.5…−41.5) — documented, not preferred.

**Step 2 — ranking rubric (relevance to a nightlife/going-out app):**
- P0/P1 = high relevance going-out venues: bars, pubs, nightclubs, live-music,
  breweries, lively restaurants/lounges; **squares & parks acceptable** (people
  gather there for fun). Existing live busyness is a positive signal (BestTime
  has data and we were refreshing it) — but does **not** override irrelevance.
- Lower (P3–P5): **non-relevant types even if they have live data** — churches,
  supermarkets, drugstores, gyms, schools, offices, transit, generic shops.
- Missing live busyness is **neutral** (likely just capped), not a penalty.
- Minimums: ≥100 P0 and ≥200 P1.

**Step 3 — UPDATE (template; exact ids filled after ranking):**
```sql
UPDATE venues.venue SET priority = 0 WHERE venue_id IN (/* P0 ids */);
UPDATE venues.venue SET priority = 1 WHERE venue_id IN (/* P1 ids */);
-- … through priority = 5
```
"Set forecast/live busyness as agreed on them" is an **Open Question** — its
answer decides whether the UPDATE is priority-only or also prunes
live/weekly rows for non-prioritized venues.

## Acceptance Criteria
- `priority` column exists (default 5) with the partial index; `Venue` and RDS
  upsert persist it.
- Live and weekly refresh request the **same** ≤ `refresh_budget` venue set,
  ordered by priority; no venue outside the set is requested.
- Discovery job is not scheduled and the manual trigger is rejected when disabled.
- The monthly ledger refuses BestTime reads beyond `monthly_quota` distinct
  venues; re-reads of touched venues don't increase the count; skip metric fires.
- Add-venue geo fallback radius is 50m (default and max).
- BDD feature green; targeted pytest green.
- Documented and accepted that **real BestTime success is verifiable only after
  next month's reset** — not a plan failure if adds still fail this month.

## Open Questions
- **Confirm `manual_reserve` for prod** (reset from the temporary 500 to a small
  value so `X = quota − reserve` is sensible, e.g. 10 → X≈490). Drives whether
  P0/P1 minimums fit.
- **"Set forecast/live busyness as agreed"**: for non-prioritized venues, should
  the one-time UPDATE also clear/stop their live + weekly rows (so we don't serve
  stale busyness), or priority-only (leave existing data, just stop refreshing)?
- **Pernambuco row count** from the SELECT must be ≥300 (else revise the P0/P1
  minimums).
- **Verify with BestTime** that touching the same venue_id via both live and
  weekly endpoints counts as **1** unique, not 2 (load-bearing: if 2, X halves).
- Geo-fallback: restrict matching to owned/in-set venues only (closes the
  out-of-set touch leak) — confirm acceptable.
