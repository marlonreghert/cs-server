# Multi-Event Posts — one Instagram post can announce several events

## Branch
feature/multi-event-posts

## Goal
Let one Instagram post produce more than one event. A city-listings account
packs several events — at several different venues — into a single caption and
carousel, and the extractor currently keeps the first and silently discards the
rest.

## Non-goals
- **Slide-to-event alignment.** Deciding *which carousel image* belongs to
  *which* extracted event is a separate, harder problem. This plan attributes a
  cover photo per event only where the model states one; otherwise the post's
  flyer stands in.
- **Changing the resolution ladder.** Each event resolves through the existing
  ladder unchanged; there are simply now several per post.
- **Serving events to the app.** Still admin-only.
- **Back-filling already-extracted posts.** Re-extraction is an operator-
  triggered run, not a migration.

## Evidence

Observed live on 2026-08-06 against `@recifequecabenobolso`, the account the
operator nominated as a representative promoter. Its posts are **daily
roundups**, not single-party promos:

> "Quarta (05) com exibição d**O Homem do Fraque Verde no Cinema São Luiz**,
> **Adilson Ramos no RioMar**, **Khrystal no Terra** e muito mais"

Run through the merged pipeline, that post produced exactly one event:

```
title    = 'O Homem do Fraque Verde'
location = 'Cinema São Luiz'      -> ven_saoluiz, auto, 1.000 vs 0.522
date     = 'Quarta (05)'          -> 2026-08-05 (correct)
```

Correct, and two-thirds lost. `Adilson Ramos no RioMar` and `Khrystal no Terra`
are never recorded — no event, no review queue entry, no counter. **The loss is
invisible**, which is the part that matters: the run reports one extracted event
and success.

Scale of the archetype, measured on the same five posts: **51 images**
(15, 8, 2, 9, 17). Each carousel slide is, in general, a different event.

Two further facts from that run shape this plan:

- **No `@`-mentions at all** in those captions. Rung 1 of the resolution ladder
  — the free, exact identity path — never fires for this account, so every
  event falls to fuzzy name matching. More events per post therefore means more
  fuzzy links, and the auto-link gates carry proportionally more weight.
- Venue names are buried in prose ("no Cinema São Luiz", "no RioMar"), not in a
  location field.

**The current schema forbids this.** `0023_event_table` declares
`CONSTRAINT uq_event_source UNIQUE (source_handle, source_shortcode)`, and that
constraint is deliberate — it is what makes re-extraction idempotent rather
than duplicating an event on every run. A second event from the same post
violates it. This is a contract change, not a bug fix, which is why it is its
own plan.

## Current Behavior
`OpenAIEventExtractionClient.extract` returns one event object per post and
`EventExtractionService._extract_one` writes exactly one row, keyed
`(source_handle, source_shortcode)`. Additional events in the same caption are
discarded with no record and no metric.

## Desired Behavior
1. Extract a **list** of events from one post; a single-event post yields a list
   of one, so no account needs to be flagged as a special archetype.
2. Give each event a stable per-post ordinal so several rows can share a post
   while re-extraction stays idempotent.
3. Resolve each event's venue independently through the existing ladder — one
   post may legitimately span three venues.
4. Resolve each event's date independently against the post timestamp.
5. Keep an operator's `confirmed` or manually-linked event intact when the post
   is re-extracted, even if the model's ordering of the list changes.
6. Remove an event that a later extraction no longer finds **only** when it was
   never confirmed or manually linked.
7. Count how many events a post yielded, so a post silently collapsing to one
   is visible.

## Implementation Approach

### A. The identity problem is the whole design
A per-post ordinal (`source_event_index`) is the obvious key, and on its own it
is wrong: the model does not guarantee list order between runs, so event #2
today may be event #3 tomorrow, and an operator's confirmation would silently
migrate to a different event.

So the row identity is a **content-derived discriminator**, not a position:
a stable hash over the event's distinguishing content (normalised title plus
resolved date). The ordinal is stored for display order only and is never part
of the key.

New constraint: `UNIQUE (source_handle, source_shortcode, source_event_key)`,
replacing `uq_event_source`. A single-event post is unchanged in spirit — it
simply has one key.

**Migration `0025_multi_event_posts` must back-fill `source_event_key` for
existing rows before adding the constraint**, or it fails on the first duplicate
NULL. Existing rows get a key derived the same way, so a re-extraction matches
the row it already wrote rather than inserting a second one. Getting this order
wrong turns an upgrade into a duplicate-event event.

### B. Extraction returns a list
The prompt asks for every distinct event in the caption and flyer, each with its
own title, date text, time text, location text and lineup. The response schema
becomes `{"events": [...]}`.

**Output budget must scale with the event count**, and this is the exact hazard
the repo has already been burned by: §4 of `docs/venue-retrieval-storage.md`
records a flat `max_tokens` truncating a variable-length response so the JSON
failed to parse and the *whole batch* fell back. A 17-event roundup is a far
longer answer than a one-party flyer. Budget generously, detect truncation via
`finish_reason`, and on truncation record the post as `extraction_failed`
keeping whatever raw text arrived rather than persisting a half-parsed list.

Note `gpt-5.6-luna` is a reasoning model whose reasoning tokens count against
`max_completion_tokens`, so the headroom needs to cover both.

### C. Per-event resolution
Each extracted event runs the existing date resolver and the existing venue
ladder independently. Nothing in either changes. The only new rule: an event
whose `location_text` is absent inherits nothing — it resolves as `unresolved`
rather than borrowing its sibling's venue, because "the other event in this post
was at the Cinema" is not evidence about this one.

### D. Disappearing events
An event previously extracted from a post and absent from a later extraction is
marked `superseded` (the status already exists and is currently unused) — never
hard-deleted, and never touched at all if it is `confirmed` or manually linked.
The operator's record outranks the model's second opinion, consistent with how
`_preserve_confirmed` already behaves.

## Data, Config, And API Impact
- **Migration:** `0025_multi_event_posts` — add `source_event_key` and
  `source_event_index` to `events.event`, back-fill both, drop
  `uq_event_source`, add the three-column unique constraint. Downgrade
  reverses in the mirror order and must refuse to run if any post has more than
  one event, since collapsing them would silently destroy rows.
- **API:** `GET /admin/events` gains the two fields; the review queue may show
  several events sharing a permalink. Additive.
- **Settings:** `event_extraction_max_events_per_post` as a sanity bound.
- **Serving:** none.

## Error Handling And Observability
A single malformed event inside an otherwise-valid list is skipped and counted,
not fatal to its siblings — the failure isolation the pipeline already applies
per venue, applied per event.

Metrics:
- `event_extraction_events_per_post` histogram. A listings account collapsing
  back to one event per post is exactly the regression this feature exists to
  prevent, and only a distribution makes it visible.
- `event_extraction_posts_total{outcome="truncated"}` distinct from
  `extraction_failed` — a truncated response means the budget is too small,
  which is a different fix from a model error.

## Test Plan
Feature file: `tests/bdd/enrichment/multi-event-posts.feature`

Scenarios:
- Extract three events from the real observed caption ("Quarta (05) …
  Cinema São Luiz … RioMar … Terra") and persist three rows against one post.
- Resolve each of those three to a different venue independently.
- Resolve each event's date independently against the post timestamp.
- Yield exactly one event, unchanged, from a single-party flyer.
- Re-extract a post and produce no duplicates when the model returns the events
  in a different order.
- Preserve a confirmed event across a re-extraction that reorders the list.
- Preserve a manually-linked event across re-extraction.
- Mark an event `superseded` when a later extraction no longer finds it.
- Never supersede a confirmed or manually-linked event that disappears.
- Leave an event with no location text unresolved rather than inheriting a
  sibling's venue.
- Record `truncated` and persist no partial list when the response is cut off.
- Skip one malformed event and keep its valid siblings.

Pytest unit tests:
- `source_event_key` stability: same content across runs, different for
  different titles, unaffected by list position, and stable when the title's
  case or accents differ.
- The migration's back-fill produces exactly one key per existing row and the
  constraint applies cleanly afterwards — the ordering trap in §A.
- Output budget scales with event count; truncation detection via
  `finish_reason`.
- Per-event failure isolation.
- Downgrade refuses when any post holds more than one event.

Manual or integration checks:
- Re-run the live `@recifequecabenobolso` extraction (payload already captured)
  and confirm the 08-05 post yields the three named events at three venues.
  That post is the reason this plan exists and is the acceptance case.

## Acceptance Criteria
- One post can persist several events, keyed stably by content, not position.
- A single-event post behaves exactly as it does today.
- Re-extraction is idempotent across a reordered list.
- Confirmed and manually-linked events survive re-extraction and disappearance.
- A truncated response persists nothing partial and is counted as its own
  outcome.
- The existing `uq_event_source` behaviour is migrated without duplicating a row.
- `make test-feature`, `make test-unit` and `make test-bdd` pass.

## Open Questions
None blocking. Whether a carousel slide can be aligned to its specific event is
explicitly out of scope; the per-event cover photo falls back to the post's
flyer until that is worth solving on its own.
