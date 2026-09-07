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

## Non-goals

- **A title blocklist, a `category` blocklist, or any downstream string
  filter that hides a row while leaving `post_type = 'event'` in RDS.** See
  "Why a general rule, and why a blocklist is not achievable".
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
  go, but a real `Happy Hour com Samba ao Vivo — sexta 18h, Banda X` must
  stay. No title substring separates them. Conversely `"Especial do dia"`
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

1. The extraction prompt must classify a recurring venue service offering as
   `menu` or `promotion`, never `event`, even when it names days and times —
   and must keep classifying as `event` **every shape this repo's own
   `DEFAULT_CATEGORY_VOCABULARY` names**, including the food-anchored ones
   (`food festival`, `tasting`) and the food-alongside ones (a feijoada with
   a roda de samba, a rodízio with a jogo no telão). A rule that removes the
   buffet but also kills a real recurring night is worse than the defect it
   fixes.
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

Add a WHAT-REPEATS test in the same pre-ladder position the existing "does
this announce something attendable" test occupies, stating in substance:

> Before applying the precedence, also ask WHAT repeats. A recurring cadence
> only makes a post an event when the thing that repeats is a PROGRAMMED
> OCCASION — something the venue puts on, that would not happen if nobody
> had staged it: a show, a party, a DJ or live set, a karaoke or quiz night,
> a class or workshop, a screening, a kids' or family session, a festival, a
> tasting or degustação, a themed night.
>
> The venue simply OPERATING is not a programmed occasion, however many days
> it names: its standing menu, a buffet or lunch served "de Terça a
> Domingo", a rodízio every night, its opening hours, a permanent price
> list, a daily special ("especial do dia"), a standing discount, a happy
> hour that is only cheaper drinks during ordinary hours. Answer "menu" when
> what is announced is a dish or a menu, "promotion" when it is a price —
> never "event" — even when the post states days and times.
>
> FOOD AND PRICE DO NOT DECIDE THIS BY THEMSELVES. The post is still an
> "event" whenever, alongside the food or the price, it announces something
> programmed: a named performer, band, DJ, host, teacher or team; a lineup;
> a ticket, cover, couvert or paid enrolment; a competition; a named edition
> or theme ("Feijoada com samba ao vivo", "Oktoberfest", "degustação
> guiada", "noite de karaokê"); a stated start time that is not simply the
> venue's opening hours. When BOTH readings are available — food or a price
> AND a programmed occasion — answer "event". The existing "event first"
> precedence is unchanged for that case; this test removes only the posts
> where there is NO programmed occasion at all, only the venue being open.

The third paragraph is the load-bearing one, and it is derived against this
repo's OWN shipped category vocabulary rather than against a hunch — §1a
enumerates all 18 categories and shows the rule keeps every one.

**Revision 1 of this plan got this wrong and the correction is the point of
this revision.** It closed the rule with a single sentence — "a performance,
a competition, a class or a screening that repeats IS still an event" — in
front of a clause reading "if what repeats is FOOD, a PRICE, or the venue
simply being open, answer 'menu' or 'promotion' — never 'event'". Audited
against `DEFAULT_CATEGORY_VOCABULARY` (§1a), that pair left **5 of the 18
shipped event categories unrescued** (`karaoke`, `quiz / trivia`,
`kids / family`, `food festival`, `tasting`) and **affirmatively suppressed
two of them** (`food festival`, `tasting`) plus every food-alongside shape
(`samba / pagode` at a feijoada, `sports screening` at a rodízio + jogo).
A rule that removes "Buffet de Terça a Domingo" but also kills a weekly
degustação or a feijoada com samba ao vivo is worse than the defect it
fixes. That is why the clause is now three paragraphs and why §1a exists.

No other prompt field changes. The constant grows by roughly 260 tokens of
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

### 1a. The rule checked against this repo's own shipped category vocabulary

`app/models/post_category.py::DEFAULT_CATEGORY_VOCABULARY` is the list this
repo already ships as *what an event's category may be*. A `kind` rule that
suppresses a shape the category vocabulary blesses is self-contradictory, so
the rule is derived against that list rather than against the two offenders.
All 18 entries, each with the recurring caption shape that would carry it
and the clause that keeps it:

| shipped category | recurring caption shape | kept by |
|---|---|---|
| live music | "toda sexta com a banda X" | named performer |
| DJ / club night | "toda sexta, DJ Bibi no comando" | named performer |
| samba / pagode | "feijoada de sábado com roda de samba" | **a performance ALONGSIDE food** |
| forró | "forró todo domingo, 20h" | programmed occasion + start time |
| rock | "quarta do rock, banda convidada" | named performer |
| MPB | "MPB às quintas, voz e violão" | programmed occasion |
| jazz | "jazz night toda terça" | named theme |
| sertanejo | "sertanejo na sexta, dupla X" | named performer |
| funk | "baile funk todo sábado" | party |
| karaoke | "toda terça é noite de KARAOKÊ, 20h" | **karaoke night named explicitly** |
| comedy | "stand-up toda quinta, R$ 20" | ticket + performance |
| quiz / trivia | "quiz da quarta, o time vencedor leva uma rodada" | **a competition whose prize is a drink** |
| kids / family | "domingo é dia da criança, recreação 10h-14h" | **family session named explicitly** |
| workshop | "aula de forró toda quarta, mensalidade R$ 100" | class + paid enrolment |
| food festival | "Oktoberfest todo sábado de outubro, banda + concurso" | **festival named explicitly, and it is FOOD** |
| tasting | "degustação guiada de cachaça toda quinta, R$ 45" | **tasting named explicitly, and it is FOOD** |
| sports screening | "todo domingo jogo no telão + rodízio de petiscos" | **a screening ALONGSIDE food** |
| party | "toda sexta é Lovezinho, open bar até meia-noite" | party alongside a drinks offer |

The five in bold-with-emphasis are precisely the ones revision 1's single
rescue sentence did not reach; `food festival`, `tasting`,
`samba / pagode`-at-a-feijoada and `sports screening`-at-a-rodízio were
additionally suppressed OUTRIGHT by its "if what repeats is FOOD … never
event" clause.

**This is not hypothetical.** `Oktoberfest BeerDock`
(`evt_01KZVGAW45PBBRKGEGE4W6SG90`, handle `beerdock_recife`) is a REAL row in
today's 21-event corpus — a food festival by name, currently `category:
party`, `is_recurring: false`. The corpus has no *recurring* one only because
21 events is a small sample; the vocabulary says the shape is expected, and
the rule must survive it.

The near-miss pairs the rule must separate, and the clause that separates
them:

| stays `event` | becomes `menu` / `promotion` | separator |
|---|---|---|
| "todo domingo jogo no telão + rodízio de petiscos" | "nosso rodízio roda todas as noites, 19h-23h" | a screening is programmed; a rodízio is the kitchen |
| "feijoada de sábado com roda de samba do grupo X" | "Buffet de Terça a Domingo" | a named performance vs. the kitchen's own hours |
| "quinta do stand-up, R$ 20 na porta" | "QUINTA É DIA DE HAPPY HOUR" (a price list) | a ticketed performance vs. cheaper drinks |
| "degustação guiada, R$ 45, vagas limitadas" | "especial do dia: risoto, R$ 39,90" | paid enrolment in a session vs. a dish at a price |

All eight of those captions are blocking rows in the Test Plan's eval (see the
synthetic-control table in §5).

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

For each row in the Step-0 list, `PATCH /admin/events/{event_id}` with its
CORRECT `post_type` — `{"post_type": "menu"}` for the buffet,
`{"post_type": "promotion"}` for the happy hour, and `{"post_type":
"event"}` for every genuine row at those two handles including Sambinha —
sending **only** `post_type` (the router records exactly the keys sent, so
this freezes that one field and nothing else). Then
`POST /admin/events/{event_id}/confirm` on each, which is what actually
makes `operator_edited_fields` binding (see the corrected Evidence bullet).

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
  `accepted` needs a direct RDS update, which is out of scope for this round
  — which is why Path A confirms only rows at these two handles and never a
  broader sweep.
- B0 has nothing to roll back.
- B1: any field it moved on an unedited row is restored by re-running B1
  after the prompt is fixed, or by a PATCH. `post_type` cannot have moved if
  the preconditions held.

The operator dispatches every step. No agent dispatches any of them — the
same posture the 1.4.0 coordination plan takes for the bairro sweep and the
EAS release.

### 5. The boundary the rule must not cross

#### What the LIVE corpus can pin

`Aula de FORRÓ na Sala de Reboco` (two rows, 6 of 44 occurrences, `category`
`workshop`/`forró`, `'Mensalidade: R$ 100'`, a named teacher, stated hours
"19h iniciados, 20h iniciantes") is a recurring **class**. It repeats, it is
paid, and it is not a party — the closest thing in the corpus to the class
this change removes. It **must stay `event`**: what repeats is a scheduled
activity a person attends, not food, not a price, and not the venue merely
being open.

Also pinned to stay `event`: `Sambinha Downtown` (whose own description
mentions "HAPPY HOUR a tarde toda"), `Residência drag da Metrópole`,
`Lovezinho` (description mentions "promoções de combo a noite toda"),
`FORRÓ DOS PAIS`, `Drag Ataque`, `Baile Dançante`. Three of those name a
promotion inside an event caption — the existing "event first" precedence
was written for exactly that and must survive intact.

#### What the live corpus CANNOT pin, and the synthetic controls that do

The 21-event corpus is exactly today's live feed, which means the eval built
from it is a *regression* test, not a *boundary* test: it contains not one
food-anchored happening. A grep of the whole corpus for
feijoada / degustação / gastro / almoço / jantar / churrasco / rodízio
returns nothing, and the one food festival present (`Oktoberfest BeerDock`)
is non-recurring, so the new clause's seam is never touched. **This is the
one boundary the live corpus cannot exercise, and it is the boundary the
rule is most likely to get wrong.**

Thirteen synthetic captions therefore join the eval, labelled SYNTHETIC in
the fixture so nobody mistakes them for production data, and **blocking on
exactly the same terms as the 21 live ones**. Each is mapped to the shipped
category vocabulary entry it stands in for, so the set is derived from §1a's
audit rather than assembled by taste:

| # | caption (abridged) | vocabulary entry | expected `kind` |
|---|---|---|---|
| S1 | "OKTOBERFEST — todo sábado de outubro, chopp alemão, banda Die Kapelle e concurso de dança bávara. Entrada R$ 20." | food festival | **event** |
| S2 | "Degustação guiada de cachaças artesanais, toda quinta às 19h, com o mestre alambiqueiro João Ramos. Vagas limitadas, R$ 45." | tasting | **event** |
| S3 | "FEIJOADA DO SÁBADO 🥁 Todo sábado a partir das 13h com roda de samba do grupo Samba de Mesa. Couvert R$ 15." | samba / pagode | **event** |
| S4 | "Toda terça é noite de KARAOKÊ, 20h, inscrição na hora, sem couvert." | karaoke | **event** |
| S5 | "Domingo é dia da criança: recreação, contação de história e brinquedoteca, das 10h às 14h, todo domingo." | kids / family | **event** |
| S6 | "TODA SEXTA É LOVEZINHO 💚 open bar de caipirinha até meia-noite, DJ Bibi no comando." | party | **event** |
| S7 | "Quiz da Quarta, 20h, equipes de até 5 pessoas — o time vencedor leva uma rodada de chopp." | quiz / trivia | **event** |
| S8 | "Todo domingo tem jogo no telão + rodízio de petiscos a partir das 16h." | sports screening | **event** |
| S9 | "Buffet de Terça a Domingo — a melhor hora do dia 🍛" (the live offender, verbatim) | — | **menu** |
| S10 | "QUINTA É DIA DE HAPPY HOUR. Promoções em dobro: caldinhos x2, gin tônica x2… Chopp por R$ 6,99." (the live offender, verbatim) | — | **promotion** |
| S11 | "Especial do dia: risoto de camarão, de segunda a sexta no almoço, R$ 39,90." | — | **menu** |
| S12 | "De segunda a quinta, chopp em dobro o dia inteiro." | — | **promotion** |
| S13 | "Nosso rodízio de pizza roda todas as noites, das 19h às 23h." | — | **menu** |

S8/S13 and S3/S9 are the discriminating pairs: the same food, the same
cadence, separated only by whether anything is *programmed*. A rule that
gets S8 and S13 both right is a rule that has understood the distinction; a
rule that answers `menu` to both has simply learned "rodízio", and a rule
that answers `event` to both has learned nothing at all. S11 is the
`"Especial do dia"` regression named in the 1.4.1 brief. S6 and S7 pin that
a drinks offer *inside* an event caption still loses to "event first".

The five vocabulary entries revision 1's rule failed (`karaoke`,
`quiz / trivia`, `kids / family`, `food festival`, `tasting`) are S4, S7,
S5, S1 and S2 respectively. A future revision of this clause that reproduces
that failure fails those five rows and cannot merge.

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
- **Prompt:** `_KIND_FIELD_DOC` grows by three paragraphs (~260 input
  tokens). Both prompts interpolate it, so both change together by
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
  `canonicalized|unchanged|off_vocabulary|absent`. This is the standing
  monitor for the C1 class: a new `buffet`-shaped category appearing in
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
- **A recurring food-anchored event is still projected.** A row with
  `post_type: "event"` and `category: "food festival"`, recurring, still
  reaches `events_index_v1:recife`. The projection must never filter on
  category — `food festival` and `tasting` are first-class entries in this
  repo's own `DEFAULT_CATEGORY_VOCABULARY`, and the C1 rule lives in the
  extraction prompt precisely so nobody is tempted to solve it here with a
  category blocklist, which would delete this row too. (The BDD counterpart
  of §1a.)
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
  that both `EXTRACTION_PROMPT` and `MULTI_EVENT_EXTRACTION_PROMPT` contain
  all three paragraphs of the what-repeats test, and that they contain them
  because both interpolate `_KIND_FIELD_DOC` (assert on the shared constant,
  then on both prompts). This is the anti-drift guard CLAUDE.md asks for; it
  asserts wiring, not model behaviour. It asserts the THIRD paragraph
  specifically — the "food and price do not decide this by themselves" one —
  because that is the paragraph revision 1 lacked, and an edit that trims
  the clause back to revision 1's shape must fail a test, not only an eval
  someone might skip.

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

- **Prompt eval, resampled — the pre-merge gate for §1.** A checked-in
  fixture of **34 rows: the RAW CAPTION of all 21 live events** (built as
  above) **plus the 13 synthetic boundary controls in §5**, each labelled
  with its expected `kind` — `menu` for the buffet, `promotion` for the
  happy hour, `event` for the other 19 **including both `Aula de FORRÓ` rows
  and all three events whose captions mention a promotion**, and each
  synthetic row's label per §5's table. Synthetic rows are marked
  `"synthetic": true` in the fixture so nobody mistakes one for production
  data, and are **blocking on exactly the same terms as the live ones** —
  they are the only rows that exercise the new clause's risky side at all
  (§5: the live corpus contains no food-anchored happening).

  Run against the real model behind `@pytest.mark.live_openai`, deselected
  by default so neither `make test-unit` nor `make test-bdd` acquires a
  network dependency. **Resample 3×**; all three runs must agree, and a
  single false positive — one row expected `event` that comes back
  `menu`/`promotion`/`food`/`other` — blocks the merge, live or synthetic.
  A rule that removes a real event is worse than the defect it fixes.

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

  Any OTHER venue's count falling is a false positive and is the trigger to
  revert the §1 paragraph. Re-read at deploy + 1 day and deploy + 7 days,
  because the prompt fix is forward-only: a regression reaches the feed only
  as new posts are crawled, so a single post-deploy reading proves nothing
  about it. `ctradicao` disappearing from the feed entirely is the EXPECTED
  outcome here, not a defect — it has exactly one event and that event is
  the buffet.
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
- The prompt eval passes **3/3 resamples over all 34 rows** — the 21 live
  events built from their RAW ARCHIVED CAPTIONS plus the 13 synthetic
  boundary controls — with **zero false positives**: no row expected `event`
  comes back `menu`, `promotion`, `food` or `other`. Both `Aula de FORRÓ`
  rows and all eight food-anchored synthetic controls (S1-S8) are included
  in that zero.
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
  `downtownbeergarden_` 6 → 3, city total 44 → 35, and **every other venue
  unchanged**. A fall anywhere else is a false positive and reverts §1.
- No feature flag, no new setting, no new admin-config key.

## Review findings — disposition

Every finding assigned to this repo by `141-REVIEW-FINDINGS.md`, verified
against the shipped code and the production census before acting, and either
FIXED here or REJECTED in writing. Two of the four verifications turned up
something the reviewer had not seen; both are recorded.

### F01 [BLOCKER] — the rescue clause is too narrow. **FIXED, and the
finding was understated.**

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
**FIXED**, by the same 13 synthetic controls F01 required; §5 states
explicitly that this is the one boundary the live corpus cannot exercise.

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

## Open Questions

None.
