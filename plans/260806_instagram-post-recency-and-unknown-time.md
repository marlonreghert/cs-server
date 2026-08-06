# Instagram Post Recency And Unknown Event Time

## Branch
fix/instagram-post-recency-and-unknown-time

## Goal
Two defects found by the first live run against real Instagram data:

1. The per-venue image cap keeps images from whichever posts the scraper
   happened to list first, not from the most recent posts.
2. An event whose start time could not be read is stored as **midnight** with
   `needs_review=False`, so an unknown time is indistinguishable from a known
   one.

## Non-goals
- **Multiple events per post.** A listings account puts several events in one
  caption; the extractor takes the first and drops the rest. That is a
  contract-level change (one row per post becomes many, against a uniqueness
  constraint built on exactly that assumption) and gets its own plan —
  `260806_multi-event-posts.md`.
- **Changing the midnight value itself.** A date-only event legitimately stores
  midnight. What is wrong is that it is stored *silently*.
- **Asking the scraper for a date window.** `resultsLimit` already returns a
  recent set; the defect is ordering, and sorting what we already have is free.

## Evidence

Both were observed live on 2026-08-06 against real accounts.

**Ordering is not guaranteed.** Two accounts, same client, same call:

| Account | Returned timestamps, in array order | Newest-first? |
|---|---|---|
| `metropolerecife` | 08-02, 08-02, 07-28 | yes |
| `recifequecabenobolso` | 08-02, 08-05, 08-04, 08-06, 08-01 | **no** |

So the actor returns approximately the most recent *set* but makes no promise
about order, and one account happening to be sorted is luck, not contract.

`_fetch_instagram` in `app/services/archive_sources.py` iterates `posts` in
array order and `break`s when `len(photos) >= cap`. There is no `sort` anywhere
in that module. With `max_photos_per_venue=4` over the promoter's five posts,
the cap is consumed by whichever posts lead the array — on the observed payload
that means the 08-02 post is archived and the 08-06 post is not.

This is silent: the run reports success, the right *number* of images, and the
skip gate then treats those venues as archived. The images are simply the wrong
ones.

**An unread time is presented as a known midnight.** `resolve_event_datetime`
in `app/services/event_date_resolver.py` defaults an absent `time_text` to
`(0, 0)` and returns `needs_review=False`. On the live run, post
`DbjkVcaGkmI` resolved to `2026-08-07 00:00:00-03:00` for a nightclub event.

Two different situations collapse into that one value, and the repo already
has the signal to tell them apart **for free**: the `flyer` photo attributes
added by `260804_instagram-media-archive.md` include `names_time`, and on that
exact photo the classifier returned `names_time: {"value": "yes",
"confidence": 0.99}` — the flyer *did* state a time and the extractor missed
it. That is an extraction failure wearing the same shape as a genuinely
time-less event.

This is the same principle the plans already hold for dates: "a guessed date is
worse than a missing one — an operator will scan a queue of blanks, but will not
audit a field that looks answered."

## Current Behavior
The image cap prefers whatever the scraper listed first. An event with no
readable time silently starts at midnight and is not queued for review.

## Desired Behavior
1. Sort scraped posts by timestamp, newest first, **before** the cap is
   applied, so the cap always keeps the most recent posts.
2. Order posts deterministically when a timestamp is missing or unparseable —
   such a post sorts last rather than crashing or floating arbitrarily.
3. Keep midnight as the stored value for a date-only event, but mark the time
   as unknown so it is visible.
4. Queue for review when the flyer's `names_time` says a time was present and
   the extractor produced none — a detectable extraction miss, not an absence.
5. Leave a genuinely time-less event's date, status and metrics otherwise
   unchanged.

## Implementation Approach

### A. Sort before the cap
In `_fetch_instagram`, sort the actor's posts by parsed timestamp descending
before the expansion loop. Posts with a missing or unparseable timestamp sort
last, so a payload oddity can never displace a real recent post.

Sorting rather than requesting a window keeps the fix free: the posts are
already fetched and already billed, and the only thing that was wrong was which
of them the cap kept.

**The cap must still bound images, not posts** — the existing carousel
behaviour is correct and must not regress. This changes the *order* posts are
consumed in, nothing else.

### B. Distinguish an unknown time from a known one
`ResolvedDate` gains a `time_known: bool`. Midnight is still stored when no time
was parsed, but the caller can now tell the difference, and the extraction
service adds a review reason when the time is unknown **and** the flyer
attribute says one was stated.

Two reasons, kept apart because they mean different things:
- `unread_time` — the flyer named a time and we failed to read it. An
  extraction defect; worth an operator's eye and worth counting.
- time simply absent — the event is date-only. Stored, marked, **not** queued;
  queueing every date-only event would flood the queue with non-problems.

The `names_time` attribute is read from the classified flyer the extractor
already loads, so this costs no extra call. Where no flyer attribute exists
(caption-only qualification), the time is unknown but unverifiable, and the
event is not queued — an absent signal must not be read as a positive one.

## Data, Config, And API Impact
- **Migration:** none. `review_reason` is existing free text and
  `events.event` gains no column.
- **API:** none. `review_reason` may carry a new value, which the console
  already renders as text.
- **Behaviour:** runs after this archive the newest posts rather than an
  arbitrary recent subset. Already-archived venues are unaffected; re-running a
  venue under `skip_scope` still skips it. No back-fill is attempted — the plan
  does not re-buy anything.

## Error Handling And Observability
A post whose timestamp cannot be parsed is sorted last and logged at debug, not
dropped — it is still archivable, just never at the expense of a dated post.

Metrics:
- `event_extraction_posts_total{outcome="unread_time"}` so extraction misses
  are countable rather than anecdotal. The whole reason this defect survived to
  a live run is that it produced a plausible value and counted as success.

## Test Plan
Feature file: `tests/bdd/enrichment/instagram-post-recency-and-unknown-time.feature`

Scenarios:
- Archive the most recent posts when the scraper returns them out of order —
  built on the real observed sequence (08-02, 08-05, 08-04, 08-06, 08-01) with a
  cap smaller than the total, asserting the 08-06 post's images are archived and
  the 08-01 post's are not.
- Keep the image cap bounding images, not posts, after sorting.
- Sort a post with no timestamp last, and still archive it when capacity remains.
- Queue an event for review when the flyer says a time was named and none was
  extracted.
- Store midnight without queueing when the flyer names no time.
- Do not queue when there is no flyer attribute to contradict the missing time.
- Leave a fully-dated, fully-timed event unqueued and unchanged.

Pytest unit tests:
- The post sort: descending, missing timestamp last, unparseable timestamp last,
  stable for equal timestamps, empty list.
- `resolve_event_datetime` sets `time_known=False` for absent time and `True`
  when a time parses — including `00h`, which IS a stated midnight and must
  report `time_known=True`. That is the trap: a real midnight and a defaulted
  midnight are the same instant and must not be the same fact.
- The review-reason decision across the four combinations of
  (time parsed / not) x (names_time yes / not present).

Manual or integration checks:
- Re-run the live scrape of `recifequecabenobolso` with a small cap and confirm
  the archived images come from the newest post. This is the exact payload that
  exposed the defect.

## Acceptance Criteria
- Posts are consumed newest-first regardless of the order the scraper returned.
- The cap still bounds images including carousel children.
- A post with no usable timestamp never displaces a dated one.
- An event whose time was named but unread is queued with `unread_time`.
- A date-only event stores midnight, is marked time-unknown, and is not queued.
- A stated `00h` is reported as a known time.
- `make test-feature`, `make test-unit` and `make test-bdd` pass.

## Open Questions
None.
