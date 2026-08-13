# Hide Promoter Events — take promoters out of scope without deleting the work

## Branch
fix/hide-promoter-events

## Goal
The admin console shows only venue-sourced items, so the operator can finish
reviewing venue coverage without 587 promoter rows in the way. Reversible with
a config flag, not a migration.

## Non-goals
- **Deleting or rewriting promoter rows.** They stay exactly as they are.
- **Any vibes_bot admin-ui work.** cs-server hides them at the source, so the
  console shows fewer rows with no UI change. An explicit operator toggle in the
  console is a follow-up, deliberately out of scope — the operator asked for a
  backend-only deliverable.
- **Disabling the promoter crawl targets.** Already done operationally on
  2026-08-13 (`oquetemhojeemnatal`, `recifequecabenobolso` both
  `enabled=false`), so no new promoter items arrive.
- **The backfill.** `260812_backfill-misattributed-links.md` is merged and
  deployed but **not run**, and running it is now deferred with promoters out of
  scope — every row it would touch is promoter-sourced.

## Evidence

### Promoters are the entire remaining problem, and they are all one account
Measured 2026-08-13 against production. Of the 483 rows the backfill would
touch, **every one** comes from `oquetemhojeemnatal`:

| Outcome | Source kind | Source is a venue account? | Rows |
|---|---|---|---|
| Detach — names a handle we do not carry | promoter | False | 355 |
| Repoint / keep | promoter | False | 126 |
| Detach — no handle named | promoter | False | 2 |

**No row from a venue's own account is affected at all.** The 355 detachable
rows name **142 distinct Natal venues absent from the catalog**; the top ten
handles account for 115 of them. That is a catalogue-coverage question, not a
pipeline defect, and it is the piece the operator is descoping.

### Venue coverage is the deliverable and it is nearly clean
Venue-sourced items are unaffected by every open promoter question: Casa Bacurau
26, Entre Amigos O Bode 21, Conchittas 16, BeerDock 16, Sala de Reboco 8,
Downtown Beer Garden 7. Those are the rows worth reviewing, and today they are a
minority of what the console displays.

### Hiding beats staling
The operator asked to "stale them". Do **not** mutate `status`:

- A read-time filter is reversible by flipping a config value; a status rewrite
  across 587 rows needs a second script to undo and a decision about what the
  prior status was.
- `status` already carries review semantics (`accepted`, `pending_review`,
  `superseded`, `rejected`). Overloading it with a scope decision would corrupt
  the meaning the review queue depends on.
- Nothing is lost by filtering: the rows, their sources and their provenance all
  stay queryable for the day promoters come back into scope.

`events.post_item` never reaches the Redis serving projection, so **no filter
here can affect vibes_bot's app API or mobile**. This is admin-surface only.

## Current Behavior
Every admin events read returns venue-sourced and promoter-sourced items
together. Promoter items outnumber venue items roughly five to one.

## Desired Behavior
1. Admin event reads exclude promoter-sourced items by default.
2. The exclusion is reversible at runtime, without a deploy.
3. Counts and the review queue agree with the filter.
4. Nothing is deleted or rewritten.

## Implementation Approach

### A. Identify a promoter item by its sources, and require unanimity
An item is promoter-sourced when **every** `post_item_source` row attached to it
has `source_kind = 'promoter_post'`.

**Unanimity, not "any".** Cross-post merging can attach a promoter source to an
item that also has a venue source (`_absorb_unresolved_sibling` and the merge
path both do this). An item with any venue-sourced evidence is a venue item and
must stay visible — hiding it would remove real venue coverage, which is the
opposite of this plan's purpose. An item with no sources at all is not promoter-
sourced and stays visible.

### B. Filter at read time, behind a runtime flag
Apply the exclusion in the admin events read path — the list, the review queue,
and any count or aggregate the console shows. They must agree; a queue badge
that counts hidden rows is the stranding bug this project has already shipped
once.

Gate it on **admin config** (Redis-backed, runtime-editable — the same mechanism
as `menu_expiry_days` and the category vocabulary), defaulting to **hidden**.
Not `app/config.py`: bringing promoters back must not need a deploy.

Expose the flag's current value on the admin API so the console can eventually
render a toggle, and so an operator reading an unexpectedly short list can tell
*why* it is short. A silent filter is how someone concludes the data is missing.

### C. Do not touch the crawl or extraction paths
Promoter crawling is already off at the target level. This plan changes reads
only — an item that arrives from a promoter source (via a manual run, or a
re-extraction) is still stored exactly as today, just not shown.

## Data, Config, And API Impact
- **Migration** — none.
- **Config (admin, runtime)** — `hide_promoter_events`, default `true`.
- **Admin API** — the flag's value is exposed additively; existing fields
  unchanged. The console is a released client, so nothing may be removed.
- **Serving projection** — untouched. `post_item` does not reach Redis.
- **Rollback:** flip the config value; revert removes the filter entirely. No
  data is altered either way.

## Error Handling And Observability
- Count admin reads by whether the filter was applied, and how many items it
  hid. **If the hidden count ever reaches zero while promoter rows exist, the
  unanimity rule in §A has broken** — that is the signal worth watching.
- Log the flag's value once per read path at debug, not per row.

## Test Plan
Feature file: `tests/bdd/api/hide-promoter-events.feature`

Scenarios:
- Hide a promoter-sourced item from the events list.
- Hide a promoter-sourced item from the review queue.
- Show a venue-sourced item in both.
- Show an item that has one promoter source and one venue source.
- Show an item with no sources at all.
- Make the counts agree with the filtered list.
- Show promoter items again when the flag is turned off.
- Report the flag's current value on the admin API.
- Leave stored promoter rows unchanged whether the flag is on or off.

Pytest unit tests:
- The unanimity predicate: all-promoter; all-venue; mixed; empty; a single
  source of each kind.
- The flag's default is `true` when unset in config.
- Filtered list length and the reported count are derived from the same
  predicate — assert they cannot disagree.

Manual or integration checks:
- After deploy, read the admin events list and confirm only venue-sourced items
  appear, and that the promoter rows are still present in the database. No
  crawl, no external calls.

## Acceptance Criteria
- Admin event reads exclude all-promoter items by default and include everything
  else.
- An item with any venue source stays visible.
- Counts agree with the filtered list.
- The flag is runtime-editable and its value is readable from the API.
- No row is deleted, rewritten, or re-statused.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
