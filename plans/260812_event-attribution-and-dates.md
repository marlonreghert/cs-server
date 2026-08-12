# Event Attribution And Dates — link the event that was extracted, not the post

## Branch
fix/event-attribution-and-dates

## Goal
An event links to the venue **its own text names**, not to whichever venue the
surrounding post happened to mention first. A date expression the model read
correctly must not be lost because a regex did not recognise its shape. A post
that only *talks about* an event is not filed as one.

## Non-goals
- **Merging duplicate events.** Deferred by the operator, agreed. §A will reduce
  the duplicate surface as a side effect (correctly-attributed items land under
  different venues), but merging title variants is its own plan.
- **Crawl-level error handling, media type, cap truncation.** Separate plan,
  `260812_crawl-error-visibility.md`.
- **Replacing the extraction model or the one-call-per-post shape.**
- **Changing what `post_type`/`category` mean.**

## Evidence

### 98.6% of `handle_mention` links are wrong
From the 2026-08-12 production snapshot (636 items, 660 sources): of **494**
items linked by `handle_mention`, **487** have a `location_text` sharing no word
with the venue they were linked to. Five agree; two have no `location_text`.

`event_venue_resolution.resolve_event_venue` rung 1 iterates
`extract_mentions(caption)` — the **whole post's** caption — and returns the
first mention that maps to a known venue at `score=1.0`, short-circuiting the
entire ladder. A promoter roundup lists ~20 events at ~20 venues under one
caption, so all twenty inherit the first mention:

```
linked to: Sempre Rock Bar   event: Guitarra Medieval   location_text: @tavernapubnatal
linked to: Sempre Rock Bar   event: Forró Pé de Serra   location_text: @casadomatutonatal
linked to: Sempre Rock Bar   event: Karaokê             location_text: @wesleysbar
```

**The correct answer was already extracted.** The model puts the right handle in
each event's own `location_text` (filled on 97.5% of all items). The pipeline
then overrules it with a post-level signal. This is not a model-quality problem
and not a matching-difficulty problem — it is a precedence problem.

### The same precedence bug hits single-venue accounts
`@beerdock_recife` is one account for two bars. Six events say `CASA FORTE` in
their own `location_text` and all six are linked to *BeerDock Boa Viagem*:

```
FECHADO / QUARTA DO ROCK / QUINTA DIFERENTE
DISCO NIGHT / SHANDY GAMA / SUNSET COM RODRIGO   →  BeerDock Boa Viagem
```

The other ten items from that account correctly say `BOA VIAGEM`, so again the
extractor is right and the account→venue mapping overrides it. Here the
per-event evidence is a **neighbourhood**, not a handle or a venue name, so §A's
handle rung alone will not catch it — see §B.

### The date resolver leaks, and one leak is worse than failing
Four distinct unresolved shapes in the snapshot's 19 undated items, plus two
mis-resolved shapes among the dated ones:

| `date_text` | outcome | cause |
|---|---|---|
| `"É HOJE"` | `missing_date` | `resolver:424` exact-matches `("hoje", "hoje à noite", "hoje a noite")` |
| `"05/SET"` | `missing_date` | no three-letter-month finder |
| `"Quinta (02)"` | `missing_date` × 3 | no weekday+day finder |
| `"De 06 a 09 de fevereiro"` | **2027-02-09** | the `de X a Y` range form is unrecognised, so the ordinary finder matches the trailing date and silently keeps the **last** day |
| `"08/08"` (post later than 08/08) | **2027-08-09** | `_roll_forward` has zero tolerance |

The `de X a Y` case is the dangerous one: it does not fail, it silently returns
the wrong day, and the `date_range` flag that would have told an operator "a
range was collapsed" is never set — confirmed by its absence on both affected
rows. `_date_range_candidate` handles `"01, 02 e 03 de julho"` but not the
preposition form.

`_roll_forward` (`resolver:284`) is `if candidate >= anchor: keep; else: +1
year`. A date **one day** older than its own post becomes a date 364 days in the
future. Live example: `SÁBADO DO CONCHITTAS`, `date_text "08/08"`, resolved to
`2027-08-09`, currently sitting in the review queue.

### Why not hand §A to an agent
The operator asked us to consider replacing brittle deterministic logic with an
agent. For §A the answer is no, and the data is the reason: the model **already
produced the right answer** and we discarded it. Adding a second model call to
re-derive a fact we are holding in a column would cost money per event to
recover information we threw away for free. The fix is a precedence change.

Where intelligence *is* needed — matching `"Cinema da Fundação"` or
`"CASA FORTE"` to a venue — the ladder's existing name/proximity rungs already
do fuzzy work deterministically and are unit-testable. Keep them.

### Why §C is agent-shaped, and the hard constraint on it
The date module is a different story: six distinct gaps in one regex module,
each fix another regex, in a language with open-ended date phrasing. That is the
losing game the operator is pointing at.

But `event_identity.compute_source_event_key` hashes the **resolved calendar
date**. A resolver that can return a different answer for the same input turns
every re-extraction into a duplicate generator — precisely the failure
`0025_multi_event_posts` was written to prevent. And
`openai_event_extraction_client`'s docstring records a deliberate decision that
temporal arithmetic stays "provable, not asserted by a vendor".

Both constraints survive if the model does **interpretation** and Python keeps
**arithmetic** — see §C. Asking the model for a computed date would violate both
and must not be done.

## Current Behavior
A post-level caption mention outranks every per-event signal, so roundups and
multi-location accounts mis-link. Date expressions outside a fixed regex set are
dropped, one range form silently resolves to the wrong day, and a date slightly
older than its post is pushed a year into the future.

## Desired Behavior
1. Link an event using its own stated location before any post-level signal.
2. Distinguish two venues behind one account by what each event says.
3. Interpret the date shapes the model already reads correctly.
4. Keep the arithmetic deterministic and provable.
5. Never silently collapse a range to its last day.
6. Stop turning a just-past date into a next-year date.

## Implementation Approach

One commit per section on a single branch and PR, each commit landing that
section's code together with its own scenarios, per the operator's standing
preference for phased multi-defect fixes. The scenarios all live in this plan's
single slug-named feature file (below), grouped by section.

### A. Per-event evidence outranks post-level evidence
Reorder `resolve_event_venue` so the event's own `location_text` is consulted
first:

1. `@`-mention found **in this event's `location_text`**.
2. Instagram's location tag.
3. Name match against `location_text`.
4. `@`-mention in the **post caption** — kept, but demoted, and only when it is
   unambiguous (see below).
5. Existing lower rungs unchanged.

**A caption mention must stop being authoritative when the caption mentions
several known venues.** One mention in a caption that names one venue is good
evidence; the first of twenty is nearly worthless. When the caption resolves to
more than one known venue and the event's own text gave nothing, resolve to
**no venue** and queue for review — `unresolved_venue` is the correct answer
there, and the queue already exists for exactly this.

**A second, different outcome needs its own review reason.** Once per-event
evidence is honoured, the common case stops being "we cannot tell where this is"
and becomes "we can tell exactly where this is, and it is not a venue we carry"
— the roundup's 492 usable `location_text` values name **159 distinct handles**,
only about a dozen of which are in the catalog. Record that as
`venue_not_in_catalog`, distinct from `unresolved_venue`. The distinction is
load-bearing twice over: an operator can action the first and cannot action the
second, and the second doubles as a ranked venue-acquisition backlog.

This constant is shared with `260812_backfill-misattributed-links.md`, which
needs it to avoid drowning the review queue when it repairs existing rows. It
belongs in `event_reconciliation` beside the other review reasons. **Whichever
plan ships first defines it**; the other imports it. If this plan ships without
it, freshly-crawled rows and backfilled rows will describe the same situation in
two different words.

Keep `promoter_handle` self-link suppression as it is.

This changes the meaning of the `handle_mention` link method. Either split it
(`event_handle_mention` vs `caption_handle_mention`) or record which text the
mention came from — an operator auditing a link must be able to tell a
per-event mention from a post-level one, and today's 487 bad rows are all
labelled identically to the 5 good ones.

**Backfill is out of scope for this plan but must be planned before it is run:**
487 existing rows carry a wrong `venue_id`, some may have been operator-edited,
and `operator_edited_fields` must win. Re-extraction from S3 (never Apify) is
the cheap path. Write it up separately; do not bolt it onto this PR.

### B. Two venues behind one account
When a crawl target's handle maps to a venue but the event's `location_text`
names a different branch, prefer the branch.

Match `location_text` against the candidate venues' **address/neighbourhood**,
not only their names — `"CASA FORTE"` matches no venue *name* but is decisive
against the address. Restrict this to venues that share the target's handle or
brand so a neighbourhood string cannot drag an event to an unrelated venue
across town.

If the branch cannot be determined and the candidates genuinely differ, queue
for review rather than picking the target's default venue. Sending a user to the
wrong bar is worse than showing an operator a decision.

### C. The model interprets; Python computes
Extend the extraction schema so the model returns, **alongside** the verbatim
`date_text` it already returns, a structured interpretation of that text — a
small tagged shape covering the forms actually seen: relative
(`hoje`/`amanhã`), day+month, day+month+year, weekday, weekday+day, and an
explicit range with its own first and last members.

Then:

- **`date_text` stays exactly as it is** — verbatim, unchanged. It remains the
  auditable record of what the flyer said, and the input the deterministic
  finders still try **first**.
- The structured interpretation is a **fallback, used only when the
  deterministic finders return no match**, so today's 97% keep their existing,
  proven path and their existing keys. This is what makes the change safe to
  ship without re-keying the corpus.
- **Python does every calculation** — anchoring to the post timestamp, rolling
  years, picking a range's first day, weekday corroboration. The model never
  returns a computed date, and the plan must include a test asserting a
  model-supplied absolute date is ignored if one ever appears.

**Determinism guard.** Persist the structured interpretation on
`post_item_source` next to `raw_extraction`. On re-extraction, when the incoming
verbatim `date_text` is byte-identical to the stored one, reuse the stored
interpretation instead of trusting a fresh model answer. Same text in, same date
out, same `source_event_key` — the identity guarantee holds even though a model
took part.

If the structured interpretation is absent or unusable, behave exactly as today:
no date, `missing_date`, queued. Never guess.

### D. The arithmetic fixes, which are Python's alone
- **Range → first day, always.** Recognise the `de X a Y` preposition form in
  `_date_range_candidate` so it is consumed as a range and `date_range` is set.
  A range must never be resolved by a finder that sees only its tail.
- **Grace window on the year roll.** `_roll_forward` must not add a year for a
  candidate that is only slightly older than its anchor. Default the window to
  **60 days**, in admin config (matching `menu_expiry_days` and the category
  vocabulary — this project makes first-guess values runtime-configurable).
  Inside the window, keep the anchor's year; outside it, roll as today. Flag
  `year_inferred` whenever the year was not stated, rolled or not, so the
  operator still sees it.
- Keep `vote_on_sibling_years` and its tie rule (prefer the non-inferred year,
  same month only) exactly as they are.

Boundary tests are mandatory: one day inside the window, exactly on it, one day
outside, and a December/January case where the window and the year boundary
interact.

### E. A greeting is not an event
The extraction prompt types a post that merely *talks about* an event as an
`event`. The live instance:

```
title       '31 Anos'
description 'Parabéns pelos seus 31 anos! Feliz aniversário!'
post_type   event      category  party
lineup      []         starts_at 2026-08-08 03:00   time_known false
```

That is a birthday greeting congratulating a promoter on their 31st year. It has
no lineup, no stated time, and announces nothing a user could attend. It sits in
the catalogue as a party.

Both prompts already carry an explicit precedence list over
`event | promotion | menu | food | other`. Extend it: **a post that
congratulates, thanks, recaps, or reports on something is `other`, even when it
names a real event, a real venue and a real date.** The test is whether the post
*announces something attendable*, not whether an event is mentioned. A recap of
last Friday names a date, a venue and a lineup, and is still not an event.

This is the same class of defect as `"Especial do dia"` being typed `event`
rather than `promotion` — the model reaches for `event` as the default when a
post is *about* nightlife. State the negative cases, do not just re-weight the
positive ones.

`resolve_post_type` and `NON_EVENT_KINDS` already carry `other` and need no
change; this is prompt work plus the parser test that proves an `other` verdict
survives into the column.

**Coordination:** `260812_event-dedup-fuzzy-title.md` §B2 refuses to merge across
differing `post_type` and relies on this section to re-type `'31 Anos'`. Its
guard is deliberately belt-and-braces — it also refuses that pair on disjoint
token sets — so the two plans can land in either order without either being
wrong in the meantime.

## Data, Config, And API Impact
- **Migration** — a column on `events.post_item_source` for the structured date
  interpretation (§C's determinism guard). Nullable, additive, no back-fill.

  **Revision-number coordination.** `260812_crawl-error-visibility.md` also adds
  columns to `events.post_item_source` and claims `0036`. Both branch from
  `0035_time_known`, so whichever lands second must **re-parent onto the first
  and take the next free revision**; two migrations declaring
  `down_revision = "0035_time_known"` gives Alembic multiple heads and breaks
  CI's scratch-Postgres migrate step. Run `alembic heads` before writing the
  revision id.
- **Admin config** (Redis-backed and runtime-editable, *not* `app/config.py` —
  the same mechanism as `menu_expiry_days` and the category vocabulary, so the
  first guess can be corrected without a deploy) — `date_year_roll_grace_days`,
  default 60.
- **Review reason** — `venue_not_in_catalog` (§A), shared with
  `260812_backfill-misattributed-links.md`. Additive; the admin console is a
  released client, so review-reason values are append-only.
- **Link method values** — a new or refined value from §A. The admin API and
  console read `linked_by`; released clients must keep working, so add, never
  remove or repurpose an existing value.
- **Extraction schema** — one additional optional output field. Both prompts
  (single-venue and promoter) must change together; they have drifted before.
- **Rollback:** revert. New columns nullable; §A is pure precedence and leaves
  no residue. Rows already written under the old precedence keep their values
  until the separate backfill runs.

## Error Handling And Observability
- `CRAWL_VENUE_ATTRIBUTION_TOTAL` must distinguish per-event from caption
  attribution and count the new ambiguous-caption refusal. Note the existing
  gauge sits **after** an early return in `instagram_crawl_service:558`, and
  `_chain_shared_handle` is a second pipeline with its own instrumentation —
  verify both paths increment before trusting either counter as evidence.
- Count date resolutions by how they were reached: deterministic finder,
  structured fallback, stored-interpretation reuse, unresolved. **The fallback
  rate is the signal that matters** — if it climbs, the deterministic finders
  are decaying and nobody would otherwise notice.
- Log the ambiguous-caption refusal with the mention count. A promoter whose
  every post refuses is a target that needs a different strategy, not a bug.

## Test Plan

Feature files under `tests/bdd/enrichment/`:

Feature file: `tests/bdd/enrichment/event-attribution-and-dates.feature`

Scenarios:
- Link an event to the venue named in its own text, not the caption's first
  mention.
- Link twenty events from one roundup post to twenty different venues.
- Refuse to link when the caption names several venues and the event names none.
- Keep linking correctly when the caption names exactly one venue and the event
  names none.
- Never self-link to the promoter's own handle.
- Prefer the event's own text over an Instagram location tag.
- Record `venue_not_in_catalog` when the event names a handle we do not carry.
- Send a Casa Forte event to the Casa Forte venue, not the account's default.
- Keep a Boa Viagem event at the account's own venue.
- Queue for review when the branch cannot be determined.
- Never let a neighbourhood drag an event to an unrelated venue.
- Resolve "É HOJE" to the post's own date.
- Resolve a range stated as "de 06 a 09 de fevereiro" to its first day.
- Resolve a comma-listed range to its first day.
- Mark a collapsed range with the range flag.
- Resolve a three-letter month.
- Resolve a weekday stated with its day number.
- Keep the current year for a date a few days older than its post.
- Roll the year for a date months older than its post.
- Reuse a stored interpretation when the same post is extracted again.
- Ignore an absolute date supplied by the model.
- Leave an unreadable date unresolved and queued.
- Type a birthday greeting as "other", not as an event.
- Type a recap of a past night as "other" even though it names a venue, a date
  and a lineup.
- Keep typing a genuine announcement as an event.

Pytest unit tests:
- Ladder precedence: per-event mention beats caption mention; caption mention
  used when the event has nothing; ambiguous caption refuses; single-mention
  caption still resolves.
- Handle-case parity — the single-venue path uses `_handle_for(venue_id)` and
  the shared-handle path uses the normalized `target["handle"]`. Every fixture
  seeding lowercase handles makes these coincide and hides a real bug; include
  a **mixed-case** fixture.
- Neighbourhood matching, including a neighbourhood string that must **not**
  drag an event to an unrelated venue.
- Each date shape from the Evidence table, asserted against the exact strings
  observed in production.
- `_roll_forward` boundaries: inside, on, outside the window; December/January.
- Range resolution returns the first day for both the comma-list and the
  preposition form.
- Determinism: identical `date_text` yields an identical `source_event_key`
  across two extractions where the model returns a *different* interpretation
  the second time.
- A model-supplied absolute date is ignored.

**Assertions must name the venue, not count the rows.** A count-based
assertion ("4 events, not 8") already stayed green here against a deliberately
reintroduced wrong-handle bug, because both passes computed the same wrong
number.

Manual or integration checks:
- Re-extract one archived roundup post **from S3, never Apify**, and confirm its
  events land on distinct, correct venues.

## Acceptance Criteria
- An event's own stated location outranks the post caption.
- A roundup post's events resolve to their own venues, or to no venue with the
  caption refusal recorded.
- A multi-branch account routes each event to the right branch or queues it.
- Every date shape in the Evidence table resolves correctly, and the two
  currently-wrong ones no longer produce 2027 dates.
- A range resolves to its first day and is flagged as a range.
- The same `date_text` always yields the same `source_event_key`.
- A greeting or a recap is typed `other`, and `'31 Anos'` specifically is no
  longer typed `event`.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None.
