# Review Queue Completeness, And Venue Names On Events

## Branch
fix/review-queue-completeness-and-venue-names

## Goal
Make the event review queue hold every event awaiting a human decision — not
only promoter events awaiting a venue — and give each event its venue's **name**
so an operator can read the queue without decoding an opaque id.

## Non-goals
- **Changing what puts an event into `pending_review`.** The extractor's rules
  are correct; only the queue's SQL is too narrow.
- **Auto-confirming anything.** Nothing here decides on an operator's behalf.
- **Paging or filtering the queue.** The existing list endpoint's contract is
  unchanged apart from its population and one added field.
- **The console.** vibes_bot renders this; see
  `vibes_bot/plans/260807_review-queue-venue-names.md`.

## Evidence

Both defects were observed live on 2026-08-07 by an operator running the
documented runbook.

**Defect 1 — venue events can never reach the queue.**
`RdsVenueStore.list_events_pending_location()` is the queue's entire
population, and its predicate is:

```sql
WHERE source_kind='promoter_post' AND location_resolution IS NULL
```

`source_kind='promoter_post'` excludes every venue-post event **by
construction**, whatever its status. The operator extracted three Métropole
events — all `pending_review`, one of them with **no date at all** — and none
can ever appear in the queue.

That directly contradicts two shipped plans.
`260804_event-review-console.md` specifies the queue "lists every event awaiting
a decision". `260804_instagram-event-extraction.md` requires an unparseable date
to store NULL and set `pending_review` precisely so a human sees it — its stated
reason being that "a guessed date is worse than a missing one: an operator will
scan a queue of blanks, but will not audit a field that looks answered." The
blank is being stored and then hidden.

**`pending_review` is an inbox, not an error flag.** `event_reconciliation.py:238`
sets it on every persisted event unconditionally. So "awaiting a decision" and
"pending_review" already mean the same thing; the queue simply is not reading it.

**Defect 2 — the queue shows an opaque id.** `EventOut`
(`app/routers/admin_events_router.py`) carries `venue_id` and no venue name, and
`_EVENT_SELECT` selects only from `events.event`. The console therefore renders
`ven_672d365457624e4f754c7a52637771594d636c6e6f574a4a496843`, which led the
operator to conclude the events were **not linked** when in fact all three were
correctly attributed. A display defect produced a false diagnosis of a data
defect — that is the cost worth fixing.

## Current Behavior
The review queue returns only promoter-post events with a NULL
`location_resolution`. Events carry a venue id and no name.

## Desired Behavior
1. Return every event awaiting a human decision: anything `pending_review`, plus
   any promoter event still lacking a location decision even if its data was
   already confirmed.
2. Keep excluding events an operator has finished with — `confirmed` (with a
   location settled), `rejected`, `superseded`.
3. Keep returning ranked venue candidates for events that have them, and an
   empty list for those that do not.
4. Carry the venue's name on every event that has a venue, and NULL when it has
   none.
5. Preserve the queue's oldest-first ordering.
6. Change no other field, and no app-facing response.

## Implementation Approach

### A. Widen the predicate, and say what it means
Replace the promoter-only predicate with the union of the two genuine decision
states:

- `status = 'pending_review'` — nobody has confirmed this event's data yet, and
- `source_kind = 'promoter_post' AND location_resolution IS NULL` — nobody has
  decided where it happens.

The second clause is **not** redundant: an operator can confirm an event's
fields while leaving its venue unresolved, and that event still needs a
decision. Dropping it would silently retire those.

Rename the DAO method to match what it now returns — it is no longer "pending
location". A method whose name has drifted from its predicate is how the next
reader re-narrows it by accident.

**Every event starts `pending_review`, so this queue is an inbox and will be as
long as the catalog is busy.** That is the intended meaning — the console plan's
requirement is that no event is attributed without a person agreeing — but it is
worth stating plainly, because a queue that was three rows will now be hundreds
once targeting runs at its new bound.

### B. Venue name comes from the database, not the console
`_EVENT_SELECT` gains a `LEFT JOIN venues.venue` and selects the name as
`venue_name`. LEFT, not INNER: an unresolved promoter event has `venue_id` NULL
and must still be returned — an inner join would silently drop exactly the rows
the queue exists for.

`EventOut` and the review-queue item gain `venue_name: Optional[str]`. Additive,
so no existing consumer breaks.

Resolving in SQL rather than having the console look each id up keeps the
backend authoritative and avoids N+1 lookups from the browser for a queue that
is now much longer.

## Data, Config, And API Impact
- **Migration:** none. Read-only change to a SELECT.
- **API:** `GET /admin/events`, `/admin/events/{id}` and `/admin/events/review`
  gain `venue_name`. `/review` returns a larger population. Both additive to the
  response shape.
- **Serving:** none. No app-facing route or response changes.
- **Rollback:** revert; nothing is written differently.

## Error Handling And Observability
An event whose `venue_id` no longer resolves to a venue row returns
`venue_name: null` rather than failing the whole listing — one dangling
reference must not blank the queue.

`event_review_queue_depth` now reflects the wider population. That is a genuine
change in what the gauge means, so it must be noted where the gauge is defined;
a dashboard reading it as "ambiguous promoter links" would silently start
reading "unconfirmed events".

## Test Plan
Feature file: `tests/bdd/enrichment/review-queue-completeness.feature`

Scenarios:
- Return a venue-post event awaiting review — the case that is impossible today.
- Return a venue-post event queued because its date could not be determined.
- Return a promoter event still awaiting a venue decision.
- Return a promoter event whose data was confirmed but whose venue is still
  undecided.
- Exclude a confirmed event whose venue is settled.
- Exclude rejected and superseded events.
- Return ranked candidates for an event that has them.
- Return an empty candidate list for a venue-post event, without failing.
- Order the queue oldest-first across both kinds.
- Carry the venue name on a linked event.
- Carry a null venue name on an unresolved event, and still return it.
- Return a null venue name for a dangling venue reference without dropping the
  event.

Pytest unit tests:
- The predicate across the full matrix of (source_kind) x (status) x
  (location_resolution), so a future narrowing fails loudly.
- The LEFT JOIN returns venue-less events — asserted directly, since an INNER
  join is the likely accidental edit.
- `venue_name` is present on the single-event and list endpoints too, not only
  the queue.

Manual or integration checks:
- Against the live catalog: confirm the three Métropole events (including the
  no-date one) appear in the queue with `Métropole` as their venue name.

## Acceptance Criteria
- A venue-post event awaiting review appears in the queue.
- A no-date event appears in the queue.
- A promoter event awaiting a venue still appears.
- Confirmed-and-settled, rejected and superseded events do not.
- Every event carries `venue_name`, null when it has no venue.
- An unresolved promoter event is still returned.
- No app-facing response changes.
- `make test-feature`, `make test-unit` and `make test-bdd` pass.

## Open Questions
None.
