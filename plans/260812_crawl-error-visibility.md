# Crawl Error Visibility — stop reading a blocked scrape as an empty account

## Branch
fix/crawl-error-visibility

## Goal
A crawl that failed must be reported as a failure — not as an account with
nothing to say. An operator must be able to see, per target, that the last run
returned an error, what kind, and whether it is worth retrying.

## Non-goals
- **Changing the reels-on-seed-only policy.** §A removes the reason it looked
  wrong; the policy itself stays as `260811_reels-on-seed-only.md` set it. See
  §A's note on `downtownbeergarden_`.
- **Venue attribution and date resolution.** Separate plan,
  `260812_event-attribution-and-dates.md`.
- **Merging duplicate events.** Deferred by the operator, agreed.
- **Any admin-UI work in vibes_bot.** This plan puts the facts on the API and in
  metrics; surfacing them in the console is a follow-up.
- **Retrying inside a run.** Apify already retried eleven times (§A). Another
  retry loop on top of that is not the fix.

## Evidence

### The crawler cannot tell "blocked" from "empty"
On 2026-08-12 five targets logged `Fetched 0 posts`. Apify's own run records
show every one of those runs returned **one** dataset item, not zero, and
`SUCCEEDED`:

| target | stream | Apify items | error | requestErrorMessages |
|---|---|---|---|---|
| `downtownbeergarden_` | posts | 1 | `no_items` | **11 × "Request blocked, retrying it again with different session"** |
| `burburinhobar` | posts/reels | 1 | `no_items` | none |
| `armazem14.recifeantigo` | posts/reels | 1 | `no_items` | none |
| `downtownrecife` | posts/reels | 1 | `no_items` | none |
| `champagne_recifee` | posts/reels | 1 | **`not_found`** ("Post does not exist") | n/a |
| `recifequecabenobolso` | reels | 1 | `no_items` | none |

`app/api/apify_instagram_client.py`'s mapping loop drops any item carrying an
`error` key with a bare `continue`, then logs `Fetched {len(posts)}`. The error
item is the only thing in the dataset, so the log reads `0` and the error text
is never seen by anything.

`downtownbeergarden_` is the case that proves this is not "the account is
empty": Instagram blocked the scrape eleven times, Apify exhausted its session
rotation and gave up. The account posts regularly — the operator confirmed it.
Its **reels** stream succeeded in the same cycle (12 items), so the handle,
the token, and the actor are all fine.

### A blocked run is currently recorded as a healthy one
In `instagram_crawl_service._run_stream`, an empty `kept` returns
`OUTCOME_EMPTY`. In `run_target`:

- `OUTCOME_EMPTY` is not `OUTCOME_FAILED`, so `any_failed` stays false and
  **`consecutive_failures` is reset to 0** (line ~1109). A target that is
  blocked on every single run keeps a zero failure count forever and never
  trips `max_consecutive_failures`.
- `last_run_results` is written as **1** and `last_run_cost_usd` bills for it —
  the error item is counted by `CRAWL_RESULTS_TOTAL` and charged against the
  monthly budget, because `result_count` is taken from the raw response before
  the error item is dropped.
- The cursor correctly does not advance (only `OUTCOME_SUCCESS` writes it), so
  the target is not silently marked as seeded.

So the target reads as: ran today, cost money, returned one result, zero
failures. Every one of those is misleading.

**`downtownbeergarden_` is the one target that will genuinely go dark.** Its
reels stream *succeeded*, so `cursor_reels_at` is now set and
`reels_already_seeded` is true; from the next run on, reels are skipped by
policy while posts stay blocked. That is the seed-only policy behaving exactly
as designed on top of a posts stream that is failing silently — which is why
§A is the fix and the policy is not.

### Nothing records whether a post was a video
`apify_instagram_client` maps Apify's `type` to `post_type` (`"Video"`,
`"Image"`, `"Sidecar"`) and it reaches the S3 manifest via
`archive_sources.py:329`, but `events.post_item_source` has no column for it.
The operator asked "how well do we handle video posts?" and the database cannot
answer — note this is a *different* `post_type` from
`events.post_item.post_type` (`event`/`promotion`/`menu`), which is an unrelated
name collision worth not deepening.

### A truncated roundup looks identical to a complete one
`DEFAULT_MAX_EVENTS_PER_POST = 20` caps how many items one post can yield.
Across the 2026-08-12 snapshot, `source_event_index` reaches exactly 20 on
**14 separate posts** — all from `oquetemhojeemnatal`, whose roundups routinely
list more than twenty events. Nothing records that the cap bit.

There is already an `OUTCOME_TRUNCATED` (`event_extraction_service.py:141`),
but it means something else — the model's *output token budget* ran out and the
post is left entirely unprocessed. Hitting the event cap is a different event:
the response was well-formed and we deliberately kept the first twenty. Do not
overload the existing outcome.

## Current Behavior
A blocked or not-found scrape is indistinguishable from an empty account: it is
logged as `Fetched 0`, recorded as `empty`, billed, and resets the failure
counter. Media type is never persisted. Cap-truncated posts are silent.

## Desired Behavior
1. Read Apify's error item instead of discarding it.
2. Record a permanently-wrong handle differently from a transient block.
3. Count a failed fetch as a failure, so `consecutive_failures` means something.
4. Never bill an error item as a result.
5. Persist each source post's media type.
6. Record when the per-post event cap truncated a post.

## Implementation Approach

Ship as **one commit and one feature file per section** on a single branch and
PR, per the operator's standing preference for phased multi-defect fixes.

### A. Parse the error item at the client edge
In `apify_instagram_client.fetch_recent_posts`, stop discarding error items.
Return them to the caller alongside the posts — a small result object, not a
bare list — so `_run_stream` can distinguish:

- **`not_found`** — the handle does not exist. Permanent. Retrying costs money
  and will never succeed.
- **`no_items` with a non-empty `requestErrorMessages`** — Instagram blocked
  the scrape. Transient. Worth retrying on the next scheduled fire.
- **`no_items` with no request errors** — genuinely empty or private for this
  `resultsType`. Legitimately empty; `recifequecabenobolso`'s reels stream is a
  real instance (its posts stream returned 50 the same minute).

**Do not infer these from `errorDescription` text.** Apify returns the same
string "Empty or private data for provided input" for the blocked case and the
genuinely-empty case; only `error` and `requestErrorMessages` separate them.
Matching on the prose would break the moment Apify rewords it.

Treat an unrecognised `error` value as transient-failure, not success. A new
Apify error code must not silently re-enter the "empty account" path this plan
exists to close.

### B. Outcomes that mean what they say
Add outcomes distinguishing a failed fetch from an empty one — at minimum a
permanent (`handle_not_found`) and a transient (`blocked`) variant, both
counted by `CRAWL_RUNS_TOTAL` with their own `outcome` labels, and both setting
`any_failed` so `consecutive_failures` increments.

`OUTCOME_EMPTY` keeps its current meaning and must stay reachable — a real
empty stream is not a failure and must not increment the counter.

**Do not bill an error item.** `result_count` must exclude error items before
`CRAWL_RESULTS_TOTAL`, the budget increment, and `last_run_cost_usd`. This is
a real overcharge today, small per run but paid on every fire of every broken
target, forever.

For `handle_not_found`, prefer disabling the target over letting it retry on a
schedule — but **surface it, do not silently disable**: write the reason to the
target so the admin API can say why, and log at warning. A target that vanishes
from the rotation with no explanation is the failure mode this plan is about.

### C. Persist media type
Add the Apify media type to `events.post_item_source`. Name it so it cannot be
confused with `post_item.post_type` — `source_media_type` — and say why in the
migration docstring.

Back-fill is not required; existing rows keep NULL, meaning "not recorded",
which is honest. Do not guess a value from the archived image.

### D. Record cap truncation
When the parser drops entries because `max_events_per_post` was reached, record
it on the post's sources and count it in a metric. The operator must be able to
ask "which posts did we truncate" without reading logs.

Leave the cap at 20 in this plan. Raising it changes OpenAI output-budget sizing
(`compute_multi_event_max_completion_tokens`) and risks the truncated-response
failure `OUTCOME_TRUNCATED` already guards; that is a separate, measurable
decision and needs its own evidence.

## Data, Config, And API Impact
- **Migration `0036_source_media_type`** — adds `source_media_type text NULL`
  to `events.post_item_source`, plus whatever column §D needs for cap
  truncation. Additive and nullable; no back-fill.
- **`crawl_target`** — a column recording the last run's failure kind/reason,
  if one does not already serve. Check `0030_crawl_target` and the existing
  `consecutive_failures`/`last_status` fields before adding; say which you
  found.
- **Admin API** — `admin_crawl_router._to_out` gains the last-failure fields.
  Additive only. Released clients read this surface; nothing may be removed.
- **Rollback:** revert. New columns are nullable and unread by older code.

## Error Handling And Observability
- `CRAWL_RUNS_TOTAL` gains the new `outcome` labels. Because a Prometheus series
  only exists after its first increment, the **absence** of a `blocked` series
  is itself the evidence that no target was blocked — keep the labels distinct
  rather than folding them into `failed`.
- Log the Apify `error` code and the `requestErrorMessages` count at warning.
  Log the description too, but never make control flow depend on it (§A).
- Add a counter for cap-truncated posts (§D).
- **Watch the blocked rate.** A rise across many targets at once means
  Instagram is rate-limiting the whole account, not that individual venues went
  quiet — and those need telling apart before anyone starts editing handles.

## Test Plan

One feature file per section, all under `tests/bdd/enrichment/`:

Feature file: `tests/bdd/enrichment/crawl-error-visibility.feature`

Scenarios:
- Report a blocked scrape as a failure, not as an empty account.
- Report a non-existent handle as permanently failed.
- Keep treating a genuinely empty stream as empty.
- Increment the failure counter when a fetch is blocked.
- Reset the failure counter after a stream genuinely succeeds.
- Refuse to bill an error item as a crawl result.
- Leave the cursor unadvanced when a stream fails.
- Treat an unrecognised Apify error code as a transient failure.
- Surface the last failure kind on the admin crawl-target read model.
- Persist a source post's media type.
- Record that a post was truncated by the per-post event cap.

Pytest unit tests:
- Error-item classification: `not_found`; `no_items` with request errors;
  `no_items` without; an unknown code; a dataset mixing an error item with real
  posts; a dataset with no error item at all.
- Billable result count excludes error items, for a dataset that is only an
  error item and one that mixes both.
- `consecutive_failures` arithmetic across a failed run then a successful one.
- The migration is exercised by an up/down round trip, matching
  `tests/test_post_items_migration.py`'s existing shape.

Manual or integration checks:
- Re-run `downtownbeergarden_` against prod and confirm the run is now reported
  as blocked rather than empty. **Re-running costs Apify results** — one target,
  once, is enough; do not sweep all thirteen to watch the new labels appear.

## Acceptance Criteria
- A blocked scrape increments `consecutive_failures` and is visible as a
  distinct outcome in metrics and on the admin read model.
- A `not_found` handle is reported distinctly and does not silently keep
  retrying on a schedule.
- A genuinely empty stream is still `empty` and still resets the counter.
- No error item is counted by `CRAWL_RESULTS_TOTAL`, the monthly budget, or
  `last_run_cost_usd`.
- `source_media_type` is persisted for new sources.
- Cap-truncated posts are queryable.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None.
