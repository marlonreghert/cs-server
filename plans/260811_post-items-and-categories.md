# Post Items And Categories — name the thing correctly, and keep its type

## Branch
refactor/post-items-and-categories

## Goal
Stop calling every post-derived row an "event". A post yields **items**, each
with a **type** — an event, a promotion, a menu item — and each with a
free-text **category** steered toward a known vocabulary. Persist both, so
different types can be processed differently.

## Non-goals
- **Separate tables per type.** One typed table now; type-specific tables can
  follow if promotions or menus grow fields events do not have. See §A for why
  this is the reversible direction.
- **Serving any of this to the app.** Admin-only, as with every field here.
- **Renaming the `events` schema.** It names the *pipeline*, which is still
  event extraction; only the entity was misnamed.
- **Back-filling categories.** Existing rows get a type, not a category — see
  §Data.

## Evidence

### The table's name is a claim it cannot support
`events.event` holds whatever a post produced. Production, right now:

```
title                        what it actually is
'Especial do dia'            a risotto — weekday lunch special
'Oficina Vida de Inseto'     a children's craft workshop
'Bingo dos Animais'          a bingo
'NOITE DA PATROA'            a club night
```

Only the last is an event by any working definition. The operator's:

> Um evento é uma experiência programada com atrações ou temáticas específicas
> (música ao vivo, jantar harmonizado) — foco em experiência e entretenimento.
> Uma promoção é uma oferta comercial focada em preço ou volume para estimular
> vendas imediatas (terça do chopp em dobro, 20% no rodízio) — foco em vantagem
> financeira direta.

An event sells an experience; a promotion sells a price. The schema draws no
such line, so every query, every metric and every reviewer reads "event" and
gets something else.

### The type is computed and then thrown away
`260810_post-kind-and-post-extraction-attribution.md` added a `kind` the model
returns, then **dropped every non-event without persisting it** — a deliberate
interim, taken when the alternative was a discriminator column on a table that
should not have existed in that shape. The consequence: a misclassification is
recoverable only by re-extraction, the counts are metric-only, and no promotion
or menu the pipeline has already read can be looked at.

### Free text will fragment without an anchor
The operator's example: a model left free will write `rock`, `rock-n-roll` and
`Rock And Roll` for one genre. This repo has already been bitten by the inverse
— `260806_filter-label-casing.md` records that exact-matching an LLM-produced
value against a configured label **zeroes the result** when casing differs.
Free text needs a vocabulary to aim at and case-insensitive matching to land.

## Current Behavior
Every persisted row is called an event whatever it is; non-events are counted
and discarded; nothing records what kind of event an event is.

## Desired Behavior
1. The stored entity is named for what it is: an item derived from a post.
2. Every item records its type.
3. Non-events are persisted, not dropped.
4. Every item may carry a free-text category.
5. The model is steered toward a known vocabulary without being confined to it.
6. A category differing only in case or spacing matches the known one.
7. The review queue keeps showing what needs a decision, regardless of type.

## Implementation Approach

### A. Rename the entity
`events.event` → `events.post_item`, `events.event_source` →
`events.post_item_source`, and their `event_id` columns to `post_item_id`.

**`post_item`, not `post`**, because one post can yield several rows — the
multi-event work exists precisely because a flyer lists four weeks at once. A
row is an item a post announced, not the post.

The `events` schema keeps its name: it is the event-extraction pipeline, and
renaming a schema touches every migration for no gain in truth.

**One typed table, not three.** The operator has asked for promotions and menus
as distinct entities, and this does not close that door — it is the reversible
half. Splitting a typed table later is a migration; merging three tables that
have grown apart is a rewrite. Nothing here should assume the split will never
happen: keep type-specific behaviour in code branching on `post_type`, so the
seam is visible when it is time to cut along it.

Rename via `ALTER TABLE ... RENAME`, never create-and-copy: the tables carry
live data and foreign keys, and a copy silently drops whatever it forgets.

### B. Persist `post_type`
`post_type text NOT NULL` on `events.post_item`, from the model's existing
`kind`. The reconciliation drop added by `260810_post-kind-…` §B is removed —
every extracted item is stored.

**The fail-toward-visible rule can now relax, and should be re-stated rather
than deleted.** Its original justification was that a dropped row was
invisible; a persisted, typed row is inspectable and correctable, so an unknown
type no longer needs to masquerade as an event. Store an unrecognised type
**verbatim** and let the console show it — that is strictly more honest than
coercing it, and it surfaces prompt drift instead of hiding it.

`post_type` joins the operator-editable fields, so a misclassification is fixed
in place through `operator_edited_fields`, which already exists.

**The review queue must not change shape.** It shows what needs a decision, and
that is a property of the row's state, not its type: a clean menu item with a
resolved venue auto-accepts and never queues, exactly as a clean event does. Do
**not** add a type filter to the predicate — the console can filter for display.
A `food` or `other` item with nothing to decide simply auto-accepts.

### C. A category, free but anchored
`category text NULL` on `events.post_item`, free text from the model.

Both prompts gain the field with a **vocabulary in the prompt** — an explicit
list of expected answers with an instruction to use one where it fits and to
answer freely only when nothing does. Seed it from what Recife actually has:
live music, DJ / club night, samba / pagode, forró, rock, MPB, jazz, sertanejo,
funk, karaoke, comedy, quiz / trivia, kids / family, workshop, food festival,
tasting, sports screening, party.

**The vocabulary is admin config, not a code constant.** Venue types and
busyness labels are already runtime-configurable in this project for the same
reason: the list will be wrong on contact with real data, and changing it must
not need a deploy.

Normalise on the way in — trim, collapse whitespace, casefold for comparison —
and store the **canonical vocabulary spelling** when a value matches one
case-insensitively, the model's own text when it does not. That is what stops
`rock`, `Rock` and `ROCK` becoming three categories while still letting a
genuinely new one through. Casefold, not `.lower()`: pt-BR text needs it.

**Do not reject an off-vocabulary answer.** The point of free text is to learn
what is missing; count off-vocabulary values so the vocabulary can be grown
from evidence rather than guesswork.

## Data, Config, And API Impact
- **Migration `0035_post_items`**: rename both tables and their id columns, add
  `post_type` and `category`. Rename constraints and indexes to match — a
  constraint named `uq_event_source_post` on a table called `post_item_source`
  is the next reader's confusion.
- **Back-fill:** every existing row is `post_type='event'` — that is what the
  pipeline believed when it wrote them, and it is what the console already
  shows. `category` stays NULL; it was never extracted, and inventing one from
  a title is a guess.
- **API:** `EventOut` gains `post_type` and `category`. **Verify before
  renaming any response field that no released mobile build reads these
  endpoints** — the events API is admin-only, so this should be safe, but
  confirm it rather than assume. Keep response field names stable in this PR
  even where the table renamed; the console is updated separately.
- **Rollback:** the migration's downgrade renames back and drops both columns.
  Test the downgrade — a rename migration that cannot go back is a trap.

## Error Handling And Observability
`event_extraction_posts_total{kind}` already carries the type; keep the label
name stable so existing dashboards survive. Add a counter for off-vocabulary
categories, labelled by the raw value, **capped** — an unbounded label from
model output is a cardinality bomb; count the top values and bucket the rest.

**Watch the off-vocabulary rate.** High means the vocabulary is wrong; near
zero means the model is being over-steered and everything is being forced into
the nearest listed word.

## Test Plan
Feature file: `tests/bdd/enrichment/post-items-and-categories.feature`

Scenarios:
- Store a promotion as a promotion.
- Store a menu item as a menu item.
- Store an event as an event.
- Store an unrecognised type verbatim.
- Let an operator correct an item's type.
- Auto-accept a clean menu item without queueing it.
- Queue an item of any type that needs a decision.
- Record the category the model returned.
- Match a category that differs only in case.
- Keep a category the vocabulary does not contain.
- Read the vocabulary from admin config.

Pytest unit tests:
- The migration renames both tables, their id columns, and every constraint and
  index; the downgrade reverses it; existing rows survive with
  `post_type='event'`.
- Category normalisation: exact, case-differing, accent-differing,
  whitespace-padded, empty, absent, and a genuinely new value.
- Both prompts contain the category field and the vocabulary — asserted
  directly on both, since extending only one is the likely half-fix.
- `post_type` round-trips through the patch API and is protected by
  `operator_edited_fields`.
- The queue predicate is unchanged by type — a clean item of every type stays
  out, a flagged item of every type comes back.
- Off-vocabulary label cardinality stays bounded.

## Acceptance Criteria
- The entity and its source table are named for what they hold.
- Every item stores its type; non-events are no longer dropped.
- An unrecognised type is stored verbatim, not coerced.
- Categories are free text, steered by an admin-configurable vocabulary, and
  case-insensitively canonicalised.
- The review queue's behaviour is unchanged.
- The migration's downgrade restores the previous schema.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None. If the rename cannot be done without breaking a consumer this plan has
not accounted for, stop and report rather than renaming half of it.
