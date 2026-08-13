# Review Gate And Date Vocabulary — stop queueing items that are not defective

## Branch
fix/review-gate-and-date-vocabulary

## Goal
The review queue contains only items an operator can actually fix. An item is
queued because something went wrong reading the post — never because the post
is a kind that carries no date, never because a cadence names no weekday, and
never because a clock time is missing from an event whose date is known.

## Non-goals
- **Repairing already-stored rows.** Every change here is forward-only. Three of
  today's ten queued rows would already resolve correctly if re-run — see
  §Evidence "Three queued rows are already fixed in code". That repair is
  `260812_history-repair-dates.md`, which must run **after** this merges so it
  applies this vocabulary too.
- **Re-extracting anything.** No model call, no Apify call, no prompt change.
  `date_interpretation`'s prompt wording is deliberately left alone (§A explains
  why the deterministic path is the right place for this fix instead).
- **Improving the flyer classifier.** §D stops `unread_time` from *gating* the
  queue; it does not resolve the disagreement between the classifier and the
  extractor that produces the signal. The metric stays so it remains visible.
- **The event-detail media gallery.** `260813_event-source-media.md`.
- **Changing `compute_source_event_key` or event identity.** A forward-only
  resolver change alters the key a *future* extraction computes for a post whose
  date newly resolves. That is the same hazard `260812_history-repair-dates.md`
  §"Repairing a date moves the row's identity" already confronts, and it is
  handled there, once, rather than twice.

## Evidence

All measured against production on 2026-08-13. The venue-sourced review queue
holds **10 items**; six of them are queued by one of the defects below.

### The queue, in full

| Title | post_type | reason | `date_text` | verdict |
|---|---|---|---|---|
| `Aniversário do RODOLPHO Produções` | event | missing_date | `É HOJE` | **§A defect** |
| `Happy Hour` (Downtown) | promotion | missing_date | — (`todo dia`) | **§B + §C defect** |
| `happy hour e os combinhos…` | promotion | missing_date | — | **§C defect** |
| `100K` | other | missing_date | — | **§C defect** |
| `DIA DOS PAIS` | other | missing_date | — | **§C defect** |
| `NOITE DA PATROA` | event | unread_time | `08/AGO/2026 SÁBADO` | **§D defect** |
| `Especial do dia` | event | missing_date | — (`de segunda a sexta`) | already fixed, stale row |
| `Casa BeerDock 2027` | event | year_inferred | `De 06 a 09 de fevereiro` | already fixed, stale row |
| `SÁBADO DO CONCHITTAS` | event | year_inferred | `08/08` | already fixed, stale row |
| `Menu Dia dos Namorados` | event | unresolved_venue | `12 de Junho` | genuine, leave queued |

### Three queued rows are already fixed in code
Replaying each row's own stored model output through today's
`resolve_event_datetime` (a pure function — no I/O, so this is a faithful
replay, not a simulation):

| row | today resolves to | stored in RDS |
|---|---|---|
| `Especial do dia` | 2026-07-09 11:00, recurring | no date |
| `Casa BeerDock 2027` | 2027-02-**06**, range flagged | 2027-02-**09** |
| `SÁBADO DO CONCHITTAS` | **2026**-08-08 21:00 | **2027**-08-08 |

Nothing re-runs the resolver over stored rows, so a forward fix leaves its own
evidence sitting in the queue. **This is the "loop" the operator is describing:
the queue shows defects that were fixed days ago.** It is the strongest argument
yet for `260812_history-repair-dates.md`, and that plan's hard prerequisite —
`source_uploaded_at`, the anchor the resolver needs — is now **114/114 complete
on venue-sourced rows** after the 2026-08-13 provenance backfill.

### §A — the relative-date vocabulary is a closed literal set matched by equality
`app/services/event_date_resolver.py:487`:

```python
if text in ("hoje", "hoje à noite", "hoje a noite"):
if text in ("amanhã", "amanha"):
```

Whole-string equality against five literals. Probed against today's code with
the anchor 2026-08-07:

| `date_text` | resolves |
|---|---|
| `hoje`, `HOJE`, `hoje à noite` | ✅ the anchor |
| `amanhã` | ✅ anchor + 1 |
| **`É HOJE`**, `é hoje`, `Hoje!`, `hoje!!`, `HOJE 🔥`, `hoje tem`, `hoje é dia` | ❌ `missing_date` |
| **`AMANHÃ!`**, `É AMANHÃ`, `amanha a noite` | ❌ `missing_date` |

`hoje, 07/08` and `sexta, hoje` do resolve — but by falling through to the
numeric and weekday finders, which read the *other* part of the string. Those
successes are accidental and must not be read as the relative path working.

`É HOJE` is the exact post the operator raised at the start of this work — the
Rodolpho Produções video at Conchittas Bar. It has been queued since 2026-08-07.

**Why this keeps recurring.** A safety net for precisely this case already
exists: `date_interpretation`, the model's own structured reading, consulted as
a fallback when the deterministic finders return nothing
(`event_date_resolver.py:850`). It works — feeding
`{"kind": "relative", "relative": "hoje"}` alongside `date_text="É HOJE"`
resolves to the anchor via `structured_fallback`. And the prompt at
`app/api/openai_event_extraction_client.py:154` literally names *"a bare
'É HOJE'"* as its example.

But that field is **optional and opt-in**, described to the model as being for
shapes "you are not confident a simple parser can read on its own". A model
looking at `É HOJE` may reasonably judge it simple and return null — which is
what production shows. The field only reached extractions on 2026-08-12
(20 sources) and 2026-08-13 (28 sources); of those 48, **4 carry a non-null
interpretation**, all of them `day_month` shapes like `15.AGOSTO`. The Rodolpho
row predates the field entirely (`raw_extraction` has no such key).

So the most common date phrase in Brazilian nightlife posts is guarded by a
five-item literal list on the deterministic side and by a model's discretion on
the fallback side. That is the structural reason this class of defect keeps
reappearing — and it is why the fix belongs in the deterministic path, which is
free, testable, and does not depend on a model choosing to help.

### §B — a cadence that names no weekday cannot be represented
`_detect_recurrence` returns **a set of weekday numbers or None**. Every cadence
that names no weekday is therefore unrepresentable and falls through to
`missing_date`. Probed with `is_recurring=True` (the model's own claim):

| `recurrence_text` | resolves |
|---|---|
| `de segunda a sexta`, `toda quinta`, `sextas e sábados`, `quintas`, `de terça a quinta` | ✅ |
| **`todo dia`**, `todos os dias`, `diariamente`, `todo dia!` | ❌ `missing_date` |
| `todo fim de semana`, `toda semana`, `sempre` | ❌ `missing_date` |

`Happy Hour` at Downtown Beer Garden is the live case: the model returned
`is_recurring: True`, `recurrence_text: "todo dia"`, description
`"Happy Hour todo dia!"` — a complete, correct reading — and we filed it as a
date we could not read.

The row also exposes a second-order confusion. `is_recurring` and
`recurrence_text` are persisted as
`resolved.is_recurring or bool(parsed["is_recurring"])`
(`event_extraction_service.py:1030`), so the **stored row says
`is_recurring=true, recurrence_text='todo dia'` while `starts_at` is NULL and
the reason says the date is missing**. The row contradicts itself, which is
exactly what makes it read to an operator as "this obviously has a date".

### §C — the review gate demands a date from post types that never have one
`event_reconciliation.is_clean_extraction` (line 186) requires
`starts_at is not None` for **every** item, with no reference to `post_type`.
Anything short of clean becomes `pending_review` (line 771).

Four of the ten queued rows are not events:

- `100K` (`other`) — a "100 mil seguidores" thank-you post.
- `DIA DOS PAIS` (`other`) — a Father's Day greeting.
- `happy hour e os combinhos especiais do Bacurau Food` (`promotion`) — the
  model correctly returned `date_text: null`; there is no date on the post.
- `Happy Hour` (`promotion`) — ongoing, daily.

None of these has a date to find. The extraction was **correct** in every case;
the gate is what is wrong. Note that `post_type` classification is already
working: `260812_event-attribution-and-dates.md` §E taught it to type greetings
and recaps as `other`, and it did — and then the date gate queued them anyway.

This is also the answer to "why does the operator see a date?". For a promotion
the meaningful date is the post's own upload date, which the item does carry
(`source_uploaded_at`, now complete) and which the resolver discards because it
is looking for a date *stated in* the post.

### §D — `unread_time` gates the queue on an event whose date is known
`NOITE DA PATROA` at Club Metrópole: `starts_at = 2026-08-08`, confidence 0.98,
venue linked, reason `unread_time`. It is announced by **three** posts, merged
into one item; all three independently returned `time_text: null` while the
flyer classifier's `names_time` attribute said `"yes"`.

The two answers come from the same image — `flyer_names_time` is read off the
same manifest entry that becomes `flyer_photo_key`
(`event_extraction_service.py:342-350`), and `image_key = post.flyer_photo_key
or post.any_photo_key` (line 809). So this is a genuine disagreement between two
model calls on one image, not a carousel mix-up.

It is worth *watching* and not worth *blocking on*: the operator's judgement is
that a resolved date is sufficient, and an item whose only complaint is a
missing clock time is not actionable review work.

## Current Behavior
An item is auto-accepted only when it has a start date, a venue, no review
reason, and sufficient confidence. A date is required of every post type. The
relative-date vocabulary is five exact literals; the recurrence vocabulary is
weekday names only. A missing clock time on a dated event queues the item.

## Desired Behavior
1. `É HOJE`, `Hoje!`, `AMANHÃ!` and their decorated variants resolve to the
   post's own date, deterministically, without the model's help.
2. A cadence stated without a weekday — daily, weekend — resolves.
3. A post type that carries no date is not queued for lacking one.
4. An event whose date resolved is not queued for a missing clock time alone.
5. No item's stored `is_recurring`/`recurrence_text` contradicts its own
   `review_reason`.

## Implementation Approach

### A. Match relative tokens by word boundary, not whole-string equality
Replace the two equality checks with word-boundary regex matching for the
`hoje` and `amanhã`/`amanha` families, keeping the existing "à noite" variants
working unchanged.

**Why substring matching is safe here and would not be on a caption.**
`date_text` is not free text: the extraction prompt requires the model to copy
*the date expression itself*, "character for character", into this field
(`openai_event_extraction_client.py`, "Critical rule about dates and times").
It is a short, already-scoped field. The risk that motivated exact equality —
`hoje` appearing incidentally in a sentence — is a caption risk, not a
`date_text` risk.

Two guards keep it narrow anyway:

- **Order matters.** The relative check must continue to run *before* the
  numeric/month/weekday finders, so `hoje, 07/08` keeps resolving through the
  explicit date. But it must not fire when the text also carries an explicit
  date that disagrees — prefer the explicit date, and count the disagreement.
- **A recap is not an event.** "hoje faz um ano" is a past-tense recap; the
  `post_type` classifier already types those as `other`, and §C means an `other`
  item is not queued on its date either way. Do not add a second, weaker
  tense-detector here.

### B. Represent a cadence that names no weekday
Extend the recurrence vocabulary to the forms that state a cadence without a
weekday, mapping each to the weekday set it means:

- daily — `todo dia`, `todos os dias`, `diariamente`, `todo santo dia` → all
  seven weekdays, so the existing `_next_matching_weekday_on_or_after` resolves
  to the anchor itself with no new code path.
- weekend — `todo fim de semana`, `fins de semana`, `todo final de semana` →
  Saturday and Sunday.

`toda semana` and `sempre` state a cadence with **no resolvable day** and must
**not** be guessed at. They are handled by §C instead: they carry
`is_recurring=true`, so the item is recurring-with-no-computable-next-date,
which is a legitimate state and not a reading failure.

Gate these exactly as the existing range/list forms are gated — only when the
model itself claimed `is_recurring=True` — so an ordinary caption containing
"todo dia" cannot hijack a one-off event's explicit date.

### C. Require a date only from post types that are date-bearing
`is_clean_extraction` gains the item's `post_type` and requires `starts_at` only
for `event`. For every other type a missing date is normal and is neither a
review reason nor a bar to acceptance.

Two constraints:

- **`missing_date` must stop being *written* for those types**, not merely
  ignored downstream. A reason recorded but not acted on is how the console and
  the queue count come apart — this repo has shipped that once already
  (`260813_hide-promoter-events.md` §B). The reason is set in
  `event_extraction_service` (from `resolved.review_reason`) and the gate is in
  `event_reconciliation`; both must agree, and the predicate must be the single
  place that decides.
- **`menu` keeps its own semantics.** `_menu_is_current` already derives menu
  freshness from `last_seen_at` at read time and deliberately never touches
  `status`/`review_reason` (`admin_events_router.py:235`). This change must not
  disturb that: a menu was already never expected to carry a start date.

An `event` with no date stays queued as `missing_date`. That is the one case
where the reason means what it says.

### D. Make `unread_time` informational
Keep computing `unread_time` and keep its metric. Stop adding it to
`review_reason` when the item's date resolved — which, by its own definition
(`not resolved.needs_review`), is the only case in which it is ever computed.
The net effect is that it stops queueing items entirely, and it survives as a
counter.

Expose it on the admin API as a flag on the item rather than as a review reason
so the console can still show "the flyer named a time we did not read" as an
annotation. The console is a released client — this is an additive field, and
nothing may be removed.

### E. Keep the stored row self-consistent
While §B removes the specific case that produced it, an item can still store
`is_recurring=true` with a NULL `starts_at` (a `toda semana` cadence, §B). Make
that state legible rather than contradictory: such an item is recurring with no
computable next occurrence, and must not carry `missing_date`.

## Data, Config, And API Impact
- **Migration** — none. No column changes.
- **Admin config** — none. These are correctness rules, not tunables; a runtime
  flag here would let production and tests disagree about what "clean" means.
- **Admin API** — additive only: an `unread_time` (or equivalently named) boolean
  on the event DTO. No field removed, no type narrowed — the admin console is a
  separately-released N-1 surface.
- **Serving projection** — untouched. `events.post_item` never reaches Redis, so
  vibes_bot's app API and mobile are unaffected by every change in this plan.
- **Rollback** — revert. No data is rewritten, so a revert restores the previous
  gate exactly; items accepted under the new gate stay accepted, which is
  harmless (they are not defective, they were simply not queued).

## Error Handling And Observability
- `EVENT_DATE_RESOLUTION_TOTAL` already labels the resolution `path`. Ensure the
  new relative and cadence matches land on `deterministic`, so a **rise in
  `structured_fallback` becomes a real signal** — the model rescuing shapes the
  deterministic path still cannot read — rather than noise.
- Count auto-acceptances by `post_type`, so §C's effect is measurable and a
  regression that starts queueing promotions again is visible.
- Keep an `unread_time` counter after §D. If it drops to zero, the flyer
  classifier's `names_time` has stopped firing and we would otherwise never
  notice, since it no longer surfaces anywhere an operator looks.
- Log nothing per row. These paths run once per extracted event.

## Test Plan
Feature file: `tests/bdd/enrichment/review-gate-and-date-vocabulary.feature`

Scenarios:
- Resolve `É HOJE` to the post's own date. *(the Rodolpho case)*
- Resolve `Hoje!` and `HOJE 🔥` to the post's own date.
- Resolve `É AMANHÃ` to the day after the post.
- Prefer an explicit date over a relative token when the text carries both.
- Resolve a daily cadence stated as `todo dia` to the post's own date.
- Resolve a weekend cadence to the next Saturday.
- Leave `toda semana` without a start date and do **not** report a missing date.
- Do not queue a promotion that states no date.
- Do not queue an `other` post that states no date.
- Still queue an **event** that states no date, with reason `missing_date`.
- Accept an event whose flyer named a time that was not read.
- Report the unread time as a flag on the admin API rather than a review reason.
- Keep a menu's existing freshness semantics untouched.
- Never let a stored item claim it is recurring while reporting a missing date.

Pytest unit tests:
- `resolve_event_datetime` over the full relative-token table in §A's evidence,
  asserting each decorated form and each accidental-success form
  (`hoje, 07/08`, `sexta, hoje`) resolves through the intended path.
- `_detect_recurrence` over the full cadence table in §B's evidence, including
  the three forms that must deliberately return no weekday set.
- `is_clean_extraction` across the `post_type` × `starts_at` matrix, asserting
  `event` is the only type for which a missing date blocks acceptance.
- A regression test that the resolver stays pure: no wall-clock read, so a
  replay of stored inputs is deterministic. `260812_history-repair-dates.md`
  depends on this property.

Manual or integration checks:
- After deploy, re-run the venue crawl for `conchittasbar` and
  `downtownbeergarden_` and confirm the Rodolpho and Happy Hour items leave the
  queue. Both accounts are live and cheap to re-crawl.
- Read the queue count before and after; it must fall from 10 to 4 for the six
  §A–§D rows, with the three stale rows unchanged (they need the history
  repair) and `Menu Dia dos Namorados` still queued on `unresolved_venue`.

## Acceptance Criteria
- Every relative and cadence form in the §A and §B evidence tables resolves as
  the table states.
- No item with `post_type <> 'event'` is queued for a missing date, and no such
  item carries `missing_date` in its stored `review_reason`.
- An event with no readable date is still queued as `missing_date`.
- No item is queued whose only reason would have been `unread_time`.
- No stored item reports `is_recurring=true` together with `missing_date`.
- The admin API gained only additive fields.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
