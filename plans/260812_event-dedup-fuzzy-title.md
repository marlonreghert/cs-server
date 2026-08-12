# Event Dedup — title containment and shared lineup, with a low bar for proposing a merge and a high bar for applying one

## Branch
feature/event-dedup-fuzzy-title

## Goal
One real party stored under six title variants must become one row, or one row
plus a merge an operator can accept in a click — without ever collapsing two
genuinely different events a venue ran on the same night.

Two independent signals do this: **title containment** (§B) and **shared
lineup** (§B2). Neither alone reaches every duplicate in the motivating cluster,
and the second is the stronger of the two where it applies.

## Non-goals
- **Redefining `source_event_key` or `compute_event_identity`.** §A explains
  why, and it is the constraint the whole design is shaped around.
- **Venue attribution and date resolution.** `260812_event-attribution-and-
  dates.md`. This plan consumes their output; it does not duplicate it.
- **Repairing the 487 mis-attributed rows.**
  `260812_backfill-misattributed-links.md`.
- **Cross-venue merging.** Two rows at two venues are two events. Full stop.
- **Merging menu items.** `compute_menu_identity` owns `post_type == "menu"`
  and is deliberately dateless; nothing here touches that path.
- **An LLM in the merge path.** §C.
- **Admin-UI work in vibes_bot.** This plan puts merge suggestions on the review
  queue's API payload; rendering them is a follow-up.

## Evidence

### The cluster: one party, eight rows, six normalized titles
Conchittas Bar, the Rodolpho Produções 31st-anniversary party, as stored in the
2026-08-12 snapshot (Recife local dates; every row `venue_id`-linked and
`accepted` except the first):

```
'Aniversário do RODOLPHO Produções'   no date            pending_review  (missing_date)
'Aniversário do Rodolpho Produções'   2026-08-07 19:00   accepted
'31º Rodolpho Produções'              2026-08-07 19:00   accepted
'Rodolpho'                            2026-08-07 21:00   accepted   (location_text: "CONCHAS BAR")
'Rodolpho Produções'                  2026-08-07 21:00   accepted   (sources_disagree)
'SEXTOU NO CONCHITTAS BAR!'           2026-08-07 21:00   accepted
'Aniversário do Rodolpho Produções'   2026-08-08 00:00   accepted
'31 Anos'                             2026-08-08 00:00   accepted
```

Eight distinct identity tuples. The two rows at local `2026-08-08 00:00` are the
same night — a party that starts at 21:00 and gets posted about at midnight —
which is why the local calendar date alone does not collapse them either.

### Two easy answers, both measured, both nearly worthless alone
Measured against the whole corpus:

- Switching identity to the **Recife local date** instead of the stored UTC date
  collapses exactly **1** row (two `Homenagem aos 98 anos de Onildo Almeida`
  rows at Sala de Reboco, stored at `2026-08-07 03:00Z` and `2026-08-08 00:00Z`,
  both local 2026-08-07).
- Stripping **punctuation** in `normalize_title` collapses exactly **1** row.

Neither is the answer. Both are worth doing on their own merits, and neither is
done here — changing `normalize_title` re-keys the corpus (§A).

### The false positives are real, and they are in this dataset
```
Sempre Rock Bar, 2026-08-04:
  'Ação Leitura: Bate-papo com Marcelino Freire'
  'Ação Leitura: Bate-papo com Jeferson Tenório'

Entre Amigos O Bode, same programme, same week:
  'Oficina Vida de Inseto' / 'Oficina Cobra Gigante' / 'Oficina de Sorvete'
```

Two more the corpus volunteered, and both broke a draft of this design before
the measurement caught them:

```
Entre Amigos O Bode, 2026-07-15:
  'Férias Amigos Park — Semana 1' / '… Semana 2' / '… Semana 3' / '… Semana 4'

Casanova Ecobar, 2026-08-07:
  'Bolinha do Cavaco'  /  'JB do Cavaco'        (two different acts, one night)
```

A venue legitimately runs several distinct events on one night —
`Só Mais Uma` has three separate line-ups on 2026-08-08, `Teatro Riachuelo` has
three `… toca …` shows on 2026-08-07. Any rule with a low silent bar destroys
those.

### Why a similarity *score* is the wrong instrument
On this data a plain string ratio ranks the false positives **above** several
true positives. `'Ação Leitura: Bate-papo com Marcelino Freire'` against
`'…Jeferson Tenório'` shares five of nine tokens; `'Rodolpho'` against
`'Rodolpho Produções'` shares one of two. Any single threshold that merges the
second merges the first. The discriminating fact is not *how much* the titles
overlap — it is **whether each side carries a distinctive word the other lacks**.
In every false positive above, both sides do (`marcelino/freire` vs
`jeferson/tenorio`; `cobra/gigante` vs `sorvete`; `1` vs `2`; `bolinha` vs `jb`).
In every true positive, the shorter title's distinctive words are a subset of
the longer's.

### Titles are not the only evidence, and they are not the best evidence
Two of the cluster's rows are provably the same event and **no title rule can
show it** — each row's title appears in the *other's description*:

```
TITLE: Rodolpho Produções
  desc: "SEXTOU NO CONCHITTAS BAR; mais de 10 horas de festa, com DJ no intervalo."
TITLE: SEXTOU NO CONCHITTAS BAR!
  desc: "Rodolpho Produções comanda uma noite com mais de 10 horas de festa..."
```

They also share **eleven performers**. Measured across all 574 live
venue-linked rows, inside §D's candidate window:

| shared lineup names | candidate pairs | false positives |
|---|---|---|
| ≥ 1 | 9 | — |
| **≥ 2** | **7** | **0** |
| ≥ 3 | 7 | 0 |

Every pair at ≥2 is a genuine duplicate, and the threshold is not delicate —
no pair sits at exactly 2, so ≥2 and ≥3 agree. It reaches the two clusters
containment cannot:

```
Rodolpho Produções           ↔ SEXTOU NO CONCHITTAS BAR!               11 shared
ONILDO ALMEIDA & CONVIDADOS  ↔ Homenagem aos 98 anos de Onildo Almeida  3 shared
```

Neither pair has one title contained in the other. Lineup is therefore a
**first-class signal, not a tie-break** — see §B2.

### One row in the cluster is not an event at all
`'31 Anos'` has description `"Parabéns pelos seus 31 anos! Feliz aniversário!"`,
no lineup, no time, and comes from its own post. It is a birthday *greeting*
typed as `post_type = 'event'`, `category = 'party'`. Merging it into the party
would be wrong; it is a **classification** defect, fixed upstream in
`260812_event-attribution-and-dates.md` §E. This plan's job is only to make sure
the merge layer never absorbs it — see §B2's non-event guard.

### Identity is load-bearing and must not move
`event_identity.py`'s module docstring is explicit: migration
`0025_multi_event_posts` **imports and calls `compute_source_event_key` itself**
to back-fill every existing row before adding
`UNIQUE (source_handle, source_shortcode, source_event_key)`. Anything that
changes what the key hashes — including a "harmless" punctuation strip in
`normalize_title` — makes the next re-extraction derive a different key than the
migration wrote, orphaning every operator confirmation and turning the run into
a duplicate generator. `0026_event_sources` compounds it: its one-time
historical collapse calls `compute_event_identity`/`choose_canonical`/
`merge_event_fields` directly, which is why `merge_event_fields`'s
`operator_edited_fields IS NULL` branch is now frozen forever and says so.

### Today's merge deletes the loser
`event_merge._finish_absorption` reattaches the duplicate's
`post_item_source` rows to the canonical, clears its link candidates, and then
**hard-deletes the duplicate row**. The audit trail is the reattached sources;
the row itself is gone. That is defensible for an exact-identity merge (same
venue, same date, byte-identical normalized title — there was nothing to be
wrong about). It is not defensible for a fuzzy one.

### `fix/event-attribution-and-dates` changes the picture, so the bars must be
### set after it lands
`Teatro Riachuelo` (131 rows) and `Sempre Rock Bar` (87) are almost entirely
mis-attributed roundup rows that will move to other venues or detach once §A
lands — so today's crowded buckets will empty, and buckets that are empty today
will fill as correctly-attributed rows arrive at their real venues. Both
directions matter: the fix removes false neighbours **and** creates true ones.
The Conchittas, BeerDock and Sala de Reboco clusters below all come from the
venues' own accounts and are unaffected.

## Current Behavior
Two rows merge only when `(venue_id, starts_at::date, normalize_title(title))`
matches exactly, or — for a venue-less row — when
`(source_handle, starts_at::date, normalize_title(title))` matches a resolved
sibling's. A re-phrased title, a title with the venue's name in it, an
after-midnight start, or a missing date all defeat it. `event_merge`'s own
docstring names this as a known limitation preserved on purpose, on the grounds
that fuzzy matching "would also merge genuinely different same-night events at
the same venue, which is the worse error". That judgement stands; this plan
disagrees only with the conclusion that nothing can therefore be done.

## Desired Behavior
1. Merge two rows at one venue on one night when one title's distinctive words
   are a subset of the other's.
2. Propose — never apply — a merge when the distinctive words overlap but
   neither side contains the other.
3. Do nothing at all when they share no distinctive word, or when either title
   has none.
4. Treat a party that starts before midnight and a post about it timed after
   midnight as one night.
5. Attach an undated row to its dated twin at the same venue and account.
6. Make every fuzzy merge reversible, and never absorb an operator's row.
7. Leave `source_event_key` and `compute_event_identity` exactly as they are.

## Implementation Approach

One commit per section on a single branch and PR, per the operator's standing
preference for phased multi-defect fixes.

### A. A merge layer, not a new identity
Nothing in this plan changes `normalize_title`, `compute_source_event_key` or
`compute_event_identity`. The exact-identity merge keeps running first and keeps
behaving identically; title similarity is a **second pass** over what it leaves
behind, deciding only whether two *already-persisted* rows describe one event.

That containment is what makes the feature shippable. The stored key is
untouched, so nothing is re-keyed, no operator confirmation is orphaned, and
0025's and 0026's replays keep deriving what they derived. It is also why a
"just make identity fuzzier" variant is rejected outright rather than weighed:
identity is a hash written into 660 rows by a migration, and a hash cannot be
made fuzzy.

### B. Distinctive-token containment: the similarity function
A **set predicate**, not a score. For a title at a known venue:

1. `normalize_title(title)` — the existing function, reused, never a second
   normalisation — then split on any run of non-alphanumeric characters.
2. Drop, in this order: Portuguese function words (`de`, `do`, `da`, `no`, `na`,
   `com`, `e`, `o`, `a`, …); a **generic-event vocabulary** (`festa`, `noite`,
   `show`, `baile`, `sextou`, `domingou`, `aniversario`, `oficina`, `especial`,
   `edicao`, `semana`, `anos`, `open`, `bar`, …); every token of the **venue's
   own name**; and any token shorter than two characters **unless it is
   numeric**.
3. What remains is the title's **distinctive set**.

Then, for two rows in the same candidate window (§D):

- both sets non-empty **and** one is a subset of the other → **auto-merge**;
- both sets non-empty, they intersect, neither contains the other →
  **suggest**;
- either set empty, or the sets are disjoint → **nothing**.

Every decision is explainable to an operator as two token lists, which matters
more than a number they cannot argue with.

**Three rules that look like details and are not.** Each was found by measuring
a draft against the corpus, and each one broke a real listing:

- **Never strip bare numerals.** Dropping them merges `Férias Amigos Park —
  Semana 1/2/3/4` into a single row, destroying four real weeks of a holiday
  programme. Keeping them leaves `{ferias, park, 1}` and `{ferias, park, 2}`
  non-comparable, correctly. It also keeps `'31 Anos'` (distinctive `{31}`) out
  of the Rodolpho cluster, which is the conservative answer.
- **The minimum token length is two, not three.** At three, `'JB do Cavaco'`
  reduces to `{cavaco}`, which is a subset of `'Bolinha do Cavaco'`'s
  `{bolinha, cavaco}`, and two different acts on one night become one. At two,
  `{jb, cavaco}` and `{bolinha, cavaco}` are non-comparable.
- **Strip the venue's own name.** `'SEXTOU NO CONCHITTAS BAR!'` at Conchittas
  Bar reduces to an **empty** distinctive set and therefore never merges with
  anything — which is right: it is a generic Friday post that happens to be the
  same night as the party, and nothing in its text says so.

**Why not `instagram_cascade_service.name_similarity`.** This repo's default is
reuse, and the venue-resolution ladder deliberately shares one definition of
"these two names are the same place". This is a different question. That
function is built for venue names — `venue_core` strips venue-type words,
containment short-circuits to a flat 0.95 — and on this corpus it scores the
`Ação Leitura` pair above `'Rodolpho'`/`'Rodolpho Produções'`. Reusing it would
buy consistency between two questions that are not the same question. The new
function goes in its own module with a docstring saying exactly this, so the
next reader does not "fix" the duplication.

**Measured, corpus-wide, over 574 live venue-linked rows** (same-local-date or
within-8h candidate window, §D): **12 auto-merge pairs forming 3 clusters —
6 rows collapsed — and zero false positives**, plus **33 suggested pairs**. The
three clusters are the Rodolpho family (5 rows → 1), `SAMBINHA BEERDOCK` /
`Sambinha Beerdock Extended` at BeerDock (2 → 1), and the two identical
`Homenagem aos 98 anos de Onildo Almeida` rows at Sala de Reboco (2 → 1, the
same row the local-date change would have caught). The named false positives all
land in **suggest** or **nothing**: `Ação Leitura` → suggest
(`{acao,bate,freire,leitura,marcelino,papo}` vs `{…,jeferson,…,tenorio}`,
intersecting, neither contained); the three `Oficina …` → nothing (disjoint).

**Re-measure before setting anything live.** These numbers are from the
pre-attribution-fix corpus. Ship the measurement as a script under `scripts/`
alongside the feature, run it against a restored snapshot after
`fix/event-attribution-and-dates` lands, and put the post-fix numbers in the PR.
The generic-event vocabulary is runtime-configurable admin config, matching
`menu_expiry_days`, the post-category vocabulary and the busyness labels — this
project makes first-guess vocabularies configurable rather than shipping them as
code, and this one will need tuning per city.

### B2. Shared lineup: a second, independent auto-merge signal
Inside §D's candidate window, **two rows sharing at least two normalised lineup
names auto-merge**, regardless of what their titles say. This is not a
tie-break on the title rule and must not be implemented as one — it is an
independent sufficient condition, because it is the *only* signal that reaches
`SEXTOU NO CONCHITTAS BAR!` and the Onildo pair.

Normalise a performer name with the same casefold/accent-strip/punctuation
treatment the title tokens get, and reuse `union_lineup`'s notion of a name
rather than writing a second one.

**Two is the floor and it is load-bearing.** A single shared name is a resident
DJ or house band playing the venue on both of two genuinely different nights —
that is why the window alone is not enough. The measurement above found no pair
at exactly 2, so the rule is not balanced on a knife edge, but the threshold
must be configurable and the boundary must be tested at 1, 2 and 3.

**Non-event guard, applying to this section and §B alike.** Never merge across
differing `post_type`, and never absorb a row whose `post_type` is not `event`
into one that is. `'31 Anos'` is the live instance: same venue, same night,
plausibly the same subject, and genuinely not the same thing. A greeting, a
recap, a menu and a promotion each have their own lifecycle and must not be
folded into a party.

Lineup and title containment are **complementary, and the plan needs both**.
Neither alone reaches all seven of the cluster's real duplicates: `'Rodolpho'`
has an empty lineup and is reachable only by containment; `'SEXTOU NO
CONCHITTAS BAR!'` has an empty distinctive title set and is reachable only by
lineup.

### C. Three bands, and only the middle one is new surface
The operator asked for "a very low bar for same-venue events regarding title
match". A low bar for **proposing** a merge is safe and is what they should get.
A low bar for **silently applying** one destroys real listings — the corpus
above proves it does, not in theory but on rows that exist.

- **Auto-merge** (title subset **or** §B2's shared-lineup rule, either alone
  being sufficient): applied by the pipeline, immediately after the existing
  exact-identity pass, reusing `choose_canonical` / `merge_event_fields` /
  `_finish_absorption` **unchanged** in every respect except §E's
  reversibility. Both are subject to §B2's non-event guard.
- **Suggest** (overlap, no containment): persisted as a suggestion and surfaced
  on the review queue's payload with both distinctive sets, so the operator sees
  *why*. Applying it is an explicit admin action. Never applied by the pipeline.
- **Refuse** (disjoint, or either set empty): nothing is recorded and nothing is
  shown. A suggestion nobody would ever accept is queue noise, and this project
  has already learned that a queue full of undecidable items stops being read.

**No LLM anywhere in this path.** Not because the merge layer computes
`source_event_key` — it does not — but because a suggestion that changes between
runs re-opens a decision the operator already closed, and because a
non-deterministic merge cannot be unit-tested against the false-positive pairs
above, which is the only evidence that the bar is safe. The determinism argument
`openai_event_extraction_client`'s docstring makes for dates applies here
unchanged.

### D. The candidate window: same venue, same night
Candidates for a title-similarity comparison are rows at the **same
`venue_id`** whose starts are either on the **same Recife local date** or
**within 8 hours** of each other.

The disjunction is doing real work; neither half suffices, and this was measured
rather than assumed:

- The Conchittas rows at local `2026-08-07 21:00` and `2026-08-08 00:00` are
  5 hours apart across a local-date boundary — caught by the window, missed by
  the date.
- The Sala de Reboco `Homenagem` pair is 21 hours apart on one local date —
  caught by the date, missed by the window.

A "nightlife day starts at 06:00" cutoff was tried as a single unified rule and
is **not** strictly better: at a 4- or 6-hour cutoff it fixes Conchittas and
breaks Sala de Reboco. Record that, because it is the obvious idea and it is
wrong. 8 hours is the smallest window that works; the result is unchanged at 12
and 18 hours, so the rule is not perched on a threshold.

This is a candidate **window**, not a partition — it never becomes a bucket key,
and it never touches identity.

### E. Auditable and reversible
An auto-merge under a fuzzy rule must be undoable. Today
`_finish_absorption` hard-deletes the absorbed row.

Give `_finish_absorption` an explicit mode. The **exact-identity** path keeps
`delete`, unchanged — 0026's replay depends on today's behaviour and rewriting
the semantics of every merge the system has ever performed is not this plan's
business. The **title-similarity** path uses `supersede`: the absorbed row's
sources are reattached exactly as now, its link candidates are cleared exactly as
now, but the row itself moves to `STATUS_SUPERSEDED` and records which event
absorbed it. `superseded` is already the vocabulary for "an item the pipeline
retired but did not destroy" (`event_reconciliation.py:90`), it is already
excluded from the review queue and from every served path, and reusing it is
what the operator asked for. That the two modes differ is a wart; say so in the
docstring and note unifying them as a follow-up rather than smuggling it in
here.

Reversal is then mechanical: flip the superseded row back, detach the sources
that came from it. Ship the reverse as an admin action in the same PR — a merge
you cannot undo is not reversible just because the row still exists.

**Operator protections, reused not re-derived.** `event_merge._is_protected`
(confirmed or manually linked) and the `operator_edited_fields`-names-`venue_id`
refusal from `_merge_handle_group` both apply unchanged, per candidate, without
touching the rest of the group. `choose_canonical` returning `None` for a group
with two protected members still means "leave everything alone". One addition
specific to this feature: **a row whose `operator_edited_fields` names `title`
is never absorbed by title similarity** — the operator wrote that title, and
absorbing the row on the strength of it would use their own correction against
them. It may still be a *suggestion*.

### F. The undated row
`compute_event_identity` returns `None` without `starts_at`, so the undated
`'Aniversário do RODOLPHO Produções'` has no identity of any kind and sits in
the queue forever. The precedent for fixing that is
`_absorb_unresolved_sibling`, which already folds a venue-less item into a
resolved same-handle sibling.

Allow the mirror case, at the **auto** bar only and with every gate tightened:
an undated row may be absorbed by a dated sibling when they share a `venue_id`
**and** a `source_handle`, their distinctive sets are **equal** (not merely a
subset), the undated row's `first_seen_at` is within a configurable window
(default 14 days) of the sibling's date, and `post_type` is not `menu`
(`compute_menu_identity` owns dateless identity and must not be crossed). The
dated row is always the canonical — direction is structural, never incidental,
the same rule `_merge_handle_group`'s docstring insists on.

Measured: 5 undated venue-linked rows corpus-wide, of which exactly **1**
qualifies — the Conchittas row. Small, and that is the point: it clears a class
of permanently-stuck rows without opening a door.

### G. What the cluster looks like afterwards
Eight rows become **two**, and the second one is not a duplicate:

- **One merged Rodolpho row**, reached by both signals working together —
  `'Rodolpho'` and `'31º Rodolpho Produções'` by title containment (empty and
  one-name lineups respectively), `'SEXTOU NO CONCHITTAS BAR!'` by shared
  lineup (empty distinctive title set), the 8th-dated `'Aniversário…'` through
  §D's ±8h window, and the undated row through §F.
- **`'31 Anos'`, left standing on purpose** — held back by §B2's non-event
  guard once `260812_event-attribution-and-dates.md` §E re-types it, and by the
  disjoint-set refusal even before that. It is a birthday greeting, not the
  party.

An earlier draft of this plan claimed eight rows become three, treating
`'SEXTOU NO CONCHITTAS BAR!'` as unreachable. That was true of title similarity
alone and is no longer true: the lineup signal reaches it, which is precisely
why §B2 exists as an independent condition rather than a refinement.

## Data, Config, And API Impact
- **Migration `0037_event_merge_suggestions`** — a table for suggested merges
  (`event_id`, `candidate_event_id`, the two distinctive sets as evidence,
  created/decided timestamps, decision), plus a nullable `superseded_by` on
  `events.post_item` for §E. Additive; no back-fill; a suggestion is derived
  data and is safe to recompute.
- **Config (admin, runtime)** — the generic-event vocabulary; the Portuguese
  stopword list; the candidate window in hours (default 8); the undated
  absorption window in days (default 14).
- **Admin API** — `GET /admin/events/review` items gain a `merge_suggestions`
  list (additive; the console is a released client and nothing may be removed).
  Two new actions: apply a suggestion, and reverse a merge.
- **New file** — `scripts/measure_event_dedup.py`, the corpus measurement §B
  requires before the bars are set.
- **Rollback:** revert. Merges already applied are reversible by §E's admin
  action; the suggestion table is derived and can be dropped.

## Error Handling And Observability
- Extend `EVENT_MERGE_TOTAL` with `identity="title"` and outcomes `merged`,
  `suggested`, `refused_disjoint`, `refused_no_distinctive_tokens`,
  `refused_protected`, `refused_operator_title`. Keep the labels distinct rather
  than folding them into one `refused` — a Prometheus series exists only after
  its first increment, so the **absence** of `refused_protected` is itself the
  evidence that no operator's row was ever a candidate.
- **Watch the suggested-to-merged ratio.** If suggestions accumulate unactioned,
  the low bar is producing landfill rather than decisions, and the answer is a
  narrower suggest band, not a bigger queue.
- **Watch `refused_no_distinctive_tokens`.** A climb means the generic-event
  vocabulary has grown too greedy and is eating real titles.
- Log every auto-merge at info with both titles, both distinctive sets and the
  canonical's id. This is the record an operator reads when they ask why two
  listings became one, and it must be readable without a database.

## Test Plan

Feature file: `tests/bdd/enrichment/event-dedup-fuzzy-title.feature`

Scenarios:
- Merge a shortened title into its fuller form at the same venue on the same
  night.
- Merge two rows whose starts straddle midnight within the night window.
- Merge two identical titles stored on different UTC dates but one local date.
- Refuse to merge two workshops that share only their series prefix.
- Refuse to merge two talks that share everything except the speaker's name, and
  offer them as a suggestion instead.
- Refuse to merge four numbered weeks of one programme.
- Refuse to merge two acts that share only a surname.
- Refuse to merge a generic weekday post into a named party at the same venue.
- Refuse to merge two rows at different venues on the same night.
- Refuse to merge two rows at the same venue a week apart.
- Absorb an undated row into its dated twin from the same account.
- Refuse to absorb an undated row whose distinctive set is only a subset.
- Never absorb a confirmed row.
- Merge two rows sharing two performers even though neither title contains the
  other.
- Merge a row whose title shares nothing with its twin but whose lineup shares
  eleven names.
- Refuse to merge two rows sharing only one performer at the same venue on the
  same night.
- Refuse to merge a row that has no lineup on the strength of lineup alone.
- Refuse to merge a birthday greeting into the party it congratulates.
- Refuse to merge across differing post types at the same venue on one night.
- Collapse the whole Rodolpho cluster to a single row while leaving the
  greeting standing.
- Never absorb a row whose operator edited its title, but still suggest it.
- Never absorb a row whose operator edited its venue.
- Leave a group with two confirmed members entirely alone.
- Supersede rather than delete the row a title merge absorbs, and keep its
  sources on the surviving event.
- Reverse an applied merge and restore the absorbed row.
- Apply a suggestion only when an operator asks for it.
- Leave menu items entirely out of the title-similarity path.
- Keep the exact-identity merge behaving exactly as it does today.

Pytest unit tests:
- The distinctive-set function, pinned per rule: function words dropped;
  generic-event words dropped; venue-name tokens dropped; numerals **kept**;
  two-character tokens **kept**; an all-generic title yielding the empty set.
- The band predicate, pinned against the exact production strings — the six
  Rodolpho variants must all land in **auto** against each other where the
  subset holds; `Ação Leitura: Bate-papo com Marcelino Freire` /
  `… Jeferson Tenório` must land in **suggest**; the three `Oficina …` titles
  must land in **refuse**; `Férias Amigos Park — Semana 1..4` must land in
  **suggest**, never auto; `Bolinha do Cavaco` / `JB do Cavaco` must land in
  **suggest**, never auto. These are the regression tests; anything that loosens
  the rule has to break one of them first.
- Symmetry: the band for `(a, b)` equals the band for `(b, a)`, over the full
  production title list.
- The lineup rule, pinned against production lineups: `Rodolpho Produções` /
  `SEXTOU NO CONCHITTAS BAR!` (11 shared) and `ONILDO ALMEIDA & CONVIDADOS` /
  `Homenagem aos 98 anos de Onildo Almeida` (3 shared) must land in **auto**
  despite failing containment. Boundary at 1, 2 and 3 shared names. A pair
  where one side's lineup is empty must never auto-merge on lineup. Performer
  normalisation must treat `DAYANNE` and `Dayanne` as one name and `Dayanne`
  and `Dayanne Henrique` as two.
- The non-event guard: `'31 Anos'` (`post_type` non-`event` after
  `event-attribution-and-dates` §E) is never absorbed into the party, asserted
  both by post type and, independently, by the disjoint-set refusal so the guard
  is not the only thing holding it.
- Signal independence: a pair passing containment but not lineup auto-merges, a
  pair passing lineup but not containment auto-merges, and neither rule is
  implemented as a tie-break on the other.
- Determinism: the same inputs give the same bands and the same canonical across
  two runs and across two candidate orderings.
- The candidate window: same local date across a 21-hour gap includes; 8 hours
  across a local-date boundary includes; 25 hours excludes; a different venue
  excludes.
- `compute_source_event_key` and `compute_event_identity` are byte-identical
  before and after this feature, asserted on the production titles — the
  cheapest possible guard against §A being quietly undone.
- Transitive closure within one candidate window is order-independent and
  produces one canonical, asserted on the five-member Rodolpho cluster.
- `_finish_absorption` in `supersede` mode leaves the row readable with
  `superseded_by` set and its sources reattached; in `delete` mode it behaves
  exactly as today, asserted so 0026's replay path is provably untouched.
- Reversal restores status, `superseded_by` and source attachment.
- The migration is exercised by an up/down round trip, matching
  `tests/test_post_items_migration.py`'s existing shape.

**Assertions must name the surviving title and the absorbed one, not count the
rows.** A count-based assertion already stayed green here against a deliberately
reintroduced bug, because both passes computed the same wrong number. "8 became
3" proves nothing about *which* 3.

Manual or integration checks:
- Run `scripts/measure_event_dedup.py` against a **restored snapshot** — never
  production — after `fix/event-attribution-and-dates` has landed, and record
  the auto/suggest/refuse counts and every auto pair in the PR. A single
  unexplained auto pair blocks the merge.
- No re-crawl, no re-extraction, no external calls of any kind. Everything this
  feature needs is already in RDS.

## Acceptance Criteria
- The seven Rodolpho-family rows — including `'SEXTOU NO CONCHITTAS BAR!'`, via
  lineup, and the undated one, via §F — become **one** row at Conchittas Bar.
- `'31 Anos'` is still its own row, and is refused by the non-event guard as
  well as by the disjoint-set rule.
- A pair is auto-merged on shared lineup alone when containment fails, and on
  containment alone when lineup is empty.
- Every false-positive pair in the Evidence section is refused or suggested, and
  none is merged.
- No confirmed, manually-linked, `venue_id`-edited or `title`-edited row is ever
  absorbed.
- Every title-similarity merge is reversible, and the absorbed row is still
  readable with the event that absorbed it recorded.
- `compute_source_event_key` and `compute_event_identity` produce identical
  output before and after this change.
- The measurement script's post-attribution-fix output is recorded in the PR and
  contains no unexplained auto pair.
- `make test-feature`, `make test-unit`, `make test-bdd` pass, and CI's
  scratch-Postgres migrate step is green.

## Open Questions
None.
