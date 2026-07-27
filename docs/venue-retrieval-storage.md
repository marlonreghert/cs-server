# Venue retrieval & the `retrieved/` archive

How VibeSense pulls venue photos and place data from third parties and stores
them in the S3 data lake. Written for agents (and humans) picking this up cold.

Owning code, in the order a run touches it:

| Concern | File |
|---|---|
| Where photos come from | `app/services/archive_sources.py` |
| Google client | `app/api/google_places_client.py` |
| Apify client | `app/api/apify_gmaps_extractor_client.py` |
| The run itself | `app/services/venue_photo_archive_service.py` |
| S3 layout & writes | `app/dao/media_archive_store.py` |
| Trigger / estimate / stop | `app/routers/admin_trigger_router.py` |
| Operator UI | `vibes_bot/app/admin/static/admin.html` |
| Bucket & IAM | `infra/datalake/` |

---

## 1. The layout

```
retrieved/source=<source>/year=<YYYY>/month=<MM>/day=<DD>/run_id=<ulid>/venue_id=<id>/
    media/<category>/<photo_id>.<ext>
    info/place.json
    info/_manifest.json
retrieved/source=<source>/_latest.json
```

- **`key=value` partitions** (`source=`, `year=`, …) are Hive-style so the lake's
  existing query tooling discovers `retrieved/` the same way it discovers `raw/`.
- **`media/` and `info/` are plain directories, not partitions.** They split
  large binary images from a few KB of JSON so a consumer can read one without
  listing the other. `<category>` is likewise a plain directory.
- **`media/` (the old root) is superseded** but still listable. Nothing writes
  run-scoped data there any more.

### `run_id` is a ULID, and that is load-bearing

The run id is the **only** thing separating two runs on the same day, and
"latest run" is resolved by taking the **last key from a listing** — the writer
role has no `s3:GetObject`, so it can never read a pointer to find out.

A random id (uuid4) sorts randomly: measured over 2,000 trials with three runs
in a day, it picks the wrong "latest" **67.6%** of the time. That silently
breaks `append_latest` *and* the skip-scope cost gate.

So `new_run_id()` is a ULID — 48-bit ms timestamp then 80 bits of randomness,
each half base32-encoded **independently** so they never share a character.
(Interleaving them reintroduces the mis-ordering; an early attempt was wrong 14%
of the time for exactly that reason.) Crockford alphabet, so no `I`/`L`/`O`/`U`
to mistranscribe out of a bucket listing.

**The job id IS the run id.** One time-ordered value in the path, the logs and
the run record.

---

## 2. Sources are a registry, not an if-branch

`ARCHIVE_SOURCES` in `app/services/archive_sources.py`. Each descriptor owns
four things the pipeline must not hard-code:

- `config_schema` — the extra fields it needs (rendered by the admin panel)
- `fetch` — venue → `{"photos": [...], "info": {...}}`
- `estimate_units` / `unit_cost_usd` — **its own cost model**
- `requires_attr` — the client it needs to be usable

`GET /admin/jobs` publishes the catalog, so **adding a source is a registry
entry with no admin-UI change.**

### Current sources

| | `google_photos` | `apify_gmaps_extractor` |
|---|---|---|
| Billing | per **request** | per **place scraped** |
| Calls/venue | **1 Place Details + N photos** | 1 actor run |
| 2,000 venues × 10 photos | ~$154 | ~$12 |
| Licensed | yes | it is a scrape, not a licence |

The `1 +` in Google's model is easy to forget — omitting it understated every
Google run by **33%** in a control whose job is holding a $10/month gate.

### Adding a source

1. Add an `ArchiveSource` to `ARCHIVE_SOURCES`.
2. Give its client a method returning `{"photos", "info"}`.
3. Name the client attribute on `VenuePhotoArchiveService` to match
   `requires_attr`.

That's it — the panel renders the new option and its fields from the catalog.

---

## 3. Cost guarantees (do not rearrange)

Two orderings carry them:

1. **Config is validated and the prefix resolved before any fetch**, so a bad
   request costs nothing.
2. **The already-archived check runs before the fetch**, because both sources
   bill per venue. A skip after the fetch has already spent the money it exists
   to save.

### `skip_scope` — why it exists

Every run writes to its own `run_id=` prefix, so "have I already retrieved this
venue?" **cannot** be asked of the prefix being written to: it is empty by
construction, the answer is always no, and **every run re-buys the whole
catalog.** It is asked of the most recent *existing* partition instead.

- `latest_run` (default) — the previous run
- `this_run` — only what this run wrote (resume)
- `none` — skip nothing; **requires `overwrite`**, so the expensive combination
  is unreachable rather than merely validated

### Other spend controls

- `max_venues`, `max_photos_per_venue` — always applied; defaults small
  (50 × 5 = 250 upper-bound requests, inside Google's free tier)
- `max_photos_per_category` — optional second cap, per category
- `dry_run` — selection + estimate, **zero calls, nothing written**
- **No cron.** Operator-triggered only, so steady-state spend is $0.

---

## 4. Photo categories: what is and isn't possible

**Google tags no individual image with a category.** An image object is
`imageUrl`, `authorName`, `authorUrl`, `uploadedAt` — nothing more.
`imageCategories` is a **place-level list of which tabs exist**
(`["All","Menu","Food & drink","Vibe","By owner",…]`), not a per-image label and
not an input filter. Verified against both compass actors' output schemas.

So the only category derivable for free is **`by_owner` vs `by_visitor`**, by
comparing the uploader to the venue title — which is one of Google's own tabs
and the useful one, since owners upload the official shots.

Photos come back in **Google Maps' display order** (the "All" tab, Google's own
ranking), so a cap of N takes the **top N**, not an arbitrary N.

**Real Google categories** would need SearchApi.io's `google_maps_photos` engine
with `category_id` (present as `app/api/serpapi_client.py`, currently
deprecated), at **one request per category per venue** — cost multiplies by the
number of categories.

Category names reach an S3 key, so they are **untrusted input**: `_safe_category`
collapses traversal (`../../raw` → `raw`) and empties to `uncategorised`.

---

## 5. IAM — the sharp edges

`infra/datalake/iam.tf`. The writer role is **append-only**:

- `s3:PutObject` on `raw/*`, `media/*`, `retrieved/*`
- `s3:ListBucket` limited to those prefixes
- **no `s3:GetObject`** — the pipeline can see that an object exists and add
  new ones, and can never read archived content back

Consequences you must design around:

- **"Latest" is resolved by listing, never by reading a pointer.** `_latest.json`
  is written for analytics roles that *can* read; the pipeline never reads it.
- **A new prefix needs a terraform apply first.** Writing outside the policy
  fails *after* the fetch has been paid for.
- ⚠️ **Never edit `aws_iam_policy.description`.** It is immutable in AWS, so a
  change forces destroy-and-recreate, opening a window where cs-server has no
  `PutObject` — and a data-lake flush in that window is **dropped, not
  retried**, losing BestTime observations that cannot be re-fetched. The policy
  *document* updates in place; only that string is dangerous.
- ⚠️ **`infra/datalake` terraform has drifted**: a bare `terraform apply` plans
  `3 to add, 1 to change, 2 to destroy`, including destroying the S3 VPC gateway
  endpoint ("index [0] is out of range for count" — applied with vars absent
  from the committed config). Use `-target=<resource>` until reconciled.

---

## 6. Operating a run

- **Trigger** returns a `job_id`; every log line carries it; the record is at
  `GET /admin/jobs/runs/{job_id}`.
- **Estimate** (`POST /admin/trigger/venue_photo_archive/estimate`) prices a run
  with **zero** provider calls. Its caveat is shown verbatim — the Apify
  per-image charge is unpublished, so estimates are a **floor**.
- **Stop** (`POST /admin/trigger/venue_photo_archive/stop`) cancels the in-flight
  asyncio task; it halts at the next await, before the next paid request. A
  cancelled run still saves its record (`aborted: true`).
- **Backstop:** the EC2 box is SSM-registered, so
  `docker restart vibes_bot-cs-server-1` kills a run in ~15s. A redeploy takes
  10–12 min.

### Outcome counters

`archived`, `skipped_existing`, `no_place_id`, `no_match`, `info_only`,
`failed`, `credit_exhausted`, `aborted`.

`no_match` (source answered, found nothing) is deliberately **distinct** from
`failed` (the call itself failed) — conflating them makes an unmatched venue
look like an outage. `info_only` means the place data was kept although no
images came back: it is the cheaper half of what was already paid for.

---

## 7. Metrics

`media_archive_*` in `app/metrics.py`, labelled `source` / `result`.

**`job_id` is deliberately NOT a label** — a run id is unbounded cardinality and
would degrade the whole metrics store. Per-run detail lives in the logs
(queryable by `job_id` in Loki) and in the run record. The Grafana dashboard
(`vibes_bot/config/grafana/provisioning/dashboards/photo-archive-dashboard.json`)
reflects that split: Prometheus panels are fleet-wide, only the Loki panels
honour `$job_id`, and a text panel says so.

---

## 8. Compliance

`retrieved/` is **internal-use only**. Images displayed in the app must be
served from Google's CDN — see the BLOCKED section of
`plans/260726_venue-list-hero-photo.md`. The bucket blocks public access and the
writer role is denied `GetObject`, so this is enforced by infra, not convention.

Google's terms permit the licensed API; scraping the same content is a
different posture. This repo already accepted Apify scraping for menu photos, so
the source choice is a consistency decision the operator makes per run — it is
surfaced in the picker, not hidden.
