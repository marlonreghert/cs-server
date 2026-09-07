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
  Used as-is by the verification plan; not modified.

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
- `post_type` is in `event_reconciliation.py`'s operator-protected field
  tuple, so a later re-extraction of the same post cannot flip it back.
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

- The Events UI v1 coordination plan states *"28% of events have no
  category"*. Measured today: **0 of 44 occurrences have a null
  `category`**. 11 distinct values, 5 of them off-vocabulary.
- `141-DETAIL-DEFECTS.md` D11 lists `endsAt` as *"15/44 real"*. It is 15/44
  PRESENT and 0/44 real — every one is inverted.

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
   and must keep classifying a genuine recurring night, and a recurring
   scheduled class, as `event`.
2. The serving projection must carry a casing-normalised `recurrence_text`,
   while RDS keeps the verbatim extraction.
3. The serving projection must carry a `category` canonicalised against the
   LIVE admin vocabulary at projection time, while RDS keeps the stored
   value.
4. The serving projection must carry, for a recurring occurrence, an
   `ends_at` derived by applying the source row's stored DURATION to that
   occurrence's own re-derived `starts_at` — and must carry `null` rather
   than a wrong instant whenever that duration is not usable.
5. Every substitution must be observable per outcome, computed once per
   source row, and must never abort a projection cycle.

## Implementation Approach

### 1. Extraction — make the `kind` ladder reachable (`app/api/openai_event_extraction_client.py`)

Amend `_KIND_FIELD_DOC` **only**. It is the single shared constant both
prompts interpolate, so the single-event and multi-event prompts cannot
drift — CLAUDE.md's own warning ("both extraction prompts must change
together; they have drifted before") is satisfied structurally.

Add a STANDING-OFFER test in the same pre-ladder position the existing
"does this announce something attendable" test occupies, stating in
substance:

> A recurring cadence only makes something an event when the thing itself is
> a HAPPENING — a show, a party, a DJ night, a class, a screening. The venue
> operating normally on its normal days is not a happening, however many
> days it names: a buffet or lunch served Terça a Domingo, a standing happy
> hour every Thursday, opening hours, a permanent menu, a daily special
> ("especial do dia"), a standing discount. Decide WHAT is being offered
> before deciding whether it repeats. If what repeats is FOOD, a PRICE, or
> the venue simply being open, answer "menu" or "promotion" — never "event"
> — even when the post states days and times. A performance, a competition,
> a class or a screening that repeats IS still an event.

The final sentence is load-bearing: it names the boundary the rule must not
cross (see §5's boundary cases). No other prompt field changes; token
budgets are unchanged (this replaces no field and adds ~90 tokens of prompt,
not of output).

**This change is forward-only.** `plans/260826_skip-already-extracted-posts.md`
makes the scheduled path skip a post already turned into an event, so the
amended prompt reaches only new posts unless the operator deliberately
re-extracts. That is the point of §4 below, not an oversight.

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
- `ends_at` — a duration, not an instant. Compute
  `duration = row.ends_at - row.starts_at` once per row, then:
  - **non-recurring row** → carry `row["ends_at"]` verbatim, exactly as
    today (outcome `carried`; zero live examples, so this branch is
    unchanged and unexercised in production);
  - **recurring row**, `duration` present, strictly positive, and
    `<= MAX_OCCURRENCE_DURATION` (a module constant, 24h — the same
    "pure module, frozen constant, never a setting" posture as
    `NIGHTLIFE_CUTOFF_HOUR` and `PAST_GRACE`) → `occ.starts_at + duration`
    (outcome `derived`);
  - **recurring row**, duration null/zero/negative → `None` (outcome
    `dropped_inverted`);
  - **recurring row**, duration over the bound → `None` (outcome
    `dropped_implausible`);
  - row has no `ends_at` → `None` (outcome `absent`).

  Projecting `None` rather than a wrong instant is the same posture
  `time_known: false` already takes for a defaulted midnight: suppress a
  misleading value rather than serve it.

`ends_at` is the only one of the three that varies per occurrence, so the
per-row work is the DURATION and the per-occurrence work is one addition.

No feature flag. `ticket_url` normalisation shipped unflagged in the same
function for the same reason: the projector re-asserts every 2 minutes, so a
bad rule is visible within minutes and reverted by a deploy, and a flag
would add a second code path to an already-branchy loop.

### 4. Removal path for rows already projected — operator-gated, no new code

Both paths exist. **Yes, it is operator-gated**, and it must be: an
automatic drop is the blocklist this plan rejects, wearing a different hat.

**Path B first (the proof), then Path A (the guarantee).**

- **Path B — deliberate re-extraction, after §1 deploys.** `POST /admin/
  trigger/event_extraction` with `{"eligibility": {"mode": "handles",
  "handles": ["ctradicao", "downtownbeergarden_"]}}`. This bypasses the
  already-extracted skip and re-reads the real captions through the amended
  prompt. Two handles, bounded by the per-venue cap of 20, so ≤40 vision
  calls. If the buffet row comes back `post_type: "menu"` and the happy hour
  `"promotion"`, the rule is proven **on production captions**, and both
  rows leave the index on the next projector cycle with nothing further
  done. This is a far stronger gate than any fixture.
  Precondition, checked read-only first: `GET /admin/events?venue_id=…` must
  show `status: "accepted"` and `operator_edited_fields: null` for both
  rows. A `confirmed` row is frozen against reconciliation by design — if
  either is confirmed, Path B cannot correct it and Path A is the only
  route.
- **Path A — operator correction.** `PATCH /admin/events/{event_id}` with
  `{"post_type": "menu"}` (buffet, `evt_01M1Y1W3V4HNXDQ7R2KN20B5JM`) and
  `{"post_type": "promotion"}` (happy hour,
  `evt_01M0XTA552MYQMA8P83AB01HN5`). Deprojects within one 2-minute cycle
  via `is_selectable`; records the field in `operator_edited_fields`, so no
  future re-extraction can flip it back. Removes 9 of 44 occurrences
  (20.5%) from the feed. Nothing is deleted; the row stays in RDS,
  correctly typed.

The operator dispatches both. No agent dispatches either — the same posture
the 1.4.0 coordination plan takes for the bairro sweep and the EAS release.

### 5. The boundary the rule must not cross

`Aula de FORRÓ na Sala de Reboco` (two rows, 6 of 44 occurrences, `category`
`workshop`/`forró`, `'Mensalidade: R$ 100'`, a named teacher, stated hours
"19h iniciados, 20h iniciantes") is a recurring **class**. It repeats, it is
paid, and it is not a party — the closest thing in the corpus to the class
this change removes. It **must stay `event`**: what repeats is a scheduled
activity a person attends, not food, not a price, and not the venue merely
being open. §1's closing sentence exists for it, and §7's eval pins it.

Also pinned to stay `event`: `Sambinha Downtown` (whose own description
mentions "HAPPY HOUR a tarde toda"), `Residência drag da Metrópole`,
`Lovezinho` (description mentions "promoções de combo a noite toda"),
`FORRÓ DOS PAIS`, `Drag Ataque`, `Baile Dançante`. Three of those name a
promotion inside an event caption — the existing "event first" precedence
was written for exactly that and must survive intact.

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
- **Prompt:** `_KIND_FIELD_DOC` grows by one paragraph. Both prompts
  interpolate it, so both change together by construction. No output-token
  budget change.

## Error Handling And Observability

Three new counters, all mirroring `EVENTS_PROJECTION_TICKET_URL_TOTAL`'s
shape (closed `outcome` label set, zero-filled at import so an absent label
is real evidence a path never ran — see the `/metrics` diagnostic that
depends on that property), each incremented **once per source row**:

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
  `carried|derived|dropped_inverted|dropped_implausible|absent`. Today's
  production would read `derived: 15` (or `dropped_*` if any stored pair is
  itself inverted) and `absent: 29`.

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
- `tests/test_redis_projection_events.py` (extend) — the `ends_at` decision
  table as a parametrised pure-input matrix over all five outcomes.
- `tests/test_event_projection_selection.py` (extend) — `is_selectable` is
  False for `post_type` in `menu`/`promotion`/`food`/`other` with every
  other criterion passing. (Guards the removal path's mechanism.)
- `tests/test_event_extraction_prompt_kind.py` (new) — an OFFLINE assertion
  that both `EXTRACTION_PROMPT` and `MULTI_EVENT_EXTRACTION_PROMPT` contain
  the standing-offer paragraph, and that they contain it because both
  interpolate `_KIND_FIELD_DOC` (assert on the shared constant, then on both
  prompts). This is the anti-drift guard CLAUDE.md asks for; it asserts
  wiring, not model behaviour.

Manual or integration checks:

- **Prompt eval, resampled — the pre-merge gate for §1.** A checked-in
  fixture of all 21 live events (title + description + category +
  recurrence_text, transcribed from the census in Evidence), each labelled
  with its expected `kind`: `menu` for the buffet, `promotion` for the happy
  hour, `event` for the other 19 **including both `Aula de FORRÓ` rows and
  all three events whose captions mention a promotion**. Run against the
  real model behind `@pytest.mark.live_openai`, deselected by default so
  neither `make test-unit` nor `make test-bdd` acquires a network
  dependency. **Resample 3×**; all three runs must agree, and a single false
  positive on the 19 blocks the merge — a rule that removes a real event is
  worse than the defect it fixes. Honest limitation, recorded here so nobody
  over-reads a green run: `description` is post-extraction prose, not the
  raw caption (captions are not persisted in RDS; they travel with archived
  photos in the S3 `retrieved/` layout), so this is strong evidence about
  the amended precedence, not proof over raw captions. The proof is the next
  item.
- **Production proof, post-deploy — Path B of §4.** Read-only precondition
  check (`status: "accepted"`, `operator_edited_fields: null`), then
  `mode="handles"` re-extraction of `ctradicao` and `downtownbeergarden_`,
  then re-read `GET /admin/events?venue_id=…`: the buffet row must be
  `post_type: "menu"` and the happy hour `"promotion"`. This runs the real
  captions through the amended prompt. Operator-dispatched.
- **Production verification, post-deploy, read-only.** Re-run the same
  44-occurrence sweep used to build this plan
  (`GET /events?city=recife&from=…&to=…&page_size=100`, then each
  `GET /events/{occurrence_id}`) and assert over the whole set:
  no occurrence has `ends_at < starts_at`; the distinct `recurrence_text`
  values contain no two entries differing only by casing; no occurrence's
  `category` is `buffet` or `happy hour`; `total_count` has dropped by 9
  (or by whatever the two rows contributed at that moment).
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
- The prompt eval passes 3/3 resamples with zero false positives on the 19
  genuine events, both `Aula de FORRÓ` rows included.
- **After deploy, the events serving projection carries exactly the
  1.4.0 field list, with three fields changed in VALUE only:**
  - `recurrence_text` — casing-normalised sentence case (`'Toda QUARTA'` →
    `"Toda quarta"`), still free prose, still nullable, never a vocabulary;
  - `category` — canonicalised against the LIVE
    `admin_config:post_category_vocabulary` at projection time, with an
    off-vocabulary value passed through unchanged and never dropped;
  - `ends_at` — for a recurring occurrence, the source row's duration
    applied to that occurrence's own `starts_at`, or `null` when that
    duration is missing, non-positive or over 24h; for a non-recurring
    occurrence, the stored value verbatim, unchanged from today.

  Every other field is byte-for-byte what 1.4.0 served.
- Production, verified read-only after the deploy and the operator's Path B
  or Path A run: zero occurrences with `ends_at < starts_at`; zero
  case-variant duplicate `recurrence_text` values; zero occurrences whose
  `category` is `buffet` or `happy hour`.
- No feature flag, no new setting, no new admin-config key.

## Open Questions

None.
