# Backfill Mis-attributed Venue Links — repair the 487 rows the old precedence wrote

## Branch
fix/backfill-misattributed-links

## Goal
Every already-stored item whose venue came from a post-level caption mention is
re-decided against the event's **own** stated location, in one auditable pass,
without spending a cent on OpenAI or Apify and without overwriting anything an
operator decided.

## Non-goals
- **The forward fix.** `260812_event-attribution-and-dates.md` §A/§B own the
  precedence change. This plan repairs history and nothing else.
- **Merging duplicates.** `260812_event-dedup-fuzzy-title.md`. This script
  never calls `merge_touched_events` — see §D for why, and for what it does
  when a repair creates a collision.
- **Re-running the extraction model.** §A explains why the answer is already in
  the database and why paying to re-derive it would be worse than free.
- **Adding the venues the corpus names.** The script *reports* them (§E); adding
  them is the operator's `batch-add-venues` workflow, and it costs money.
- **Any admin-UI work in vibes_bot.**

## Evidence

### The right answer is already stored, on every row that needs repairing
Of the 636 items in the 2026-08-12 snapshot, **494** carry
`linked_by = 'handle_mention'`. All 494 come from **28 posts** on the single
promoter account `oquetemhojeemnatal`, and within each post every item inherits
the same `venue_id` (28 of 28 posts are single-venue in the stored data, for
post sizes of 8 to 20 items).

**492 of the 494 carry an `@`-mention inside their own `location_text`**, and
**none of the 494** shares a word longer than three characters with the venue
name it is linked to. Two have no `location_text` at all. So the corrupted set
is essentially the whole `handle_mention` population, and the evidence needed to
repair it is sitting on the row.

The stored `post_item_source.raw_extraction` is a complete JSON object of the
model's original per-event answer — `title`, `date_text`, `time_text`,
`location_text`, `price_text`, `confidence` and the rest. **644 of 660** source
rows carry a non-empty `raw_extraction.location_text`. Nothing needs to be
fetched, re-read, or re-inferred.

### What the damage looks like from the venue side
The 494 rows are spread over ten venues that are not hosting them:

| linked venue | items it is not hosting |
|---|---|
| Teatro Riachuelo | 129 |
| Sempre Rock Bar | 87 |
| Seu Chico Botequim | 65 |
| Casanova Ecobar | 60 |
| Taverna Pub Medieval Bar & Avalon Events | 40 |
| Só Mais Uma | 40 |
| La Luna Bar e Petiscaria | 20 |
| Rastapé House of Forro | 20 |
| Wesley's Bar - Capim Macio | 19 |
| Casa do Matuto | 14 |

### The dominant outcome is a detach, not a re-point — and that is the hard part
The 492 usable `location_text` values name **159 distinct Instagram handles**.
Cross-matching them against the venue names present in the corpus, only about a
dozen correspond to a venue we carry:

```
seuchicobotequim 16   wesleysbar 14   semprerockbar 12   tavernapubnatal 11
teatroriachuelonatal 9   somaisumabar 9   casadomatutonatal 8
bar54_ 5   casanovaecobar 5   rastapecasadeforro 3 (+1 misspelling)
```

That is roughly **95-110 rows that re-point to a correct venue**. The remaining
**~385 (about 80%)** name a Natal venue the catalog does not hold, and will
resolve to no venue at all.

`VenueRepository.list_events_awaiting_decision` (`app/dao/rds_venue_store.py`
:823) puts **every row with `venue_id IS NULL` in the review queue regardless of
status**. A naive detach therefore turns a 29-row queue into a ~420-row queue
overnight, of which ~385 are not decisions a human can make — the venue does not
exist to pick. That is the defect this plan has to design around, not the
attribution itself. See §C.

### These rows are not served, so this is an admin-side repair
`events.post_item` does not reach the Redis serving projection —
`app/services/redis_projection_service.py` has no reference to it, and the only
reader outside the pipeline is `app/routers/admin_events_router.py`. So the
blast radius is the admin console and the review queue, and the data-flow
invariant is not exercised. The admin console is still a released client:
`linked_by`, `location_resolution` and `review_reason` values may be added, never
removed or repurposed.

### The corpus is currently free of the complications that would make this hard
Measured across all 524 `oquetemhojeemnatal` items:

- **0** rows are `confirmed`, **0** are manually linked, **0** carry a non-null
  `operator_edited_fields`. All 494 `handle_mention` rows are `accepted`.
- Only **3** `(calendar date, normalized title)` groups hold more than one row
  (all three are `flow e meditacao`, all already venue-less). **None** contains
  a row this backfill would touch.

None of that is a licence to skip the protections — the snapshot is one moment,
and the whole point of writing the plan before the script is that an operator may
have confirmed a dozen rows by the time it runs. It does mean the protections can
be asserted in tests rather than discovered in production.

## Current Behavior
487 rows point at a venue their own text does not name. Nothing distinguishes
them from the five rows that are correct: every one is `linked_by =
'handle_mention'`, `location_resolution = 'auto'`, `status = 'accepted'`.

## Desired Behavior
1. Re-decide each affected row's venue from the event's own stored
   `location_text`, using the production ladder, never a second copy of it.
2. Leave every operator decision — confirmed, manually linked, or
   `operator_edited_fields`-recorded — exactly as it is.
3. Detach a row whose own text names a venue the catalog does not hold, and say
   **that** rather than "we could not tell".
4. Report every change before making any change, and balance the arithmetic.
5. Be safely re-runnable and resumable.
6. Spend nothing on Apify, S3, or OpenAI.

## Implementation Approach

One commit per section on a single branch and PR, per the operator's standing
preference for phased multi-defect fixes.

### A. Re-resolve from the stored extraction — no model call, and no S3 read
The re-attribution reads `post_item_source.raw_extraction["location_text"]` and
runs `event_venue_resolution.resolve_event_venue` against it. Nothing else.

Re-extraction from S3 is this project's cheap **validation** loop and it stays
that — one archived post, once, to prove the forward fix works
(`260812_event-attribution-and-dates.md` already books that check). It is the
wrong **repair** mechanism here, for two reasons that both matter:

- **It is not free.** S3 costs nothing, but every re-extracted post is a fresh
  OpenAI vision call. 115 posts is 115 calls to recover a string we are already
  storing on 644 of 660 rows.
- **It is not idempotent against the stored keys.** A fresh model answer can
  return a different `date_text` reading or a re-phrased title, which changes
  `compute_source_event_key`, which orphans the row and any operator
  confirmation attached to it — precisely the failure `0025_multi_event_posts`
  exists to prevent (see `app/services/event_identity.py`'s module docstring).
  A repair that can invent new rows is not a repair.

Reading the stored `raw_extraction` has neither problem: the input is frozen, the
ladder is deterministic, and the script writes only the four link columns.

**This plan depends on `fix/event-attribution-and-dates` having landed.**
The ladder as it stands today reads rung 1 from the **caption**, and the script
has no caption to give it (`raw_extraction` stores the model's per-event answer,
not the post's caption; the location tag is likewise absent). Running the
current ladder with `caption=None, location_tag=None` would silently skip rungs
1-2 and resolve everything through rung 3's name match against the whole
servable catalog — a **third** answer, different from both what is stored and
what the fixed pipeline will produce on its next run. The corpus would be
repaired into a state the pipeline then disagrees with.

Enforce the dependency mechanically, do not document it and hope: the script
imports the per-event link-method value §A introduces (the split
`event_handle_mention` / `caption_handle_mention`, or whichever name §A settles
on) and **aborts before reading a single row** if that symbol does not exist.
An ImportError on line one is a better failure than 494 quietly re-broken rows.

### B. What each status gets, and why
Selection is by `linked_by`, not by status: the population is "rows whose venue
came from a caption mention", and a future re-run must select the same set.

| status | action |
|---|---|
| `accepted` | re-resolve; re-point, or detach per §C |
| `pending_review` | re-resolve the **link only**; status never moves |
| `superseded` | **skip** |
| `extraction_failed` | **skip** |
| `rejected` | **skip** |
| `confirmed` | **skip** |
| `location_resolution = 'manual'` | **skip** |

`superseded` is skipped on principle, not for convenience. A superseded row
records an extraction the post no longer yields; it is excluded from the review
queue, never served, and repairing its link changes nothing anyone reads —
while making it eligible for a future merge that would resurrect content the
pipeline already retired. The 31 superseded rows stay exactly as they are.

`extraction_failed` rows have no `source_event_key` and no `location_text` —
`reconcile_post_events` already treats them as pre-dating content identity
entirely, and so does this.

`confirmed` and manually-linked rows are skipped for the same reason
`reconcile_post_events`'s own confirmed branch skips them: the operator outranks
the pipeline, and outranks a backfill just as completely.

**`operator_edited_fields` wins, using the rule that already exists.** A row
whose `operator_edited_fields` contains `venue_id` is skipped and counted —
the identical refusal `event_merge._merge_handle_group` already applies
(`if "venue_id" in (duplicate.get("operator_edited_fields") or [])`). Do not
write a second version of that test; import or mirror it exactly, and add a
unit test that a row edited on `venue_id` survives a full `--apply` untouched.

A row edited on `location_text` is **not** skipped — an operator who corrected
the location text improved the input this script reads, and honouring the
correction is the point. The resulting link is still recorded as automatic,
because the ladder made it.

### C. Detach honestly: `venue_not_in_catalog` is not `unresolved_venue`
For the ~385 rows whose own text names a handle we do not carry, the two
existing outcomes are both wrong. `RESOLUTION_UNRESOLVED` plus
`REVIEW_REASON_UNRESOLVED_VENUE` says "we could not tell where this is". We can
tell exactly where it is. It is at `@donanapubnatal`, and we do not have
`@donanapubnatal`.

Add a review reason — `venue_not_in_catalog` — set when the event's own
`location_text` yields an `@`-mention (or a name) that resolves to **no known
venue**, as distinct from yielding nothing to resolve at all. It is a value, not
a column, and it joins `review_reason`'s existing `"; "`-separated list through
the same join `reconcile_post_events` writes.

This buys three things at once: the review queue can filter out a decision no
human can make; the count becomes the venue-acquisition backlog (§E); and the
distinction is honest, which the current one is not.

**Coordination note, and a real seam with the sibling plan.** The forward path
will produce exactly these rows from the next crawl onward, so the constant
belongs in `app/services/event_reconciliation.py` beside
`REVIEW_REASON_UNRESOLVED_VENUE`, and `fix/event-attribution-and-dates`'s §A
must set it too. `260812_event-attribution-and-dates.md` does not currently
mention it. Whichever branch lands second adopts the other's constant; neither
defines a second one. If §A ships first without it, this plan adds it and §A's
call site is updated here — a three-line change, but it must actually happen or
the backfilled rows and the freshly-crawled ones will describe the same
situation with two different words.

**Never silently un-queue, never wrongly resurrect.** Two rules, both of which
reuse machinery rather than re-deriving it:

- Removing a reason: only the `unresolved_venue` / `venue_not_in_catalog`
  tokens may be dropped from a row's `"; "`-joined `review_reason`, and only
  when the row actually gained a venue. `event_merge._fold_review_reason`
  already implements exactly this token-wise drop-and-preserve; import it. A row
  queued for `missing_date` or `year_inferred` stays queued with that reason
  even after its venue is fixed.
- Restoring a status: a row may return to `accepted` only through
  `event_reconciliation.is_clean_extraction`, called with the same
  `min_confidence` the service is configured with — never by the script
  asserting a status. If the predicate says pending, it stays pending.

### D. Collisions are reported, never resolved
Re-attribution changes `venue_id`, which changes
`event_merge.compute_event_identity`'s `(venue_id, date, normalized_title)`. Two
rows can therefore collide after a repair that did not collide before.

**The script never calls `merge_touched_events`.** It writes the four link
columns and stops. Merging has protections the script must not re-implement —
`choose_canonical`'s two-protected-members refusal, the confirmed-canonical
field table, `post_item_source` reattachment — and the next reconciliation of
those posts runs all of it correctly and in the right order.

When a repair *would* create a collision, the script writes the correct venue
anyway and records the pair in its report. Refusing to fix a known-wrong link
because a future merge might have to think about it leaves the worse of the two
states in place.

Two collision shapes to report separately, because they behave differently:

- **Venue identity** — two repaired rows landing on the same
  `(venue_id, date, normalized_title)`. Measured today: **0**.
- **Handle identity** — a *detached* row (venue-less, dated, from
  `oquetemhojeemnatal`) becoming a `_merge_handle_group` candidate that a
  resolved same-handle sibling could absorb on the next crawl. This is the
  second-order hazard the detach creates, and it is the one that could quietly
  move an event to a venue nobody chose. Measured today: **3** candidate
  `(date, title)` groups corpus-wide, all already venue-less, **0** involving a
  row this script touches. The report must recompute it, not trust that number.

### E. Ship it as an operator CLI, and make it prove its own arithmetic
`python -m scripts.backfill_event_venue_links` — dry-run by default, `--apply`
to write. Exactly the shape `scripts/backfill_price_tiers.py` and
`scripts/reconcile_venue_dups.py` already established, down to the docstring
usage block.

Rejected alternatives, on the record:

- **An Alembic data migration.** It runs unattended on deploy with no dry run,
  in a transaction that should be short, and it cannot check that the *code*
  change it depends on (§A) is in the same image. Worse, this repo already
  carries the cost of the last one: `0026_event_sources`'s historical collapse
  calls `merge_event_fields` directly, which is why that function's
  `operator_edited_fields IS NULL` branch is now **frozen forever** and says so
  in its own docstring. One permanently-pinned code path per repair is enough.
- **An admin endpoint.** This runs once. Putting it on the released admin
  surface makes it re-fireable by anyone with the token, forever, for a job that
  will be irrelevant in a week.

Required properties, all testable:

- **Dry-run first and by default.** No flag, no writes. The report is the
  deliverable: per-row before/after `venue_id` and `linked_by`, and totals.
- **Idempotent.** Inputs are the frozen `raw_extraction` and the deterministic
  ladder; the only clock-derived value written is `linked_at`. A second
  `--apply` changes nothing and must report zero changes.
- **Resumable.** Process in `post_item_id` order in batches, with a
  `--since-id` to resume. Every row's decision is independent of every other's.
- **Bookkeeping that cannot fail silently.** This project has been bitten by
  exactly that (`260730_durable-run-records.md`;
  `260710_projection-persistence-integrity.md`). So: count rows selected,
  skipped by each reason, re-pointed, detached, and unchanged; assert
  `selected == sum(everything else)` before exit; and **exit non-zero** if the
  arithmetic does not balance or if any `UPDATE` reports zero rows affected.
  A backfill that prints "done" having written nothing is the failure mode.

## Data, Config, And API Impact
- **Migration** — none. No column changes; only `venue_id`,
  `location_resolution`, `location_confidence`, `linked_by`, `linked_at`,
  `review_reason` and `status` values move.
- **Review reason value** — `venue_not_in_catalog` (§C). Additive; the admin
  console displays `review_reason` as text and gains a new possible value.
  Nothing is removed or repurposed.
- **Config** — none. The script takes the venue-resolution floor/margin from
  `settings` so it cannot disagree with the running pipeline.
- **New file** — `scripts/backfill_event_venue_links.py`.
- **Rollback:** there is no revert for rows already written. The mitigation is
  the dry run plus the report, and the fact that nothing is deleted — every
  changed row's previous `venue_id` appears in the dry-run report, which must
  be captured before `--apply`. State that in the script's usage text.

## Error Handling And Observability
- The script logs, and prints as a report: totals per outcome; the before/after
  `linked_by` distribution; the before/after per-venue item counts for the ten
  venues in the Evidence table; and both collision lists from §D.
- **The venue-acquisition backlog is a first-class output**, not a footnote: the
  handles named by detached rows, ranked by how many items each would recover.
  ~385 rows resolve to roughly 150 handles; the top ten are worth more than the
  tail combined, and this list is the only place that fact exists.
- No new Prometheus metrics. This is a one-shot CLI, not a runtime path — a
  counter that increments once and never again is worse than the report. Note
  that `EVENTS_TOTAL{status}` will move sharply on the next pipeline run
  (`update_events_gauge` re-snapshots it), so warn whoever watches dashboards
  before running it.
- Exit codes: 0 clean, non-zero on unbalanced arithmetic, on a zero-row
  `UPDATE`, or on the §A dependency check failing.

## Test Plan

Feature file: `tests/bdd/enrichment/backfill-misattributed-links.feature`

`enrichment/`, not `persistence/`: the behaviour under test is venue
attribution, which is where `event-venue-targeting.feature` and
`stream-dedupe-and-venue-attribution.feature` already live. Nothing here changes
a persistence boundary.

Scenarios:
- Re-point an item to the venue named in its own stored location text.
- Re-point twenty items from one roundup post to their own venues.
- Detach an item whose own text names a venue the catalog does not hold, and
  record that reason rather than "unresolved".
- Leave a confirmed item completely untouched.
- Leave a manually-linked item completely untouched.
- Leave an item whose operator edited its venue completely untouched.
- Re-resolve from an operator-corrected location text.
- Leave a superseded item untouched.
- Leave an extraction-failed placeholder untouched.
- Keep an item queued for a date problem queued after its venue is fixed.
- Return a repaired item to accepted only when the extraction is otherwise
  clean.
- Report every change and write nothing when run without `--apply`.
- Change nothing on a second `--apply` run.
- Report a post-repair identity collision without merging the two rows.
- Report a detached item that a resolved same-handle sibling could later absorb.

Pytest unit tests:
- The dependency guard: the script refuses to run against a build without §A's
  per-event link method.
- Selection: exactly the `linked_by = caption-mention` population, and nothing
  else, for a fixture mixing every `linked_by` value and every status.
- Per-status action table from §B, one case per row of it.
- `operator_edited_fields` containing `venue_id` refuses; containing
  `location_text` does not.
- Review-reason folding: `unresolved_venue` dropped when a venue is found;
  `missing_date` preserved; both preserved when no venue is found; no reason
  duplicated.
- Status restoration goes through `is_clean_extraction`, asserted by feeding a
  row that gains a venue but has confidence below the floor and must stay
  pending.
- Arithmetic balance: a fixture where a row is skipped for each distinct reason,
  asserting the totals sum to the selection and the exit code is 0; and a
  fabricated imbalance asserting a non-zero exit.
- Idempotency: two consecutive `--apply` passes over the same fixture, the
  second reporting zero changes.
- Collision detection: a fixture where two repaired rows share
  `(venue_id, date, normalized_title)`, asserting both are written and the pair
  is reported.

**Assertions must name the venue, not count the rows.** A count-based assertion
already stayed green here against a deliberately reintroduced wrong-handle bug,
because both passes computed the same wrong number.

Manual or integration checks:
- Run the dry run against a **restored snapshot** of production, never against
  production itself, and diff its report against the Evidence table's per-venue
  counts. Teatro Riachuelo must drop from 129 to single digits; if it does not,
  the ladder is not doing what this plan assumes and nothing should be applied.
- Capture the dry-run report as a file before `--apply`. It is the only record
  of the previous `venue_id` values.

## Acceptance Criteria
- Every row whose venue came from a caption mention is re-decided from its own
  stored location text, or skipped for a reason the report names.
- No confirmed, manually-linked, or `venue_id`-edited row changes.
- No superseded or extraction-failed row changes.
- A row detached because its venue is not in the catalog says so, distinctly
  from a row we simply could not resolve.
- No row is un-queued while it still has a non-venue reason to be queued.
- A dry run writes nothing; a second `--apply` changes nothing.
- Post-repair identity collisions are reported and left unmerged.
- The report names the handles the detached rows point at, ranked.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None.
