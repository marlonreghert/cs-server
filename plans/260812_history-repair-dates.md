# History Repair: Dates — re-resolve stored date text without asking the model again

## Branch
fix/history-repair-dates

## Goal
Every stored item whose `date_text` the improved resolver can now read gets its
real date, computed from data already in RDS — no model call, no Apify, no
re-extraction.

## Non-goals
- **Improving the resolver.** `260812_event-attribution-and-dates.md` §C/§D owns
  that. This plan only re-runs it over stored text.
- **Re-extracting anything.** The moment a model is involved, `date_text` and
  `title` can move and `source_event_key` with them — that hazard belongs to
  `260812_history-repair-reextraction.md`, which confronts it deliberately.
- **Repairing venue links.** `260812_backfill-misattributed-links.md`.
- **Merging duplicates.** `260812_event-dedup-fuzzy-title.md`, and it must run
  **after** this plan — see §F.
- **Inventing a date.** A row the resolver still cannot read stays undated and
  stays queued.

## Evidence

### The forward fix leaves the wrong dates in place
`260812_event-attribution-and-dates.md` §C/§D teaches the resolver
`de X a Y`, three-letter months, weekday-plus-day, and a grace window on the
year roll. It changes nothing already stored. Known-wrong rows as of the
2026-08-12 snapshot:

| row | `date_text` | stored | should be |
|---|---|---|---|
| `SÁBADO DO CONCHITTAS` | `08/08` | **2027**-08-09 | 2026-08-08 |
| `Casa BeerDock 2027` | `De 06 a 09 de fevereiro` | 2027-02-**09** | 2027-02-**06** |
| `Aniversário do RODOLPHO Produções` | `É HOJE` | no date | the post's own date |
| 3 rows | `Quinta (02)` | no date | resolvable |
| 1 row | `05/SET` | no date | resolvable |

Plus 14 rows whose `date_text` was empty — those are **not** repairable here and
must stay undated.

**These counts are pre-fix and will not match what the dry run finds.** More
rows have been crawled since; the forward fix will have corrected some of them
at the source; and the 2027 rows may have been superseded. The dry run is the
measurement — see §E. Do not carry these numbers into the PR as expected
results.

**Correction, 2026-08-13: `É HOJE` was never taught to the deterministic
resolver.** An earlier revision of this plan claimed
`260812_event-attribution-and-dates.md` §C did so. It did not. §C added the
model's optional `date_interpretation` as a *fallback*, which resolves `É HOJE`
only when the model volunteers a `{"kind": "relative", "relative": "hoje"}`
reading — and the model returned null for the Rodolpho post. The deterministic
path still matches five exact literals by whole-string equality, so `É HOJE`
resolves to nothing today. Verified by replaying that row's own stored model
output through the current resolver.

The deterministic gap is closed by **`260813_review-gate-and-date-vocabulary.md`
§A**, which is now a hard prerequisite of this plan: running the repair before
it would leave `É HOJE`, `Hoje!` and `É AMANHÃ` unresolved and would have to be
run a second time.

### The queue is showing defects that were already fixed
Replaying all ten queued rows through the current resolver on 2026-08-13 found
three whose stored value is wrong and whose correct value is already computable:

| row | `date_text` | stored | resolver today |
|---|---|---|---|
| `SÁBADO DO CONCHITTAS` | `08/08` | 2027-08-08 | **2026**-08-08 21:00 |
| `Casa BeerDock 2027` | `De 06 a 09 de fevereiro` | 2027-02-**09** | 2027-02-**06**, range flagged |
| `Especial do dia` | — (`de segunda a sexta`) | no date | 2026-07-09 11:00, recurring |

This is the operator-visible cost of not running this plan: a forward fix leaves
its own evidence sitting in the review queue, which reads as the same bug
recurring.

### The prerequisite anchor is now complete
`source_uploaded_at` — called out below as a hard prerequisite — was back-filled
on 2026-08-13 and is now present on **114 of 114** venue-sourced rows
(promoter-sourced rows reached 564/587). The anchor this plan needs exists.

### The inputs are already in RDS, or will be
`post_item_source.raw_extraction` carries the model's verbatim `date_text` and
`time_text` (confirmed on the snapshot: present on 644 of 660 sources). The
anchor the resolver needs — the post's own timestamp — is added by
`260812_crawl-error-visibility.md` §C as `source_uploaded_at` and back-filled
from the S3 manifest. **That back-fill is a hard prerequisite**: without the
anchor, `hoje` and every unrolled year are unanswerable.

### Repairing a date moves the row's identity
This is the whole difficulty of this plan and it must not be discovered during
execution. `event_identity.compute_source_event_key` hashes **normalised title
plus resolved calendar date**. Change `starts_at` and the key that *would* be
computed for that source changes with it.

Two failures follow if it is handled naively:

- **Leave `source_event_key` as stored.** The next extraction of that post
  computes the new key, matches nothing under
  `uq_post_item_source_post (source_handle, source_shortcode,
  source_event_key)`, and inserts a **second source row** for the same real
  event. The repair quietly becomes a duplicate generator — the exact failure
  `0025_multi_event_posts` was written to prevent.
- **Rewrite it blindly.** The new key may already exist for that
  `(handle, shortcode)`, and the unique constraint rejects the update — or
  worse, the repair silently makes two rows identical.

## Current Behavior
Stored dates reflect whatever the resolver could read at extraction time. A row
the improved resolver could now read keeps its wrong date or its absence
forever, and its review reason with it.

## Desired Behavior
1. Re-resolve every stored `date_text` with the current resolver.
2. Write the corrected date and keep the row's identity coherent.
3. Never overwrite an operator's own answer.
4. Correct the review reason in both directions.
5. Report everything before changing anything, and change nothing twice.

## Implementation Approach

A mode of the operator-script family
`260812_backfill-misattributed-links.md` establishes —
`python -m scripts.repair_event_dates`, **dry-run by default**, `--apply` to
write, idempotent, resumable. Not an Alembic data migration: it must be run
attended, with its report read first, and a migration offers neither.

### A. Re-resolve from stored text only
For each `post_item_source`, feed `raw_extraction.date_text`,
`raw_extraction.time_text` and `source_uploaded_at` to
`resolve_event_datetime` — the same function the pipeline calls, imported, never
reimplemented. Skip any source with no `source_uploaded_at`: no anchor, no
answer, report it as skipped rather than guessing with `now()`.

Where a `post_item` has several sources, resolve each and fold them with the
existing precedence rather than picking the first — reuse
`merge_event_fields`/the reconciliation path so this script cannot disagree with
the pipeline about which source wins.

### B. Move `source_event_key` with the date, and treat a collision as a merge
Recompute `compute_source_event_key` whenever the resolved date changes, and
write it in the same transaction as `starts_at`. Never write one without the
other.

**If the new key collides with an existing source row for the same
`(source_handle, source_shortcode)`, that is not an error — it is the discovery
that two source rows were always the same event and only a date bug kept them
apart.** Do not force the write. Record the pair and hand it to the merge layer,
exactly as `260812_event-dedup-fuzzy-title.md` handles any other same-identity
pair. If that plan has not landed, report the collision and skip the row; a
skipped repair is recoverable, a broken constraint at 2 a.m. is not.

### C. Never overwrite an operator
- `operator_edited_fields` containing `starts_at` → never touched, counted,
  reported.
- `status = 'confirmed'` → never touched silently. An operator confirmed the
  event *including its date*. Report these separately as "confirmed but the
  resolver now disagrees" and let a human decide; a confirmation is the strongest
  signal in the system and a script does not get to overrule it.
- `superseded` rows → skipped. Repairing a row that lost is pointless and its
  identity change could collide with the winner.

### D. Correct the review reason in both directions
A row queued `missing_date` that now resolves must **leave** the queue. A row
that resolves only by an inferred year must **gain** `year_inferred` and stay
visible. A row that gains a `date_range` collapse must be flagged.

**Removing a reason must never un-queue a row that is still undecided for a
different reason.** `review_reason` is a composite (`sources_disagree;
year_inferred; date_range` appears live), so operate on the reason *set*, not
the string.

### E. The dry run is the measurement
The report must list, per row: id, venue, title, `date_text`, stored date,
resolved date, the reason set before and after, and the disposition — repaired,
unchanged, skipped-no-anchor, skipped-operator, collided, confirmed-conflict.

Group it by `date_text` **shape**, not by row, and require every distinct shape
in the report to be accounted for before `--apply`. That is the gate that
catches a resolver regression: if a shape that resolved correctly yesterday
appears under "unchanged", the forward fix broke something.

Capture the report before applying. There is no revert for a row already
written; the report plus the pre-apply snapshot are the recovery path.

### F. Order: dates before the dedup sweep
Run this **before** `260812_event-dedup-fuzzy-title.md`'s historical sweep.
Repairing a date changes which rows share a calendar date, and the same local
date is half the dedup candidate window. Sweeping first would compute merges
against dates this plan is about to change, then have to redo them.

## Data, Config, And API Impact
- **Migration** — none. `starts_at`, `time_known`, `source_event_key` and
  `review_reason` values move; no schema changes.
- **Prerequisites** — `260812_event-attribution-and-dates.md` merged and
  deployed, and `260812_crawl-error-visibility.md` §C's `source_uploaded_at`
  back-fill completed. Enforce the first by importing a symbol the new resolver
  exports and aborting on `ImportError`; enforce the second by refusing to run
  when more than a configurable share of sources have a null anchor.
- **New file** — `scripts/repair_event_dates.py`.
- **Rollback:** none for rows already written. Mitigated by dry-run-first, the
  captured report, and an RDS snapshot immediately before `--apply` (see the
  wrapper coordination plan's snapshot table).

## Error Handling And Observability
- Count repairs by disposition, and by `date_text` shape.
- **Watch the collision count.** More than a handful means the date bug was
  splitting events at scale, which is a finding in its own right and changes
  what the dedup sweep should expect.
- A row that raises must not abort the run: log it with its id, count it, carry
  on. A resumable script that dies on row 300 of 600 has done the worst possible
  thing — half a repair with no report.

## Test Plan
Feature file: `tests/bdd/enrichment/history-repair-dates.feature`

Scenarios:
- Repair a row whose date text the resolver can now read.
- Correct a row whose year was rolled forward wrongly.
- Correct a range that was resolved to its last day.
- Leave a row whose date text is empty undated and queued.
- Leave a row with no stored anchor untouched and report it.
- Never change a date an operator edited.
- Never change a confirmed row's date, and report the disagreement.
- Skip a superseded row.
- Drop `missing_date` when the date resolves.
- Keep a row queued for a different reason when its date resolves.
- Add `year_inferred` when the repair had to guess a year.
- Rewrite the source event key together with the date.
- Hand a key collision to the merge layer instead of failing.
- Report every change and write nothing without `--apply`.
- Change nothing on a second `--apply`.

Pytest unit tests:
- Each `date_text` shape from the Evidence table, asserted against the exact
  production strings.
- Key rewriting: the recomputed key equals what a fresh extraction of the same
  post would compute — assert against `compute_source_event_key` directly, so
  the script cannot drift from the pipeline.
- Collision detection against `uq_post_item_source_post`.
- Reason-set arithmetic: removal, addition, and a composite where one reason is
  removed and another must survive.
- Multi-source folding uses the shared precedence, asserted against
  `merge_event_fields` rather than a local reimplementation.
- Idempotency: a second pass over already-repaired rows produces an empty change
  set.

Manual or integration checks:
- Dry-run against a **restored snapshot**, never production, and attach the
  shape-grouped report to the PR.
- No external calls of any kind: no Apify, no OpenAI, no S3 beyond what the
  `source_uploaded_at` back-fill already did.

## Acceptance Criteria
- Every Evidence-table shape resolves correctly on a real row.
- `starts_at` and `source_event_key` always move together, and a collision is
  reported rather than forced.
- No operator-edited or confirmed row is silently changed.
- Review reasons are corrected in both directions without un-queueing a row that
  is still undecided.
- The dry-run report accounts for every distinct `date_text` shape.
- A second `--apply` changes nothing.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
