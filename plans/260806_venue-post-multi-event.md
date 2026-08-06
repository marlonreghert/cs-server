# Venue-Post Multi-Event — a venue's own post can announce several events

## Branch
feature/venue-post-multi-event

## Goal
Let a venue's own Instagram post produce more than one event, closing the half
of multi-event extraction that `260806_multi-event-posts.md` left open — and do
it by **sharing** the reconciliation the promoter path already has, not by
growing a second copy of it.

## Non-goals
- **Re-attributing a venue's event to another venue.** A venue post's events
  belong to the venue that posted them. See §D.
- **A new migration.** `0025_multi_event_posts` already added
  `source_event_key`, `source_event_index` and the three-column unique
  constraint. Nothing schema-shaped is missing.
- **Changing the resolution ladder**, the date resolver, the qualification
  pre-filter, or the extraction prompt.
- **Serving events to the app.** Still admin-only.

## Evidence

**#147 implemented multi-event in one service only.** `PromoterCrawlService`
calls `openai_client.extract_events()` and reconciles a list;
`EventExtractionService._extract_one` (app/services/event_extraction_service.py:349)
still calls the singular `extract()`, parses one object, and writes one row. So
a venue announcing two nights in one post silently keeps the first and drops the
rest — the same invisible loss the promoter plan was written to stop, in the
other half of the pipeline.

**Everything expensive already exists**, and is reusable as-is:

| Piece | Where |
|---|---|
| `extract_events()` → `(raw_text, truncated)` | `app/api/openai_event_extraction_client.py:359` |
| `parse_multi_event_extraction_response()` (per-item isolation) | same file, `:255` |
| `compute_source_event_key(title, starts_at)` | `app/services/event_identity.py` |
| `source_event_key` / `source_event_index` + 3-column constraint | migration `0025` |
| `EVENT_EXTRACTION_EVENTS_PER_POST`, `EVENT_EXTRACTION_MALFORMED_EVENTS_TOTAL` | `app/metrics.py` |

**The two services have already drifted, in the operator's disfavour.** The
venue path's `_preserve_confirmed`
(`app/services/event_extraction_service.py:447`) updates `raw_extraction` and
`last_seen_at` **and flags `review_reason = DIVERGES_FROM_CONFIRMED`** when the
model's title or date no longer matches what the operator confirmed. The
promoter path's confirmed branch
(`app/services/promoter_crawl_service.py:~447`) updates the same two fields and
**flags nothing** — so a confirmed promoter event whose source post materially
changed looks untouched.

That is the argument against duplicating: the two copies have been out of step
since the day the second one was written, and the field they disagree about is
the one that tells an operator their confirmation may be stale.

**The venue path's outcome vocabulary is short a value.**
`EVENT_EXTRACTION_POSTS_TOTAL` documents `extracted, not_event_like, no_date,
low_confidence, extraction_failed, skipped_seen, unread_time` — no `truncated`,
because only the promoter path could truncate until now.

## Current Behavior
A venue post yields exactly one event. A second event in the same caption or
flyer is discarded with no row, no review-queue entry and no counter.

## Desired Behavior
1. Extract a **list** of events from a venue's post; a single-event post yields
   a list of one and behaves exactly as it does today.
2. Attribute every event in a venue post to the venue that posted it.
3. Resolve each event's date independently against the post's timestamp.
4. Key each event by content (`source_event_key`), never by list position, so
   re-extraction is idempotent across a reordered list.
5. Preserve a `confirmed` event and flag a divergence when the model's title or
   date has moved — on **both** the venue and promoter paths.
6. Preserve a manually linked event.
7. Supersede an event a later run no longer finds, never hard-delete it, and
   never touch it once confirmed or manually linked.
8. Persist nothing partial on a truncated response, and count `truncated` on
   the venue path too.
9. Skip one malformed event in a list without losing its siblings.
10. Record how many events each post produced.

## Implementation Approach

### A. One reconciliation, two callers
Extract the per-post reconciliation into `app/services/event_reconciliation.py`
and have **both** services call it. It owns exactly the behaviour that must
never differ between them:

- index existing rows for `(source_handle, source_shortcode)` by
  `source_event_key`;
- per parsed event: resolve the date against the post timestamp, compute the
  key, record it as seen;
- confirmed → update `raw_extraction` + `last_seen_at`, flag divergence;
- manually linked → preserve the operator's venue fields;
- otherwise upsert;
- supersede every existing key this run did not return, skipping confirmed and
  manually linked rows;
- observe `EVENT_EXTRACTION_EVENTS_PER_POST`.

### B. The one thing that genuinely differs: venue attribution
The promoter path must run the resolution ladder per event; the venue path
already knows the venue. That is the **only** real difference, so it is the only
thing parameterised — the caller supplies a per-event attribution step:

- promoter: run the existing ladder on **that event's own** `location_text`;
- venue: attach the owning `venue_id`, `location_resolution` unchanged.

Nothing else may be parameterised. A knob for "should confirmed events be
preserved" or "should missing events be superseded" would re-create the drift
this refactor exists to remove.

### C. Migrating the promoter path is part of the work, not a follow-up
The promoter path moves onto the shared module in this change. Leaving it
behind would mean three copies instead of two.

Its 14 `multi-event-posts.feature` scenarios plus the wider promoter and
extraction suites are the regression guard, and they are strong: they already
pin reordering, confirmed/manual preservation, supersession, truncation,
per-event venues and malformed isolation. **Every one of them must still pass
untouched.** If a promoter scenario needs editing to accommodate the refactor,
that is a signal the refactor changed behaviour — stop and report rather than
adjusting the scenario.

The promoter path **gains** divergence flagging by moving onto the shared
behaviour. That is a deliberate, desirable change and needs its own scenario.

### D. A venue post's `location_text` does not re-attribute
A venue's post that names a location keeps `location_text` recorded, and the
event still belongs to the posting venue. Re-attributing a venue's own post to
somewhere else on a fuzzy name match would be a surprising behaviour change, and
it would need the ladder the venue path deliberately does not run. Stated so the
next reader does not "fix" it.

### E. First run over already-extracted posts
Existing venue rows were back-filled with a key by `0025`. On re-extraction the
model may phrase a title slightly differently, producing a different key: the
old row is **superseded** and a new one inserted. That is the designed
consequence of content-derived identity — nothing is deleted, and confirmed or
manually linked rows are exempt by rule.

Worth naming because it will look like churn on the first run and is not.

## Data, Config, And API Impact
- **Migration:** none. `0025` already carries the schema.
- **Settings:** reuse `event_extraction_max_events_per_post`.
- **API:** none. `GET /admin/events` already returns the fields.
- **Behaviour:** a venue post may now produce several rows. `source_kind` stays
  `venue_post` for all of them.
- **Serving:** none. `redis_projection_service` untouched, no app-facing
  response changes.

## Error Handling And Observability
Truncation persists nothing and is recorded, per post, before any parsing. One
malformed event is skipped and counted; its siblings persist.

Metrics:
- `EVENT_EXTRACTION_POSTS_TOTAL` gains `truncated` (and the docstring's outcome
  list is updated with it — a counter whose documented values are stale is how
  the next reader mis-reads a dashboard).
- `EVENT_EXTRACTION_EVENTS_PER_POST` now observes venue posts as well as
  promoter posts. A venue path collapsing back to one event per post is only
  visible in that distribution.

## Test Plan
Feature file: `tests/bdd/enrichment/venue-post-multi-event.feature`

Scenarios:
- Extract three events from one venue post and persist three rows.
- Attribute every event in a venue post to the posting venue.
- Resolve each event's date independently against the post timestamp.
- Yield exactly one event, unchanged, from a single-event venue post.
- Produce no duplicates when a re-extraction returns the events reordered.
- Preserve a confirmed venue event across a reordered re-extraction.
- Flag a divergence when a confirmed event's title no longer matches.
- Flag a divergence when a confirmed event's date no longer matches.
- Flag a divergence on a confirmed **promoter** event too — the behaviour the
  promoter path gains here.
- Supersede a venue event a later run no longer returns.
- Never supersede a confirmed or manually linked venue event that disappears.
- Persist nothing and count `truncated` when the response is cut off.
- Skip one malformed event and keep its siblings.
- Record a venue post's event count in the events-per-post distribution.
- Keep `location_text` on a venue event without re-attributing it.

Pytest unit tests:
- The shared reconciliation driven directly: confirmed, manual, supersede,
  reorder, and the divergence flag, each asserted once rather than once per
  caller.
- Both attribution strategies: venue attaches the owning id; promoter runs the
  ladder on the event's own `location_text` and never a sibling's.
- Venue-path truncation maps to `truncated` and writes nothing.
- A single-event venue post produces byte-identical persisted fields to the
  pre-change behaviour — the regression that matters most, since every existing
  venue event was written by the old path.

Manual or integration checks:
- Re-run extraction over the captured `@metropolerecife` posts and confirm the
  single-event flyers still yield one event each, with the same dates verified
  earlier (2026-08-07 and 2026-08-08).

## Acceptance Criteria
- A venue post can persist several events, each attributed to the posting venue.
- A single-event venue post is unchanged in count and in persisted fields.
- Re-extraction is idempotent across a reordered list.
- Confirmed and manually linked events survive re-extraction and disappearance,
  on both paths, and a divergence is flagged on both.
- A truncated response persists nothing and counts `truncated`.
- One reconciliation implementation exists, not two.
- Every existing promoter and extraction scenario passes **unedited**.
- `make test-feature`, `make test-unit` and `make test-bdd` pass.

## Open Questions
None.
