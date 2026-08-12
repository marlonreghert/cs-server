# Events Per Post Cap — measure what we are throwing away before changing the number

## Branch
chore/events-per-post-cap

## Goal
Know how many events the model actually offers on a capped post, and how many
output tokens it spends doing it, before anyone touches `20`. Then move the two
knobs that are currently welded together — the number of events we **keep** and
the output-token budget we **allow** — independently, because they control
different things and pull in opposite directions.

## Non-goals
- **Raising the cap in this plan's first commit.** The number comes out of the
  histogram, not out of a plan. §D fixes the decision rule in advance so nobody
  relitigates it once the data lands.
- **Telling the model a limit in the prompt.** That trades a data loss we can
  measure for one we cannot. See §B.
- **Changing what `OUTCOME_TRUNCATED` means.** It means the API cut the response
  off (`finish_reason == "length"`) and the whole post was discarded.
  `260812_crawl-error-visibility.md` §D already warns against overloading it,
  and this plan does not.
- **Crawl-level error handling, media type, attribution, dates.** Separate
  plans, `260812_crawl-error-visibility.md` and
  `260812_event-attribution-and-dates.md`.
- **Admin-UI surfacing** of truncated posts in vibes_bot.

## Evidence

### The cap is biting, on more posts than the sibling plan recorded
In the 2026-08-12 snapshot (636 items, 660 sources, 115 distinct posts),
`post_item_source.source_event_index` reaches exactly **20** on **20 distinct
posts**, every one of them from `oquetemhojeemnatal` — the promoter account whose
roundups list a night's programme for the whole city. That account produced 55
posts in the snapshot; the per-post live-item distribution is:

```
items kept:  0  1  2  3  5  8 12 14 15 16 19 20
posts:      10 13  2  1  1  1  2  2  2  3  2 16
```

Sixteen posts sit at exactly 20 kept items, and four more reach index 20 with
fewer surviving rows (duplicate `source_event_key`s inside one post collapse, per
`reconcile_post_events`'s own de-duplication). Twenty posts touched the ceiling.

**`260812_crawl-error-visibility.md` §D records this as 14 posts, and the number
is not reproducible from the snapshot.** Counted five different ways — any
source at index 20 (**20**), posts with exactly 20 source rows (**17**), posts
with 20 live items (**16**), posts at index 20 with every row live (**20**),
distinct shortcodes at index 20 (**20**) — nothing yields 14. That discrepancy is
not a footnote; it is the argument for this plan's shape. **`source_event_index`
is not a truncation signal.** It is an ordinal that survives de-duplication,
supersession and cross-post merging, so counting it after the fact gives a
different answer depending on which join you write. Only an explicit signal
recorded at parse time can be trusted, which is exactly what §D adds.

### The cap costs nothing to raise, because we have already paid for what it drops
`parse_multi_event_extraction_response` applies the cap at **parse time**:

```python
if max_events:
    events_raw = events_raw[:max_events]
```

`app/api/openai_event_extraction_client.py:523`. And the prompt
(`_build_multi_event_extraction_prompt`, same file, line 198) tells the model the
opposite of a limit:

> Extract EVERY distinct event the post announces, however many there are.

So on a 26-event roundup the model reads 26, generates 26, we are billed for 26,
and we then delete six of them in Python. The 21st through 26th events are
already bought. Raising the keep-cap recovers data we have paid for and adds
**no OpenAI spend at all**.

### The knob that does cost money is the other one
`compute_multi_event_max_completion_tokens(max_events)` (same file, line 286) is
`2304 + 550 × max_events`, so `max_events = 20` yields a **13,304**-token
`max_completion_tokens`. That is a *ceiling*, not a charge — the API bills tokens
generated, not tokens permitted. Raising it does not raise the bill either,
except in the one case where it lets a response that would have been cut off run
to completion. And that case is currently a **total loss**: `finish_reason ==
"length"` sets `truncated`, `event_extraction_service.py:803` (and its twin,
`promoter_crawl_service.py:490`) discards the entire post, and the tokens are
paid for regardless. Every truncated post is money spent on nothing.

Two facts follow, and they invert the usual framing:

- The keep-cap is a **data-loss** knob with no cost effect.
- The token budget is the **cost-and-risk** knob, and raising it converts
  "paid and discarded the whole post" into "paid and kept it".

Welding them together means raising the keep-cap silently raises the ceiling,
and there is no way to raise the ceiling for a genuinely long roundup without
also keeping more events. Neither dependency is wanted.

### None of the 20 capped posts truncated
All 20 posts at index 20 produced parseable responses and `accepted` items, so
none hit `finish_reason == "length"`. The model emitted at least twenty events'
worth of JSON inside 13,304 tokens. That bounds the risk of raising the keep-cap
sharply — the current budget already covers ≥20 events — but it does not tell us
where the ceiling actually is, because nothing records per-post
`completion_tokens`.

### The counter that would price this is dead
`EVENT_EXTRACTION_COST_USD` is declared in `app/metrics.py:1206` with a careful
docstring about never guessing a per-post cost — and it is **never incremented
anywhere in the codebase**. The only live signal is
`OPENAI_TOKENS_TOTAL{endpoint="event_extract"}`, which is a cumulative
counter with no per-post distribution. So today, "what would raising the cap
cost?" is genuinely unanswerable from production, and any number in this plan
would be invented. That is the honest reason measurement comes first.

## Current Behavior
A post announcing more than twenty events is silently truncated to twenty, after
the model has generated and we have paid for all of them. Nothing records that
it happened, how many were dropped, or how close the response came to the token
ceiling. The keep-cap and the token ceiling move together whether or not that
makes sense.

## Desired Behavior
1. Record, per post, how many events the model **offered** before the cap.
2. Record, per post, how many output tokens the call actually spent.
3. Report a per-post cost derived from real token counts, not a guess.
4. Size the keep-cap and the token budget from those two distributions, by a
   rule written down before the data arrives.
5. Keep the two knobs independent.

## Implementation Approach

One commit per section on a single branch and PR, per the operator's standing
preference for phased multi-defect fixes.

### A. Count what the model offered, not what we kept
`260812_crawl-error-visibility.md` §D records **that** the cap truncated a post.
This plan needs **by how much**, and that number is free: it is
`len(events_raw)` before the slice at
`openai_event_extraction_client.py:523`, already in memory, already paid for.

`parse_multi_event_extraction_response` returns
`(events, malformed, malformed_attractions)`. Add the offered count to that
tuple — one more integer from a pure function, unit-testable on its own, and the
same shape the malformed counters already take. Both callers
(`event_extraction_service._process_post`, `promoter_crawl_service` around
`:503`) then have it without a second parse.

Observe it as a histogram, `EVENT_EXTRACTION_EVENTS_OFFERED_PER_POST`, beside
the existing `EVENT_EXTRACTION_EVENTS_PER_POST` (which observes what was
*persisted*, in `reconcile_post_events`). Two histograms, and the gap between
them is the answer this plan needs. Persist the overflow count on the post's
sources alongside §D's flag so the question is answerable in SQL as well as in
Prometheus — an operator asking "which posts, and how badly" should not need a
metrics query.

**Do not sidestep this with a bigger cap and a hope.** With a cap of 40, a post
offering 45 truncates just as silently as one offering 25 does today. The
instrument is the durable part of this plan; the number is not.

### B. Measure the token spend, and light up the dead counter
Observe `response.usage.completion_tokens` per call as a histogram, and
increment `EVENT_EXTRACTION_COST_USD` from the reported token counts — the
counter already exists, already has the right docstring, and has never been
wired. Price it from configurable per-1k input/output rates, exactly as
`photo_classification_service` already does
(`photo_classification_cost_per_1k_input_usd` /
`..._output_usd`, `app/config.py:513`) rather than inventing a second pricing
mechanism. §4 of `docs/venue-retrieval-storage.md` records a 9x cost-estimate
error on that exact path; the lesson taken there was "price from reported
tokens, never from a per-item guess", and it applies unchanged.

Rejected: **telling the model a maximum in the prompt.** It would cut generation
cost on long roundups, which is real. But it moves the truncation inside the
model, where nothing observes it: the model would silently pick twenty of
twenty-six with no record that six existed, and `len(events_raw)` — the only
number that can size the cap — would become permanently equal to the cap. It
also means two prompts to keep in step, and this repo has already had those two
prompts drift. Revisit only if the measured spend justifies it, with the
instrument already in place to notice what it costs us.

### C. Split the two knobs
Today one setting, `event_extraction_max_events_per_post` (`app/config.py:562`),
feeds both the parse cap and — through
`compute_multi_event_max_completion_tokens` — the output-token ceiling.

Introduce a second setting for the ceiling. Default it to
`compute_multi_event_max_completion_tokens(20)` = **13,304** so the shipped
behaviour is byte-identical on the day it lands, and let the keep-cap move
without dragging it. Keep `compute_multi_event_max_completion_tokens` as the
function that computes the default — it documents the relationship and its
per-event constants stay meaningful — but stop making it the only way the
ceiling can be set.

**Both service constants must stay in step.**
`event_extraction_service.DEFAULT_MAX_EVENTS_PER_POST` (`:202`) and
`promoter_crawl_service.DEFAULT_MAX_EVENTS_PER_POST` (`:118`) are deliberately
independent module defaults; production overrides **both** from the one setting
(`app/container.py:689` and `:750`), so a config change already moves both live
paths and only direct constructions — unit tests, ad-hoc scripts — see the
constants. Move them together anyway and add a unit test asserting they are
equal. This repo has been bitten four times by two copies of one rule; the list
is in `build_location_text_attribute_fn`'s docstring. A one-line equality
assertion is the cheapest possible guard against being bitten a fifth time.

Note which path actually bites: all 20 capped posts are `oquetemhojeemnatal`,
`kind = 'promoter'`, so `PromoterCrawlService` is the live offender. The venue
path shares the defect and must be fixed with it, not after it.

### D. The decision rule, fixed before the data lands
Run the instrumented build for **one full weekly crawl cycle** —
`oquetemhojeemnatal` posts daily and 20 of its 55 snapshot posts hit the cap, so
a week is a comfortably large sample and costs nothing extra (the crawl runs
anyway). Then, mechanically:

- **Keep-cap** = the observed maximum of `events_offered_per_post` over the
  window, rounded up to the next multiple of 10, floor 20.
- **Token ceiling** = the observed p99 of `completion_tokens` plus 50%, floor
  13,304.

Set both, in config, in one commit, quoting the measured numbers in the commit
message. Nothing else is decided by argument.

**If the maximum is a long tail driven by one account, do not raise the global
cap.** `crawl_target` already carries per-target limits (`seed_results_limit`,
`reels_results_limit`, `reels_seed_results_limit` — migrations 0031/0032), so a
per-target `max_events_per_post` is the cheap, no-classifier version of "detect
a roundup and handle it differently", and it keeps the blast radius on the one
account whose posts are unlike everyone else's.

Rejected alternatives, on the record:

- **Paginate a post's extraction across calls.** It re-sends the flyer image and
  the full caption on every call, and input tokens dominate a vision request —
  so the second call costs roughly what the first did, to recover events we
  currently get for free by simply not deleting them. It also needs the model to
  reliably continue from "event 21", which it has no mechanism to guarantee, and
  a response whose contents depend on call ordering re-opens exactly the
  instability `source_event_key` exists to defeat. Pay twice for a less stable
  answer.
- **A roundup classifier.** A model call, or a heuristic, to decide a post is a
  roundup and route it elsewhere. This is a larger machine than the problem: we
  already know which account it is, and the per-target override above gets the
  same outcome for the cost of one nullable column.
- **Just set it to 40.** Likely the right destination and the wrong first move —
  40 is as unevidenced as 20, and without §A nobody would ever learn whether 40
  was also too low.

## Data, Config, And API Impact
- **Migration** — a nullable integer on `events.post_item_source` for the
  offered-event count, folded into whichever migration
  `260812_crawl-error-visibility.md` §D adds for its cap-truncation flag. **The
  two must land in one migration, not two**: they are the same fact recorded at
  the same moment, and splitting them across branches invites a duplicate
  column. Whichever branch lands second extends the other's migration.
- **Config** — a new output-token-ceiling setting, defaulted to 13,304;
  per-1k input/output pricing for event extraction, mirroring the photo
  classification settings; later, the measured value of
  `event_extraction_max_events_per_post`.
- **API** — none. No admin surface changes.
- **Metrics** — `EVENT_EXTRACTION_EVENTS_OFFERED_PER_POST` (new histogram), a
  completion-token histogram, `EVENT_EXTRACTION_COST_USD` (existing counter,
  finally incremented).
- **Rollback:** revert. The new column is nullable; the ceiling setting's
  default reproduces today's arithmetic exactly.

## Error Handling And Observability
- The two histograms are the deliverable. **Watch the gap** between
  `events_offered_per_post` and `events_per_post`: it is the only number that
  says how much of the city's programme we are deleting, and it is invisible
  everywhere else.
- Log at warning when the cap drops entries, with the handle, the shortcode, the
  offered count and the cap. One line per truncated post, ~20 per snapshot — a
  volume an operator can read.
- Watch `event_extraction_posts_total{outcome="truncated"}`. It is currently the
  only symptom of a too-small ceiling and it costs a whole post each time. If it
  is non-zero after the ceiling is raised, the ceiling is still too small.
- Because a Prometheus series only exists after its first increment, the
  **absence** of a truncated series is itself the evidence that the ceiling is
  adequate — keep it a distinct outcome rather than folding it into a generic
  failure label.

## Test Plan

Feature file: `tests/bdd/enrichment/events-per-post-cap.feature`

Scenarios:
- Record how many events a post offered when the cap dropped some of them.
- Record an offered count equal to the kept count when the cap did not bite.
- Keep every event of a post that offers exactly the cap, and record no
  overflow.
- Persist the offered count on the post's sources.
- Keep the output-token ceiling unchanged when only the keep-cap is raised.
- Keep the keep-cap unchanged when only the output-token ceiling is raised.
- Discard the whole post and record the truncated outcome when the API cuts the
  response off, unchanged from today.
- Report a per-post extraction cost derived from the reported token counts.

Pytest unit tests:
- `parse_multi_event_extraction_response` returns the offered count: for a
  response under the cap, exactly at it, and over it; and for a response mixing
  malformed entries with real ones, asserting the offered count includes the
  malformed ones (they were generated and paid for) while `malformed` still
  counts them separately.
- `compute_multi_event_max_completion_tokens` is unchanged, and the new ceiling
  setting's default equals `compute_multi_event_max_completion_tokens(20)` =
  13,304 — pinned as a literal, so a future edit to either constant has to
  confront the other.
- The two `DEFAULT_MAX_EVENTS_PER_POST` constants are equal.
- Raising the keep-cap in settings does not change the ceiling passed to the
  API client, and vice versa — asserted on the actual kwargs the fake client
  receives, on **both** the venue and the promoter path.
- Cost accounting: a known `usage` payload produces a known USD increment, and a
  response with no `usage` increments nothing rather than guessing.

Manual or integration checks:
- After deploy, read `event_extraction_events_offered_per_post` from
  `/metrics` once a full weekly cycle has run, and record the maximum and the
  p99 completion tokens **in the PR that changes the numbers**. No re-crawl is
  needed — the scheduled crawl produces the sample. Do not trigger extra runs to
  fill the histogram faster; each one costs Apify results and OpenAI calls.

## Acceptance Criteria
- Every extraction records how many events the model offered, in metrics and on
  the post's sources.
- Every extraction call records its own completion-token count, and
  `EVENT_EXTRACTION_COST_USD` is no longer a counter that never increments.
- The keep-cap and the output-token ceiling are separately configurable, and the
  shipped defaults reproduce today's behaviour exactly.
- Both `DEFAULT_MAX_EVENTS_PER_POST` constants are equal and a test says so.
- The cap is changed only in a commit that quotes the measured distribution, per
  §D's rule.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None.
