# Post Kind, And Attribution After Extraction

## Branch
feature/post-kind-and-post-extraction-attribution

## Goal
Stop treating every qualifying post as an event: have the model say what a post
actually is — an event, a promotion, a menu item, food, or something else — and
keep the non-events out of the event flow. And move venue attribution to *after*
extraction, so it can use the model's own reading of where the event is instead
of guessing from a raw caption.

## Re-scoped mid-execution (post-approval correction)
**§B originally modeled `kind` as a discriminator column on `events.event`**
(persist every kind, exclude non-events from `list_events_awaiting_decision`
via a predicate). The operator corrected this during execution: *"event,
promotion and menus are expected to be different entities. And they both
comes from posts. Promotions can be related to events, but its not
mandatory. Can also be related to venue directly."* Events, promotions and
menus are separate entities with their own relationships — a promotion may
point at an event, at a venue directly, or at neither yet. One table with a
flag is the wrong shape for that, and building the promotion/menu entities
themselves is explicitly a separate, later effort the operator is planning
with more information than this plan has.

§B below reflects the corrected scope: **a post classified as anything other
than `event` produces no `events.event` row at all** — counted on the
`event_extraction_posts_total` metric's `kind` label and logged at a level
an operator can grep, then dropped. **No migration, no `events.event.kind`
column, no queue predicate change.** §A, §C and §D are unaffected by this
correction — see each section below.

Re-extraction reads archived posts back out of S3, never re-calls Apify, so
nothing is unrecoverable: once the promotion/menu entities exist, the posts
this plan drops today can be re-extracted into them without re-crawling or
re-paying for the crawl.

## Non-goals
- **Changing the caption pre-filter.** `event_caption_matcher` stays as it is —
  see §A for why tightening it is the wrong lever.
- **Serving any of this to the app.** Admin-only, as with every event field.
- **A menu or promotions product.** This tells a non-event apart from an event
  and stops it from becoming one; it builds no promotion/menu entity, no
  relationship to an event or a venue, and no operator workflow for either —
  all of that is separate, deliberately, and not decided here.
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
**Re-scoped mid-execution — see above.** A post whose classified `kind` is
`promotion`, `menu`, `food`, or `other` produces **no `events.event` row**.
Nothing is persisted for it in this table: no discriminator column, no
placeholder row, no queue-predicate exclusion to write, because there is no
row to exclude. `events.event` stays exactly what it already is — events
only.

**Counted and logged, not silently dropped.** `event_extraction_posts_total`
carries the outcome (a new `not_an_event` value, distinct from
`not_event_like` — that one means the post never reached the model at all;
this one means the model looked and said "not an event") and the `kind`
label (§Error Handling). A line logged at a level an operator can grep
records the handle, shortcode, kind and title, so a specific post's
classification is traceable without a database row to point at.

**An unrecognised or missing `kind` is treated as `event` — this still
matters, more than before.** Before, a wrongly-excluded event was still a
row sitting quietly out of the queue, findable by a query. Now, a
wrongly-classified non-event produces *nothing at all* — no row, anywhere,
for anyone to find. The fail-toward-visible rule is what stands between a
model glitch and a genuinely lost event: only a value the model actually
said, that literally matches `promotion`/`menu`/`food`/`other`, drops the
row. Anything else — `event`, `null`, a typo, a value this pipeline has
never seen — proceeds through the ordinary event flow and reaches the queue
exactly as it always has.

**No operator correction path today.** Because no row is created for a
non-event, there is nothing for `EventPatch`/`operator_edited_fields` to
act on — an operator cannot promote a misclassified post back into an event
through the admin API the way this plan originally intended. Recovery is
re-extraction, once the promotion/menu entities exist to re-extract into
(see the re-scope note above): the archived post is still in S3, and
re-extraction never re-calls Apify. This is a real, accepted gap in this
phase, not an oversight — flagged explicitly rather than worked around.

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
- **No migration.** `events.event` gains no column — re-scoped mid-execution,
  see above. `kind` still rides inside each event's `raw_extraction` JSONB
  blob (the model's parsed answer, copied verbatim, exactly like every other
  extracted field) for a genuine `event`-kind row, purely as an audit trail —
  never a queryable column, never relied on by any predicate.
- **No back-fill, nothing to back-fill.** No column exists to backfill.
- **The risotto row stays an event** until an operator rejects it or its post is
  re-extracted — and it will not be, since its stream's cursor has moved past it.
  Worth stating so it is not mistaken for the fix failing.
- **API:** `EventOut`/`EventPatch` unchanged — no `kind` field, since no
  events.event column exists to expose or correct.
- **Queue:** `list_events_awaiting_decision` unchanged. A non-event post
  never produces a row for it to have a predicate over.
- **Serving:** none.
- **Rollback:** revert. No column to drop; a non-event post that was being
  dropped starts producing an event row again, which is noisy (the risotto
  problem returns) but not lossy — nothing this feature added was ever the
  only copy of anything.

## Error Handling And Observability
Metrics: `event_extraction_posts_total` gains a `kind` label and a new
`not_an_event` outcome value (distinct from `not_event_like`, which means
the post never reached the model at all), so the split between events and
everything else — and how many posts a `kind` decision actually dropped — is
visible from the first run. A dropped post's handle/shortcode/kind/title is
also logged at a level an operator can grep, so a specific decision is
traceable without a database row to point at.

**Watch the `event` share.** If nearly everything still classifies as `event`,
the prompt's precedence is not landing; if almost nothing does, the classifier is
eating real events — and that failure is now silent in a stronger sense than
before the re-scope: a misclassified event produces no row anywhere, not
merely one excluded from the queue.

## Test Plan
Feature file: `tests/bdd/enrichment/post-kind-and-post-extraction-attribution.feature`

Scenarios (rewritten post-approval to match the §B re-scope — see the note
at the top of this plan; two scenarios whose premise depended on a persisted
`kind` column no longer make sense as originally written and are replaced,
one is dropped as genuinely unconstructible, both explained below):
- Classify a daily lunch special as a menu item, not an event — no event is
  recorded for it.
- Classify a happy-hour offer as a promotion — no event is recorded for it.
- Classify a DJ night as an event.
- Classify a plain food photo as food — no event is recorded for it.
- Classify an event advertised alongside a drinks offer as an event.
- A non-event never reaches the review queue (replaces "Keep a non-event out
  of the review queue" — same intent, now trivially true once no row exists,
  kept as an explicit regression guard rather than dropped).
- A non-event never appears in the events list either (replaces "Show a
  non-event in the events list with its kind" — the original scenario's
  premise, that a non-event is visible in `/admin/events` with a `kind`
  field, is now the OPPOSITE of the real behaviour; rewritten to assert the
  row's absence from the general listing too, not just the queue).
- Treat a missing kind as an event.
- Treat an unrecognised kind as an event.
- ~~Let an operator correct a menu item to an event and see it queue~~ —
  **dropped, genuinely unconstructible under the re-scoped design.** No
  `events.event` row exists for a non-event post, so there is nothing for
  `EventPatch` to act on and no way for an operator to "correct" it into an
  event through the admin API today. See §B's "No operator correction path
  today" for the accepted gap and the re-extraction recovery route.
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
- A missing or unrecognised `kind` produces a normal event row (fail-toward-
  visible); each of the four recognised non-event kinds produces none.
- A non-event outcome is counted (`event_extraction_posts_total{outcome=
  "not_an_event"}`) and logged.
- Attribution: model location text present, absent, single-venue handle, and a
  case just below the confidence floor.
- The single-venue path is unchanged — asserted against pre-change behaviour.
- `_detect_recurrence` over: `toda quinta`, `de segunda a sexta`,
  `de sexta a sábado`, `sextas e sábados`, `todas as sextas`, prose with no
  recurrence, and a one-off `sábado` with `is_recurring` false.
- A recurring event with a resolvable schedule is not flagged `missing_date`.

Manual or integration checks:
- Crawl `@entreamigosobode` and confirm its jazz and samba nights classify as
  events with resolved recurrences, while food posts classify as food or menu
  and produce no event row.

## Acceptance Criteria
- Every extracted post is classified, and the precedence rule is in both
  prompts.
- A non-event post produces no `events.event` row; the outcome and kind are
  counted and logged.
- A missing or unknown kind is treated as an event.
- Venue attribution uses the model's location text, after extraction.
- A single-venue handle behaves exactly as before.
- A weekday-range or weekday-list recurrence resolves and is not flagged
  dateless.
- The narrowed one-off weekday path is not re-widened.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
