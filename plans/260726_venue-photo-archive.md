# Venue Photo Archive Pipeline

## Branch
feature/venue-photo-archive

## Goal
Add an admin-triggerable pipeline that downloads every available Google Places
photo for venues and stores the image bytes in the data lake bucket under a
`media/` prefix with a day-only dated partition, so the catalog's imagery is
captured as a dated snapshot rather than only as ephemeral keyless URLs.

The run is parameterised from the admin panel before it starts:

- **Path mode** — write into today's day partition, append to the latest
  existing day, or use an explicit override prefix.
- **Sources** — `google_photos` only for now, shaped so a second source is a
  registry entry rather than a refactor.
- **Venue subset** — an optional comma-separated `venue_ids` list; empty means
  the whole active catalog.
- **Overwrite** — off by default. A venue that already has objects in the target
  partition is skipped **before any Google call is made**, so an interrupted run
  resumes without paying for the same photos twice.

## Non-goals
- **Serving these images.** Nothing reads from `media/`; the app keeps serving
  the existing fresh keyless URLs. This pipeline only writes.
- **Other sources.** Instagram, Apify, and menu photos are out of scope. The
  `source=` partition and a source registry keep the door open.
- **Deduplication across days.** Each dated partition is an independent
  snapshot; the same photo appearing on two days is stored twice by design.
- **Migrating the existing menu-photo bucket** into this layout or into
  Terraform.
- **The admin modal itself** — that is vibes_bot's plan
  (`plans/260726_venue-photo-archive-modal.md`). This repo only defines and
  consumes the config contract.

## Evidence
- `JOB_REGISTRY` (`app/routers/admin_trigger_router.py:71`) already accepts a
  per-run `config` dict: `trigger_job(job_name, config)` passes it to the
  runner, `_run_job` logs it, and `/admin/jobs` exposes each job's
  `default_config` (`app/routers/admin_trigger_router.py:218`). The parameterised
  trigger mechanism exists — this feature is the first to use it seriously.
- vibes_bot's proxy already forwards a JSON body:
  `trigger_enrichment_job` reads `await request.json()` and passes it through as
  `json_body` (`vibes_bot/app/admin/routes.py:1408`). No API change is needed on
  either side; only the browser modal is missing.
- `GooglePlacesAPIClient.get_place_photos(place_id, max_photos=5)`
  (`app/api/google_places_client.py:509`) is a two-step call: Place Details with
  `PHOTOS_FIELDS_MASK` returns photo resource names, then each is resolved to a
  keyless media URI. It returns **URLs, not bytes**, and caps at 5.
- `MenuPhotoEnrichmentService` is the precedent for downloading image bytes and
  putting them in S3, via `S3Client.upload_photo_bytes`
  (`app/api/s3_client.py:45`), which writes
  `places/<venue_id>/photos/menu/<photo_id>.jpg`.
- `S3Client.__init__` (`app/api/s3_client.py:34`) **requires** static access
  keys. `DatalakeWriter._build_s3_client` (`app/dao/datalake_writer.py`) already
  solves this correctly by falling through to the default credential chain (the
  EC2 instance role); the new store reuses that helper rather than the old
  client.
- The data lake bucket, its layout conventions, and the writer IAM policy live
  in `infra/datalake/` — the writer currently holds `s3:PutObject` on
  `raw/*` **only** (`infra/datalake/iam.tf`).

## Current Behavior
Venue photos are resolved on demand to short-lived keyless
`googleusercontent.com` URLs and cached in Redis
(`PhotoEnrichmentService.fetch_and_cache_photos`, capped at 5). Those URLs
expire, and nothing ever retains the image bytes. There is no way to ask "what
did this venue's photos look like last month", and no admin job that archives
imagery. `media/` does not exist in the bucket, and the writer IAM policy would
reject a write there.

## Desired Behavior
1. A new `venue_photo_archive` job appears in `/admin/jobs` with a
   `default_config` describing its options.
2. Triggering it with a config resolves a **target prefix** from `path_mode`,
   selects the venue set, and for each venue: skips if already archived in that
   prefix (unless `overwrite`), otherwise fetches every available Google photo
   and stores each image under the target prefix.
3. Objects are keyed
   `media/source=google_photos/dt=<YYYY-MM-DD>/venue_id=<venue_id>/<photo_id>.jpg`
   — the same Hive-style `key=value` convention as `raw/`, so the media archive
   is queryable/joinable with the same tooling.
4. The run returns and logs a summary: venues considered, skipped, archived,
   failed, photos stored, bytes stored.
5. A venue that fails (no place id, Google error, download error, S3 error) is
   logged and counted, and the run continues to the next venue.
6. Every outcome is exposed as Prometheus metrics so a run's cost and health are
   visible in Grafana.

## Implementation Approach

### A. Target-prefix resolution (`path_mode`)
One pure, unit-testable function maps config → prefix:

- `new_day` (default): today's Recife date, `media/source=<src>/dt=<today>/`.
  Recife rather than UTC because an operator triggering a run at 22:00 local
  means "today" in their own terms; unlike the raw lake's automated hourly
  partitions, this path is chosen by a human. The chosen date is echoed in the
  run summary so there is never ambiguity about where a run landed.
- `append_latest`: list the `dt=` partitions under `media/source=<src>/` and use
  the lexicographically greatest (ISO dates sort chronologically). If none
  exists, fall back to `new_day` — stated explicitly so the first-ever run has
  defined behavior.
- `override`: use `path_override` verbatim. **Validated**: must be non-empty,
  must normalise under `media/`, and must not contain `..`. A run that would
  escape the prefix is rejected before any Google call, because the writer's IAM
  policy would reject those puts anyway and a half-failed run costs real money.

### B. Venue selection
`venue_ids` is a comma-separated string (trimmed, empties dropped, de-duplicated
while preserving order). Empty/absent means the active catalog from the RDS
repository. Unknown ids are reported in the summary rather than failing the run,
so a typo in a 40-id paste does not waste the whole run.

### C. Skip-before-spend
For each venue, when `overwrite` is false, list
`<prefix>venue_id=<venue_id>/` with `MaxKeys=1`. Any hit → skip, counted as
`skipped_existing`, **with no Google call**. This ordering is the entire point:
the check must precede the Place Details call, or the cost is already incurred.

### D. Fetching all photos
`get_place_photos` caps at 5 and returns URIs. The archive path needs every
available photo and the bytes:

- Raise the cap for this path (Google returns at most ~10 photo references), via
  an explicit argument rather than changing the on-demand default.
- Download each resolved media URI with the existing httpx client, bounded by a
  per-photo timeout and a max byte size, so one pathological image cannot stall
  a whole run.
- Preserve `authorAttributions` alongside each image (Google requires
  attribution) by writing a small `_manifest.json` per venue partition holding
  the photo id, author attributions, source URI, and content type. Losing
  attribution while keeping the image would make the archive unusable.

### E. Storage
New `app/dao/media_archive_store.py` (DAO boundary, mirroring
`datalake_writer`): `put_image(prefix, venue_id, photo_id, data, content_type)`
and `exists_for_venue(prefix, venue_id)`. It builds its boto3 client with
`_build_s3_client` from `app/dao/datalake_writer.py`, so it inherits the default
credential chain (EC2 instance role) and the bounded timeouts, and needs no
static keys.

Unlike the raw lake writer this path is **synchronous and blocking within the
job** — the job is the unit of work, and a photo that fails to store must be
counted, not silently queued. Failures never propagate out of the job runner.

### F. Terraform / IAM
`infra/datalake/iam.tf` extends the writer policy:

- `s3:PutObject` on `media/*` in addition to `raw/*`.
- `s3:ListBucket` on the bucket, **conditioned to the `media/*` prefix**, which
  the skip check and `append_latest` both require.

`s3:GetObject` is deliberately **not** granted: the pipeline must be able to see
*that* objects exist and to add new ones, but never to read archived content
back. The append-only property of the lake is preserved.

### G. Job wiring
A `venue_photo_archive` entry in `JOB_REGISTRY` with `service_attr` set so it
reports `available: false` when Google Places or the bucket is unconfigured,
and a `default_config` that documents every option — that dict is what the
vibes_bot modal renders.

## Data, Config, And API Impact
- **API:** none. `POST /admin/trigger/{job_name}` already accepts an optional
  config body; this adds a job name and a config shape, not an endpoint.
- **Persistence:** no RDS schema change, no migration, no Redis key change.
  Writes are S3-only, to a prefix nothing currently reads.
- **New settings:** `media_archive_enabled` (bool, default false),
  `media_archive_bucket` (defaults to `datalake_bucket` when empty),
  `media_archive_max_photos_per_venue` (int, 10),
  `media_archive_photo_timeout_seconds` (float, 15),
  `media_archive_max_photo_bytes` (int, 10 MiB).
- **Infrastructure:** the IAM policy change above must be applied **before** the
  job is enabled, or every put is denied.
- **Job config contract** (the cross-repo boundary — vibes_bot's modal must
  produce exactly this):

```json
{
  "sources": ["google_photos"],
  "venue_ids": "ven_abc, ven_def",
  "path_mode": "new_day | append_latest | override",
  "path_override": "media/manual/backfill-2026-07/",
  "overwrite": false
}
```

## Error Handling And Observability
Per-venue failures are isolated: no single venue can abort a run. Each is logged
with the venue id and reason, counted, and included in the summary.

| Failure | Behavior | Counted as |
|---|---|---|
| Venue has no `google_place_id` | skip, continue | `no_place_id` |
| Google Place Details / media call fails | skip venue, continue | `google_error` |
| Photo download fails, times out, or exceeds the byte cap | skip that photo, keep the rest | `download_error` |
| S3 put fails | skip that photo, keep the rest | `store_error` |
| Already archived and `overwrite` false | skip venue, no Google call | `skipped_existing` |
| Invalid `path_override` | reject the whole run before any spend | run rejected |

New metrics in `app/metrics.py`:

```
media_archive_runs_total{source,status}              status: success | error
media_archive_venues_total{source,result}            result: archived, skipped_existing,
                                                     no_place_id, google_error, failed
media_archive_photos_stored_total{source}
media_archive_photo_failures_total{source,reason}    reason: download_error, store_error, too_large
media_archive_bytes_stored_total{source}
media_archive_run_duration_seconds{source}
media_archive_last_success_timestamp
```

`media_archive_photos_stored_total` is the cost proxy: Google bills per photo
request, so this counter multiplied by the per-request price is the run's
Google spend, visible in Grafana without reading logs.

## Test Plan
Feature file: `tests/bdd/enrichment/venue-photo-archive.feature`

Scenarios:
- Archive every available photo for a venue into the dated media prefix, keyed
  by source, day, and venue.
- Store the author attributions alongside the images so the archive stays
  usable.
- Default to today's day partition when the path mode is `new_day`.
- Append to the most recent existing day when the path mode is `append_latest`.
- Fall back to a new day when `append_latest` finds no existing partition.
- Write to an explicit prefix when the path mode is `override`.
- Reject a run whose override prefix escapes the media prefix, before any
  Google call is made.
- Skip a venue already archived in the target partition without calling Google.
- Re-download an already-archived venue when overwrite is requested.
- Restrict a run to the venues named in a comma-separated id list.
- Report unknown venue ids in the summary instead of failing the run.
- Continue the run when one venue's Google fetch fails.
- Continue the run when one photo download fails, keeping the venue's other
  photos.
- Report a summary of venues considered, skipped, archived, failed, and photos
  stored.
- Emit the archival metrics a run's cost is judged by.

Pytest unit tests:
- `tests/test_media_archive_paths.py` — path-mode resolution for all three
  modes, the `append_latest` empty fallback, lexicographic day selection, and
  override validation (empty, traversal, outside `media/`).
- `tests/test_media_archive_store.py` — object key format, `exists_for_venue`
  short-circuit semantics, boto3 client built with no explicit credentials, and
  S3 errors surfaced as counted failures rather than raised.
- `tests/test_venue_photo_archive_service.py` — venue-id parsing (trimming,
  dedup, empty), skip-before-Google ordering (the cost guarantee: assert the
  Google client is never called for a skipped venue), per-venue and per-photo
  failure isolation, byte-cap enforcement, and summary accounting.

Manual or integration checks:
- Apply the Terraform IAM change and confirm with the policy simulator that the
  role can `PutObject` under `media/*` and `ListBucket` for that prefix, and
  still cannot `GetObject`.
- Trigger a real run restricted to two venue ids; confirm the objects, the
  manifest, and the metrics; re-trigger and confirm both venues are skipped with
  no Google calls.

## Acceptance Criteria
- The job appears in `/admin/jobs` with its `default_config` and correct
  availability.
- A run stores every available Google photo per venue under
  `media/source=google_photos/dt=<YYYY-MM-DD>/venue_id=<id>/`.
- All three path modes resolve as specified, and an invalid override is rejected
  before any Google call.
- A second run over the same venues and prefix makes **zero** Google calls and
  reports every venue as `skipped_existing`.
- `overwrite: true` re-downloads and re-stores.
- A comma-separated `venue_ids` list restricts the run; unknown ids are reported,
  not fatal.
- One venue's failure never aborts the run.
- The role can write `media/*` and list it, and still cannot read it back.
- All `media_archive_*` metrics are exposed on `GET /metrics`.
- `make test-bdd` and `make test-unit` pass, and the `@wip` tag is removed.

## Open Questions
- None. Bucket (data lake, `media/` prefix), photo count (all available, ~10 by
  Google's cap), and skip-unless-overwrite semantics are decided.

## Note recorded during planning
Google Maps Platform terms restrict retaining Places content: place ids may be
cached indefinitely, but photo content generally may not be copied into
third-party storage long term. This was raised with the operator before planning
and the pipeline was requested regardless; storing `authorAttributions` with each
image (§D) keeps the attribution requirement satisfiable. Flagged here so the
decision is on the record, not fixed by this plan.
