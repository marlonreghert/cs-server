# Server-side batch-add endpoint (deterministic bulk venue adds)

Date: 2026-07-05 · Branch: `feature/batch-add-venues-endpoint`

## Why

Bulk venue adds have been driven from the client (an operator/agent looping
`POST /admin/venues/by-address` row by row). That burns the operator's Claude
session budget on orchestration, stalls on client/tooling timeouts, and is
non-deterministic across interruptions. Move the loop **server-side**: one
request ships the whole list, the server runs each row through the **existing**
`AddVenueHandler.add()` (same dedupe, geo-fallback, timeout-recovery, Google
enrichment, and the PR #71 venue-search rate limiter that paces BestTime), and
persists a structured summary the client polls. Finishing a campaign becomes one
POST + a few status GETs instead of hundreds of client round-trips.

## Behavior

- `POST /admin/venues/batch-add` accepts `{venues: [{venue_name, venue_address,
  venue_lat?, venue_lng?, place_id?}], resolve_coords?: true}` (1..1000 rows).
  It creates a job, launches a background asyncio task, and returns
  `{job_id, total, status:"running"}` immediately (never blocks on the adds).
- The background task processes rows **strictly sequentially** — reusing the one
  proven add path per row, so every existing guarantee holds (no live/forecast
  retrieval at add time, deterministic venue_id, monthly-slot reserve/release,
  optimistic geo-link + 24h undo). No artificial sleeps: BestTime pacing is the
  client limiter's job (30/min, 300/hr; over-budget fails fast as besttime_error).
- Coordinate resolution: a row missing lat/lng (and `resolve_coords` true) is
  resolved via the Google client (place_id → location, else Text Search →
  location). Unresolvable → row outcome `skipped_unresolved_coords`, no spend.
- Each row's outcome is classified and appended to the job doc in Redis after it
  completes, so `GET /admin/venues/batch-add/{job_id}` shows live progress
  (`processed/total`) and, when done, the full per-row results + a counts
  summary + budget before/after.
- Stop conditions (job ends early, status `stopped`): a `quota_exhausted` or
  `besttime_monthly_cap` (429) row, or a `besttime_bad_response` (our contract
  bug) — all three mean "don't keep spending". A terminal
  `besttime_rejected_no_geo_match` is recorded and the job CONTINUES (per-venue
  terminal, not batch-fatal).
- Idempotent/resumable: re-POSTing the same list is safe — already-added rows
  short-circuit `already_exists` for free (deterministic ids), so a re-run only
  fills gaps.

## Outcome classification (from AddVenueOutcome status_code + body)

| Condition | job outcome | job effect |
|---|---|---|
| 201, `recovered_from_timeout` | `created_recovered_timeout` | continue |
| 201 | `created` | continue |
| 200 `already_exists` | `already_exists` | continue |
| 200 `matched_via_geo_fallback` | `geo_linked` (+newly_linked, match_reason) | continue |
| 429 (`Monthly venue quota` / cap) | `quota_exhausted` / `besttime_monthly_cap` | **stop** |
| 502 detail ~ "unparseable" | `besttime_bad_response` | **stop** |
| 502 with `candidates_seen` | `besttime_rejected_no_geo_match` | continue |
| 502 other | `besttime_error` | continue |
| coords unresolved (pre-handler) | `skipped_unresolved_coords` | continue |

## Feature file

`tests/bdd/api/batch-add-venues.feature`
- Scenario: a batch runs every row through the add flow and summarizes outcomes
  (created + already_exists + terminal rejection in one job → correct counts,
  status done).
- Scenario: a quota-exhausted row stops the batch and leaves the rest unrun.

## Implementation

- `app/api/google_places_client.py`: `get_place_location(place_id)` (GET
  places/{id}, fieldmask `location`) → `(lat, lng)` or None.
- `app/models/batch_add.py`: `BatchAddRow`, `BatchAddRequest`.
- `app/services/batch_add_service.py`: `BatchAddService(handler, google_client,
  redis_client, budget_service)`; `start_job`, `_run_job` (background),
  `get_job`; Redis key `admin:batch_add_job:{job_id}` (JSON, 7-day TTL); keeps a
  task ref per job to avoid GC; done-callback logs exceptions and marks the job
  `failed` on an unexpected error.
- `app/routers/admin_trigger_router.py`: the two endpoints, mirroring the
  by-address wiring (`_container.batch_add_service`).
- `app/container.py`: construct `batch_add_service` after `add_venue_handler`.

## Tests

- Unit (`tests/test_batch_add_service.py`): fake handler returning scripted
  outcomes + fake redis + fake google → per-row classification, summary counts,
  coord resolution + unresolved skip, quota-exhausted early stop, resume-safety
  (already_exists passthrough). No real network, no sleeps.
- BDD: the two scenarios above over a stubbed handler.

## Ops

No schema/Redis-key-format migration (a new job namespace only); no deploy-order
constraint. The endpoint is additive — `POST /admin/venues/by-address` is
unchanged. `# bdd-note`: BestTime pacing is unit-level timing already covered by
PR #71; batch BDD asserts classification/summary, not wall-clock.
