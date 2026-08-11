# Merge An Unresolved Item Into Its Resolved Sibling

## Branch
fix/merge-unresolved-into-resolved-sibling

## Goal
When two posts from the same account announce the same thing on the same day
and only one of them resolved a venue, keep one item — the resolved one —
instead of two.

## Non-goals
- **Merging across handles.** Two bars can both run a Friday karaoke; same title
  and date across different accounts is not evidence of the same event.
- **Changing the existing identity key.** `(venue_id, date, normalized_title)`
  stays exactly as it is for resolved-to-resolved merges.
- **Resolving a venue.** This adopts a venue a sibling already resolved; it
  never runs the resolver or lowers its floors.
- **Back-filling.** Existing duplicates collapse when their posts are next
  extracted; see §Data.

## Evidence

### Two posts, one programme, two rows
Production, after re-extracting `@entreamigosobode`:

```
'Oficina de Sorvete' @ 2026-07-08
  shortcode DavIrhjDnj2   venue: Entre Amigos O Bode   'year_inferred; date_range'
  shortcode DaYA7y0BaPQ   venue: NULL                  'date_range; unresolved_venue'
  identical source_event_key, identical source_handle
```

The FÉRIAS flyer lists the holiday weeks **and** their activities; a second post
lists the activities alone. Both legitimately extract the same workshop on the
same day. Nine such pairs exist right now.

### The merge ran and could not see them
`merge_touched_events` is invoked on every extraction path, including the
by-handle one. It did not fire because `compute_event_identity`
(`app/services/event_merge.py:148`) **returns `None` whenever `venue_id` or
`starts_at` is missing** — an unresolved item has no identity, so it can never
join a group.

That guard is correct as written and must stay: without a venue, title and date
alone would merge a karaoke night at one bar into a karaoke night at another.
What it lacks is the one case where a second key is available — **the handle**.
Two items from the *same account* with the same title on the same day are the
same announcement, whether or not both posts named the venue.

## Current Behavior
An unresolved item has no merge identity, so it survives alongside its resolved
twin and appears in the review queue asking a human to link something already
linked.

## Desired Behavior
1. An unresolved item merges into a resolved sibling from the same handle with
   the same title and calendar date.
2. It adopts that sibling's venue.
3. A resolved item is never dragged to no-venue.
4. Nothing merges when the evidence is ambiguous.
5. The adopted venue is auditable as adopted, not as resolved from the post.
6. Resolved-to-resolved merging is unchanged.

## Implementation Approach

### A. A second identity, used only for the unresolved
Alongside the existing key, compute a **handle identity** —
`(source_handle, calendar date, normalized_title)` — and use it **only** to
attach an item that has no `venue_id` to a group that already has one.

Reuse `normalize_title` and the calendar-date truncation the existing key
already uses. Two normalisations of the same idea is how this repo has been
bitten four times; there must be one.

**`starts_at` is still required.** An item with no date has no handle identity
either — a title alone is not an announcement, and the whole point of the date
is that it is what makes two posts about the same night recognisable.

### B. Direction is one-way, and enforced
The unresolved item adopts the resolved item's `venue_id`. **A resolved item
must never adopt NULL**, and this needs to be a property of the code rather
than of the order rows happen to arrive in — write it so the resolved member is
always the survivor, and assert it in a test that feeds the pair in both orders.

Ordering bugs of exactly this shape have already cost this project: an event
attributed by whichever venue was processed last, and a cursor advancing before
the work it guarded.

### C. Refuse to guess when the evidence splits
Do not merge when:
- **Two resolved siblings match at different venues.** `@entreamigosobode` maps
  to two venues, so this is reachable, not hypothetical. Leave the unresolved
  item unresolved and queued — a wrong venue is invisible, an unresolved one is
  visibly waiting.
- **The unresolved item is `confirmed` or manually linked.** A human decided
  this row's state; a merge must not overrule it. The existing group rules
  already protect a confirmed member — extend them, do not bypass them.
- **`operator_edited_fields` covers the venue.** If someone deliberately cleared
  or set it, that is a decision.

### D. Say where the venue came from
An adopted venue is weaker evidence than one the post itself named: it is an
inference from a sibling. Record it as such — reuse `linked_by` if it can carry
the distinction rather than adding a parallel field — so an operator auditing a
link can tell "the post said so" from "a sibling post said so".

Drop `unresolved_venue` from the review reason once a venue is adopted; keep any
other reason the item carried. An item that still has `date_range` is still
worth a look.

## Data, Config, And API Impact
- **No migration.** This changes which rows survive a merge, not the schema.
- **No back-fill.** The nine existing pairs collapse the next time their posts
  are extracted, which the by-handle mode now makes cheap. Say so in the PR so
  the pairs still visible afterwards are not read as the fix failing.
- **API:** unchanged.
- **Rollback:** revert. Merged rows stay merged — that is the correct outcome
  either way, and un-merging would be a second guess.

## Error Handling And Observability
Count merges by how the group was identified — the existing venue key versus
the new handle key — so the new path's volume is visible from the first run.
Count refusals by reason: ambiguous venue, confirmed member, operator-edited.

**Watch the ambiguous count.** A shared handle whose siblings keep resolving to
different venues means the attribution upstream is unstable, and merging is the
wrong place to fix that.

## Test Plan
Feature file: `tests/bdd/enrichment/merge-unresolved-into-resolved-sibling.feature`

Scenarios:
- Merge an unresolved item into its resolved sibling.
- Adopt the sibling's venue.
- Keep one item where two posts announced the same thing.
- Leave a resolved item resolved when its unresolved twin is processed first.
- Refuse to merge when two siblings resolved to different venues.
- Refuse to merge an item an operator confirmed.
- Refuse to merge when an operator set the venue.
- Never merge items from different handles.
- Never merge items on different dates.
- Keep merging resolved items exactly as before.
- Drop the unresolved reason once a venue is adopted.
- Keep the item's other review reasons.

Pytest unit tests:
- The handle identity: same handle/title/date, differing case, differing
  accents, differing time on the same date, and a null date.
- One-way direction, asserted with the pair fed in **both** orders.
- Ambiguity: two resolved siblings at different venues leaves both and the
  unresolved item untouched.
- A confirmed or operator-edited unresolved item is left alone.
- The resolved-to-resolved path is byte-for-byte unchanged — asserted against
  the pre-change behaviour, since it is the common case and the likeliest
  casualty.
- The real production case: the nine `@entreamigosobode` pairs collapse to nine
  items, each attributed to Entre Amigos O Bode.

## Acceptance Criteria
- An unresolved item merges into its resolved same-handle sibling and adopts
  its venue.
- A resolved item is never dragged to no-venue, in either processing order.
- Ambiguous, confirmed and operator-edited cases are left alone.
- Adopted venues are distinguishable from post-resolved ones.
- Resolved-to-resolved merging is unchanged.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None. If `linked_by` cannot carry §D's distinction without changing its meaning
for existing rows, stop and report rather than overloading it.
