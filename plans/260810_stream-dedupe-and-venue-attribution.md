# Stream Dedupe And Venue Attribution — pay once, attribute deliberately

## Branch
fix/stream-dedupe-and-venue-attribution

## Goal
Stop processing the same Instagram post twice when it is returned by both the
posts and reels streams, and stop fanning a shared handle's posts out to every
venue that shares it — choose the venue the post is actually about.

## Non-goals
- **Telling a promotion apart from an event**, and **using the model's
  recurrence**. Both are real and both came out of the same run; they are
  `260810_event-kind-and-recurrence.md`, executed after this.
- **Recovering the Apify double-charge.** Not possible — see §A.
- **Changing the cursors.** They stay per-stream and independent.
- **New venue-matching infrastructure.** §B reuses the promoter path's existing
  resolver rather than building a second one.

## Evidence

### The two streams overlap, measured
From the first real crawl of `@entreamigos.praia` (run
`01KZNTX31S8VD90DPDSTRBN0YA`, manifest read from S3):

```
posts stream            16 results
reels stream            16 results   -> 32 billed, $0.096
distinct shortcodes     19
manifest entries        47
distinct S3 keys        35   -> 12 entries are duplicates
```

**All 12 duplicated shortcodes are `post_type: Video`, each resolving to one
identical S3 key.** The Sidecar (carousel) items appear exactly once.

The cause is Instagram, not this repo: **a reel is also a grid post**, so the
profile-grid endpoint and the Reels-tab endpoint both return it. The design
treated the two streams as disjoint sets. They are not — reels are largely a
*subset* of posts, and only reels hidden from the grid are unique to that
stream. Here the reels stream cost 16 results and contributed roughly 3 items
the grid did not already carry.

**Consequence today:** the same image is archived, classified and extracted
twice. It does **not** corrupt data — `uq_event_source_post` keys on
`(source_handle, source_shortcode, source_event_key)`, so re-extracting the
same post is idempotent and yields one event. It is duplicated *cost* (a second
OpenAI classification and extraction call per shared post) and a manifest that
lists the same key twice.

### A shared handle fans out to every venue
`instagram_crawl_service.chain_venue` takes `venue_ids: list[str]` and loops:

```
for venue_id in venue_ids:
    manifest_entries = await self._archive_venue_posts(prefix, venue_id, handle, new_posts)
...
await self.event_extraction_service.run(
    {"eligibility": {"mode": "venue_ids", "venue_ids": ",".join(venue_ids)}})
```

Every post is archived under **every** venue sharing the handle, and extraction
runs for all of them. Production has this case ready to fire:
`@entreamigosobode` maps to **`Entre Amigos O Bode`** and **`Entre Amigos O
Bode Espinheiro`** — two `venue_id`s, one handle, and the operator is about to
schedule it.

The uniqueness constraint is venue-agnostic, so the second venue's extraction
of the same post does not create a second event — it **updates the first**,
overwriting `venue_id`. The event therefore lands on whichever venue happened
to be processed last, which is an ordering accident, not a decision.

**The executing agent must confirm this end-to-end before fixing it** — the
reasoning above is read from the code, not observed, and the failure mode
differs meaningfully depending on whether the result is one mis-attributed
event or two competing rows.

### There is already a venue resolver, and it is not being used here
The promoter path solves exactly this problem: a promoter post names a venue in
its text and `260804_instagram-promoter-events.md` resolves it to a `venue_id`
with a confidence floor and margin, auto-linking above the gates and queueing
below them (`location_resolution`, `location_confidence`, `linked_by`).

A shared venue handle is the same question with a smaller candidate set — two
venues instead of the catalog. Building a second matcher would be the drift
this repo has been bitten by repeatedly (two `escHtml`s, two freeze rules, two
cap resolutions).

## Current Behavior
A post returned by both streams is archived, classified and extracted twice. A
post from a handle shared by two venues is archived under both and attributed
to whichever venue the extraction loop reached last.

## Desired Behavior
1. Process each post once per run, however many streams returned it.
2. Keep both streams' cursors advancing independently on their own newest item.
3. Report how much the reels stream actually added, so its cost is visible.
4. Default `crawl_reels` to off.
5. For a handle covering several venues, choose the venue from the post's own
   content rather than processing every venue.
6. When the content does not clearly indicate one venue, queue the event for a
   human rather than guessing — the same posture the promoter path takes.
7. Never silently attribute a post to a venue on the strength of ordering.

## Implementation Approach

### A. Dedupe at the chain boundary
Both streams run and bill as they do today; before chaining, merge their posts
into one set keyed by `shortcode`, preferring the richer payload when the two
copies differ. The chain then archives, classifies and extracts each post once.

**The Apify charge cannot be avoided and this plan does not pretend to.** The
actor bills per result *returned*, and its input schema has eight fields, none
of which filter by media type — verified against the live build. There is no
way to ask for "grid posts, excluding reels". What is recoverable is everything
downstream: the second S3 write, the second classification, the second
extraction.

**Cursors stay per-stream.** Each stream's cursor still advances to the newest
item *that stream* returned. Deduping is about what gets processed, not about
what each stream has seen — collapsing them would reintroduce exactly the
permanent-loss failure separate cursors were built to prevent.

### B. Make the reels stream's value visible, and default it off
Record, per run, how many reels results were already present in the posts
stream and how many were new, and expose it on the read model so the console can
show "reels: 16 fetched, 3 new".

Flip `crawl_reels` to default **off**. For most accounts the posts stream
already carries the reels, so the default should not silently double the bill;
an operator turns it on for a venue shown to hide reels from its grid. Existing
targets keep whatever they have — this changes the column default, never a row.

### C. Attribute a shared handle's post to one venue
When a handle maps to exactly one venue, nothing changes — that is the common
case and it must stay byte-for-byte identical.

When it maps to several, resolve the venue **per post** from its own text,
reusing the promoter path's resolver against the handle's own venues as the
candidate set. Match on what a caption actually carries: an address or
neighbourhood, a branch name ("Espinheiro", "Boa Viagem"), a location tag, an
`@`-mention.

Apply the same floors the promoter path already uses. **Above the gates**,
attribute and archive under that venue only. **Below them**, attribute to no
venue and queue the event with `location_resolution` unset, exactly as an
unresolved promoter event is queued — the review queue already surfaces those
and the console already renders them.

**Do not fall back to "first venue" or "all venues".** A wrong venue is worse
than an unresolved one: an unresolved event is visibly waiting for a human,
while a confidently wrong one is invisible and propagates into the app. This is
the same principle the date resolver already enforces — a guessed date is worse
than a missing one.

**Archive once, under the resolved venue.** If no venue resolves, archive under
the handle rather than duplicating across candidates, so the bytes exist for a
human to look at without inventing an attribution the pipeline could not make.

## Data, Config, And API Impact
- **Migration `0033_crawl_reels_default_off`**: flip the column default only.
  No back-fill, no row updated — an operator who deliberately enabled reels
  keeps it.
- **Read model:** per-run reels overlap counts on `CrawlTargetOut`. Additive.
- **Serving:** none.
- **Rollback:** revert. The dedupe changes what is processed, not what is
  stored, and the attribution change leaves earlier rows untouched.

## Error Handling And Observability
Metrics: `crawl_stream_overlap_total{result_type}` for items a later stream had
already supplied, and `crawl_venue_attribution_total{outcome}` with
`resolved` / `ambiguous` / `single_venue`.

**Watch `ambiguous`.** A shared handle whose posts never resolve means the
signals in §C are not present in that account's captions, and the answer is to
change the matching, not to loosen the floor.

## Test Plan
Feature file: `tests/bdd/enrichment/stream-dedupe-and-venue-attribution.feature`

Scenarios:
- Process a post returned by both streams exactly once.
- Keep both cursors advancing when the streams overlap completely.
- Archive one image per post when both streams returned it.
- Count what the reels stream added beyond the posts stream.
- Crawl only posts for a target created with the new default.
- Keep reels enabled for a target that already had it on.
- Attribute a post to the single venue when a handle maps to one.
- Attribute a post to the branch its caption names when a handle maps to two.
- Queue an event with no venue when a shared handle's post names none.
- Never attribute a shared handle's post to more than one venue.
- Archive a shared handle's post once, not once per venue.

Pytest unit tests:
- Dedupe keyed on shortcode, preferring the richer payload, order-independent.
- Cursor advancement is unaffected by dedupe, per stream.
- The single-venue path is unchanged — asserted against the pre-change
  behaviour, since that is the common case and the likeliest casualty.
- Venue resolution: a caption naming a branch, a caption naming neither, a
  caption naming both, and one just below the confidence floor.
- The migration changes the default and no existing row.

Manual or integration checks:
- Schedule `@entreamigosobode` (two venues) and confirm each post is archived
  once and attributed to one venue, with unresolved posts queued rather than
  guessed.

## Acceptance Criteria
- A post returned by both streams is archived, classified and extracted once.
- Cursors still advance independently per stream.
- The reels stream's marginal contribution is recorded and exposed.
- `crawl_reels` defaults off; existing targets are untouched.
- A shared handle's post is attributed to one venue, chosen from its content.
- An unresolvable post is queued with no venue, never guessed.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None. The fan-out failure mode in §Evidence must be confirmed by observation
before the fix, and the finding recorded in the PR either way.
