# Record What Superseded A Row — a tombstone that says what replaced it

## Branch
fix/record-what-superseded-a-row

## Goal
A row marked `superseded` records **what replaced it**, whenever that can be
determined without guessing — so "why did this event disappear?" is answerable
from the data instead of by re-deriving a re-extraction's history.

## Non-goals
- **Changing when a row is superseded.** The reconciliation rule is correct and
  untouched; only the bookkeeping alongside it changes.
- **Deleting superseded rows.** Supersede-not-delete is deliberate and stays.
- **Hiding them in the console.** That is
  `vibes_bot/plans/260814_hide-superseded-by-default.md`, shipped alongside.
- **Guessing a replacement.** A superseded row with no unambiguous successor
  keeps `superseded_by = NULL`. Inventing a link would be worse than none.
- **Re-running dedup.** Unrelated: the merge path already records its survivor.

## Evidence

### Two supersede paths disagree about their own bookkeeping
```
event_merge.py:468            {"status": STATUS_SUPERSEDED, "superseded_by": canonical_id}
event_reconciliation.py:977   {"status": STATUS_SUPERSEDED}
```

The dedup/merge path records the survivor. The **re-extraction** path — a post
read again, producing a different `source_event_key`, so the old row is retired
and a new one created — records nothing. The row states that it lost, and not
to whom.

### The case that surfaced it
`SECRET CLUB` at Club Metrópole, both rows from the **same post**
`Dbt0M1ooIPp`:

| status | `starts_at` | `source_event_key` | first seen |
|---|---|---|---|
| accepted | 2026-09-05 | `4dad3535…` | 2026-08-07 19:33 |
| superseded | — (`missing_date`) | `46a45bf9…` | 2026-08-07 14:23 |

`date_text` is `'05/SET'` in both. The first extraction could not read the
three-letter month; a later one could. The resolved date changed, so the key
changed (`compute_source_event_key` hashes normalised title + resolved date),
so reconciliation created a new row and superseded the old one. Correct
behaviour — but the tombstone cannot say so, and to an operator the two rows
read as an un-deduplicated pair.

### The scale is small and must not be overstated
A first count of "44 affected posts" was **wrong**: it counted roundup posts
legitimately yielding many events (one `oquetemhojeemnatal` post produces 20
events, so 20 keys — correct). Constrained to the same post **and** the same
normalised title:

- **6** pairs where a superseded row sits beside a live one.
- Most same-post/same-title pairs are not splits at all — `oficina de sorvete`
  on 2026-07-08 **and** 2026-07-10, `semana do downtown` on 08-13 **and**
  08-15 — genuinely distinct occurrences announced in one post, correctly
  distinct rows.
- Only two look like true date-change splits: `secret club` (dated vs undated)
  and `14ª edição da cinema mostra aids` (08-04 vs 08-05, the pre- and
  post-repair readings).

So this is a contained bookkeeping gap, not systemic duplication.

### The field exists and is already read; it is simply never written here
`superseded_by` is a real column, selected by
`rds_venue_store.py:492/556/589`, and the un-merge route already clears it
(`admin_events_router.py:790`). It is not on `EventOut`, so no client can see it
either.

## Current Behavior
A re-extraction supersede sets `status` only. `superseded_by` stays NULL, and
nothing else records which event replaced the row.

## Desired Behavior
1. A re-extraction supersede records the replacing event when exactly one
   unambiguous successor exists.
2. It records nothing — leaving NULL — when the successor is ambiguous, and
   that case is counted rather than hidden.
3. Existing orphaned superseded rows gain their link where it is unambiguous.
4. The value is readable from the admin API.

## Implementation Approach

### A. Link only when the successor is unambiguous
`reconcile_post_events` knows the post's newly-computed event set. A superseded
row's replacement is **not** always determinable: a post that used to yield
three events and now yields two has a retired row with no successor at all.

Record `superseded_by` only when, among the events this same post just produced,
exactly **one** carries the same normalised title as the row being superseded.
Reuse the existing normalisation (`event_identity`'s own title normalisation) —
never a second, drifting comparison. Zero matches, or more than one: leave NULL.

That is exactly the rule that made the `secret club` pair unambiguous, and it
refuses the cases where a human would also have to guess.

### B. Count both outcomes
A counter labelled by whether the supersede was linked or left unlinked. If
"unlinked" dominates, §A's rule is too narrow and we will know from data rather
than by noticing another confusing pair in the console months later.

### C. Back-fill the existing orphans
A dry-run-first script, the same discipline as every other repair here:
idempotent, `--apply` to write, naming every row it links and every row it
leaves alone. It must apply the **same** §A predicate — same post, same
normalised title, exactly one live candidate — and never link across posts.

Roughly 6 rows are eligible. The script reports rather than guesses when a
superseded row has several or no candidates.

### D. Expose it additively
Add `superseded_by` to `EventOut`. Additive only — the console is a
separately-released N-1 client. The console is not required to render it in this
plan; exposing it is what makes the follow-up possible.

## Data, Config, And API Impact
- **Migration** — none. `superseded_by` already exists.
- **Admin API** — one additive field on `EventOut`. Nothing removed or narrowed.
- **Serving projection** — untouched.
- **Rollback** — revert. §C's writes are data; the script names every row it
  touched, so reversal is exact.

## Error Handling And Observability
- Count supersedes by `linked` / `unlinked_ambiguous` / `unlinked_no_candidate`.
- Never log the whole row; the event id and the chosen successor id are enough.
- A back-fill run that finds nothing must exit successfully — "already correct"
  is not a failure.

## Test Plan
Feature file: `tests/bdd/enrichment/record-what-superseded-a-row.feature`

Scenarios:
- Record the replacement when a re-extraction supersedes a row with one
  same-titled successor.
- Leave the link empty when the post's new events carry no matching title.
- Leave the link empty when two new events share the superseded row's title.
- Never link a row to an event from a different post.
- Keep the merge path's own `superseded_by` behaviour unchanged.
- Report `superseded_by` on the admin API.
- Count a linked supersede and an unlinked one distinctly.
- Back-fill an existing orphan whose successor is unambiguous.
- Leave an ambiguous orphan untouched and name it in the report.
- Change nothing on a second back-fill run.

Pytest unit tests:
- The successor predicate across zero / one / many same-title candidates.
- Title normalisation matches `event_identity`'s, not a second implementation.
- The un-merge route still clears `superseded_by` as it does today.

Manual or integration checks:
- Dry-run the back-fill against production and confirm it names the `secret
  club` pair and leaves the genuinely-distinct occurrences
  (`oficina de sorvete` on two dates) alone.

## Acceptance Criteria
- A re-extraction supersede with one same-titled successor records it.
- An ambiguous supersede records nothing and is counted.
- The `secret club` superseded row points at its dated replacement.
- No row is ever linked to an event from another post.
- `superseded_by` is on `EventOut`.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
