# Agentic venue-link fallback — an audit-sourced, address-aware reviewer that closes its own false positives

## Branch
feature/agentic-venue-resolution-fallback

## Goal

Give `venue_link_audit`'s flagged `(handle, venue)` pairs a second, agentic look
that the current `EventVenueAdvisorService` structurally cannot give them, and
let a sufficiently-agreed verdict **act** — closing a false-positive flag, or
marking a real defect for an operator — instead of writing another inert
annotation nothing consumes.

Concretely, ship:

1. A new pass, `VenueLinkAuditReviewerService`, whose input is
   `collect_venue_link_audit`'s own flagged pairs (not an extraction run's
   touched event ids), and whose evidence includes the candidate venue's real
   **street address**, not just its name and neighborhood.
2. A `confirm` / `contradict` / `insufficient` verdict per pair, behind a
   K-of-K consensus gate and a hard structural exclusion, both calibrated
   against real measured production data (below).
3. A durable, operator-visible review record (new table + admin endpoints) —
   a `confirm` suppresses the flag, a `contradict` keeps it flagged and
   annotates *why*, and an operator can override either.
4. Two new admin-config flags, both default `False`.

## Non-goals

- **No change to any existing flag.** `venue_link_audit_enabled` and
  `event_venue_advisor_enabled` (both live in production as of today) keep their
  current defaults and behavior. `EventVenueAdvisorService`, its
  `RESOLUTION_QUEUED` trigger, its prompt, and its
  `set_event_venue_link_candidate_recommendation` write are all untouched.
- **No change to the deterministic layer.** `gate_auto_link`,
  `DEFAULT_CONFIDENCE_FLOOR` (0.55), `DEFAULT_MARGIN` (0.08),
  `DEFAULT_NAME_MATCH_TOP_K` (20) and `venue_corroborates_location_text`'s own
  matching logic are not retuned. This plan adds a separate agentic layer on top
  of the audit's output; it does not change what the audit flags.
- **No write to `events.post_item`.** This pass never sets `venue_id`,
  `location_resolution`, `linked_by`, `linked_at`, `status` or `review_reason`
  on any event row. See Evidence §E for the measurement that makes this a
  *finding*, not a limitation: across all 12 currently-flagged pairs, zero call
  for a reattribution.
- **Not adding `botecobeer.jsp`'s real venue** ("Boteco Beer Bistrô & Bar") to
  the catalog. This plan must surface it unmistakably and must not force-fit it.
- **No change to `force-reassign` / `unresolved-venue`** backfill modes shipped
  this week in `scripts/backfill_event_venue_links.py`.
- **No cron/scheduler wiring.** The pass is operator-triggered (a script) in
  this cycle. Automating it is a named follow-up, gated on the first batches
  being reviewed by a human.

## Evidence

Everything in this section was re-verified read-only against live production on
**2026-09-14** (EC2 `i-0893fb6d283243480`, container `vibes_bot-cs-server-1`,
via AWS SSM `send-command`), against `origin/main` at `408da88`. Nothing was
written. Where this disagrees with the brief that commissioned the plan, the
live reading is what is recorded here.

### §A — Production flag state, and the real flagged set

`venue_link_audit_enabled = True` and `event_venue_advisor_enabled = True`,
both confirmed live. `collect_venue_link_audit` returns **11 flagged handles /
12 flagged `(handle, venue)` pairs** — the brief's count of 11 handles is
correct, but its *composition* differs from the nine handles it names:

| handle | mapped venue | checkable | non-corroborating |
|---|---|---|---|
| `beerdock_recife` | BeerDock Boa Viagem | 31 | 2 |
| `botecobeer.jsp` | Recife Beer Cervejaria | 4 | 4 |
| `casabacurau` | Casa Bacurau | 34 | **31** |
| `conchittasbar` | Conchittas Bar | 28 | 20 |
| `downtownbeergarden_` | Downtown Beer Garden | 12 | 6 |
| `emporio_universitarioo` | Empório Universitário | 3 | 3 |
| `entreamigosobode` | Entre Amigos O Bode Espinheiro | 13 | 12 |
| `entreamigosobode` | Entre Amigos O Bode | 18 | 12 |
| `garage66.recife` | Garage 66 Pub Motorcycle | 5 | 3 |
| `lacasarecife` | La Casa Recife | 3 | 2 |
| `rockandribsmarcozero` | Rock & Ribs Lounge Pub | 8 | 6 |
| `saladerebocorecife` | Sala de Reboco | 17 | 2 |

Two corrections to the working set the brief inherited from
`plans/260914_recife-venue-mapping-corrections.md`'s deferred list:

- **`tatubola.bar` is no longer flagged.** It appears on that plan's list of
  nine untriaged handles; it does not appear in today's live audit output.
- **`casabacurau` IS flagged, at 31/34** — the worst ratio in the whole set —
  even though `plans/260914_recife-venue-mapping-corrections.md` **refuted** it
  with human verification ("Rua Capitão Lima, 100" *is* Casa Bacurau's own
  catalogued address). This is the single most valuable case in the corpus: a
  pair with independently-established human ground truth of "the mapping is
  correct", which the audit flags 31 times, *purely because the audit compares
  name and neighborhood and never the street*. It is the strongest available
  evidence for this plan's §1 and is used as such below.

### §B — The structural blocker the brief did not anticipate: there is no candidate set

The brief's instruction for §1 is "don't widen the candidate SET — that's the
existing resolution ladder's job; this is about giving the LLM richer evidence
about the candidates it's already given." Measured live, **there are no
candidates to enrich** for 8 of the 9 named handles:

| handle | events with ≥1 `event_venue_link_candidate` row | max candidates |
|---|---|---|
| `entreamigosobode` | 47 / 47 | 2 |
| `conchittasbar` | **0** / 69 | 0 |
| `saladerebocorecife` | **0** / 42 | 0 |
| `downtownbeergarden_` | **0** / 24 | 0 |
| `garage66.recife` | **0** / 12 | 0 |
| `rockandribsmarcozero` | **0** / 11 | 0 |
| `emporio_universitarioo` | **0** / 10 | 0 |
| `lacasarecife` | **0** / 6 | 0 |
| `botecobeer.jsp` | **0** / 5 | 0 |

The cause is already documented in `app/services/venue_link_audit.py`'s own
docstring: a `kind='venue'` crawl target whose handle maps to exactly one venue
has every event **force-assigned** by `EventExtractionService._extract_one`'s
default closure and never goes through `resolve_event_venue` at all — so
`_on_persisted`'s `replace_event_venue_link_candidates` never runs and no
candidate row is ever written. Only `entreamigosobode` has rows, because it is
double-mapped and therefore *does* run the ladder (2 candidates = its 2 mapped
venues).

`EventVenueAdvisorService.run_for_events` short-circuits on
`if not candidate_rows: continue` (`app/services/event_venue_advisor.py:188`).
A design that reads `event_venue_link_candidate` would therefore do **literally
nothing** for 8 of the 9 named handles, regardless of how rich its prompt got.

**Consequence for the design, stated plainly:** this plan cannot source its
candidate set from `event_venue_link_candidate`. It sources it from the handle's
own currently-mapped venue(s) — which is *not* a widening: it is the identical
closed set `compute_venue_link_audit` already compares each event against, and
it is strictly narrower than the ladder's top-20. The catalog is never searched.
The brief's intent (never let the LLM go shopping in the catalog) is honored;
its stated mechanism is not available.

### §C — Topic E: the candidate-cap backfill is NOT a prerequisite

`scripts/backfill_event_venue_candidate_cap.py` has still never been run against
production. Measured live today, the population is unchanged from its
2026-09-13 reading: **93 events** carry more than `DEFAULT_NAME_MATCH_TOP_K`
(20) candidates, all 93 of them over 1,000, max **2,661**.

The check that settles it — grouping those 93 event ids back to their
`post_item_source.source_handle`:

| handle owning an oversized candidate list | events |
|---|---|
| `oquetemhojeemnatal` | 88 |
| `recifequecabenobolso` | 5 |

**Zero of the nine named handles, and zero of the twelve flagged pairs, own any
oversized candidate list.** The intersection is empty. Combined with §B — this
plan's design reads no `event_venue_link_candidate` row at all — the backfill
cannot skew any signal this plan relies on, by two independent arguments.

Recorded as a **still-outstanding, non-blocking cleanup item**: 93 oversized
lists remain, and they do still affect `GET /admin/events/review` reads and any
future `EventVenueAdvisorService` call for those two handles. It should be run
(dry-run, confirm, apply) — just not as a gate on this plan.

### §D — Signal measurement: the address is load-bearing; event names are not

12 flagged pairs × K independent live `gpt-5.6-luna` calls per arm, same model
and `sampling_kwargs` the existing advisor uses. Arms:

- **A** = venue name + neighborhood (today's signal set).
- **B** = venue name + **street** + neighborhood + city (this plan's proposal).
- **C** = arm B + the handle's own post titles.

The decisive comparison, `lacasarecife` (catalog stores neighborhood
"Parnamirim"; posts say "La Casa em Casa Forte — Av. 17 de Agosto,1893 - Casa
Forte"; catalog street is "Av. Dezessete de Agosto 1893"):

| arm | verdict (K=5) | cited field |
|---|---|---|
| A (name + neighborhood) | **`contradict` 5/5** | neighborhood |
| B (+ street + city) | **`confirm` 5/5** | street |

Arm A does not merely decline on `lacasarecife` — it returns a **confident,
unanimous, wrong** verdict, for exactly the reason the user suspected: the
catalog's stored neighborhood is the erroneous field, and a reviewer that can
only see name and neighborhood has no way to notice. Adding the street flips it
to a unanimous, correctly-reasoned `confirm`
("*the post gives the same street and number as the catalog venue despite
spelling and neighborhood-label differences*"). `casabacurau` behaves the same
way (arm B: `confirm`, citing "Rua Capitão Lima, 100" — the venue's own street).

Across all 12 pairs, arm B cites `street` as the deciding field on **8 of 12**.
This is the measured justification for §1.

**Event names / lineup are not load-bearing.** Arm C changed the *direction* of
zero verdicts on zero pairs. Its only measured effect was to turn two
genuinely-mixed pairs (`beerdock_recife` 3/1/1 → 5/5; `casabacurau` 4/1 → 5/5)
into unanimous ones — i.e. to push cases that *should* reach a human past the
consensus gate. That is the wrong direction for this plan's risk posture. The
honest answer to "does an event-name/lineup similarity signal add real value for
any of the nine cases" is **no** — every one of the twelve pairs turns on
location text vs. venue name/address, and none turns on what the event was
called. Arm C is therefore **not shipped**.

### §E — What the nine cases actually need, and why almost none of them is a correction

Verified per handle from live `location_text` values and live `venues.address`
rows:

| handle | catalog street | what the posts say | honest verdict |
|---|---|---|---|
| `downtownbeergarden_` | `Rua Quarenta e Oito, 490` | "Rua Quarenta e Oito, 490 – Espinheiro" ×6 | **confirm** — exact street match; stored neighborhood is "Quintal Espinheiro" |
| `conchittasbar` | `R. Imperatriz Teresa Cristina 218` | "Rua da Imperatriz **Tereza** Cristina, 218" ×20 | **confirm** — same street, one letter apart (Teresa/Tereza) |
| `emporio_universitarioo` | `Av. Professor Luiz Freire, n° 600 Ao lado do IFPE…` | "Clube Assif - Av. Professor Luiz Freire, 600 (Ao lado do IFPE)." | **confirm** — street *and landmark* match verbatim |
| `lacasarecife` | `Av. Dezessete de Agosto 1893` | "Av. 17 de Agosto,1893 - Casa Forte" | **confirm** + surface the neighborhood-data discrepancy |
| `garage66.recife` | `RUA JOSE DA SILVA LUCENA 619 BOA VIAGEM` | "Rua José da Silva Lucena, 619 — Boa Viagem" | **confirm** + surface the discrepancy (stored neighborhood "Imbiribeira") |
| `rockandribsmarcozero` | `Avenida Alfredo Lisboa` | "Marco Zero" ×6 | **confirm** — a literal substring of the venue's own name; scoring artifact |
| `saladerebocorecife` | `R. Gregório Júnior, 264` | 15 corroborate; 2 are "Praia de Boa Viagem – Quiosque 13" and "BONITO – PE" | **no action** — mapping correct, 2 genuine off-site gigs |
| `entreamigosobode` | two siblings: `R. da Hora, 695` / `R. Marquês de Valença, 50` | "Amigos Park" ×12; one post naming **both** units' addresses | **decline** — genuinely undecidable from text |
| `botecobeer.jsp` | `R. Abdon Batista, 300 - Santo Amaro` | "Rua Madre Rosa, 181 – Jd. São Paulo" ×4 | **contradict** — real defect, needs a venue ADD |

**Seven of nine are "the mapping is right and the audit's textual check cannot
see it." One is "correctly decline." One is "correctly refuse and escalate."
Zero are "write a different `venue_id`."** The same holds for the two extra
flagged pairs (`casabacurau` → confirm, `beerdock_recife` → mapping correct with
2 residual events).

This is why the auto-approve pathway in this plan writes a **review verdict**,
not a venue attribution: there is no measured case in production today that
calls for an LLM to reattribute an event, and shipping that capability without
one would be unjustifiable risk. The seam is left clean for a future plan that
*does* have such a case (Open Questions).

### §F — Consensus measurement, and a confound that had to be removed first

**The confound.** An initial K=5 run at `max_completion_tokens=400` produced
**9 empty responses out of 120 calls (7.5%)**, all with the content field empty.
`gpt-5.6-luna` is a reasoning model and its invisible reasoning tokens are
charged against `max_completion_tokens`. Re-running the identical 120 calls at
`max_completion_tokens=3000` produced **0 empty responses out of 120**.

This has a direct consequence for the *existing*, now-live advisor:
`EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS = 200`
(`app/api/openai_event_extraction_client.py:104`) is **below the measured floor
for this model**. An empty response returns `None` from
`parse_event_venue_advisor_response`, which the validator records as
`REJECTION_MALFORMED` and the service counts as
`EVENT_VENUE_ADVISOR_OUTCOME_TOTAL{outcome="rejected"}` — indistinguishable, on
the dashboard, from the gate correctly refusing a bad answer. The brief's report
of "genuine non-determinism — calling it twice with IDENTICAL input returned two
DIFFERENT verdicts" is very likely this, at least in part, rather than model
judgment. **Flagged as a separate finding; see Open Questions. It is not fixed
in this plan**, whose non-goals forbid touching the existing advisor.

**The measurement**, K=10 independent calls per pair on arm B, at the corrected
token budget (120 calls, 2026-09-14):

| pair | verdict distribution (K=10) | unanimous | verbatim-quote gate |
|---|---|---|---|
| `conchittasbar` | confirm 10 | ✅ | 10/10 |
| `downtownbeergarden_` | confirm 10 | ✅ | 10/10 |
| `emporio_universitarioo` | confirm 10 | ✅ | 10/10 |
| `garage66.recife` | confirm 10 | ✅ | 10/10 |
| `lacasarecife` | confirm 10 | ✅ | 10/10 |
| `rockandribsmarcozero` | confirm 10 | ✅ | 10/10 |
| `botecobeer.jsp` | **contradict 10** | ✅ | 10/10 |
| `entreamigosobode` (Espinheiro) | confirm 10 | ⚠️ see below | 10/10 |
| `entreamigosobode` (Boa Viagem) | confirm 10 | ⚠️ see below | 10/10 |
| `casabacurau` | confirm 9 / insufficient 1 | ❌ | 10/10 |
| `beerdock_recife` | confirm 6 / insufficient 4 | ❌ | 10/10 |
| `saladerebocorecife` | confirm 7 / insufficient 2 / contradict 1 | ❌ | 10/10 |

Three results matter:

1. **The verbatim-evidence gate passes 120/120.** Every single answer quoted a
   span that appears character-for-character in one of the supplied
   `location_text` values. The existing validator's gate posture is viable
   unchanged for this pass — it will not become the bottleneck.

2. **Unanimity separates the mixed-evidence cases cleanly.** The three
   non-unanimous pairs are exactly the three where a human genuinely should
   look: `saladerebocorecife` (2 real off-site gigs), `beerdock_recife` (2 real
   residual events), `casabacurau` (31 non-corroborating texts, most naming
   other establishments at the same address). Across all 120 calls, **no verdict
   was factually wrong** once the address was supplied — the model's variance is
   in *how confident*, never in *which venue*.

3. **Unanimity alone is NOT sufficient, and no amount of resampling fixes it.**
   Both `entreamigosobode` siblings return `confirm` **10/10**, stably and
   confidently, on a shared pool of 12 "Amigos Park" events that cannot belong
   to both. One run even cited "Boa Viagem – Rua Marquês de Valença, **30**"
   as confirming a venue catalogued at number **50**. A pair-level question
   asked independently per sibling has no way to notice the conflict. This is a
   **structural** failure and needs a structural gate, not more samples.

## Current Behavior

- `compute_venue_link_audit` flags a `(handle, venue)` pair when ≥2 of the
  handle's checkable events corroborate neither the venue's name (via
  `name_similarity >= 0.55`) nor its neighborhood (folded substring). It is
  computed on demand, persists nothing, and has no memory — the same false
  positive is re-flagged on every `GET /admin/events/dedup-backlog` hit, forever.
- `EventVenueAdvisorService` only considers events at `RESOLUTION_QUEUED`
  (`location_resolution IS NULL AND venue_id IS NULL`) **and** sourced from the
  extraction run that just touched them, **and** carrying at least one
  `event_venue_link_candidate` row. None of the 12 flagged pairs satisfies all
  three. Its candidate context is `venue_id` + `venue_name` only. Its single
  write annotates a candidate row that nothing consumes.
- There is no pathway, anywhere in the repo, from "the audit flagged this pair"
  to any action at all. An operator reads the list and does everything by hand.

## Desired Behavior

A `VenueLinkAuditReviewerService` pass must:

1. Take its input from `collect_venue_link_audit(dao)` — the flagged pairs
   themselves — never from an extraction run's touched event ids.
2. For each flagged pair, assemble evidence from the venue's **name, street,
   neighborhood and city** (`venues.address`), plus the handle's own
   corroborating and non-corroborating `location_text` values, and the sibling
   mapped venues' same fields as labeled context.
3. Refuse to consider any event the skip table protects — `confirmed`,
   `manual_link`, `superseded`, `extraction_failed`, `rejected`,
   `operator_edited_venue` — reusing the repository's single existing expression
   of those rules rather than restating them.
4. Skip, without a model call, any handle mapped to **more than one venue**, and
   record that it was skipped and why.
5. Skip, without a model call, any pair an operator has already decided.
6. Issue K independent model calls and accept a verdict only when **all K agree,
   all K pass the verbatim-evidence validator, and all K calls succeed**.
7. Persist a durable review record for every pair it looked at — including the
   ones it declined to act on — carrying the verdict, the quoted evidence, the
   matched field, the per-call verdict breakdown, and the audit counts as of the
   review.
8. Let a consensus `confirm` **suppress** that pair from the audit's flagged
   output, and re-open it automatically if the pair's non-corroborating count
   later grows beyond the count recorded at review.
9. Leave a consensus `contradict` **flagged**, annotated with the machine's
   reason, so `botecobeer.jsp` surfaces as "the posts give a different street —
   this venue is probably not in the catalog" rather than being silently closed
   or silently force-fitted.
10. Expose every review record through the admin API, and let an operator accept
    or reject any of them; a rejected review never suppresses anything again.

## Implementation Approach

### 1. Promote the skip table into `app/services/`

`_skip_reason` is currently private to `scripts/backfill_event_venue_links.py`,
where its own docstring calls it *"the ONE place this check is expressed."* This
plan adds a fourth consumer, so promote it (and `SKIP_*` constants) to a shared
module under `app/services/`, re-export from the script so
`decide_one` / `decide_one_disputed` / `decide_one_force_reassign` keep
importing the same object. **No behavior change, no reordering of its six
checks.** This is the "reuse them, don't reinvent" requirement, done properly.

### 2. `app/services/venue_link_audit_reviewer.py` — the pure core plus a thin adapter

Mirror the `compute_*` / `collect_*` split `event_dedup_backlog.py` and
`venue_link_audit.py` both already use: a pure function that takes
already-fetched data and holds every rule (what every test drives), and one
adapter that fetches and delegates.

The pure core must own: the multi-mapped exclusion, the skip-table filter, the
evidence-bundle assembly, the consensus tally, and the suppression/staleness
decision. It must touch no DAO, no Redis, and no OpenAI client.

### 3. Validation — extend the existing gate's posture, do not weaken it

A new validator alongside `event_venue_advisor_validator.py`, keeping its exact
discipline (frozen dataclasses, never raises, rejection reasons as constants,
never logs the raw model payload). Rejections:

- `malformed` — unparseable, or a missing/empty required field.
- `verdict_not_recognised` — not one of `confirm` / `contradict` /
  `insufficient`.
- `matched_field_not_recognised` — not one of `name` / `street` /
  `neighborhood` / `city` / `none`.
- `evidence_not_verbatim` — the quote is not a substring of any supplied
  `location_text`. Same plain `in` test, same case- and accent-sensitivity, same
  refusal to search anything but the texts actually shown to the model.
  Measured 120/120 pass rate (§F), so this stays strict.
- A `confirm` or `contradict` with `matched_field="none"` is rejected: a verdict
  that acts must name the field it acted on.

### 4. The consensus gate — K=5 unanimous, plus one structural exclusion

Two gates, both required, both justified by §F:

- **K = 10 independent calls, all ten agreeing**, all ten validating.
  Decided over §F's own K=5 recommendation: at this document volume (12
  flagged pairs today, not a catalog-wide sweep run continuously), the extra
  cost of K=10 is negligible and buys a real margin — §F's own data shows
  `casabacurau` ran 9/10, meaning K=5 has a real chance of reading a
  mixed-evidence pair as unanimous and missing a hold-back it should have
  caught. K=10 does not miss `casabacurau` in the measured sample. K is a
  module constant and can be lowered later if per-pair cost ever becomes the
  binding constraint. The manual/integration checks below must confirm K=10
  does not introduce noise of its own (a pair that was unanimous at K=5
  becoming non-unanimous at K=10 only from added sampling variance, not from
  genuine new disagreement) before this is treated as settled.
- **Single-mapped handles only.** A handle mapped to ≥2 venues is never
  auto-applied. §F item 3 is the measurement: both `entreamigosobode` siblings
  return `confirm` 10/10 on a shared, mutually-exclusive event pool. Sampling
  cannot detect this; only the structural rule can.

Asymmetric action, because the risks are asymmetric: **only `confirm` closes a
flag.** A wrong `confirm` hides a real defect; a wrong `contradict` merely adds
noise to a queue an operator already reads. `contradict` is therefore recorded
and annotated but **never** closes anything, and never writes to an event.

### 5. Persistence — a new table at the audit's own grain

New Alembic migration (`0048`), table `events.venue_link_audit_review`, primary
key `(handle, venue_id)` — the grain of a flagged pair, not of an event. It must
carry: the verdict, evidence quote, matched field, the model's one-line reason,
the model id, K, the per-verdict tally, the `checkable_count` and
`non_corroborating_count` as of the review, timestamps, and a nullable operator
decision (`accepted` / `rejected`) with its own timestamp and note.

Reusing `event_venue_link_candidate.llm_recommendation` was considered and
rejected: it is per-event, and §B measured that 8 of the 9 handles have zero
rows on that table to attach anything to.

**Suppression and staleness.** The collector filters a pair out of the flagged
output when a stored review says `confirm` **and** the operator has not rejected
it **and** the pair's current `non_corroborating_count` is not greater than the
count stored at review time. New non-corroborating evidence re-opens the pair on
its own, with no operator action and no job.

### 6. Surfacing — a list a human can actually look at

- `GET /admin/events/venue-link-audit/reviews` — the operator-visible log:
  every review record, newest first, filterable by verdict and by whether an
  operator has decided it. Paged like every sibling admin list.
- The existing `venue_link_audit_candidates` entries on
  `GET /admin/events/dedup-backlog` gain an additive, optional `review`
  sub-object, so a still-flagged pair shows its machine verdict inline. Additive
  only — no existing field changes shape or meaning.
- `POST /admin/events/venue-link-audit/reviews/{handle}/{venue_id}/decision` —
  an operator accepts or rejects a machine verdict. A rejection is permanent for
  that pair: it is never re-reviewed and never suppresses anything.

### 7. The runner

`scripts/review_venue_link_audit.py`, following
`backfill_event_venue_candidate_cap.py`'s established shape: **dry-run by
default**, `--apply` to persist, `--handle` to scope to one pair, `--report-json`
for the run report, idempotent, resumable. Dry-run makes every model call and
prints every verdict but writes nothing — so the first production run is a
measurement an operator reads before anything is ever suppressed.

## Data, Config, And API Impact

**Config — two new admin-config keys, both `False` by default**, following the
five-step convention in `app/container.py` / `app/services/event_dedup.py`
(`ADMIN_CONFIG_*_KEY` with the full `admin_config:` prefix, a
`validate_*_config` that raises `TypeError` on a non-bool, a `load_*` through
`_load_validated_config`, registration in the `AdminConfigService(validators=…)`
dict under the **bare** key name, and a single gate at the call site):

- `venue_link_audit_reviewer_enabled` — gates whether the pass runs at all.
  When `False` no model call is made and no row is read.
- `venue_link_audit_reviewer_auto_apply_enabled` — gates whether a consensus
  verdict **suppresses** a flag. When `False` the pass still runs and still
  records review rows, but every flagged pair stays flagged.

Two flags rather than one deliberately: the risk here is a write that hides a
real defect, so the pass must be runnable in shadow mode — producing a full,
inspectable verdict log against live data — before it is ever allowed to close
anything.

**Persistence** — one new table via migration `0048`,
`events.venue_link_audit_review`, and the DAO methods to upsert/read/list it.
No existing table, column, index, or Redis key changes. `events.post_item` and
`events.event_venue_link_candidate` are not written by this pass at all.

**API** — one new list endpoint, one new decision endpoint, and one additive
optional field on the existing `VenueLinkAuditVenueOut`. No existing response
field changes type or meaning; nothing is removed.

## Error Handling And Observability

The pass must never raise into its caller, matching
`EventVenueAdvisorService.run_for_events`'s contract: an OpenAI failure, a
validator rejection, or a DB error on one pair is caught, counted, logged, and
leaves that pair exactly as the audit produced it.

New counter `VENUE_LINK_AUDIT_REVIEW_OUTCOME_TOTAL{outcome}`, with outcomes:
`confirmed` (consensus confirm, applied), `contradicted` (consensus contradict,
flagged), `insufficient` (consensus insufficient), `no_consensus` (the K calls
disagreed), `skipped_multi_mapped`, `skipped_operator_decided`,
`skipped_all_events_protected`, `validator_rejected`, `error`.

A climbing `no_consensus` or `validator_rejected` means the prompt needs work,
not that the data got worse — the same reading `event_venue_advisor`'s own
`rejected` counter was designed to support. **Because §F showed an empty
response is silently counted as a validator rejection**, this pass must count
an empty/truncated model response under its own distinct outcome
(`empty_response`) rather than folding it into `validator_rejected`, so the
confound that hid in the existing advisor cannot recur here.

Model spend rides the existing `OPENAI_API_CALLS_TOTAL` /
`OPENAI_TOKENS_TOTAL` / `OPENAI_API_CALL_DURATION_SECONDS` families under a new
`endpoint` label, kept separate from `event_venue_advisor` so the two passes'
costs never merge. The K-call multiplier must be visible: the plan's own
acceptance criteria include reading that label after the first production run.

Logs carry handle, venue_id, verdict, outcome and the consensus tally — never
the raw model payload, matching the existing validator's discipline.

## Test Plan

Feature file: `tests/bdd/enrichment/agentic-venue-resolution-fallback.feature`

Scenarios:

- **An audit-flagged pair whose posts give the venue's own street address is
  confirmed and stops being flagged** — the `conchittasbar` / `downtownbeergarden_`
  shape; proves the audit-sourced input path and the address signal together.
- **A pair whose catalog neighborhood is wrong but whose street matches is
  confirmed, and the discrepancy is recorded** — the `lacasarecife` shape;
  proves arm A's measured false `contradict` cannot recur.
- **A pair whose posts give a different street is left flagged and annotated as
  needing a new catalog venue** — the `botecobeer.jsp` shape; proves a
  `contradict` never closes a flag and never force-fits a venue.
- **A handle mapped to more than one venue is never auto-applied, even when every
  call agrees** — the `entreamigosobode` shape; the structural gate from §F.
- **A pair whose K calls disagree is recorded and left flagged** — the
  `saladerebocorecife` / `beerdock_recife` shape.
- **A model answer quoting text that was never supplied is rejected and writes
  nothing** — the verbatim gate.
- **An empty model response is counted separately and never read as a rejection**
  — the §F confound, pinned so it cannot recur.
- **An event the skip table protects is never used as evidence and never touched**
  — one scenario per protected reason worth distinguishing (confirmed,
  manual_link, operator_edited_venue).
- **A confirmed pair re-opens when new non-corroborating evidence arrives** —
  the staleness rule.
- **An operator rejection is permanent and suppresses nothing** — the override.
- **The pass is invisible and makes no model call while its flag is off** — the
  default-`False` guarantee, for both flags independently.
- **A second run over unchanged data changes nothing** — idempotency.

Pytest unit tests:

- `tests/test_venue_link_audit_reviewer.py` — the pure core: consensus tally at
  K=5 (unanimous / one dissent / all-error), the multi-mapped exclusion, the
  skip-table filter, the suppression + staleness comparison, evidence-bundle
  assembly including the sibling-context block.
- `tests/test_venue_link_audit_reviewer_validator.py` — every rejection reason,
  including `confirm` with `matched_field="none"`, and the verbatim gate's
  case/accent sensitivity.
- `tests/test_review_venue_link_audit.py` — the script's dry-run/apply split,
  `--handle` scoping, report JSON shape, idempotency.
- DAO tests for the new table against both `RdsVenueStore` and
  `tests.rds_fake.InMemoryRdsVenueStore`, so the two stores cannot drift (the
  `_EVENT_COLUMNS` allowlist trap documented in `rds_venue_store.py`).
- Admin-config tests for both new flags: default `False`, non-bool rejected,
  `"false"` never coerced to `True`.

Manual or integration checks:

- Run `scripts/review_venue_link_audit.py` **dry-run** against production with
  both flags still `False`, and confirm its verdicts reproduce §F's table.
- A labeled fixture corpus under `tests/fixtures/venue_link_audit_reviewer/`
  with a `MANIFEST.md`, following the per-item provenance convention
  `tests/fixtures/venue_link_audit/MANIFEST.md` established — sourcing every
  `location_text` and address to a live read, PII-reviewed before commit.
- **K=10 noise check**: re-run §F's K=10 measurement (or a fresh equivalent) and
  confirm no pair that was unanimous at K=5 becomes non-unanimous at K=10 for
  reasons other than genuine model disagreement already visible in the K=5
  sample — i.e. going from K=5 to K=10 should only ever ADD confidence
  (catching `casabacurau`-shaped near-misses) never SUBTRACT it. Record the
  real distribution, don't assume it.

## Acceptance Criteria

- With both new flags `False`, a full deploy makes **zero** model calls, writes
  **zero** rows, and changes **zero** bytes of every existing endpoint's
  response. Provable by running the suite with the flags at their defaults.
- The pass reaches all 12 currently-flagged pairs from
  `collect_venue_link_audit`'s output alone, with no extraction run involved.
- The venue's street reaches the model for every pair; a pair whose venue has no
  `venues.address` row degrades to name+neighborhood rather than failing.
- A consensus `confirm` on a single-mapped pair suppresses that pair and writes
  exactly one review row. No event row is modified by any code path in this plan.
- A consensus `contradict` leaves the pair flagged.
- A multi-mapped handle is never auto-applied, regardless of agreement.
- Every pair the pass looked at has a review row, including skipped and
  no-consensus ones.
- An operator can list every review and accept/reject any of them; a rejection
  is permanent.
- `VENUE_LINK_AUDIT_REVIEW_OUTCOME_TOTAL` distinguishes `empty_response` from
  `validator_rejected`.
- No `@wip` tag remains on the feature file after `/execute-feature`.
- `venue_link_audit_enabled`, `event_venue_advisor_enabled`,
  `DEFAULT_CONFIDENCE_FLOOR`, `DEFAULT_MARGIN`, `DEFAULT_NAME_MATCH_TOP_K`,
  `gate_auto_link` and `venue_corroborates_location_text` are byte-identical to
  `408da88`.

## Open Questions

1. **RESOLVED — separate, small fix, running independently of this plan's own
   execution** (not before or after as a hard sequencing dependency — the two
   touch different files and this plan's own §F measurement already worked
   around the bug by using 3000 tokens in its own calls, so neither blocks the
   other). `EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS = 200` against a
   reasoning model produced 9 empty responses in 120 calls at 400 tokens and 0
   at 3000 (§F). Tracked and shipped as its own tiny plan/PR, outside this
   plan's non-goals boundary.
2. **RESOLVED — K=10, not K=5.** At today's document volume (12 flagged pairs,
   operator-triggered, not a continuous sweep) the extra spend is negligible
   and buys real margin against `casabacurau`-shaped near-misses. Execution
   must include the K=10 noise check added to Manual/integration checks above.
3. **Should the runner ever become a cron job?** Confirmed: operator-triggered
   only for this plan, unchanged. Once this plan is deployed and has run
   against real production data, produce a careful, separate writeup (can be
   done by a subagent) of the pass's actual observed behavior and exactly what
   turning it into a cron job would mean — cadence, re-review of
   already-reviewed pairs, interaction with the staleness/re-opening rule,
   cost at a recurring cadence — so that decision is made from the real,
   deployed system's behavior, not from this plan's own predictions. Not
   scheduled as part of this plan's own execution.
4. **RESOLVED — the corpus is the twelve live flagged pairs**, not the nine
   handles the original brief named. §A's live measurement is authoritative;
   the nine-handle list was the brief's own stale working set at the time it
   was written and is superseded by this plan's own re-verification.
5. **RESOLVED — `botecobeer.jsp`'s real venue is being added separately**,
   outside this plan, by the operator directly (investigation + the existing
   venue-add pipeline). This plan's own scope is unchanged: surface it
   correctly via `contradict`, never force-fit it.
6. **RESOLVED — the 93 oversized candidate lists were backfilled on
   2026-09-14**, ahead of and independent of this plan
   (`scripts/backfill_event_venue_candidate_cap.py --apply`, all 93 events
   truncated to `top_k=20`, 203,421 stale rows removed). §C's own two
   independent arguments already established this plan's design reads no
   `event_venue_link_candidate` row at all, so this was never a dependency —
   it is simply no longer outstanding either.

## Execution Report (2026-09-14, `feature/agentic-venue-resolution-fallback`)

### What shipped

| Area | File |
|---|---|
| Skip table, promoted to ONE definition | `app/services/event_link_skip.py` (new); `scripts/backfill_event_venue_links.py` re-exports |
| Pure core + service adapter | `app/services/venue_link_audit_reviewer.py` (new) |
| Validator gate | `app/services/venue_link_audit_reviewer_validator.py` (new) |
| Model call | `app/api/venue_link_review_client.py` (new) |
| Persistence | `migrations/versions/0048_venue_link_audit_review.py` (new) + DAO on `RdsVenueStore`, `VenueRepository`, `InMemoryRdsVenueStore` |
| Surfacing | `GET /admin/events/venue-link-audit/reviews`, `POST …/{handle}/{venue_id}/decision`, additive `review` on the audit output |
| Runner | `scripts/review_venue_link_audit.py` (new) |
| Observability | `VENUE_LINK_AUDIT_REVIEW_OUTCOME_TOTAL{outcome}`; `OPENAI_*{endpoint="venue_link_review"}` |
| Flags | `venue_link_audit_reviewer_enabled`, `venue_link_audit_reviewer_auto_apply_enabled` — both `False` |

`app/api/openai_event_extraction_client.py` was deliberately NOT touched (the
advisor's token-budget fix is a concurrent, independent change), so the new
prompt and call live in their own module. That module's docstring records why,
and that the two token budgets must stay separate.

### K=10 noise check — the amended plan's required manual check

Required: "confirm no pair that was unanimous at K=5 becomes non-unanimous at
K=10 … going from K=5 to K=10 should only ever ADD confidence, never SUBTRACT
it. Record the real distribution, don't assume it."

Measured, not assumed — both distributions are the real production arm-B runs
from planning (12 pairs; 60 calls at K=5, 120 at K=10, same model, same
prompt, same corrected 3000-token budget):

| pair | K=5 | K=10 | unanimous K=5 → K=10 |
|---|---|---|---|
| `conchittasbar` | confirm 5 | confirm 10 | ✅ → ✅ |
| `downtownbeergarden_` | confirm 5 | confirm 10 | ✅ → ✅ |
| `emporio_universitarioo` | confirm 5 | confirm 10 | ✅ → ✅ |
| `garage66.recife` | confirm 5 | confirm 10 | ✅ → ✅ |
| `lacasarecife` | confirm 5 | confirm 10 | ✅ → ✅ |
| `rockandribsmarcozero` | confirm 5 | confirm 10 | ✅ → ✅ |
| `botecobeer.jsp` | contradict 5 | contradict 10 | ✅ → ✅ |
| `entreamigosobode` (Espinheiro) | confirm 5 | confirm 10 | ✅ → ✅ |
| `entreamigosobode` (Boa Viagem) | confirm 5 | confirm 10 | ✅ → ✅ |
| `casabacurau` | confirm 4 / insuf 1 | confirm 9 / insuf 1 | ❌ → ❌ |
| `beerdock_recife` | confirm 3 / contra 1 / insuf 1 | confirm 6 / insuf 4 | ❌ → ❌ |
| `saladerebocorecife` | insuf 2 / confirm 3 | insuf 2 / confirm 7 / contra 1 | ❌ → ❌ |

**Result: LOST unanimity going 5→10: NONE. GAINED: NONE.** The unanimous set is
identical (9 of 12) at both K. K=10 adds no noise of its own; it only reduces
the probability that a mixed-evidence pair reads as unanimous on a given draw
(`casabacurau` at 9/10: P(unanimous) falls from ~0.59 at K=5 to ~0.35 at K=10).
The check passes as specified.

Caveat recorded honestly: this is one K=5 draw and one K=10 draw over 12 pairs,
not a repeated-sampling study. It establishes that K=10 does not *introduce*
disagreement; it does not establish a precise miss rate for either K.

### Defects found and fixed during execution

1. **`venue_link_audit_reviewer_validator.py` — a non-string `evidence_quote`
   raised instead of rejecting.** `218 in "…, 218"` raises `TypeError`, and the
   validator is called outside `_review_one`'s try/except, so one bad answer
   would abort a whole batch — breaking both the validator's "never raises" and
   the service's "never raises into its caller". Now rejected as `malformed`.
   Genuinely reachable: the decisive evidence in this corpus is house numbers.
2. **`build_review_evidence` restated the skip table.** It carried its own
   `status == STATUS_SUPERSEDED` branch ahead of `skip_reason`, so superseded
   rows were excluded but never counted in `protected_event_count` — 5 of 6
   reasons counted. Removed; `skip_reason` is again the single expression, as
   Desired Behavior §3 requires.
3. **Re-persisting an observed pair could destroy a live suppression.** A pair
   already confirmed, or one an operator had decided, was re-written on every
   run. With `auto_apply` off for that run it would store a NULL
   `non_corroborating_count_at_review` over a live one — so merely toggling the
   auto-apply flag off for one run would silently re-open every previously
   confirmed pair. `PairOutcome.persist=False` now marks an observed pair and
   nothing is written.
4. **Shadow mode misreported itself.** `suppressed` was set from the verdict
   alone, so in shadow mode a fresh consensus confirm reported
   `suppressed: true` in the very run report an operator reads before enabling
   auto-apply. Now gated on `auto_apply` — but only for a fresh consensus; a
   pair already suppressed really is suppressed whatever the flag now says.
5. **`--consensus-k 1` would have re-created the single-call auto-apply this
   plan exists to prevent.** Added `MIN_REVIEW_CONSENSUS_K = 5` (the smallest K
   the production measurement actually exercised): below it a verdict is
   recorded but can never suppress a flag. Deliberately a downgrade to
   "record, never act", not a hard failure, so small-K shadow runs stay cheap.

### Tests

- New BDD: `tests/bdd/enrichment/agentic-venue-resolution-fallback.feature` —
  **23 scenarios / 171 steps**, all passing, `@wip` removed. Backed by
  `tests/fixtures/venue_link_audit_reviewer/corpus.json` + `MANIFEST.md`, 7
  cases sourced per-item to the 2026-09-14 read-only production reads.
- New pytest: 4 modules, **265 cases** — validator (every rejection reason,
  incl. case- and accent-sensitivity), pure core, service, script, DAO parity.
- Full suites green: **`make test-unit` 4856 passed / 7 skipped**;
  **`make test-bdd` 130 features / 1637 scenarios / 10507 steps, 0 failed**.
- Two step texts were reworded for collisions in the shared ~130-file step
  namespace (`the run does not fail` → `the reviewer run does not fail`;
  `no model call is made` → `no model call is charged for any pair`), per this
  repo's reword-never-dispatch-patch rule.
- **Mutation check.** Because the production modules were written before the
  step definitions existed, the suite was verified to actually bite: relaxing
  unanimity to a majority (3 failures), removing the multi-mapped gate (1),
  and ignoring an operator rejection in the suppression rule (2) each fail the
  new tests. All mutations reverted.

### Acceptance criteria — status

- [x] Both flags `False` → zero model calls, zero rows, zero response-byte change
- [x] Reaches flagged pairs from `collect_venue_link_audit` alone, no extraction run
- [x] Street reaches the model; a venue with no address row degrades rather than fails
- [x] Consensus `confirm` suppresses and writes exactly one row; **no event row is written by any path in this plan**
- [x] Consensus `contradict` leaves the pair flagged
- [x] Multi-mapped handle never auto-applied, regardless of agreement
- [x] Every pair *decided* has a review row (see note below)
- [x] Operator can list and accept/reject; a rejection is permanent
- [x] `empty_response` is distinct from `validator_rejected`
- [x] No `@wip` tag remains
- [x] `venue_link_audit.py`, `event_venue_resolution.py`, `event_venue_advisor*.py` and `openai_event_extraction_client.py` byte-identical to `408da88`

Refinement to one criterion: "every pair the pass looked at has a review row"
holds per **decision**, not per **look**. A pair the run only observed — already
confirmed and still in force, or operator-decided — writes no row, by defect
fix 3 above. Its prior row is still there and still listed; it is simply not
rewritten.

### Still outstanding

- The first production run should be `scripts/review_venue_link_audit.py`
  **dry-run**, with both flags still `False`, to confirm the verdicts reproduce
  §F's table against live data. **Not done in this session: AWS SSO expired**,
  so no read-only production verification was possible after implementation
  began. This is the one Manual/integration check from the plan that remains
  unexecuted.
- Cron (Open Question 3) remains deliberately out of scope, to be designed from
  the deployed system's real behaviour.
