# Skip posts event extraction has already successfully extracted

## Branch
fix/skip-already-extracted-posts

## Goal
Event extraction must stop paying for an OpenAI vision call on an archived
post it has already successfully turned into an event. The scheduled path
(`EventExtractionService.run()`'s `event_candidates`/`venue_ids` branch) must
skip such a post BEFORE the model call and BEFORE the per-venue cap is
applied, so the cap is spent on genuinely new posts. A post whose only prior
attempt(s) all failed must still be retried. The deliberate re-extraction path
(`mode="handles"`, `TRIGGER_HANDLE_REEXTRACTION`) must keep re-extracting
unconditionally.

## Non-goals
- Changing `_run_handles` / `mode="handles"` in any way. It is the sanctioned
  "deliberately re-extract" path (reachable in production today via
  `POST /admin/trigger/event_extraction` with a `{"eligibility": {"mode":
  "handles", ...}}` body — see Evidence) and must keep calling the model
  unconditionally for every archived post it is given, or the only way to
  force a correction stops working (constraint 4).
- Changing `_sort_newest_first` or the cap value (`DEFAULT_MAX_POSTS_PER_VENUE
  = 20`). Both are the subject of `plans/260826_extraction-newest-posts-first.md`
  (already merged, `1978487`) and are built on here unmodified.
- Adding a migration/new column. The skip is derived entirely from
  `venue_dao.list_events_by_source(handle, shortcode)`, data that already
  exists (see Implementation Approach for why this is enough).
- Re-reading a caption/date "live" from Instagram to catch an edited post.
  See §5a below for why this is explicitly out of scope, not silently
  ignored.
- Touching `event_reconciliation.py`. The skip lives entirely in
  `event_extraction_service.py`, before `_extract_one`/`reconcile_post_events`
  are ever called; nothing about reconciliation itself changes.

## Evidence

### The bug
- `app/services/event_extraction_service.py:845` (`_extract_one`) —
  `existing_events = self.venue_dao.list_events_by_source(handle,
  post.shortcode)` is fetched and used only for supersession bookkeeping
  (`already_superseded`, `existing_by_title`) — never to decide whether to
  call the model at all. `self.openai_client.extract_events(...)` at line 854
  runs unconditionally for every post that clears `post_qualifies`.
- Scale (operator-supplied, this task): ~15 enabled crawl targets x up to
  `max_posts_per_venue` (20) x ~3 scheduled fires/week ≈ 900 vision calls/week,
  the large majority re-reads of posts already turned into events, since the
  60-day lookback window re-returns the same archived posts run after run
  while new posts arrive slowly relative to it.

### The real scheduled caller (confirms which branch actually matters)
- `app/services/instagram_crawl_service.py:656-660` — the cron-scheduled,
  per-crawl-target chain (`InstagramCrawlChainer._chain`) calls
  `self.event_extraction_service.run({"eligibility": {"mode": "venue_ids",
  "venue_ids": ",".join(venue_ids)}})` after archiving new posts. This is the
  ONLY cron-driven caller of `EventExtractionService.run()` in production
  (`main.py` schedules the Instagram crawl sync, not `event_extraction`
  itself — grepped, no match). It always uses `mode="venue_ids"`, never
  `"handles"`.
- `app/routers/admin_trigger_router.py:308-325` — the generic
  `POST /admin/trigger/event_extraction` endpoint's `default_config` is
  `{"eligibility": {"mode": "event_candidates", ...}}`, and its `runner` is
  `lambda c, cfg: c.event_extraction_service.run(cfg)` — `cfg` is caller-
  supplied, so an operator CAN pass `{"eligibility": {"mode": "handles",
  "handles": "..."}}` through this same endpoint. This is the production
  reachability path for `TRIGGER_HANDLE_REEXTRACTION` (constraint 4) — it is
  not dead code, and this plan must not break it.
- Both `event_candidates` and `venue_ids` are handled by the SAME `run()`
  branch (`event_extraction_service.py:616`, the `else` of `if
  cfg["eligibility_mode"] == "handles"`) — the skip added below covers both
  sub-modes identically; only `mode="handles"` (`_run_handles`, a separate
  method) is exempt.

### Why no migration is needed
- `venue_dao.list_events_by_source(handle, shortcode)` (already called by
  `_extract_one`) returns every `events.post_item_source` row for this exact
  post, each carrying its parent event's `status`
  (`app/dao/rds_venue_store.py:642-654`, `_EVENT_SOURCE_SELECT`). Every
  status the pipeline can write is one of `pending_review`, `confirmed`,
  `rejected`, `superseded`, `extraction_failed`, `accepted`
  (`app/services/event_reconciliation.py:115-118`, `ALL_STATUSES`). A post
  "has already been successfully extracted" iff at least one of its rows has
  a status other than `extraction_failed` — no new storage is needed to
  answer that.

### §5a — edited Instagram posts (explicit decision, not silently accepted)
- `app/services/event_extraction_service.py:298-353` (`_bucket_entries`) —
  for a given shortcode, `caption`/`permalink`/`timestamp`/`media_type`/
  `location_tag` are set via `grouped.setdefault(shortcode, {...})` from
  manifests processed OLDEST-run-first (`_manifests_since`'s own
  contract). `setdefault` only writes on the FIRST manifest that contains
  this shortcode; every later manifest re-seeing the same shortcode does
  NOT update these fields (only `_run_date`, `any_photo_key`, and the
  best-confidence flyer fields are updated across iterations). **This means
  an edited Instagram caption is not reliably surfaced by this pipeline's
  archive read TODAY, independent of this fix**: `_extract_one` has always
  been fed the FIRST-ever-archived caption for a shortcode within the
  lookback window, not a live-refetched one, because the crawl only
  re-fetches an Instagram account's *recent* posts (an old shortcode is not
  reliably re-fetched once other, newer posts push it out of "recent") and
  even when it is, the bucketing keeps the oldest-seen text. Skipping the
  model call on an already-successful post therefore does not create a new
  staleness problem: the caption/image handed to the model on a hypothetical
  re-call would already be the same first-seen bytes, just paid for again.
  Decision: accept this — the sanctioned way to force a fresh read (whatever
  it would read) is the explicit `mode="handles"` re-extraction path, which
  this plan does not touch.

### §5b — `last_seen_at` freshness (menu-item expiry)
- `app/models/menu_lifecycle.py:88-` (`is_menu_item_current`) derives a menu
  item's currency purely from `last_seen_at`, read at request time — see
  module docstring, "no nightly job... derived at READ TIME".
  `app/routers/admin_events_router.py`'s `_menu_is_current` (or its
  equivalent read path) feeds it the aggregated `last_seen_at`
  (`RdsVenueStore`'s `agg` LATERAL, `MAX(last_seen_at)` across every source
  row of the event — `app/dao/rds_venue_store.py:562-594`).
- Today, `last_seen_at` for a post's source row is refreshed ONLY as a side
  effect of `_extract_one` calling the model and then
  `reconcile_post_events`/`_record_failure` writing to
  `events.post_item_source`. A post this plan newly skips would otherwise
  never touch that row again — its menu item (if any) could silently expire
  even though the crawl keeps re-seeing the same post every run.
- Decision: the skip must not skip the DB touch, only the model call. Add a
  cheap, direct `last_seen_at` refresh (`venue_dao.update_event(event_id,
  {"last_seen_at": now, "source_handle": handle, "source_shortcode":
  shortcode})`) for every LIVE (non-superseded) row this post already has,
  the moment it is skipped. This is the SAME "poke" shape
  `tests/rds_fake.py:748`'s own comment already documents as an accepted,
  pre-existing pattern ("every existing direct `update_event({"last_seen_at":
  ...})` poke in this repo's tests and routers") — not a new mechanism.
  Superseded rows are left untouched, mirroring
  `event_reconciliation.py`'s own "never touched once superseded"
  invariant (module docstring, and see the bullet list under "Owns
  everything...").

## Current Behavior
`run()`'s non-handles branch: fetch a venue's archived posts, sort newest
first, truncate to `max_posts_per_venue`, then for each surviving post: skip
if it doesn't qualify (`post_qualifies`), otherwise call `_extract_one`, which
always calls the model — regardless of whether this exact post already has a
live, successfully-extracted event. On a steady-state venue (posting cadence
slower than the 60-day lookback re-scans), the SAME already-extracted posts
are re-billed to OpenAI on every scheduled crawl, forever, until they age out
of the lookback window.

## Desired Behavior
In `run()`'s non-handles branch, immediately after `_sort_newest_first` and
BEFORE the `max_posts_per_venue` truncation: for each post with a shortcode,
look up `existing_events = venue_dao.list_events_by_source(handle,
post.shortcode)`. If `_already_extracted(existing_events)` is true (at least
one row's status is not `extraction_failed`), the post is NOT a candidate for
this run's cap: bump `EVENT_EXTRACTION_POSTS_TOTAL{outcome="skipped_seen"}`,
refresh `last_seen_at` on its live rows, and move on WITHOUT calling the
model. Otherwise the post proceeds into the (now correctly sized) pool that
`max_posts_per_venue` truncates. A post whose every existing row is
`extraction_failed` is NOT treated as already-extracted — it proceeds and is
retried, exactly as constraint 3 requires. A brand-new post (no existing
rows at all) proceeds exactly as before. `_run_handles` (mode="handles")
is entirely unchanged: it keeps calling the model for every post it is
given, unconditionally.

## Implementation Approach
All changes are in `app/services/event_extraction_service.py`.

1. Add a module-level pure predicate, next to `post_qualifies` (same "pure,
   directly unit-testable on call count" convention):
   `_already_extracted(existing_events: list[dict]) -> bool` — `bool(
   existing_events) and any(row.get("status") != STATUS_EXTRACTION_FAILED for
   row in existing_events)`.
2. Add `EventExtractionService._touch_seen(self, existing_events, handle,
   post, now)`: for each row in `existing_events` whose status is not
   `STATUS_SUPERSEDED`, call `self.venue_dao.update_event(row["event_id"],
   {"last_seen_at": now, "source_handle": handle, "source_shortcode":
   post.shortcode})`. No other field is touched — a skip must never alter
   `raw_extraction`, `status`, or `review_reason` (unlike a real failure,
   nothing failed).
3. In `run()`'s non-handles branch (currently: fetch, sort, truncate, then
   loop), restructure to: fetch, sort, THEN partition into
   `already_extracted` (skipped: bumped + touched, per posts above) vs.
   `unprocessed` (posts with no shortcode pass through unprocessed unchanged,
   matching today's silent `continue` further down and so consuming no new
   behavior), THEN truncate `unprocessed` to `max_posts_per_venue`, THEN run
   the existing per-post qualify/extract loop over the truncated
   `unprocessed` list. `now` for the touch is the SAME `now = self._now()`
   already computed once at the top of `run()`.
4. `_run_handles` is not touched at all.
5. Reuse the already-declared-but-unused `OUTCOME_SKIPPED_SEEN = "skipped_seen"`
   (line 128) and its pre-existing mention in `EVENT_EXTRACTION_POSTS_TOTAL`'s
   own outcome-label comment (`app/metrics.py:1240`) — no metrics.py edit
   needed, this label was already reserved for exactly this.

## Data, Config, And API Impact
None. No schema/migration, no config key, no request/response shape change.
`max_posts_per_venue`'s meaning is unchanged; which posts are eligible to
fill it changes.

## Error Handling And Observability
- New outcome value actually emitted for the first time:
  `event_extraction_posts_total{outcome="skipped_seen", kind="not_applicable"}`
  — the direct measure of calls saved (compare its rate to
  `openai_api_calls_total{endpoint="event_extract"}` dropping).
- Log line at skip time (`logger.info`), naming handle/shortcode and prior
  status, so a suspiciously-never-updated post is diagnosable (mirrors the
  existing `_run_handles` cost-stated-before-spent log convention).
- No new failure mode: `_touch_seen` calls the same `update_event` DAO method
  already used elsewhere in this file; a failure there surfaces the same way
  an existing `update_event` failure would (this file does not currently
  wrap those calls in their own try/except, so this introduces no new
  inconsistency).

## Existing Test Impact (read before implementing — this is the wide part of the diff)
The skip changes what "run `EventExtractionService.run()` twice over the same
already-extracted shortcode in `event_candidates`/`venue_ids` mode" means: the
model is no longer called the second time. Several existing tests construct
EXACTLY that shape to exercise `event_reconciliation.py`'s merge/dedup/
confirmed-protection logic via a genuine second model call. Each is
retargeted to drive its SECOND run through `mode="handles"` instead (still a
real, unconditional re-extraction, per this plan's whole design) — this
changes ONLY which eligibility mode reaches the model for that one call, and
is provably behavior-preserving: `_run_handles`' single-venue branch (the
shape every one of these fixtures has) calls `_extract_one` with the exact
same `venue_id`/`handle`/`attribute_fn=None` the non-handles branch does
(`event_extraction_service.py:730-740` vs. `:626-642`) — the only difference
is the `trigger` label passed through (affects only
`EVENT_EXTRACTION_SUPERSEDED_TOTAL`'s label, asserted nowhere in these
tests).

Verified NOT affected (confirmed by reading each file, not assumed): every
scenario in `multi-event-posts.feature` drives `PromoterCrawlService`, a
sibling service this plan does not touch; `record-what-superseded-a-row.feature`
and `auto-accept-and-field-protection.feature` call
`event_reconciliation.reconcile_post_events` directly, bypassing
`EventExtractionService` entirely; `event-attribution-and-dates.feature`'s
"extracted again" scenario calls `resolve_event_datetime`/
`select_date_interpretation_for_reuse` directly, same reason;
`menu-item-lifecycle.feature`'s "posted again" scenarios use a NEW shortcode
merged by dish identity, not a repeat of the SAME shortcode, so
`_already_extracted` is false for it the first time it is seen (unaffected).

Add one shared BDD helper, `_run_reextraction(context, **overrides)`, next to
`_run_extraction` in `tests/bdd/steps/instagram_event_extraction_steps.py`
(`eligibility={"mode": "handles", "handles": context.ee_handle}`, then
delegates to `_run_extraction`) — a single documented place recording WHY
"run again" now means handles mode, imported by the two files below instead
of duplicating the `eligibility` dict at each call site.

Retarget (swap the bare `_run_extraction(context)` call for
`_run_reextraction(context)`; no feature-file wording changes needed):
- `tests/bdd/steps/instagram_event_extraction_steps.py`:
  `step_when_event_extraction_runs_again_over_that_post`,
  `step_when_event_extraction_runs_again_over_its_post`.
- `tests/bdd/steps/venue_post_multi_event_steps.py`:
  `step_when_venue_event_extraction_runs_again_reordered`,
  `step_when_venue_event_extraction_runs_again_only_two`,
  `step_when_venue_event_extraction_runs_again_different_title`,
  `step_when_venue_event_extraction_runs_again_different_date`,
  `step_when_venue_event_extraction_runs_again_no_longer_returns_that_event`.
  (NOT `step_when_the_multi_event_extraction_runs_again_different_title` —
  that one drives the promoter path via `_run_mep_crawl`, already unaffected.)
- `tests/bdd/steps/one_event_many_posts_steps.py`:
  `step_when_that_post_is_extracted_again`.

Retarget in `tests/test_event_extraction_service.py` (pytest): the SECOND
`service.run(cfg)`/`service2.run(cfg)` call in
`TestIdempotentReExtraction.test_two_runs_with_the_same_answer_leave_one_row`,
`TestIdempotentReExtraction.test_a_changed_title_supersedes_the_old_row_instead_of_updating_it`,
and `TestConfirmedIsNeverReverted.test_confirmed_title_and_date_survive_reextraction`
to `{"eligibility": {"mode": "handles", "handles": "v1_handle"}}`. This
file's own `_FakePostSource` (test-local, distinct from the BDD one) needs a
minimal `posts_for_handle(self, handle, venue_ids, since)` added (return the
concatenation of `self.posts_by_venue.get(vid, [])` for `vid` in
`venue_ids`) — `_run_handles` calls it and the current fake only implements
`posts_for_venue`.

## Test Plan
Feature file: `tests/bdd/enrichment/skip-already-extracted-posts.feature`

Scenarios:
- A post already successfully extracted is not sent to the model again (and
  `event_extraction_posts_total{outcome="skipped_seen"}` records it).
- A post whose only prior attempt(s) failed (`extraction_failed`) IS
  retried — the model is called and a second failure/success is recorded
  normally.
- The cap is spent on unprocessed posts, not consumed by already-extracted
  ones: several already-extracted posts plus a tight cap of new qualifying
  posts — every new post within the cap is extracted, none are starved by
  the already-extracted ones sharing the lookback window.
- A first-ever run for a venue (no prior extraction at all) behaves exactly
  as before — every qualifying post is sent to the model.
- Deliberate re-extraction (`mode="handles"`) still calls the model for an
  already-successfully-extracted post — proves constraint 4 is not broken by
  this plan's own change.
- §5b: an already-extracted post that is skipped still refreshes its menu
  item's freshness (`last_seen_at`) without a model call — built on the
  existing `menu-item-lifecycle.feature` fixtures
  (`tests/bdd/steps/menu_item_lifecycle_steps.py`'s `_seed_menu_item`).

Pytest unit tests (`tests/test_event_extraction_service.py`, new class(es)):
- `_already_extracted`: empty list -> False; all rows `extraction_failed` ->
  False; one non-`extraction_failed` row among several `extraction_failed`
  ones -> True; a single `confirmed`/`accepted`/`pending_review`/`rejected`/
  `superseded` row each -> True (every non-`extraction_failed` status
  counts).
- `_touch_seen`: updates `last_seen_at` (+ identifying fields) on a live row;
  leaves a `superseded` row's `last_seen_at` untouched; never calls
  `openai_client`.
- Integration through `service.run()`: an already-extracted post plus a cap
  of 1 and one new qualifying post -> the new post is extracted, the
  already-extracted one is not re-sent, `client.calls == 1`.
- Retargeted `TestIdempotentReExtraction`/`TestConfirmedIsNeverReverted`
  tests above continue to pass unchanged in intent (still prove
  reconciliation upsert/confirmed-protection), just via `mode="handles"` for
  their second call.

Manual or integration checks: None (BDD + pytest cover this fully with
deterministic fakes; no live S3/OpenAI/Apify required).

## Acceptance Criteria
- A post with at least one existing, non-`extraction_failed` event row is
  never passed to `openai_client.extract_events` again by `run()`'s
  non-handles branch.
- A post whose every existing row is `extraction_failed` is still sent to
  the model.
- `max_posts_per_venue` is applied AFTER the already-extracted filter, never
  before — verified by a scenario where already-extracted posts alone would
  have exceeded the cap.
- A venue with no prior extractions sees no behavior change.
- `_run_handles` (mode="handles") is unmodified and still calls the model
  unconditionally for every post it is given, including an
  already-successfully-extracted one.
- A skipped post's live (non-superseded) event row(s) have `last_seen_at`
  refreshed to the run's `now`.
- `event_extraction_posts_total{outcome="skipped_seen"}` increments exactly
  once per skipped post.
- Every retargeted existing test (BDD + pytest, listed above) passes.

## Open Questions
None.
