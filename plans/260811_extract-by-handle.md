# Extract By Handle — make archived posts re-readable

## Branch
feature/extract-by-handle

## Goal
Let event extraction run over the posts archived under an Instagram **handle**,
so anything already in S3 can be re-extracted without re-crawling and
re-paying Apify.

## Non-goals
- **Re-crawling.** This reads what is already archived. It must never call Apify.
- **Changing how posts are archived.** The `promoter=<handle>` prefix stays as
  it is here; see §Evidence for why renaming it is a separate, riskier change.
- **Promotion and menu entities.** Separate, planned with the operator.
- **Back-filling old rows.** Re-extraction is an operator action, not a migration.

## Evidence

### Shared-handle posts are write-only today
`260810_post-kind-and-post-extraction-attribution.md` §C moved shared-handle
archiving under the handle. Production, after one crawl of `@entreamigosobode`:

```
promoter=entreamigosobode              115 objects
venue_id=ven_677a3743…                 153 objects
venue_id=ven_3065395457…               144 objects
```

`EventExtractionService` accepts two eligibility modes, `event_candidates` and
`venue_ids`, and `EventPostSource` reads archived posts under a **`venue_id=`**
prefix. Neither can reach a `promoter=` prefix.

Measured consequence — the operator triggered extraction with:

```json
{"eligibility": {"mode": "venue_ids", "venue_ids": "ven_49454b…,ven_6b3367…"},
 "lookback_days": 120, "max_posts_per_venue": 30}
```

and the job logged **`completed in 0.6s`**: zero posts read, no OpenAI call, no
row touched. The four `FÉRIAS AMIGOS PARK` events still carry their wrong 2027
dates, because the fix that corrects them could not be given the posts.

Every shared-handle post is therefore extracted exactly once, on the way in, and
is unreachable afterwards. That also invalidates a premise this project has
already relied on: `260810_post-kind-…` §B drops non-event posts without
persisting them, justified by *"re-extraction reads archived posts back out of
S3, never re-calls Apify, so nothing is unrecoverable."* True for `venue_id=`
prefixes, false for `promoter=` ones. This plan makes that premise true again.

### Why not just rename the prefix
A venue's own account being filed under `promoter=` is genuinely wrong, and
`260810_date-correctness-…` already corrected the matching `source_kind` column
so the two now disagree. But renaming a prefix strands every object already
written under the old one, and `cover_photo_key`s pointing at those objects are
already stored on `events.event_source` rows and already served to the console.
Reading by handle works for both spellings and strands nothing. **Fix the read
path first; the prefix is a separate decision with a migration attached.**

## Current Behavior
Extraction can be pointed at event candidates or at explicit venue ids, both of
which resolve to `venue_id=` archive prefixes. Posts archived under a handle
cannot be extracted again by any supported means.

## Desired Behavior
1. Extraction accepts a list of handles and reads the posts archived under them.
2. It reads whatever prefix those posts were actually written to.
3. It never calls Apify.
4. Re-extracting a post updates the existing event rather than duplicating it.
5. The two existing modes behave exactly as they do today.

## Implementation Approach

### A. A third eligibility mode
Add `mode: "handles"`, taking `handles` as a comma-separated string or a list —
matching how `promoter_event_crawl` already accepts handles, so an operator does
not meet two spellings of the same idea.

`EventPostSource` gains a by-handle read alongside its by-venue read. It must
resolve **both** archive spellings — `promoter=<handle>` and, when a handle maps
to venues, their `venue_id=` prefixes — because a handle's history can span both
shapes: `@entreamigosobode` was archived per-venue before
`260810_post-kind-…` and under the handle after it. A mode that silently reads
only the newer half would look like it worked while skipping older posts.

Deduplicate by shortcode across the prefixes it reads, preferring the newest
archived copy. The dedupe rule already exists from
`260810_stream-dedupe-and-venue-attribution.md` §A — reuse it rather than
writing a second one.

### B. Attribution and kind on the re-extraction path
Re-extraction must apply the **same** venue attribution, kind classification and
date resolution as a first pass — it is the same work over the same bytes, and
an operator re-running extraction to pick up a date fix expects the current
rules, not the rules that applied when the post was first seen.

Idempotency already holds: `uq_event_source_post` keys on
`(source_handle, source_shortcode, source_event_key)`, so a re-extraction with
an unchanged title and date updates its row. **A corrected date changes
`source_event_key` and therefore inserts a new row rather than updating the old
one** — the four `FÉRIAS` events would become eight, four right and four wrong.
Handle this explicitly: when re-extracting a post, supersede the event rows that
the *same post* previously produced and that the new pass did not re-emit. Do
not delete them; `superseded` is the existing status for exactly this.

**This is the single riskiest part of the plan.** A superseding rule that is too
eager erases events an operator has curated; too timid and every date fix
doubles the catalog. Cover both directions in tests, and respect
`operator_edited_fields` — a field a human has edited is not overwritten by a
re-extraction, per `260810_...event-merge` protection.

### C. Cost, stated before it is spent
Re-extraction is free of Apify but **not** free of OpenAI: it is a vision call
per qualifying post. Log the number of posts about to be extracted before
extracting them, and honour `max_posts_per_venue` as a cap in this mode too
(rename its meaning to per-handle where it applies, without renaming the key —
an operator's saved config must keep working).

## Data, Config, And API Impact
- **No migration.**
- **Config:** `eligibility.mode` gains `"handles"`; `handles` joins the accepted
  keys. Additive — an unknown mode still raises, as today.
- **Rollback:** revert. Nothing new is persisted.

## Error Handling And Observability
- An unknown handle, or one with nothing archived, is a **no-op with a clear
  log line**, never an error — an operator typing a handle that has not been
  crawled should be told so, not shown a traceback.
- `event_extraction_posts_total{kind}` already exists; this mode must emit it,
  since the whole point is that it does the same work.
- Count superseded-on-re-extraction separately from superseded-by-a-newer-post.
  They mean different things and conflating them hides §B going wrong.

## Test Plan
Feature file: `tests/bdd/enrichment/extract-by-handle.feature`

Scenarios:
- Extract the posts archived under a handle.
- Read a handle whose posts were archived under a venue prefix.
- Read a handle whose posts span both archive spellings.
- Process a post archived twice under different prefixes only once.
- Report a handle with nothing archived without failing.
- Never call Apify when extracting by handle.
- Apply the current date rules when re-extracting an old post.
- Supersede an event whose corrected date changed its identity.
- Keep an operator's edited field through a re-extraction.
- Leave the venue-ids mode behaving exactly as before.

Pytest unit tests:
- Config parsing: handles as string, as list, empty, whitespace, unknown mode.
- The prefix resolver over: handle-only, venue-only, both, neither.
- Dedupe across prefixes prefers the newest archived copy.
- Supersession: a changed date supersedes the stale row and inserts the
  corrected one; an unchanged post updates in place and supersedes nothing.
- `max_posts_per_venue` caps the by-handle read.
- The real case: the four `FÉRIAS` date texts re-extracted from an archived
  post resolve to 2026 and leave exactly four live events, not eight.

## Acceptance Criteria
- `{"eligibility": {"mode": "handles", "handles": "entreamigosobode"}}` reads
  that handle's archived posts and re-extracts them.
- Apify is never called.
- Re-extraction applies current date, kind and attribution rules.
- A corrected date leaves one live event, not two.
- Operator-edited fields survive.
- Existing modes are unchanged.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None. If §B's supersession cannot be made safe for operator-curated rows, stop
and report rather than shipping a rule that quietly rewrites them.
