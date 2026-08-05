# Instagram Event Extraction — turn archived posts into structured events

## Branch
feature/instagram-event-extraction

## Goal
Read the archived Instagram posts of event-candidate venues and produce
structured event records in RDS — title, start time, lineup, ticket link, price
— with an honest confidence and a review status, so an operator can see and
correct what the pipeline believes before anything downstream trusts it.

## Non-goals
- **Promoter accounts and location resolution.** Every event here comes from a
  venue's own handle, so its venue is known by construction. Events whose venue
  must be inferred are `260804_instagram-promoter-events.md`.
- **Serving events to the app.** No Redis projection, no public API, no mobile
  DTO. The serving projection is untouched, so the data-flow invariant is not
  engaged in this cycle.
- **The review UI.** This ships the admin API; the console is
  `vibes_bot/plans/260804_event-review-console.md`.
- **Scraping.** This reads what `260804_instagram-media-archive.md` archived and
  what `instagram.posts` already holds. It starts no actor run.
- **Deleting or expiring past events.** Retention is a later decision; an event
  simply ages.

## Evidence

**There is no event concept anywhere in the stack.** `grep -ril event` across
`cs-server/app` and `vibes_bot/app` returns only `hot_like_event` (engagement)
and unrelated identifiers. No table, no model, no endpoint. This is greenfield.

**The corpus will exist.** `260804_instagram-media-archive.md` puts post images
under `retrieved/source=instagram_posts/…` with a manifest carrying caption,
permalink, shortcode and post timestamp, and adds a `flyer` photo category.
`260804_event-venue-targeting.md` provides `eligibility.mode =
event_candidates`.

**A cheap pre-filter gating an expensive model call is an established pattern
here.** §4 of `docs/venue-retrieval-storage.md`: a photo whose `legible` is `no`
is never sent to the menu extractor, "so the classifier pays for itself in OCR
calls not made" — and only a *confident* `no` drops a photo, because "could not
tell" is not "unreadable". Event extraction reuses the shape exactly.

**Vision cost is measured, not assumed, and the measurement was once wrong by
9x** (§4): gpt-4o-mini bills a low-detail thumbnail at ~2,833 tokens, not the 85
that gpt-4o's card implies. Any cost claim in this plan is therefore a figure to
verify at execution, not a constant to trust.

**Batching above ~10 images silently loses verdicts** (§4) — at 20 the model
returned 16 verdicts for 20 images, and a flat `max_tokens` truncated the JSON
so the whole batch fell back. Event extraction is one post per call for a
different reason (see below), which sidesteps this, but the failure mode is why
per-item accountability is a requirement rather than a nicety.

**Timezone behavior is load-bearing.** `CLAUDE.md` requires preserving BestTime
day-index and Recife timezone behavior unless a plan proves the current
behavior wrong. Event times inherit America/Recife.

## Current Behavior
Archived Instagram flyers and captions sit in S3 and RDS as unread text and
pixels. Nothing knows that a post announces a party on Friday.

## Desired Behavior
1. Select posts belonging to event-candidate venues within a lookback window.
2. Send a post to the extractor only when it is plausibly an event — its image
   classified `flyer`, or its caption matched an event marker. Everything else
   costs nothing.
3. Extract one structured event per qualifying post: title, start (and end when
   stated), lineup, ticket URL, price text, and a free-text location string when
   the post names one.
4. Resolve relative and partial dates **against the post's own timestamp**, not
   against the time the job runs.
5. Never invent a date. An unparseable or ambiguous date stores NULL and marks
   the record for review.
6. Represent a recurring announcement as recurring, with the next occurrence as
   its start.
7. Be idempotent: re-extracting the same post updates its event in place.
8. Store a per-record confidence and a review status, and expose an admin API to
   list, inspect, correct, confirm and reject.
9. Never let a model failure lose a post — an unextractable post is recorded as
   such and can be retried without re-archiving.

## Implementation Approach

### A. The pre-filter decides the bill
A post qualifies if any archived image of it classified `flyer` above the
category confidence floor, **or** its caption matches the event-marker matcher
built in `260804_event-venue-targeting.md` (shared, not duplicated). Everything
else is skipped and counted `not_event_like`.

Both signals are already paid for. The whole cost control of this feature is
that the model only ever sees posts two free signals already agree are worth
looking at.

### B. One call per post, with the flyer image
Text alone loses most of the content: on a Brazilian event flyer the date, the
line-up and the door price are pixels, not caption. So the call carries the
caption **and** the flyer image — which is exactly why the archive had to exist
first, since the original Instagram URL has expired by now and the archived copy
is readable through the `GetObject` grant on `retrieved/*` that
`infra/datalake/iam.tf` already holds for `rederive_attributes`.

One post per call, not a batch. A batch shares one output budget across posts,
and this schema's output is large and variable — a flyer with a twelve-act
line-up produces many times the tokens of a single-DJ night. The measured batch
failure in §4 was precisely a variable-length output overrunning a flat
`max_tokens` and taking its whole batch down with it. Per-post calls cost more
prompt tokens and are worth it: a failure is isolated to one post, and the
retry is one post.

### C. Dates are the hard part, and the post timestamp is the anchor
Rules the extractor must follow, in order:

- **Relative expressions (`hoje`, `amanhã`, `hoje à noite`, `este sábado`)
  resolve against the post's timestamp, never the run's.** A post crawled three
  weeks after it was published would otherwise land on the wrong day, and the
  error is invisible — it produces a perfectly plausible date. This is the
  single most likely way for this feature to be quietly wrong at scale.
- **A date with no year resolves to the next occurrence at or after the post
  date.** `15/08` on a post from July 2026 is 15 August 2026; the same string on
  a post from December 2026 is 15 August 2027.
- **Day-first.** `05/08` is 5 August. The catalog is Brazilian and an American
  reading silently produces a valid, wrong date for every day ≤ 12.
- **A time with no date, or a date the model cannot pin, stores NULL and sets
  `pending_review`.** A guessed date is worse than a missing one: an operator
  will scan a queue of blanks, but will not audit a field that looks answered.
- **Recurring announcements** (`toda quinta`, `todo sábado`) set
  `is_recurring`, store the rule as stated, and set `starts_at` to the next
  occurrence after the post date.
- Times are America/Recife, stored as timestamptz. `22h`, `22:00` and `10pm`
  are the same instant.

### D. Persistence
Migration `0023_event_table` — `events.event`:

| column | notes |
|---|---|
| `event_id` | PK, ULID (time-ordered, same rationale as the archive run id) |
| `venue_id` | FK `venues.venue`, **nullable** — promoter events arrive unlinked in the next plan |
| `source_kind` | `venue_post` here |
| `source_handle`, `source_shortcode`, `source_permalink` | provenance |
| `starts_at`, `ends_at` | timestamptz, nullable |
| `is_recurring`, `recurrence_text` | |
| `title`, `description`, `lineup` (jsonb), `ticket_url`, `price_text` | |
| `location_text` | what the post said, unresolved; the next plan consumes it |
| `cover_photo_key` | S3 key of the flyer |
| `confidence` | float |
| `status` | `pending_review` \| `confirmed` \| `rejected` \| `superseded` |
| `review_reason` | why it is queued |
| `raw_extraction` | jsonb, the model's unedited answer |
| `first_seen_at`, `last_seen_at`, `updated_at` | |

`UNIQUE (source_handle, source_shortcode)` is what makes re-extraction
idempotent — requirement 7 is a constraint, not a code path that can be
forgotten.

`venue_id` is nullable from the start even though every event in *this* plan has
one. Making it NOT NULL now would force a migration on a table with rows in it
one plan later, and nullable-then-populated is the cheaper order.

**A confirmed record is never overwritten by a re-run.** An operator's
correction outranks the model; a re-extraction of a `confirmed` event writes to
`raw_extraction` and `last_seen_at` only, and flags a divergence for review
rather than silently reverting the human's edit.

### E. Job and admin API
`event_extraction` in `JOB_REGISTRY`, config
`{eligibility (default event_candidates), max_venues, max_posts_per_venue,
lookback_days, min_confidence, dry_run}`. Operator-triggered, no cron. `dry_run`
reports which posts qualify and what the run would cost, with zero model calls —
the same contract as the archive estimate.

Admin API: `GET /admin/events` (filter by venue, status, date range),
`GET /admin/events/{id}`, `PATCH /admin/events/{id}`,
`POST /admin/events/{id}/confirm`, `POST /admin/events/{id}/reject`.

## Data, Config, And API Impact
- **Migration:** `0023_event_table` — `events.event` plus indexes on
  `(venue_id, starts_at)`, `status`, and the uniqueness constraint above.
- **Settings:** `event_extraction_model`, `event_extraction_min_confidence`,
  `event_extraction_max_tokens`.
- **API:** the admin routes above. Nothing public.
- **Serving:** none. `redis_projection_service.py` is untouched and no venue
  response changes.
- **Cost:** one vision call per qualifying post. Unknown until measured — the
  first bounded run reports actual tokens, and §4's 9x error is the reason no
  figure is asserted here.

## Error Handling And Observability
A model failure, a truncated response or unparseable JSON records the post as
`extraction_failed` with the raw response kept, and the run continues. Nothing
is deleted and nothing needs re-archiving to retry. A post that yields no event
is recorded as examined, so a second run does not re-pay for the same negative
answer.

Metrics:
- `event_extraction_posts_total{outcome}` — `extracted`, `not_event_like`,
  `no_date`, `low_confidence`, `extraction_failed`, `skipped_seen`.
- `event_extraction_cost_usd` and `openai_tokens_total{endpoint="event_extract"}`
  split by direction, matching the photo-classification convention.
- `events_total{status}` gauge.
- `job_id` stays out of every label (unbounded cardinality, §7); per-run
  narrative goes to Loki.

## Test Plan
Feature file: `tests/bdd/enrichment/instagram-event-extraction.feature`

Scenarios:
- Extract an event from a flyer post of a candidate venue and persist every
  field.
- Skip a post that is neither flyer-classified nor caption-matched, and assert
  **zero** model calls.
- Resolve `amanhã` against the post's timestamp, not the run time — asserted
  with a run clock deliberately weeks later than the post.
- Resolve a day-first date with no year to the next occurrence at or after the
  post date.
- Read `05/08` as 5 August, not 5 May.
- Store NULL and queue for review when the date cannot be determined, and assert
  no date was invented.
- Mark a `toda quinta` announcement recurring with the next Thursday as its
  start.
- Update in place on re-extraction of the same shortcode rather than inserting a
  duplicate.
- Preserve an operator's confirmed event on re-extraction and flag the
  divergence.
- Record `extraction_failed` and continue the run when the model returns
  unparseable JSON.
- Queue an extraction below `min_confidence` for review instead of accepting it.
- Report qualifying posts and cost under `dry_run` with zero model calls.
- List, confirm and reject through the admin API, and assert the status
  transitions.

Pytest unit tests:
- The date resolver as its own unit: relative terms, day-first parsing, missing
  year across a year boundary, weekday+time, ranges (`22h às 04h`), `22h`
  vs `22:00` vs `10pm`, and the strings that must resolve to NULL.
- Recurrence detection and next-occurrence computation, including a post made on
  the recurring weekday itself.
- Timezone: an event at `22h` is 22:00 America/Recife, asserted through a DST-
  free but offset-correct comparison.
- The qualification pre-filter: flyer only, caption only, both, neither, and a
  low-confidence flyer label.
- Idempotency: two extractions of one shortcode leave one row.
- A `confirmed` row survives re-extraction unchanged except `last_seen_at`.
- Response parsing: valid, truncated, extra fields, missing required fields.

Manual or integration checks:
- A bounded live run over 3–5 known event venues; read every extracted event
  against its permalink by hand. Date correctness cannot be asserted from
  fixtures alone — the whole risk of this feature is a plausible wrong date, and
  only real flyers exercise it.

## Acceptance Criteria
- Qualifying posts of event-candidate venues produce `events.event` rows with
  provenance back to the permalink.
- Non-qualifying posts cost nothing, proven by call count.
- Every date rule above holds, including resolution against the post timestamp.
- No event is stored with a guessed date; unknown is NULL plus `pending_review`.
- Re-running produces no duplicates and does not revert operator corrections.
- A model failure loses no post and requires no re-archiving to retry.
- The serving projection and every public response are byte-identical to before.
- `make test-feature` and `make test-unit` pass.

## Open Questions
None blocking. The extraction model and its real per-post cost are settled by
measurement during the first bounded run, and the plan asserts no cost figure
precisely because the last one in this repo was wrong by 9x.
