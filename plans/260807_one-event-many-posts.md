# One Event, Many Posts — a countdown campaign is one event, not three

## Branch
feature/one-event-many-posts

## Goal
Recognise that several Instagram posts can announce the same real-world event,
collapse them into one event row carrying every source post, and merge their
extracted fields so a later post completes an earlier one instead of duplicating
it.

## Non-goals
- **Changing per-post extraction.** One post can still yield several events;
  that stays exactly as `260806_multi-event-posts.md` built it. This adds the
  opposite direction.
- **Merging events that lack a venue or a date.** Without both there is no
  reliable identity — see §B.
- **Fuzzy title matching.** Identity uses the same normalisation
  `source_event_key` already uses. A materially different title is a different
  event; see the honest limitation in §B.
- **`ticket_info` / `attractions`.** Held at the operator's request.
- **The console's rendering of multiple sources.** cs-server first; vibes_bot
  follows.

## Evidence

The operator's review queue shows three rows titled "Noite da Patroa" /
"NOITE DA PATROA", all `2026-08-08`, all at Club Metrópole. They are three
distinct posts running a countdown campaign, from the archived manifest:

| shortcode | caption |
|---|---|
| `Dbs1FdsEWr7` | "NOITE DA PATROA • 08/AGO Equilibrium na Metrópole" |
| `DbtSQngKcPm` | "Faltam **2 dias** … NOITE DA PATROA … 08/Ago Sábado" |
| `DbvhZJqkUyf` | "EQUILIBRIUM NA METRÓPOLE **É AMANHÃ!** 8/Ago" |

**This is not an identity bug.** `UNIQUE (source_handle, source_shortcode,
source_event_key)` is scoped **per post**, deliberately: `source_event_key`
exists so re-extracting the *same* post is idempotent, and it does that
correctly. Nothing was ever built to notice that three posts describe one night.

`260806_multi-event-posts.md` solved *one post → many events* and never
considered *many posts → one event*. A venue promoting Saturday all week makes
this the common case, not an edge case.

**The titles already normalise together.** `normalize_title` in
`app/services/event_identity.py` casefolds, strips accents and collapses
whitespace, so "NOITE DA PATROA" and "Noite da Patroa" are already the same
string to the key function. What is missing is an identity that does not include
the post.

## Current Behavior
Each post produces its own event row. Three posts about one night produce three
events, each with whatever subset of the detail its own caption happened to
carry.

## Desired Behavior
1. Treat `(venue_id, start date, normalised title)` as one event, whichever post
   announced it.
2. Keep every source post on that event — permalink, shortcode, handle, cover —
   so nothing about provenance is lost.
3. **Complement, do not overwrite**: a field absent on one post and present on
   another is filled from whichever post has it.
4. **When two posts contradict on the same field**, keep the most recent post's
   value and flag the conflict for review — never silently pick.
5. Merge existing duplicates during migration, preserving any operator decision.
6. Never merge events that lack a venue or a date.
7. Keep re-extraction of a single post idempotent, exactly as today.

## Implementation Approach

### A. Sources become a child table
`events.event_source`, one row per post that announced the event:
`event_id`, `source_kind`, `source_handle`, `source_shortcode`,
`source_permalink`, `cover_photo_key`, `raw_extraction`, `first_seen_at`,
`last_seen_at`, with `UNIQUE (source_handle, source_shortcode,
source_event_key)` moving here from `events.event`.

That constraint moving is what preserves today's per-post idempotency: the same
post still cannot produce two rows, it now attaches to an event instead of being
one.

`events.event` keeps the merged fields and loses the single-source columns.

### B. The identity, and what it cannot do
`(venue_id, starts_at::date, normalize_title(title))`, reusing the existing
normaliser so identity and key can never disagree about what counts as the same
title.

**Date, not datetime.** The countdown posts disagree about the time — one names
none at all. Including the clock time would defeat the merge on exactly the
campaign it exists to handle.

**An event with a NULL venue or a NULL date is never merged.** Without a venue
you cannot tell whether two events are at the same place; without a date you
cannot tell whether they are the same night. Both are common for unresolved
promoter events, and merging them on title alone would attribute one venue's
event to another's. Those stay one-row-per-post, as today.

**The honest limitation:** identity is title-based. If the model titles one post
"Noite da Patroa" and another "Equilibrium na Metrópole" — both plausible from
these captions — they will not merge. Fuzzy title matching would fix that and
would also merge genuinely different same-night events, which is the worse
error. Left as a known gap rather than papered over.

### C. Complement, and flag a contradiction
Per scalar field, in order:

- exactly one source has a value → use it;
- several agree → use it;
- **several disagree → take the most recent source's value and set
  `review_reason = sources_disagree`**, naming the field.

Most-recent because a later post in a campaign is more plausibly a correction
than a regression. Flagged because the project's standing rule is that a value
we had to choose between is not a value we can present as settled — the same
posture as `weekday_mismatch` and `model_diverges_from_confirmed_record`.

`lineup` is a list, not a scalar: union it, preserving order and dropping
duplicates. A teaser naming two DJs and a final flyer naming five should yield
five, not a contested choice.

**An operator's decision always outranks the merge.** A `confirmed` event's
fields are never recomputed by a later source; the source attaches, the fields
stand, and a divergence is flagged as it already is today.

### D. The migration is the risky part
`0026_event_sources`, from head `0025_multi_event_posts`:

1. create `events.event_source`;
2. back-fill one source row per existing event from its current columns;
3. group events by `(venue_id, date(starts_at), normalize_title(title))` where
   **both venue and date are non-null**, keep the **oldest** `event_id` as
   canonical, re-point the other groups' sources at it, merge fields by the §C
   rules, and delete the now-sourceless duplicate events;
4. drop the old unique constraint and the single-source columns from
   `events.event`.

**Order matters and step 3 is destructive.** It must run inside the migration's
transaction, must never collapse a group containing more than one `confirmed`
or manually-linked event (leave those alone and flag instead — an operator
confirmed two rows and only they can say they are one), and must be preceded by
the back-fill or sources are lost.

**Downgrade must refuse** when any event has more than one source, exactly as
`0025`'s does when a post holds more than one event: the split cannot be
reconstructed, and silently dropping sources would destroy provenance.

The alternative — restructure now, merge later via re-extraction — was rejected:
after a non-merging migration, three events share one identity and a
re-extracted post has no deterministic rule for which to attach to.

## Data, Config, And API Impact
- **Migration `0026_event_sources`** as above. Destructive in step 3, guarded.
- **API — additive, deliberately not breaking.** `EventOut` gains `sources[]`
  **and keeps** `source_permalink`, `source_handle`, `source_shortcode` and
  `cover_photo_key`, now *derived* in the response layer from the primary
  source (the most recent one) rather than read from a column.

  The columns leave `events.event` — the data is normalised — but the response
  shape does not change. The deployed console reads `source_permalink` and
  `cover_photo_key` today, and a breaking change here would force a lockstep
  deploy and break the flyer viewer in the window between them. Deriving the
  scalars costs one lookup and removes that coupling entirely; vibes_bot can
  adopt `sources[]` whenever it likes, independently.
- **Serving:** none. No app-facing route changes.
- **Metric:** `event_sources_per_event` histogram — a campaign collapsing back
  to one source per event is the regression this feature exists to prevent, and
  only a distribution shows it.

## Error Handling And Observability
A source that cannot be attached is logged and skipped; it never fails the run.
`review_reason` gains `sources_disagree`, and the outcome metric gains a
matching label.

## Test Plan
Feature file: `tests/bdd/enrichment/one-event-many-posts.feature`

Scenarios:
- Collapse the three real Noite da Patroa posts into one event with three
  sources.
- Complete a thin teaser's fields from a later detailed post.
- Union the lineup across posts rather than choosing one.
- Flag `sources_disagree` when two posts give different times, keeping the most
  recent.
- Keep a `confirmed` event's fields when a new source attaches, and flag the
  divergence.
- Keep re-extraction of a single post idempotent — no second source, no second
  event.
- Never merge two events that lack a venue.
- Never merge two events that lack a date.
- Never merge two same-night events with materially different titles.
- Merge events differing only in title case and accents.
- Expose every source's permalink and cover on the merged event.
- Keep serving `source_permalink` and `cover_photo_key` as scalars derived from
  the primary source, so the deployed console keeps working unchanged — the
  compatibility guarantee, asserted directly.

Pytest unit tests:
- The identity function across case, accents, whitespace, null venue, null date.
- The field merge: absent/present, agreeing, disagreeing, and list union.
- The migration's grouping and its refusal to collapse a group with two
  confirmed events.
- Downgrade refuses when any event has more than one source.
- The back-fill precedes the collapse — asserted on statement order, as
  `0025`'s test does.

Manual or integration checks:
- Against the live catalog: the three Noite da Patroa rows become one event with
  three sources, and SECRET CLUB and METRÓPOLE DELUXE are untouched.

## Acceptance Criteria
- The three countdown posts yield one event with three sources.
- Fields complement; a contradiction keeps the newest and is flagged.
- Lineups union.
- Venue-less and date-less events never merge.
- Single-post re-extraction stays idempotent.
- Operator decisions survive.
- Migration back-fills before collapsing and refuses an unsafe downgrade.
- `make test-feature`, `make test-unit` and `make test-bdd` pass.

## Open Questions
None. The console's rendering of multiple sources is an optional follow-up in
vibes_bot, not a prerequisite: `EventOut` stays backward compatible by design,
so this can deploy on its own.
