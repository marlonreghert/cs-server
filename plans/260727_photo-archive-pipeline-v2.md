# Photo Archive Pipeline v2 — versioned runs, targeting, cost control

## Branch
feature/photo-archive-pipeline-v2

## Goal

Turn the existing one-shot `venue_photo_archive` job into an operable pipeline:
**versioned run paths**, **bounded targeting** (max venues, max photos, geo
radius), a **pre-run cost estimate**, **rate limiting**, and a **job id** that
ties a triggered run to its logs, metrics, and a retrievable run record.

The pipeline already downloads Google photos into the data lake. What it lacks is
everything an operator needs to run it *safely at catalog scale*: today a single
click can walk 1,725 venues x N photos with no cap, no estimate, and no
throttle — and the repo carries a **$10/month Google spend gate**.

## Non-goals

- **Serving archived images to the app.** `media/` is internal-use only;
  every displayed image must come from Google's CDN. Established by
  `plans/260726_venue-list-hero-photo.md` (BLOCKED section) and enforced by
  infra: the bucket sets `restrict_public_buckets = true` and the writer role is
  denied `s3:GetObject` (`infra/datalake/iam.tf:9`). Do not re-propose an S3
  serving path.
- **A second source.** `instagram_posts` and friends stay unimplemented; the
  source stays an extensible enum with `google_photos` its only member. The
  point of this plan is that adding the second source is config, not surgery.
- **Scheduling.** This job is admin-triggered only, deliberately — it spends
  money per photo. No cron.
- **Reading archived bytes back.** The writer role cannot `GetObject` and this
  plan does not change that.
- **Backfilling the existing `dt=` layout** into the new run layout. Old
  partitions stay readable where they are; see Data Impact.

## Evidence

- `app/services/venue_photo_archive_service.py` — the pipeline today.
  `run()` (`:221`) resolves a prefix, selects venues, loops `_archive_venue`
  (`:284`) with per-venue isolation, and returns a summary dict.
  `_archive_venue` orders the already-archived check **before** the Google call
  (`:287-294`) — that ordering is the existing cost guarantee and must survive.
- `_select_venues` (`:193`) supports exactly two modes: every active venue, or a
  comma-separated id list. **There is no cap and no geo filter.**
  `max_photos_per_venue` is a constructor arg (`:158`, default 10), **not** a
  per-run config key.
- `resolve_prefix` (`:172`) implements `new_day | append_latest | override`;
  `day_prefix` (`:82`) yields `media/source=<source>/dt=<YYYY-MM-DD>/`.
- `app/dao/media_archive_store.py` — layout is
  `media/source=<s>/dt=<day>/venue_id=<v>/<photo_id>.jpg` + `_manifest.json`
  (`:12-13`). `list_day_partitions` (`:70`) discovers partitions by
  **`list_objects_v2` with a delimiter**, and notes ISO dates sort
  lexicographically so "latest" is the last entry. `exists_for_venue` (`:94`) is
  the cost gate and fails **open** (returns False) on a listing error.
  The class docstring records that IAM grants PutObject + prefix-scoped
  ListBucket but **not GetObject** (`:15-17`).
- `app/routers/admin_trigger_router.py:153` — the `venue_photo_archive`
  JOB_REGISTRY entry. Its `default_config` (`:160-165`) is
  `{sources, venue_ids, path_mode, path_override, overwrite}` and the comment at
  `:158-159` states **every key becomes a control in the admin pre-run modal**.
  That is the mechanism this plan extends.
- `_run_job` / `trigger_job` (`:194`, `:247`) dispatch by job **name** and hold a
  single `_running_jobs[job_name]` task (`:39`, `:319`) — one run per job name,
  and **no job id is issued or returned**.
- `app/metrics.py:418-465` — `MEDIA_ARCHIVE_{RUNS,VENUES,PHOTOS_STORED,
  PHOTO_FAILURES,BYTES_STORED,RUN_DURATION_SECONDS,LAST_SUCCESS_TIMESTAMP}`
  already exist. There is **no** rate-limit, throttle, or cost metric.
- `GooglePlacesAPIClient.get_place_photos(place_id, max_photos, include_ref)`
  (`app/api/google_places_client.py:509`) is **sequential** over
  `photos[:max_photos]` and carries **no pacing, no concurrency bound, and no
  429 handling** — confirmed by grep for sleep/semaphore/rate/throttle.
- Google spend context: `plans/260726_venue-list-hero-photo.md` Phase 3 records
  the **$10/month gate** ("nothing may merge that [increases the Google bill by
  more than $10/month] without explicit user approval"), the free tier of
  10,000 calls/SKU/month, and ~$7/1,000 calls — with **Q1b flagging that the
  rate card was never verified against Google's current pricing**.
- Serving-set size is **1,725** (`plans/260726_venue-list-hero-photo.md` S3).
  At 10 photos/venue an uncapped run is ~17,250 billed calls.
- Migration head is `0019_venue_closure_signal`; this plan adds **no** migration.
- `app/dao/rds_venue_store.py` exposes `count_venues_in_radius(lat, lng, radius)`
  (used by `recount_discovery_points`, `venues_refresher_service.py:718`) — the
  geo primitive the point+radius eligibility mode reuses rather than reinvents.

## Current vs desired behavior

| Situation | Current | Desired |
|---|---|---|
| Operator triggers with defaults | walks **every** active venue, up to 10 photos each, unbounded spend | bounded by `max_venues` + `max_photos_per_venue`, both required |
| Operator wants venues near a point | impossible | `eligibility.mode = "point_radius"` |
| Operator wants to know the bill first | impossible | `POST /admin/trigger/venue_photo_archive/estimate` |
| Two runs on the same day | collide in one `dt=` prefix; second skips the first's venues | each run gets its own `run_ts=/run_id=` prefix; skipping is an explicit choice |
| "Where did the last dump land?" | infer by listing `dt=` partitions | `_latest.json` marker at the source root + listing still works |
| Google 429 / burst | no pacing; retries nothing | token-bucket pacing + bounded concurrency + backoff on 429 |
| Correlating a run to its logs | job name only | `job_id` returned by the trigger, on every log line, and in a retrievable run record |

## Implementation approach

### 1. Path layout — versioned runs (`media_archive_store.py`)

New layout, keeping the Hive-style `key=value` convention so the existing lake
tooling still discovers it:

```
media/source=<source>/year=<YYYY>/month=<MM>/day=<DD>/run_ts=<YYYYMMDDTHHMMSSZ>/run_id=<uuid4>/venue_id=<venue_id>/<photo_id>.<ext>
                                                                               .../venue_id=<venue_id>/_manifest.json
media/source=<source>/_latest.json
```

`run_ts` is UTC, second-resolution, **lexicographically sortable** — the same
property `list_day_partitions` already relies on, so "latest run" is resolvable
by `list_objects_v2` + delimiter walks and needs **no `GetObject`**. This is a
hard constraint, not a preference: the writer role cannot read objects.

`_latest.json` is written (PutObject — permitted) at the end of a successful run
as an **informational marker** carrying `{source, prefix, run_id, run_ts,
completed_at, venues_archived, photos_stored, bytes_stored}`. The pipeline itself
never reads it back — `append_latest` resolves by listing. Analytics roles that
do hold `GetObject` are its audience.

New store methods: `list_run_prefixes(source) -> list[str]` (ascending, latest
last), `put_latest_marker(source, marker)`, and `exists_for_venue` gains an
explicit `reference_prefix` argument (see §3).

### 2. Path modes (`resolve_prefix`)

- `new_run` (**new default**) — mint `run_ts` + `run_id` under today's
  `year=/month=/day=`.
- `append_latest` — resolve the most recent run prefix by listing; **fall back to
  a new run** when none exists (preserving the existing, deliberate fallback at
  `:186-189`).
- `override` — operator-supplied prefix, still validated by `validate_override`
  (unchanged: normalised, traversal-rejected, must stay under `media/`).

`new_day` is **retained as an alias of `new_run`** rather than deleted, so a
saved admin config or a queued call does not break.

### 3. The cost gate must survive versioned paths (the load-bearing decision)

`_archive_venue` skips a venue when `exists_for_venue(prefix, venue_id)` — with
`prefix` being where we are *writing*. Under `new_run` that prefix is empty by
construction, so **every run would re-pay Google for every photo**. Left
unhandled this turns a safety feature into a bill.

Therefore the existence check takes a **reference prefix** decoupled from the
write prefix, chosen by a new config key `skip_scope`:

- `latest_run` (**default**) — check the most recent *previous* run for this
  source. A new versioned run does not re-buy what the last run already captured.
- `this_run` — check only the write prefix (meaningful for `append_latest` /
  `override`, and for resuming an interrupted run).
- `none` — check nothing; every selected venue is fetched. Requires
  `overwrite: true` so it cannot be reached by accident.

`overwrite: true` continues to bypass the skip entirely. The ordering — resolve
reference prefix, check, *then* call Google — is preserved exactly.

### 4. Eligibility (`_select_venues`)

`eligibility.mode`:
- `all` — every active venue (today's behavior).
- `venue_ids` — comma-separated, reusing `parse_venue_ids`; unknown ids reported,
  never fatal (unchanged).
- `point_radius` — `{lat, lon, radius_km}`, resolved through the existing
  `rds_venue_store` radius primitive. Validation: lat in [-90,90],
  lon in [-180,180], radius_km in (0, 500].

Selection is then **truncated to `max_venues`**, and the summary reports both
`selected` and `truncated_from` so a cap that silently halved the run is visible.
`max_venues` and `max_photos_per_venue` are required, validated positive ints
with conservative defaults (see §7).

### 5. Rate limiting and throttling

A small `AsyncRateLimiter` (token bucket, `rate_per_second` + burst) in
`app/utils/`, applied to **Google photo requests** and, separately, to **S3
puts**. Plus:
- bounded concurrency via `asyncio.Semaphore` (`concurrency`, default 4) so a
  large run does not open 1,725 sockets;
- retry with exponential backoff + jitter on HTTP **429** and 5xx, capped at
  `max_retries` (default 3), counted in a metric;
- the limiter is injected, so tests drive it deterministically with a fake clock
  rather than sleeping.

Per-photo timeout and the byte cap already exist and are unchanged.

### 6. Cost estimate

`POST /admin/trigger/venue_photo_archive/estimate` accepts the *same* config body
as the trigger and returns, **without making a single Google call**:

```
venues_selected, venues_after_skip (when skip_scope resolvable), photos_max,
est_google_calls, est_cost_usd, est_bytes, est_duration_seconds, assumptions[], caveat
```

`est_google_calls = venues_after_skip * max_photos_per_venue` (an upper bound —
venues with fewer photos cost less). Cost uses a new setting
`google_photo_cost_per_1k_usd` (default `7.0`) because
`plans/260726_venue-list-hero-photo.md` **Q1b** records that this rate card was
never verified. The response's `caveat` says so in words: an upper-bound
estimate, unverified unit price, may be wrong.

The estimate is also **logged**, so what an operator saw before triggering is
reconstructable.

### 7. Config schema (drives the admin modal)

```json
{
  "source": "google_photos",
  "path_mode": "new_run",
  "path_override": "",
  "max_venues": 50,
  "max_photos_per_venue": 5,
  "eligibility": {"mode": "all", "venue_ids": "", "lat": null, "lon": null, "radius_km": null},
  "skip_scope": "latest_run",
  "overwrite": false,
  "dry_run": false
}
```

Defaults are deliberately **small** (50 venues x 5 photos = 250 calls, well
inside the free tier) so the default click is cheap; scaling up is a conscious
edit. `dry_run` runs selection, prefix resolution, and the estimate, and writes
nothing — the safe rehearsal.

Validation happens **before** any Google call and returns a 400 with the offending
field. `sources` (list) is accepted as a deprecated alias for `source`.

### 8. Job id and run records

`trigger_job` mints a `job_id` (uuid4), returns it in the response body, and
passes it into the runner config. Every log line the pipeline emits carries
`job_id=<id>` (Loki-queryable), and on completion the summary — including
`job_id`, config echo, counts, duration, and estimated vs actual calls — is
stored in Redis (`admin:job_run:<job_id>`, 30-day TTL) and served by
`GET /admin/jobs/runs/{job_id}`, plus `GET /admin/jobs/runs?limit=` for the
recent list.

**Deliberately not a Prometheus label.** A uuid label is unbounded cardinality
and would degrade the whole metrics store; per-run detail belongs in Loki and the
run record. Prometheus keeps aggregate labels (`source`, `result`) only. The
dashboard reconciles the two: aggregate panels from Prometheus, per-`job_id`
panels from Loki.

### 9. Metrics (added)

`MEDIA_ARCHIVE_VENUES_SELECTED` (gauge, by source), `..._VENUES_TRUNCATED_TOTAL`,
`..._GOOGLE_CALLS_TOTAL{source}`, `..._THROTTLED_TOTAL{source,reason}` (429/5xx
backoffs), `..._RATE_LIMIT_WAIT_SECONDS` (histogram),
`..._ESTIMATED_COST_USD` (gauge, last run), `..._RUN_INFO` — plus
**`MEDIA_ARCHIVE_VENUES_WITH_MEDIA`**, a coverage gauge (venues with at least one
archived photo in the latest run / active venues) so "how much of the catalog has
pictures" is answerable, which is the operator question behind this whole feature.

## Data, config, and API impact

- **API (admin only):** `POST /admin/trigger/{job}` response gains `job_id`.
  New `POST /admin/trigger/venue_photo_archive/estimate`,
  `GET /admin/jobs/runs/{job_id}`, `GET /admin/jobs/runs`. No public API change.
- **Persistence:** no migration, no RDS schema change. New Redis keys
  `admin:job_run:<job_id>` (30d TTL) and `admin:job_runs` (capped recent index).
- **S3:** new run-scoped prefix layout + `_latest.json`. Additive — existing
  `dt=` partitions are untouched and remain listable; `append_latest` prefers a
  `run_ts=` prefix and ignores legacy `dt=` ones (they are not run-scoped).
- **New settings:** `photo_archive_rate_per_second` (default 5),
  `photo_archive_concurrency` (4), `photo_archive_max_retries` (3),
  `photo_archive_default_max_venues` (50),
  `photo_archive_default_max_photos` (5), `google_photo_cost_per_1k_usd` (7.0).
- **Cost:** default config = 250 upper-bound calls. The $10/month gate is
  respected by defaults + the estimate; **no scheduled execution is added**, so
  steady-state spend stays $0 unless an operator clicks.

## Error handling and observability

Per-venue and per-photo isolation is preserved verbatim — one bad venue or photo
never aborts a run that may be paying for thousands of others.

| Failure | Behavior |
|---|---|
| Invalid config (bad mode, radius, caps, override path) | 400 **before** any Google call; nothing written |
| Venue has no `google_place_id` | counted `no_place_id`; no Google call |
| Google 429 / 5xx | backoff + retry to `max_retries`, then count the venue failed and continue |
| Photo exceeds byte cap / download error | counted, venue continues with its other photos |
| S3 put fails | photo counted failed; venue continues |
| Manifest write fails | loud log, venue still counted archived (existing behavior) |
| `_latest.json` write fails | warn only — the run's images are already durable |
| Listing fails during skip check | fails **open** (re-archive) — existing, deliberate: wrongly skipping loses data silently, wrongly fetching only costs money and is visible |
| Redis unavailable for the run record | run still completes; record is best-effort |

## Test plan

Feature file: `tests/bdd/enrichment/photo-archive-pipeline-v2.feature`

Scenarios:
- A run under `new_run` writes to a `run_ts=/run_id=` prefix and the images land under it.
- Two runs on the same day write to two different run prefixes and neither overwrites the other.
- `append_latest` resolves the most recent run prefix; with no prior run it falls back to a new one.
- `override` outside `media/` is rejected before any Google call is made.
- `skip_scope=latest_run` skips a venue archived in the previous run and makes **zero** Google calls for it.
- `skip_scope=none` without `overwrite` is rejected.
- `overwrite: true` re-fetches an already-archived venue.
- `max_venues` truncates the selection and the summary reports `truncated_from`.
- `max_photos_per_venue` bounds the photos requested per venue.
- `eligibility.mode=point_radius` selects only venues inside the radius.
- `eligibility.mode=venue_ids` ignores unknown ids and reports them without failing.
- An invalid radius / lat / lon is rejected with 400 before any Google call.
- A Google 429 is retried with backoff and counted; the run completes.
- `dry_run` performs selection and estimate and writes nothing to S3 and calls no Google API.
- The estimate endpoint returns an upper-bound call count and cost and makes no Google call.
- A successful run writes `_latest.json` for the source.
- Triggering returns a `job_id`, and the run record is retrievable by that id.
- A per-venue failure does not abort the run; remaining venues still archive.

Pytest unit tests:
- Run-prefix construction and **lexicographic ordering** of `run_ts` (the property `append_latest` depends on).
- `validate_override` traversal cases (kept green).
- Config validation matrix: every field's accept/reject boundary, and the `sources`→`source` alias.
- Selection: cap truncation, geo bounds, id de-duplication, unknown ids.
- `skip_scope` resolution — that `latest_run` picks the previous run and never the current one.
- `AsyncRateLimiter`: pacing under a fake clock, burst behavior, and that it never sleeps in tests.
- Backoff: 429 retried to the cap then surfaced; jitter bounded.
- Cost estimate arithmetic, including the zero-venue and skip-everything cases.
- Run record write/read + TTL, and graceful degradation when Redis is down.

## Acceptance criteria

- No config path can trigger an unbounded run: `max_venues` and
  `max_photos_per_venue` are always applied, defaults are small, and the
  estimate is available before spending.
- **A versioned run never silently re-pays Google** for venues the previous run
  already archived — proven by a scenario asserting zero Google calls, not by
  inspection.
- Every run is addressable: `job_id` returned on trigger, present in logs, and
  retrievable as a run record.
- The archive is discoverable without `GetObject` — latest-run resolution uses
  listing only.
- Rate limiting is enforced and 429s are retried and counted.
- Existing behavior preserved: per-venue/photo isolation, skip-before-spend
  ordering, `validate_override` semantics, and the existing metrics keep working.
- No migration, no public API change, no scheduled execution.

## Open questions

None blocking. Two recorded assumptions, both surfaced in the estimate's
`caveat` rather than hidden:

- The Google Photo unit price (`$7/1,000`) is **unverified** — inherited Q1b from
  `plans/260726_venue-list-hero-photo.md`. It is a setting so a correction is a
  config change, not a code change.
- Archived-photo retention under Google's Places terms is governed by the
  existing `media/` internal-use-only posture; this plan does not widen it.
