# Auto-Accept Clean Events, And Protect Fields Instead Of Rows

## Branch
feature/auto-accept-and-field-level-protection

## Goal
Stop asking an operator to confirm events that have nothing wrong with them, and
replace the blunt row-level freeze on a confirmed event with a rule that only
protects what an operator actually edited, and only when a re-extraction would
contradict or degrade it.

## Non-goals
- **Changing what flags an event.** `missing_date`, `unread_time`,
  `low_confidence`, `weekday_mismatch`, `sources_disagree`,
  `extraction_failed` are all correct and stay as they are.
- **`ticket_info` / `attractions`.** Still parked.
- **Any console change.** The queue narrows on the server; vibes_bot renders
  whatever it is given and already sorts flagged-first.
- **Auto-linking a venue.** Untouched — the floor-and-margin gates from
  `260804_instagram-promoter-events.md` still decide that.

## Evidence

**The approval gate was never asked for.** The operator's original decision was
*auto-link high confidence, queue the rest* — about **venue resolution** for
ambiguous promoter events. This repo generalised that into an every-event
approval gate: `260804_event-review-console.md` states "no event is attributed
to a venue without a person agreeing", and `event_reconciliation.py:257` sets
`status = pending_review` on **every** persisted event unconditionally.

That was defensible when nothing consumed `confirmed` and the queue held a
handful of promoter events. It is not defensible now: `max_evidence_venues` is
1000 and one post can yield many events, so the queue fills with rows whose only
message is "nothing is wrong". A queue in which most rows need no action is an
inventory, not a work list.

**The freeze is too blunt.** `confirmed` currently does two jobs at once:

- `event_reconciliation.py:215` — a confirmed event's fields are **all** frozen;
  only `raw_extraction` and `last_seen_at` move.
- lines 359 and 386 — a confirmed event is exempt from supersession.

So an operator who corrects a **title** also freezes the **date**, the
**lineup**, the **price** and everything else against every later post — even
when the later post is strictly better. That directly fights the countdown merge
shipped in `0026`, whose whole purpose is a later post completing an earlier one.

**The information needed to do better already exists.** The console's
`_buildEventPatch` sends only the fields the operator actually changed (the
partial-patch fix in vibes_bot #169), and cs-server reads them with
`model_dump(exclude_unset=True)` (`admin_events_router.py:242`). The set of
operator-edited fields is therefore knowable per request; it is simply not
recorded.

## Current Behavior
Every event starts `pending_review` and needs a human click, however clean it
is. Confirming one field freezes the whole row against all future extractions
and exempts it from supersession forever.

## Desired Behavior
1. Accept a clean extraction automatically, with no human action: no review
   reason, a resolved start date, a linked venue, and confidence at or above the
   floor.
2. Keep a flagged event awaiting a human, as today.
3. Record which fields an operator edited, per event.
4. On re-extraction: **never replace a value with a null** — a later post that
   omits something must not degrade what we already know.
5. Update a field the operator never edited, following the existing merge rules.
6. Keep an operator-edited field's value when a re-extraction contradicts it,
   and flag the divergence — but do not freeze the fields they never touched.
7. Exempt from supersession only what a human confirmed or manually linked, not
   what was auto-accepted.
8. Leave the review queue holding only what needs a person.

## Implementation Approach

### A. A clean extraction is accepted, not queued
Add `accepted` to the status vocabulary. An extraction is clean when it has no
review reason, a non-null `starts_at`, a non-null `venue_id`, and
`confidence >= min_confidence`. Anything short of that stays `pending_review`.

`accepted` and `confirmed` are deliberately different words: one means the
pipeline had no doubts, the other means a person looked. Collapsing them would
lose the ability to answer "what has a human actually seen?", which is the
question an audit asks first.

### B. Protection moves from the row to the field
New `operator_edited_fields` on `events.event` — the union of every field name
an operator has patched. Written on PATCH from the keys `exclude_unset` already
yields, so it records what they touched, not what the form posted.

Re-extraction then applies, per field:

| situation | outcome |
|---|---|
| new value is null/absent | keep existing — **never degrade** |
| new value equals existing | nothing |
| field not operator-edited, values differ | update, per the existing merge rules |
| **field operator-edited, values differ** | keep the operator's value, flag `model_diverges_from_confirmed_record` |

This is the rule the operator asked for: no lock unless the update both
contradicts and would worsen what a human decided. A field they never touched is
not theirs to freeze, and a null is never an improvement.

**`lineup` unions rather than contests**, as `0026` already does — a later post
naming more performers is additive, not a contradiction.

### C. Supersession narrows to human decisions
Exempt from supersession only `confirmed` (human) and `manual` location
resolution. `accepted` events supersede normally.

**This is load-bearing.** If auto-accepted events inherited the exemption, then
once most events are accepted almost nothing could ever be superseded, and the
supersede behaviour shipped in `0025`/`0026` would quietly become dead code.

### D. The queue narrows on its own
Once clean events become `accepted`, `status = 'pending_review'` already means
"something needs a person". The existing predicate — `pending_review`, plus a
promoter event awaiting a location, plus `extraction_failed` — therefore needs
**no change**, and the queue becomes a work list you can empty.

Worth stating because the temptation is to also edit the predicate; doing both
would double-narrow it and hide flagged events.

## Data, Config, And API Impact
- **Migration `0027_operator_edited_fields`** from head `0026_event_sources`:
  add `operator_edited_fields text[]` (or jsonb array), nullable, no default.
  **No back-fill** — an existing `confirmed` row has no record of which fields
  were edited, and inventing one would either freeze everything (today's bug) or
  nothing. Treat a NULL as "unknown": for those rows only, keep today's
  whole-row protection, so no operator's past work is lost. New edits populate
  the column and get the finer behaviour.
- **Status vocabulary** gains `accepted`. `EventOut.status` is a string; no
  constraint change.
- **API:** `EventOut` gains `operator_edited_fields`. Additive.
- **Serving:** none.
- **Downgrade:** drop the column. Safe — it only removes the finer protection,
  falling back to the NULL-means-whole-row rule.

## Error Handling And Observability
Metrics: `event_extraction_posts_total` gains `accepted`. An
`events_total{status}` gauge already exists and will show the accepted/pending
split — the number that says whether the queue is actually shrinking.

Watch for `accepted` staying at zero after deploy: that would mean the clean
predicate is too strict and everything is still queueing.

## Test Plan
Feature file: `tests/bdd/enrichment/auto-accept-and-field-protection.feature`

Scenarios:
- Auto-accept an extraction with no flag, a date, a venue and good confidence.
- Keep an event with a review reason awaiting a human.
- Keep an event with no resolved date awaiting a human.
- Keep an event with no venue awaiting a human.
- Keep an event below the confidence floor awaiting a human.
- Leave an auto-accepted event out of the review queue.
- Record the fields an operator patches, and only those.
- Update a field the operator never edited when a later post differs.
- Keep an operator-edited field when a later post contradicts it, and flag it.
- Never overwrite a known value with a null from a later post.
- Union the lineup across posts even on an operator-edited event.
- Supersede an auto-accepted event that a later run no longer finds.
- Never supersede a human-confirmed event that disappears.
- Never supersede a manually linked event that disappears.
- Apply whole-row protection to a legacy confirmed row whose edited fields are
  unknown.

Pytest unit tests:
- The clean predicate across every combination of (review reason) x (date) x
  (venue) x (confidence).
- The per-field decision table from §B, including null-in / null-out.
- `operator_edited_fields` accumulates across successive patches and never
  records a field the operator did not send.
- Supersession exemption: accepted supersedes, confirmed and manual do not.
- A NULL `operator_edited_fields` on a confirmed row still protects every field.

Manual or integration checks:
- Against the live catalog: re-run extraction and confirm the queue holds only
  flagged events, while the Events tab still lists the accepted ones.

## Acceptance Criteria
- A clean extraction is `accepted` and absent from the review queue.
- A flagged, dateless, venueless or low-confidence event still queues.
- An operator's edited field survives a contradicting re-extraction and is
  flagged; their untouched fields keep improving.
- No re-extraction ever replaces a value with a null.
- Auto-accepted events supersede; confirmed and manually linked ones do not.
- A legacy confirmed row with unknown edits keeps whole-row protection.
- `make test-feature`, `make test-unit` and `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None.
