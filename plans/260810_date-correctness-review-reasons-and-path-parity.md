# Date Correctness, Review Reasons, And Path Parity

## Branch
fix/date-correctness-review-reasons-and-path-parity

## Goal
Stop publishing confidently wrong dates, stop queueing events with no stated
reason, and make the shared-handle crawl path observable and honestly labelled
— it is a second pipeline today, instrumented differently from the first.

## Non-goals
- **Promotion and menu entities.** Separate, planned with the operator.
- **An `ends_at` column.** §B records which day an event *starts*; modelling a
  multi-day run is a data-model change and is deliberately deferred.
- **Merging the two crawl pipelines.** §D makes them agree on what they report
  and what they call things; collapsing them is a bigger change than this fix.
- **Changing the confidence floors or the caption pre-filter.**

## Evidence
All from the first real crawl of `@entreamigosobode` (77 results, $0.231,
`last_run_at=2026-08-10T20:17:03Z`), a handle mapping to **two** venues:
`Entre Amigos O Bode` and `Entre Amigos O Bode Espinheiro`.

### A. A post-anchored roll-forward invented 2027
One post, one flyer, four weeks of a school-holiday programme:

```
date_text                    starts_at      status
'01, 02 e 03 de julho'  ->   2027-07-03     accepted
'08, 09 e 10 de julho'  ->   2027-07-10     accepted
'15, 16 e 17 de julho'  ->   2026-07-17     accepted
'22, 23 e 24 de julho'  ->   2026-07-24     accepted
```

`_roll_forward` pushes a date earlier than the anchor into next year. The anchor
is the **post timestamp**, and the post was published mid-programme, so weeks 1
and 2 had already happened and were rolled a full year forward. Weeks 3 and 4
were still ahead and stayed put.

Internally consistent; externally wrong. Two events now sit in the catalog dated
**July 2027**, `accepted`, `confidence=0.98`, unflagged. This is the
silent-wrong-date class `260807_date-resolution-correctness.md` already fixed
once for `Sábado • 05/SET` — the same failure through a different door, and the
same reason it is expensive: nothing about the row says it is a guess.

Note the constraint the current resolver throws away: **all four dates come from
one post**, and three of the four resolve to 2026. A sibling date disagreeing
with its siblings by a year is the strongest available signal that the roll is
wrong.

### B. A three-day event became a one-day event
`'01, 02 e 03 de julho'` resolved to `2027-07-03` — the **last** date in the
range. The first two days are dropped and nothing records that a range existed.
This gap was known and theoretical (`05 e 06/09`); it is now in production data.

### C. Thirteen events queued with no reason
```
status           review_reason   venue    count
accepted         NULL            set        5
pending_review   NULL            NULL      13
```
Every queued event has a null `review_reason`, so the console shows "awaiting
confirmation" with nothing explaining what a human is being asked to decide.
This is the operator's standing complaint, unfixed: an unresolved venue is a
perfectly good reason and simply is not written down.

### D. The shared-handle path is a second pipeline
`_chain_shared_handle` (`app/services/instagram_crawl_service.py:702`) reuses
the promoter path's per-post pipeline (`_archive_post_images` / `_process_post`)
instead of `EventExtractionService.run()`. Verified against production metrics
after a completed run of a shared handle, in a single-process container with no
`PROMETHEUS_MULTIPROC_DIR`:

- `event_extraction_posts_total` — **registered, zero series**. Its only
  increment lives in `EventExtractionService.run()`, which this path never
  calls. The `kind` split added by
  `260810_post-kind-and-post-extraction-attribution.md` is therefore blind on
  this path. *Kind filtering itself does run* (`promoter_crawl_service.py:471`);
  only the counting is missing.
- `crawl_venue_attribution_total` — **registered, zero series**. Its only
  increment is `outcome="single_venue"` at `instagram_crawl_service.py:558`,
  which sits *after* the early return into the shared-handle branch and is
  unreachable for a shared handle. The `resolved` and `ambiguous` outcomes named
  in `260810_stream-dedupe-and-venue-attribution.md` §Error Handling **were
  never implemented**; that plan's own instruction to "watch `ambiguous`" cannot
  be followed.
- `events_total` — a Gauge over `events.event` by status, **zero series**,
  independently of all of the above.

Provenance is also mislabelled: this path stamps
`source_kind = 'promoter_post'` on posts from a **venue's own** account. It is
how the 13 unresolved events reach the queue at all, since
`list_events_awaiting_decision` keys the unresolved case on
`source_kind='promoter_post'`. The queue works by accident of a wrong label, and
the console renders venue posts as promoter posts.

## Current Behavior
A date earlier than its post rolls forward a year, silently and per-date. A date
range keeps only its last day. An event with an unresolved venue is queued with
no reason. The shared-handle path reports through a different metric set than
the single-venue path and labels venue posts as promoter posts.

## Desired Behavior
1. Never auto-accept a date whose year was inferred by rolling forward.
2. Resolve dates from one post coherently, rather than each in isolation.
3. Record the first day of a date range as the start, and flag the range.
4. Never leave an event `pending_review` with no reason.
5. Report the same outcomes from both crawl paths.
6. Label a venue's own post as a venue post, whichever path handled it.

## Implementation Approach

### A. Roll-forward becomes visible, and siblings agree
Two changes, both narrow.

**Flag the inference.** When `_roll_forward` moves a date across a year
boundary, mark the event `year_inferred` and keep it out of auto-accept. The
roll is a guess; today it is a guess wearing `confidence=0.98`. Do **not** stop
rolling — for a genuinely future-dated flyer it is usually right — just stop
presenting it as certain.

**Let siblings vote.** When one post yields several dated events, resolve them
as a set: if a date's rolled year disagrees with the year the majority of its
siblings resolved to, prefer the majority and flag it. Weeks 1–2 above would
have followed weeks 3–4 into 2026 instead of jumping to 2027.

Restrict sibling voting to dates from the **same post** — the evidence is that
one flyer describes one programme. Do not extend it across posts, where an
unrelated event would drag its year onto another.

### B. A range starts on its first day
Where the model returns several dates for one event (`01, 02 e 03 de julho`),
take the **first** as `starts_at` — an event starts on the day it starts — and
flag `date_range` so the queue shows a human that a multi-day run was collapsed.

Deliberately not adding `ends_at`: the operator has not asked for multi-day
modelling, and inventing a column changes the read model, the console and the
serving contract. Recording the right start and admitting the range exists is
the honest minimum.

### C. No queued event without a reason
Add `unresolved_venue`, set wherever an event is queued for want of a venue —
on both paths.

Then enforce the invariant centrally rather than by remembering to set it at
each call site: **an event may not be persisted as `pending_review` with a null
`review_reason`.** Give it a fallback reason at the reconciliation boundary and
assert it in tests. Every place that queues an event has now failed to state a
reason at least once; a rule that has been broken repeatedly needs to be
enforced in one place, not documented at several.

### D. Both paths report the same things
- Bump `event_extraction_posts_total` with its `kind` label from the per-post
  path too, so the event/non-event split is visible however a post arrived.
- Implement `resolved` and `ambiguous` on `crawl_venue_attribution_total` in
  `_chain_shared_handle`, as `260810_stream-dedupe-and-venue-attribution.md`
  §Error Handling specified. Keep `EVENT_VENUE_LINK_TOTAL` as well — it is
  finer-grained and already relied upon.
- Set the `events_total` gauge from wherever status counts are already computed,
  or delete it. A permanently empty gauge is worse than no gauge: it reads as
  "no events" to anyone building a dashboard on it. **State which was chosen and
  why in the PR.**

**Provenance.** Stamp `source_kind='venue_post'` for a post from a venue's own
handle, whichever path handled it. This **will** drop those events out of
`list_events_awaiting_decision`, whose unresolved branch keys on
`promoter_post` — so widen that predicate to queue *any* unresolved-venue event
in the same change. Sequence the two edits together and prove it with a test
that a shared-handle venue post with no venue still appears in the queue;
getting this half-right silently empties the operator's queue, which is the
worst outcome available here.

## Data, Config, And API Impact
- **No migration.** `review_reason` is existing free text; the new values are
  data, not schema.
- **No back-fill.** The two July-2027 rows keep their dates until re-extracted
  or corrected by hand; say so in the PR so it is not read as the fix failing.
- **API:** none. `review_reason` already ships in `EventOut`.
- **Rollback:** revert. Nothing is written that the previous code cannot read.

## Error Handling And Observability
`event_extraction_posts_total{kind}` and `crawl_venue_attribution_total{outcome}`
become meaningful on both paths — that is most of §D.

**Watch `year_inferred` and `ambiguous` after deploy.** If `year_inferred`
dominates, the sibling rule is not doing its job and dates are being flagged
rather than fixed; if `ambiguous` dominates, attribution needs better signal,
not a lower floor.

## Test Plan
Feature file: `tests/bdd/enrichment/date-correctness-and-path-parity.feature`

Scenarios:
- Flag an event whose year was inferred by rolling forward.
- Keep a rolled-forward event out of auto-accept.
- Resolve a post's sibling dates to the same year as the majority.
- Do not let one post's dates influence another post's.
- Start a multi-day event on its first day.
- Flag an event whose date range was collapsed.
- State a reason when an event is queued for want of a venue.
- Never queue an event with no reason at all.
- Queue an unresolved venue post from a shared handle.
- Label a venue's own post as a venue post on the shared-handle path.
- Count the kind split on the shared-handle path.
- Count resolved and ambiguous attribution on the shared-handle path.

Pytest unit tests:
- `_roll_forward` across a year boundary, flagged; within the same year, not.
- Sibling voting: majority 2026 pulls an outlier back; a genuine single-date
  post is unaffected; a two-date tie is left flagged rather than guessed.
- The exact production case — the four `FÉRIAS AMIGOS PARK` date texts anchored
  to a mid-programme post timestamp — resolving all four to 2026.
- A date range yields the first day; a single date is unchanged.
- The `pending_review` + null `review_reason` invariant, asserted over every
  code path that can queue an event.
- The queue predicate returns an unresolved `venue_post`, proving §D's two
  halves landed together.
- Both metrics carry the expected labels after a shared-handle run.

## Acceptance Criteria
- No event is auto-accepted on an inferred year.
- The four `FÉRIAS` events resolve to 2026 when re-extracted.
- A multi-day event starts on its first day and says the range was collapsed.
- No event can be `pending_review` with a null reason.
- An unresolved shared-handle venue post is still queued, and is labelled a
  venue post.
- Both crawl paths emit the kind split and the attribution outcomes.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None. If §D's provenance change cannot be made without emptying the queue, stop
and report rather than shipping half of it.
