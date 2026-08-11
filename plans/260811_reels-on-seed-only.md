# Reels On The Seed Run Only

## Branch
fix/reels-on-seed-only

## Goal
Crawl a target's reels once, on its first run, to reach history the posts cap
cannot — and never again, because after that the posts stream already carries
them.

## Non-goals
- **Removing `crawl_reels`.** It still decides whether reels are crawled at all.
- **Deduplicating differently.** Cross-stream dedupe stays as it is.
- **Changing the posts stream, the caps, the budget, or the cursors' meaning.**

## Evidence

### The steady-state reels stream buys almost nothing
`@entreamigosobode`, first real crawl, from the target's own bookkeeping:

```
last_run_reels_fetched   32
last_run_reels_new        1
```

Thirty-two reels results bought **one** post the grid had not already
supplied — roughly 40% of that run's $0.231 spent on duplicates. `@entreamigos.praia`
measured the same shape: 12 of 16 reels were already in the posts stream, and
every duplicated shortcode was `post_type: Video`.

The cause is Instagram, not this repo: a reel is also a grid post, so the
profile-grid endpoint returns it too. Apify bills per result *returned*, and its
actor has no media-type filter, so the overlap cannot be avoided at request
time — only by not making the request.

### But the seed run is different
On a seed, the posts stream is capped at `seed_results_limit`. That cap bounds
how far back the grid reaches. The reels endpoint has its **own** cap and its
own ordering, so a seed can pull reels **older** than the oldest post the grid
cap allowed — history that is otherwise unreachable, since the cursor only ever
moves forward and re-seeding means deleting the target.

So the value is entirely front-loaded: real on the first run, near-zero after.

## Current Behavior
A target with `crawl_reels` on crawls both streams on every run, paying full
price each time for content the posts stream nearly always already returned.

## Desired Behavior
1. A target with reels enabled crawls reels on its **seed** run.
2. It does not crawl reels on subsequent runs.
3. A target with reels disabled never crawls them.
4. A seed that fails still crawls reels when it is retried.
5. The posts stream is unaffected.

## Implementation Approach
Gate the reels stream on the **reels cursor being unset** — that is already the
repo's definition of "this stream has never successfully run", it needs no new
column, and it is the same signal the seed cap keys off.

Deriving it from the cursor rather than from a "has run before" flag matters for
§4: if the seed fails before bookkeeping, the cursor stays null and the retry
correctly seeds again. A separate flag would have to be unset by hand on every
failure path, and this project has already shipped a bookkeeping write that
failed and left a target looking permanently healthy.

**The two streams keep independent cursors.** Nothing here merges them; the
posts cursor advancing must never suppress a reels seed, and vice versa.

**Say it in the console's language too.** The `crawl_reels` control now means
"crawl reels once, on the first run" — the label and help text must say so, or
an operator will reasonably expect ongoing reels and read their absence as a
bug. That copy change lives in vibes_bot; this plan does not touch it, but the
PR must name it as a required follow-up rather than leaving the console lying.

**Cost note in the estimate.** The run estimate should reflect that reels are
only charged on a seed, so an operator sizing a second run is not quoted for a
stream that will not run.

## Data, Config, And API Impact
- **No migration.** The gate reads an existing column.
- **Existing targets:** any whose reels cursor is already set simply stop
  crawling reels — which is the intent. A target that never seeded reels will
  seed them on its next run.
- **Rollback:** revert; the cursor is untouched either way.

## Error Handling And Observability
Record why the reels stream was skipped — disabled versus already seeded — so a
zero-reels run is legible without reading code. The existing
`crawl_stream_overlap_total` and the per-run reels counters stay as they are;
they simply stop incrementing after the seed.

## Test Plan
Feature file: `tests/bdd/enrichment/reels-on-seed-only.feature`

Scenarios:
- Crawl reels on a target's first run.
- Skip reels on a target that has already seeded them.
- Never crawl reels for a target with reels disabled.
- Crawl reels again when the first attempt never recorded a cursor.
- Leave the posts stream unaffected on every run.
- Record why reels were skipped.

Pytest unit tests:
- The gate: reels cursor null, set, and set-with-posts-cursor-null.
- A failed seed leaves the cursor null and the next run seeds.
- The posts stream's behaviour is unchanged, asserted against pre-change
  behaviour.
- The run estimate excludes reels for a target that has already seeded.

## Acceptance Criteria
- Reels are crawled on the seed run and not afterwards.
- A failed seed retries.
- Posts are unaffected.
- The skip reason is recorded.
- The PR names the console copy change as a required follow-up.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None.
