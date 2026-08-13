# Crawl Transport Failure Visibility — a timeout is not an empty account either

## Branch
fix/crawl-transport-failure-visibility

## Goal
A crawl that failed at the HTTP layer must be reported as a failure, exactly as
`260812_crawl-error-visibility.md` made a dataset error item a failure. Today it
is still recorded as an empty account.

## Non-goals
- **Re-litigating the dataset error-item classification.** `260812_crawl-error-
  visibility.md` shipped and is verified working in production (see Evidence).
  This plan closes the hole *beside* it.
- **Retrying inside a run.** Apify already retries internally. A second retry
  loop on top is not the fix.
- **Recovering the abandoned results.** The next run re-fetches; the cursor
  never advanced.

## Evidence

### The same symptom, a different door
`260812_crawl-error-visibility.md` fixed the case where Apify returns a
structured error item. Verified in production 2026-08-13: `champagne_recifee`
was classified `handle_not_found`, disabled, `consecutive_failures` incremented
to 1, the reason recorded on the target.

The same night, `downtownbeergarden_` — a public account with **121 posts** —
produced this:

```
DB:     enabled=True  last_failure_kind=None  consecutive_failures=0
        last_run_results=0  cursor_posts_at=NULL
Apify:  downtownbeergarden_ posts SUCCEEDED items=16  started 01:16:46
Log:    2026-08-13 01:18:46 ERROR [ApifyInstagram] Timeout for instagram_posts
        (no "Fetched N posts for @downtownbeergarden_" line — the call never returned)
```

Apify's run **succeeded with 16 posts**. Our client timed out at 120 s
(`ApifyInstagram.__init__`'s `timeout: float = 120.0`), two minutes after the
run started. Apify continued server-side, completed, and billed us for 16
results we never received.

The chain:
1. `_run_actor_sync` catches `httpx.TimeoutException`, logs, returns **`None`**
2. `fetch_recent_posts` hits `if not items: return FetchPostsResult(posts=[])` —
   **`error_code=None`**, indistinguishable from an empty dataset
3. `_run_stream` sees no error code and an empty `kept` → **`OUTCOME_EMPTY`**
4. `consecutive_failures` **reset to 0**, `last_failure_kind` left None, cursor
   left NULL, target reads healthy

That is precisely the defect `260812` was written to eliminate, reached through
the transport layer instead of the dataset. `FetchPostsResult`'s own docstring
records the gap honestly — "`.posts` is `[]` on a transport-level failure
(unchanged from before this dataclass existed)" — because the earlier plan only
scoped the error item. This plan is that plan's missing half.

### `cursor_posts_at` has never advanced for this target
It is still NULL, meaning the posts stream has **never once succeeded** since
the target was created. A target that has never produced a single post through
one of its streams is a distinct, louder condition than a target that failed
today, and nothing reports it.

### A speculation that the evidence did not support
While diagnosing this, I proposed that `no_items` with zero
`requestErrorMessages` might be a false "empty" — Apify exhausting sessions
without recording per-request errors. **The evidence contradicts that.**
`downtownbeergarden_`'s earlier failure carried 11 request errors and was
correctly classified `blocked`; tonight's was a timeout, a different mechanism
entirely. There is currently **no** observed case of a `no_items`-with-no-request-errors
result on an account known to have posts. Do not change that branch on this
plan's authority — it would need its own evidence.

### The timeout is also too short
A 20-post seed against this account exceeded 120 s. The run then succeeded
server-side, so the work was done and paid for; only the answer was thrown away.
The default was set for a general-purpose client, not for a seed that downloads
and returns twenty posts.

## Current Behavior
An Apify call that times out, or fails with an HTTP or transport error, is
recorded as an empty crawl: no failure, no reason, counter reset, results
billed and discarded.

## Desired Behavior
1. Report a transport failure as a failure, distinctly from an empty result.
2. Name the handle in the log line that reports it.
3. Give a seed long enough to finish before abandoning work already paid for.
4. Report a stream that has never once succeeded.

## Implementation Approach

One commit per section, one branch, one PR.

### A. Carry the transport failure through the client contract
`_run_actor_sync` already distinguishes failure (`None`) from an empty dataset
(`[]`); `fetch_recent_posts` collapses both into `posts=[]`. Stop collapsing
them.

Surface a transport failure on `FetchPostsResult` — reusing `error_code` with
values that cannot collide with Apify's own (`timeout`, `http_error`,
`request_error`) is acceptable and keeps one field to classify on, but say so
explicitly in the docstring: Apify owns `no_items`/`not_found`, we own these.
An empty dataset with no error item keeps `error_code=None`, which is the only
thing that still means "genuinely nothing".

### B. Classify it as a failure
Map the transport codes to `OUTCOME_FAILED` — the outcome that already exists
for a fetch exception, and already sits in `FAILURE_OUTCOMES`, so
`consecutive_failures` and `last_failure_kind` need no new wiring.

**Do not add a new outcome label for this.** A timeout and an in-process
exception are the same fact — the call did not answer — and splitting them
would fragment the metric for no operational gain. The log line carries the
detail.

### C. Put the handle in the transport log lines
`_run_actor_sync` logs `endpoint_label` (`instagram_posts`), which is identical
for every target, so a timeout cannot be attributed to a handle from logs
alone. This cost a full diagnostic round trip on 2026-08-13: grepping the logs
by handle returned nothing while the failure was sitting there under a shared
label.

Pass the username through and include it in the timeout, HTTP-error and
request-error lines. Same reasoning as
[the memory note on Apify run history]: our own logs must not be the thing that
hides the failure.

### D. Give a seed room to finish
Raise the Apify client timeout, and make it configurable rather than a
constructor default. A seed that has already been billed must not be discarded
at 120 s.

Pick the value from the observed distribution, not by guessing: the successful
seeds on 2026-08-12 ranged from a few seconds to over a minute, and this one
exceeded two. **300 s** is the starting proposal; state the measured basis in
the PR.

A timeout must remain possible — an unbounded wait would hang the whole cycle,
since streams run in sequence per target.

### E. Report a stream that has never succeeded
Surface, on the admin read model, that a stream's cursor is still NULL after N
runs. `cursor_posts_at IS NULL AND last_run_at IS NOT NULL` is already the
"never successfully seeded" signal this codebase uses; make it visible rather
than something an operator has to notice by reading two columns.

This is the condition that would have caught `downtownbeergarden_` months ago
without anyone needing to look at a screenshot.

## Data, Config, And API Impact
- **Migration** — none expected. §E is derived from columns that already exist;
  confirm before adding anything.
- **Config** — the Apify client timeout (§D), defaulting to the chosen value.
- **Admin API** — §E adds a derived field. Additive; the console is a released
  client and nothing may be removed.
- **Rollback:** revert. No schema, no data change.

## Error Handling And Observability
- `CRAWL_RUNS_TOTAL{outcome="failed"}` now covers transport failures. Because a
  Prometheus series only materialises after its first increment, the **absence**
  of `failed` for a target that keeps reporting `empty` is the tell this plan
  removes.
- `APIFY_API_ERRORS_TOTAL{error_type="timeout"}` already exists and already
  fired for this incident — it was never joined to a handle. §C fixes the log;
  consider whether the metric needs the handle too, weighing cardinality.
- **Watch the timeout rate after §D.** If raising the ceiling merely moves where
  runs pile up, the constraint is Apify-side and a longer wait is not the answer.

## Test Plan
Feature file: `tests/bdd/enrichment/crawl-transport-failure-visibility.feature`

Scenarios:
- Report a timed-out fetch as a failure, not as an empty account.
- Report an HTTP error from Apify as a failure.
- Keep treating a genuinely empty dataset as empty.
- Increment the failure counter when a fetch times out.
- Leave the cursor unadvanced when a fetch times out.
- Name the handle in the log line for a timed-out fetch.
- Surface a stream that has never successfully seeded.
- Keep the dataset error-item classification behaving exactly as it does today.

Pytest unit tests:
- `fetch_recent_posts` returns a transport error code when `_run_actor_sync`
  returns `None`, and `error_code=None` when it returns `[]` — the distinction
  this plan exists to preserve, asserted directly.
- Each transport exception type maps to its code.
- Apify's own `no_items`/`not_found` classification is unchanged, asserted
  against the cases `260812` pinned, so this plan cannot regress it.
- The never-seeded predicate: cursor NULL with no runs (not yet reportable),
  cursor NULL with runs (reportable), cursor set (never reportable).

Manual or integration checks:
- None that spend money. The production evidence for this defect already exists
  in the 2026-08-13 logs and Apify run history; do not re-run a crawl to
  reproduce a timeout.

## Acceptance Criteria
- A timeout increments `consecutive_failures` and records a failure kind.
- A genuinely empty dataset is still `empty` and still resets the counter.
- The handle appears in every Apify transport-error log line.
- The client timeout is configurable, and its value is justified from measured
  run durations.
- A never-seeded stream is visible on the admin read model.
- `260812_crawl-error-visibility.md`'s behaviour is provably unchanged.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
