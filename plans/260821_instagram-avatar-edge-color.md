# Instagram Avatar Edge Colour (`edge_color`)

## Branch
feature/instagram-avatar-edge-color

## Goal
Record, for every archived Instagram profile photo, the dominant colour along
the strips a `contain` fit would letterbox, and project it to Redis as a
nullable `edge_color` hex string so vibes_bot can serve it and mobile can paint
the leftover strip in the avatar's own colour instead of a grey bar.

Backfill the photo rows that already exist (≈556 at time of writing) **without
spending a single Apify unit and without an S3 read grant**, by re-fetching each
object over the public CloudFront URL already stored on its row.

## Non-goals
- No change to the cross-repo contract that already ships: the Redis key
  `venue_profile_photo_v1:{venue_id}` (no TTL), the S3 key layout
  `venue-profile-photos/<venue_id>/<sha256[:16]>.jpg`, the RDS facet
  `instagram.profile_photo`, or migration `0043`. `edge_color` is **additive**
  to the existing payload.
- No re-scrape. Nothing in this change may call Apify.
- No IAM change, no `infra/media` change, no `infra/datalake` change, and no
  edit to any existing `aws_iam_policy.description` (the field is immutable in
  AWS; editing it forces destroy-and-recreate of the policy and its attachment).
- No change to the venue **detail** photo carousel (`venue_photos_v1`,
  `venue_photos_fresh_v1`, `PhotoEnrichmentService`).
- No change to the scheduled job's default behaviour or its spend.
- No image re-encoding, no new S3 object, no CDN invalidation.

## Evidence
- `app/models/instagram.py:101` — `VenueInstagramProfilePhoto`: `venue_id`,
  `instagram_handle`, `photo_url`, `s3_key`, `content_hash`, `content_type`,
  `byte_size`, `fetched_at`. The facet's `payload` column is `jsonb`
  (`app/dao/venue_repository.py:375` writes `_json(photo)`), so a new optional
  model field needs **no Alembic migration**.
- `app/services/redis_projection_service.py:81` — `_REBUILD_MODELS` maps
  `instagram.profile_photo` → `(VenueInstagramProfilePhoto,
  set_venue_profile_photo, delete_venue_profile_photo)`. The projector
  re-serializes the whole model, so a new field reaches Redis on the next cycle
  with no projector change.
- `app/services/venue_profile_photo_service.py:719` — the downloaded bytes
  (`data`) are already in memory at hash time, immediately before `_persist`.
  That is where a new photo's colour is sampled: zero extra network calls.
- Same file, `:725` — the unchanged-hash short-circuit re-persists the row
  without re-uploading. It also holds `data`, so it can fill a missing
  `edge_color` for free.
- Same file, `:857` — `_persist` is the single writer of the photo row.
- Same file, `:363` — `select()` is the ONLY place that decides who costs
  money, shared by `run()` and `estimate()`. A new mode must go through it.
- `app/services/venue_profile_photo_service.py:305` — `download_image()` already
  streams with a byte cap and a timeout; reusable verbatim for a CDN fetch.
- `infra/media/main.tf:235` — the bucket policy grants `s3:GetObject` to the
  **CloudFront service principal**, conditioned on the distribution ARN. It is
  not a grant to cs-server.
- `infra/media/main.tf:279` — `aws_iam_policy.media_profile_photo_writer` grants
  cs-server `s3:PutObject` on `venue-profile-photos/*` and **nothing else** — no
  `GetObject`, no `ListBucket`. An S3 read-back of the 556 archived photos is
  therefore impossible without a Terraform change, and is not planned.
- `infra/media/main.tf:175-205` — the distribution has `aliases =
  [var.public_hostname]`, `viewer_protocol_policy = "redirect-to-https"`, no
  `trusted_key_groups`, no `trusted_signers`, no `web_acl_id`. The objects are
  anonymously readable over HTTPS at `media_cdn_base_url` — which is exactly how
  every installed mobile client already renders them. **This is the backfill
  route.**
- `app/config.py:458,467,495-503` — `media_cdn_base_url`,
  `instagram_profile_photo_enabled`, `..._retry_days`,
  `..._max_venues_per_run` (200), `..._max_bytes` (5 MiB),
  `..._download_timeout_seconds` (15.0), `..._interval_hours` (24).
- `main.py:368` — the APScheduler job passes `MODE_BACKFILL` explicitly, so a
  cron can never widen the spend. A new mode must not change that line's
  behaviour.
- `app/routers/admin_trigger_router.py:221,613` — the admin trigger registry
  entry and the `POST /admin/trigger/instagram_profile_photos/estimate` route.
- Pillow 10.2.0 is importable in the environment but is **not** declared in
  `requirements.txt`. It is therefore a transitive/ambient dependency and must
  be pinned explicitly by this change (see Data, Config, And API Impact).

## Current Behavior
`VenueProfilePhotoService` scrapes a venue's Instagram profile through Apify,
downloads the picture, content-addresses it, uploads it to the media bucket, and
persists `{venue_id, instagram_handle, photo_url, s3_key, content_hash,
content_type, byte_size, fetched_at}` to `instagram.profile_photo`. The projector
mirrors that row verbatim to `venue_profile_photo_v1:{venue_id}`.

Nothing anywhere records what the image looks like at its edges. vibes_bot serves
only `photo_url`, and mobile cannot sample a remote image's pixels — so a
`contain` fit has nothing to paint the letterbox with except a theme grey.

## Desired Behavior
1. **On store.** Every row `_persist` writes must carry `edge_color` — an
   uppercase `#RRGGBB` string, or `null` when the bytes could not be sampled.
   A sampling failure is an **absence, never a failure**: the photo is still
   stored, still projected, still served. This follows the module's existing
   "what is never a failure" rule.
2. **On the unchanged-hash short-circuit.** The re-persist must fill
   `edge_color` when the stored row lacks one, using the bytes it already holds.
3. **Backfill.** A new manual mode must fill `edge_color` on every existing row
   that lacks one, by fetching the row's own `photo_url` over HTTPS. It must
   make **zero** Apify calls, **zero** S3 API calls, and must preserve every
   other field of the row byte-for-byte.
4. **Projection.** The next projector cycle must carry `edge_color` into
   `venue_profile_photo_v1:{venue_id}`. A row without a colour must still
   project the key, with `edge_color` absent/null — never a missing key.

## Implementation Approach

### 1. The sampler — `app/services/image_edge_color.py` (new, pure)
One public function, `sample_edge_color(data: bytes) -> Optional[str]`.

- Decode with Pillow; take the first frame; convert to `RGBA` then composite
  over opaque white before converting to `RGB`. Instagram avatars are displayed
  as circles over a light chrome, so a transparent border must resolve to white
  rather than to whatever garbage sits in the alpha-zero channels.
- Sample the **left and right border strips** — the strips a `contain` fit
  letterboxes for a square source in a box wider than it is tall. Strip width is
  `max(1, round(width * 0.02))` so a single anti-aliased column cannot decide
  the colour on its own. The vertical extent is the full height.
  - cs-server deliberately does **not** encode mobile's exact box ratio. It
    encodes only the stable property "the list thumbnail is wider than it is
    tall", which is what makes the vertical edges the letterboxed ones.
  - Confirmed against the FINAL approved mobile geometry ("3c, recuo 6, raio
    10", mobile plan `plans/260821_instagram-avatar-edge-color.md`): the box is
    **95 x 84**. A 1:1 avatar under `contain` scales to 84 x 84, leaving 5.5dp
    strips on the **left and right**. The vertical-edge rule holds. It also held
    for the superseded 101 x 96 geometry, which is the point of not hard-coding
    the ratio.
- Quantise each sampled pixel to a coarse bucket (each channel rounded to the
  nearest 8) and take the **modal** bucket; return the arithmetic mean of the
  pixels in that bucket, formatted `#RRGGBB` uppercase. Modal-then-mean is what
  makes the result both stable against JPEG ringing and exact on a flat border
  (a pure-white border must return `#FFFFFF`, not `#FEFDFF`).
- Deterministic: identical bytes must always yield an identical string. No
  randomness, no sampling stride that depends on image size beyond the rule
  above.
- Returns `None` — never raises — on any decode error, a zero-dimension image,
  or an unsupported mode.

### 2. The model — `app/models/instagram.py`
Add `edge_color: Optional[str] = None` to `VenueInstagramProfilePhoto`, with a
docstring paragraph stating it is the dominant colour of the letterboxed strips,
that `None` is normal and means "not sampled", and that consumers must fall back
to their own neutral rather than treating absence as an error.

`VenueInstagramProfilePhotoAttempt` is untouched — it is the negative cache, is
in no `_REBUILD_MODELS` entry, and must never gain a serving field.

### 3. The store path — `venue_profile_photo_service.py`
- `_persist` grows an `edge_color` parameter and writes it onto the model.
- In `_process_venue`, sample immediately after `content_hash` is computed, from
  the same `data`. Wrap the call so a sampler defect can never abort a run that
  already paid for the scrape.
- The unchanged branch passes the freshly sampled colour, so an existing row
  missing a colour gains one the next time its venue is legitimately re-scraped.
- **Ordering guarantee preserved:** sampling happens after the download, so it
  cannot move `venue_profile_photo_apify_calls_total` and cannot change which
  venues `select()` chooses.

### 4. The backfill mode
Add `MODE_BACKFILL_EDGE_COLOR = "edge_color"` to `MODES`.

- `select(mode=MODE_BACKFILL_EDGE_COLOR)` chooses venues whose
  `instagram.profile_photo` row **exists** and whose payload has no non-empty
  `edge_color`. It reads the same bulk RDS rows the other modes read; no handle
  is required (the row already holds its own URL), and the negative-cache gate
  does not apply (no scrape is bought, so there is nothing to suppress). Every
  gate stays inside `select()` so `estimate()` still prices the mode without a
  single `await`.
- Processing one venue: read `photo_url` from the row → `download_image(url)`
  with the existing byte cap and timeout → `sample_edge_color(data)` → re-persist
  via `_persist` with **`instagram_handle`, `photo_url`, `s3_key`,
  `content_hash`, `content_type`, `byte_size` and `fetched_at` all carried over
  verbatim** from the stored row, and only `edge_color` added.
  - Preserving `content_hash` and `instagram_handle` is load-bearing: they are
    what `_has_current_photo` and the unchanged-hash short-circuit read. A
    rewritten hash or a dropped handle would make the next scheduled backfill
    re-buy an Apify scrape for all 556 venues.
  - Preserving `fetched_at` keeps the row honest about when the *photo* was
    captured. The backfill did not fetch a photo; it read one we already had.
- **The Apify client is not referenced on this code path at all**, and
  `media_store.put_profile_photo` is never called. Those two absences are the
  cost guarantee, and the BDD asserts them against the counter, not a log line.
- The mode honours an optional `max_venues` in the trigger config, defaulting to
  `instagram_profile_photo_max_venues_per_run` (200). 556 rows is therefore three
  runs at the default, or one run with the override. Deferred venues are reported
  through the existing `deferred` property.
- Reachable **only** from the admin trigger. `main.py:368` keeps passing
  `MODE_BACKFILL` explicitly and is not edited, so the scheduler can never enter
  this mode.
- Per-venue failure isolation, exactly like the existing run: one unreachable
  URL never aborts the rest. There is no `ApifyCreditExhaustedError` equivalent
  to stop on — nothing is billed.

### 5. Admin trigger
Extend the `instagram_profile_photos` registry entry's mode documentation at
`admin_trigger_router.py:221` to name the third mode and state that it is free.
`InvalidProfilePhotoMode` continues to reject anything else rather than
defaulting — the modes differ by a whole catalog of Apify units.

## Data, Config, And API Impact
- **RDS:** `instagram.profile_photo.payload` gains an optional `edge_color` key.
  The column is `jsonb`; **no Alembic migration, and migration `0043` is not
  touched.** Rows written before this change simply lack the key, which
  deserialises to `None`.
- **Redis:** `venue_profile_photo_v1:{venue_id}` gains an `edge_color` key.
  Additive, no TTL change, no key-name change. Size impact is ~9 bytes/venue —
  ~13 KB across the catalog, against ~20 MB in use on a `maxmemory 0` box.
- **Config:** no new setting. The mode's only knob is the existing per-run cap
  plus the optional per-trigger `max_venues`.
- **Dependencies:** pin `Pillow` in `requirements.txt`. It resolves today only
  because something else dragged it in; an undeclared import is one transitive
  bump away from an ImportError at job start. Version to be pinned at the
  installed 10.2.0 unless the lock resolution says otherwise.
- **API:** no cs-server HTTP contract changes. The admin trigger accepts one
  additional `mode` value.
- **Infra:** none. No IAM change, no Terraform apply, no bucket-policy edit.

## Error Handling And Observability
- A sampler failure yields `edge_color = None`. The photo is stored regardless.
- A backfill fetch failure leaves the row **completely untouched** — not a
  partial write, not a `None` colour written over an absent one — so the next
  run retries it naturally. Nothing distinguishes "we tried and failed" from
  "we have not tried", and that is correct here because retrying costs nothing.
- New outcome labels on the run summary for the edge-colour mode:
  `edge_color_sampled`, `edge_color_skipped_has_color`, `edge_color_no_url`,
  `edge_color_fetch_failed`, `edge_color_decode_failed`. The outcome set stays
  closed — an outcome label that never appears in Prometheus is itself the
  diagnostic that its code path never ran.
- New gauge `venue_profile_photo_edge_color_venues`, set by the projector
  alongside `VENUE_PROFILE_PHOTO_PROJECTED_VENUES`
  (`redis_projection_service.py:233`), counting projected rows that carry a
  colour. Coverage is then readable straight off `/metrics`: a value stuck at 0
  while `venue_profile_photo_projected_venues` is ~556 proves the backfill never
  ran.
- `venue_profile_photo_apify_calls_total` must be **unchanged** across a
  complete edge-colour backfill. That is the cost assertion.

## Test Plan
Feature file: `tests/bdd/enrichment/instagram-avatar-edge-color.feature`

Scenarios:
- A newly stored profile photo records the avatar's edge colour in the row and
  in the projected Redis payload.
- A photo whose bytes cannot be decoded is still stored and still projected, with
  no edge colour — an absence, not a failure.
- The unchanged-hash short-circuit fills a missing edge colour without
  re-uploading the object.
- The edge-colour backfill fills every row that lacks a colour and makes **zero**
  Apify calls — `venue_profile_photo_apify_calls_total` is unchanged.
- The edge-colour backfill uploads nothing: `put_profile_photo` is never called
  and `venue_profile_photo_bytes_stored_total` is unchanged.
- The edge-colour backfill reads each object from the row's stored CloudFront
  `photo_url`, never from S3.
- The edge-colour backfill preserves `instagram_handle`, `photo_url`, `s3_key`,
  `content_hash`, `content_type`, `byte_size` and `fetched_at` verbatim, so the
  next scheduled backfill still reports `skipped_has_photo` for that venue.
- A row that already carries an edge colour is skipped by the backfill.
- A backfill fetch that fails leaves the row untouched and is retried by the next
  run.
- One unreachable URL does not abort the rest of the backfill run.
- The estimate for the edge-colour mode reports the same venue count the run
  processes, and performs no fetch.
- The scheduled job still runs `backfill` and can never enter the edge-colour
  mode; an unknown mode is rejected rather than defaulted.
- The projector writes `venue_profile_photo_v1:{venue_id}` for a row with **no**
  edge colour too — the key is never omitted.

Pytest unit tests:
- `tests/unit/test_image_edge_color.py` — determinism (identical bytes yield an
  identical string across repeated calls); a synthesised avatar with a known flat
  border returns that border's exact hex; the output is uppercase `#RRGGBB`; a
  transparent PNG border composites over white rather than producing a bogus
  colour; garbage bytes, an empty payload and a zero-dimension image all return
  `None` without raising; a noisy/dithered border returns the modal colour rather
  than a muddy average.
- Targeted tests for `select(mode="edge_color")`: only rows lacking a colour are
  chosen, the cap and the `max_venues` override are honoured, and `select()`
  performs no `await` and touches no client.

Manual or integration checks:
- Against prod, read-only first: confirm the count of `instagram.profile_photo`
  rows and how many lack `edge_color` (expected ≈556 lacking).
- `curl -I` one stored `photo_url` from the cs-server host to confirm an
  anonymous HTTPS GET returns 200 before the first backfill run — this is the
  one assumption the whole cost story rests on.
- Run the estimate, then one capped backfill run; verify on `/metrics` that
  `venue_profile_photo_apify_calls_total` did not move and
  `venue_profile_photo_edge_color_venues` rose after the next projector cycle.
- Read one `venue_profile_photo_v1:{venue_id}` from prod Redis and confirm the
  `edge_color` value against the visible avatar (sampled examples: Retrô 17
  `#FFFFFF`, Seu Boteco `#004A9D`, Casa Bacurau `#171717`, Venda Bom Jesus
  `#3154A5`).

## Cost
| Item | Expected cost |
|---|---|
| Apify | **$0.00** — the actor is never invoked on any path in this change |
| S3 API | **$0.00** — no `GetObject` (not granted, not attempted), no `PutObject` |
| CloudFront | **$0.00** — 556 requests / ~17 MB egress against a 1 TB + 10M-request free tier |
| Google Places | **$0.00** — untouched |
| RDS / Redis | negligible — one `jsonb` key, ~13 KB total across the catalog |

**Total expected cost of the backfill: $0.00.** It is cheap enough to re-run,
which is why a failed fetch is simply retried rather than negative-cached.

## Acceptance Criteria
- Every newly stored profile photo carries an `edge_color` (or an explicit
  `null`) in RDS and in `venue_profile_photo_v1:{venue_id}`.
- A complete edge-colour backfill of the existing rows leaves
  `venue_profile_photo_apify_calls_total` and
  `venue_profile_photo_bytes_stored_total` **unchanged**.
- After the backfill and one projector cycle,
  `venue_profile_photo_edge_color_venues` is within one of
  `venue_profile_photo_projected_venues` (the difference being genuinely
  unsamplable images).
- Every backfilled row's `instagram_handle`, `photo_url`, `s3_key`,
  `content_hash`, `content_type`, `byte_size` and `fetched_at` are identical
  before and after.
- The scheduled job's behaviour and spend are unchanged.
- `venue_profile_photo_v1:{venue_id}`, the S3 key layout, the RDS facet name and
  migration `0043` are unchanged.
- No file under `infra/` is modified.

## Open Questions
- None.
