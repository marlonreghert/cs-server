# Instagram Promoter Events — external accounts, and mapping their events to venues

## Branch
feature/instagram-promoter-events

## Goal
Crawl Instagram accounts that promote events but belong to no single venue,
extract their events with the existing extractor, and resolve each event's
location to a known venue — linking automatically when the evidence is
conclusive, and queueing for an operator when it is not.

## Non-goals
- **A second extractor.** `260804_instagram-event-extraction.md` owns turning a
  post into an event. This plan adds a different *source of posts* and the
  *venue resolution* that a venue-owned post does not need.
- **Creating venues.** A promoter event at a place not in the catalog stays
  unresolved. Adding venues is `batch_add_service`'s job and a separate operator
  decision with its own cost.
- **Serving events to the app.** Unchanged from the previous plan: no
  projection, no public API.
- **Automatic promotion of discovered accounts.** Discovery proposes; an
  operator disposes.
- **Scheduling.** Operator-triggered, no cron, like every other job in the
  registry.

## Evidence

**Promoter posts cannot live in the existing table.** `instagram.posts` has
`venue_id` as its primary key *and* a foreign key to `venues.venue`
(`0001_baseline_schemas.py`). An account attached to no venue has nowhere to go.

**Handle → venue is already a table.** `instagram.handle` maps `venue_id` to a
confirmed handle, with the resolving tier in `source` since
`0020_instagram_handle_source`. Reversing it gives an **exact** venue identity
for any post that @-mentions a venue we know — no string matching involved.

**Name matching already exists and is already tested.**
`tests/test_venue_name_matching.py` and `plans/260731_venue-name-matching.md`
landed a matcher for the Instagram cascade. Reusing it keeps one definition of
"these two names are the same place" instead of growing a second, subtly
different one.

**Geo is available.** `venue_eligibility.haversine_km` and the venue rows'
coordinates support proximity as a tie-breaker.

**Ambiguity-with-review is the house pattern for irreversible attribution.**
The `batch-add-venues` skill triages every geo-link outcome and queues the
ambiguous ones for a human inside a 24h undo window rather than guessing. Event
attribution has the same shape: a wrong link puts a party at the wrong bar.

**`events.event.venue_id` was made nullable in advance**
(`260804_instagram-event-extraction.md`, migration `0022`), so an unresolved
promoter event has a legal representation from day one.

## Current Behavior
Only a venue's own handle can produce events, and its venue is known because the
post came from it. Accounts that promote parties across many venues — which is
where a large share of a city's nightlife programming actually lives — are
invisible to the pipeline, and nothing can map a location string to a venue.

## Desired Behavior
1. Maintain a registry of promoter accounts with a lifecycle: `candidate`,
   `active`, `paused`, `rejected`.
2. Let an operator add an account by handle, and propose accounts automatically
   from @-mentions and tags observed in already-crawled event posts.
3. Crawl active accounts within per-account and per-run bounds, and extract
   events with the existing extractor.
4. Resolve each event's location through an ordered ladder, cheapest and most
   certain first, and stop at the first conclusive answer.
5. Link automatically only above a confidence floor **and** only when the best
   candidate is clearly ahead of the runner-up.
6. Queue everything else with its ranked candidates so an operator chooses from
   evidence rather than from a search box.
7. Record how every link was made, and let an operator override or unlink it.
8. Never attribute an event to a venue on a weak guess — unresolved is a
   legitimate terminal state.

## Implementation Approach

### A. The promoter registry
Migration `0024_promoter_accounts`:

- `events.promoter_account` — `handle` PK, `display_name`, `status`,
  `discovery_source` (`manual` | `mention` | `tag`), `discovered_from_event_id`,
  `mention_count`, `notes`, `added_by`, `last_crawled_at`, `posts_crawled`,
  `events_extracted`, `created_at`, `updated_at`.
- `events.event_venue_link_candidate` — `event_id`, `venue_id`, `rank`, `score`,
  `method`, `evidence` (jsonb). The ranked alternatives behind a decision.
- `events.event` gains `location_resolution`
  (`auto` | `manual` | `unresolved`), `location_confidence`, `linked_by`,
  `linked_at`.

Storing the losing candidates, not just the winner, is what makes the review
queue usable: the operator is choosing between three named venues with scores
and reasons, which is a five-second decision, instead of being handed a location
string and a search box.

### B. Discovery proposes, an operator disposes
Every handle @-mentioned or tagged in the caption of an event post already
extracted is counted. A handle that is not a known venue handle, is not already
registered, and clears a mention threshold is inserted as `status=candidate`
with the event that surfaced it.

This costs nothing — the captions were already scraped and the events already
extracted. It also cannot run away: a candidate is crawled only after a human
sets it `active`, so an over-eager discovery pass wastes storage rather than
money.

### C. Crawling
Job `promoter_event_crawl`, config `{handles, max_accounts, max_posts_per_account,
lookback_days, dry_run}`. Empty `handles` means every `active` account, bounded
by `max_accounts`.

**A promoter posts far more than a venue does**, so the per-account bound is not
optional and its default is small. Posts are archived through the same
`instagram_posts` archive source, into
`retrieved/source=instagram_posts/…/promoter=<handle>/` — the source folder
already separates by `source=`, and promoters get their own key segment rather
than a fake `venue_id`, because inventing a venue id for a non-venue would
poison every consumer that joins on it.

The extractor is called unchanged. Its `location_text` field — specified in the
previous plan and unused there — is the input to resolution.

### D. The resolution ladder
Tried in order; the first conclusive answer wins, and each rung is cheaper or
more certain than the one below it:

1. **@-mention of a known venue handle.** Reverse `instagram.handle`. This is an
   *identity*, not a similarity: the promoter has told us which account the
   party is at. Free, exact, and the reason this rung is first.
2. **Instagram location tag.** When the post carries a location, match its name
   and coordinates against the catalog. Strong, because it is Instagram's own
   place database rather than free text.
3. **Name match on `location_text`.** The existing venue name matcher,
   unaccented and casefolded.
4. **Name match plus proximity.** A name match inside the city geo-fence
   outranks the same match outside it; `haversine_km` breaks ties among
   same-named venues.

Two independent gates decide whether the top candidate is linked automatically:
it must clear an absolute confidence floor, **and** it must beat the runner-up
by a margin. The margin gate is the one that matters — a city has three bars
called some variant of the same name, and a top score of 0.91 against a
runner-up of 0.89 is not a confident answer, it is a coin toss wearing a high
score. Failing either gate produces `pending_review` with the ranked list.

Below the floor entirely, nothing is linked: `location_resolution=unresolved`,
`venue_id` NULL. An event with no venue is honest; an event at the wrong venue
is a defect that reaches users the moment anything starts serving this data.

### E. Admin API
- `GET/POST/PATCH/DELETE /admin/events/promoters` — registry CRUD, including
  promoting a `candidate` to `active`.
- `GET /admin/events/review` — the queue: pending events with their ranked
  candidates and evidence.
- `POST /admin/events/{id}/link` — choose a candidate, or name a venue directly;
  sets `location_resolution=manual` and records `linked_by`.
- `POST /admin/events/{id}/unlink` — back to unresolved, for a bad auto-link.

A manual link is never overwritten by a later crawl, for the same reason a
confirmed extraction is not: the operator's answer outranks the model's.

## Data, Config, And API Impact
- **Migration:** `0024_promoter_accounts` — two new tables and four additive
  columns on `events.event`.
- **Settings:** `promoter_link_confidence_floor`, `promoter_link_margin`,
  `promoter_max_posts_per_account`, `promoter_mention_threshold`.
- **API:** the admin routes above. Nothing public.
- **Serving:** none.
- **S3:** promoter posts under the existing `source=instagram_posts` folder with
  a `promoter=` key segment. No new prefix, so no terraform.

## Error Handling And Observability
A promoter account that no longer exists, or is private, is marked and skipped
rather than failing the run — one dead handle must not cost the other accounts
their crawl. A resolution failure is a normal outcome, not an error.

Metrics:
- `promoter_accounts_total{status}` gauge.
- `promoter_crawl_posts_total{outcome}`.
- `event_venue_link_total{method,result}` — `result` is `auto`, `queued`,
  `unresolved`, `manual`. **`method` is what makes this dashboard worth
  reading**: it shows whether links are coming from exact mentions or from fuzzy
  name matching, which is the difference between a resolver that is working and
  one that is guessing successfully so far.
- `event_review_queue_depth` gauge — the operator-load signal, and the number
  that says whether the auto-link thresholds are set sensibly.

## Test Plan
Feature file: `tests/bdd/enrichment/instagram-promoter-events.feature`

Scenarios:
- Register a promoter account manually and crawl it within its post bound.
- Propose a candidate account from repeated @-mentions and assert it is **not**
  crawled until an operator activates it.
- Link an event automatically when its caption mentions a known venue handle,
  and record `method=handle_mention`.
- Link from an Instagram location tag when no handle is mentioned.
- Link from a name match when neither a handle nor a location tag is present.
- Queue for review when two venues score within the margin, and assert **no**
  `venue_id` was written.
- Queue for review when the best score is above the floor but the margin gate
  fails — the near-tie case.
- Leave an event unresolved when every candidate is below the floor, and assert
  it is distinguishable from a queued one.
- Present ranked candidates with scores and evidence in the review queue.
- Link manually from the queue, set `location_resolution=manual`, and record the
  operator.
- Preserve a manual link across a later crawl of the same post.
- Unlink a wrong auto-link and return the event to unresolved.
- Skip a private or missing account, mark it, and finish the run.
- Report the selection and write nothing under `dry_run`.

Pytest unit tests:
- The ladder's ordering: assert a handle mention wins even when a name match
  scores higher, because certainty is not the same as score.
- The margin gate at, just inside, and just outside the threshold.
- The floor gate, including a single candidate below the floor (no runner-up to
  compare against — the branch that is easy to get wrong).
- Reverse handle lookup: known handle, unknown handle, handle differing only by
  case or a leading `@`.
- Proximity tie-break between two identically-named venues.
- Mention extraction from captions: multiple mentions, an email address that
  looks like a mention, a mention of the promoter itself (must not self-link).
- Candidate insertion is idempotent across repeated discovery runs.
- A manual link survives re-resolution.

Manual or integration checks:
- Crawl two or three real Recife promoter accounts and hand-audit every link
  against the permalink. Precision here cannot be measured from fixtures — the
  question is whether the thresholds are right on real data, and that is only
  answerable by reading the queue.

## Acceptance Criteria
- Promoter accounts are registered, lifecycled, and crawled only when `active`.
- Discovery proposes candidates without ever crawling them.
- Every auto-link clears both the confidence floor and the runner-up margin.
- Every queued event carries ranked candidates with scores, methods and
  evidence.
- An event below the floor is stored unresolved with a NULL `venue_id` and is
  distinguishable from a queued one.
- A manual link is never overwritten by a later crawl.
- `event_venue_link_total{method}` shows how each link was made.
- `make test-feature` and `make test-unit` pass.

## Open Questions
None blocking. Two items are settled by measurement rather than by decision now,
and both are settings: whether the Apify Instagram actor returns the post's
location tag (rung 2 degrades to rungs 3–4 if not, and is confirmed against one
live response before the parser is written, as with the `externalUrls` drift in
`260729_instagram-candidate-loss.md`); and the floor and margin values, which
the first hand-audited crawl calibrates.
