# Capture the Instagram profile photo at add time

## Branch
feature/add-time-profile-photo

## Goal
Retrofit PR #215's already-implemented add-time profile-photo capture
(`VenueProfilePhotoService.capture_for_venue`, wired from
`AddVenueHandler._capture_profile_photo` / `_finalize_created_venue` /
`_geo_fallback`) through this repo's mandated plan+BDD lifecycle, and fix four
real defects an adversarial review found in that implementation before it
ships. This plan supersedes the freeform doc at
`plans/260822_add-time-profile-photo.md` (removed from the feature branch by
this same effort — a plan file lives on `main`, never duplicated on the
branch).

## Non-goals
- No change to the scheduled backfill job's (`run()`/`select()`) own gates,
  cap, or negative-cache semantics — only the add-time path
  (`capture_for_venue`) and its handler wiring change.
- No change to the `edge_color` backfill mode.
- No rotation of the `cs-server/.env` committed secret (tracked separately,
  per `CLAUDE.md`'s security notes).
- No deploy. The feature stays flagged (`add_venue_profile_photo_enabled`,
  default on) and held pending operator sign-off, matching the original PR's
  own "NOT deployed" status note.

## Evidence
- `app/services/venue_profile_photo_service.py`:
  - `run()` calls `self._note_attempt(...)` after every `_process_venue` call
    (line ~755); `capture_for_venue` never calls `_note_attempt` at all (ends
    at line ~869 with a bare `return done(outcome)`).
  - `_process_venue` (line ~872) awaits `self.media_store.put_profile_photo`
    (an `asyncio.to_thread`-wrapped boto3 call — `app/dao/venue_media_store.py`
    line 128) and only then calls `self._persist(...)`. Both are reachable
    while wrapped in the handler's blanket `asyncio.wait_for`.
  - `select()` (line 434) reads `self.rds_store.list_servable_venue_ids()`
    (the `serving.eligible_venue` gate) before iterating; `capture_for_venue`
    has no equivalent read.
  - `capture_for_venue`'s handle fallback (line ~828) calls
    `self.rds_store.list_instagram_handles()` — a full scan of
    `instagram.handle` — for a single venue.
  - Metrics: `_process_venue` increments `VENUE_PROFILE_PHOTO_APIFY_CALLS_TOTAL`,
    `VENUE_PROFILE_PHOTO_ESTIMATED_COST_USD`, `VENUE_PROFILE_PHOTO_BYTES_STORED_TOTAL`
    unconditionally; `capture_for_venue`'s `done()` closure increments
    `VENUE_PROFILE_PHOTO_VENUES_TOTAL` — the same family `run()` uses, with no
    way to separate the two sources.
- `app/handlers/add_venue_handler.py`:
  - `_capture_profile_photo` (line ~1386) wraps the *entire*
    `capture_for_venue(...)` call in `asyncio.wait_for(...,
    timeout=settings.add_venue_profile_photo_deadline_seconds)`.
  - `_finalize_created_venue` (line ~448) is the one call site of
    `_capture_profile_photo`.
  - `_geo_fallback`'s `was_new` branch (lines ~805-863) calls
    `_discover_instagram_handle(venue)` but never `_capture_profile_photo`,
    even though the service's own docstring justifies the handle-fallback
    lookup by exactly this path (a geo-linked venue that already carries a
    handle).
- `tests/test_venue_profile_photo.py::test_add_time_capture_records_the_edge_colour_like_the_job_does`
  (line ~1780) asserts only `"edge_color" in payload`, which is true whether
  the sampled value is a real hex string or `None`.
- `tests/test_add_venue_handler.py` has zero references to
  `venue_profile_photo_service` or `_capture_profile_photo` — no handler-level
  coverage of the add-time capture boundary at all.
- `app/dao/rds_venue_store.py:1435` (`get_enrichment`) is a single-row read,
  already used by `VenueRepository._rds_enrichment`; `app/services/
  batch_add_service.py`'s `_save()` (line 112) shows the repo's existing
  idiom for taking a blocking SQLAlchemy call off the event loop
  (`loop.run_in_executor(None, self._persist, job)`).

## Current Behavior
- An add-time capture failure (`no_pic`/`fetch_failed`/`download_failed`/
  `upload_failed`) leaves no `instagram.profile_photo_attempt` row, so the
  same venue is re-billed by the next add of that venue AND by the next
  24h backfill sweep.
- A slow upload can be cancelled by the handler's deadline after the S3 PUT
  has already started: `asyncio.to_thread` cannot stop the underlying OS
  thread, so the object can finish uploading with no RDS row ever written for
  it (an orphaned, unbilled-for-tracking-purposes S3 object) and no attempt
  row either, so the venue looks "never attempted" and can be re-bought.
- A venue with a blocked type or a hard-blocked name keyword still gets an
  Apify-billed photo capture at add time, even though the scheduled job would
  never buy one for it and no user will ever see it (it is excluded from
  `serving.eligible_venue`).
- A geo-linked add (`matched_via_geo_fallback`, `newly_linked: true`) never
  captures a photo at all, regardless of whether the venue already carries a
  handle.
- `VENUE_PROFILE_PHOTO_VENUES_TOTAL`, `..._APIFY_CALLS_TOTAL`,
  `..._ESTIMATED_COST_USD`, and `..._BYTES_STORED_TOTAL` conflate add-time and
  backfill-job volume with no way to separate them; add-time volume is
  uncapped (the per-run cap deliberately does not apply to it), so a
  dashboard built against these assuming `max_venues_per_run` silently
  includes it.
- The edge-colour regression test cannot fail on a missing/`None` colour.
- `AddVenueHandler._capture_profile_photo`'s `except Exception` branch (the
  guarantee that a photo-capture failure never fails the add) has no test
  coverage at all.
- The handle fallback does a full `instagram.handle` table scan per venue,
  including inside `BatchAddService`'s per-row loop, on the synchronous
  RDS engine (no `run_in_executor`), unlike the batch loop's own per-row hot
  path (`_save`) which already takes blocking RDS calls off the event loop.

## Desired Behavior
- Every add-time capture attempt that produces no photo (including a
  fetch/download timeout) writes or refreshes an
  `instagram.profile_photo_attempt` row, exactly like the job does, so a
  repeat add or the next backfill tick is suppressed by the same negative
  cache.
- The add-time deadline bounds only the two genuinely cancellable I/O calls
  (the Apify scrape, the image download — both real `httpx.AsyncClient`
  requests). Once bytes are downloaded, content-type/size validation, the S3
  upload, and the RDS persist run to completion unconditionally — never
  wrapped in a cancellable `wait_for` — so an add-time capture can no longer
  leave an S3 object with no RDS row.
- `capture_for_venue` mirrors `select()`'s eligibility gate: a venue outside
  `serving.eligible_venue` is skipped before any billed call, with its own
  outcome label.
- The geo-fallback's `was_new` branch captures a profile photo for the newly
  linked venue exactly like `_finalize_created_venue` does, and reports it in
  the response body.
- Add-time and backfill-job spend are separately queryable on the shared
  counters via a `source` label (`add_time` | `backfill`), so a dashboard can
  choose to include or exclude add-time volume explicitly instead of by
  accident.
- The edge-colour regression test asserts the sampled hex value.
- `AddVenueHandler._capture_profile_photo` has direct test coverage for a
  successful capture, a raising service, and a service that times out
  internally — proving the add always still succeeds with a well-formed
  `profile_photo` body.
- The handle fallback uses a single-row `get_enrichment` lookup off the event
  loop via `run_in_executor`, not a full-table scan on the event loop.

## Implementation Approach
- `venue_profile_photo_service.py`:
  - Split `_process_venue`'s body into a new `_fetch_and_download` coroutine
    (the Apify call + the image download only, raising a small internal
    `_EarlyOutcome` exception to carry a terminal outcome — `no_pic`,
    `fetch_failed`, `download_failed` — out of an enclosing `wait_for` without
    being confused with a real `asyncio.TimeoutError`) and the unchanged tail
    (content-type/size validation, hashing, unchanged-short-circuit,
    upload, persist).
  - `_process_venue` gains an optional `deadline_seconds` kwarg. When given,
    only `_fetch_and_download` is wrapped in `asyncio.wait_for`; a timeout
    there returns a new `OUTCOME_TIMEOUT`, added to `ATTEMPT_RECORDED_OUTCOMES`
    (Apify may already have been billed by the time the download step times
    out, and even when it was not, negative-caching a timeout is strictly
    safer than re-buying it on the very next add). `run()` never passes a
    deadline, so job-mode behavior is unchanged (verified by the existing 40+
    job-level tests staying green untouched).
  - `_process_venue` and `_fetch_and_download` also gain a `source` kwarg
    (`"backfill"` default, `"add_time"` from `capture_for_venue`), threaded
    into the three previously-unlabeled counters as a new `source` label.
  - `capture_for_venue`:
    - Adds the eligibility gate (`self.rds_store.is_venue_servable(venue_id)`,
      a new single-row method — see below) right after the existing config
      gates, before the handle fallback or any per-venue read, mirroring
      `select()`'s ordering. A new `OUTCOME_SKIPPED_INELIGIBLE` outcome; not
      added to `ATTEMPT_RECORDED_OUTCOMES` (nothing was billed, and the venue
      must be picked up immediately if its type/name is later unblocked).
    - The handle fallback becomes a single-row
      `get_enrichment("instagram.handle", venue_id)` read via
      `loop.run_in_executor(None, ...)` instead of
      `list_instagram_handles()`.
    - After `_process_venue` returns, calls `self._note_attempt(venue_id,
      handle, outcome, had_attempt=attempt is not None)` — the exact call
      `run()` already makes — before returning.
    - Passes `deadline_seconds` (new parameter, forwarded from the handler)
      and `source="add_time"` into `_process_venue`.
  - `VENUE_PROFILE_PHOTO_VENUES_TOTAL`'s `record()`/`done()` closures in
    `run()` and `capture_for_venue` pass `source="backfill"` /
    `source="add_time"` respectively (the edge-colour loop, which is
    job-only, also passes `source="backfill"`).
- `app/dao/rds_venue_store.py` / `tests/rds_fake.py`: add
  `is_venue_servable(venue_id) -> bool`. Real store:
  `SELECT 1 FROM serving.eligible_venue WHERE venue_id = :v LIMIT 1` (a plain
  view, so this pushes down to an indexed point lookup rather than the O(catalog)
  read `list_servable_venue_ids()` would cost per add). Fake store: reuses
  `list_servable_venue_ids()` (already the single source of eligibility truth
  for tests) and checks membership, to avoid a second, drifting copy of the
  eligibility predicate in the fake.
- `app/handlers/add_venue_handler.py`:
  - `_capture_profile_photo` no longer wraps `capture_for_venue(...)` in its
    own `asyncio.wait_for`. It passes
    `deadline_seconds=settings.add_venue_profile_photo_deadline_seconds`
    straight through to the service call and awaits it directly; the
    `except Exception` fallback (unconfigured service / cascade bug) stays,
    unchanged in shape.
  - `_geo_fallback`'s `was_new` branch calls
    `profile_photo = await self._capture_profile_photo(venue,
    instagram.get("handle"))` right after `_discover_instagram_handle`, and
    the response body gains `body["profile_photo"] = profile_photo` alongside
    the existing `body["instagram"] = instagram`, both only under
    `newly_linked: true` (additive field — see API impact below).
- `app/metrics.py`: add a `source` labelname to `VENUE_PROFILE_PHOTO_VENUES_TOTAL`,
  `VENUE_PROFILE_PHOTO_APIFY_CALLS_TOTAL`, `VENUE_PROFILE_PHOTO_ESTIMATED_COST_USD`,
  `VENUE_PROFILE_PHOTO_BYTES_STORED_TOTAL`; update `ADD_VENUE_PROFILE_PHOTO_TOTAL`'s
  docstring so its "separate from venue_profile_photo_venues_total" claim is
  literally true (it now reads that counter's `source="add_time"` series plus
  handler-level outcomes — `skipped`, `error` — the service-level counter
  never sees).

## Data, Config, And API Impact
- `POST /admin/venues/by-address` (geo-fallback, `newly_linked: true` only):
  response body gains a `profile_photo` field, shaped identically to the
  field `_finalize_created_venue` already returns on the `created` /
  `created_recovered_timeout` / `created_google_only` paths. Additive only —
  no field is removed or renamed (mobile/vibes_bot are second consumers of
  this API; see the wrapper's `feedback_released-clients-are-a-second-api-
  consumer` note).
- Prometheus: `venue_profile_photo_venues_total`,
  `venue_profile_photo_apify_calls_total`, `venue_profile_photo_estimated_cost_usd`,
  and `venue_profile_photo_bytes_stored_total` gain a `source` label
  (`add_time` | `backfill`). Any existing dashboard/alert querying these
  WITHOUT a `source` selector keeps working (Prometheus sums across label
  values by default), but a query that assumed the old, job-only meaning of
  `venue_profile_photo_venues_total` and relied on it staying bounded by
  `max_venues_per_run` should add `{source="backfill"}` — flagged here since
  it is an operator-visible interpretation change, not a breaking query
  change.
- No RDS schema change. No new settings.

## Error Handling And Observability
- New `OUTCOME_TIMEOUT` and `OUTCOME_SKIPPED_INELIGIBLE` labels join the
  service's closed outcome set (`VENUE_PROFILE_PHOTO_VENUES_TOTAL{outcome=...}`
  / `ADD_VENUE_PROFILE_PHOTO_TOTAL{result=...}`), both add-time-only (never
  emitted by `run()`).
- Deliberate tradeoff, stated plainly: removing the handler's outer
  `asyncio.wait_for` means the upload+persist tail is no longer bounded by
  `add_venue_profile_photo_deadline_seconds`. In the pathological case of an
  S3/RDS outage, an add response can take longer than that deadline — bounded
  instead by botocore's own client-side connect/read timeouts (default 60s
  each) and the RDS engine's own connection behavior, both of which already
  raise (never hang forever), landing in the existing `OUTCOME_UPLOAD_FAILED`
  path with its now-guaranteed attempt-row write. This is the one honest way
  to close the orphan-object defect: a timeout that is still allowed to
  cancel the upload is a timeout that can still orphan an object.
- `_note_attempt`'s existing try/except (bookkeeping degrades to "re-scrape
  next window" on a write failure, never to a failed add/run) is unchanged
  and now also guards the add-time call site.
- No new background/orphaned task is introduced anywhere in this change: the
  add-time capture stays one linear coroutine end to end (no `asyncio.shield`,
  no fire-and-forget) — only which of its steps sit inside a `wait_for`
  changes.

## Test Plan
Feature file: `tests/bdd/api/add-time-profile-photo.feature`

(Domain: `api`, not `enrichment` — this feature's observable contract is the
`POST /admin/venues/by-address` response shape and the geo-fallback body,
exactly like its sibling `tests/bdd/api/add-venue-instagram-discovery.feature`
which covers the analogous add-time Instagram-handle-discovery inline call.
Internal service mechanics — the negative-cache table shape, the freshness/
handle-mismatch gates, the edge-colour sampler — stay covered by
`tests/enrichment` — no, by `tests/test_venue_profile_photo.py`'s existing
job-level unit suite, which this change does not touch.)

Scenarios:
- Capture and store the profile photo when a venue is added — happy path.
- Record a negative-cache attempt when add-time capture finds no picture
  (defect 1) — asserts an `instagram.profile_photo_attempt` row exists after
  a `no_pic` outcome.
- A slow media upload never times out or orphans the photo (defect 2,
  structural fix) — upload slower than the deadline, fetch/download fast;
  asserts `profile_photo.status` is `stored`, not `timeout`, and the photo is
  persisted.
- Create the venue when photo capture exceeds its deadline during the Apify
  fetch (defect 2, the deadline still bounds the risky calls) — asserts
  `profile_photo.status` is `timeout` and the add still succeeds (201).
- Skip add-time capture for a venue blocked by the eligibility gate (defect
  3) — a hard-blocked name keyword; asserts Apify is never called.
- Capture the profile photo for a newly geo-linked venue (defect 4) —
  asserts `profile_photo.status` is `stored` on the `matched_via_geo_fallback`
  / `newly_linked: true` body.
- Create the venue when photo capture raises an error — degrade-safe parity
  with the sibling Instagram-discovery feature.
- Create the venue when the profile-photo service is not configured —
  `profile_photo.status` is `skipped`.

Pytest unit tests (`tests/test_venue_profile_photo.py`):
- Fix `test_add_time_capture_records_the_edge_colour_like_the_job_does` to
  assert the sampled hex value (defect 6).
- `capture_for_venue` writes/refreshes an `instagram.profile_photo_attempt`
  row for every `ATTEMPT_RECORDED_OUTCOMES` outcome, including the new
  `timeout` (defect 1).
- The deadline wraps only the fetch/download phase: a slow
  `image_fetcher`/`apify_client` triggers `OUTCOME_TIMEOUT`; a slow
  `media_store.put_profile_photo` does not (defect 2).
- The eligibility gate skips an ineligible venue before any Apify call, and
  the fake `is_venue_servable`/`list_servable_venue_ids` parity holds
  (defect 3).
- The `source` label is `"add_time"` on every counter `capture_for_venue`
  touches and `"backfill"` on every counter `run()` touches (defect 5).
- The handle fallback calls `get_enrichment`, not `list_instagram_handles`
  (defect 8, via a spy/mock on the fake store).

Pytest unit tests (`tests/test_add_venue_handler.py`, new — defect 7):
- `_capture_profile_photo` with a stub service returning a stored outcome —
  well-formed `profile_photo` body.
- `_capture_profile_photo` with a stub service that raises — degrades to
  `status: "error"`, never propagates.
- `_capture_profile_photo` with a stub service that internally times out
  (mirrors the service's own `wait_for`) — degrades to `status: "timeout"`,
  never blocks past the stub's own bound.
- `_geo_fallback`'s `was_new` branch calls `_capture_profile_photo` and
  reports it in the body.

Manual or integration checks: None (feature stays flagged off from prod
traffic pending operator sign-off, per the original PR's status note).

## Acceptance Criteria
- Every add-time capture outcome in `ATTEMPT_RECORDED_OUTCOMES` (including
  the new `timeout`) writes an `instagram.profile_photo_attempt` row, proven
  by a test that fails when `_note_attempt` is not called from
  `capture_for_venue`.
- No add-time code path can await `media_store.put_profile_photo` inside a
  cancellable `asyncio.wait_for`, proven by a test that fails when the
  deadline is (re-)widened to cover the upload.
- A venue outside `serving.eligible_venue` is never scraped at add time,
  proven by a test that fails when the eligibility gate is removed.
- A newly geo-linked venue (`matched_via_geo_fallback`, `newly_linked: true`)
  gets a `profile_photo` capture attempt, proven by a test that fails when
  the call is removed from `_geo_fallback`.
- `venue_profile_photo_venues_total`, `..._apify_calls_total`,
  `..._estimated_cost_usd`, and `..._bytes_stored_total` carry a `source`
  label distinguishing add-time from backfill volume.
- `test_add_time_capture_records_the_edge_colour_like_the_job_does` fails
  when `edge_color` is `None`.
- `AddVenueHandler._capture_profile_photo` has direct test coverage for
  success, a raising service, and an internally-timing-out service.
- Full suite green: `pytest tests/ -q`.

## Open Questions
None.
