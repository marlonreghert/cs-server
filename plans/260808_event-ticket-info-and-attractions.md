# Ticket Info And Attractions — stop discarding what the flyer says

## Branch
feature/event-ticket-info-and-attractions

## Goal
Capture two things every Recife flyer states and the event row currently throws
away: how to buy a ticket when there is no URL, and what is actually playing —
which acts, on which stage, in which musical styles.

## Non-goals
- **Reshaping `lineup`.** It stays a flat array of name strings, populated by
  the same union rule it uses today. `attractions` is additive; see §C for why
  `lineup` is NOT re-derived from the merged attractions.
- **Non-musical attractions.** "tarô & leitura de mãos", "telões", "duas
  pistas" as prose — these stay in `description`. Two observed examples do not
  justify a venue-amenity taxonomy with no downstream consumer.
- **Promotions.** "Aniversariante do Mês com Entrada…" is an offer, not an
  attraction. Also stays in `description`.
- **Serving any of this to the app.** Admin-only, as with every event field.
- **A structured ticket-channel model.** See §A for why free text wins.
- **The console's rendering of either field.** cs-server first;
  `vibes_bot/plans/260808_event-attractions-and-ticket-info-console.md` follows.
- **Changing what flags an event.** `attractions` never sets a review reason —
  see §D, which states that decision rather than leaving it implicit.

## Evidence

Both gaps are visible in the archived captions already run through the pipeline.

**Gap A — ticket references that are not URLs.** Shortcode `Dbt0M1ooIPp`:

```
🎫 TICKETS | 📍 SECRET CLUB • Club Metrópole | 📅 Sábado • 05/SET
```

`🎫 TICKETS` is a purchase reference with no link. The row has `ticket_url` and
`price_text` and nowhere for it, so it is discarded. Brazilian venues also
routinely say "link na bio", "ingressos na bilheteria", "vendas pelo WhatsApp
(81) 9xxxx", "@ticketeria", "lote promocional até 23:30" — none of which
survives today.

**Gap B — what is playing.** `lineup` is a flat array of bare strings. The
normaliser in `app/api/openai_event_extraction_client.py::_parse_event_fields`:

```python
lineup_raw = data.get("lineup")
lineup = [str(x) for x in lineup_raw] if isinstance(lineup_raw, list) else []
```

and both prompts ask for "array of performer/DJ names, empty array if none
stated" (`openai_event_extraction_client.py:77` and `:137`). No role, no stage,
no style, no DJ-vs-live distinction.

Shortcode `DbjkVcaGkmI` shows what that loses:

```
METRÓPOLE DELUXE 07/08
Pista NY (Estilo Musical Especial Ariana Grande + Pop Internacional)
  com DJ's: @ramoncabrall, @djthomashenry, @lea.farsaid
Pista Brasil (Show @elizamelloficial + Pop Brasil + Funk):
  DJ's @harrydmof e @arturdiegox
```

**Two stages**, each with its own musical styles, and a live show distinct from
the DJ sets. Today all six names collapse into one undifferentiated list.

**Nothing in the console depends on `lineup`'s shape.** The editable field list
is hardcoded twice to `['title','description','starts_at','ends_at',
'location_text','ticket_url','price_text']`
(`vibes_bot/app/admin/static/admin.html:4548` and `:4750`) — `lineup` is neither
rendered nor edited.

**What changed since this was first drafted.** The earlier draft targeted head
`0025` and a one-source-per-event row. Both assumptions are now false, and the
difference is load-bearing rather than cosmetic:

- Head is `0027_operator_edited_fields`.
- An event carries **many** sources (`0026_event_sources`), and two runtime
  paths merge fields across them: `event_reconciliation.py` (same post,
  re-extracted) and `event_merge.py` (different posts, same night).
- Field protection is per-field, driven by
  `PROTECTABLE_EVENT_FIELDS` / `_TEXT_FIELDS` in `event_reconciliation.py:178`,
  which `event_merge.py:105` imports rather than redefines.

So neither field can be added as a column alone: each must be placed
deliberately in that merge machinery, and they belong in **different halves of
it** — §B and §C.

## Current Behavior
A ticket reference without a URL is dropped. Performers are stored as a flat
list of names with no stage, style, or type; everything else in the flyer's
programming survives only as prose in `description`, if at all.

## Desired Behavior
1. Capture a non-URL ticket reference verbatim, alongside — not instead of — a
   URL when both are present.
2. Capture each attraction with its name, whether it is a DJ set or a live act,
   which stage it plays, and its musical styles.
3. Constrain musical styles to the existing product vocabulary so an event's
   styles are comparable to a venue's vibe profile.
4. Treat `ticket_info` as an ordinary protectable field: an operator's edit
   survives a contradicting re-extraction, and a null never overwrites it.
5. Accumulate attractions across the posts announcing one event, complementing
   a thin teaser from a fuller flyer rather than replacing either.
6. Keep `lineup` populated and unchanged in shape and in merge behaviour.
7. Skip one malformed attraction without losing its siblings, exactly as a
   malformed event is skipped without losing its siblings today.
8. Never let the richer output silently truncate a response.

## Implementation Approach

### A. `ticket_info` — free text, copied verbatim
A nullable `ticket_info text`, populated the way `price_text` and
`location_text` already are: copied, never interpreted.

A structured `{channel, handle, note}` model was considered and rejected. The
observed variety — a bare emoji label, a WhatsApp number, an `@handle`, a
platform name, a lote deadline — is arbitrary operational strings, not a domain
with reuse value. Nobody downstream needs "all events sold via WhatsApp", and a
channel enum would either need constant extension or would strip the one thing
that matters: the literal contact string.

**Both-present rule: store both, independently, no precedence.** A URL answers
"where do I click"; the text answers "what does a human need to know" — a lote
deadline beside a real Sympla link is exactly the case where suppressing either
loses information. Both prompts must say so explicitly, or the model will leave
`ticket_info` null whenever it finds a URL.

### B. `ticket_info` is a scalar — it joins the existing per-field table
Add `"ticket_info"` to **`PROTECTABLE_EVENT_FIELDS`** and to **`_TEXT_FIELDS`**
in `app/services/event_reconciliation.py`. That is the whole integration: both
merge paths inherit it, because `event_merge.py` imports both names from there
rather than keeping its own copy (the duplication that #154 deliberately
removed).

Both edits are required and each fails differently if forgotten:

- omit it from `PROTECTABLE_EVENT_FIELDS` → the field never merges across posts
  at all, so the teaser's value silently wins forever;
- omit it from `_TEXT_FIELDS` → a fresh `""` compares unequal to a stored
  `None`, registering a phantom change on every re-extraction.

Adding it in one place and not the other is the likely half-fix, so the test
plan asserts membership in both directly.

### C. `attractions` is a list — it follows `lineup`, not the scalar table
A nullable `attractions jsonb`: a list of `{name, type, stage, styles}`.

- `name` — free text; the act or handle as written.
- `type` — a small event-scoped enum: `dj | live | other`.
- `stage` — free text, optional; `"Pista NY"` as written.
- `styles` — a list constrained to `taxonomy.musica`, validated with the same
  `validate_category_labels` the venue vibe profile already uses.

**`attractions` must NOT be added to `PROTECTABLE_EVENT_FIELDS`.** That table
resolves a difference by *choosing a winner*, which for a list means a fuller
flyer's five acts replacing a teaser's two. Attractions are additive for exactly
the reason `lineup` is: a later post naming more performers is more information,
not a contradiction. So it unions, in the same four places `union_lineup` is
already called — `event_reconciliation.py` (confirmed and non-confirmed
branches) and `event_merge.py:246` and `:270`.

Missing any one of those four sites produces a merge that loses attractions only
on that path, which is the failure mode hardest to notice by hand; the test plan
covers each path separately rather than trusting one.

**Attraction identity, for the union.** Group by normalised `name` (reuse
`event_identity.normalize_title`'s casefold/strip-accents/collapse-whitespace —
do not write a second normaliser). Within a name group:

- entries whose `stage` differs and is non-null on both stay **separate** — one
  DJ playing both pistas is two real slots;
- a null `stage` absorbs into a non-null one for the same name — the teaser said
  the DJ was playing, the flyer said where;
- `type`: a specific `dj`/`live` beats `other`; two disagreeing specific types
  keep the first seen;
- `styles`: union, preserving first-seen order.

**Why the nesting goes at the performer and not the stage.** A flat list of
styles per event cannot say which DJs belong to which pista — exactly the
information the METRÓPOLE DELUXE flyer conveys. But grouping attractions *under*
stage objects is more than needed and is where a reasoning model's JSON degrades
under token pressure (arrays of arrays). Tagging each attraction with its own
`stage` keeps the list flat, per-item validatable, and reuses the existing "skip
one malformed item, keep its siblings" behaviour. The cost is a repeated stage
string per performer, which is bounded and cheap.

**Why `type` is a new enum and not `taxonomy.music_format`.** That vocabulary
(`DJ`, `Som ao vivo`, `Banda ao vivo`, `Roda de samba`…) describes a venue's
standing programming. Forcing "Show @elizamelloficial" into "Banda ao vivo" vs
"Som ao vivo" makes the model guess whether there is a full band — detail the
caption never states. `dj | live | other` is the coarser fact the flyer supports.

**Why `styles` DOES reuse `taxonomy.musica`, and what it costs.** Reusing it
makes an event's styles directly comparable to a venue's vibe profile at no
design cost, since the validator exists. The loss is real and worth naming:
"Pop Internacional" and "Pop Brasil" — the two labels the flyer uses
*specifically to tell the stages apart* — both collapse to `Pop`. That is
tolerable **only** because `stage` preserves the distinction verbatim. The
coarsening is backstopped by another field, not by nothing.

**`lineup` is derived per post, and keeps its own union across posts.** Each
extraction sets that post's `lineup` from its own `attractions[].name`, so one
model output yields both representations with no duplicate question. Across
posts, `lineup` keeps using `union_lineup` on the name strings exactly as today
— it is **not** re-derived from the merged attractions.

Re-deriving was considered and rejected: `attractions` is NULL on every existing
row (no back-fill, §Data), so a legacy event's populated `lineup` would be wiped
the moment a new source attached. The accepted cost of not re-deriving is an
asymmetry — `@djthomashenry` and `DJ Thomas Henry` normalise together as one
attraction but remain two `lineup` entries. That is precisely today's `lineup`
behaviour, so it is a pre-existing imprecision rather than a new one.

### D. Attractions never flag
A disagreement between two posts about an act's `type` resolves silently, per §C.
It does not set `review_reason`.

Stated explicitly because the project's standing rule points the other way — a
value we had to choose between is normally flagged, as `sources_disagree` and
`weekday_mismatch` do. The exception is deliberate: `attractions` is additive
like `lineup`, which has never flagged, and a caption calling the same act a DJ
set in one post and a show in the next is ambiguous by nature rather than
erroneous. Flagging it would refill the queue that `0027` was written to empty,
for information no operator can adjudicate better than the merge can.

### E. The output budget is the real risk
Turning each performer from a bare string into a four-field object is roughly a
4–6× per-entry token increase — and `gpt-5.6-luna` is a reasoning model whose
**invisible** reasoning tokens count against the same `max_completion_tokens`.
Reasoning load rises too, because this asks the model to *classify* (dj vs live)
and *map free text into a controlled vocabulary*, which is qualitatively harder
than the copy-verbatim instructions the rest of that prompt is built on.

Three budgets must move together, and **both prompts must be extended** —
`EXTRACTION_PROMPT` (`:51`) and `MULTI_EVENT_EXTRACTION_PROMPT` (`:102`) — or
the single-event path silently never produces the new fields:

- `MULTI_EVENT_PER_EVENT_COMPLETION_TOKENS` (currently 300) — roughly 450–550.
- `MULTI_EVENT_BASE_COMPLETION_TOKENS` (1536) — a modest bump; reasoning
  overhead does not scale purely per event.
- `DEFAULT_MAX_COMPLETION_TOKENS` (4096) — **this one is a flat cap not scaled
  by event count**, which is precisely the hazard `docs/venue-retrieval-storage.md`
  §4 already recorded: a flat budget truncated a variable-length response, the
  JSON failed to parse, and the whole batch fell back while still being billed.

Worst case is a promoter roundup near `event_extraction_max_events_per_post`
(20): twenty events of richer payload. Watch
`event_extraction_posts_total{outcome="truncated"}` after deploy — a single
truncation means the budget is still wrong, and truncation already persists
nothing partial by design.

## Data, Config, And API Impact
- **Migration `0028_event_ticket_info_and_attractions`** from head
  `0027_operator_edited_fields`: add `ticket_info text` and `attractions jsonb`
  to `events.event`, both nullable, no default. Both live on the **event**, not
  on `events.event_source` — they are merged fields like `title` and `lineup`,
  and each source's own copy is already retained in its `raw_extraction`.
- **No back-fill.** Unlike `0025`'s `source_event_key`, these describe
  information the model was never asked to produce on earlier runs. NULL is the
  historically accurate value, not a placeholder needing repair; a row gets real
  data when re-extracted, consistent with the "re-extraction is
  operator-triggered" stance in `260806_multi-event-posts.md`.
- **No constraint changes.** Neither column participates in
  `uq_event_source_post`.
- **Downgrade** is a plain column drop — no destructive-refusal guard needed,
  unlike `0026`'s, because dropping these discards per-row detail and never
  merges or deletes rows.
- **DAO:** both columns join `_EVENT_COLUMNS` (`rds_venue_store.py:439`) and
  **both** event projections — `_EVENT_SELECT` (`:479`) and
  `_EVENT_SOURCE_SELECT` (`:507`). Missing the second is invisible to most
  tests and silently starves reconciliation of the values it is merging.
  `attractions` is jsonb and must bind through the existing `_jsonb()` helper —
  psycopg has no adapter for a bare `dict`/`list` against `CAST(:p AS jsonb)`,
  the trap `0026` already hit.
- **`tests/rds_fake.py` must model both columns**, including `attractions`
  surviving a round-trip as a list. The fake has twice modelled the happy path
  and not the constraint, and both times a real defect reached CI or production.
- **API:** `EventOut` and `EventPatch` gain both fields — `EventPatch` gains
  `ticket_info` only (`attractions` is not operator-editable, matching `lineup`).
  Additive; `admin_events_router.py:97` and `:171`.
- **Serving:** none.

## Error Handling And Observability
A malformed attraction is skipped and counted; its siblings persist. A style
outside `taxonomy.musica` is dropped rather than stored — an unvalidated label
would silently corrupt the vocabulary the venue side depends on.

Metrics: reuse the `event_extraction_malformed_events_total` shape for a dropped
attraction, and keep `outcome="truncated"` as the budget alarm.

## Test Plan
Feature file: `tests/bdd/enrichment/event-ticket-info-and-attractions.feature`

Scenarios:
- Capture a non-URL ticket reference from a caption that has no link.
- Capture both a ticket URL and a text reference when the caption has both.
- Leave `ticket_info` null when the caption says nothing about buying.
- Capture two stages from one flyer, each attraction tagged with its own stage.
- Distinguish a live act from a DJ set in the same event.
- Constrain styles to the product vocabulary and drop an unlisted one.
- Populate `lineup` from the attractions' names.
- Skip one malformed attraction and keep its siblings.
- Persist nothing partial when the response truncates, and count it.
- Leave both fields null for an event whose flyer states neither.
- Accumulate a later post's attractions onto an earlier post's event rather than
  replacing them — the cross-post merge path.
- Fill in a stage from a fuller flyer for an act a teaser named without one.
- Keep an operator-edited `ticket_info` when a later post contradicts it, and
  flag the divergence.
- Never overwrite a known `ticket_info` with a null from a later post.

Pytest unit tests:
- The attraction normaliser: well-formed, missing optional `stage`, unknown
  `type`, non-list `styles`, an unlisted style, and an entry that is not an
  object at all.
- `lineup` derivation matches `attractions[].name` in order, per post.
- The attraction union: name normalisation, two stages for one name kept apart,
  a null stage absorbed by a non-null one, `type` precedence, `styles` union.
- `"ticket_info"` is a member of BOTH `PROTECTABLE_EVENT_FIELDS` and
  `_TEXT_FIELDS` — asserted directly, since adding it to one is the likely
  half-fix.
- `"attractions"` is NOT a member of `PROTECTABLE_EVENT_FIELDS` — asserted, so a
  later edit cannot quietly turn an additive list into a contested scalar.
- Attractions union on all four merge sites: both `event_reconciliation`
  branches and both `event_merge` branches.
- Both prompts contain the new fields — asserted directly, since extending only
  the multi-event prompt is the likely half-fix.
- Budget arithmetic at 1, 10 and 20 events, pinning that the flat single-event
  cap also moved.
- The migration adds both columns nullable and its downgrade drops exactly them.
- `_EVENT_SELECT` and `_EVENT_SOURCE_SELECT` both project both columns.

Manual or integration checks:
- Re-extract the real Métropole posts and confirm the METRÓPOLE DELUXE event
  carries two distinct stages with the right DJs under each, and that SECRET
  CLUB carries `🎫 TICKETS` in `ticket_info`.
- Re-extract a `@recifequecabenobolso` roundup and confirm no `truncated`.

## Acceptance Criteria
- A non-URL ticket reference is captured, and coexists with a URL.
- An attraction carries name, type, stage and validated styles.
- The two-stage flyer produces attractions tagged with their own stages.
- Styles outside the vocabulary are dropped, not stored.
- `lineup` is unchanged in shape and in cross-post merge behaviour.
- `ticket_info` participates in per-field operator protection; `attractions`
  unions on every merge path and never flags.
- One malformed attraction does not lose its siblings.
- Both prompts and all three token budgets are updated.
- No truncation on a 20-event roundup.
- `make test-feature`, `make test-unit` and `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None. The exact token figures are set from the measured first run rather than
asserted here, and `outcome="truncated"` is the alarm if they are wrong.
