# Bounded Refresh Selects Served (Eligible) Venues by Priority

## Branch
fix/eligible-priority-live-refresh

## Goal
Point the bounded BestTime refresh (live + weekly) at the **served** venues —
the eligibility serving view `serving.eligible_venue`, ordered by priority —
instead of all `active` venues. This concentrates the scarce monthly BestTime
budget on venues actually served to users, improving served-venue live-data
coverage. (Standalone cs-server pipeline fix; see wrapper coordination plan
`plans/260617_eligible-priority-live-refresh.md`.)

## Non-goals
- **No in-month miracle.** The monthly unique-venue ledger is a hard cap
  (`monthly_quota`, currently 500) and is **maxed for June** (`besttime_touched_v1`
  = 500/500). While maxed, not-yet-touched venues stay cap-skipped regardless of
  selection. The coverage gain is **steady-state — realized from the next ledger
  cycle** (calendar-month rollover), not the day this ships.
- Not raising the monthly quota or the refresh budget (`quota − reserve`). That
  capacity decision is separate; this only changes *which* venues the existing
  budget targets.
- No touched-first / hybrid selection. Considered and **deferred** (user chose
  priority-first); a touched-first or hybrid ordering that lifts coverage within
  a maxed month is a possible phase 2 (needs a Redis-ledger × RDS-priority join).
- No change to eligibility rules, the serving view, the Redis projection, the
  ledger gate, or the cap. No vibes_bot / mobile change.

## Evidence
- `app/services/venues_refresher_service.py:141-160` — `_select_refresh_venue_ids`
  selects `list_active_venue_ids_by_priority(budget)`; **live and weekly both call
  it**, so both refresh the same active-scoped set.
- `app/dao/rds_venue_store.py:180-192` — `list_active_venue_ids_by_priority`:
  `WHERE lifecycle_status='active' ORDER BY priority ASC, reviews DESC NULLS LAST,
  rating DESC NULLS LAST, venue_id ASC LIMIT`. Scoped to active, not servable.
- `app/dao/rds_venue_store.py:204-214` — `list_servable_venue_ids` already reads
  `serving.eligible_venue` (returns `venue_id` only). The view (migration 0009)
  has no priority column → join `venues.venue` for `priority`.
- `app/dao/venue_repository.py:111` — repository wrapper the refresher calls
  through; add the servable variant here too.
- **Live diagnosis (2026-06-17):** active=1255, servable=849, refresh budget=400
  (`quota 500 − reserve 100`). Of the top-400 *active* selection: **141 (35%) are
  not servable** (budget burned on non-served venues) and **590 of 849 servable
  venues are never candidates**. Refresher logs are flooded with
  `monthly unique-venue cap reached; skipping`. Only ~133 served venues currently
  carry fresh (<24h) live data.

## Current Behavior
The bounded refresh selects the top-`budget` **active** venues by priority. ~35%
of the budget targets active-but-not-served venues, and 590 served venues are
never selected for a BestTime read.

## Desired Behavior
The bounded refresh selects the top-`budget` **served** venues
(`serving.eligible_venue`) by priority, with the same tie-breaks. Every refresh
slot targets a served venue; no active-but-non-served venue consumes budget.
Steady-state (from the next ledger cycle), served-venue live-data coverage rises
toward the budget — bounded above by the served venues that actually return
BestTime live data (≤ the count with live forecasts today, ~470, likely less:
some touched venues return no live data), not by 849 or even 400.

## Implementation Approach
- **`rds_venue_store.list_servable_venue_ids_by_priority(limit)`** (new): 
  `SELECT ev.venue_id FROM serving.eligible_venue ev JOIN venues.venue v
  ON v.venue_id = ev.venue_id ORDER BY v.priority ASC, v.reviews DESC NULLS LAST,
  v.rating DESC NULLS LAST, v.venue_id ASC LIMIT :limit`. Non-positive limit →
  `[]` (mirrors the active variant).
- **`venue_repository`**: add the matching pass-through.
- **`_select_refresh_venue_ids`**: call `list_servable_venue_ids_by_priority(budget)`
  instead of `list_active_venue_ids_by_priority(budget)`; update the log line to
  say "servable by priority". The standalone fallback (no budget service) should
  select the full servable set (`list_servable_venue_ids()`) rather than all
  active, to stay consistent with serving.
- The ledger gate (`_ledger_allows_read`) is unchanged: already-touched venues
  still re-read free; not-yet-touched stay cap-gated.
- `list_active_venue_ids_by_priority` is left in place (still covered by
  `tests/test_priority_bounded_refresh.py`); it simply stops backing the
  refresher.

## Data, Config, And API Impact
- No migration, no API change, no Redis key change (reuses `serving.eligible_venue`
  + `venues.venue`). Budget and monthly cap unchanged.
- Behavioral: the set of venues receiving BestTime live/weekly reads narrows to
  served venues.

## Error Handling And Observability
- A serving-view/selection read failure must **fail safe** — skip the refresh
  cycle (as the projector already aborts on a view-read failure) rather than fall
  back to an unbounded or active-scoped refresh. Log with job context.
- `REFRESH_SELECTED_TOTAL{job}` already records selection size; its meaning is now
  servable-scoped (note in the log). No new metric required, though a future
  served-coverage gauge could live in cs-server (the admin coverage panel reads
  Redis for now — see `vibes_bot/plans/260617_admin-stats-counts.md`).

## Test Plan
Feature file: `tests/bdd/refresh/eligible-priority-live-refresh.feature`

Scenarios:
- Bounded refresh selects only served (eligible) venues, ordered by priority, up
  to the budget.
- An active-but-not-eligible venue is not selected (it was under the old
  active-scoped selection).
- A higher-priority served venue is selected ahead of a lower-priority one;
  selection is capped at the refresh budget.
- The ledger gate is unchanged: already-touched served venues refresh under a
  maxed cap; not-yet-touched served venues are cap-skipped.
- Degraded: a serving-view read failure skips the cycle rather than refreshing an
  unbounded/active set.

Pytest unit tests:
- `list_servable_venue_ids_by_priority`: returns servable ids in priority order
  with the documented tie-breaks; honors `limit`; non-positive → `[]`; excludes
  active-but-non-servable ids.
- `_select_refresh_venue_ids`: sources from the servable-by-priority method and
  respects the budget; standalone fallback uses the servable set.

Manual or integration checks:
- After deploy + next ledger cycle: confirm refresher logs `selected … servable
  by priority`, and that served-venue fresh-coverage climbs toward the budget
  (cross-check against the admin live-coverage panel).

## Acceptance Criteria
- Bounded live/weekly refresh draws exclusively from `serving.eligible_venue`,
  ordered by priority, capped at the budget.
- No active-but-non-served venue consumes a refresh slot.
- The plan and acceptance explicitly treat the coverage gain as
  steady-state/next-ledger-cycle (no claimed in-month lift while the cap is
  maxed).
- `make test-bdd` and the targeted pytest pass.

## Open Questions
- None. Selection strategy resolved: priority-first over eligible venues
  (touched-first / hybrid deferred to a possible phase 2).
