# Scheduled Incremental Instagram Crawl — stop re-buying posts we already own

## Branch
feature/scheduled-incremental-instagram-crawl

## Goal
Turn event crawling from an operator-triggered, full-re-scrape action into a
scheduled, incremental one: each target is crawled on its own cron, each crawl
starts from the last post it already saw, and reels are crawled as well as
posts — so the pipeline runs itself and steady-state spend tracks *new* content
instead of the size of the catalog.

## Non-goals
- **Crawling the whole catalog.** Scheduling is opt-in per target. See
  §Evidence for why an all-venues schedule is unaffordable by a wide margin.
- **A date *range*.** The actor has no upper bound — §B. Only "since" is real.
- **Changing extraction, reconciliation, merge or review.** This feature ends
  where `event_extraction` already begins; it changes what reaches it and when.
- **Comments, stories, mentions, tagged posts.** `resultsType` supports them;
  none carries event flyers, and each is a separate billed run.
- **Per-venue LLM cost control.** The OpenAI side is already gated by
  `post_qualifies`; this plan does not touch it.
- **The console.** `vibes_bot/plans/260809_crawl-schedule-console.md`.

## Evidence

### The actor bills per result, so a server-side filter is a discount
Read from the live build of `apify/instagram-scraper`:

```
pricingModel: PAY_PER_EVENT
  event "result" — "Each result written to the dataset"
  FREE tier: $0.0027 per result
```

The operator's balance is $5 — about **1,850 results, total, ever**. Every
result the actor writes is billed whether or not we already have it.

`fetch_recent_posts` (`app/api/apify_instagram_client.py:219`) currently sends
only `directUrls`, `resultsType` and `resultsLimit`. There is no lower bound, so
**every crawl re-fetches and re-pays for posts already archived in S3.**

### The filter we need exists; the other half does not
From the same build's input schema, verbatim:

| field | fact |
|---|---|
| `onlyPostsNewerThan` | `YYYY-MM-DD`, ISO, or relative (`1 day`, `2 months`, `3 years`) |
| — | *"Times are in UTC, not local time."* |
| — | *"Pinned posts may still appear even with this filter set."* |
| `resultsType` | enum: `posts`, `details`, `comments`, `reels`, `mentions`, `stories` |
| **`onlyPostsOlderThan`** | **does not exist** |

So: incremental crawling is directly supported; a bounded date *range* is not,
and an upper bound applied after the fetch saves nothing because the results are
already billed.

`reels` is a **separate `resultsType`**, meaning a second actor run and a second
bill — not a flag on the existing one.

### Scheduling every venue is not affordable
Measured against production:

- `venues.venue` — **2,341** rows
- `instagram.handle` with a non-deleted handle — **1,114**
- **distinct** handles among those — **1,066**

One pass at 10 posts each is ~11,140 results ≈ **$30** — six times the balance,
for one pass. Scheduling therefore has to be opt-in per target, which is exactly
what the operator asked for.

**The 1,114 → 1,066 gap is load-bearing.** 48 venue rows share a handle with
another venue: `Entre Amigos O Bode` and `Entre Amigos O Bode Espinheiro` are
two `venue_id`s pointing at `@entreamigosobode`. A schedule keyed by `venue_id`
would run that handle twice per cycle and pay twice for identical results. This
is why §A keys on the handle.

### The "No cron" guarantee is being deliberately replaced
`docs/venue-retrieval-storage.md:130` states, in a section headed *"Cost
guarantees (do not rearrange)"*:

> **No cron.** Operator-triggered only, so steady-state spend is $0.

That guarantee was correct *because there was no cursor*: a scheduled run would
have re-bought the catalog on every tick. A cursor changes the arithmetic — an
unchanged target returns zero results and costs zero — which is what makes cron
safe now.

**It must be replaced, not deleted.** "No cron" was the only thing bounding
steady-state spend, and removing it without a successor leaves the pipeline with
no ceiling at all. §F defines the successor and this plan must edit that
document in the same change.

### The scheduler already exists
`main.py` builds an `AsyncIOScheduler` and already uses
`CronTrigger.from_crontab` (`:401`, for `weekly_forecast_cron`), with a shared
instrumentation + job-lock wrapper (`:78-121`) that every scheduled job goes
through. This feature registers jobs into that machinery; it does not build a
scheduler.

### One client method, three callers
`fetch_recent_posts` is called from `archive_sources.py:370` (venue photo
archive), `promoter_crawl_service.py:143`, and
`instagram_posts_enrichment_service.py:72`. The date bound therefore has exactly
one place to be added and three call sites to thread it through.

## Current Behavior
An operator triggers a job by hand. Every run re-scrapes the most recent N posts
of every selected venue and pays for all of them, whether or not they are
already in S3. Reels are never fetched, so an event announced only as a reel is
invisible. Nothing records how far back a target has been crawled.

## Desired Behavior
1. Crawl each target on its own schedule, without an operator.
2. Start each crawl from the newest post already seen for that target, minus a
   small overlap, and never re-fetch further back than that.
3. Seed a brand-new target from a configurable lookback, defaulting to 3 months.
4. Crawl reels as well as posts, per target.
5. Key the schedule and the cursor on the Instagram handle, so a handle shared
   by two venues is crawled — and billed — once.
6. Keep an absolute ceiling on spend that does not depend on an operator
   remembering to look.
7. Chain a completed crawl into archiving and extraction, so a scheduled crawl
   produces reviewable events with no further action.
8. Record, per target and per run, what was fetched and what it cost.

## Implementation Approach

### Step 0 — a $0.01 probe, before any design is committed
Two facts change the shape of §E and cannot be read from the schema:

1. Does `onlyPostsNewerThan` apply when `resultsType: "reels"`? The field is
   named for posts and documented generically.
2. Do reels come back with a usable `timestamp` for the cursor?

Run the actor twice against `@entreamigos.praia` with `resultsLimit: 1` — about
$0.005 — and record the answers **in the PR**. If the filter does not apply to
reels, then every reels crawl re-pays for the whole reels tab, and §E must fall
back to a `resultsLimit`-only reels crawl on a slower schedule. Designing past
this without checking is how a "cheap" feature becomes the biggest line on the
bill.

### A. `events.crawl_target` — keyed by handle
One row per Instagram handle we crawl, covering venue handles and promoter
handles alike:

`handle` (PK), `kind` (`venue` | `promoter`), `enabled`, `cron` (crontab
string), `timezone`, `crawl_reels`, `initial_lookback`, `results_limit`,
`cursor_posts_at`, `cursor_reels_at`, `last_run_at`, `last_run_results`,
`last_run_cost_usd`, `consecutive_failures`, `notes`, timestamps.

**Keyed on the handle, not the venue.** The 1,114/1,066 measurement above is the
whole argument: a `venue_id` key double-bills every shared handle. Venue
attribution is a *separate* question already answered downstream — a post is
attributed by the pipeline that consumes it, not by the schedule that fetched
it — so nothing is lost by keying here on the thing we actually pay per.

`events.promoter_account` keeps its own registry (status, discovery, mention
counts) and gains nothing here; a promoter that should be crawled gets a
`crawl_target` row alongside, with `kind='promoter'`. Merging the two tables was
rejected: the promoter registry answers "is this account worth crawling", the
crawl target answers "when and from where do we crawl it", and a discovered
promoter must be able to exist without a schedule.

### B. The cursor is the newest **post** timestamp, not the run time
After a successful crawl, `cursor_posts_at` becomes the greatest `timestamp`
among the posts returned. The next crawl sends
`onlyPostsNewerThan = cursor - overlap`.

**Never the run's wall clock.** The actor filters in UTC, our scheduler runs in
Recife time, Instagram's own timestamps lag publication, and a post can appear
in the API minutes after it was posted. A wall-clock cursor silently drops every
post that lands inside that gap — and drops it *permanently*, because the cursor
has already moved past it. The failure is invisible: no error, just events that
never existed.

**Overlap: 6 hours, configurable, deliberately not a day.** The operator asked
for 3–6h. At $0.0027 a result, six hours of overlap on a venue posting twice a
day is about **one and a half cents a month**. Re-fetched posts are absorbed
without duplicates by machinery that already exists and is already tested: S3
skip-if-exists on the archive side, and `source_event_key` idempotency on the
extraction side (`UNIQUE (source_handle, source_shortcode, source_event_key)`).
So the overlap costs cents and buys the only protection against a whole class of
silent, unrecoverable data loss.

**The cursor advances only on success.** A failed or partial crawl must leave it
untouched, or the failure window is skipped forever.

### C. Seeding a new target
A target with a null cursor sends `onlyPostsNewerThan = initial_lookback`,
default `3 months`, and is bounded by `results_limit` in the same call.

Both bounds apply together and both are needed: the date bound is what the
operator reasons about, the count bound is what stops a prolific account from
spending the entire balance on its first tick. A 3-month seed of a venue posting
daily is ~90 results ≈ $0.24; the same seed of a high-volume promoter could be
600+. Only the count cap makes the worst case knowable in advance.

### D. Scheduling, and the two timezones
Each target carries a crontab string, registered with
`CronTrigger.from_crontab` into the existing `AsyncIOScheduler`, through the
existing instrumented-job wrapper and its `lock_name` guard so two crawls of the
same handle can never overlap.

`timezone` defaults to `America/Recife`. **This feature has two timezones and
they are not the same one:** the schedule fires in local time (an operator
saying "Friday night" means Friday night in Recife), while
`onlyPostsNewerThan` is interpreted by the actor in **UTC** — the actor's own
documentation says so explicitly. The cursor must therefore be stored and sent
as UTC, and only the cron trigger may be local. Getting this backwards shifts
every bound by three hours, which the 6-hour overlap would mask for a while and
then stop masking.

Schedule changes take effect without a restart: the registry is re-read and
jobs re-registered when a target is written, the same way
`main.py:420`'s comment describes for existing jobs.

### E. Reels
When `crawl_reels` is set, the target gets a **second** actor run with
`resultsType: "reels"` and its own `cursor_reels_at`.

**Separate cursors, because the two streams advance independently.** A venue can
post ten grid posts and no reels for a month; one shared cursor would drag the
reels bound forward past reels that were never fetched, and they would never be
seen. This is the same permanent-loss failure as §B, arriving by a different
route.

The operator's example — a venue running jazz and samba on Friday and Saturday
announced through reels — is exactly the case a posts-only crawl misses
entirely.

**Naming:** the catalog has no venue called *Entre Amigos Boa Viagem*. It has
`Entre Amigos Praia` (`@entreamigos.praia`), `Entre Amigos O Bode` and
`Entre Amigos O Bode Espinheiro` (both `@entreamigosobode`), and `Bar Entre
Amigos` (no handle). Boa Viagem is Recife's beachfront, so `Entre Amigos Praia`
is taken as the intended target for manual verification. If that is the wrong
one, it changes the verification target only — no design depends on it.

### F. The replacement for "No cron"
`docs/venue-retrieval-storage.md` §3 must be edited in this change: the "No
cron" bullet is replaced by the guarantees that actually hold afterwards.

1. **A hard monthly result budget**, checked *before* each actor call and
   decremented by each run's result count. On exhaustion, scheduled crawls stop
   and say so loudly; operator-triggered runs still refuse. This is the direct
   successor to "No cron" — it bounds steady-state spend by a number the
   operator sets rather than by the absence of automation.
2. **A per-run result cap** (`results_limit`), always applied.
3. **The cursor itself**, which makes an unchanged target cost nothing.
4. **Skip-before-spend ordering is unchanged** — the budget check, the enabled
   check and the cursor are all resolved before any call is made, exactly as
   §3's rule 2 already requires.

The existing `ApifyCreditExhaustedError` → `ArchiveCreditExhausted` translation
must keep working: a scheduled run that exhausts credit has to stop the whole
cycle, not fail one target and continue into an empty balance for the rest.

### G. Pinned posts
The actor states pinned posts can bypass the filter. They arrive billed — that
cannot be avoided — but they must not be *processed*: a pinned flyer from last
year would otherwise consume cap slots and re-enter extraction on every single
crawl, forever.

Drop anything older than the requested bound after the fetch, count it, and
never let it move the cursor. The existing `_sort_posts_newest_first`
(`archive_sources.py:380`) is where the ordering assumption already lives.

### H. Chaining to extraction
A successful crawl enqueues the archive + extraction step for what it fetched,
so "almost fully automated" holds end to end. Nothing new is invented: the
chain calls the same services the operator's manual sequence calls today, in the
same order.

A crawl that returns zero new posts must chain nothing — the common steady-state
case, and it must cost nothing downstream either.

## Data, Config, And API Impact
- **Migration `0029_crawl_target`** from head `0028_event_ticket_info_and_attractions`:
  create `events.crawl_target` as §A. No back-fill — an existing venue has no
  schedule until an operator gives it one, and inventing schedules for 1,066
  handles is precisely the unaffordable case §Evidence rules out.
- **Settings:** `crawl_cursor_overlap_hours` (default 6),
  `crawl_default_initial_lookback` (default `3 months`),
  `crawl_monthly_result_budget`, `crawl_default_results_limit`.
- **Client:** `fetch_recent_posts` gains `only_posts_newer_than` and
  `results_type`, defaulting to today's behaviour so the three existing callers
  are unaffected until they opt in.
- **Admin API:** CRUD for crawl targets, plus a run-now action and a read model
  carrying last-run results/cost and next-fire time. Additive.
- **Serving:** none. No app-facing change.
- **Downgrade:** drop the table; unregister the jobs. Safe — losing cursors
  means the next manual crawl re-fetches a lookback window, which costs money
  but breaks nothing.

## Error Handling And Observability
A target that fails increments `consecutive_failures` and is skipped once it
crosses a threshold, so a dead handle cannot burn the budget on retries forever.

Metrics: `crawl_runs_total{handle_kind,result_type,outcome}`,
`crawl_results_total` (the billed number — the one that maps to money),
`crawl_budget_remaining`, `crawl_cursor_age_seconds`.

**Watch `crawl_cursor_age_seconds` above all.** A cursor that stops advancing
while runs keep succeeding is the signature of every silent failure in §B, §E
and §G, and it is the only symptom any of them produce.

`handle` is a label of bounded cardinality here (targets are opt-in and few); a
`run_id` never is, and never becomes a Prometheus label.

## Test Plan
Feature file: `tests/bdd/enrichment/scheduled-incremental-instagram-crawl.feature`

Scenarios:
- Crawl a brand-new target from the default three-month lookback.
- Crawl an existing target from its cursor minus the overlap.
- Advance the cursor to the newest post returned.
- Leave the cursor untouched when a crawl fails.
- Leave the cursor untouched when a crawl returns nothing.
- Crawl one handle once when two venues share it.
- Crawl reels as a separate run with a separate cursor.
- Advance the posts cursor without advancing the reels cursor.
- Drop a pinned post older than the requested bound, and count it.
- Never let a pinned post move the cursor.
- Refuse to crawl when the monthly result budget is exhausted, before calling.
- Apply the per-run result cap alongside the date bound.
- Skip a disabled target entirely.
- Skip a target past its consecutive-failure threshold.
- Chain archiving and extraction after a crawl that returned new posts.
- Chain nothing after a crawl that returned none.
- Stop the whole cycle when Apify reports credit exhaustion.

Pytest unit tests:
- The bound sent to the actor for: no cursor, a cursor, a cursor plus overlap,
  and a configured lookback — asserted as the exact string passed.
- Cursor selection is the max post timestamp, not the run clock — including a
  batch returned out of order.
- Cursor is stored and sent in UTC while the cron trigger is Recife-local, with
  a case that fails if the two are swapped.
- Overlap arithmetic at 3h and 6h.
- Handle-keyed dedup: two venue rows, one handle, one call.
- The budget gate refuses **before** the client is called — asserted on call
  count, not on the return value.
- Crontab parsing rejects a malformed string at write time, not at fire time.
- Reels and posts cursors move independently.

Manual or integration checks:
- Step 0's probe results, recorded in the PR.
- Schedule `@entreamigos.praia` with reels on, run it, and confirm a
  Friday/Saturday jazz-or-samba announcement reaches the review queue.
- Re-run the same target immediately and confirm near-zero results and
  near-zero cost.

## Acceptance Criteria
- A new target seeds from three months; an existing one resumes from its cursor
  minus a 6-hour overlap.
- The cursor is the newest post's timestamp and advances only on success.
- A handle shared by two venues is crawled once per cycle.
- Reels are crawled with an independent cursor.
- Pinned posts older than the bound are dropped, counted, and never move the
  cursor.
- The monthly budget is checked before any call and stops scheduled crawling.
- `docs/venue-retrieval-storage.md` §3 no longer claims "No cron" and states the
  replacement guarantees.
- A scheduled crawl produces reviewable events with no operator action.
- `make test-feature`, `make test-unit`, `make test-bdd`, and CI's
  scratch-Postgres migrate step all pass.

## Open Questions
None blocking. Step 0's two reels facts are resolved by a $0.01 probe as the
first execution step and recorded in the PR; the venue-name ambiguity in §E
affects only which venue is used for manual verification.
