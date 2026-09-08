# Stop non-events reaching the feed, and normalise recurrence text

## Branch
fix/events-non-event-and-recurrence-normalisation

## Goal

Two data-quality defects that no amount of app work can fix, plus one
projection bug found while measuring them:

1. **A recurring venue SERVICE OFFERING must never be classified as an
   event.** A lunch buffet served Tuesday to Sunday, a standing Thursday
   happy hour, opening hours, a permanent menu, a daily special — these are
   the venue being open, not something staged on a night. Today the
   extraction prompt's own precedence rule forces them into `kind: "event"`,
   and they reach the feed. 9 of the 44 live occurrences (20.5%) are this
   class. Fix the rule, not the rows.

   **Revision 3 simplifies this rule under an explicit operator decision**
   (§0). The rule now asks one question — what does the post ANNOUNCE — and
   answers `menu`/`promotion` whenever the answer is food or a price,
   however many days the post names. Genuine food-anchored events are
   accepted as collateral. §1b names exactly what that sacrifices.
2. **`recurrence_text` must be casing-normalised in the serving
   projection.** 33 of 44 occurrences carry it and mobile 1.4.1 renders it
   prominently; the live corpus contains two pure-casing duplicate pairs
   (`'Toda QUARTA'`/`'toda quarta'`, `'TODOS OS SÁBADOS'`/`'todos os
   sábados'`).
3. **`ends_at` must stop being a stale absolute instant on a recurring
   occurrence.** 15 of 44 occurrences carry `ends_at`, and **15 of 15 have
   `ends_at` strictly before their own `starts_at`** — there is not one
   correct `ends_at` in production today.

Plus one decision recorded, not built: what the projection could offer so
mobile can build an events filter without client-side bucketing
(see "The events-filter question").

## §0 — Operator decision, 2026-09-07 22:13 Recife: simplify the non-event rule

Verbatim: *"its ok to miss food events as of now. its better then having
dirty events or complex solutions"*

This **inverts the optimisation target revision 2 was built around** and
takes precedence over anything earlier in this plan or in
`141-FEEDBACK.md`. Revision 2 spent a three-paragraph PROGRAMMED OCCASION
vs. THE VENUE OPERATING clause, an 18-row category audit, a four-row
near-miss separator table and 13 synthetic controls defending the
food-anchored boundary. The operator has said that boundary need not be
defended.

**What the decision buys.** A false negative at the food boundary — a
`food festival`, a `tasting`, a feijoada-plus-samba, a rodízio-plus-jogo
answered `menu` instead of `event` — is now an ACCEPTABLE cost. A dirty
feed is worse than a thin one, and a complex rule is worse than both.

**What it does NOT buy — the two boundaries this concession must not cross.**

1. It covers the FOOD/PRICE-anchored boundary and nothing else. `karaoke`,
   `quiz / trivia`, `kids / family` and **every music or genre night**
   remain fully protected. A rule that also kills
   "todo sábado tem sertanejo" is still a defect, and §1a shows this one
   does not.
2. The cost is stated, not silent. §1b names the shapes and the live rows
   the simplified rule sacrifices, and the re-admission follow-up is named
   in "Follow-ups" so the trade is deliberate and reversible.

**Deliberately DELETED in revision 3, and why** — kept here so a later
reader does not restore them as an oversight:

| removed | why it existed | why it goes |
|---|---|---|
| the third prompt paragraph ("FOOD AND PRICE DO NOT DECIDE THIS BY THEMSELVES", ~150 of the clause's ~260 tokens) | to keep food-alongside shapes as `event` | it defends the conceded boundary, and it is the paragraph that made the rule hard to reason about |
| §1a's 18-row `DEFAULT_CATEGORY_VOCABULARY` audit | to derive the rule against every shipped category | 14 of the 18 rows are either unaffected or conceded; §1a keeps the 4 rows the concession does NOT cover |
| §1a's four-row near-miss separator table | to show the rule separated food-alongside pairs from standing offers | those pairs now land on the same side, so the table asserts nothing |
| synthetic controls S1, S2, S3, S8 (`food festival`, `tasting`, feijoada+samba, rodízio+jogo) | blocking gates on the food boundary | they gate a boundary that is now conceded; recorded by name in §1b instead |
| the "34 rows, zero false positives incl. S1-S8" acceptance line | the gate for the above | replaced by a 30-row gate whose zero-false-positive clause covers only the protected set |

**N4 dissolves.** "The thin food boundary is under-tested" was raised as a
defect against revision 2. Under this decision it is not a defect: it is
the accepted consequence, recorded here and in §1b. It is not carried as an
open finding.

## Non-goals

- **A title blocklist, a `category` blocklist, or any downstream string
  filter that hides a row while leaving `post_type = 'event'` in RDS.** See
  "Why a general rule, and why a blocklist is not achievable".
- **Defending the food-anchored boundary in the `kind` rule.** Conceded by
  §0. A recurring feijoada with a roda de samba, a weekly guided
  degustação, a recurring food festival and a rodízio-with-the-game will
  be answered `menu`/`promotion` by the simplified rule. Named in §1b,
  re-admission named in "Follow-ups". Not a defect this round.
- **Retro-fixing rows already in RDS by migration.** No migration, no bulk
  UPDATE. Existing wrong rows are corrected through the two operator paths
  in "Removal path for rows already projected" — both of which already
  exist and neither of which this change writes code for.
- **A hard-delete endpoint for an event.** This repo already retired
  "drop the non-event without persisting it" (`app/models/event_kind.py`,
  `resolve_post_type`'s docstring): a persisted, typed, inspectable row is
  the honest record. Reclassify, never delete.
- **Changing `EventOccurrence`'s field set.** Nothing added, removed,
  renamed or retyped — the 1.4.0 cross-repo contract is append-only and
  three fields change VALUE semantics only, exactly as `venue_neighborhood`
  and `ticket_url` did in `plans/260906_events-venue-bairro-and-ticket-url.md`.
- **A new Redis key family, a new serving parameter, or a category index.**
  Offered, sized and pinned as a follow-up in "The events-filter question";
  deliberately not built in 1.4.1.
- **Touching `weekdays_from_recurrence_text`** (`app/services/event_date_
  resolver.py:418`) or anything else that reads `recurrence_text` from RDS.
  Normalisation happens at the projection boundary only; RDS keeps the
  verbatim extraction.
- **Re-parsing recurrence prose to invent occurrence days.** Unchanged.
- **`mode="handles"` re-extraction behaviour, the per-venue cap, and the
  already-extracted skip** (`plans/260826_skip-already-extracted-posts.md`).
  Used as-is by Path B1; not modified.
- **A dry-run / non-persisting mode on `POST /admin/trigger/
  event_extraction`.** Considered as the safe production proof and rejected:
  it adds a second branch to a live write path in order to avoid using that
  write path. Path B0's offline script achieves the same proof with no
  production code at all (§4).
- **Un-confirming a row, or any RDS write outside the admin API.** Recorded
  as a rollback limitation in §4, not built here.

## Evidence

### The corpus: all 44 live occurrences, each fetched individually

`GET /events?city=recife&from=2026-09-07&to=2026-11-03&page_size=100`
against production on 2026-09-07 returned `total_count: 44`,
`has_more: false`, 44 occurrences over **21 distinct `event_id`s** and 10
venues. Every occurrence's detail was then fetched individually from
`GET /events/{occurrence_id}` (44/44, 200). Every number below is a census
of that set, not a sample.

### C1 — the non-event class, and its root cause

Two live events are unambiguously a standing venue service offering:

| event | venue | `category` | `recurrence_text` | occurrences | `time_known` |
|---|---|---|---|---|---|
| `Buffet de Terça a Domingo` | Cachaçaria Tradição | `buffet` | `Terça a Domingo` | **6** | true |
| `QUINTA É DIA DE HAPPY HOUR` | Downtown Beer Garden | `happy hour` | `Toda quinta` | **3** | true |

**9 of 44 occurrences — 20.5% of the live feed.** The buffet is, by
occurrence count, the single largest `event_id` in the feed. Its whole
description is `'A melhor hora do dia 🍛'`. The happy hour's description is
nothing but a price list: `'Promoções em dobro: caldinhos x2, gin tônica x2,
caipirinha x2, caipiroska x2 e bananinha x2. Chopp Amstel gelado por R$ 6,99
e Amstel 600ml por R$ 10,99.'`

The cause is a **self-contradiction inside one prompt constant** —
`_KIND_FIELD_DOC` in `app/api/openai_event_extraction_client.py` (shared by
both prompts, so neither can drift). It defines the vocabulary:

- `"promotion": an offer or price advantage — happy hour`
- `"menu": a dish or menu announcement, including a daily special`

and then orders the decision:

- `a happening with a date or recurring schedule -> "event"; else an offer
  or price advantage -> "promotion"; else a named dish or menu -> "menu"`

A buffet served Tuesday to Sunday and a happy hour every Thursday both state
a recurring schedule, so the **first branch fires and the `promotion` and
`menu` branches are unreachable for exactly the posts they were written
for** — including the `happy hour` example the `promotion` bullet names by
hand. The prompt already carries a pre-ladder test ("does this announce
something attendable", added for recaps and birthday greetings) which proves
this shape of correction is the established way to fix the ladder; that test
simply does not cover standing offers, because a buffet IS attendable.

The `"Especial do dia"` case named in the 1.4.1 brief — one generic
recurring title that was 28% of the feed at one point — is the same
mechanism: a daily special is the `menu` bullet's own written example, and
"daily" is a recurring schedule, so the first branch wins.

### What does NOT distinguish the class — hypotheses tested and killed

- **Absence of a start time — FALSE.** Both offenders have
  `time_known: true` (buffet `14:30Z` = 11:30 local; happy hour `01:00Z`).
  A "no stated time" filter catches neither. Meanwhile the 4 genuine
  `time_known: false` occurrences are all real parties (`FERIADÃO
  METRÓPOLE`, `HALESSIA NA METRÓPOLE!!!`, `METROPOLE EXPERIENCE`,
  `BONITO – PE`). The signal is inverted from the guess.
- **`is_recurring` — FALSE.** 33 of 44 occurrences are recurring, and 24 of
  those 33 are unambiguously genuine recurring nights (`Sambinha Downtown`,
  `FORRÓ DOS PAIS`, `Lovezinho`, `Drag Ataque`, `Residência drag da
  Metrópole`, `Baile Dançante`, and two `Aula de FORRÓ` rows). Recurrence
  is the norm, not the tell.
- **An empty `lineup` — FALSE.** Both offenders have `lineup: []`, but so do
  `Lovezinho`, `Drag Ataque`, `Sambinha Downtown`, `FERIADÃO METRÓPOLE`,
  `QUINTA…` and `37ª REFENO`. 12 of 21 events have no acts.
- **`recurrence_text` shaped as a day RANGE (`Terça a Domingo`) rather than
  a weekday cadence — WEAK.** It fires on the buffet and on nothing else,
  but n=1, and it would misfire on a genuine multi-day festival
  ("de quinta a domingo"). Secondary evidence only, never the rule.
- **An off-vocabulary `category` — WEAK, and useful only as a monitor.**
  Both offenders are off `DEFAULT_CATEGORY_VOCABULARY` (`buffet`,
  `happy hour`), but so are three genuine events (`drag show`, `brega`,
  `turismo / excursão`): 5 of the 11 distinct live categories are
  off-vocabulary. Precision would be 2/5.

**The signal is in the caption — in WHAT is being offered — and the
classifier already has the field and the vocabulary to say so.** The fix
belongs in the classifier's precedence, not in a filter after it.

### Why a general rule, and why a blocklist is not achievable

- Titles are **model-authored free text**. A blocklist is a list of the
  strings this model happened to emit last month; the same buffet reposted
  next week can come back as `Almoço de Terça a Domingo`, `Self-service`,
  or `Buffet livre`. Zero of the 21 live titles repeat across venues.
- **It cannot be right on either side.** `QUINTA É DIA DE HAPPY HOUR` must
  go, but `Sambinha Downtown` — at the SAME venue, whose own caption reads
  "HAPPY HOUR a tarde toda" — must stay. A "happy hour" blocklist kills
  both; no title or caption substring separates them. (Revision 2 made this
  point with a hypothetical `Happy Hour com Samba ao Vivo`, which §0 now
  concedes — its announced subject reads as a happy hour. `Sambinha
  Downtown` is a real live row, is pinned in §1a and §5, and makes the
  argument without depending on the conceded boundary.) Conversely
  `"Especial do dia"`
  reached 28% of the feed precisely because ONE generic title carried many
  *different* posts — blocking that string would have removed genuine items
  along with the noise.
- **It puts the judgement at the wrong layer.** A downstream filter leaves
  `post_type = 'event'` in the system of record — wrong data behind a
  curtain — while the classifier keeps making the identical mistake at every
  new venue we onboard. The RDS row is what the admin console, the review
  queue, `event_reconciliation` and every future consumer read.
- The five-value `kind` vocabulary already has a correct home for both
  offenders (`menu`, `promotion`). Nothing new needs inventing; the ladder
  needs to become reachable.

### C2 — `recurrence_text` is unnormalised

33 of 44 occurrences carry it (identical to the 33 with `is_recurring:
true`), across **9 distinct raw values**:

| n | value |
|---|---|
| 6 | `'Toda QUARTA'` |
| 6 | `'Terça a Domingo'` |
| 3 | `'TODOS OS DOMINGOS'` |
| 3 | `'Aos domingos'` |
| 3 | `'toda quarta'` |
| 3 | `'Toda quinta'` |
| 3 | `'Toda sexta, às 21 horas'` |
| 3 | `'todos os sábados'` |
| 3 | `'TODOS OS SÁBADOS'` |

Two pairs differ by **casing alone** — `'Toda QUARTA'`/`'toda quarta'` (9
occurrences) and `'TODOS OS SÁBADOS'`/`'todos os sábados'` (6). That is 15
of the 33, side by side in one shelf, saying the same thing in two
spellings. `'TODOS OS DOMINGOS'` vs `'Aos domingos'` differ by PHRASING, not
casing, and must stay distinct.

This is raw extraction output of the same class as `category` and
`ticket_url` — the prompt explicitly orders it copied verbatim ("the raw
recurrence phrase"), so the model is behaving correctly and the fix is not
a prompt fix.

### C3 — `ends_at` is inverted on 15/15 occurrences that carry it

`app/services/redis_projection_service.py:481` sets
`ends_at=row.get("ends_at")` — the source row's single stored absolute end
instant — inside the per-occurrence loop, while `starts_at` comes from the
re-derived `Occurrence`. For a recurring row whose days are re-derived from
the weekday PATTERN (`app/services/event_occurrences.py`), the stored
`ends_at` stays pinned to the announcement's original date. Measured:

| event | occurrence `starts_at` | projected `ends_at` | delta |
|---|---|---|---|
| `Buffet de Terça a Domingo` | `2026-09-27T14:30Z` | `2026-08-04T18:00Z` | −54 days |
| `Aula de FORRÓ` (×2 rows) | `2026-09-23T22:00Z` | `2026-08-12T23:00Z` / `2026-09-02T23:00Z` | −42 / −21 days |
| `Residência drag da Metrópole` | `2026-09-26T03:30Z` | `2026-09-05T05:00Z` | −21 days |

**15 occurrences carry `ends_at`; all 15 are recurring; all 15 are
inverted. Zero non-recurring occurrences carry `ends_at` at all**, so there
is no correct example in production and no regression risk in the
non-recurring branch. The time-of-day component is plausible in every case
(3.5h, 1h, 1.5h), which is what makes the duration-carry fix below the right
shape rather than a fabrication.

### The precedent this change follows, in the same function

The projector already substitutes a serving-shaped value over the RDS one,
once per source row, outside the occurrence loop, with a labelled counter —
`classify_ticket_url` at `redis_projection_service.py:459-466`, whose own
comment states the rule: *"the projection substitutes a serving-shaped value
over the RDS one, and RDS keeps the verbatim extraction"*, and *"calling it
per occurrence would recompute an identical answer up to 22 times AND scale
the counter by recurrence multiplicity"*. C2, C3 and the category work all
adopt that exact shape.

`self.redis_only_dao.client` is already available to the projector and
already used once per cycle for admin config (`load_hide_promoter_events`,
line 372) — the same handle `load_post_category_vocabulary` needs.

### The operator paths that already exist

- `PATCH /admin/events/{event_id}` accepts `post_type`
  (`app/routers/admin_events_router.py:327-348`, route at :707) and records
  the field in `operator_edited_fields` (:721-722).
- `app/services/event_projection_selection.py::is_selectable` returns False
  unless `post_type == "event"`, so a PATCHed row leaves the projection on
  the next cycle through the existing prune-then-delete
  (`redis_projection_service.py:544-576`). No deletion code is needed.
  `SELECTABLE_STATUSES = ("accepted", "confirmed")`, so confirming a row
  does not deproject it.
- **A PATCH alone does NOT protect `post_type` from the next
  re-extraction.** Revision 1 of this plan asserted it did ("`post_type` is
  in `event_reconciliation.py`'s operator-protected field tuple, so a later
  re-extraction of the same post cannot flip it back"). That is **false for
  a non-confirmed row**, which is the state every row in this corpus is in.
  `reconcile_post_events` consults `operator_edited_fields` on ONE branch
  only — `event_reconciliation.py:749`, `existing["status"] ==
  STATUS_CONFIRMED`, via `_confirmed_update_fields`. Every other row takes
  the branch at `:790-802`, which does a plain `fields.update(prepared)`:
  **every** content field is overwritten (`post_type`, `title`, `starts_at`,
  `category`, …), `status` is recomputed by `is_clean_extraction`, and
  `operator_edited_fields` is never read. So the protection is
  **PATCH _then_ confirm**: the PATCH records the field
  (`admin_events_router.py:721-722`), and `POST /admin/events/{id}/confirm`
  (`:731-738`) moves the row onto the branch that honours the record.
  Because `apply_operator_field_protection` is PER FIELD, patching only
  `post_type` freezes only `post_type`: title, dates, lineup and everything
  else keep refreshing from each re-extraction, and a model that later
  disagrees raises `REVIEW_REASON_DIVERGES_FROM_CONFIRMED` instead of
  silently winning. Cost on record: the admin API has `confirm` and
  `reject` but **no un-confirm**, and `EventPatch` does not accept `status`
  (`admin_events_router.py:327-347`), so confirming is one-way through the
  API today.
- `POST /admin/trigger/event_extraction` with
  `{"eligibility": {"mode": "handles", ...}}` is the sanctioned deliberate
  re-extraction path and calls the model unconditionally, bypassing the
  already-extracted skip (`plans/260826_skip-already-extracted-posts.md`,
  Non-goals).

### Constraint on record

`app/models/post_category.py`'s module docstring:
*"exact-matching an LLM-produced value against a configured label zeroes the
result when casing differs"* — and the sibling incident in vibes_bot
(`plans/260801_bot-query-vocabulary.md`), where a guessed plural matched
nothing and the bot then told users no open restaurants existed. Nothing in
this change may turn `recurrence_text` or `category` into a closed
vocabulary that a non-match falls out of.

### Corrections to earlier documents

These are measurements, not opinions, and the sibling 1.4.1 plans were
written in parallel with this one, so the corrections did not propagate.
**These figures supersede the earlier ones wherever they appear.**

- **`category` is never null in production.** `plans/260906_events-ui-v1-
  coordination.md:31-32` states *"28% of events have no category"*. Measured
  in this census: **0 of 44 occurrences have a null `category`**; 11 distinct
  values, 5 of them off-vocabulary. The 28% figure is also quoted in
  vibes_bot's 1.4.1 contract table (*"category (str|null, ~28% null)"*) and
  in mobile's D15 (*"a card with `category: null` (28% of the feed)"*) —
  both should read **"nullable by contract; 0/44 null in the 2026-09-07
  census"**. The defensive null handling both plans specify is correct and
  must stay: `category` IS nullable in the contract, `EventOccurrence` has
  always allowed it, and this round does not change that. Only the stated
  evidence was wrong.
- **`ends_at` is present on 15/44 and correct on 0/44.**
  `141-DETAIL-DEFECTS.md` D11 lists `endsAt` as *"15/44 real"*. It is 15/44
  PRESENT and **0/44 correct** — every one of the 15 is strictly before its
  own occurrence's `starts_at`.

## Current Behavior

- The extraction prompt's `kind` precedence sends any post stating a
  recurring cadence to `"event"` before the `promotion` and `menu` branches
  are ever considered, so a standing buffet and a standing happy hour are
  persisted as `post_type: 'event'`, selected by `is_selectable`, expanded
  by `expand_occurrences` into one occurrence per matching day for 21 days,
  and served.
- The projection copies `recurrence_text` and `category` from the RDS row
  byte-for-byte, so the served values carry whatever casing the model wrote,
  and `category` is frozen at whatever the vocabulary said on the day the
  row was extracted.
- The projection copies `ends_at` from the RDS row byte-for-byte onto every
  re-derived occurrence, producing an end instant weeks before its own
  start.

## Desired Behavior

1. The extraction prompt must classify a post whose announced SUBJECT is
   FOOD or a PRICE as `menu` or `promotion`, never `event`, even when it
   names days, a date or times — and must keep classifying as `event`
   every post whose announced subject is something else, including
   `karaoke`, `quiz / trivia`, `kids / family` and **every music or genre
   night**, even when the caption mentions food or a price ALONGSIDE it.
   A rule that removes the buffet but also kills "todo sábado tem
   sertanejo, chopp em dobro" is worse than the defect it fixes.
   Food-anchored events are conceded by §0 and enumerated in §1b; the
   non-food shapes above are not conceded and are gated in §5.
2. The serving projection must carry a casing-normalised `recurrence_text`,
   while RDS keeps the verbatim extraction.
3. The serving projection must carry a `category` canonicalised against the
   LIVE admin vocabulary at projection time, while RDS keeps the stored
   value.
4. The serving projection must carry, for an occurrence whose `starts_at`
   was **RE-DERIVED** from a weekday pattern, an `ends_at` derived by
   applying the source row's stored DURATION to that occurrence's own
   `starts_at` — and `null` rather than a wrong instant whenever that
   duration is not usable. For an occurrence served on the announcement's
   own stored `starts_at` it must carry the stored `ends_at` verbatim **at
   any length**, dropping only a stored end that precedes its own start.
   The decision is keyed on DERIVATION, never on `is_recurring`: a recurring
   row whose recurrence prose this repo cannot parse is served on its own
   stored dates and its stored end is correct (§3a).
5. Every substitution must be observable per outcome, computed once per
   source row, and must never abort a projection cycle.

## Implementation Approach

### 1. Extraction — make the `kind` ladder reachable (`app/api/openai_event_extraction_client.py`)

Amend `_KIND_FIELD_DOC` **only**. It is the single shared constant both
prompts interpolate, so the single-event and multi-event prompts cannot
drift — CLAUDE.md's own warning ("both extraction prompts must change
together; they have drifted before") is satisfied structurally.

Add a WHAT-IS-ANNOUNCED test in the same pre-ladder position the existing
"does this announce something attendable" test occupies, stating in
substance:

> Before applying the precedence, ask ONE question: what is this post
> ANNOUNCING?
>
> - If the answer is FOOD — a dish, a menu, a buffet, a rodízio, a
>   self-service, a lunch, a daily special ("especial do dia") — answer
>   "menu".
> - If the answer is a PRICE — a happy hour, a discount, a standing offer,
>   "chopp em dobro", a price list — answer "promotion".
>
> Answer that way even when the post states days, a date or times. A
> schedule does not turn the venue's own kitchen or its own price list into
> an event.
>
> For ANYTHING ELSE the existing precedence is unchanged and the answer is
> still "event": a show, a party, a DJ or live set, a karaoke or quiz
> night, a class or workshop, a screening, a kids' or family session, any
> named music or genre night. Food or a price mentioned ALONGSIDE one of
> those does not change the answer — "toda sexta é Lovezinho, open bar até
> meia-noite" and "todo sábado tem sertanejo, chopp em dobro" are both
> "event".

**The rule is deliberately ASYMMETRIC, and that asymmetry is the whole
design.** A food-or-price SUBJECT wins over anything mentioned alongside
it; a non-food subject wins over food or a price mentioned alongside IT.
The first half is what reliably kills the standing-offer class; the second
half is what keeps `Sambinha Downtown` (whose own caption says "HAPPY HOUR
a tarde toda"), `Lovezinho` ("promoções de combo a noite toda") and every
sertanejo-with-chopp night. Both halves are gated in §5; §1a shows the
second half covers all four protected shapes.

**Revision 3 replaced revision 2's clause under §0's operator decision.**
Revision 2 asked "is there a PROGRAMMED OCCASION alongside the food?" and
answered `event` whenever there was — three paragraphs, a tie-break, and an
18-row audit to prove the tie-break was safe. Revision 3 does not ask that
question at all: at the food/price boundary the subject decides and the
alongside-test is gone. That is the simplification, and its cost is that
food-anchored events now lose (§1b). Revision 1's own failure is still on
record and still avoided: it left `karaoke`, `quiz / trivia` and
`kids / family` unrescued because its rescue sentence enumerated only "a
performance, a competition, a class or a screening". The clause above names
those three explicitly, so that failure cannot recur even though the clause
is now shorter than revision 2's.

No other prompt field changes. The constant grows by roughly 130 tokens of
INPUT on a prompt that already carries a vision payload; output-token
budgets (`DEFAULT_MAX_COMPLETION_TOKENS = 6400`) are untouched, and the
reasoning work this adds is the same *shape* the `kind` precedence already
asks for — the constant's own comment names this exact overlap ("a risotto
special at a stated price on weekdays is simultaneously a dish, an offer and
a recurring weekly thing").

**This change is forward-only.** `plans/260826_skip-already-extracted-posts.md`
makes the scheduled path skip a post already turned into an event, so the
amended prompt reaches only new posts unless the operator deliberately
re-extracts. That is the point of §4 below, not an oversight — and it is
also why the Test Plan's false-positive signal must be a STANDING counter and not a
one-off check: a regression would otherwise appear only at new venues, on
new posts, gradually, with nothing to scroll past.

### 1a. The four shapes the concession does NOT cover, and how the rule keeps them

§0 concedes the food/price boundary and nothing else. Revision 2 audited all
18 entries of `app/models/post_category.py::DEFAULT_CATEGORY_VOCABULARY` to
prove a tie-break safe; there is no tie-break any more, so 14 of those rows
now say only "unaffected" or "conceded" and the audit has been deleted as
dead weight (§0). What remains is the set §0 says must still be protected —
and it is protected by ONE clause, the rule's closing sentence:

| protected shape | vocabulary entries | recurring caption | why the rule keeps it |
|---|---|---|---|
| karaoke | `karaoke` | "toda terça é noite de KARAOKÊ, 20h, sem couvert" | the announced subject is a karaoke night; "sem couvert" is a price mentioned alongside |
| quiz / trivia | `quiz / trivia` | "quiz da quarta, 20h — o time vencedor leva uma rodada de chopp" | the announced subject is a quiz; the free round is a prize alongside |
| kids / family | `kids / family` | "domingo é dia da criança: recreação e contação de história, 10h-14h" | the announced subject is a family session; no food or price subject at all |
| every music / genre night | `live music`, `DJ / club night`, `samba / pagode`, `forró`, `rock`, `MPB`, `jazz`, `sertanejo`, `funk`, `comedy`, `party` (11 of the 18) | "todo sábado tem sertanejo, chopp em dobro" | the announced subject is a music night; the drinks offer is alongside |

Every one of the four is kept by the same sentence — *"Food or a price
mentioned ALONGSIDE one of those does not change the answer"* — so there is
one clause to reason about and one clause to gate. §5's controls are exactly
these four shapes plus the standing-offer class the rule must kill. If a
future revision trims that sentence, all four fail at once and the eval
blocks the merge.

Two live rows sit on this seam and are pinned in §5 and in Path B0: `Sambinha
Downtown` (`evt_01KZVG66809JD1SCTQ2EJQAFJ4`, caption says "HAPPY HOUR a tarde
toda") and `Lovezinho` ("promoções de combo a noite toda"). Both name a price
inside a music-night caption. Both must stay `event`.

### 1b. What the simplified rule sacrifices — stated, not discovered

This is the cost §0 accepts. It is recorded here rather than left implicit so
that a `menu` answer on one of these shapes is read as the known trade, not
as a new defect.

**Shapes now answered `menu`/`promotion` that revision 2 kept as `event`:**

| sacrificed shape | vocabulary entry it stands in for | example caption | in today's corpus? |
|---|---|---|---|
| a recurring food festival | `food festival` | "Oktoberfest — todo sábado de outubro, chopp alemão, banda Die Kapelle, concurso de dança. Entrada R$ 20." | **see the Oktoberfest note below** |
| a guided tasting / degustação | `tasting` | "Degustação guiada de cachaças, toda quinta 19h, com o mestre alambiqueiro. R$ 45." | none |
| a food-anchored night with live music | `samba / pagode` | "FEIJOADA DO SÁBADO — todo sábado 13h com roda de samba do grupo X. Couvert R$ 15." | none |
| a food-anchored night with a screening | `sports screening` | "todo domingo jogo no telão + rodízio de petiscos, 16h" | none |
| a one-off dated dish or price post | `menu` / `promotion` (correctly) | "neste sábado tem feijoada com samba ao vivo" | none |

**The live rows this names.** A grep of all 21 events in the 2026-09-07
census for feijoada / degustação / gastro / almoço / jantar / churrasco /
rodízio returns **nothing**, so four of the five rows above have no live
example. The fifth does: **`Oktoberfest BeerDock`
(`evt_01KZVGAW45PBBRKGEGE4W6SG90`, handle `beerdock_recife`, 1 of the 44
occurrences)** is a real `food festival` by name in today's corpus. Stated
precisely, because the difference matters:

- It is **`is_recurring: false`** and it is **not re-extracted by this
  round** — the prompt fix is forward-only and Path B1 reaches only
  `ctradicao` and `downtownbeergarden_`. So it does **not** drop from the
  44→35 acceptance table, and the plan does not claim it does.
- Its caption's announced subject is a NAMED FESTIVAL, not a dish or a
  price, so the rule as written does not obviously reach it either. But §0
  has withdrawn the clause that used to guarantee it survives, so the answer
  is now the model's unassisted judgement.
- Consequence, recorded: **a re-post of Oktoberfest, or a recurring edition
  of it ("todo sábado de outubro"), may come back `menu` and never reach the
  feed.** That is the single named live casualty of this concession.

**Not sacrificed, and must not be read as sacrificed:** any caption whose
subject is not food or a price. The four shapes in §1a, and the two live
seam rows named there, are gated in §5 on exactly the same blocking terms as
the standing-offer class. The concession is a floor under the food boundary,
not a licence to drop recurring nights.

**Re-admission is a named follow-up, not a hope** — see "Follow-ups".

### 2. Projection — `recurrence_text` (new `app/services/event_recurrence_text.py`)

A new pure module, sibling to and modelled on `app/services/
event_ticket_url.py`: no I/O, no config read, unit-tested in isolation.

`normalize_recurrence_text(raw) -> (value, outcome)`:

- `None`/blank → `(None, "absent")`.
- Otherwise: trim, collapse internal whitespace, lowercase the phrase, then
  uppercase its first cased character. A token beginning with `@` is
  preserved verbatim (an Instagram handle is a case-carrying identifier).
- `outcome` is `"normalized"` when the result differs from the trimmed
  input, else `"unchanged"`.

Sentence case, not Title Case: pt-BR weekday names are correctly lowercase,
so this is the orthographically right form as well as the collapsing one.
Applied to the live corpus it takes 9 distinct values to 7 —
`'Toda QUARTA'`/`'toda quarta'` → `'Toda quarta'`, `'TODOS OS SÁBADOS'`/
`'todos os sábados'` → `'Todos os sábados'` — while `'Aos domingos'` and
`'Todos os domingos'` correctly stay apart, `'Toda sexta, às 21 horas'` is
untouched, and `'Terça a Domingo'` → `'Terça a domingo'`.

**Casing only. This is not a vocabulary and nothing exact-matches against
it** — the `post_category.py` constraint above. A phrase the rule does not
improve simply passes through; there is no non-match to fall out of.

Risk accepted and measured: lowercasing would also lowercase a proper noun
inside a recurrence phrase ("toda quarta no Bar do Zé"). Zero of the 9 live
values contain one. RDS keeps the raw text, so the rule can be revised later
without a migration or a re-extraction.

### 3. Projection — `ends_at`, and `category` (`app/services/redis_projection_service.py`)

All three substitutions computed **once per source row, outside the
occurrence loop**, alongside `classify_ticket_url`, each incrementing its
own labelled counter exactly once per row:

- `recurrence_text` — `normalize_recurrence_text(row["recurrence_text"])`.
- `category` — `canonicalize_category(row["category"], vocabulary)`, where
  `vocabulary` comes from `load_post_category_vocabulary(self.redis_only_
  dao.client)` read **once per cycle**, next to the existing
  `load_hide_promoter_events` call, and passed down. An unreadable/invalid
  config already falls back to `DEFAULT_CATEGORY_VOCABULARY` inside that
  loader; a value that matches nothing is passed through unchanged, never
  dropped. This makes an operator's vocabulary edit reach the app on the
  next 2-minute cycle instead of requiring a re-extraction of every row.
- `ends_at` — see §3a. This is the only one of the three that varies per
  occurrence, so the per-row work is the stored PAIR and the per-occurrence
  work is one comparison plus at most one addition.

### 3a. `ends_at` — keyed on DERIVATION, never on `is_recurring`

**Revision 1 keyed this decision on `is_recurring`, and that was wrong.**
`app/services/event_occurrences.py:161-163`: a row with `is_recurring: true`
whose `recurrence_text` yields no weekday set falls through to
`_single_occurrence(event_id, starts_at)` — ONE occurrence whose `starts_at`
**is** the stored `starts_at` and whose `occurrence_id` is the bare
`event_id`. For that occurrence the stored `ends_at` is simply correct, and
revision 1's `is_recurring`-keyed table would have nulled a genuine
multi-day interval ("de quinta a domingo") as `dropped_implausible`. That is
exactly the deletion vibes_bot's own 1.4.1 plan REFUSED to make at serve
time (`plans/260907_events-ends-at-invariant.md`, Non-goals: "applying it at
serve time would delete the genuine end of a multi-day non-recurring
festival, whose stored `ends_at` cs-server carries verbatim by design").
**vibes_bot is right and cs-server was wrong**; the two repos are aligned
below, and the contract sentence in Acceptance Criteria is written so
vibes_bot can hold its guard unchanged.

The correct discriminator is not "was the row recurring" but **"was this
occurrence's `starts_at` RE-DERIVED"** — because that is the only case in
which the stored `ends_at`'s DATE was thrown away. `expand_occurrences`
answers it unambiguously: a re-derived occurrence's id is
`f"{event_id}_{day.isoformat()}"`, a single-shape occurrence's id is the
bare `event_id`. So the test is `occ.occurrence_id != occ.event_id`, read
off the `Occurrence` the projector already has in hand. (`occ.starts_at !=
row["starts_at"]` is equivalent but weaker — it depends on tz-awareness
normalisation and would misread a re-derived occurrence that happens to land
on the stored date.)

The logic moves into a new pure module `app/services/event_occurrence_end.py`
— sibling to `event_ticket_url.py` and `event_recurrence_text.py`, no I/O,
no config — so the six-way table is unit-testable as a pure-input matrix
rather than only through the projector:

`resolve_occurrence_end(stored_start, stored_end, occurrence_start, *, derived) -> (value, outcome)`

| branch | condition | projected `ends_at` | outcome |
|---|---|---|---|
| any | `stored_end` is None/absent | `None` | `absent` |
| **carried** (`occ.occurrence_id == occ.event_id`) | `stored_end < stored_start` | `None` | `dropped_inverted` |
| **carried** | anything else, **any length** | `stored_end` **verbatim** | `carried` |
| **derived** (`occ.occurrence_id != occ.event_id`) | `duration < 0` | `None` | `dropped_inverted` |
| **derived** | `duration == 0` | `None` | `dropped_zero` |
| **derived** | `0 < duration <= MAX_DERIVED_DURATION` (24h) | `occurrence_start + duration` | `derived` |
| **derived** | `duration > MAX_DERIVED_DURATION` | `None` | `dropped_implausible` |

`MAX_DERIVED_DURATION` is a module constant of 24h — the same "pure module,
frozen constant, never a setting" posture as `NIGHTLIFE_CUTOFF_HOUR` and
`PAST_GRACE`.

Why the carried branch has **no length bound at all**: on that branch the
occurrence IS the stored announcement, so the stored `ends_at` is the end
of that exact interval and a three-day festival's end is real data, not a
stale instant. Applying a 24h bound there is precisely the multi-day
deletion this repo and vibes_bot both refuse. The bound belongs only where
the stored end's DATE has been discarded — the derived branch — and there it
is not a plausibility heuristic about events but a statement about what
survives the discard: a >24h "duration" on a weekly night is
indistinguishable from the announcement's original absolute end, which is
the exact defect C3 exists to fix.

Why `dropped_zero` is split out from `dropped_inverted` (revision 1 folded
them): a zero-length interval is not an inversion, and merging them makes
the counter unreadable as the data-defect signal it is for. They also do
different things — a zero-length interval is **carried** on the carried
branch (matching vibes_bot, whose plan states "Equality is permitted by the
upstream invariant, so a zero-length interval is served, not dropped") and
**dropped** on the derived branch, where deriving it would produce an end
equal to the start it was derived from, which tells a reader nothing.

Also on record: a null `stored_start` is **unreachable here** —
`event_occurrences.py:154-155` returns no occurrences at all when
`starts_at` is None, so such a row never enters the payload loop.
`resolve_occurrence_end` still returns `(None, "absent")` for it rather than
raising, because a pure function must be total; the plan simply does not
claim that outcome will ever be observed.

Projecting `None` rather than a wrong instant is the same posture
`time_known: false` already takes for a defaulted midnight: suppress a
misleading value rather than serve it.

**What today's production will read, and how confident that is.** The public
API serves each occurrence's RE-DERIVED `starts_at`, never the row's stored
one, so the 44-occurrence census cannot measure a stored duration directly
and this plan does not claim it did. It is a strong inference: every one of
the 15 stored `ends_at` values falls on a weekday its own `recurrence_text`
matches — `Terça a Domingo` → `2026-08-04` (Tuesday), `Toda QUARTA` →
`2026-08-12` and `2026-09-02` (both Wednesdays), `todos os sábados` →
`2026-09-05` (Saturday) — which is what a stored `starts_at` on the SAME
calendar day implies, and the resulting durations are 3.5h, 1h and 1.5h
against each occurrence's own time-of-day (which `expand_occurrences` copies
from the stored `starts_at`). The 15 affected occurrences belong to **4
source rows** (the buffet, both `Aula de FORRÓ` rows and `Residência drag da
Metrópole`), and the counter is per row, so the expected reading is
`derived: 4` / `absent: 17` — 21 selected rows in total. It is
**verified, not asserted**, by the post-deploy metrics check in the Test Plan, which
reads the split rather than predicting it — if any of those rows turns out
to store a negative or over-24h duration it will read `dropped_inverted` or
`dropped_implausible` instead, and the served value is `null` either way,
which is correct in both cases.

No feature flag. `ticket_url` normalisation shipped unflagged in the same
function for the same reason: the projector re-asserts every 2 minutes, so a
bad rule is visible within minutes and reverted by a deploy, and a flag
would add a second code path to an already-branchy loop.

### 4. Removal path for rows already projected — operator-gated, and safe by construction

**Yes, it is operator-gated**, and it must be: an automatic drop is the
blocklist this plan rejects, wearing a different hat.

**Revision 1 sequenced the write-path re-extraction FIRST, as the proof,
and that sequencing was unsafe.** The reasoning behind it was right — a real
caption through the real model beats any fixture — but as specified the
proof had write authority over the very rows it was measuring, and the
precondition it demanded (`operator_edited_fields: null`) is exactly the
state in which that authority is unrestricted. Concretely:
`downtownbeergarden_` is the home of BOTH the happy-hour offender and
`Sambinha Downtown` (`evt_01KZVG66809JD1SCTQ2EJQAFJ4`), a genuine recurring
night §5 pins as must-stay-`event`, whose own description says "HAPPY HOUR a
tarde toda" — the single caption in the corpus most likely to trip a
standing-offer test. A misfire would have deprojected it within one 2-minute
cycle. And per the corrected Evidence bullet above, the damage is not
limited to `post_type`: on a non-confirmed row the re-extraction rewrites
every content field and resets `status`.

The proof is therefore redesigned so it **cannot write at all**, and the
write-path run is demoted to optional and gated behind an explicit freeze.

#### Step 0 — enumerate the blast radius (read-only, before anything else)

`GET /admin/events?venue_id=ven_49496f596d643751764b4d52637771597350507948306e4a496843`
(Cachaçaria Tradição, `ctradicao`) and
`GET /admin/events?venue_id=ven_3870374f6b554444626c61526377715a38634c624662354a496843`
(Downtown Beer Garden, `downtownbeergarden_`), **with no status and no date
filter** — the serving census cannot answer this, because
`mode="handles"` re-extraction is bounded by the per-venue post cap (20),
not by the 70-day serving window, so it reaches rows that are outside the
window, `pending_review`, or already past. Record `event_id`, `title`,
`status`, `post_type` and `operator_edited_fields` for every row returned.
That list, not the census, is the blast radius.

**The enumeration is deliberately unfiltered; the ACTION set is not.** The
query returns every status — `list_events(venue_id, status=None)` adds no
status clause (`admin_events_router.py:349-359` → `rds_venue_store.py:
1304-1315`), so `pending_review`, `rejected`, `extraction_failed` and
superseded rows all come back. Reading them is the point. **Writing to them
is not**, and Path A below is scoped accordingly: confirming a
`pending_review` or `rejected` row would move its `status` into
`SELECTABLE_STATUSES = ("accepted", "confirmed")`
(`event_projection_selection.py:32`) and PROJECT a row that is not in the
feed today — inflating the feed through the one transition this plan records
as **one-way** (there is no un-confirm route and `EventPatch` does not accept
`status`, `admin_events_router.py:327-347`), and breaking this round's own
acceptance table in the direction the table has no room for. Split the Step-0
list in two before touching anything:

| Step-0 row | Path A does |
|---|---|
| `status` is `accepted` or `confirmed` **and** `post_type == "event"` **and** `venue_id` is present **and** `superseded_by` is None — i.e. **already selectable** | PATCH + confirm (below) |
| anything else — `pending_review`, `rejected`, `extraction_failed`, superseded, or already `menu`/`promotion`/`food`/`other` | **nothing. Left exactly as found.** |

The two offenders are in the first group today (both `accepted`, both
`post_type: "event"`), so the correction still reaches them. Every row in
the second group is already outside the projection and stays outside it;
this round does not review the queue.

The three rows already known from the census, with their expected
post-round `post_type`:

| event_id | handle | title | today | expected after |
|---|---|---|---|---|
| `evt_01M1Y1W3V4HNXDQ7R2KN20B5JM` | ctradicao | Buffet de Terça a Domingo | `event` | **`menu`** |
| `evt_01M0XTA552MYQMA8P83AB01HN5` | downtownbeergarden_ | QUINTA É DIA DE HAPPY HOUR | `event` | **`promotion`** |
| `evt_01KZVG66809JD1SCTQ2EJQAFJ4` | downtownbeergarden_ | Sambinha Downtown | `event` | **`event` (unchanged)** |

Occurrence effect: `ctradicao` goes 6 → **0** occurrences and leaves the
feed entirely; `downtownbeergarden_` goes 6 → **3** (Sambinha only); the
city total goes 44 → **35**. Any other value, at any row, is a failure of
this round and is rolled back per "Rollback" below.

#### Path A — operator correction (the guarantee). Run FIRST.

For each row **in the first Step-0 group only — the ALREADY-SELECTABLE rows**
— `PATCH /admin/events/{event_id}` with its CORRECT `post_type`:
`{"post_type": "menu"}` for the buffet, `{"post_type": "promotion"}` for the
happy hour, and `{"post_type": "event"}` for every genuine already-selectable
row at those two handles including Sambinha — sending **only** `post_type`
(the router records exactly the keys sent, so this freezes that one field and
nothing else). Then `POST /admin/events/{event_id}/confirm` on each, which is
what actually makes `operator_edited_fields` binding (see the corrected
Evidence bullet).

**Re-read `status` immediately before each PATCH and skip any row that is not
`accepted`/`confirmed`.** This is the guard, not a formality: the confirm
that follows is one-way through the API, so a `pending_review` row confirmed
by mistake cannot be put back without a direct RDS write. Confirm changes
selectability; that is why nothing outside the already-selectable set may be
confirmed.

Net effect on selectability, which is the reason the scope is safe:

- the two offenders leave the projection (their `post_type` is no longer
  `"event"`; `confirmed` does not rescue them, because `is_selectable` tests
  `post_type` first — `event_projection_selection.py:67`);
- every other row Path A touches was **already selectable and stays
  selectable**, so it contributes exactly the occurrences it contributes
  today. Path A can therefore only SUBTRACT from the feed, never add — which
  is what makes the 44 → 35 acceptance table a closed prediction rather than
  a lower bound;
- nothing outside the already-selectable set is touched, so no row enters the
  feed that is not in it today.

The two offenders deproject within one 2-minute cycle via `is_selectable`.
The genuine rows stay projected — `confirmed` is a selectable status — and
are now immune to a re-extraction misfire on `post_type`, while their
titles, dates and lineups keep refreshing normally.

Nothing is deleted. Every row stays in RDS, correctly typed and inspectable.

#### Path B0 — the production proof, with NO write path. Run after §1 deploys.

The proof does not need the write path; it needs the real captions and the
real model. Both are available without persisting anything.

Run `scripts/eval_kind_on_captions.py` (the Test Plan's fixture-builder, reused with a
`--handles` argument) inside the production container over SSM: for every
archived post at `ctradicao` and `downtownbeergarden_` within the extraction
cap, read the caption out of the archive
(`post_item_source.cover_photo_key` → `event_source_media.derive_run_partition`
→ `retrieved/…/info/_manifest.json` → `event_extraction_service.
EventPostSource._bucket_entries`' own `caption` field), call
`OpenAIEventExtractionClient` with the deployed prompt, and print
`shortcode → kind` to stdout. **It constructs no DAO, calls no
`reconcile_post_events`, and writes nothing** — it has no write path to
misfire through. It is read-only against S3 and RDS and additive-only
against the OpenAI bill (≤40 calls).

Pass condition: the buffet's post answers `menu`, the happy hour's answers
`promotion`, and **every other post at those two handles that is a genuine
event still answers `event`** — Sambinha's above all. That last clause is
the half revision 1 could not check at all, and it is the whole point.

#### Path B1 — the write-path re-extraction. OPTIONAL, and only after A and B0.

If the operator wants the rows re-typed by the model rather than by hand,
`POST /admin/trigger/event_extraction` with `{"eligibility": {"mode":
"handles", "handles": ["ctradicao", "downtownbeergarden_"]}}`.

**Preconditions, all three:** Path A is complete for EVERY row in the Step-0
list (patched AND confirmed); B0 is green; and a read-back confirms
`status: "confirmed"` and `post_type` in `operator_edited_fields` on every
one of them. With those in place the confirmed branch runs, the
re-extraction may write only `raw_extraction` / `last_seen_at` /
`source_event_key` / `source_handle` / `source_shortcode` plus unedited
fields, and a model that now disagrees sets
`REVIEW_REASON_DIVERGES_FROM_CONFIRMED` rather than overwriting anything.

Read the model's fresh answer out of `GET /admin/events/{event_id}` →
`sources[].raw_extraction["kind"]`, which is written on the confirmed branch
too (`event_reconciliation.py:753`) and stores the model's word verbatim
(`event_kind.normalize_kind`). B1 therefore yields the same evidence B0
does, at strictly higher risk, which is why it is optional.

#### Rollback

- Path A, per row: `PATCH /admin/events/{event_id}` with the previous
  `post_type` recorded in Step 0. Deprojection/reprojection follows on the
  next 2-minute cycle. `operator_edited_fields` still lists `post_type`
  after the revert — harmless, and the honest record that a human decided
  this field.
- Confirming is one-way through the API (no un-confirm route, and
  `EventPatch` does not accept `status`). A row that must be returned to
  `accepted` — or to `pending_review` — needs a direct RDS update, which is
  out of scope for this round. **This is why Path A confirms only rows that
  were ALREADY selectable**, and never a `pending_review`, `rejected`,
  `extraction_failed` or superseded row, at these two handles or anywhere
  else. There is no rollback for a wrongly-confirmed queue row through the
  admin API; the Step-0 scope split is the only protection, so it is checked
  per row at write time, not once at the start.
- If the post-round sweep shows the city total ABOVE 35, or a NEW `event_id`
  appearing at `ctradicao`/`downtownbeergarden_` that was not in the
  pre-round census, a queue row was confirmed in error. PATCH its
  `post_type` to `menu` to remove it from the feed immediately, then raise
  the RDS status repair as a separate task — the row's `status` cannot be
  restored through the API.
- B0 has nothing to roll back.
- B1: any field it moved on an unedited row is restored by re-running B1
  after the prompt is fixed, or by a PATCH. `post_type` cannot have moved if
  the preconditions held.

The operator dispatches every step. No agent dispatches any of them — the
same posture the 1.4.0 coordination plan takes for the bairro sweep and the
EAS release.

### 5. The boundaries the rule must not cross — and the one §0 lets it

#### What the LIVE corpus can pin

`Aula de FORRÓ na Sala de Reboco` (two rows, 6 of 44 occurrences, `category`
`workshop`/`forró`, `'Mensalidade: R$ 100'`, a named teacher, stated hours
"19h iniciados, 20h iniciantes") is a recurring **class**. It repeats, it is
paid, and it is not a party — the closest thing in the corpus to the class
this change removes. It **must stay `event`**: the announced subject is a
forró class, and the `Mensalidade` is a price named ALONGSIDE it, which
under §1's closing sentence does not change the answer.

Also pinned to stay `event`: `Sambinha Downtown` (whose own description
mentions "HAPPY HOUR a tarde toda"), `Residência drag da Metrópole`,
`Lovezinho` (description mentions "promoções de combo a noite toda"),
`FORRÓ DOS PAIS`, `Drag Ataque`, `Baile Dançante`. Three of those name a
promotion inside an event caption — §1's closing sentence ("food or a price
mentioned ALONGSIDE one of those does not change the answer") is the clause
that keeps them, and it is the clause §5's S6/S14 gate.

#### What the live corpus CANNOT pin, and the synthetic controls that do

The 21-event corpus is exactly today's live feed, so the eval built from it
is a *regression* test, not a *boundary* test. Two boundaries it cannot
exercise, for opposite reasons:

- **The food boundary** — a grep of the whole corpus for feijoada /
  degustação / gastro / almoço / jantar / churrasco / rodízio returns
  nothing. Revision 2 covered this with eight blocking food-anchored
  controls. **§0 has conceded this boundary, so those controls are deleted**
  (S1, S2, S3, S8) rather than left as dead weight; the shapes they stood
  for are named in §1b and re-admitting them is a named follow-up. Nothing
  in the gate defends them any more, and that is deliberate.
- **The protected boundary** — the corpus contains no karaoke, quiz or
  kids/family caption either, and only two captions (`Sambinha Downtown`,
  `Lovezinho`) exercise the music-night-with-a-price seam. Those are the
  shapes §0 says must still survive, so these controls STAY, and stay
  blocking.

**Ten synthetic captions** therefore join the eval — five that must survive
and five that must be suppressed — labelled SYNTHETIC in the fixture so
nobody mistakes them for production data, and **blocking on exactly the same
terms as the live rows**. The surviving five are the four protected shapes
from §1a plus the seam the operator named by hand.

| # | caption (abridged) | shape | expected `kind` |
|---|---|---|---|
| S4 | "Toda terça é noite de KARAOKÊ, 20h, inscrição na hora, sem couvert." | karaoke | **event** |
| S5 | "Domingo é dia da criança: recreação, contação de história e brinquedoteca, das 10h às 14h, todo domingo." | kids / family | **event** |
| S6 | "TODA SEXTA É LOVEZINHO 💚 open bar de caipirinha até meia-noite, DJ Bibi no comando." | party, with a drinks offer alongside | **event** |
| S7 | "Quiz da Quarta, 20h, equipes de até 5 pessoas — o time vencedor leva uma rodada de chopp." | quiz / trivia, with a drinks prize alongside | **event** |
| S14 | "TODO SÁBADO TEM SERTANEJO 🤠 com a dupla Léo & Rafa. Chopp em dobro até as 22h." | sertanejo, with a price alongside | **event** |
| S9 | "Buffet de Terça a Domingo — a melhor hora do dia 🍛" (the live offender, verbatim) | standing food offer | **menu** |
| S10 | "QUINTA É DIA DE HAPPY HOUR. Promoções em dobro: caldinhos x2, gin tônica x2… Chopp por R$ 6,99." (the live offender, verbatim) | standing price offer | **promotion** |
| S11 | "Especial do dia: risoto de camarão, de segunda a sexta no almoço, R$ 39,90." | daily special | **menu** |
| S12 | "De segunda a quinta, chopp em dobro o dia inteiro." | standing discount, no occasion at all | **promotion** |
| S13 | "Nosso rodízio de pizza roda todas as noites, das 19h às 23h." | the kitchen's own hours | **menu** |

**S12/S14 and S6/S10 are the discriminating pairs, and they are the ones
that matter now.** S12 and S14 both offer "chopp em dobro" on a recurring
cadence; only S14 announces a music night. A rule that answers `promotion`
to both has learned "chopp em dobro" and is eating genre nights — the exact
defect §0 forbids. S6 and S10 are the same test at the seam the live corpus
does exercise (`Sambinha Downtown`, `Lovezinho`): a drinks offer INSIDE an
event caption still loses to the announced subject. S11 is the
`"Especial do dia"` regression named in the 1.4.1 brief.

Revision 1's five unrescued vocabulary entries were `karaoke`,
`quiz / trivia`, `kids / family`, `food festival` and `tasting`. Three of
them are still gated here (S4, S7, S5); the other two are conceded by §0 and
recorded in §1b. A future revision that trims §1's closing sentence fails
S4, S5, S6, S7 and S14 at once and cannot merge.

**`Oktoberfest BeerDock` is in the fixture but is NOT blocking.** It is the
one named live casualty of the concession (§1b), and §0 accepts either
answer for it, so gating on it would re-erect the boundary the operator
removed. It is scored and its answer is **RECORDED in the merge note**, so
the re-admission follow-up starts from a measurement rather than a guess.

## The events-filter question — what the system of record can offer

Asked: should the projection expose anything new so mobile can build a basic
events filter without client-side bucketing? Mobile carries an
operator-approved thin-presentation exception for genre shelves and a filter
would widen it.

**Yes — but one of the four things on offer, and not the one that sounds
biggest.**

**NO to a closed genre enum in cs-server.** Mapping free-text `category`
into a fixed `event_genre` would be the `post_category.py` casing trap at
scale: with 21 events across 11 distinct categories, most buckets are n=1 or
n=2, and 5 of the 11 are off-vocabulary. A server-side enum either drops
those five real values or is as near-degenerate as the client bucketing it
replaces. The vocabulary must keep growing from evidence, which is exactly
why it is admin config and free text today.

**NO to a category index or a `category=` serving parameter in 1.4.1.** It
is a three-repo round this plan cannot sequence, and with 21 events a
server-side filter and a client-side one return identical results — it buys
correctness for a catalog size we do not have. **Offered as a follow-up with
the key shape pinned now** so it is a decision already made when the catalog
justifies it:

- `events_category_v1:<city_slug>:<category_key>` — ZSET, score =
  `starts_at` epoch seconds UTC, same write/prune lifecycle as
  `events_index_v1:<city_slug>`. `category_key` = the canonical category,
  casefolded + NFKD accent-stripped. Sized against production: 11 keys,
  ~44 members total, against a Redis holding 20 MB with no `maxmemory`
  ceiling.
- `events_city_categories_v1:<city_slug>` — plain SET of the canonical
  category LABELS currently indexed for that city, re-asserted every cycle,
  no TTL. This is the piece that makes a filter genuinely *basic*: mobile
  renders the chips the server says exist and can never offer a bucket that
  returns nothing. It is the same shape, and for the same reason, as
  `events_known_cities_v1`, and it inherits that key's rule — **an empty
  read means Redis had a bad moment, never "no categories exist"; downstream
  fails OPEN.**

**YES, in 1.4.1, to canonicalising `category` in the projection** (§3). This
is the half mobile cannot do for itself and the half that makes *either*
filter implementation correct: today the served `category` is frozen at
whatever the vocabulary said when the row was extracted, so an operator who
folds a stray spelling into the vocabulary sees no change until every
affected post is re-extracted. Canonicalising at projection time makes an
admin-config edit reach the app on the next 2-minute cycle. It costs one
function call in a path that already reads admin config once per cycle, adds
no field, and — decisively — **reaches the released 1.4.0 binaries, which
cannot be updated to re-map anything client-side.**

**YES, already delivered by C1 and C2, to the two things that would break a
filter worst.** A `buffet` chip and a `happy hour` chip in a nightlife
filter is the same defect as the buffet in the feed; and a filter that
buckets on `recurrence_text` or displays it as a facet would today show
`Toda QUARTA` and `toda quarta` as two chips.

Net: mobile's exception is not widened by this change, and the follow-up
above is what retires it.

## Data, Config, And API Impact

- **RDS:** none. No migration, no schema change, no bulk UPDATE. RDS keeps
  the verbatim extraction for every field this change touches.
- **Redis key shapes:** unchanged. `event_occurrence_v1:<occurrence_id>`,
  `events_index_v1:<city_slug>`, `events_venue_v1:<venue_id>`,
  `events_known_cities_v1` — no key added, removed or re-scored.
- **`EventOccurrence` field set:** unchanged — nothing added, removed,
  renamed or retyped. **Three fields change VALUE semantics only**:
  `recurrence_text`, `category`, `ends_at`. See the contract statement in
  Acceptance Criteria.
- **vibes_bot:** no change required and none requested. Every field keeps
  its name, type and nullability; `ends_at` was already
  `Optional[datetime]` and mobile already treats it as optional (it is
  currently unrendered — `141-DETAIL-DEFECTS.md` D11).
- **Config:** no new setting, no new admin-config key, no feature flag.
  `admin_config:post_category_vocabulary` gains a second reader (the
  projector); its shape and validator are untouched.
- **Prompt:** `_KIND_FIELD_DOC` grows by one question, two bullets and two
  closing sentences (~130 input tokens — revision 2's clause was ~260, and
  §0 halved it). Both prompts interpolate it, so both change together by
  construction. No output-token budget change.
- **New files, all additive:** `app/services/event_recurrence_text.py` and
  `app/services/event_occurrence_end.py` (pure, no I/O, no config) and
  `scripts/eval_kind_on_captions.py` (an offline read-only tool: it builds
  the caption fixture and runs Path B0; it constructs no DAO and calls no
  reconciler, so it has no write path). Nothing existing is renamed or
  removed.
- **Metrics:** four new counters (see Error Handling And Observability), all
  with closed label sets. `EVENTS_PROJECTION_ENDS_AT_TOTAL` carries six
  outcome labels, not five — `dropped_zero` is split from
  `dropped_inverted`.

## Error Handling And Observability

Four new counters. Three mirror `EVENTS_PROJECTION_TICKET_URL_TOTAL`'s
shape (closed `outcome` label set, zero-filled at import so an absent label
is real evidence a path never ran — see the `/metrics` diagnostic that
depends on that property) and are incremented **once per source row**; the
fourth lives on the extraction path, because that is the only place the
suppression this plan introduces is ever visible.

- `EVENTS_PROJECTION_RECURRENCE_TEXT_TOTAL{outcome}` —
  `normalized|unchanged|absent`.
- `EVENTS_PROJECTION_CATEGORY_TOTAL{outcome}` —
  `canonicalized|unchanged|off_vocabulary|absent`, defined the same way
  `recurrence_text`'s three labels are (**N3**: revision 2 declared
  `unchanged` without defining it and without any scenario or unit test
  reaching it, in a plan whose whole observability posture is that a
  zero-filled label's presence is the evidence — an undefined, unexercised
  label is a claim nobody checked):
  - `canonicalized` — the stored value matched a vocabulary entry and the
    projected spelling DIFFERS from the stored one (`'forró'` → `"Forró"`).
  - `unchanged` — the stored value matched a vocabulary entry and was
    ALREADY spelled exactly as the vocabulary spells it (`'Forró'` →
    `"Forró"`). This is the label that says the operator's vocabulary and
    the extraction agree; a fleet where it is 0 while `canonicalized` is
    high means every stored spelling is being rewritten, which is worth
    knowing.
  - `off_vocabulary` — no entry matched; the value is passed through
    unchanged, never dropped.
  - `absent` — null or blank.

  Exercised by a BDD Examples row and by the projector unit test (Test
  Plan), so the label is evidence rather than decoration. This is also the
  standing monitor for the C1 class: a new `buffet`-shaped category appearing in
  production is visible here without waiting for an operator to scroll the
  app. Label cardinality is bounded by the closed outcome set — the raw
  category is deliberately NOT a label (the extraction-time counter
  `POST_CATEGORY_OFF_VOCABULARY_TOTAL` already carries capped raw values,
  and duplicating that at projection time would be a second cardinality
  surface for the same question).
- `EVENTS_PROJECTION_ENDS_AT_TOTAL{outcome}` —
  `carried|derived|dropped_inverted|dropped_zero|dropped_implausible|absent`
  (six labels; §3a explains why `dropped_zero` is split out from
  `dropped_inverted` — a zero-length interval is not an inversion, and
  folding them makes the counter unreadable as the data-defect signal it
  exists to be). Counted once per SOURCE ROW, on the branch that row's
  occurrences take. Expected reading today: `derived: 4` rows / `absent: 17`
  rows (the 15 affected occurrences belong to 4 source rows) — see §3a for
  why that is a well-founded inference rather than a measured fact, and why
  a `dropped_*` reading instead would also be a correct outcome.

- **`EVENT_EXTRACTION_SUPPRESSED_RECURRING_TOTAL{kind}`** — the
  false-positive signal for the §1 rule, on the EXTRACTION path
  (`event_extraction_service`, in the same loop that already reads `kind`
  and `category`, before any date/confidence filtering so no branch can hide
  a suppression). Incremented once per parsed item whose `kind` is
  `menu`/`promotion`/`food`/`other` AND whose parsed answer states a
  recurring cadence (`is_recurring` true, or a non-blank `recurrence_text` —
  both are parsed for every item regardless of kind,
  `openai_event_extraction_client.py:523-524`). `kind` is the only label and
  it is a closed four-value set, so there is no cardinality surface.

  **This is the only place a suppression can be seen.** A post the rule sends
  to `menu` is never selected by `is_selectable`, so it is never projected,
  so `EVENTS_PROJECTION_CATEGORY_TOTAL` and every other projection counter
  are structurally blind to it — a false positive is counted nowhere else,
  produces no row an operator would scroll past, and (because the prompt fix
  is forward-only) shows up only on NEW posts at NEW venues, gradually.
  Watch rule: today's steady state is roughly the two known offenders' posts
  per crawl of those two handles. A step change, or a non-zero reading at a
  venue that has never had one, means the rule is eating real recurring
  nights and is the trigger to revert the prompt paragraph — which is a
  deploy, with no data to repair, because nothing was deleted.

Also: `load_post_category_vocabulary`'s existing fallback logging covers the
unreadable-config path; a fallback must not fail the cycle. Every new
computation is pure and total (no exception path of its own), and all three
sit inside the existing per-event `try/except` that already degrades a bad
row to "waits for next cycle" and counts
`EVENTS_PROJECTION_ERRORS_TOTAL{stage="event"}`.

No new log line per row — the projector already logs a per-cycle summary,
and a per-row line at 44 rows × 30 cycles/hour is noise.

## Test Plan

Feature file: `tests/bdd/persistence/events-non-event-and-recurrence-normalisation.feature`

Domain rationale: the observable outcome of all three defects is what the
serving projection writes to Redis, which is `persistence/`. This mirrors
`tests/bdd/persistence/events-venue-bairro-and-ticket-url.feature`, which
covered a projection value-semantics change that likewise originated
upstream (in address enrichment). The extraction-prompt half is asserted by
pytest and by the live eval below, never by BDD — CLAUDE.md forbids a live
OpenAI dependency in BDD.

Scenarios:

- **A standing service offering is never selected for the projection.** A
  row with `post_type: "menu"` and `is_recurring: true` whose weekday
  pattern covers the horizon contributes zero occurrences to
  `events_index_v1:recife`, and no `event_occurrence_v1:*` key exists for
  it. (The C1 outcome, expressed at the boundary the projection owns.)
- **A `food festival` row is still projected.** A row with
  `post_type: "event"` and `category: "food festival"`, recurring, still
  reaches `events_index_v1:recife`. **This scenario gets STRONGER under §0,
  not weaker.** The extraction rule now suppresses food-anchored captions,
  so the only way such a row exists is that an operator PATCHed
  `post_type: "event"` onto it — the re-admission path §0 promises is
  reversible. The projection must therefore never filter on category:
  `food festival` and `tasting` are first-class entries in this repo's own
  `DEFAULT_CATEGORY_VOCABULARY`, a category blocklist here would delete the
  operator's own correction, and §0's trade would stop being reversible.
- **An operator re-admitting a suppressed row re-projects it.** A row
  currently `post_type: "menu"` with no occurrences, PATCHed to
  `post_type: "event"`, reaches `events_index_v1:recife` on the next cycle
  with its occurrences restored. This is the mechanical proof that §0's
  concession is reversible per row without a migration, a re-extraction or
  a deploy — the property the "Follow-ups" re-admission plan depends on.
- **A recurring class with a paid enrolment is still projected.**
  `category: "workshop"`, recurring weekly — the `Aula de FORRÓ` shape.
- **An operator reclassification deprojects every occurrence of the event.**
  A recurring row already projected as 6 occurrences, PATCHed to
  `post_type: "menu"`, leaves zero occurrence keys and zero members in both
  the city and venue indexes after the next cycle — and the RDS row still
  exists.
- **A reclassified row is not resurrected by a later cycle.** Re-running the
  projection over the same store leaves it absent.
- **`recurrence_text` is casing-normalised in the projection.** Rows
  carrying `'Toda QUARTA'` and `'toda quarta'` both project
  `recurrence_text: "Toda quarta"`; `'TODOS OS SÁBADOS'` and
  `'todos os sábados'` both project `"Todos os sábados"`.
- **Phrasing differences are preserved.** `'Aos domingos'` and
  `'TODOS OS DOMINGOS'` project as two DIFFERENT values
  (`"Aos domingos"`, `"Todos os domingos"`) — normalisation folds casing,
  never meaning.
- **A recurrence phrase needing no change is passed through untouched.**
  `'Toda sexta, às 21 horas'` projects verbatim.
- **RDS keeps the verbatim extraction.** After a projection cycle, the
  source row still reads `'Toda QUARTA'`.
- **`category` is canonicalised against the LIVE admin vocabulary.** With
  `admin_config:post_category_vocabulary` holding `"Forró"`, a row stored as
  `'forró'` projects `category: "Forró"`; with the key absent, the shipped
  default spelling is projected instead.
- **A category already spelled as the vocabulary spells it reports
  `unchanged`.** With the vocabulary holding `"Forró"`, a row stored as
  `'Forró'` projects `category: "Forró"` and the category counter records
  `unchanged`, not `canonicalized` (**N3** — the label is otherwise declared
  but never reached).
- **An off-vocabulary category is passed through, never dropped.** A row
  stored as `'brega'` projects `category: "brega"` with the vocabulary
  unchanged.
- **An unreadable vocabulary config does not fail the cycle.** With the
  admin-config read raising, every occurrence is still written and
  `category` falls back to the shipped defaults.
- **A recurring occurrence's `ends_at` carries the source duration onto its
  own date.** A row `starts_at 22:00`/`ends_at 23:00` recurring weekly
  projects, for each occurrence, `ends_at == starts_at + 1h` — and never a
  date earlier than that occurrence's `starts_at`.
- **An inverted stored pair projects `ends_at: null`.** A row whose stored
  `ends_at` precedes its stored `starts_at` projects null rather than a
  guessed instant.
- **An implausible duration projects `ends_at: null`.** A stored duration
  over 24h yields null.
- **A non-recurring row's `ends_at` is carried verbatim.** Unchanged
  behaviour, pinned so the fix cannot leak into it.
- **A recurring row whose recurrence text cannot be parsed carries its
  stored `ends_at` verbatim.** `is_recurring: true`, `recurrence_text:
  "toda semana"` (prose this repo deliberately does not parse), so
  `expand_occurrences` yields the single-occurrence shape on the stored
  `starts_at` — the stored end is served unchanged, NOT re-derived and NOT
  dropped. This is the F04 branch; revision 1 would have nulled it.
- **A multi-day interval on the carried branch survives the 24h bound.**
  A row starting Thursday 18:00 and ending Sunday 04:00 — `is_recurring:
  true` with unparseable recurrence text — projects its stored end
  verbatim, 82 hours out. The bound must not reach this branch: this is the
  deletion vibes_bot refused at serve time.
- **A zero-length stored interval is carried, not dropped, on the carried
  branch.** Equality is permitted by the contract and vibes_bot serves it.
- **An inverted stored pair on the carried branch projects null.** cs-server
  never emits an occurrence that ends before it starts, on either branch.
- **No occurrence in a full cycle has `ends_at` before its `starts_at`.**
  An exhaustive assertion over every written payload, not a per-scenario
  one — an enumerated check over named scenarios would go green while a
  branch nobody listed stayed broken.

Pytest unit tests:

- `tests/test_event_recurrence_text.py` — `normalize_recurrence_text` over
  all **9 real production values** verbatim, asserting the 9→7 collapse and
  each expected output; plus `None`, `''`, whitespace-only, an `@handle`
  token, a phrase with digits (`'às 21 horas'`), and the `outcome` label for
  each. Idempotence: `f(f(x)) == f(x)` for every case.
- `tests/test_redis_projection_events.py` (extend) — the per-row
  computations happen once per row regardless of occurrence count: a
  6-occurrence recurring row increments each new counter exactly once.
  This is the defect `classify_ticket_url`'s own comment warns about.
  Also parametrised over all FOUR `EVENTS_PROJECTION_CATEGORY_TOTAL`
  outcomes — `canonicalized` (`'forró'` against a `"Forró"` vocabulary),
  **`unchanged`** (`'Forró'` against the same vocabulary), `off_vocabulary`
  (`'brega'`) and `absent` (null) — so no label in that counter's declared
  set is unexercised (N3).
- `tests/test_event_occurrence_end.py` (new) — `resolve_occurrence_end` as a
  parametrised pure-input matrix over all **six** outcomes on **both**
  branches, explicitly including:
  - a CARRIED occurrence whose stored interval is 3 days long keeps its
    stored `ends_at` verbatim (the multi-day festival vibes_bot refuses to
    truncate — the 24h bound must not reach this branch);
  - a CARRIED occurrence with a zero-length stored interval keeps it
    (equality is served, matching vibes_bot's serve-time guard);
  - a CARRIED occurrence whose stored end precedes its stored start is
    dropped (`dropped_inverted`) — cs-server never emits the inversion its
    own invariant forbids;
  - a DERIVED occurrence with a 30h duration is dropped
    (`dropped_implausible`), and one with a 0h duration is `dropped_zero`,
    not `dropped_inverted`.
- `tests/test_redis_projection_events.py` (extend) — the projector routes to
  the right branch: a recurring row with a PARSEABLE `recurrence_text`
  produces `occurrence_id != event_id` and takes the derived branch, while a
  recurring row with UNPARSEABLE text ("toda semana") produces
  `occurrence_id == event_id` and takes the carried branch. This is the
  routing revision 1 got wrong; asserting it only inside
  `resolve_occurrence_end` would not catch a projector that passes the wrong
  `derived` flag.
- `tests/test_event_extraction_service.py` (extend) —
  `EVENT_EXTRACTION_SUPPRESSED_RECURRING_TOTAL` increments once per parsed
  item whose `kind` is `menu`/`promotion`/`food`/`other` AND whose answer
  carries a recurring cadence, and does NOT increment for a `menu` item with
  no cadence, nor for an `event` item with one. Asserted with a stub client,
  no network.
- `tests/test_event_projection_selection.py` (extend) — `is_selectable` is
  False for `post_type` in `menu`/`promotion`/`food`/`other` with every
  other criterion passing. (Guards the removal path's mechanism.)
- `tests/test_event_extraction_prompt_kind.py` (new) — an OFFLINE assertion
  that both `EXTRACTION_PROMPT` and `MULTI_EVENT_EXTRACTION_PROMPT` carry the
  what-is-announced test, and that they carry it because both interpolate
  `_KIND_FIELD_DOC` (assert on the shared constant, then on both prompts).
  This is the anti-drift guard CLAUDE.md asks for; it asserts wiring, not
  model behaviour. It asserts **the closing sentence specifically** — the
  "food or a price mentioned ALONGSIDE one of those does not change the
  answer" one — because that single sentence is what keeps all four
  protected shapes (§1a), and an edit that trims it must fail a test, not
  only an eval someone might skip. It asserts the FOOD and PRICE bullets
  too, so a revision cannot delete the suppression half either.

Manual or integration checks:

- **Fixture builder — `scripts/eval_kind_on_captions.py` (new).** Revision 1
  built the eval from `title + description + category + recurrence_text`,
  i.e. POST-EXTRACTION PROSE, and said so. That is not good enough: that
  prose is a cleaned summary the previous run already decided was
  event-shaped, so it biases the eval toward passing on the 19 genuine
  events — **the eval could not have detected a false positive on a real
  caption, which is the only failure mode that matters.** The eval is
  therefore built from RAW CAPTIONS.

  Captions are not in RDS (`post_item_source` has no caption column and
  `raw_extraction` holds the model's parsed answer, not its input) but they
  ARE archived, and they are addressable without a listing scan: for each
  event, `list_event_sources(event_id)` gives `cover_photo_key` and
  `source_shortcode`; `event_source_media.derive_run_partition` recovers the
  run prefix from that key; `retrieved/…/info/_manifest.json` holds the
  post's own `caption` (this is exactly the field
  `event_extraction_service.EventPostSource._bucket_entries` reads at
  ingestion, so the eval sees byte-for-byte what the model saw). The script
  runs in the production container over SSM (`docs`' prod verification
  playbook: ship to `/app`, not `/tmp`), reads S3 and RDS read-only, writes
  nothing, and emits the fixture JSON. **It is the same script Path B0 in §4
  re-uses with `--handles`.**

  **The expected label must be assigned FROM THE CAPTION, and the captions
  must be READ before the fixture is checked in.** Revision 2 built the
  fixture from raw captions but took each row's expected `kind` from the
  census — which is post-extraction prose (`title`, `description`,
  `category`, `recurrence_text`), i.e. the previous run's own output. That
  is circular twice over, and worst exactly where it matters: the `menu` and
  `promotion` labels on the two offenders, which are the entire prediction
  this round rests on, were themselves inferred from prose the model wrote.
  The script therefore emits the caption with **`expected_kind: null`**, and
  a human reads each of the 21 captions and fills the label in from the
  CAPTION TEXT alone before check-in. The census may be shown alongside as
  context; it may not supply the answer.

  **A caption that does not support its census-derived label is a FINDING,
  not a fixture bug.** If the buffet's raw caption turns out not to read as
  a menu announcement, or a row the census called a genuine event reads as a
  standing offer, that changes what this round believes about the corpus —
  record it in the plan and re-derive the acceptance table before merging.
  Do not quietly relabel the row to match the census and move on. The
  fixture records both values (`census_kind`, `expected_kind`) so any
  divergence is visible in the diff rather than resolved in someone's head.

- **Prompt eval, resampled — the pre-merge gate for §1.** A checked-in
  fixture of **31 rows: the RAW CAPTION of all 21 live events** (built as
  above, each label assigned from the caption text) **plus the 10 synthetic
  controls in §5**. Of the 31, **30 are blocking and 1 is observed only**:

  | rows | expected | blocking? |
  |---|---|---|
  | 18 live events (incl. both `Aula de FORRÓ` rows and all three whose captions mention a promotion) | `event` | yes |
  | `Buffet de Terça a Domingo` (live) | `menu` | yes |
  | `QUINTA É DIA DE HAPPY HOUR` (live) | `promotion` | yes |
  | `Oktoberfest BeerDock` (live) | — | **no — recorded, not gated (§1b)** |
  | S4, S5, S6, S7, S14 (synthetic) | `event` | yes |
  | S9, S10, S11, S12, S13 (synthetic) | `menu` / `promotion` per §5 | yes |

  Synthetic rows are marked `"synthetic": true` in the fixture so nobody
  mistakes one for production data, and are **blocking on exactly the same
  terms as the live ones** — they are the only rows that exercise the
  protected boundary at all (§5).

  Run against the real model behind `@pytest.mark.live_openai`, deselected
  by default so neither `make test-unit` nor `make test-bdd` acquires a
  network dependency. **Resample 3×**; all three runs must agree, and a
  single false positive on a BLOCKING row — one row expected `event` that
  comes back `menu`/`promotion`/`food`/`other` — blocks the merge, live or
  synthetic. A rule that removes a real music, karaoke, quiz or family
  night is worse than the defect it fixes. A `menu`/`promotion` answer on
  `Oktoberfest BeerDock` does **not** block; it is recorded in the merge
  note as the measured cost of §0's concession.

  Residual limitation, recorded so nobody over-reads a green run: the eval
  sends the caption but not the flyer IMAGE, so a post whose only
  event evidence is on the flyer is judged more harshly here than in
  production. That biases the eval toward finding false positives, which is
  the safe direction; the two offenders are caption-only anyway.

- **Production proof, post-deploy — Path B0 of §4.** The same script,
  `--handles ctradicao downtownbeergarden_`, over every archived post at
  those handles within the extraction cap: the buffet's post must answer
  `menu`, the happy hour's `promotion`, and **every genuine event at those
  handles must still answer `event`** — `Sambinha Downtown` above all, whose
  caption says "HAPPY HOUR a tarde toda". No DAO, no `reconcile_post_events`,
  no writes. Operator-dispatched. §4 states why the write-path re-extraction
  (B1) is optional and gated behind the Path A freeze.
- **Production verification, post-deploy, read-only.** Re-run the same
  44-occurrence sweep used to build this plan
  (`GET /events?city=recife&from=…&to=…&page_size=100`, then each
  `GET /events/{occurrence_id}`) and assert over the whole set:
  no occurrence has `ends_at < starts_at`; the distinct `recurrence_text`
  values contain no two entries differing only by casing; no occurrence's
  `category` is `buffet` or `happy hour`; `total_count` has dropped by 9
  (or by whatever the two rows contributed at that moment).

- **Per-venue occurrence baseline — the standing false-positive check.**
  The sweep above also groups by venue. The 2026-09-07 baseline, and the
  only movement this round is allowed to cause:

  | handle | occurrences today | expected after | why |
  |---|---|---|---|
  | saladerebocorecife | 10 | 10 | — |
  | clubmetropole | 9 | 9 | — |
  | ctradicao | 6 | **0** | the buffet is its only event |
  | downtownbeergarden_ | 6 | **3** | happy hour out, Sambinha stays |
  | casabacurau | 5 | 5 | — |
  | tropical_gardeniashow | 3 | 3 | — |
  | ferroviariodeafogados | 2 | 2 | — |
  | tatubola.bar / seubotecorecife / beerdock_recife | 1 each | 1 each | — |
  | **total** | **44** | **35** | |

  **Why the total is still 35 under the simplified rule**, re-derived rather
  than carried over. Only two mechanisms can remove a row this round, and
  neither reaches a food-anchored one:

  1. **Path A** — operator PATCH, at two handles, scoped to already-selectable
     rows (§4 Step 0). It removes the buffet (6) and the happy hour (3) and
     nothing else, and — because of the Step-0 scope split — it can only
     subtract. 44 − 9 = **35**.
  2. **The §1 prompt change** — forward-only
     (`plans/260826_skip-already-extracted-posts.md`): no existing row is
     re-typed unless Path B1 is dispatched, and B1 reaches only `ctradicao`
     and `downtownbeergarden_`.

  So the concession costs **zero occurrences in this round's table**: the
  only food-anchored row in the corpus is `Oktoberfest BeerDock`
  (`beerdock_recife`, 1 occurrence), which is non-recurring, sits at a handle
  neither path touches, and is therefore unchanged at 1. **N = 35, and the
  concession's cost is zero here by measurement, not by assumption.** Where
  it does bite is the forward steady state — the deploy + 7 day re-read
  below.

  **The watch rule, restated under §0.** A fall at any other venue is
  triaged, not assumed:

  | what fell | verdict |
  |---|---|
  | a music, genre, karaoke, quiz, kids/family or class row | **false positive — revert the §1 clause.** These are §0's protected shapes and §1a's four rows. |
  | a row whose caption's subject is food or a price | **the accepted cost of §0.** Record the `event_id` and caption in the re-admission follow-up's evidence list. Not a revert trigger. |
  | anything you cannot classify from its caption in one reading | treat as a false positive until shown otherwise, and read the caption before deciding |

  Re-read at deploy + 1 day and deploy + 7 days, because the prompt fix is
  forward-only: a regression reaches the feed only as new posts are crawled,
  so a single post-deploy reading proves nothing about it. `ctradicao`
  disappearing from the feed entirely is the EXPECTED outcome here, not a
  defect — it has exactly one event and that event is the buffet.
- **Metrics check.** `EVENTS_PROJECTION_ENDS_AT_TOTAL{outcome="derived"}`
  and `EVENTS_PROJECTION_RECURRENCE_TEXT_TOTAL{outcome="normalized"}` must
  both be non-zero after a cycle, and `EVENTS_PROJECTION_ERRORS_TOTAL
  {stage="event"}` must not have moved. Read `process_start_time_seconds`
  first — an all-zero counter set on a young process says nothing.
- **No BDD scenario may reach OpenAI, S3, Apify or Instagram.**

## Acceptance Criteria

- `make test-unit` and `make test-bdd` are green, and the `@wip` tag is
  removed from the feature file.
- `EventOccurrence`'s field set is byte-identical to `main` — nothing added,
  removed, renamed or retyped.
- Redis key shapes are unchanged; no key family added or removed.
- No Alembic migration is added and no RDS row is modified by this change's
  code.
- `_KIND_FIELD_DOC` is the only prompt constant edited, and a test proves
  both prompts carry the change.
- The prompt eval passes **3/3 resamples over its 30 BLOCKING rows** — 20
  live events built from their RAW ARCHIVED CAPTIONS (each label assigned
  from the caption text, not from the census — see the Test Plan) plus the
  10 synthetic controls of §5 — with **zero false positives**: no blocking
  row expected `event` comes back `menu`, `promotion`, `food` or `other`.
  Both `Aula de FORRÓ` rows and all five protected synthetic controls
  (S4, S5, S6, S7, S14) are included in that zero. The 31st row,
  `Oktoberfest BeerDock`, is scored but **not gated** (§0/§1b) and its
  answer is recorded in the merge note.
- **The accepted cost is stated in the plan and its re-admission is a named
  follow-up.** §1b names the sacrificed shapes and the single live row
  (`Oktoberfest BeerDock`, `evt_01KZVGAW45PBBRKGEGE4W6SG90`); "Follow-ups"
  names the re-admission work. A round that suppresses food-anchored events
  without both of those recorded has made a silent regression, not a trade.
- **Path A wrote only to rows that were already selectable.** For every
  `event_id` PATCHed or confirmed, the Step-0 record shows
  `status ∈ {accepted, confirmed}`, `post_type == "event"`, a non-null
  `venue_id` and a null `superseded_by` BEFORE the write. No
  `pending_review`, `rejected`, `extraction_failed` or superseded row was
  confirmed. Verified from the Step-0 list, not from memory — the confirm is
  one-way and has no rollback through the admin API (§4).
- `EVENT_EXTRACTION_SUPPRESSED_RECURRING_TOTAL` exists, is zero-filled at
  import over its four `kind` labels, and has a unit test proving it counts
  a recurring `menu` answer and does not count a recurring `event` one.
- **After deploy, the events serving projection carries exactly the
  1.4.0 field list, with three fields changed in VALUE only:**
  - `recurrence_text` — casing-normalised sentence case (`'Toda QUARTA'` →
    `"Toda quarta"`), still free prose, still nullable, never a vocabulary;
  - `category` — canonicalised against the LIVE
    `admin_config:post_category_vocabulary` at projection time, with an
    off-vocabulary value passed through unchanged and never dropped;
  - `ends_at` — see the contract sentence below. **`is_recurring` does not
    appear in it.**

  Every other field is byte-for-byte what 1.4.0 served.

- **The `ends_at` contract, stated for vibes_bot and mobile** (this is the
  sentence the sibling repos should align to; it replaces revision 1's
  `is_recurring`-keyed wording, which was wrong):

  > An occurrence whose `starts_at` cs-server **re-derived** from a weekday
  > pattern (`occurrence_id` of the form `<event_id>_<YYYY-MM-DD>`) carries
  > an `ends_at` derived by applying the source row's stored DURATION to
  > that occurrence's own `starts_at`, or `null` when that duration is
  > missing, negative, zero, or longer than 24 hours.
  > An occurrence served on the announcement's OWN stored `starts_at`
  > (`occurrence_id == event_id` — every non-recurring row, and every
  > recurring row whose recurrence prose this repo cannot parse into
  > weekdays) carries the stored `ends_at` **verbatim, at any length**,
  > except that a stored end strictly before its stored start is served as
  > `null`.
  > Therefore: cs-server never serves an occurrence whose `ends_at`
  > precedes its `starts_at`; cs-server MAY serve an `ends_at` more than
  > 24 hours after its `starts_at` (a multi-day festival on the carried
  > branch) and MAY serve one exactly equal to it.

  **This resolves the disagreement F04 identified, in vibes_bot's favour.**
  vibes_bot's 1.4.1 plan declines to apply a 24h bound at serve time because
  it would delete a real multi-day festival's end; revision 1 of THIS plan
  would have made that deletion one layer earlier, on the recurring-but-
  unparseable branch, by keying on `is_recurring`. cs-server was wrong and
  is corrected in §3a. vibes_bot needs **no change**: its serve-time guard
  (drop only when `ends_at < starts_at`; keep equality; keep a forward end
  over 24h; keep when `starts_at` is null) is exactly compatible with the
  contract above, and after this deploy it should observe zero drops —
  it becomes defence in depth against a future projector bug rather than a
  live repair.
- Production, verified read-only after the deploy and the operator's Path A
  run (and Path B0 if dispatched): zero occurrences with
  `ends_at < starts_at`; zero case-variant duplicate `recurrence_text`
  values; zero occurrences whose `category` is `buffet` or `happy hour`.
- **Per-venue occurrence counts match the Test Plan's baseline table
  exactly** at deploy + 1 day and deploy + 7 days: `ctradicao` 6 → 0,
  `downtownbeergarden_` 6 → 3, **city total 44 → 35**, and **every other
  venue unchanged** — including `beerdock_recife` at 1, which the
  concession does not cost this round (the arithmetic is re-derived in the
  Test Plan). A count ABOVE 35, or a new `event_id` at either handle, means
  a queue row was confirmed in error (§4 Rollback).
  A fall anywhere else is triaged by caption per the Test Plan's watch-rule
  table: a protected shape falling reverts §1; a food- or price-subject
  caption falling is §0's accepted cost and is recorded in the follow-up's
  evidence list, not reverted.
- No feature flag, no new setting, no new admin-config key.

## Review findings — disposition

Every finding assigned to this repo by `141-REVIEW-FINDINGS.md`, verified
against the shipped code and the production census before acting, and either
FIXED here or REJECTED in writing. Two of the four verifications turned up
something the reviewer had not seen; both are recorded.

### F01 [BLOCKER] — the rescue clause is too narrow. **FIXED in revision 2;
the FIX is SUPERSEDED by §0 in revision 3.**

> Revision 2 answered F01 by widening the rescue until every food-anchored
> shape survived. §0 has since withdrawn the requirement that they survive,
> so the widening is gone and the eight food-anchored controls with it. The
> half of F01 that still binds — `karaoke`, `quiz / trivia` and
> `kids / family` were unrescued by revision 1 — is still fixed, by §1's
> closing sentence and §1a's four-row table, and still gated (S4, S5, S7).
> The revision-2 text below is kept for the audit trail.

**FIXED, and the finding was understated.**

Verified: `app/models/post_category.py:39-45` ships `food festival` and
`tasting` as first-class event categories, and revision 1's clause ("if what
repeats is FOOD … never event") suppressed both. Auditing all 18 entries
(§1a) shows the reviewer named 2 of **5** unrescued categories — `karaoke`,
`quiz / trivia` and `kids / family` are not "a performance, a competition, a
class or a screening" either — and that the food-alongside shapes
(`samba / pagode` at a feijoada, `sports screening` at a rodízio + jogo)
were suppressed too. Fixed by rewriting the clause around PROGRAMMED
OCCASION vs. THE VENUE OPERATING with an explicit "food and price do not
decide this by themselves" paragraph (§1), deriving it against the
vocabulary rather than against the two offenders (§1a), and adding 13
blocking synthetic controls (§5) of which 8 are food-anchored events that
must survive. Also on record: `Oktoberfest BeerDock` is a REAL food festival
already in the corpus, so the shape is not hypothetical.

### F02 [BLOCKER] — Path B can destroy what it measures. **FIXED, and the
prescribed fix was itself insufficient.**

Verified: `downtownbeergarden_` hosts both the happy-hour offender and
`Sambinha Downtown` (`evt_01KZVG66809JD1SCTQ2EJQAFJ4`). But the reviewer's
prescription — "PATCH `post_type: 'event'` … that write populates
`operator_edited_fields` and freezes them against reconciliation" — **does
not work on these rows.** `event_reconciliation.py:749` gates
`_confirmed_update_fields` (the only reader of `operator_edited_fields`) on
`status == "confirmed"`; an `accepted` row takes the branch at `:790-802`
which does a plain `fields.update(prepared)`, overwriting every content
field and recomputing `status`. Revision 1 of this plan asserted the same
false thing about Path A. Fixed by: correcting that claim in Evidence;
making protection **PATCH _then_ confirm**; replacing the proof with **Path
B0**, an offline caption eval with no write path at all; demoting the
write-path run to **Path B1**, optional and gated behind the freeze;
enumerating the blast radius by `venue_id` with expected post-run
`post_type` per row; and recording the rollback, including that confirming
is one-way through the admin API.

### F03 [MAJOR] — no gate can detect a false positive on a real caption.
**FIXED.**

Verified: captions are genuinely absent from RDS (`post_item_source` has no
caption column; `raw_extraction = dict(parsed)` is the model's OUTPUT) and
genuinely present in the archive manifests that
`event_extraction_service._bucket_entries` reads. Fixed by building the eval
from those raw captions via a new offline script (Test Plan), by adding
`EVENT_EXTRACTION_SUPPRESSED_RECURRING_TOTAL` on the extraction path — the
only place a suppression is observable, since a suppressed post is never
projected and therefore counted nowhere else — and by adding the per-venue
occurrence baseline re-read at deploy + 1 and deploy + 7 days, because a
forward-only prompt fix cannot regress on the first reading.

### F04 [MAJOR] — the ≤24h bound is keyed on `is_recurring`. **FIXED;
vibes_bot's position is the correct one.**

Verified: `event_occurrences.py:161-163` sends a recurring row with
unparseable recurrence text to `_single_occurrence`, whose occurrence
carries the stored `starts_at` and the bare `event_id`. Revision 1's table
would have nulled a genuine multi-day interval on that branch — the exact
deletion vibes_bot's plan refuses. Fixed by re-keying on DERIVATION
(`occ.occurrence_id != occ.event_id`), giving the carried branch **no length
bound at all**, and stating the contract in Acceptance Criteria in terms
that never mention `is_recurring`. vibes_bot needs no change; its serve-time
guard is compatible as written and becomes defence in depth. Measured today:
0 of 44 occurrences are on the recurring-but-unparseable branch, so this is
a latent hazard, not a live one — which is why a BDD scenario, not a
production check, pins it.

### F12 [MINOR] — no positive boundary case for a food-centred event.
**WITHDRAWN in revision 3.** Revision 2 fixed it with 13 synthetic controls,
8 of them food-anchored. §0 concedes that boundary, so the food-anchored
controls (S1, S2, S3, S8) are deleted rather than left as dead weight, and
the shapes they gated are named as accepted cost in §1b. The finding is not
carried as open: under §0 it is not a defect. The *other* boundary — the
four protected shapes — is still gated, by S4, S5, S6, S7 and S14.

### F18 [MINOR] — `dropped_inverted` is applied to non-inversions.
**FIXED.** Verified both halves: a zero-length interval is not an inversion,
and `event_occurrences.py:154-155` returns no occurrences when `starts_at`
is None, so a null start is unreachable in the payload loop. `dropped_zero`
is now its own label (§3a), the outcome set is six values, and §3a records
the unreachability. Zero is **carried** on the carried branch, matching
vibes_bot's "equality is permitted"; it is dropped only on the derived
branch, where the derived end would equal the start it came from.

### F08 / F16 [MINOR] — the stale "28% of events have no category".
**FIXED for this repo's half.** This plan already carried the correction;
it is now stated as superseding, names all three documents that repeat the
figure (`plans/260906_events-ui-v1-coordination.md:31-32`, vibes_bot's
contract table, mobile's D15), gives the replacement wording, and states
that the defensive null handling in both sibling plans is correct and must
stay — `category` IS nullable in the contract; only the evidence cited for
it was wrong.

## Revision 3 — the operator decision and the round-2 findings

### S1 — simplify the non-event rule. **DONE, as an operator decision, not a review fix.**

§0 records the decision verbatim, what it buys, the two boundaries it does
NOT cross, and a table of exactly what was deleted and why. §1 collapses the
clause from three paragraphs (~260 input tokens) to one question, two
bullets and two closing sentences (~130). §1a replaces the 18-row vocabulary
audit with the four shapes the concession does not cover. §1b states the
cost and names `Oktoberfest BeerDock` as its one live casualty. §5 drops the
four food-anchored controls and adds S14 (`todo sábado tem sertanejo, chopp
em dobro`) because the sertanejo-with-a-price shape is the one the operator
named by hand and revision 2 had no control for.

**Re-derived acceptance number: N is still 35, and the derivation is now in
the plan rather than inherited.** The reviewer expected it to move. It does
not, and the reason is mechanical rather than lucky: this round removes rows
by exactly two mechanisms — Path A (two handles, already-selectable rows
only) and a forward-only prompt change that re-types no existing row — and
the corpus contains exactly one food-anchored row, `Oktoberfest BeerDock`,
which is non-recurring and sits at a handle neither mechanism touches. 44 −
6 (`ctradicao`) − 3 (the happy hour) = **35**, with `beerdock_recife`
unchanged at 1. What the concession actually costs is in the FORWARD steady
state, so the cost is booked where it is really paid: the deploy + 7 day
re-read now triages a fall by caption subject instead of treating every fall
as a revert trigger, and §1b names what will stop arriving.

### N1 [MAJOR] — Path A can inflate the feed through a one-way state change. **CONFIRMED — fixed.**

Verified end to end: `list_events` passes `status` straight through and
`rds_venue_store.py:1313-1315` adds the clause only when it is not None, so
Step 0's deliberate `status=None` returns `pending_review`, `rejected`,
`extraction_failed` and superseded rows; `SELECTABLE_STATUSES = ("accepted",
"confirmed")` at `event_projection_selection.py:32`, so confirming any of
them makes it selectable; and `EventPatch` (`admin_events_router.py:327-347`)
has no `status` field while the router exposes only `confirm` and `reject` —
the transition really is one-way. Path A as written said "for each row in
the Step-0 list", which is the whole unfiltered set. Fixed by splitting
enumeration from action in §4 Step 0: the query stays unfiltered (reading
the blast radius is the point), the ACTION set is restricted to
already-selectable rows, the status is re-read immediately before each
write, the acceptance table gains an above-35 failure mode, and the rollback
section records that there is no un-confirm through the API and what to do
if one happens anyway.

### N2 [MINOR] — the eval's expected labels come from the wrong source. **CONFIRMED — fixed.**

Revision 2 built the fixture from raw captions but assigned each expected
`kind` from the census, which is post-extraction prose — the model's own
prior output — including the `menu`/`promotion` labels on the two offenders
that the entire round's prediction rests on. Fixed in the Test Plan: the
builder emits `expected_kind: null`, a human reads all 21 captions and
assigns the label from the caption text before check-in, the fixture carries
`census_kind` alongside `expected_kind` so any divergence shows up in the
diff, and a caption that does not support its census-derived label is
recorded as a **FINDING** that re-opens the acceptance table, never quietly
relabelled.

### N3 [MINOR] — `EVENTS_PROJECTION_CATEGORY_TOTAL{outcome="unchanged"}` is declared but unexercised. **CONFIRMED — fixed by exercising it.**

Confirmed against revision 2: the label appeared in the counter's declared
set and in no scenario, no Examples row and no unit test, in a plan whose
stated posture is that a zero-filled label's presence is the evidence — so
it asserted nothing. **Exercised rather than dropped**, because it carries a
real distinction the sibling `recurrence_text` counter already draws
(`normalized` vs `unchanged`): `canonicalized` means the vocabulary rewrote
the stored spelling, `unchanged` means the two already agreed. All four
labels are now defined in Error Handling And Observability, a BDD scenario
and an Examples row cover `unchanged`, and the projector unit test asserts
it.

### N4 — "the thin food boundary is under-tested". **DISSOLVED, not fixed.**

Under §0 this is not a defect. Suppressing a genuine food-anchored event is
the accepted cost of the simplest rule that reliably kills the standing-offer
class. It is recorded as such in §0 and §1b, with the sacrificed shapes named
and re-admission carried as a follow-up. It is not carried as an open finding
and no gate defends that boundary any more.

## Follow-ups

- **Re-admit food-anchored events.** §0's concession is deliberate and
  reversible; this is the work that reverses it. Evidence to gather first:
  the recorded `Oktoberfest BeerDock` eval answer, and every `event_id` the
  deploy + 7 day watch rule books as an accepted-cost fall. Cheapest first
  step, if the cost turns out to be higher than expected: scope §1's
  food/price test to STANDING cadences only, which restores one-off dated
  food posts without restoring revision 2's tie-break. The full restoration
  is revision 2's third paragraph, which remains in this file's history.
  Reversal per existing row needs no code: `PATCH /admin/events/{id}` with
  `post_type: "event"` re-projects it on the next 2-minute cycle (pinned by
  a BDD scenario in the Test Plan).
- **The events category index** — `events_category_v1:<city_slug>` and
  `events_city_categories_v1:<city_slug>`, shapes pinned in "The
  events-filter question", deliberately not built in 1.4.1.

## Open Questions

None.
