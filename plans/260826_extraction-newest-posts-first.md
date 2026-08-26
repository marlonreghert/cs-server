# Extraction processes the newest archived posts first

## Branch
fix/extraction-newest-posts-first

## Goal
Event extraction must spend its per-venue post cap on the NEWEST archived
Instagram posts within the lookback window, never the oldest. Today it
silently discards every post newer than whatever the cap already consumed,
which can permanently starve a venue of extraction even while its crawl keeps
fetching current flyers.

## Non-goals
- Raising `DEFAULT_MAX_POSTS_PER_VENUE` (20). Tuning the cap size is a
  separate decision from fixing its ordering; a recommended value is
  discussed below but not changed by this fix.
- Changing `_manifests_since`'s oldest-first contract or its docstring. Other
  callers (see Evidence) may rely on it; nothing here touches it.
- Any change to `EventPostSource.posts_for_venue` / `posts_for_promoter` /
  `posts_for_handle`'s own return order. They keep returning posts in
  whatever order `_bucket_entries` produces (manifest-insertion order); the
  sort is applied by the caller, immediately before the cap is applied, so no
  other consumer of these methods (see Evidence) is affected.
- A "skip posts already successfully extracted before spending an OpenAI
  call" optimization. This was proposed mid-task as a second part of this
  fix (a real, separate cost issue: `_extract_one` fetches `existing_events`
  but never uses it to skip the model call, so a steady-state run re-sends
  already-extracted posts to OpenAI every time it runs). It is deliberately
  NOT bundled here: it changes a different, cost-sensitive decision point
  (when to skip the model), needs its own BDD scenarios, interacts with the
  deliberate `TRIGGER_HANDLE_REEXTRACTION` re-extraction path, and would
  roughly double the size and review surface of what was briefed as a
  single-purpose ordering defect fix under a "review, then I merge/deploy"
  gate. It is real and worth doing, but as its own `/plan-feature` cycle
  with its own approval — see Open Questions for the concrete follow-up.

## Evidence
- `app/services/event_extraction_service.py:201-202` —
  `DEFAULT_MAX_POSTS_PER_VENUE = 20`, `DEFAULT_LOOKBACK_DAYS = 60`.
- `app/services/event_extraction_service.py:271-296` — `_manifests_since`'s
  own docstring: "chronologically ascending (oldest first —
  `list_run_prefixes`'s own guarantee)". This ordering is a *manifest* order
  (mixing run order and, within a run, this actor's own array order), not a
  per-post recency order.
- `app/services/event_extraction_service.py:298-353` — `_bucket_entries`
  groups manifest photo entries by shortcode, preserving that same manifest
  insertion order (dict iteration order == first-seen order).
- `app/services/event_extraction_service.py:370-377` — `posts_for_venue`
  returns `[self._post_from_bucket(b) for b in grouped.values()]`: oldest
  manifest first, in.
- `app/services/event_extraction_service.py:586-588` (`run()`, the
  `event_candidates`/`venue_ids` eligibility branch):
  ```python
  posts = await self.post_source.posts_for_venue(venue_id, since)
  if cfg["max_posts_per_venue"]:
      posts = posts[: cfg["max_posts_per_venue"]]
  ```
  Taking the first N of an oldest-first list keeps the N OLDEST posts.
- `app/services/event_extraction_service.py:681-690` (`_run_handles`, the
  `mode="handles"` branch) has the IDENTICAL shape:
  ```python
  posts = await self.post_source.posts_for_handle(archive_handle, venue_ids, since)
  ...
  if cfg["max_posts_per_venue"]:
      posts = posts[: cfg["max_posts_per_venue"]]
  ```
  `posts_for_handle` (line 391-423) is also oldest-first (it dedupes several
  `_manifests_since` reads via `post_dedupe.dedupe_by_shortcode`, which does
  not reorder). This is the same bug, reachable a second way.
- Only three callers read through `_manifests_since`: `posts_for_venue`,
  `posts_for_promoter`, `posts_for_handle` (all in `EventPostSource`, same
  file). `posts_for_promoter` is not currently called by anything in
  `app/`. `posts_for_venue` has one other caller,
  `app/services/promoter_registry_service.py:150` (`run_discovery`), which
  does not slice or cap its result — it looks up a single post by shortcode
  from the full list — so it is order-independent and unaffected by sorting
  at the `run()`/`_run_handles` call sites instead of inside
  `EventPostSource`.
- Precedent for the fix's shape already exists in this codebase and is
  reused, not reinvented:
  - `app/services/archive_sources.py:274-301` (`_sort_posts_newest_first`):
    sorts pre-archival post dicts newest-first by parsing each post's own
    `timestamp`; a post with a missing/unparseable timestamp sorts LAST,
    stable among ties, and the function never raises. Its own docstring:
    "the actor's own array order is not a contract... two accounts scraped
    by the same client return their posts in a different relative order on
    the same day."
  - `app/services/instagram_crawl_service.py:467-487`
    (`_split_kept_and_dropped`): "An unparseable/missing timestamp is KEPT,
    not dropped — mirrors this repo's existing 'a bad value never
    disqualifies, it just sorts/behaves conservatively' convention."
  These operate on raw post dicts with string timestamps (pre-archival);
  `ArchivedPost.timestamp` here is already a parsed `Optional[datetime]`
  (see `_post_from_bucket`, which calls
  `event_venue_targeting._parse_timestamp` on `bucket["timestamp"]`), so
  reusing `_sort_posts_newest_first` directly is not a type-compatible fit
  (it expects `list[dict]`, not `list[ArchivedPost]`, and would re-parse a
  string that is already parsed here). A small sibling helper is added in
  `event_extraction_service.py` instead, mirroring the SAME convention
  (newest first, undated last, stable, never raises) rather than a new one.
- Production evidence (2026-08-26), refined after the initial report: the
  mechanism is not "total archive volume", it is which MANIFEST is oldest.
  - `casabacurau` (`ven_7338734d...`): one Aug-12 manifest run alone holds 54
    distinct posts. At cap 20, that single oldest run fully consumes the cap
    before any later run is ever reached — the venue's newest extracted post
    is pinned at Aug 12 no matter how many fresher crawls have run since,
    and stays pinned until that Aug-12 run ages out of the 60-day lookback.
  - `clubmetropole` (`ven_672d3654...`): its oldest in-window run (Aug 07)
    holds only 5 posts, so the cap spills into the Aug-25/26 runs, and
    within a manifest the entries are ordered however the archiving crawl
    happened to enumerate them that day — not reliably chronological. It
    shows no gap by circumstance (small oldest run), not by any ordering
    guarantee.
  - This is why the fix sorts on each post's own timestamp rather than
    reversing the list or trusting manifest/run order: manifest order is a
    mix of run order and per-run enumeration order, and neither is a
    reliable proxy for recency (the same reasoning `_sort_posts_newest_first`
    was already built on for the pre-archival case).
  - `tatubola.recifepe`: a cron crawl fetched 30 posts, archived 20 flyer
    images under the correct venue_id with a manifest, and extraction
    produced zero events with no error — consistent with the cap being fully
    spent on an older manifest before this fresh one was ever read.

## Current Behavior
`run()`'s `event_candidates`/`venue_ids` branch and `_run_handles`'s
`mode="handles"` branch both fetch a venue's/handle's archived posts (oldest
manifest first) and then keep `posts[:max_posts_per_venue]` — the OLDEST
`max_posts_per_venue` posts in the lookback window. Every post archived after
the cap is reached is silently never looked at: no error, no log, no metric,
nothing distinguishes "nothing qualified" from "never examined". A venue
whose oldest in-window archived run already holds >= the cap is permanently
stuck on that run until it ages out of the 60-day lookback, regardless of how
many fresh crawls happen in between.

## Desired Behavior
Both branches sort the fetched posts by each post's own `timestamp`, newest
first, immediately before applying `max_posts_per_venue`. A post with no
usable timestamp (`ArchivedPost.timestamp is None`) sorts last — it is never
preferred over a dated post, and never excluded outright by the sort itself;
only the cap (unchanged) can still drop it, exactly as it would drop any
other post beyond the Nth. The cap continues to bound how many posts are
processed per run — this fix changes WHICH posts are chosen, not whether a
bound exists.

## Implementation Approach
Add one small module-level helper in `app/services/event_extraction_service.py`
(near `EventPostSource`, operating on `list[ArchivedPost]`):

- Split posts into "has a timestamp" and "no timestamp".
- Sort the dated group by `.timestamp`, descending, using Python's stable
  sort (ties keep their original relative order — the same "not a literal
  reversal" guarantee `_sort_posts_newest_first` already documents and
  relies on).
- Return dated (newest first) + undated (in their original order), mirroring
  `_sort_posts_newest_first`'s and `_split_kept_and_dropped`'s "a bad or
  missing value never disqualifies, it just behaves conservatively"
  convention.

Call this helper in exactly two places, both immediately before the existing
`if cfg["max_posts_per_venue"]: posts = posts[:cfg["max_posts_per_venue"]]`
truncation:

1. `run()`'s `event_candidates`/`venue_ids` branch (after
   `await self.post_source.posts_for_venue(...)`).
2. `_run_handles()` (after `await self.post_source.posts_for_handle(...)`,
   and after the existing `if not posts:` early-continue so an empty list is
   never sorted).

`EventPostSource.posts_for_venue` / `posts_for_promoter` / `posts_for_handle`
themselves are NOT changed — they keep returning posts in manifest order.
Sorting happens once, at the point each cap is applied, which is also the
only point in the file that currently assumes an order. This is deliberately
the minimal-blast-radius fix: it does not touch `_manifests_since`'s
documented contract, does not touch `promoter_registry_service.run_discovery`
(which is order-independent already), and does not touch the
`post_dedupe.dedupe_by_shortcode` merge `posts_for_handle` uses.

## Data, Config, And API Impact
None. No schema, no config key, no request/response shape changes.
`max_posts_per_venue`'s meaning (a count bound) is unchanged; only which
posts fall inside it changes.

## Error Handling And Observability
No new runtime path (no new I/O, no new failure mode) — the sort is pure and
operates on data already in memory. No new metric is added for the sort
itself; `EVENT_EXTRACTION_POSTS_TOTAL`'s existing `outcome`/`kind` labels
already report what happens to every post that makes it past the cap, and a
post cut off BY the cap remains, as it is today, invisible to per-post
metrics (a pre-existing gap, not introduced or worsened here — a "dropped by
cap" counter would be a distinct observability improvement, not required to
fix the ordering defect).

## Cost Note
This ships a one-off backlog effect: the next run over every affected venue
will see up to `max_posts_per_venue` (20) previously-unseen, genuinely-recent
posts instead of re-looking at already-exhausted old ones. Only posts that
clear `post_qualifies` (a classified `flyer` above the confidence floor, or a
caption event-marker match) cost an OpenAI vision call — worst case, one run
examines up to `max_venues` x `max_posts_per_venue` = 25 x 20 = 500 posts
across every eligible venue, of which only the qualifying subset is actually
billed. Per this file's own existing convention (`run()`'s
`"estimated_cost_usd": None  # Never asserted as fact: measure a real run
before trusting this"`), no dollar figure is asserted here either — the
operator should watch `EVENT_EXTRACTION_POSTS_TOTAL`'s outcome counts and
`OPENAI_API_CALLS_TOTAL` / `OPENAI_TOKENS_TOTAL` (both already emitted by
`openai_event_extraction_client.py`) on the first run after this ships. This
is intended, one-off, and expected — not a regression.

## Cap Value Recommendation (separate from the ordering fix)
Not changed by this fix. Recommendation for a future, separate change: raise
`DEFAULT_MAX_POSTS_PER_VENUE` modestly (e.g. 20 -> 30) once the fixed
ordering has run in production for a few cycles and the real qualifying-post
volume per run is visible in `EVENT_EXTRACTION_POSTS_TOTAL`. Reasoning: even
with the ordering fixed, a venue whose genuine posting cadence exceeds the
cap within the 60-day lookback can still have older-but-in-window posts
permanently fall outside the "newest N" cut on every run (a steady-state
version of the same starvation, far less severe since it only affects the
tail of an unusually active venue, not the entire archive). Raising the cap
without evidence of the real post-per-run volume this fix surfaces would be
guessing; this plan intentionally leaves that tuning to a follow-up once the
fix has produced real numbers.

## Idempotency Confirmation
Re-processing a post that was already extracted stays idempotent and is
UNCHANGED by this fix. `_extract_one` matches existing rows via
`venue_dao.list_events_by_source(handle, post.shortcode)` before calling the
model, and `event_reconciliation.reconcile_post_events` (shared with
`PromoterCrawlService`) upserts by the content-derived `source_event_key`,
so a re-extraction of the same post updates the same row(s) rather than
duplicating them. `uq_post_item_source_post` is the DB-level backstop for
the underlying post-item write. Nothing in this fix changes which posts get
re-examined across runs in a way that defeats this — it only changes the
ORDER in which posts are selected into the cap, not the reconciliation path
each selected post goes through.

## Test Plan
Feature file: `tests/bdd/enrichment/extraction-newest-posts-first.feature`

Scenarios:
- Cap keeps the newest posts, never the oldest, regardless of archive order
  (posts inserted out of chronological order; cap smaller than the post
  count) — proves an explicit timestamp sort, not a list reversal or
  reliance on manifest order (a plain `reversed()` would pass a
  single-shuffle case but not a genuinely scrambled one; the fixture
  scrambles the order so a reversal-only fix would fail).
- The cap still bounds how many posts are processed (same fixture; asserts
  the qualifying-post count equals the cap, not the total).
- A post with no usable timestamp is handled, never crashes, and is not
  preferred over dated posts when the cap is tight (dropped, not the
  extraction blowing up).
- A post with no usable timestamp is still processed when the cap does not
  bind (proves "conservative", not "disqualified" — it is only ever pushed
  behind dated posts, never removed by the sort itself).
- A venue with fewer posts than the cap behaves exactly as before (no
  regression): every post is still extracted regardless of archive order.
- Re-extracting by handle (`mode="handles"`, `_run_handles`) also keeps the
  newest posts, never the oldest — same fixture shape as the venue-mode
  scenario, proving the second occurrence of the bug is fixed the same way.

Pytest unit tests (`tests/test_event_extraction_service.py`):
- The new sort helper directly: dated posts come back strictly newest-first
  by timestamp regardless of input order; undated posts are appended after
  every dated post; a mix of dated/undated never raises; ties preserve
  input order (stability).
- `run()`'s truncation, given a stub post source returning posts out of
  chronological order, selects the newest `max_posts_per_venue` by
  timestamp, not by input position.

Manual or integration checks: None (BDD + pytest cover this fully with
deterministic fakes; no live S3/OpenAI/Apify required).

## Acceptance Criteria
- A venue/handle with more archived posts (within the lookback window) than
  `max_posts_per_venue` has its NEWEST posts, by their own timestamp,
  processed — never the oldest.
- A post with no usable timestamp never crashes the run and is never
  preferred over a dated post; it is only dropped when the cap does not have
  room for it, same as any other post beyond the Nth.
- The cap still bounds the number of posts processed per venue/handle per
  run.
- A venue/handle with fewer archived posts than the cap sees no behavior
  change.
- Both the `venue_ids`/`event_candidates` path (`run()`) and the `handles`
  path (`_run_handles`) apply the same newest-first ordering.
- `_manifests_since`'s docstring and return-order contract are untouched.

## Open Questions
None for this fix. Follow-up (not blocking, not in this plan): a second,
separate change to skip an OpenAI call for a post whose extraction already
succeeded (`_extract_one` currently fetches `existing_events` but only uses
it for reconciliation/supersession, never to skip the model call before
spending it) was raised mid-task as a cost concern. It is real, but is
deliberately scoped OUT of this fix (see Non-goals) and should go through its
own `/plan-feature` cycle — it changes a different decision point (when to
skip the model, not which posts the cap admits), needs its own BDD coverage
(a post already extracted is not re-sent; an `extraction_failed` post IS
retried; the cap is spent on new posts, not already-extracted ones; a
first-ever run is unaffected), and deserves its own dedicated review rather
than being folded into a same-day defect-fix PR.
