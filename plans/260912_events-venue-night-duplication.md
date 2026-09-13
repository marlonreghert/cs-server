# Events venue-night duplication — attribution first, then a measured dedup bar, then a one-off repair

## Branch
fix/events-venue-night-duplication

## Goal

Stop the Eventos tab showing several cards that all describe one venue on one
night, and stop that class of row re-accumulating.

Three independent defects produce that one symptom (RCA:
`vibesense_wrapper/plans/260912_events-venue-night-duplication-rca.md`), and
this plan fixes all three plus the two things the RCA proves are missing around
them: a **historical** repair of the rows already stored wrong, and **operator
visibility** into the refusals, which today leave no trace at all.

Every behaviour change in this plan ships **behind its own admin-config gate,
defaulting to the current behaviour**, because `event_dedup_auto_merge_enabled`
is **already `true` in production** (RCA, set 2026-08-14). The safety property
`260812_event-dedup-fuzzy-title.md` §C bought by shipping the auto band
disabled has already been spent: any widening of the candidate window or
lowering of a threshold that merges on deploy would begin mutating the live
corpus on the very next crawl. **The deploy of this branch must provably change
no row.** Every mutation is an explicit, separately-reviewed operator act
afterwards.

## Non-goals

- **Changing `compute_event_identity`, `compute_source_event_key` or
  `normalize_title`.** `260812_event-dedup-fuzzy-title.md` §A is the constraint
  this whole design is shaped around: migrations `0025_multi_event_posts` and
  `0026_event_sources` call those functions directly against real rows, so a
  hash that moves orphans every operator confirmation and turns the next run
  into a duplicate generator. Defect 3 is therefore fixed at the **candidate
  window**, never at identity.
- **Changing `expand_occurrences`' serving behaviour**
  (`app/services/event_occurrences.py:115-197`). Re-deriving a recurring row's
  served days from its weekday pattern and discarding its stored date is
  deliberate and load-bearing — its own docstring at `:141-146` says so, and
  `is_selectable` (`app/services/event_projection_selection.py:113-117`) bounds
  it by source freshness instead. Nothing here touches either.
- **Unifying `MERGE_MODE_DELETE` and `MERGE_MODE_SUPERSEDE`**
  (`app/services/event_merge.py:435-472`). The asymmetry is deliberate —
  `0026_event_sources`' historical replay depends on the hard delete
  (`260812` §E). Do not tidy it as a side effect.
- **An LLM anywhere in a merge/no-merge decision.** See "Why the LLM title
  selection does not contradict `260812` §C" below; the decision stays fully
  rule-based, deterministic and unit-testable. Only a **display string** is
  ever chosen by a model, and only after a merge has already been decided.
- **Cross-venue merging.** Two rows at two venues are two events
  (`260812` Non-goals). Defect 2 changes *which* venue a row belongs to; it
  never merges across two.
- **Any vibes_bot or mobile change.** The RCA is explicit that the fix belongs
  entirely in cs-server. The chosen display title reaches the app through the
  **existing** `title` field of the serving projection
  (`app/services/redis_projection_service.py:546`) — no new app-facing field,
  nothing removed, released clients unaffected.
- **Re-attributing a venue's own post on a fuzzy NAME match.**
  `260806_venue-post-multi-event.md` §D forbids exactly that, for exactly the
  right reason. See §C for how this plan stays inside it.
- **Recurring-vs-one-off collision detection.** Defect 3 is scoped to
  recurring↔recurring. A weekly row and a one-off row that collide on one
  expanded date are a real but unmeasured case (the RCA measured only the
  recurring pair), and pairing them risks a one-off being absorbed into a
  weekly identity. Listed as future work, not attempted here.
- **The two side issues the RCA parks** (an implausible `16H57` extracted time;
  a weekly programme resolving onto a single date). Both are date-resolution
  questions, not merge questions.

## Evidence

Every citation below was re-read from this worktree at commit `52945f4`.

### Defect 1 — the refusal bar is tuned for a different shape of duplicate

- `compute_event_identity` (`app/services/event_merge.py:201-209`) keys on
  `(venue_id, starts_at.date(), normalize_title(title))`. Different titles
  never share an identity.
- The fuzzy second pass is a **set predicate, not a score**:
  `band_for_distinctive_sets` (`app/services/event_dedup.py:301-313`) returns
  `auto` only when one distinctive set is a subset of the other, `suggest` on a
  non-containing intersection, and refuses on disjoint-or-empty.
- The independent lineup signal `lineup_reaches_auto`
  (`app/services/event_dedup.py:332-340`) needs
  `DEFAULT_LINEUP_THRESHOLD = 2` (`:82`) shared normalised names.
- A refused pair records **nothing**: `_run_pairwise_pass`
  (`app/services/event_merge.py:999-1006`) calls `_observe_title_refusal`
  (`:892-907`) which only increments
  `EVENT_MERGE_TOTAL{identity="title", outcome="refused_disjoint"|
  "refused_no_distinctive_tokens"}` and returns. No row, no queue entry, no
  operator-visible artefact. The RCA measured `refused_disjoint=2793` against
  `merged=21` in prod — a ~133:1 ratio nobody could see.
- The false-positive corpus that set the bar is real and is in this repo:
  `260812_event-dedup-fuzzy-title.md` Evidence names
  `Ação Leitura … Marcelino Freire` / `… Jeferson Tenório`,
  three `Oficina …` workshops, `Férias Amigos Park — Semana 1..4`, and —
  decisively for this plan — **`Bolinha do Cavaco` / `JB do Cavaco` at Casanova
  Ecobar: two different acts, one night, deliberately kept apart.** That is the
  same shape as the Club Metrópole cluster the operator now wants collapsed.

### Defect 2 — a venue post's own `location_text` is discarded

- `EventExtractionService._extract_one` builds a fixed closure that returns the
  crawled handle's single mapped venue for every event the post yields:
  `app/services/event_extraction_service.py:1212-1216`, whose body is literally
  `return {"venue_id": venue_id}, None`, under a comment at `:1203-1211` citing
  `260806_venue-post-multi-event.md` §D.
- The multi-venue ladder already exists and is already wired — but only on the
  `len(venue_ids) == 1` **else** branch: `:747-757` sets
  `attribute_fn = None` for a single-venue handle, and `:809-824` builds
  `build_location_text_attribute_fn` per post only when `venue_id is None`.
- The ladder's **rung 3** is exactly the signal the BeerDock case needs:
  `_neighbourhood_match_candidates`
  (`app/services/event_venue_resolution.py:383-407`) matches `location_text`
  by folded substring containment against each **sibling candidate's ADDRESS**,
  and its own docstring says `"CASA FORTE" names no venue by NAME but is
  decisive against an address`. It is gated at `:528` on
  `same_account_venues and len(same_account_venues) >= 2`.
- Rung 1 (`:484-496`) resolves an `@`-mention of a known venue handle found in
  the event's **own** `location_text` — an identity, score 1.0. Rung 4 (name
  match, `:542-561`) is the fuzzy rung `260806` §D refuses, and it is already
  hardened against `@handle` text (`_strip_handles_for_name_match`, `:280`).
- A second frozen copy of each event's `location_text` survives on the source
  row: `list_event_sources` selects `raw_extraction`
  (`app/dao/rds_venue_store.py:979-993`), which is what
  `scripts/backfill_event_venue_links.py:150-164` (`_location_text_input`)
  already reads — a precedent for repairing attribution with no model call, no
  S3 read and no Apify call.

### Defect 3 — a recurring row's stored date is not a date it serves

- `expand_occurrences` (`app/services/event_occurrences.py:115-197`) uses the
  stored `starts_at` only for its **clock time** once `is_recurring` is true
  (`:165-166`), regenerating days from `weekdays_from_recurrence_text`
  (`app/services/event_date_resolver.py:418-432`) over
  `[nightlife_date(now), local_date + horizon_days]` (`:178-196`).
- Both merge gates key on that same stored `starts_at`: identity at
  `event_merge.py:201-209`, and the dedup candidate window `in_candidate_window`
  (`app/services/event_dedup.py:352-368`), which matches only on the same
  Recife local date or a `window_hours` gap.
- So two rows storing the same weekly night 21 days apart are never compared,
  yet expand onto the same served dates. The RCA confirmed this on two live
  `Aula de FORRÓ na Sala de Reboco` rows, both `recurrence_text = 'Toda QUARTA'`.

### What the merge layer already gives us, unchanged

- Reversibility: `_finish_absorption(mode="supersede")`
  (`event_merge.py:439-472`) moves the absorbed row to `STATUS_SUPERSEDED` and
  records `superseded_by`; `reverse_title_similarity_merge` (`:1264-1291`)
  replays it precisely from `moved_source_ids`.
- The audit trail: `events.event_merge_suggestion`
  (`migrations/versions/0039_event_merge_suggestions.py`), written for both
  bands, surfaced on the review queue as `ReviewQueueItemOut.merge_suggestions`
  (`app/routers/admin_events_router.py:483-485`, built by
  `_merge_suggestions_for`, `:496-510`).
- One predicate, two callers: `scripts/measure_event_dedup.py` imports the same
  `evaluate_pair`/`in_candidate_window` and calls `run_title_similarity_pass`
  itself for `--apply` (`scripts/measure_event_dedup.py:149-219`, `:255-285`).
- Config is runtime-editable and validated on write and on read: keys and
  validators in `app/services/event_dedup.py:63-160`, registered in
  `app/container.py:691-696`, served by `PUT /admin/config/{key}`.
- Protections that must keep holding: `_is_protected`
  (`event_merge.py:262-266`), `_edge_blocked_for_absorption` (`:832-856`),
  `choose_canonical` returning `None` for two protected members (`:269-277`).

### The measurement script does **not** yet expose the knob the RCA asks for

`measure()` takes a `config` parameter (`scripts/measure_event_dedup.py:149`)
so a threshold sweep is possible **in Python**, but `main()` (`:288-316`) only
parses `--apply` and `--since-venue-id`. Re-measuring at
`lineup_threshold=1` from the CLI needs a flag that does not exist today. That
is Phase B, not an assumption.

### Two facts that shape every gate below

- **Auto-merge is already on in prod.** Anything that widens the bar merges on
  the next crawl, unattended. Hence a per-signal flag, each defaulting to
  today's behaviour.
- **A non-empty `review_reason` withholds auto-accept.** `is_clean_extraction`
  (`app/services/event_reconciliation.py:214-246`) returns `False` on any
  review reason, and `is_selectable`
  (`app/services/event_projection_selection.py:101-108`) only serves
  `accepted`/`confirmed`. So writing a new review reason on a disputed row
  **removes it from serving**. That is a real behaviour change and gets its own
  flag (§C), not a free ride on a detection feature.

## Current Behavior

- Five posts about five acts at one club on one Saturday become five served
  rows. Nine of the ten pairs are refused as disjoint and recorded nowhere; the
  three that share one performer sit one below the auto floor.
- Every event a single-venue handle's post yields is attributed to that handle's
  mapped venue, whatever the event's own `location_text` says — so a chain
  posting three branches from one account files all three at one branch, and the
  app shows two "duplicates" that are two different bars.
- Two posts about the same weekly night, stored three weeks apart, are never
  evaluated against each other and both expand onto the same served dates.
- When a merge does happen, the surviving title is whichever source
  `merge_event_fields` (`event_merge.py:378-412`) last saw — an arbitrary pick
  for display purposes.
- An operator has no way to see refused pairs, the size of the duplicate
  backlog, or which accounts are posting about venues the catalog does not carry.

## Desired Behavior

1. Attribute an event to the venue **its own text names** when that text is
   identity-grade evidence for a different known venue, and refuse-and-flag
   (never guess) when the text disagrees with the mapped venue but names no
   venue we carry.
2. Treat two recurring rows at one venue whose weekday patterns intersect as
   candidates for the same night, whatever their stored dates say.
3. Decide the dedup bar from a **measurement against the post-repair corpus**,
   at thresholds 1, 2 and 3, with every new auto pair reproduced as a pinned
   unit test before the bar moves.
4. Collapse a venue-night to one listing **only where an operator has said that
   venue runs one night rather than a programme** — a deterministic, per-venue,
   empty-by-default policy, never a corpus-wide rule.
5. Repair the rows already stored wrong, once, idempotently, reversibly,
   off-peak, with a blast-radius guard and a before/after count.
6. Choose a merged listing's display title deterministically when one source
   title obviously wins, and with exactly **one LLM call per merge group** when
   none does — never per pair, never per post, never in control flow.
7. Surface the refused-pair backlog, the venue-night duplicate backlog and the
   attribution-dispute backlog where an operator can act on them.
8. Change no stored row on deploy.

## Implementation Approach

One commit per phase on a single branch and PR, per the operator's standing
preference for phased multi-defect fixes.

### Sequencing, and why

**A. Backlog instrumentation lands first (read-only).** Every later phase is
judged on a before/after number, and the instrument has to exist before the
"before". It also gives the historical repair its own success criterion.

**B. Measurement tooling.** The knobs that let the script answer questions
instead of restating its defaults.

**C. Defect 2 (attribution) before Defect 1 (dedup bar).** The RCA's own
sequencing note, and `260812`'s Manual-checks entry says the same thing in more
detail: `venue_id` is half the candidate window, so a bar tuned against today's
corpus is tuned against a corpus about to move. Repairing attribution both
**removes false neighbours** (20 Casa Forte rows leaving Boa Viagem) **and
creates true ones** (those rows arriving somewhere, or detaching).

**D. Defect 3 can be *implemented* independently but must not be *measured or
swept* before Defect 2.** Its code touches a different function
(`in_candidate_window`) and a different test file, so commit order is free. Its
**activation** is not free, for two reasons:

- It strictly widens the candidate set, and a widened window over a
  mis-attributed corpus is exactly how a Casa Forte weekly night gets absorbed
  into a Boa Viagem weekly night. Same hazard as Defect 1, same remedy: after
  the repair.
- More importantly, and easy to get backwards: **Defect 1's measurement must be
  run with Defect 3's widened window already enabled**, or the threshold is
  chosen against a narrower candidate set than production will actually use.
  So D's code must land *before* E's measurement, while D's sweep must run
  *after* C's repair. Both constraints are satisfied by the phase order below.

**E. Defect 1's bar.** Decided by measurement, not by this document.

**F. The historical repair**, in the order the scripts' own docstrings demand:
attribution repair → re-measure → dedup sweep.

**G. Display titles**, last, because a title chosen for a merge group that is
about to change membership is wasted work.

### A. Backlog instrumentation (read-only, no behaviour change)

New pure module `app/services/event_dedup_backlog.py` — a function over already
-fetched rows, no DAO calls, in the same "every function here is PURE" posture
as `app/services/event_dedup.py`'s own module docstring. It computes, for the
live `post_type == "event"`, non-superseded, venue-linked corpus:

- **venue-night groups** — `(venue_id, Recife local date)` groups with more than
  one row, the excess row count, and per group the rows' titles and
  `location_text` values. This is the RCA's own headline measure (55 groups /
  93 excess rows on 2026-09-12/13) turned into a standing number.
- **refused same-night pairs** — pairs inside the candidate window whose
  `evaluate_pair` returns `None`, split by `refused_disjoint` /
  `refused_no_distinctive_tokens`. The 2793-refusal backlog, made legible as a
  *current* population rather than a cumulative counter.
- **pending suggestions** — `decision='pending'` count.
- **attribution disputes** — `source_handle` → the distinct `location_text`
  values its rows carry → row counts, and whether each `location_text` resolves
  to a catalog venue. This is the venue-acquisition backlog: the accounts whose
  branches we do not carry.

Surfaced two ways, both following existing precedent:

- **Gauge** `EVENT_DEDUP_BACKLOG{measure}` in `app/metrics.py`, set from a full
  scan at the end of an extraction run — the exact shape of
  `update_events_gauge` (`app/services/event_reconciliation.py:1055-1093`) and
  `EVENT_REVIEW_QUEUE_DEPTH.set(...)`
  (`app/services/promoter_crawl_service.py:867`). `measure` label values are a
  small fixed set: `venue_night_groups`, `venue_night_excess_rows`,
  `refused_disjoint_pairs`, `refused_no_distinctive_tokens_pairs`,
  `pending_suggestions`, `attribution_disputed_rows`. Bounded cardinality, per
  `app/metrics.py`'s own stated rule. There is no collect-on-scrape collector in
  this repo; this must be pushed from a job.
- **Admin route** `GET /admin/events/dedup-backlog` on
  `app/routers/admin_events_router.py`, modelled on
  `GET /admin/events/targeting`
  (`app/routers/admin_trigger_router.py:1594-1600`) — a report-only aggregate
  with `limit`/`offset`, returning the group and dispute detail an operator
  needs to decide which venues belong in §E's single-night list and which
  branches to add to the catalog. Additive; nothing existing changes.

### B. Measurement tooling (`scripts/measure_event_dedup.py`)

Additive CLI flags only; the default invocation stays byte-identical in
behaviour:

- `--lineup-threshold N` — feeds `measure()`'s existing `config` parameter and
  `sweep()`'s forced config. This is the RCA's "cheapest experiment first".
- `--recurring-window` / `--no-recurring-window` — measure with §D's widened
  window on or off, so the threshold question is asked against the window
  production will use.
- `--single-night-venue VENUE_ID` (repeatable) — measure §E's policy for named
  venues without writing the admin-config key.
- `--max-auto-pairs N` — **the blast-radius guard**. In `--apply` mode, refuse
  to write anything and exit non-zero when the dry-run report's auto-pair count
  exceeds `N`. This is what makes an unattended `--apply` safe: the sweep stops
  on a surprise instead of merging its way through one.
- `--report-json PATH` — write the full report (counts plus every auto and
  suggest pair, by title) as JSON, so a before/after diff is mechanical rather
  than a log read.

The script must keep its existing discipline verbatim: dry-run default,
read-only dry run, the live `admin_config:event_dedup_auto_merge_enabled` key
never read or written, `--apply` re-measuring afterwards and exiting non-zero if
any auto pair remains, idempotent, resumable via `--since-venue-id`.

### C. Defect 2 — when the event's own text disagrees with the mapped venue

**What "disagreement" means, and why not string comparison.** Comparing
`location_text` to the mapped venue's name is the obvious rule and it is wrong:
the values are free text written for humans (`CASA FORTE`, `@beerdock.cf`,
`Rua da Aurora, 123`, `nossa unidade de Boa Viagem`), and a mismatch against a
venue name carries no information at all — the majority of correct rows would
"mismatch". The only reliable evidence is the evidence the resolution ladder
already grades, and this plan reuses it rather than inventing a second notion:

> **Trigger.** For an event from a **single-venue handle** whose own
> `location_text` is non-empty, run the existing ladder
> (`resolve_event_venue`, `app/services/event_venue_resolution.py:453-631`)
> with `venues` = the whole servable catalog, `handle_index` = the reverse
> handle index, and `same_account_venues` = the venues that share this venue's
> **brand root** (see below). The event's attribution is **disputed** when, and
> only when, the ladder returns `RESOLUTION_AUTO` for a `venue_id` **different
> from the mapped one**, via a method in
> `{METHOD_HANDLE_MENTION, METHOD_LOCATION_TAG, METHOD_NEIGHBOURHOOD_MATCH}`.
> `METHOD_NAME_MATCH` and `METHOD_CAPTION_HANDLE_MENTION` **never** trigger a
> dispute.

That exclusion is the whole reason this does not contradict
`260806_venue-post-multi-event.md` §D. §D's objection is specifically to
re-attributing a venue's own post **on a fuzzy name match** — rung 4, the
scored rung, the one that produced the `@mahalilacafe` → "Maria Café" links
`260813_handle-attribution-hardening.md` was written to kill. Rungs 1, 2 and 3
are not scores: rung 1 is an `@`-handle identity the model read out of *this
event's own text*, rung 2 is Instagram's own place database, and rung 3 is a
folded substring of the event's own text inside a *sibling branch's address*,
bounded to a handful of candidates. §D also assumed the premise "a handle really
does belong to one venue", which is exactly the premise that fails here. The
rule below keeps §D's ban intact and narrows the exception to the case §D never
contemplated.

**Brand root**, for rung 3's bounded candidate set: the venues whose
`venue_name` shares a normalised leading token run with the mapped venue's name
(`BeerDock Boa Viagem` / `BeerDock Casa Forte` / `BeerDock Madalena`), computed
with the existing `normalize_title` tokenisation `event_dedup.venue_name_tokens`
already uses (`app/services/event_dedup.py:260-268`) — never a new similarity
function, and never the whole catalog. When the brand root yields fewer than two
venues, rung 3 does not run (its own `len(same_account_venues) >= 2` gate,
`event_venue_resolution.py:528`), and the dispute can then only come from rung 1
or rung 2.

**Two outcomes, two gates.**

- **Dispute with a resolvable target** (the ladder names a different catalog
  venue): behaviour is controlled by admin config
  `event_attribution_dispute_action`, default **`flag`**.
  - `flag` (default, ships this way): keep the mapped `venue_id` exactly as
    today, append review reason `location_text_disputes_venue`, count
    `EVENT_VENUE_LINK_TOTAL{method=..., result="disputed"}`, and record the
    proposed target in the event's link candidates via the ladder's existing
    `_on_persisted` callback (`event_venue_resolution.py:721-733`) so the
    operator sees a ranked candidate in the review queue and can accept it with
    the **existing** `POST /admin/events/{event_id}/link` action. No new admin
    verb, no new merge path.
  - `reattribute`: adopt the ladder's answer, writing
    `location_resolution`/`location_confidence`/`linked_by`/`linked_at`
    exactly as `build_location_text_attribute_fn` already does
    (`:738-745`).
- **Dispute with no resolvable target** (the event's own text names a specific
  `@handle` we do not carry — the ladder's `METHOD_VENUE_NOT_IN_CATALOG`,
  `event_venue_resolution.py:435-450`): never re-attribute (there is nowhere to
  attribute it to) and never silently keep serving it at a venue we now have
  evidence is wrong. Record review reason `location_text_disputes_venue` and
  leave `venue_id` alone.

**The review reason withholds auto-accept, and that is the point.** Because
`is_clean_extraction` refuses any non-empty `review_reason`
(`event_reconciliation.py:214-246`), a newly-extracted disputed row lands
`pending_review` and is therefore not served
(`event_projection_selection.py:103`). For a chain like BeerDock that is the
correct answer — we would rather show nothing than show a Casa Forte party at
Boa Viagem — but it is a real withdrawal of content, so it is gated too:
`event_attribution_dispute_withhold_enabled`, default **`false`**. With it
false, the dispute is recorded on the row and in the metric and the backlog
report, and the row's status is computed exactly as today.

All three flags are ordinary admin config, validated on write and on read
through the same `_load_validated_config` path
(`app/services/event_dedup.py:172-206`) and registered alongside the existing
`event_dedup_*` validators in `app/container.py:691-696` — never coerced, so a
stored `"false"` can never read back as `True`.

**Historical half** — extend `scripts/backfill_event_venue_links.py` rather than
write a second sweep. Today it selects rows with `linked_by = 'handle_mention'`.
Add a second selection mode, `--mode disputed-location-text`, that selects rows
whose `linked_by` indicates the fixed venue-post attribution and whose stored
`raw_extraction["location_text"]` (read through the script's existing
`_location_text_input`, `:150-164`, so an operator's correction still wins)
triggers the dispute rule above. It keeps every existing property: dry-run
default, `--apply` to write, idempotent, resumable via `--since-id`, no network
client importable, `operator_edited_fields` always wins, and — critically —
**it still never calls `merge_touched_events`** (`scripts/backfill_event_venue_links.py:20-25`).
Repaired rows are merged by the dedup sweep in Phase F, as a separate, separately
-guarded step.

### D. Defect 3 — a recurring-aware candidate window

`in_candidate_window` (`app/services/event_dedup.py:352-368`) stays **exactly as
it is**, signature and behaviour, so its existing unit tests keep meaning what
they mean. Add a sibling:

```
in_candidate_window_for_rows(a, b, *, window_hours, recurring_window_enabled)
  -> in_candidate_window(a.starts_at, b.starts_at, window_hours=window_hours)
     or (recurring_window_enabled
         and bool(a.is_recurring) and bool(b.is_recurring)
         and (weekdays_from_recurrence_text(a.recurrence_text) or frozenset())
             & (weekdays_from_recurrence_text(b.recurrence_text) or frozenset()))
```

Three deliberate choices, each of which a future reader will want to undo:

- **`weekdays_from_recurrence_text`, imported, never re-parsed.** It is the same
  function `expand_occurrences` uses (`event_occurrences.py:35, :161`). If this
  window and the expansion could ever disagree about which nights a row serves,
  the window would stop being evidence — the identical argument
  `event_dedup.py`'s docstring makes about `evaluate_pair` having one caller for
  measurement and one for merging.
- **Intersection, not equality, of weekday sets.** `Toda QUARTA` and
  `Quartas e sextas` collide on every Wednesday they both serve. Requiring equal
  sets would miss exactly the collision this exists to catch. Intersection is
  also the faithful analogue of the existing rule: the same-local-date branch
  already admits two rows on one date regardless of their clock times.
- **No clock-time condition.** Same reason. The `window_hours` half exists to
  bridge a *local-date boundary*, not to separate a 19:00 show from a 23:00
  party on one date — the existing rule does not do that either, and the
  distinctive-set predicate is what separates them.

Both `_run_pairwise_pass` (`app/services/event_merge.py:999-1002`) and
`scripts/measure_event_dedup.py:186-189` switch to the new function; nothing
else calls the old one. New admin config
`event_dedup_recurring_window_enabled`, default **`false`**, carried on
`DedupConfig` alongside the existing six keys, so the deploy widens nothing.

A merged pair of recurring rows is safe in a way worth stating: because
`expand_occurrences` ignores a recurring row's stored date entirely
(`event_occurrences.py:141-146`), the surviving row's `starts_at` **date** has
no effect on which nights it serves — only its clock time does, and
`merge_event_fields` already carries `time_known` alongside whichever
`starts_at` wins (`event_merge.py:387-399`). The merge therefore cannot move a
weekly night.

### E. Defect 1 — a measured bar, and the one thing a threshold cannot do

**State the arithmetic plainly, because it changes what "fixed" means.**
Retuning `event_dedup_lineup_threshold` from 2 to 1 cannot collapse the Club
Metrópole cluster. The RCA measured that cluster as five posts, ten pairs: nine
have disjoint distinctive-token sets and are refused outright, and the three
pairs that share a performer share exactly one (`@neguindabasersv`). Threshold 1
unions those three pairs — the five rows become **three**, not one. `ROWKA` and
`VITINHO POLÊMICO` share no token and no performer with anything; **no threshold
on either existing signal reaches them.** Anyone who reads "retune the threshold"
as "the operator's report is fixed" will be wrong, and will discover it in prod.

So this phase has two independent parts.

**E1 — the threshold, decided by measurement.** The plan does **not** change the
shipped default. It ships the means to decide and a decision rule execution must
follow:

1. Run `scripts/measure_event_dedup.py --report-json` at
   `--lineup-threshold 1|2|3`, with `--recurring-window`, against the
   **post-Phase-F-attribution-repair** corpus (read-only; see Verification).
2. The threshold moves to 1 only if **every** auto pair that appears at 1 and
   not at 2 is individually reviewed, is a genuine duplicate, and is added as a
   pinned pair to `tests/test_event_dedup.py`'s existing production-string band
   tests. Any pair that cannot be explained blocks the change — the same
   "a single unexplained auto pair blocks the merge" rule `260812`'s Manual
   checks already state.
3. If it moves, it moves by an admin-config edit
   (`PUT /admin/config/event_dedup_lineup_threshold`), not a deploy. It is
   already runtime-configurable (`app/services/event_dedup.py:81-82`,
   `app/container.py:693`).

On the substance the RCA asks about: `260812` §B2's stated justification for the
floor of 2 was *"a single shared name is a resident DJ or house band playing the
venue on both of two genuinely different **nights** — that is why the window
alone is not enough."* The candidate window as shipped is same-Recife-local-date
or ≤8h (`event_dedup.py:352-368`), so the cross-night case that justified the
floor is **already excluded by the window**. The residual risk at 1 is narrower:
one resident performing at two genuinely different events at one venue on one
night. That is plausible and it is exactly what the measurement is for. Phase D
does not re-open the cross-night risk either — two weekly nights on different
weekdays have disjoint weekday sets and never become candidates. **Measure it;
do not assume it either way.**

**E2 — the venue-night policy, which is what the operator actually asked for.**
New admin config `event_dedup_single_night_venues`: a list of `venue_id`s,
default **`[]`**. For a venue on that list, two live `post_type == "event"` rows
in the same candidate window auto-merge regardless of title or lineup — subject,
without exception, to every guard the auto band already applies:
`_is_protected`, `_edge_blocked_for_absorption`'s operator-edited `title`/
`venue_id` refusals, `choose_canonical` returning `None` for two protected
members, the non-event guard in `_dedup_candidate_events`
(`event_merge.py:793-810`), and `mode="supersede"` reversibility. It is
expressed as a third `reasons` token (`single_night_venue`) on the existing
`PairDecision`, so it flows through the existing audit row, the existing metric
labels and the existing reversal path with no new machinery.

Why a per-venue list and not a rule: `260812`'s measured false-positive corpus
contains `Bolinha do Cavaco` / `JB do Cavaco` at Casanova Ecobar — two different
acts, one night, same venue, deliberately kept apart — which is the **same
shape** as Club Metrópole's five acts. There is no property of the rows that
tells the two situations apart; the difference is a fact about the venue. A
per-venue list is the only mechanism that respects both the operator's new ask
and the evidence that previously refused it, and it is fully deterministic and
unit-testable. It defaults empty, so the deploy changes nothing, and the Phase A
backlog report is exactly the instrument for choosing which venues go on it.

Merging is not lossy for this case: `union_lineup`/`union_attractions`
(`event_merge.py:401-407`) already fold every absorbed row's acts onto the
survivor, so five act-posts become one listing that names all five acts.

### F. The historical repair

Three steps, in this order, off-peak (03:00–05:00 Recife), each capturing a
report file before it writes. Nothing here is new sweep infrastructure — both
scripts already exist, are idempotent, are resumable and refuse to run in
`--apply` without an explicit flag.

1. **Attribution repair.** `python -m scripts.backfill_event_venue_links
   --mode disputed-location-text` (dry run) → review the per-venue before/after
   counts and the venue-acquisition backlog the script already prints → `--apply`.
2. **Re-measure.** `python -m scripts.measure_event_dedup --report-json
   <after-repair>.json --recurring-window --lineup-threshold <chosen>` — this is
   the corpus E1's decision is made against, and it is the "before" for step 3.
3. **Dedup sweep.** `python -m scripts.measure_event_dedup --apply
   --recurring-window --lineup-threshold <chosen> --single-night-venue <...>
   --max-auto-pairs <ceiling> --report-json <sweep>.json`. The ceiling is set
   from step 2's own auto-pair count plus a stated margin; if the sweep's own
   dry-run pass exceeds it, the script writes nothing and exits non-zero.

**The count must be re-measured, never hardcoded.** The RCA's 55 groups / 93
excess rows were measured on 2026-09-12/13 and will have moved by execution
time — every crawl adds rows and Phase F step 1 moves rows between venues before
step 3 ever runs. Acceptance is stated as a *reduction against the run's own
"before"*, not against 93.

**The live-identity break.** A title-similarity merge supersedes the absorbed
row, so its `occurrence_id` stops being projected on the next projector cycle
and `GET /events/{occurrence_id}` in vibes_bot 404s for any client holding a
stale list. This is pre-existing (`260812` §E chose `supersede` precisely so the
row survives) and this sweep exercises it harder than normal. Mitigation is
timing, not code: run off-peak, when the live-client population holding stale
occurrence ids is smallest, and let the app's own next list fetch resolve it.
An alias key mapping an absorbed occurrence id to its canonical is explicitly
**not** attempted — it would need a new Redis key family in cs-server and a
lookup in vibes_bot, which is a cross-repo change this plan's own non-goals
exclude. Recorded here so the next reader does not think it was missed.

**Reversal.** Every merge this sweep performs is per-pair reversible through
`POST /admin/events/{event_id}/reverse-merge` →
`reverse_title_similarity_merge` (`event_merge.py:1264-1291`), which replays
exactly the sources that moved. Attribution repair is **not** reversible beyond
the dry-run report; that report is the only record of previous `venue_id`s and
must be captured to a file, as the script's own docstring insists.

### G. Narrowly-scoped LLM title selection

**The structural finding first, because it determines the whole design.** The
merge path is **synchronous** — `merge_touched_events` → `run_title_similarity_pass`
→ `_absorb_title_similarity` (`event_merge.py:640-695`, `:910-960`) — and
`OpenAIEventExtractionClient` is **async** (`app/api/openai_event_extraction_client.py:709`,
`:753`). A title call cannot be made inside the merge without making the merge
async, which would be a large refactor of a path three migrations replay. That
constraint is a gift: it forces the model out of the decision path entirely.

**Where it runs.** A separate pass, after the merge, over the event ids a run
touched: `EventExtractionService.run()` is already `async` and already holds
`self.openai_client` (`app/services/event_extraction_service.py:598`, `:621`).
`_extract_one` accumulates its `touched_event_ids` (`:1218`, `:1235`) into a
run-level list; at the end of `run()` a new `EventDisplayTitleService` walks the
surviving canonical rows once. The historical equivalent is a new
`scripts/backfill_event_display_titles.py` following the two existing scripts'
discipline exactly (dry-run default, `--apply`, idempotent, resumable,
`--max-calls` cost ceiling). Promoter-only rows are out of scope: they are
hidden from serving by `hide_promoter_events`
(`app/services/redis_projection_service.py:378`), so the served corpus is fully
covered by the extraction path.

**The deterministic pre-check — "no single source title is an obvious canonical
pick".** Defined as a pure predicate on the group's **source** titles, which
survive the merge on `post_item_source.raw_extraction["title"]`
(`app/dao/rds_venue_store.py:979-993`), not on the single surviving `title`:

```
obvious_canonical_title(source_titles, venue_name, config) -> Optional[str]
  sets := { distinctive_set(t) for t in source_titles }          # event_dedup
  maximal := { t : distinctive_set(t) is not a PROPER subset of any other's }
  if every t in maximal has the same normalize_title(t):
      return the raw title of the oldest source among `maximal`   # obvious
  return None                                                     # ambiguous
```

This reuses `distinctive_set` and `normalize_title`
(`app/services/event_dedup.py:271-298`, `app/services/event_identity.py:32-48`)
— never a second notion of "same title" — and is symmetric, order-independent
and total. `Rodolpho` / `Rodolpho Produções` / `31º Rodolpho Produções` has a
unique maximum → **no call**. Club Metrópole's five acts have five mutually
non-comparable sets → **one call**. An empty distinctive set (a generic
`SEXTOU NO …` post) is a subset of everything and can never be the maximum, so
it never forces a call and never wins.

**Three further deterministic short-circuits, checked before the predicate:**
a group whose canonical has `title` in `operator_edited_fields` never calls (the
operator wrote that title); a group with one source never calls; a group already
carrying a `display_title` never calls.

**What the model is asked, and what is done with the answer.** One call per
merge **group**, reusing the existing client and prompt discipline — a new
method on `OpenAIEventExtractionClient` with its own `ENDPOINT` label
(`event_title_pick`) so `OPENAI_API_CALLS_TOTAL` /
`OPENAI_API_CALL_DURATION_SECONDS` / `OPENAI_TOKENS_TOTAL` separate it from
extraction spend, `response_format={"type": "json_object"}`, the same
`sampling_kwargs(self.model, 0.1)`, and a **text-only** call (no image, no S3
read) with a small `max_completion_tokens`. Input: the venue name, the local
date, and the group's source titles and lineups. Output:
`{"display_title": str}`.

The answer is then put through a **deterministic validation gate**, and this is
what keeps a non-deterministic value out of anything that matters:

- non-empty after `normalize_title`, and no longer than 120 characters;
- `distinctive_set(display_title)` must be a **subset of the union** of the
  group's source distinctive sets — the model may select, shorten or recombine
  what the posts said, and may never introduce a proper noun no post contained;
- on any failure — validation, API error, timeout, malformed JSON — write
  nothing, count `EVENT_DISPLAY_TITLE_TOTAL{outcome="rejected"|"error"}`, log
  the rejected string, and leave `display_title` NULL so the row serves its
  existing `title` exactly as today.

**Where it is stored, and why not in `title`.** New nullable column
`display_title` on `events.post_item` (migration `0046_event_display_title`),
added to `RdsVenueStore._EVENT_COLUMNS` (`app/dao/rds_venue_store.py:777-830`) —
that allowlist is what makes `update_event` actually reach a column, and its own
comment records that omitting a new column there makes the SQL store silently
no-op while the fake accepts it. The column is written **only** by this service
and the backfill script, is **never** part of `PROTECTABLE_EVENT_FIELDS`
(`app/services/event_reconciliation.py:266-289`), and is cleared to NULL by
`_finish_absorption` (`event_merge.py:439-472`) whenever a merge changes a
canonical's source membership, so a stale title cannot survive a group's growth.

Writing into `title` instead was considered and rejected: `title` is an input to
`compute_event_identity` (`event_merge.py:201-209`), so a synthesised title
would change which rows the *exact* identity pass considers siblings on the next
crawl, and a title no post contains would have a distinctive set that no future
post's title can be contained in — the merge layer would stop recognising its
own canonical. `display_title` is presentation only and touches no key.

**How it reaches the app with no cross-repo change.** The projection writes
`title=row.get("display_title") or row.get("title")` at
`app/services/redis_projection_service.py:546`. No new field on the
`EventOccurrence` contract, nothing removed, released clients see a better title
in the field they already read. The admin API gains `display_title` additively
on `EventOut` so an operator can see both.

**Why this does not contradict `260812_event-dedup-fuzzy-title.md` §C.** A
reviewer will flag it, so answer it head-on. §C's rejection has two stated
grounds, quoted from the plan: *"a suggestion that changes between runs re-opens
a decision the operator already closed"*, and *"a non-deterministic merge cannot
be unit-tested against the false-positive pairs above, which is the only
evidence that the bar is safe."* Both are arguments about the **merge decision**.
Neither applies here, on four counts:

1. **The decision is untouched.** Whether two rows merge is still
   `evaluate_pair` + `choose_canonical` + the guards — pure, deterministic,
   and still pinned by `tests/test_event_dedup.py`'s production-string band
   tests. The model is consulted strictly *after* a merge has already happened
   and can never cause, prevent or reverse one.
2. **Even the decision to CALL is deterministic.** `obvious_canonical_title` is
   a pure predicate over source titles, unit-tested on the production strings.
   Only the chosen string is non-deterministic.
3. **Nothing an operator closed is re-opened.** A model answer never becomes a
   suggestion, never re-enters the queue, and never overrides an
   operator-edited title (short-circuited before the call).
4. **The blast radius is one presentation field that tolerates variance.** A
   run-to-run difference changes the words on a card, not which rows exist, not
   what is served, not what is merged. The determinism argument
   `openai_event_extraction_client`'s own docstring makes for **dates** — that
   temporal arithmetic must be provable, not asserted — is about a value other
   logic computes on. No logic computes on `display_title`.

Cost is grounded in existing usage: cs-server already spends one vision call per
qualifying post. This adds one **text-only** call per ambiguous merge group,
which the RCA sizes at 55 groups corpus-wide today, and zero calls for groups
with an obvious title.

## Data, Config, And API Impact

- **Migration `0046_event_display_title`** — one nullable `display_title text`
  column on `events.post_item`. Additive, no back-fill, chains from
  `0045_venue_address_provenance`. Downgrade drops it; it is derived data.
  Must also be added to `RdsVenueStore._EVENT_COLUMNS` and to the fake store's
  merged view, or writes silently no-op in prod while passing every test.
- **Admin config (runtime, validated on write and on read), all defaulting to
  today's behaviour:**
  - `event_dedup_recurring_window_enabled` — bool, default `false`
  - `event_dedup_single_night_venues` — list of venue_id strings, default `[]`
  - `event_attribution_dispute_action` — `"flag"` (default) | `"reattribute"`
  - `event_attribution_dispute_withhold_enabled` — bool, default `false`
  - `event_display_title_enabled` — bool, default `false`
  - Existing `event_dedup_lineup_threshold` keeps its shipped default of 2;
    this plan does not change it, it only makes the decision measurable.
  - Each needs a validator in `app/services/event_dedup.py` (or, for the
    attribution keys, alongside them) and registration in
    `app/container.py:691-696`, plus the BDD environment's own validator dict.
- **Review reason vocabulary** — one new token, `location_text_disputes_venue`,
  joining the `"; "`-separated set the reconciler already folds. It withholds
  auto-accept by `is_clean_extraction`'s existing rule **only** when
  `event_attribution_dispute_withhold_enabled` is true.
- **Admin API (additive only — the console and the app are released clients and
  nothing may be removed):**
  - `GET /admin/events/dedup-backlog` — new report-only aggregate.
  - `EventOut` gains `display_title: Optional[str]`.
  - No new merge verb: a disputed attribution is accepted through the existing
    `POST /admin/events/{event_id}/link`.
- **Scripts:** new flags on `scripts/measure_event_dedup.py`; a new
  `--mode` selection on `scripts/backfill_event_venue_links.py`; new
  `scripts/backfill_event_display_titles.py`.
- **Serving:** the projection's existing `title` field becomes
  `display_title or title`. No contract change, no key-format change, no
  Redis migration.
- **Makefile:** every new pytest file must be added to `test-unit`'s hand-
  maintained file list, or it never runs.
- **Rollback:** revert the branch. Every flag defaults to today's behaviour, so
  a revert with flags untouched is a no-op on data. Merges already applied by
  the sweep are reversible per pair; attribution repairs are not reversible
  beyond the captured dry-run report.

## Error Handling And Observability

- **`EVENT_MERGE_TOTAL{identity="title"}`** gains outcome
  `merged_single_night_venue`, kept distinct from `merged` so §E2's policy can
  be watched separately from the title/lineup signals. The definition's inline
  outcome inventory (`app/metrics.py:1575-1596`) must be updated — a counter
  whose documented values are stale is how the next reader misreads a dashboard.
- **`EVENT_DEDUP_BACKLOG{measure}`** (new Gauge) — the six measures named in
  §A, pushed from the end of an extraction run in the shape of
  `update_events_gauge`. `venue_night_excess_rows` is the number this whole
  plan is judged on; it must fall after Phase F and must not climb afterwards.
- **`EVENT_VENUE_LINK_TOTAL`** gains `result="disputed"`, keeping the existing
  `method` label so an operator can tell a rung-1 identity dispute from a
  rung-3 neighbourhood dispute. Because a Prometheus series exists only after
  its first increment, the **absence** of `result="disputed"` after a crawl is
  itself the evidence that no post disagreed with its handle's venue.
- **`EVENT_DISPLAY_TITLE_TOTAL{outcome}`** (new Counter) — `obvious`,
  `llm_accepted`, `rejected`, `error`, `skipped_operator_edited`. A climbing
  `rejected` means the validation gate is doing its job and the prompt needs
  work; a climbing `error` is an availability problem, never a data problem,
  because the fallback writes nothing.
- **`OPENAI_API_CALLS_TOTAL{endpoint="event_title_pick"}`** and its token
  siblings — title-pick spend must never be conflated with extraction spend.
- **Logs:** every dispute logs the handle, the stored `location_text` and the
  proposed venue at info — this is the venue-acquisition backlog, the same
  purpose `_venue_not_in_catalog_result` (`event_venue_resolution.py:435-450`)
  already logs for. Every `single_night_venue` merge logs both titles and the
  canonical id, joining the existing auto-merge log line
  (`event_merge.py:953-959`). Never log raw model payloads.
- **Failure posture:** an unreadable admin-config key falls back to the shipped
  default and counts `EVENT_DEDUP_CONFIG_TYPE_FALLBACK_TOTAL{key}`
  (`app/services/event_dedup.py:204`) — never coerced. An OpenAI failure in the
  title pass never fails the extraction run; it is caught, counted and the row
  keeps its existing title. A backlog-scan failure never fails the crawl that
  triggered it.

## Test Plan

**Domain choice.** This repo's own convention puts merge, dedup, attribution and
backfill-script behaviour under `tests/bdd/enrichment/` — `event-dedup-fuzzy-title.feature`,
`one-event-many-posts.feature`, `merge-unresolved-into-resolved-sibling.feature`
and `backfill-misattributed-links.feature` all live there — so **enrichment is
the primary domain**, not persistence. Two further files carry the pieces that
genuinely belong elsewhere: operator-facing backlog visibility is observability,
and "the chosen title reaches the serving projection" can only be proved by
driving the real projector, whose harness lives in persistence (and whose own
feature file enforces that no scenario may hand-seed projection keys).

Feature file: `tests/bdd/enrichment/events-venue-night-duplication.feature`
Feature file: `tests/bdd/observability/events-venue-night-duplication-backlog.feature`
Feature file: `tests/bdd/persistence/events-venue-night-duplication-display-title.feature`

Every `Feature:` ships tagged `@wip`. `tests/bdd/environment.py:262-264` skips a
`@wip` feature, so **execution must remove the tag before driving red** — a
skipped feature is not a red BDD failure and does not satisfy this repo's BDD
policy.

### Scenarios — `tests/bdd/enrichment/events-venue-night-duplication.feature`

Attribution (Defect 2):
- Flag an event whose own location text names a different known venue by handle.
- Flag an event whose own location text names a sibling branch's neighbourhood.
- Re-attribute that event when the dispute action is set to reattribute.
- Keep the posting venue and change nothing when the dispute action is flag.
- Never dispute an attribution on a fuzzy name match alone.
- Never dispute an attribution on a caption mention alone.
- Record a venue-acquisition entry when the named branch is not in the catalog.
- Withhold a disputed event from serving only when withholding is enabled.
- Never dispute an event whose operator edited its venue.
- Leave a single-venue handle whose posts name no location exactly as today.
- Repair a stored mis-attribution through the backfill script's disputed mode.
- Change nothing on a second run of the disputed-mode backfill.
- Report every repair and write nothing when the disputed-mode backfill runs
  without apply.

Recurring window (Defect 3):
- Merge two weekly announcements of one night stored three weeks apart.
- Refuse to merge two weekly announcements on different weekdays.
- Merge two weekly announcements whose weekday sets overlap on one day.
- Refuse to widen the window at all while the recurring window is disabled.
- Refuse to pair a recurring row with a one-off row.
- Keep the surviving weekly row serving the same nights it served before.

Dedup bar (Defect 1):
- Collapse a venue-night to one listing for a venue on the single-night list.
- Leave a venue-night alone for a venue that is not on the list.
- Never absorb a confirmed row on a single-night venue.
- Never absorb a row whose operator edited its title on a single-night venue.
- Refuse to collapse two workshops at a venue that is not on the list.
- Merge a pair sharing one performer only when the threshold is one.
- Keep the exact-identity merge behaving exactly as it does today.

Historical sweep (Phase F):
- Sweep an existing cluster no crawl has touched.
- Write nothing and exit non-zero when the sweep exceeds its auto-pair ceiling.
- Change nothing on a second sweep.
- Reverse a swept merge and restore the absorbed row with its sources.
- Sweep only the auto band, never the suggest band.

### Scenarios — `tests/bdd/observability/events-venue-night-duplication-backlog.feature`

- Report every venue-night group holding more than one live row.
- Report the excess row count for the whole corpus.
- Report refused same-night pairs split by refusal reason.
- Report the accounts whose events name a location the catalog does not carry.
- Report an empty backlog when no venue-night holds more than one row.
- Publish the backlog measures as gauges after an extraction run.
- Exclude superseded rows from every backlog measure.
- Exclude non-event post types from every backlog measure.
- Show the backlog shrinking after a sweep absorbs a cluster.

### Scenarios — `tests/bdd/persistence/events-venue-night-duplication-display-title.feature`

- Serve the chosen display title in the projection's title field.
- Serve the stored title when no display title was chosen.
- Choose the containing title deterministically without calling the model.
- Call the model exactly once for a group with no obvious title.
- Never call the model for a group whose operator edited its title.
- Reject a model title that introduces a word no post contained.
- Keep serving the stored title when the model call fails.
- Clear a stale display title when a later merge grows the group.
- Change nothing while display-title selection is disabled.

### Pytest unit tests

House style (`tests/test_event_dedup.py` and siblings): open with the module
under test, cite this plan, and state the division of labour against the BDD
sibling. **Every new file must be added to the Makefile's `test-unit` list.**

- `tests/test_event_dedup_recurring_window.py` — `in_candidate_window_for_rows`:
  intersecting weekday sets include; disjoint exclude; unparseable
  `recurrence_text` on either side excludes; one side non-recurring excludes;
  the flag off reduces the function to `in_candidate_window` **exactly**;
  symmetry over `(a,b)`/`(b,a)`; and a guard asserting the weekday set comes
  from `weekdays_from_recurrence_text` and not a local re-parse.
- `tests/test_event_attribution_dispute.py` — the dispute predicate on bare
  dicts: rung-1 handle mention to a different venue disputes; rung-2 location
  tag disputes; rung-3 neighbourhood within a brand root disputes; rung-4 name
  match does **not**; rung-5 caption mention does **not**; the mapped venue's
  own name in `location_text` does not; empty `location_text` does not; an
  operator-edited `venue_id` never disputes; brand root with fewer than two
  members disables rung 3. Pinned against the RCA's real strings
  (`CASA FORTE`, `BeerDock Boa Viagem`).
- `tests/test_event_dedup_single_night.py` — the single-night policy: a listed
  venue merges a same-window pair with disjoint titles; an unlisted venue does
  not; the policy never bypasses `_is_protected`,
  `_edge_blocked_for_absorption` or the non-event guard; the emitted `reasons`
  token and metric label; determinism across two candidate orderings.
- `tests/test_event_display_title.py` — `obvious_canonical_title` pinned against
  the production strings: the Rodolpho family returns the containing title;
  Club Metrópole's five act titles return `None`; a group whose maximal members
  share one `normalize_title` returns it; an empty distinctive set never wins;
  order-independence. Then the validation gate: a subset title is accepted; a
  title introducing an unseen proper noun is rejected; an over-long title is
  rejected; a blank title is rejected; a rejected title leaves `display_title`
  NULL. Plus a wiring guard in the shape of
  `tests/test_event_extraction_prompt_kind.py` asserting the title prompt
  states "choose only from what these posts say".
- `tests/test_event_dedup_backlog.py` — the pure backlog function: group and
  excess-row arithmetic across a mixed corpus; superseded and non-event rows
  excluded; a venue-less row excluded; refusal split by reason; the dispute
  index keyed by handle; an empty corpus yields zeros, not an error.
- `tests/test_measure_event_dedup.py` (extend if present, else new) — the new
  flags: `--lineup-threshold` reaches `DedupConfig`; `--max-auto-pairs` refuses
  to apply and exits non-zero; `--report-json` round-trips; the default
  invocation is unchanged.
- **Regression guards, non-negotiable:** `compute_source_event_key` and
  `compute_event_identity` produce byte-identical output before and after this
  branch over the production title list — the existing guard in
  `tests/test_event_dedup.py`, extended with the new titles. `_finish_absorption`
  in `delete` mode still behaves exactly as today, so `0026`'s replay is
  provably untouched. `expand_occurrences` output is unchanged for every case in
  `tests/test_event_occurrences.py`.

### BDD exemptions

- `# bdd-exempt: migration 0046's own up/down round trip has no externally
  observable behaviour` — pinned instead by an offline SQL-inspection test in
  the shape of `tests/test_event_sources_migration.py`.
- `# bdd-exempt: adding display_title to RdsVenueStore._EVENT_COLUMNS and to the
  fake store's merged view is a persistence-allowlist wiring change with no
  behaviour of its own` — pinned by a DAO round-trip unit test, which is the
  only thing that catches the "SQL store silently no-ops while the fake accepts
  it" failure that allowlist's own comment warns about.
- `# bdd-exempt: the title-pick prompt's own text` — pinned by the wiring guard
  above, not by a scenario; BDD must not depend on a live model
  (CLAUDE.md's BDD policy forbids live OpenAI in BDD).

### Manual or integration checks

- `make test-unit`, `make test-bdd`, and `make test-feature FEATURE=<each new
  feature file>` all green, and CI's scratch-Postgres migrate step green.
- No BDD scenario may make a live OpenAI, Apify, S3 or BestTime call; the title
  pass is exercised through the existing `_FakeOpenAIClient` seam in
  `tests/bdd/steps/instagram_event_extraction_steps.py`.

## Verification (production, read-only unless stated)

Every step below is precise enough for an execution pass that holds prod access
to run it; none of it was attempted while writing this plan.

1. **Before-picture.** Deploy Phase A, then read
   `event_dedup_backlog{measure="venue_night_groups"}` and
   `{measure="venue_night_excess_rows"}` from the cs-server `/metrics`
   endpoint, and capture `GET /admin/events/dedup-backlog` to a file. This
   replaces the RCA's 55/93, which **will** have moved.
2. **Threshold measurement (read-only, the RCA's own "cheapest experiment
   first").** On the EC2 host, inside the cs-server container, run:
   `python -m scripts.measure_event_dedup --lineup-threshold 1 --recurring-window --report-json /app/reports/dedup_t1.json`
   and the same at `--lineup-threshold 2` and `3`. The script's dry-run path is
   strictly read-only, never reads or writes
   `admin_config:event_dedup_auto_merge_enabled`, and makes no external call.
   Ship the script to `/app`, not `/tmp`, and send the SSM command standalone.
   Diff `dedup_t1.json` against `dedup_t2.json` and enumerate every auto pair
   present only at 1 — that list is E1's decision input and each entry must
   become a pinned unit test before the threshold moves.
3. **Attribution repair.** Dry-run
   `scripts.backfill_event_venue_links --mode disputed-location-text`, capture
   the report to a file, review the per-venue before/after counts and the
   venue-acquisition backlog, then `--apply` off-peak. Verify the BeerDock case
   by name: the rows carrying `location_text = 'CASA FORTE'` must no longer be
   attributed to the Boa Viagem venue.
4. **Re-measure** after the repair — this is the corpus the bar is set against
   and the "before" for the sweep.
5. **Dedup sweep**, off-peak, with `--max-auto-pairs` set from step 4's own
   count. The script re-measures itself afterwards and exits non-zero if any
   auto pair remains.
6. **After-picture.** Re-read the two gauges and the admin backlog report.
   `venue_night_excess_rows` must be materially lower than step 1's value, and
   `venue_night_groups` must not have grown.
7. **Non-regression on serving.** Confirm the projected occurrence count did not
   collapse: read `events_projected_occurrences` before and after, and spot-check
   one absorbed pair's survivor through the app-facing list to confirm it still
   appears with its acts folded into `lineup`.
8. **Standing watch, one week.** `venue_night_excess_rows` must not climb back
   toward its step-1 value. If it does, the forward fix is not holding and the
   backlog report names the venues to look at.

## Acceptance Criteria

**Deploy safety**
- The deploy of this branch changes **no** stored row: every new flag defaults
  to today's behaviour, and `event_dedup_lineup_threshold`'s shipped default
  stays 2. Demonstrated by running the full pipeline against a seeded corpus
  with no config set and asserting byte-identical rows.
- `compute_source_event_key` and `compute_event_identity` produce identical
  output before and after this branch over the production title list.
- `_finish_absorption(mode="delete")` behaves exactly as today, so `0026`'s
  replay is provably untouched.
- `expand_occurrences` behaves exactly as today for every existing test case.

**Defect 2 — attribution**
- An event whose own `location_text` names a different known venue by `@handle`,
  by Instagram location tag, or by a sibling branch's neighbourhood is disputed;
  one that merely name-matches or is only mentioned in the post caption is not.
- With the dispute action at its default, the row keeps its venue and gains a
  ranked candidate an operator can accept through the existing link action.
- The disputed-mode backfill is idempotent, resumable, writes nothing without
  `--apply`, never calls `merge_touched_events`, and never touches
  `title`/`starts_at`.
- After the prod repair, no row at "BeerDock Boa Viagem" carries
  `location_text = 'CASA FORTE'`.

**Defect 3 — recurring**
- Two weekly announcements of the same night stored three weeks apart are
  evaluated as candidates and merge; two on different weekdays never are.
- With `event_dedup_recurring_window_enabled` false, the candidate window is
  byte-identical to today's.
- A merged weekly row serves exactly the nights it served before the merge.

**Defect 1 — the bar**
- The threshold is changed only by admin config, only after the three-threshold
  measurement against the post-repair corpus, and only if every auto pair that
  appears at the new threshold is explained and pinned as a unit test.
- The single-night-venue policy collapses a listed venue's night and leaves an
  unlisted venue's night alone, and never bypasses any existing protection.
- Every false-positive pair named in `260812_event-dedup-fuzzy-title.md`'s
  Evidence is still refused or suggested — never auto-merged — for a venue that
  is **not** on the single-night list.

**Historical repair**
- The sweep is idempotent, resumable, and reversible per pair through the
  existing `reverse-merge` admin action.
- The sweep refuses to write and exits non-zero when its own dry-run exceeds
  `--max-auto-pairs`.
- `venue_night_excess_rows` is materially lower after the repair than before,
  measured against the run's own captured "before" — **never** against the RCA's
  93, which will have moved.
- Assertions name the surviving and absorbed titles, never row counts. A
  count-based assertion has already stayed green here against a deliberately
  reintroduced bug.

**Process fix — visibility**
- `GET /admin/events/dedup-backlog` lists every venue-night group with more than
  one live row, the refused-pair population split by reason, and the
  attribution-dispute backlog keyed by account.
- `event_dedup_backlog{measure}` is published after every extraction run, with
  superseded and non-event rows excluded from every measure.
- A refused pair is no longer invisible: it is counted in the gauge and listed
  in the report.

**Display title**
- No LLM call is made for a merge group with an obvious canonical title, for a
  single-source group, or for a group whose operator edited its title.
- At most one LLM call is made per merge **group**, never per pair or per post,
  and it is counted under its own `endpoint` label.
- A model title that introduces a word no source post contained is rejected and
  the row keeps its stored title.
- The projection serves `display_title or title`; no app-facing field is added
  or removed.
- The merge/no-merge decision remains fully deterministic: the same inputs give
  the same bands, the same canonical and the same absorptions across two runs
  and two candidate orderings, with the model unavailable.

**Hygiene**
- `make test-unit`, `make test-bdd` and `make test-feature` pass for each new
  feature file; CI's scratch-Postgres migrate step is green; every new pytest
  file is registered in the Makefile's `test-unit` list; no `@wip` tag remains
  on a feature whose steps are implemented.

## Open Questions

**Blocking: none.** Every decision this plan needs is either resolved above or
carries a stated safe default that changes no data, so execution may proceed.

**One product judgement, deliberately given a safe default rather than left to
block execution.** The operator's ask — *"events at the same place must never
overlap; they should complement each other under one main event"* — **directly
contradicts a prior measured decision in this repo.**
`260812_event-dedup-fuzzy-title.md` names `Bolinha do Cavaco` / `JB do Cavaco`
at Casanova Ecobar as a false positive that must never merge: two different acts,
one venue, one night. Club Metrópole's five acts on one Saturday are the *same
shape*. Nothing in the rows distinguishes the two situations; the difference is a
fact about the venue — a club runs one night, a theatre and a bookshop run a
programme.

This plan therefore does **not** decide it. It ships the deterministic mechanism
(`event_dedup_single_night_venues`, default `[]`), the measurement that shows
exactly what adding a venue would do (`--single-night-venue`), and the backlog
report that lists the candidate venues. **Default behaviour is unchanged**, so
the unattended pass deploys safely either way. The question for the operator,
answerable after they read the first backlog report:

> Which venues run *one night* (collapse every same-night row into one listing
> with a combined lineup) and which run a *programme* (several genuinely
> different events on one night that must stay apart)? Club Metrópole is
> presumably the first; Teatro Riachuelo, Sempre Rock Bar and Entre Amigos O
> Bode are, on `260812`'s own measured evidence, the second.

Until that list is populated, the Club Metrópole cluster will shrink — from five
rows toward three, if the threshold measurement supports moving to 1 — but will
**not** become one row, because no threshold on either existing signal reaches
`ROWKA` or `VITINHO POLÊMICO`. That is stated here so nobody reads a green
pipeline as "the reported symptom is gone".

**A structural finding worth recording, not a question.** The merge path is
synchronous and the OpenAI client is async, so the LLM title choice **cannot**
live inside the merge without an async refactor of a path three migrations
replay. Rather than a problem, this is what makes §G's scoping honest: the model
is architecturally excluded from the decision, not merely asked to stay out of it.
