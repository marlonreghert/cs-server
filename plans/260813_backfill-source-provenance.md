# Backfill Source Provenance — recover upload time and media type from the archive

## Branch
fix/backfill-source-provenance

## Goal
Every existing `post_item_source` row carries the `source_uploaded_at` and
`source_media_type` its archived manifest already recorded, so date repair has
an anchor.

## Non-goals
- **Changing how new rows are written.** `260812_crawl-error-visibility.md` §C
  (venue path) and `260813_promoter-source-provenance-parity.md` (promoter path)
  both shipped; new rows already carry both fields.
- **Re-extracting anything.** No model call, no Apify call.
- **Repairing dates.** `260813_history-repair-dates.md` consumes this plan's
  output; it does not belong here.

## Evidence

### The columns are nearly empty where it matters
Measured in production 2026-08-13, after both forward fixes had deployed:

| source_kind | total | `source_uploaded_at` | `source_media_type` |
|---|---|---|---|
| `promoter_post` | 587 | 20 | 20 |
| `venue_post` | 113 | 7 | 7 |

Only rows written since the fixes carry them. The other 673 are null, and they
are the entire historical corpus.

### The values are sitting in S3
`archive_sources.py` writes a manifest record per archived post carrying
`uploaded_at` (the post's own timestamp) and `post_type` (the media type), along
with `shortcode` and `permalink`. Nothing needs to be re-derived, re-fetched or
guessed — only read across and written down.

### It blocks date repair
`260813_history-repair-dates.md` anchors every re-resolved date on
`source_uploaded_at` and refuses to run when too many sources have a null
anchor. Without this back-fill it cannot run on the historical rows that are the
whole point of it.

## Current Behavior
673 of 700 source rows have no record of when their post was posted or what
medium it was, though the archive recorded both at crawl time.

## Desired Behavior
1. Populate both columns from the archive manifest.
2. Never invent a value.
3. Report what could not be matched, and why.
4. Change nothing twice.

## Implementation Approach

An operator script in the family
`260812_backfill-misattributed-links.md` established —
`python -m scripts.backfill_source_provenance`, **dry-run by default**,
`--apply` to write, idempotent, resumable.

### A. Match a source row to its manifest record
`post_item_source` carries `source_shortcode` and `cover_photo_key`; the
manifest carries `shortcode` and the S3 layout the key was built from. Establish
the join from what is actually stored — read
`docs/venue-retrieval-storage.md` before touching S3 paths, several invariants
there are load-bearing — and say in the PR which key you joined on and why.

**A row whose manifest record cannot be found is reported, not guessed.** Say
how many, and for which handles; a large unmatched count means the join is wrong,
not that the archive is missing.

### B. Write only what the manifest states
An absent, empty or unparseable `uploaded_at` leaves the column NULL. **Never
substitute the crawl time**, `first_seen_at`, or `now()` — the whole reason this
column exists is that those are different facts, and a wrong anchor is worse for
date repair than a null it knows how to skip.

Never overwrite a value already present: rows written since the forward fixes
have the value from the live crawl, which is at least as good.

### C. Read-only against S3
List and read manifests; write nothing to the bucket. No Apify, no OpenAI.
The archive is the system of record for this and must not be modified by a
repair that reads it.

## Data, Config, And API Impact
- **Migration** — none. Both columns exist (`0036_source_media_type`).
- **Config** — none.
- **New file** — `scripts/backfill_source_provenance.py`.
- **Rollback:** none for rows already written, but the risk is low: the script
  only fills nulls with recorded facts and never overwrites. The dry-run report
  is still the record to capture first.

## Error Handling And Observability
- Report counts by disposition: filled, already-present, unmatched, unparseable.
- **Watch the unmatched count.** It is the correctness signal for §A's join; a
  high value means the join key is wrong and the run should be abandoned rather
  than applied.
- A single unreadable manifest must not abort the run — log, count, continue,
  stay resumable.

## Test Plan
Feature file: `tests/bdd/enrichment/backfill-source-provenance.feature`

Scenarios:
- Fill both fields from an archived manifest record.
- Leave a row that already has values untouched.
- Leave a row whose manifest has no upload time with a null upload time.
- Never substitute the crawl time for a missing upload time.
- Report a row whose manifest record cannot be found.
- Report every change and write nothing without apply.
- Change nothing on a second apply.
- Continue past an unreadable manifest.

Pytest unit tests:
- The manifest join, including a shortcode present in several runs (take the
  record that produced this source row, and say how you decide).
- Timestamp parsing: valid, empty, missing, malformed.
- Media type passes through verbatim, including an unrecognised value.
- Idempotency: a second pass produces an empty change set.

Manual or integration checks:
- Dry-run against production **read-only** and attach the disposition counts to
  the PR. S3 reads are cheap; do not skip this.

## Acceptance Criteria
- Both columns are populated wherever the archive recorded them.
- No value is invented and no existing value is overwritten.
- Unmatched rows are reported with enough detail to diagnose the join.
- A second `--apply` changes nothing.
- Nothing is written to S3.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
