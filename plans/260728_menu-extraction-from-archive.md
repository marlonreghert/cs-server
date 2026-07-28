# Menu extraction reads the archive

## Branch
feature/menu-extraction-from-archive

## Goal

Point menu extraction at the photos the retrieval pipeline already archives —
the newest `searchapi_gmaps_photos` run's `media/menu/` folder — instead of
having a second pipeline fetch its own copies into a separate bucket.

The archive already gets **real Google "Menu" tab photos**, which is exactly
what extraction wants and strictly better than what it fetches today.

## Non-goals

- **Deleting `menu_photos`.** It is the only path that reads **Instagram**
  highlights, which the archive has no equivalent for. It stops being the
  source for extraction; whether it is retired is a separate decision.
- **Changing what extraction produces.** Same `VenueMenuData`, same Redis key,
  same serving DTO.
- **Reading `raw/`.** The BestTime lake stays unreadable by the app.
- **Serving archived images to the app.** `retrieved/` remains internal-use only.

## Evidence

- `app/services/menu_extraction_service.py:68-80` — reads
  `venue_menu_photos_v1:{venue_id}` from Redis, then
  `s3_client.generate_presigned_url(photo.s3_key)` per photo, and hands the URLs
  to GPT-4o. **It never downloads bytes**; OpenAI fetches them, so whatever
  signs the URL must hold `s3:GetObject`.
- `app/api/s3_client.py:32-71` — bound to the **menu** bucket
  (`settings.s3_bucket`) with **static keys** (`s3_access_key_id`), writing
  `places/{venue_id}/photos/menu/{photo_id}.jpg`. A different bucket and a
  different credential from the lake.
- `app/services/menu_photo_enrichment_service.py:1-13` — Instagram highlights
  primary, gmaps extractor fallback, then download + upload + Redis.
- `infra/datalake/iam.tf` — cs-server's instance role is attached **only** to
  `datalake_writer`: `PutObject` on `raw/*`, `media/*`, `retrieved/*` and a
  prefix-scoped `ListBucket`. **No `GetObject`.** `datalake_analytics` grants
  reads but is attached to nothing.
- The archive writes
  `retrieved/source=searchapi_gmaps_photos/year=/month=/day=/run_id=<ulid>/venue_id=<v>/media/menu/<photo_id>.jpg`,
  and `run_id` is a ULID so the newest run is the last key from a listing.
- Prod today: **0 of 28 venues** carry `venue_menu`, so the feature is empty and
  this change risks nothing that currently works.

## Current vs desired behavior

| | Current | Desired |
|---|---|---|
| Photo source | menu bucket, fetched by `menu_photos` | the archive's newest SearchApi run |
| Photo quality | Instagram highlights / gmaps fallback, keyword-filtered | Google's own **Menu** tab |
| Buckets involved | two | one |
| Extraction can run | only after `menu_photos` | after any archive run |
| cs-server can read the lake | **no** | `retrieved/*` only |

## Implementation approach

### 1. IAM — the part that must land first

Add `s3:GetObject` scoped to `retrieved/*` to the writer policy document.

This is a **deliberate narrowing of the append-only posture** and should be
understood as such: the pipeline currently cannot read anything back, and after
this it can read the media archive. `raw/*` — the BestTime lake — stays
unreadable, which is the part that matters most, and the prefix-scoped grant
keeps the blast radius to photos we ourselves wrote.

Edit the policy **document** only. `aws_iam_policy.description` is immutable in
AWS; changing it forces a destroy-and-recreate, and a lake flush inside that
window is dropped rather than retried. Apply with `-target` — `infra/datalake`
has drift that a bare apply would act on (it plans to destroy the S3 VPC
endpoint).

### 2. Store: find and presign the newest run's menu photos

On `MediaArchiveStore`:

- `latest_run_prefix(source)` — reuse `list_run_prefixes`, take the last.
- `list_venue_photos(prefix, venue_id, category)` — keys under
  `venue_id=<v>/media/<category>/`.
- `presign(key, expires_in)` — a presigned GET, so OpenAI can fetch without the
  bucket being public. Short expiry: the URL is handed to a third party.

### 3. A source-agnostic seam in extraction

`extract_menu_for_venue` currently reaches straight into Redis. Introduce a
`photo_source` seam with two implementations:

- `redis_menu_photos` — today's behavior, unchanged, still the fallback.
- `archive_menu_photos` — newest run for the configured source, that venue's
  `media/menu/` keys, presigned.

Config picks the source (`menu_extraction_photo_source`, default
`archive`), so the change is reversible without a deploy and the old path stays
exercised.

A venue with no menu photos in the newest run yields nothing and is counted
`no_photos` — the existing outcome, not a new failure mode.

### 4. Which run, explicitly

Always the **newest** run for the source. Not "any run that has photos": a run
is a snapshot, and mixing runs would silently blend a fresh menu with a stale
one. If the newest run did not cover a venue, that venue has no photos this
cycle — correct, and visible in the counters.

## Data, config, and API impact

- **API:** none. Same `VenueMenuData`, same Redis key, same DTO.
- **Persistence:** no migration. No new Redis keys.
- **New settings:** `menu_extraction_photo_source` (`archive` | `redis`),
  `menu_extraction_archive_source` (default `searchapi_gmaps_photos`),
  `menu_extraction_archive_category` (default `menu`),
  `menu_photo_presign_seconds` (default 900).
- **Infra:** `s3:GetObject` on `retrieved/*` for the writer role.
- **Cost:** strictly lower — extraction stops paying Apify/Instagram for a
  second copy of photos the archive already holds. OpenAI spend is unchanged.

## Error handling and observability

| Failure | Behavior |
|---|---|
| No archive run for the source | counted `no_photos`; nothing extracted |
| Venue absent from the newest run | counted `no_photos` |
| Listing fails | venue skipped, run continues, logged with the venue id |
| Presign fails | that photo skipped; the venue proceeds with the rest |
| GetObject denied (IAM not applied) | fails loudly on the first venue with the policy name in the message, rather than silently extracting nothing |
| OpenAI failure | existing handling, unchanged |

Existing `MENU_*` metrics keep working. Add the photo source as a label so the
two paths are distinguishable in Grafana.

## Test plan

Feature file: `tests/bdd/enrichment/menu-extraction-from-archive.feature`

Scenarios:
- Extraction uses the newest archive run's menu photos for a venue.
- A second, older run is ignored even when it has more photos.
- A venue missing from the newest run is counted as having no photos.
- A source with no runs at all yields no extraction and no error.
- Only the configured category is read — `vibe` photos are never sent to the model.
- Photos are presigned, and the raw bucket URL is never handed to the model.
- A presign failure skips that photo and the venue still extracts.
- Denied read access fails loudly and names the missing permission.
- The Redis path still works when configured, so the change is reversible.
- Extraction output is unchanged: same structure, same Redis key.

Pytest unit tests:
- Newest-run selection, including ULID ordering and a single-run case.
- Key filtering: category, venue, and that a manifest/info key is never treated as a photo.
- Presign expiry is bounded and passed through.
- The source seam: each implementation returns the same shape.
- Counters for every outcome above.

## Acceptance criteria

- Extraction reads the newest archive run and produces the same menu structure
  it does today.
- No venue is extracted from a mix of runs.
- The model only ever receives presigned URLs, and only for the configured
  category.
- The Redis path remains selectable and working.
- cs-server gains read access to `retrieved/*` and **nothing else** — `raw/`
  stays unreadable.
- `menu_photos` is untouched and still runs; only extraction's source changes.

## Open questions

None blocking. One decision recorded: granting `GetObject` on `retrieved/*`
relaxes the append-only posture by design, scoped to the prefix the app itself
writes. If that is unacceptable, the alternative is a separate reader principal
and a presigning sidecar, which is more moving parts for the same result.
