# Enrichment And Budget Hardening

## Branch
fix/enrichment-and-budget-hardening

## Goal
Google enrichment must re-check business status so permanently closed venues
stop serving and stop consuming BestTime credits; transient Google failures
must never permanently poison venues; Instagram validation must only delete on
definitive non-existence; paid refresh jobs must not run concurrently with
their scheduled twins; the manual-add path must not double-spend or
mis-account the monthly BestTime budget; and photo category tags must reach
the fresh-photos payloads.

## Non-goals
- Accent-folding in `_geo_lookup` (owned by in-flight
  `fix/admin-breakdown-and-addvenue-fold`; this plan rebases after it merges).
- Refresh-selection reserve erosion (wrapper bug assessment cs-server L4,
  PLAUSIBLE — revisit after the ledger behavior is observed in prod).
- Discovery-point counter inflation (L1, dormant while discovery is disabled).
- Projector/persistence fixes (companion plan
  `260710_projection-persistence-integrity.md`).

## Evidence
Wrapper `plans/260710_bug-assessment.md`, cs-server H2, H3, H4, L3, M2, M3,
M4, M6, cross-repo 1.6; re-verified on current `main`:

- `app/services/google_places_enrichment_service.py:409-414`: enriched venues
  are skipped unless `force_refresh`; no scheduled or default-admin caller
  passes it (`main.py:134`; `app/routers/admin_trigger_router.py` job
  defaults), though the job's own docstring names closure detection as a
  purpose. A permanently closed venue is never deprecated and keeps its
  place in the priority refresh selection — paid BestTime live reads every
  cycle, indefinitely.
- Same file `:438-446` (and the pending backfill twin): when
  `search_place_id` returns falsy, a permanent empty `VibeAttributes` marker
  is written. `app/api/google_places_client.py:175-189` returns `None` for
  HTTP errors, timeouts, and quota rejections exactly as for a genuine
  zero-result — a mid-run Places QPM spike (observed in prod 2026-07-05)
  permanently poisons every remaining venue in the loop, with no retry path.
- `google_places_enrichment_service.py:663-687` with `:637-661`: Instagram
  validation deletes the handle on any non-200 (429, 403, login redirect)
  while exceptions are treated as "exists" — a mid-sweep rate-limit
  soft-deletes valid handles en masse; re-discovery costs paid Apify runs.
- `app/routers/admin_trigger_router.py:36,238-256`: `_running_jobs` dedupes
  only admin-triggered tasks; `main.py:375-394` APScheduler `max_instances=1`
  guards only scheduler instances. An admin trigger during a scheduled
  live/weekly refresh doubles the paid BestTime calls for the cycle; the same
  gap lets admin `rebuild_redis` interleave with the scheduled projection
  (transient resurrection of just-removed venues).
- `app/handlers/add_venue_handler.py:570-654` (`undo_geo_link`): eligibility
  is only exists + active + created within 24h — no record that the venue was
  created via geo-fallback (`newly_linked` lives only in the HTTP response),
  and `app/services/venue_budget_service.py:212-229` decrements the *current*
  month. Undoing a normally-created venue (or one straddling month rollover)
  under-reports the ledger; BestTime's real unique-venue counter never goes
  down, so a later add passes our counter and hits the real cap.
- `add_venue_handler.py:122-254`: no concurrency guard around
  reserve → create; `_geo_lookup` reads the Redis geo index while
  `upsert_venue` writes RDS-only, so a just-added venue is invisible to the
  duplicate check for up to one projection interval; double-submit reserves
  two slots and issues two paid creates for one venue.
  `app/services/batch_add_service.py:145-166` also allows concurrent batch
  jobs.
- `add_venue_handler.py:398-422` (`_find_in_account_inventory`, the
  timeout-recovery matcher): containment matching has no
  `MIN_CONTAINMENT_MATCH_LEN` guard (the deliberate guard exists at `:53` and
  `:857` for `_find_name_match`) — a short folded name ("vila", "casa") can
  "recover" the wrong venue, return 201 with the wrong venue_id, and poison
  the address cache so all future adds of that address short-circuit to it.
- Photo categories: the vibe-classifier's category tags are attached only to
  the legacy embedded photo list (`app/handlers/venue_handler.py:444-449`);
  the fresh-photos projection and `ResolvePhotosResponse`
  (`app/routers/internal_router.py:30-38`) carry `{url, author_name}` only,
  so the tags are invisible to vibes_bot/mobile, which still render them.

## Current Behavior
Closed venues serve and spend forever; a Places outage mid-run poisons the
tail of the venue list permanently; Instagram sweeps can mass-delete valid
handles; admin triggers can double paid refresh cycles; budget accounting
drifts on undo/races; short-name timeout recovery can link the wrong venue;
photo categories never reach consumers.

## Desired Behavior
- The nightly Google job must re-check `businessStatus` for already-enriched
  venues using a fields-masked Place Details call (status-only, cheapest SKU)
  without re-running full vibe enrichment; permanently/temporarily closed
  venues follow the existing `remove_*_closed_venues` deprecation paths.
  Bounded per run (config, default covering the full catalog nightly) and
  flag-gated (`BUSINESS_STATUS_RECHECK_ENABLED`, default true).
- `search_place_id` must distinguish "Google answered: no match" (returns a
  no-match sentinel/None) from transport/quota errors (raises a typed error);
  the enrichment loop writes the empty-marker only on genuine no-match, and
  on error skips the venue for this run with a distinct metric result label.
- Instagram validation must delete a handle only on definitive 404; 429,
  403, redirects-to-login, and errors count as "unknown — keep".
- One shared per-job concurrency guard must cover scheduler wrappers and
  admin triggers (`live_forecast`, `weekly_forecast`, `rebuild_redis`,
  `google_enrichment`): a trigger while the job runs returns the existing
  "already running" response instead of starting a second run.
- Geo-link provenance (`geo_linked: true` + ledger `year_month`) must be
  persisted at link time (venue row `extra`); `undo_geo_link` must require it
  and decrement the recorded month's counter only.
- Manual adds must take a short-TTL Redis `SET NX` lock keyed by the folded
  name+address hash around reserve→create→persist, re-check the address cache
  after acquiring, and release on completion; batch add must refuse to start
  while another batch job is running.
- `_find_in_account_inventory` must apply `MIN_CONTAINMENT_MATCH_LEN` to
  containment matches (exact folded equality allowed regardless) and require
  non-zero address-token overlap for non-exact single candidates.
- Fresh-photos payloads and `ResolvePhotosResponse` must include an optional
  `category` per photo, sourced from the vibe-profile evidence mapping the
  legacy path already uses. Additive field; readers unaffected until they opt
  in.

## Implementation Approach
- Enrichment: add a status-only recheck branch in `enrich_all_venues` for
  already-enriched venues (fields mask `businessStatus`), reusing the existing
  closure counters/deprecation calls; new typed error in
  `google_places_client.search_place_id` with the no-match/error split; wire
  the two markers/metric labels in both `enrich_all_venues` and
  `enrich_pending_venues`.
- Instagram: narrow the delete predicate in
  `validate_cached_instagram_handles` to definitive 404.
- Job lock: a small named-lock helper (asyncio locks in-process — scheduler
  and admin share the event loop/process) used by both `main.py` job wrappers
  and `_run_job`; admin returns the existing already-running shape.
- Budget: persist provenance in the venue row's `extra` at geo-link time;
  `undo_geo_link` validates provenance and passes the recorded `year_month`
  to a month-aware release; `venue_budget_service.release_manual_slot`
  gains an explicit month parameter.
- Add-venue race: Redis `SET NX EX` lock (existing DAO boundary) around the
  create section; post-lock re-check of the address cache; batch-add
  single-flight via the same named-lock helper.
- Recovery matcher: apply the existing guard constant + address-overlap
  requirement.
- Photos: extend the fresh-photos projection payload model and
  `ResolvePhotosResponse` with optional `category`.

## Data, Config, And API Impact
- Config: `BUSINESS_STATUS_RECHECK_ENABLED` (default true) and
  `BUSINESS_STATUS_RECHECK_LIMIT` (default 0 = full catalog). Existing
  `remove_*_closed_venues` flags keep gating the deprecation action.
- RDS: no schema change (provenance lives in the venue row's existing
  `extra`/residual JSON).
- Redis: one new short-TTL lock key format for manual adds (documented in the
  DAO); fresh-photos payload gains an optional `category` per photo.
- API: `ResolvePhotosResponse` gains optional `category` (additive);
  admin trigger endpoints now return the already-running response when
  racing a scheduled run (was: silent double-run).
- Cross-repo: vibes_bot may later pass `category` through its photo resolver
  (its own plan); no reader change required for compatibility.
- Money note: the first recheck-enabled nightly run performs one
  status-only Details call per enriched venue (~catalog size); subsequent
  runs the same. This is the intended, bounded cost of closure detection —
  flag off restores today's zero-recheck behavior.

## Error Handling And Observability
- New metric labels on the existing enrichment results counter:
  `recheck_closed`, `recheck_ok`, `skipped_error` (error ≠ no-match).
- Counter for lock rejections: admin trigger refused because the scheduled
  job holds the lock (visibility into H4 occurrences).
- Budget: log undo attempts rejected for missing provenance with venue id.
- Instagram: log kept-on-unknown outcomes with status code; no deletion
  without a 404.

## Test Plan
Feature file: `tests/bdd/enrichment/enrichment-and-budget-hardening.feature`

Scenarios:
- A permanently closed venue is deprecated by the nightly recheck without
  full re-enrichment and stops being selected for live refresh.
- A Places transport error mid-run skips the venue without writing the
  empty marker, and the venue is retried on the next run.
- A genuine zero-result writes the empty marker exactly as today.
- An Instagram 429 during validation keeps the handle; a definitive 404
  deletes it.
- An admin live-forecast trigger during a scheduled live refresh is refused
  as already-running and spends no BestTime calls.
- Undoing a geo-link created last month releases last month's slot; undoing
  a venue without geo-link provenance is rejected.
- Two concurrent manual adds of the same name+address result in one
  reservation, one paid create, and one venue.
- A timed-out create for a 4-character folded name does not containment-match
  an unrelated inventory venue.
- Fresh-photos payloads carry each photo's category when the vibe profile
  provides one.

Pytest unit tests:
- `search_place_id` error taxonomy (no-match vs raise) across HTTP error,
  timeout, empty result.
- Named-lock helper: concurrent acquire semantics; admin + scheduler paths.
- Budget month-aware release; provenance validation.
- Recovery-matcher guard: short-name containment rejected, exact match
  allowed, address-overlap disambiguation.

Manual or integration checks:
- AWS SSO, after deploy: confirm the first recheck run's Google call volume
  and closure counters in Grafana; verify no BestTime spend spike during an
  intentionally-triggered admin/scheduler overlap attempt; spot-check one
  undo in the admin panel against the ledger month.

## Acceptance Criteria
- A closed venue is deprecated within one nightly run and disappears from
  refresh selection; flag off restores current behavior.
- No empty-marker writes occur on transport errors (metric label proves the
  split); no Instagram deletions occur without a 404.
- Admin triggers cannot start a second concurrent paid refresh.
- The monthly ledger is unchanged after an add→undo round-trip in the same
  month, decrements the correct month across rollover, and two racing
  duplicate adds spend exactly one create.
- Timeout recovery never links a name shorter than the containment guard.
- BDD + targeted pytest green; existing suites stay green.

## Open Questions
- None blocking approval. Execution preconditions: rebase after
  `fix/admin-breakdown-and-addvenue-fold` and
  `feature/projector-and-serving-bulk-reads` merge (shared files), and
  confirm the Google Places fields-mask SKU for `businessStatus`-only Details
  calls before enabling the recheck in prod.
