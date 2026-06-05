# BestTime plan-cap reality, add-venue shelving, and bounded-refresh intent

Status: **investigation record + intent only.** No code changed. The bounded-
refresh fix will be planned separately, later. Capacity/plan choice is a business
decision, not code.

Date: 2026-06-04. Source of the corrected understanding: BestTime support chat
(2026-06-04) + live API replication (raw responses were saved under
`/tmp/besttime_troubleshoot/`, which is ephemeral — the essentials are inlined
below).

## What triggered this
Admin "Add Venue" → "Downtown Pub ZN - Bar de Metal 🤘" (Recife) failed with:
`{"detail":"BestTime rejected the address and the geo fallback found no matching
venue near (-8.0409745,-34.8932654) within 200m"}`.

## What it actually was (root cause)
Replicating the exact prod call (`POST /forecasts`, prod keys from
`app/config.py`) returned **HTTP 400**:
`"Error: Max amount of monthly venues (500) reached. Venue counter will reset at
midnight on the first day of the month..."`

Per BestTime support, the **500 is a plan cap on UNIQUE venue_ids interacted with
per calendar month — counting every forecast/live/query READ, not just new
additions.** It resets on the calendar 1st. Key quotes:
- "the 500-venue quota is not cumulative ... The limit refers to the number of
  unique venues you can interact with during a single calendar month."
- "as soon as you call the API to get data for those venues in the current month,
  they count toward that month's 500-venue limit."
- "Once you hit the 500th unique venue in a month, the API will stop returning
  data for any new (501st) venue IDs until the 1st of the next month."

Our account holds **1331 active venues** (`GET /venues`, credit-free). So our own
live + weekly refresh jobs blow the 500-unique allowance within days of the 1st,
and BestTime then rejects everything else — including a brand-new add. The
add-venue failure is a **symptom of a catalog 2.6× larger than the plan cap.**

The vague "rejected the address" text was a second-order artifact: once
`POST /forecasts` is rejected, the handler runs `_geo_fallback` (a 200m
`venue_filter`), finds no name match for the decorated Google name (nearest was
the *different* "Downtown Beer Garden"), and surfaces that message — burying the
real cap reason in the un-returned `besttime_message`.

## Issues discovered (for later planning)
1. **Catalog over-capacity (the core problem).** 1331 active venues vs a
   500-unique/month plan ⇒ live busyness is structurally unavailable for ~62% of
   venues each month. This is a coverage problem independent of add-venue.
2. **Unbounded refresh wastes the scarce budget.**
   `refresh_live_forecasts_for_all_venues()` / `refresh_weekly_forecasts_for_all_venues()`
   read **every** active venue (`list_active_venue_ids()` → `get_live_forecast`
   per venue, `app/services/venues_refresher_service.py:1018-1045`). On a capped
   plan they spend the 500 unique slots arbitrarily, then error out, instead of
   prioritizing the venues that matter.
3. **Budget counter measures the wrong thing.**
   `VenueBudgetService.month_counter` (`venue_add_counter_v1:YYYY-MM`) counts only
   NEW additions; it is blind to the refresh READS that actually consume
   BestTime's cap. Result: the admin panel advertised "Budget: 0/500 • manual
   500" while BestTime was already full → `reserve_manual_slot` granted a slot the
   account could not honor. (Note: both reset on the calendar 1st — the earlier
   "different reset schedule" theory is superseded; the gap is *what each side
   counts*, not *when each resets*.)
4. **Cap rejection is not surfaced clearly.** A 500-cap rejection is laundered
   through `_geo_fallback` into a misleading 502 "rejected the address". It should
   be its own legible state, mirroring the existing `quota_exhausted` 429 path in
   `app/handlers/add_venue_handler.py`.
5. **No proactive cap query exists.** BestTime has no usage/quota/remaining
   endpoint; `GET /venues` has no created/added timestamp (only `forecast_updated_on`,
   which is last-refresh), so month-to-date unique usage cannot be derived. The
   `POST /forecasts` rejection is the **only** authoritative cap signal ⇒ any
   guard must be reactive.

## Decisions (2026-06-04)
- **Shelve the admin "Add Venue" feature as stale** until there is another way to
  source live busyness for venues beyond the plan cap. (Implementation deferred —
  no code changed now. Lives in the vibes_bot admin panel:
  `app/admin/static/admin.html` + `app/admin/routes.py`.)
- **Plan a bounded-refresh fix later** (this repo): refresh a prioritized ≤cap
  subset rather than all active venues, so the limited allowance is spent on the
  venues that matter instead of erroring out arbitrarily.
- Capacity options are a separate business call: upgrade (Pro Max 5K ≈ $399/mo
  covers 1331), switch to a Metered plan (no unique cap), prune the active set to
  ≤500, or adopt an alternative live-busyness source.

## Intended future work (sketch — not committed scope)
- **bounded-refresh** (cs-server): cap the per-month unique-venue read set to a
  configurable budget; pick the subset by priority (e.g., engagement/favorites,
  rating, recency); make the live/weekly jobs honor it. Add observability for
  unique-venues-touched vs cap.
- **cap-aware budget + surfacing** (cs-server): treat BestTime's cap rejection as
  authoritative; cache a "cap reached" flag with the reset hint; return a clear
  cap status instead of the geo-fallback message.
- **shelve add-venue UI** (vibes_bot): hide/disable the Add Venue tab and show a
  clear "BestTime monthly cap reached / feature paused" notice.
- Rotate the BestTime keys (currently hardcoded in `app/config.py`) and move them
  to env — pre-existing, separate from this work.

## Open questions
- Which capacity path (upgrade / metered / prune / alternative source)? Drives
  whether bounded-refresh is a stopgap or the long-term shape.
- What is the priority signal for the ≤cap refresh subset?
- Does BestTime's "first day of the month" mean UTC or account-local? (Affects
  exactly when the window frees up; not blocking.)

## Evidence pointers
- Replication script + raw responses: `/tmp/besttime_troubleshoot/` (ephemeral):
  `forecasts_decorated_name.json` (the 400 cap error), `account_inventory.json`
  (1331 venues, no created date), `venue_filter_200m_geofallback.json`, `FINDINGS.md`.
- Code: `app/handlers/add_venue_handler.py`, `app/services/venue_budget_service.py`,
  `app/dao/venue_budget_dao.py`, `app/services/venues_refresher_service.py:1018-1045`,
  `app/api/besttime_client.py` (`add_venue_to_account`, `get_live_forecast`).
