# BestTime Probe Manifest

This directory holds the fixtures captured by
`scripts/probe_besttime_forecasts.py`, per the Pre-Implementation Verification
section of `plans/add_venue_by_address_26_05_26.md`.

## Status

| Probe | File | First run | Status | Notes |
| --- | --- | --- | --- | --- |
| A — idempotent re-add | `forecasts_post_known_ok.json` | 2026-05-26 | **UNVERIFIED** | Account already at 500/month cap; `/forecasts` returns `{"status":"Error","message":"Max amount of monthly venues (500) reached…"}`. Re-probe required after 2026-06-01 reset. |
| B — deliberate fake address | `forecasts_post_unknown_error.json` | 2026-05-26 | **UNVERIFIED** | Same cap guard masked the geocoder-rejection shape. Re-probe required after 2026-06-01. |
| C — `/venues/filter` radius hit | `venues_filter_radius_200m.json` | 2026-05-26 | **VERIFIED** | 200 OK, returned Casas Bahia with full `venue_id`, `venue_name`, `venue_address`, `venue_lat`, `venue_lng`, `day_info`, `rating`, `reviews`, `venue_type`. Geo-fallback contract pinned. |
| D — fresh BestTime create | `forecasts_post_fresh_create_ok.json` + `forecasts_post_fresh_then_reread_ok.json` | (not run) | **BLOCKED** | Cannot run while monthly cap is at 500/500. Run once, supervised, after 2026-06-01. |

## Locking-in plan after 2026-06-01

1. Re-run `BESTTIME_KEY=… GOOGLE_PLACES_KEY=… python scripts/probe_besttime_forecasts.py --probe ab` to capture the real success + geocoder-rejection shapes.
2. Run Probe D once with a real venue we actually want, following the script's interactive prompts.
3. Diff each captured fixture against the version checked in here. Any field changes (new optional fields, renames, type changes) must be reflected in `app/models/` and `app/api/besttime_client.py`.
4. Flip the row's Status column to **VERIFIED** above and commit.

## What the unverified fixtures actually contain today

Probes A and B captured the **server-side monthly-cap error shape**:

```json
{
  "status": "Error",
  "message": "Error: Max amount of monthly venues (500) reached. Venue counter will reset at midnight on the first day of the month. Contact us for a higher monthly limit"
}
```

This is a real failure shape the production add-venue path must also tolerate (it
is the failure mode that triggers if our internal counter drifts from
BestTime's). The client model treats any non-2xx + `status="Error"` body as a
recoverable failure, which covers both this cap message and the
geocoder-rejection shape we'll capture in June.
