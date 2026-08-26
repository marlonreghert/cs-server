# Crawl cursor advance after chain

## Branch
fix/crawl-cursor-advance-after-chain

## Goal
`ScheduledInstagramCrawlService.run_target` must never persist a cursor
advance (`cursor_posts_at`/`cursor_reels_at`/`reels_seeded_at`) for posts whose
archive/classify/extract chain has not actually finished. Today the cursor
commits before the chain runs, so a crash, restart, or an exception anywhere
in the chain leaves posts permanently unrecoverable: already fetched, already
billed to Apify, and never retried, because the cursor already moved past
them.

## Non-goals
- Do not change `_run_stream`, `compute_bound`, pinned-post handling, the
  monthly budget gate, or the per-stream fetch failure/dormancy logic. Those
  determine *what gets fetched*; this plan only changes *when the fact of
  having processed it is committed*.
- Do not make the fetch → archive → classify → extract pipeline transactional
  across Apify/S3/OpenAI/Postgres. That is not achievable from this process
  (see Error Handling below) and is not attempted.
- Do not change the existing per-photo/per-post degrade-gracefully behavior
  inside `InstagramCrawlChainer` (a failed image download, a failed
  classification, a failed single-post extraction are already caught, logged,
  and intentionally do not fail the whole chain). This plan does not touch
  that granularity.
- Do not add per-post/per-shortcode success tracking to `_ChainReport` or the
  chainer. It is not there today (see Evidence) and inventing it is a bigger
  change than this bug fix; see Open Questions for why it is deferred.

## Evidence
- `app/services/instagram_crawl_service.py`, `run_target` (~1321-1537, current
  `main` @ `1343d8c`): builds `cursor_updates` and `new_posts_by_stream` in
  the per-stream loop (~1353-1396), merges them into one `updates` dict with
  the billing/attempt fields (`last_run_at`, `last_run_results`,
  `last_run_cost_usd`, `consecutive_failures`, `posts_dormant`,
  `last_failure_kind`/`last_failure_at`, `enabled`, `last_run_reels_fetched`/
  `last_run_reels_new`), and writes the WHOLE thing in one
  `self.venue_dao.update_crawl_target(handle, updates)` call (~1488) — BEFORE
  `all_new_posts = dedupe_posts_by_shortcode(new_posts_by_stream)` (~1529) and
  `chained = await self._chain(target, all_new_posts, now)` (~1532) ever run.
- Production evidence (2026-08-25/26, this session): a `clubmetropole` run
  fetched and billed 50 posts, committed the cursor, then the container
  restarted mid-chain — zero rows landed in `events.post_item_source`,
  recovery required manually NULLing the cursor and re-paying for the same
  50 posts. A second run committed `last_run_at` and advanced the cursor
  while the chain was still mid-flight (downloading images) — this is normal
  control flow, not a rare race.
- `OUTCOME_BOOKKEEPING_FAILED` (~131-137, handling at ~1486-1521) already
  exists for a DIFFERENT incident (2026-08-09, entreamigos.praia): the
  bookkeeping WRITE itself failing after results were billed. Its guarantee
  (a failed write still counts as a failure via a second, minimal write, and
  `_chain` never runs past a failed bookkeeping write —
  `tests/test_instagram_crawl_service.py::TestBookkeepingWriteFailureCountsAsAFailure::test_the_archive_and_extract_chain_never_runs_when_the_bookkeeping_write_fails`)
  must survive this change unmodified.
- `InstagramCrawlChainer.chain_venue`/`_chain_shared_handle`/`chain_promoter`
  (~531-888) and `PromoterCrawlService._process_post`/`_archive_post_images`
  (`app/services/promoter_crawl_service.py:354-900ish`): every per-photo
  download (`_archive_venue_posts`/`_archive_post_images`), every
  classification call, and every single post's OpenAI extraction call
  (`_process_post`'s own try/except around `openai_client.extract_events`) is
  ALREADY individually caught, logged, and degrades gracefully — none of
  these raise past their own call site. `_ChainReport` (~518-528) only
  aggregates `archived: int`, `extracted: bool`, `classification_outcome:
  Optional[str]` — no per-post/per-shortcode outcome is returned to
  `run_target`. `event_extraction_service.run()` inside `chain_venue`
  (~633-640) is also wrapped and does not raise. The only ways `_chain` (or
  what it calls) can fail from `run_target`'s point of view are (a) a genuine
  unhandled bug/outage escaping one of the NOT-wrapped call sites (e.g.
  `self.venue_dao.list_instagram_handles()` in `_chain` itself, or
  `venue_dao.list_events_by_source`/`insert_event`/`update_event` inside
  `reconcile_post_events`, none of which are wrapped) or (b) the process
  dying outright (SIGKILL/OOM/deploy restart) — which is not an exception at
  all and cannot be caught by any amount of Python try/except.
- Idempotency: `app/services/event_reconciliation.py::reconcile_post_events`
  (~659-830) reads `existing_events = venue_dao.list_events_by_source(handle,
  shortcode)`, indexes them by `source_event_key`
  (`compute_source_event_key(title, starts_at)`), and calls
  `venue_dao.update_event` for a matching key or `venue_dao.insert_event` only
  for a genuinely new one. `rds_venue_store.py::insert_event`'s own docstring
  (~816) confirms this: "relies on the UNIQUE (...) constraint
  (`uq_post_item_source_post`, migration 0034) to reject a duplicate — the
  service is expected to check `list_events_by_source` first; this is the
  backstop, not the primary idempotency mechanism." Re-running the chain for
  an already-(partially-)processed post is therefore safe at the DB layer:
  no duplicate rows. The re-run is NOT free — it re-downloads images and
  re-calls OpenAI extraction (`_process_post` always calls
  `openai_client.extract_events` before checking `existing_by_title`) — an
  additional real cost on top of the already-accepted Apify re-bill, stated
  under Error Handling below.
- `app/dao/rds_venue_store.py::update_crawl_target` (~1935-1961): a plain
  partial-field `UPDATE ... WHERE handle=:handle`; "only the keys present in
  `fields` are set; everything else is left untouched." Calling it twice in
  one `run_target` invocation (once for attempt fields, once for cursor
  fields) is safe and already the pattern the existing
  `OUTCOME_BOOKKEEPING_FAILED` fallback write uses.
- `tests/bdd/steps/scheduled_incremental_instagram_crawl_steps.py` wires a
  REAL `InstagramCrawlChainer` + REAL `EventExtractionService` over an
  in-memory RDS fake, with fakes only at the Apify/S3/downloader/OpenAI
  boundary (`_reset_context`, ~239-282) — the harness this plan's new
  scenarios extend.

## Current Behavior
`run_target` writes cursor advance + billing/attempt bookkeeping in one
combined `update_crawl_target` call, then runs the archive/classify/extract
chain. Any interruption after that write (process death, or an exception
escaping `_chain`) leaves the DB believing the crawl fully completed while
none, or only some, of the actual archiving/extraction happened. The posts
are never re-fetched (the cursor already moved past them) and are
unrecoverable without manual cursor surgery.

## Desired Behavior
`run_target` writes billing/attempt bookkeeping (facts true the moment Apify
answered: `last_run_at`, `last_run_results`, `last_run_cost_usd`,
`consecutive_failures` from the fetch outcome, `posts_dormant`,
`last_failure_kind`/`last_failure_at`, `enabled`, `last_run_reels_fetched`/
`last_run_reels_new`) immediately after the fetch, exactly as today. It then
runs the chain. Only if the chain either had nothing to do or completed
without raising does it write the cursor advance (`cursor_posts_at`,
`cursor_reels_at`, `reels_seeded_at`) in a second, separate call. If the
chain raises, the cursor write is skipped entirely — the target's stored
cursor is untouched, so the next scheduled run re-fetches (and re-bills) the
same posts and the chain gets another chance. The failure is not silent: it
is logged, reflected in a new `CRAWL_RUNS_TOTAL{outcome="chain_failed"}`
metric, and folds into the SAME `consecutive_failures` counter/threshold the
fetch-failure path already feeds, so a permanently (not just transiently)
broken chain still trips `OUTCOME_SKIPPED_FAILURES` instead of re-fetching
and re-billing the same posts forever.

## Implementation Approach

### Field split
Two dicts replace today's single `updates`:
- `attempt_updates` — everything currently written under `if not
  credit_exhausted:` (unchanged logic, unchanged field set, unchanged
  dormancy/failure-kind/enabled-disable comments) except the cursor fields.
  Written immediately after the fetch loop, exactly where today's single
  write happens. A failure here behaves EXACTLY as `OUTCOME_BOOKKEEPING_
  FAILED` does today: log at ERROR, a second minimal `{"consecutive_
  failures": ...}` write based on the target's PRE-RUN value, then re-raise
  — `_chain` must never be reached (preserves `TestBookkeepingWriteFailure
  CountsAsAFailure::test_the_archive_and_extract_chain_never_runs_when_the_
  bookkeeping_write_fails` byte-for-byte).
- `cursor_updates` — `cursor_posts_at`, `cursor_reels_at`, `reels_seeded_at`
  (unchanged: `reels_seeded_at` stays bundled with the cursors, exactly as
  today's comment already argues, because a completed-but-unpersisted reels
  seed must fail the SAME way an unpersisted cursor does — this plan extends
  that existing invariant to also cover "the chain that follows didn't
  finish," not just "the write itself failed"). Written only after the chain
  step below decides it is safe to.

### Ordering
1. Fetch loop (unchanged) → `attempt_updates` write (unchanged content,
   trimmed to non-cursor fields) → on failure: log, fallback failure-count
   write, raise (unchanged behavior).
2. `all_new_posts = dedupe_posts_by_shortcode(new_posts_by_stream)` (moved
   up, unchanged logic) → if there is anything to chain
   (`all_new_posts and not credit_exhausted and self.chainer is not None`),
   call `await self._chain(...)` inside a `try/except`. On an exception:
   log at ERROR (handle, post count, the exception), emit
   `CRAWL_RUNS_TOTAL{outcome="chain_failed"}`, attempt a second write
   bumping `consecutive_failures` (base value: `attempt_updates`'s own
   just-written value, falling back to the target's pre-run value if
   `attempt_updates` was never written) and setting `last_failure_kind`/
   `last_failure_at` to the new `OUTCOME_CHAIN_FAILED`, then re-raise the
   original exception (`raise` with no argument, matching the existing
   bookkeeping-failure style) — `cursor_updates` is never reached, so it is
   never written.
3. Only if step 2 did not raise: if `cursor_updates`, write it in its own
   `update_crawl_target` call. On a failure here (the cursor write itself
   fails, even though the chain succeeded): log at ERROR, attempt the same
   kind of fallback `consecutive_failures` bump (reusing the existing
   `OUTCOME_BOOKKEEPING_FAILED` outcome/metric — this is still, fundamentally,
   a bookkeeping write failing after real work was already billed/performed,
   just at the second write instead of the first), then re-raise.
4. `_record_cursor_age` moves to run last, after the cursor-write step,
   passing `cursor_updates` (not the old merged `updates`) — it only ever
   reads the two cursor keys, so this is a no-op signature change; it now
   reports the age actually persisted, never a value that was rolled back by
   a chain failure.

### New outcome constant
Add `OUTCOME_CHAIN_FAILED = "chain_failed"` near `OUTCOME_BOOKKEEPING_FAILED`,
documented the same way every other `OUTCOME_*` constant in this file is:
what it means, why it is distinct from `OUTCOME_FAILED` (a fetch failure,
bills nothing) and from `OUTCOME_BOOKKEEPING_FAILED` (a DB write failure, not
a chain failure), and why it stays OUT of `FAILURE_OUTCOMES` (that tuple is
per-stream fetch outcomes only; `_run_stream` never returns this value).

### Partial-chain-success finding (judgment call, see Evidence)
The chainer does not report per-post outcomes today — `_ChainReport` is
aggregate-only (`archived`/`extracted`/`classification_outcome`), and every
per-post/per-photo failure inside it is already caught and degrades
gracefully rather than raising. There is therefore no data to advance the
cursor "only to the newest successfully chained post" — that capability does
not exist and this plan does not invent it (a real design change, bigger than
this bug fix, and not evidenced as needed: the graceful-degradation cases are
already handled correctly today and are out of scope). From `run_target`'s
point of view the chain is effectively all-or-nothing: either it returns
(having done as much as it gracefully could, exactly as it does today) or an
exception/process-death means it did not reach a trustworthy conclusion at
all, and NONE of `all_new_posts` (across both streams) gets its cursor
credited. This is coarser than per-post credit but matches what the chainer
can actually report, and is a strict improvement over today's "always credit
everything, unconditionally."

## Data, Config, And API Impact
None. No schema/migration change — `cursor_posts_at`, `cursor_reels_at`,
`reels_seeded_at`, and every attempt/billing field already exist as columns
(`events.crawl_target`) and the admin API's `last_failure_kind` is already a
plain `Optional[str]` (`admin_crawl_router.py:195`), not an enum, so the new
`"chain_failed"` value needs no model change.

## Error Handling And Observability
- New `CRAWL_RUNS_TOTAL{handle_kind, result_type="posts", outcome="chain_
  failed"}` counter increment (reusing the existing counter, a new label
  value) plus an ERROR log naming the handle and post count on a chain
  failure.
- Explicit, stated trade-off: this fix cannot make the fetch → archive →
  extract pipeline transactional (Apify + S3 + OpenAI + Postgres cannot share
  a transaction, and a SIGKILL between any two statements is not observable
  by this process at all). What it changes is the FAILURE MODE: today a
  crash mid-chain is silent, permanent data loss with money already spent;
  after this fix, the same crash leaves the cursor unmoved, so the exact same
  posts are safely re-fetched (and re-billed to Apify, and re-run through
  OpenAI extraction — a real, additional cost, not just Apify's) on the next
  scheduled run. Money is recoverable; silently discarded data was not — this
  is the deliberate trade-off, not an oversight.
- Loop safety: a chain failure increments the SAME `consecutive_failures`
  counter `run_target`'s own `enabled`/`max_consecutive_failures` gate reads
  at the top of the method, and the SAME `OUTCOME_SKIPPED_FAILURES` skip path
  already in place. A target whose chain is permanently broken (not a
  transient crash) still stops spending once it crosses `max_consecutive_
  failures`, exactly like a permanently-failing fetch does today. A
  genuinely transient failure (a real crash) self-heals on the next
  successful run, which resets `consecutive_failures` to 0 via the normal
  `attempt_updates` write.
- Idempotency (see Evidence): re-processing the same post after a chain
  failure is safe at the DB layer (`reconcile_post_events` matches on
  `source_event_key` and updates in place; `uq_post_item_source_post` is the
  backstop). No new failure mode is introduced by re-running the chain twice
  for the same post.

## Test Plan
Feature file: `tests/bdd/enrichment/crawl-cursor-advance-after-chain.feature`

Scenarios:
- A chain failure leaves the target's cursor unchanged, so the same posts
  are fetched again on the next run.
- A chain failure still records the run's billing/attempt bookkeeping
  (`last_run_at`, `last_run_results`, `last_run_cost_usd`) — the fetch and
  the spend already happened and must not be hidden by a later failure.
- A chain failure counts toward the target's consecutive-failure total.
- A successful chain advances the cursor exactly as before (no regression —
  reusing/aligned with the existing "Chain archiving and extraction after a
  crawl that found posts" scenario in `scheduled-incremental-instagram-
  crawl.feature`).
- The existing bookkeeping-write-failure guarantee still holds: the chain
  never runs past a failed attempt-bookkeeping write (already covered by
  `tests/test_instagram_crawl_service.py`'s `TestBookkeepingWriteFailure
  CountsAsAFailure`; reconciled/re-verified here, not duplicated in Gherkin
  since it is already pytest-covered internal-logic).

Pytest unit tests (`tests/test_instagram_crawl_service.py`):
- A chain failure leaves `cursor_posts_at`/`cursor_reels_at`/`reels_seeded_at`
  untouched while `last_run_at`/`last_run_results`/`last_run_cost_usd` DO
  land (asserting the two-write split directly against the in-memory DAO).
- A chain failure increments `consecutive_failures` on top of whatever the
  fetch outcome already set it to (not a stale pre-run base).
- Repeated chain failures (>= `max_consecutive_failures`) eventually trip
  `OUTCOME_SKIPPED_FAILURES` on the next `run_target` call — proves this
  cannot become an unbounded re-fetch/re-bill loop.
- A cursor-write failure (chain succeeds, the SECOND `update_crawl_target`
  call raises) leaves the cursor unchanged, still bumps `consecutive_
  failures`, and re-raises (mirrors `TestBookkeepingWriteFailureCountsAs
  AFailure`'s pattern for the first write, applied to the second).
- Existing `TestBookkeepingWriteFailureCountsAsAFailure` tests continue to
  pass unmodified (no code changes expected there; run to confirm no
  regression).

Manual or integration checks: None — no live Apify/S3/OpenAI/Redis call is
required or permitted by this repo's BDD policy.

## Acceptance Criteria
- An exception raised by `_chain` (or anything it calls) leaves
  `cursor_posts_at`, `cursor_reels_at`, and `reels_seeded_at` exactly as they
  were before `run_target` was called.
- The SAME run still persists `last_run_at`, `last_run_results`, and
  `last_run_cost_usd` for that run, even though the chain failed.
- `consecutive_failures` increases on a chain failure, and a target whose
  chain keeps failing eventually reaches `OUTCOME_SKIPPED_FAILURES` (stops
  spending) rather than retrying forever.
- A successful run (chain completes, or there was nothing to chain) advances
  the cursor to exactly the same value it would have before this change.
- `TestBookkeepingWriteFailureCountsAsAFailure` (all four existing tests)
  pass unmodified.
- `make test-feature FEATURE=tests/bdd/enrichment/crawl-cursor-advance-after-chain.feature`,
  `make test-unit`, and `make test-bdd` all pass.

## Open Questions
None.
