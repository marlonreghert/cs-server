# Post Kind, And Attribution After Extraction

## Branch
feature/post-kind-and-post-extraction-attribution

## Goal
Stop treating every qualifying post as an event: have the model say what a post
actually is — an event, a promotion, a menu item, food, or something else — and
keep the non-events out of the event flow. And move venue attribution to *after*
extraction, so it can use the model's own reading of where the event is instead
of guessing from a raw caption.

## Non-goals
- **Changing the caption pre-filter.** `event_caption_matcher` stays as it is —
  see §A for why tightening it is the wrong lever.
- **Serving any of this to the app.** Admin-only, as with every event field.
- **A menu or promotions product.** This separates and records them; it builds
  nothing on top.
- **Re-extracting history.** No back-fill; see §Data.
- **Changing the crawl, the cursors, the caps or the streams.** All settled in
  `260810_stream-dedupe-and-venue-attribution.md`.

## Evidence

### A risotto became an event
From the first real crawl of `@entreamigos.praia`, persisted as
`evt_01KZNV269SFM9H5YTGMCW0JKH7`, `status=pending_review`, `confidence=0.98`:

```json
{ "title": "Especial do dia",
  "is_recurring": true, "recurrence_text": "de segunda a sexta",
  "time_text": "das 11h às 16h", "date_text": null,
  "description": "Risoto preparado com arroz carnaroli, cogumelos frescos e
                  secos salteados, creme de queijo curado e manteiga.
                  Exceto feriados." }
```

That is a weekday lunch special, not an event.

**Why it qualified.** The run classified zero images as `flyer` (24
`food_drinks`, 6 `crowd`, 5 `interior`), so the caption gate decided alone.
`_WEEKDAY_TIME_RE` in `app/services/event_caption_matcher.py` matches a weekday
within 40 characters of a time — `"de segunda a sexta … das 11h às 16h"` matches
squarely.

**Why tightening the matcher is the wrong fix.** "Weekday near a time" is also
exactly how a real event reads ("sexta 22h"). Narrowing it trades false
positives for false negatives, and a missed event is the more expensive error —
it is invisible, while a menu item in the queue is merely noise. The matcher is
a free pre-filter over cached text and should stay permissive.

**Where the judgment belongs.** `EXTRACTION_PROMPT` tells the model a post
announces an event and asks it to extract one. It is never given the option to
say "this is a lunch menu", so it dutifully produced a high-confidence event.
The model already reads the caption and the flyer image; asking it what the post
*is* costs a few tokens and puts the judgment where the evidence is.

This gap was predicted:
`260808_event-ticket-info-and-attractions.md` parked promotions as *"an offer,
not an attraction… Flagged as a possible third gap, not designed for here."*

### Attribution runs before the evidence exists
`260810_stream-dedupe-and-venue-attribution.md` resolves a shared handle's venue
at **archive** time, which is before extraction — so the only signal available
is the raw caption. Its executing agent measured the consequence: a caption must
closely echo a venue's own name to clear the 0.55 floor, so a post that merely
mentions a branch in passing queues as unresolved.

That is safe but weak, and the better signal already exists one step later:
extraction produces `location_text`, the model's own reading of where the event
is, which the promoter path already resolves against the catalog. Attribution is
simply happening at the wrong moment.

### The resolver ignores the recurrence the model found
`_detect_recurrence` (`app/services/event_date_resolver.py:295`) inspects only
`date_text`, and matches recurrence solely on `toda`/`todo`
(`_RECURRING_MARKER_RE`, `:72`). The risotto post carried
`recurrence_text="de segunda a sexta"` and `date_text=null`, so the resolver saw
nothing, produced no `starts_at`, and flagged `missing_date`.

**The model was right and the resolver never read the field it filled in.** This
is the same shape as the `Sábado • 05/SET` defect: a component blamed for the
model's output when the model was correct.

**Included here deliberately, though the operator asked only for the first two
items.** It is three lines from the same defect, in the same code path, and it
governs recurring weekly programming — exactly what
`@entreamigosobode`'s jazz and samba nights are. Shipping kind-classification
without it would leave every "de sexta a sábado" event flagged as dateless.

## Current Behavior
Every post clearing the caption gate becomes an event, whatever it actually is.
A shared handle's venue is guessed from the raw caption before the model has
read the post. A recurrence the model identified is ignored unless it happens to
be phrased with "toda".

## Desired Behavior
1. Classify each extracted post as an event, promotion, menu, food, or other.
2. Persist the classification, including for non-events.
3. Keep non-events out of the review queue and out of the event flow.
4. Let an operator correct a misclassification.
5. Attribute a venue after extraction, using the model's `location_text`.
6. Keep the post archived under the handle until attribution decides.
7. Resolve a recurrence the model reported, including weekday ranges, and stop
   flagging such an event as dateless.

## Implementation Approach

### A. `kind` — asked of the model, with a stated precedence
Both prompts gain a required `kind`, one of:

| kind | what it means |
|---|---|
| `event` | a happening at a time — show, party, DJ night, live music |
| `promotion` | an offer or price advantage — happy hour, birthday freebie |
| `menu` | a dish or menu announcement, including a daily special |
| `food` | food or drink imagery with no offer and no event |
| `other` | anything else — staff, decor, hiring, closure notices |

**The precedence rule must be in the prompt, not left to the model's taste.**
These overlap constantly in real captions: a risotto special at a stated price
on weekdays is simultaneously a dish, an offer and a recurring weekly thing.
Without an explicit order the same caption classifies differently run to run,
and the review queue's contents become a coin flip. State it as: *a happening
with a date or recurring schedule → `event`; else an offer or price advantage →
`promotion`; else a named dish or menu → `menu`; else food or drink imagery →
`food`; else `other`.*

That rule deliberately puts `event` first, so a genuine event advertised with a
drinks offer is still an event.

**An unrecognised or missing `kind` is treated as `event`.** The queue is where
a human already looks; an unknown value must fail toward being seen, never
toward being filtered out of sight.

### B. Only an event enters the event flow
Persist `kind` on `events.event` for every extracted post, including non-events.

**Persist rather than discard.** Discarding is cheaper and wrong: the operator
asked to *separate* these, which implies seeing them, and a misclassification
that is thrown away is one nobody can find or correct. The row also records what
the pipeline decided, which is the only way to judge whether the classifier is
any good.

Non-`event` rows are excluded from `list_events_awaiting_decision`, and their
status is set so they never read as awaiting a person. `kind` joins the
operator-editable fields, so correcting one to `event` puts it back in the flow
through machinery that already exists (`operator_edited_fields`).

**The queue predicate is the only filter.** Do not also filter in the console —
that is how the review queue and the Events tab drifted apart before.

### C. Attribution moves after extraction
Archive under the handle. Once extraction has produced `location_text`, resolve
the venue from it — falling back to the caption when the model reported none —
against the handle's own venues, using the same bounded-candidate resolver
`260810_stream-dedupe-and-venue-attribution.md` introduced.

A handle mapping to exactly one venue still attributes to it directly, with no
resolver call and no behaviour change. That is the overwhelmingly common case
and it must stay identical.

**Leave the archived object where it was written.** Do not copy or move S3
objects once a venue resolves: the promoter path already stores under a handle
prefix and the console renders those covers correctly today, so a handle-prefixed
key is a supported shape, not a compromise. Moving objects would cost a copy per
image and invalidate `cover_photo_key`s already handed out.

**Unresolved still means unresolved.** Below the gates, no venue and a queued
event — unchanged. Better signal should raise the resolve rate, not lower the
bar.

### D. Recurrence, read from the model
`resolve_event_datetime` takes the model's `is_recurring` and `recurrence_text`
alongside `date_text`, and `_detect_recurrence` reads the recurrence text as well
as the date text. Extend it beyond `toda`/`todo` to the forms Brazilian venues
actually use: a weekday range (`de segunda a sexta`, `de sexta a sábado`), a
list (`sextas e sábados`), and a plural weekday (`todas as sextas`, `sextas`).

A recurring event with a resolvable schedule resolves to its **next occurrence**
and is **not** flagged `missing_date`. A recurrence phrase that cannot be parsed
keeps today's behaviour — no date, flagged — because an unparsed recurrence is
exactly the case where a guess would be invented.

**Do not widen the bare-weekday path.** `260807_date-resolution-correctness.md`
narrowed the weekday fallback on purpose, to stop a weekday overriding an
explicit date. This adds a *recurrence* reading of weekday text, gated on the
model having said `is_recurring`; it must not re-open the one-off path.

## Data, Config, And API Impact
- **Migration `0034_event_kind`** from head `0033_crawl_target_reels_overlap`:
  add `kind text` to `events.event`, nullable, no default.
- **No back-fill.** Existing rows predate the question; NULL is historically
  accurate. NULL is read as `event`, per §A's fail-toward-visible rule, so no
  existing row silently vanishes from the queue.
- **The risotto row stays an event** until an operator rejects it or its post is
  re-extracted — and it will not be, since its stream's cursor has moved past it.
  Worth stating so it is not mistaken for the fix failing.
- **API:** `EventOut` gains `kind`; `EventPatch` gains `kind`. Additive.
- **Queue:** `list_events_awaiting_decision` gains a kind predicate.
- **Serving:** none.
- **Rollback:** revert and drop the column. Non-events return to the queue,
  which is noisy but not lossy.

## Error Handling And Observability
Metrics: `event_extraction_posts_total` gains a `kind` label, so the split
between events and everything else is visible from the first run.

**Watch the `event` share.** If nearly everything still classifies as `event`,
the prompt's precedence is not landing; if almost nothing does, the classifier is
eating real events — and that failure is silent, because a misclassified event
never reaches the queue where someone would notice.

## Test Plan
Feature file: `tests/bdd/enrichment/post-kind-and-post-extraction-attribution.feature`

Scenarios:
- Classify a daily lunch special as a menu item, not an event.
- Classify a happy-hour offer as a promotion.
- Classify a DJ night as an event.
- Classify a plain food photo as food.
- Classify an event advertised alongside a drinks offer as an event.
- Keep a non-event out of the review queue.
- Show a non-event in the events list with its kind.
- Treat a missing or unrecognised kind as an event.
- Let an operator correct a menu item to an event and see it queue.
- Attribute a venue from the model's location text after extraction.
- Fall back to the caption when the model reported no location text.
- Attribute directly when the handle maps to one venue.
- Queue an event with no venue when neither signal resolves.
- Resolve a weekday-range recurrence to its next occurrence.
- Resolve a weekday-list recurrence to its next occurrence.
- Keep flagging a recurrence phrase that cannot be parsed.
- Never let a recurrence reading resolve a one-off weekday post.

Pytest unit tests:
- The precedence rule across captions that satisfy two kinds at once.
- Both prompts contain `kind` and its precedence — asserted directly, since
  extending only the multi-event prompt is the likely half-fix.
- NULL and unknown `kind` both read as `event`.
- The queue predicate excludes each non-event kind and includes NULL.
- Attribution: model location text present, absent, single-venue handle, and a
  case just below the confidence floor.
- The single-venue path is unchanged — asserted against pre-change behaviour.
- `_detect_recurrence` over: `toda quinta`, `de segunda a sexta`,
  `de sexta a sábado`, `sextas e sábados`, `todas as sextas`, prose with no
  recurrence, and a one-off `sábado` with `is_recurring` false.
- A recurring event with a resolvable schedule is not flagged `missing_date`.
- The migration adds the column nullable and its downgrade drops exactly it.

Manual or integration checks:
- Crawl `@entreamigosobode` and confirm its jazz and samba nights classify as
  events with resolved recurrences, while food posts classify as food or menu
  and stay out of the queue.

## Acceptance Criteria
- Every extracted post carries a kind, and the precedence rule is in both
  prompts.
- Non-events are persisted, excluded from the queue, and correctable.
- A missing or unknown kind is treated as an event.
- Venue attribution uses the model's location text, after extraction.
- A single-venue handle behaves exactly as before.
- A weekday-range or weekday-list recurrence resolves and is not flagged
  dateless.
- The narrowed one-off weekday path is not re-widened.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None.
