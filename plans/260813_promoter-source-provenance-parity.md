# Promoter Source Provenance Parity — record when a post was posted, whichever path read it

## Branch
fix/promoter-source-provenance-parity

## Goal
`source_uploaded_at` and `source_media_type` are populated for every
`post_item_source`, not only for those the venue-extraction path wrote.

## Non-goals
- **Back-filling existing rows.** `260812_crawl-error-visibility.md` §C owns
  that (from the S3 manifest) and it is still deferred. This plan stops the
  hole widening; it does not close what is already open.
- **Changing what either column means.**
- **Anything about attribution, dates or dedup.**

## Evidence

### Half the pipeline records provenance, half does not
`260812_crawl-error-visibility.md` §C added `source_media_type` and
`source_uploaded_at`. `EventExtractionService` writes both
(`event_extraction_service.py:1054-1055`, from `ArchivedPost`).
`PromoterCrawlService` writes neither, and says so in a comment at
`promoter_crawl_service.py:666-673`:

> `source_media_type`/`source_uploaded_at` (§C) are NOT set here: that plan's
> own scope is venue posts, and this path has no `ArchivedPost.media_type`/
> `.timestamp` equivalent wired to it … left for a follow-up, not silently
> guessed.

Declining to guess was right. But the premise is wrong, and that is why this is
a small plan rather than a large one.

### The values are already in hand, under confusing names
The promoter path holds the **raw Apify dict** that
`apify_instagram_client.fetch_recent_posts` built, and that dict already carries
both facts:

```python
"timestamp": item.get("timestamp", ""),   # the post's own upload time
"post_type": item.get("type", "image"),   # "Video" | "Image" | "Sidecar"
```

**The trap is the name.** That dict's `post_type` is the **media type**, while
`events.post_item.post_type` is `event | promotion | menu | food | other` — two
unrelated things one word apart. Writing `source_media_type = post["post_type"]`
reads like a bug and is correct. Say so at the assignment, or someone will
"fix" it.

### Measured impact
An incremental promoter crawl of `oquetemhojeemnatal` on 2026-08-13 produced 21
`post_item_source` rows: **`source_uploaded_at` populated on 0, `source_media_type`
on 0**. Promoter roundups are the bulk of this corpus, so in practice the
columns are close to empty where it matters most.

### It blocks a whole plan
`260813_history-repair-dates.md` anchors every re-resolved date on
`source_uploaded_at` and refuses to run when too many sources have a null
anchor — "no anchor, no answer, report it as skipped rather than guessing with
`now()`". With the promoter path never writing it, that repair cannot run on the
rows that most need it.

## Current Behavior
A source row written by the promoter path has no record of when its post was
posted or what medium it was, though both values were in memory when the row
was written.

## Desired Behavior
1. The promoter path records both facts.
2. A missing or unparseable value is stored as NULL, never invented.
3. The venue path is untouched.

## Implementation Approach

### A. Write both fields on the promoter path
Map the raw dict's `timestamp` → `source_uploaded_at` and its `post_type` →
`source_media_type`, alongside the `source_events_truncated` and
`date_interpretation` assignments that already sit there.

Comment the collision at the assignment itself, not only in this plan.

### B. Parse the timestamp; never invent one
`fetch_recent_posts` defaults `timestamp` to `""`, so the value reaching this
code can be an empty string as easily as an ISO instant. An empty, missing or
unparseable timestamp must store **NULL**.

**Never fall back to `now()`, and never reuse `first_seen_at`.** Those are crawl
times, not post times; substituting one silently would give
`260813_history-repair-dates.md` a confidently wrong anchor and shift every
date it repairs — worse than the null it knows how to skip.

Reuse whatever parsing the venue path already applies to `ArchivedPost.timestamp`
rather than writing a second one; if the venue path relies on the archive having
parsed it upstream, put the shared helper somewhere both can import and say
where.

### C. Check the third path
`instagram_crawl_service._chain_shared_handle` is a **second** pipeline with its
own instrumentation — this project has already been misled twice by assuming a
fix on one path covered the other. Establish which of the two writers it
delegates to and state the answer in the PR. If it writes source rows itself,
it is in scope for §A too.

## Data, Config, And API Impact
- **Migration** — none. Both columns exist (`0036_source_media_type`).
- **Config / API** — none.
- **Rollback:** revert. Columns stay nullable; rows already written keep their
  values.

## Error Handling And Observability
- Count source rows written with a null `source_uploaded_at`, labelled by
  writing path. **That ratio is the readiness gate for
  `260813_history-repair-dates.md`** — it is exactly the "too many null anchors"
  check that plan refuses to run without.
- A null from a genuinely absent Apify timestamp is normal and must not log an
  error; an unparseable non-empty value should log at warning, since that means
  Apify changed a format.

## Test Plan
Feature file: `tests/bdd/enrichment/promoter-source-provenance-parity.feature`

Scenarios:
- Record a promoter post's upload time on every event it yields.
- Record a promoter post's media type on every event it yields.
- Store no upload time when the post carries none.
- Store no upload time when the post's timestamp is unparseable.
- Never substitute the crawl time for a missing upload time.
- Keep recording both fields on the venue path.
- Give every event from one post the same provenance values.

Pytest unit tests:
- Timestamp parsing: a valid ISO instant; an empty string; a missing key; a
  malformed string. The first yields an instant, the rest yield None.
- `source_media_type` takes the raw dict's `post_type` and is **not** confused
  with the stored `post_item.post_type` — assert a row whose media type is
  `"Video"` and whose item type is `"event"` holds both values distinctly. This
  is the regression test for the naming collision.
- The venue path's existing assignments are unchanged.

Manual or integration checks:
- After deploy, re-run one incremental promoter crawl and confirm the new source
  rows carry both fields. One target, once — it costs Apify results.

## Acceptance Criteria
- A promoter-path source row carries `source_uploaded_at` and
  `source_media_type` whenever the post supplied them.
- A missing or unparseable timestamp stores NULL, and no crawl time is ever
  substituted.
- The venue path is provably unchanged.
- `_chain_shared_handle`'s writer is identified in the PR.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
