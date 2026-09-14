# Candidate-generation cap, and a same-handle/same-time merge path

## Branch
fix/candidate-cap-and-handle-time-merge

## Goal
Two independent, bounded fixes to the events dedup/attribution pipeline,
shipped together because both were surfaced by the same 2026-09-13 discovery
work and both touch `app/services/event_dedup.py`/`event_venue_resolution.py`:

- **Part A.** `event_venue_link_candidate` currently stores the ENTIRE
  servable venue catalog's worth of scored candidates for any event whose
  `location_text` clears rung 4 of the resolution ladder (`_name_match_
  candidates`) — confirmed live today at up to 2,661 rows for a single event.
  Cap it at the real source, with no change to any existing auto-link/
  queued/unresolved decision.
- **Part B.** Add a narrow, OFF-by-default auto-merge path so that several
  posts from the SAME venue-owned Instagram handle, about the SAME exact
  date+time, merge into one listing regardless of title — the Boteco Nem A
  Pau Juvenal case — while continuing to refuse two genuinely different
  programmed events a venue runs on one day at different times — the Casa de
  Jorge Amado case. Both example groups were re-pulled fresh from production
  on 2026-09-13 for this plan (see Evidence); neither is assumed from the
  1130-line `260913_dedup-agentic-mitigation-discovery.md` discovery doc that
  prompted this work.

## Non-goals
- **Not re-litigating `evaluate_pair`'s title-containment or shared-lineup
  rules** (`plans/260812_event-dedup-fuzzy-title.md`). Part B adds a THIRD,
  independent sufficient condition for the `auto` band — the existing two
  keep running, unchanged, for every other pair.
- **Not touching `event_dedup_single_night_venues`/`event_dedup_single_night_
  default_enabled`** (`plans/260912_events-venue-night-duplication.md` §E2).
  That mechanism is a per-venue (or catalog-wide) "this venue runs one night"
  policy that ignores handle and clock time entirely — it is the WRONG tool
  for Part B's problem, precisely because it would also merge Casa de Jorge
  Amado's two different plays if that venue were ever added to the list. Part
  B is a new, narrower, self-limiting signal that needs no venue allowlist.
- **Not widening `event_attribution_dispute.DISPUTE_METHODS`** or touching
  rung 4 (`METHOD_NAME_MATCH`)'s exclusion from it. That is the
  `260913_dedup-agentic-mitigation-discovery.md` plan's own named follow-up
  (Casa Bacurau gap) and is out of scope here.
- **Not enabling the `event_venue_advisor` LLM hook** (already shipped inert
  by `260913_dedup-agentic-mitigation-discovery.md` Phase 4). Part A directly
  reduces the blast radius of ever turning it on (its prompt is built from
  the same uncapped candidate rows), but this plan does not flip that flag.
- **Not a historical, catalog-wide "repair every false-positive pair that
  once existed" sweep for Part B.** The specific titles named as false
  positives in `260812_event-dedup-fuzzy-title.md`'s Evidence (`Bolinha do
  Cavaco`/`JB do Cavaco`, the three `Oficina …` workshops, `Férias Amigos
  Park`, `Ação Leitura …`) no longer exist as live rows — confirmed by a
  fresh production search this session (see Evidence) — and were never
  pinned with `starts_at`/`time_known` in the first place. Re-creating them
  as labelled, explicitly-synthetic fixtures for regression coverage is
  in scope (Acceptance Criteria); re-deriving them from production is not
  possible and is not attempted.

## Evidence

All of the below was re-verified against this worktree at `8df2823` and
against live production (read-only, via AWS SSM into `i-0893fb6d283243480`,
container `vibes_bot-cs-server-1`) on 2026-09-13. Every production number is
a **fresh** read for this plan, not copied from
`260913_dedup-agentic-mitigation-discovery.md`.

### Part A — candidate generation has no cap

- `app/services/event_venue_resolution.py:348-395` (`_name_match_candidates`,
  rung 4): scores `location_text` against **every** venue in `venues`
  (`name_similarity`, a `difflib.SequenceMatcher` ratio), keeps every result
  with `score > 0`, sorts, and returns the **whole** list. No `top_k`, no
  floor beyond `> 0`.
- That full list becomes every `LinkCandidate` `resolve_event_venue` returns
  in `ResolutionResult.candidates` (`event_venue_resolution.py:735-739`), and
  is written VERBATIM to `events.event_venue_link_candidate` by `_on_
  persisted` inside `build_location_text_attribute_fn._attribute`
  (`event_venue_resolution.py:829-837`, `venue_dao.replace_event_venue_link_
  candidates(event_id, [... for rank, c in enumerate(resolution.candidates,
  start=1)])`) — no truncation anywhere in that path.
- A second write site reaches the same unbounded list on the rarer path:
  `event_extraction_service.py:1341-1356`'s `_on_persisted` writes `verdict.
  candidates` from `evaluate_attribution_dispute`
  (`event_attribution_dispute.py:383-390`), whose own call into `resolve_
  event_venue` (`event_attribution_dispute.py:350-373`) passes
  `identity_methods_only=True` — which SKIPS rung 4 entirely (`event_venue_
  resolution.py:639-651`'s documented optimisation) in the common case, but
  **not** when the event's own text names an unrecognized `@handle`
  (`unrecognized_event_handle is not None`, `:651`) — in that one sub-case
  rung 4 still runs unbounded. Fixing rung 4 at its own source
  (`_name_match_candidates`) fixes both write sites with no second change.
- Both readers the task named are confirmed real, unbounded consumers:
  `app/services/event_venue_advisor.py:186` (`EventVenueAdvisorService.
  run_for_events`: `candidate_rows = self.venue_dao.list_event_venue_link_
  candidates(event_id)`, no limit, every row stuffed into the prompt at
  `:198-203`/`:219-224`) and `app/routers/admin_events_router.py:565`
  (`GET /admin/events/review`: `candidates = dao.list_event_venue_link_
  candidates(row["event_id"])`, no limit, serialized into `LinkCandidateOut`
  for every row).
- **Why no cap exists today:** `_name_match_candidates` was written
  (`plans/260804_instagram-promoter-events.md`) before any caller needed more
  than "every venue scored, ranked" — `gate_auto_link`
  (`event_venue_resolution.py:209-230`) and the `RESOLUTION_QUEUED` branch
  (`:735-738`) only ever read `candidates[0]`/`candidates[1]`. Nothing in the
  decision path needed a cap, so one was never added; the need only became
  visible once a reader (the advisor, the review queue) started consuming
  the FULL list rather than just the top of it.
- **Fresh production numbers (2026-09-13, read-only SQL via the real
  `RdsVenueStore`/`VenueRepository`, not assumed from the discovery plan):**
  - `events.event_venue_link_candidate`: **205,977** total rows across
    **729** distinct events. Method breakdown: `name_match` 205,401 (99.8%),
    `handle_mention` 566, `caption_handle_mention` 10.
  - Per-event candidate-count histogram (729 events): **576** events with
    exactly 1 row (rungs 1/2/3/5 — identity or bounded matches); **60** with
    2-10 rows; **zero** events anywhere in the 11-1,000 range; **31** events
    with 1,001-2,000 rows; **62** events with 2,001+ rows (max observed:
    **2,661**, four distinct events). The 93 events over 1,000 rows (12.8%
    of events-with-candidates) account for roughly 186,000 of the 205,977
    total rows (~90%) — confirms the bloat is concentrated in a clean,
    separable bimodal tail, not spread evenly.
  - Servable catalog size: 3,532 non-deprecated venues of 3,648 total — the
    2,661-row events are scoring very close to the whole catalog.
  - Score shape for the single largest event (`evt_01M1Y1W0N87YSYX4XF56
    TEV4CW`, title "Yoga", `location_text="Espaço Mutá — R. José Ribeiro
    Dantas, 2689, Lagoa Nova."`, 2,661 candidates): score≥0.5 → 1 candidate;
    ≥0.4 → 54; ≥0.35 → 282; ≥0.3 → 794; ≥0.25 → 1,518; ≥0.2 → 2,168; ≥0.15 →
    2,503; ≥0.1 → 2,621; ≥0.05 → 2,658. **No natural cluster/gap exists in
    this distribution** — it decays smoothly, consistent with a
    `SequenceMatcher` ratio scoring thousands of unrelated short strings.
    This rules out "pick a score floor where legitimate candidates cluster"
    as the PRIMARY mechanism — there is no such cluster to pick.
  - For 15 sampled events that DID auto-link via `name_match` (cleared both
    `DEFAULT_CONFIDENCE_FLOOR=0.55` and `DEFAULT_MARGIN=0.08`), every
    genuine runner-up candidate observed sat at **rank ≤ 5** (e.g. one
    event's top 5: 1.0, 0.75, 0.6154, 0.6154, 0.6 — plausible same-brand
    siblings — out of 1,428 total candidates). None of the 15 samples had a
    second plausible candidate past rank 5.
  - `gate_auto_link` (`:209-230`) and the `RESOLUTION_QUEUED` branch
    (`:735-738`) read only `candidates[0]` and (for the margin check)
    `candidates[1]`. **Proof that a cap changes zero existing resolution
    decisions:** for any `top_k >= 2`, rank 0 and rank 1 are always present
    and unchanged, so every `auto`/`queued`/`unresolved` verdict this ladder
    has ever produced is reproduced byte-for-byte after capping.
  - Of the 44 live `RESOLUTION_QUEUED` events (location_resolution IS NULL
    AND venue_id IS NULL), **22 already carry at least one candidate row**
    today — several in the 2,600+ bucket (e.g. `evt_01M27H22AEJREGPTV5C3M
    THXKP`, `evt_01M27H257VC61PM33N5FM57CV3`, both 2,661 rows,
    `location_resolution=NULL`) — i.e. the exact rows `EventVenueAdvisor
    Service` would stuff into a prompt today, the moment its kill-switch is
    ever flipped on.
  - Precedent for a hard-coded bound living directly in this module family,
    not behind `AdminConfigService`: `MAX_BRAND_ROOT_VENUES = 8`
    (`event_attribution_dispute.py:204`, "the defensive bound... an
    oversized set is [capped]"). Precedent for a deploy-time-tunable,
    non-gated ladder parameter: `DEFAULT_CONFIDENCE_FLOOR`/`DEFAULT_MARGIN`
    (`event_venue_resolution.py:145-146`), wired through plain `settings.*`
    fields (`app/config.py:792-793`, `promoter_link_confidence_floor`/
    `margin`), never through `AdminConfigService`. Precedent for shipping a
    correctness fix to `_name_match_candidates` UNCONDITIONALLY, with no
    kill-switch: `_strip_handles_for_name_match`
    (`plans/260813_handle-attribution-hardening.md`, same function family) —
    shipped directly because it only ever REMOVES a wrong signal, never
    changes a correct one. This plan's cap is the same shape: given the
    proof above that it changes no decision, it follows that precedent
    rather than the `AdminConfigService`-gated-off-by-default convention
    this repo otherwise uses for behaviour that DOES change a decision.

### Part B — the real tension, investigated with fresh data, not assumed

The user's rule as stated ("same handle, known venue, same day and time,
title irrelevant") must be checked against BOTH the case it is meant to fix
and the case `260912_events-venue-night-duplication.md` already deliberately
protects. Both groups were re-pulled fresh on 2026-09-13 via `RdsVenueStore.
list_events(venue_id=...)` (through `VenueRepository`), not assumed from the
discovery plan's own "Live Validation" section (which only reported titles
and bands, not `starts_at`/`time_known`).

**Boteco Nem A Pau Juvenal** (`venue_id=ven_4146...Jf496843`, handle
`boteconemapaujuvenal`, a plain venue-owned Instagram handle — confirmed NOT
in `events.promoter_account`). Of 8 live non-superseded events, three share
2026-09-13:

| title | `starts_at` | `time_known` | `location_resolution` | `review_reason` |
|---|---|---|---|---|
| "Domingo é dia de Boteco!" | 2026-09-13 18:00:00+00 | true | NULL | NULL |
| "Show de @alexcarrerasoficial" | 2026-09-13 18:00:00+00 | true | NULL | NULL |
| "ALEX CARRERAS E BANDA" | 2026-09-13 18:00:00+00 | true | NULL | NULL |

All three: **identical `starts_at`, to the second**; both link-audit columns
NULL (never routed through the resolution ladder — the plain "attributed to
the posting venue" case, `event_extraction_service.py`'s own documented
common path). `evaluate_pair` refuses all three pairs today (two share
exactly one lineup token, below `DEFAULT_LINEUP_THRESHOLD=2`; the third pair
is disjoint because the glued handle token `alexcarrerasoficial` never
token-intersects `{alex, carreras, banda}` — the tokenization gap
`260913_dedup-agentic-mitigation-discovery.md`'s Live Validation section
already names and explicitly defers). This is a real, live, three-way
fragmented single repost of one show.

**Casa de Jorge Amado** (`venue_id=ven_7769...Jf496843`, handle
`teatrojorgeamado`, also plain venue-owned, not a promoter account). Of 5
live events, two share 2026-09-13:

| title | `starts_at` | `time_known` | `location_resolution` | `review_reason` |
|---|---|---|---|---|
| "PETER PAN" | 2026-09-13 14:00:00+00 | true | NULL | NULL |
| "CHAPEUZINHO VERMELHO" | 2026-09-13 19:00:00+00 | true | NULL | NULL |

**Different clock times, 5 hours apart, both genuinely `time_known=true`**
(neither unset) — two real, distinct plays on the same calendar day. This is
exactly the `260912` "Entre Amigos O Bode" shape the operator separately
praised as working correctly.

**Time cleanly separates the two cases on this data.** "Same `starts_at` to
the instant" (which, for two aware datetimes, already implies the same
calendar day — there is no need to check day and time as two separate
conditions) picks out the genuine Boteco repost and correctly excludes the
genuine Jorge Amado pair.

**This is not a two-example coincidence — checked catalog-wide.** A broader
read-only sweep (all 530 live, non-superseded, `post_type='event'` rows with
a `venue_id`, grouped by `(venue_id, source_handle, Recife-local date)`)
found **7** such same-handle/same-day clusters catalog-wide, 2026-09-13:

- **1** cluster where every member shares the exact same clock time: Boteco
  (the 3 rows above).
- **6** clusters where members have different clock times. Jorge Amado's
  pair is one. The other **5** all belong to `oquetemhojeemnatal` — a
  REGISTERED promoter account (`events.promoter_account.status='active'`,
  confirmed), a city-wide roundup handle whose posts cover many unrelated
  real events/venues on one day (e.g. 22 different titles at one `venue_id`
  on 2026-09-12, every one at a different clock time). Every sampled
  `oquetemhojeemnatal` row carries a non-NULL `location_resolution`
  (`'auto'` via `handle_mention`, or `'unresolved'` with `review_reason`
  `venue_not_in_catalog`/`unresolved_venue`) — i.e. it DID go through the
  resolution ladder, unlike Boteco's and Jorge Amado's rows.
- **Zero** clusters had mixed `time_known` (one row `true`, one `false`),
  and zero had both rows `time_known=false`. The "both sides equally unset"
  case the task asked me to check for has **no live example** in the
  current catalog, in either direction — it is not validated as safe OR
  shown to be a real risk; it simply has not occurred yet. See Open
  Questions.

**This sweep also surfaces the exact structural guard the rule needs.**
`oquetemhojeemnatal`'s rows would pass a naive "same handle, same day" check
at several (venue_id, date) pairs, and are excluded here ONLY because every
sampled row's clock time genuinely differs from every other's — i.e. today's
clean separation is partly empirical luck for this one account, not a
structural guarantee. A structural guard is available for free: every one of
the real target rows (Boteco ×3, Jorge Amado ×2) has `location_resolution IS
NULL` (never touched the ladder — registered single-venue handle, plain
venue-owned post), while every `oquetemhojeemnatal` row has it set to a real
value. Requiring `location_resolution IS NULL` on both sides of a pair
structurally excludes every promoter/roundup-style account — including
`oquetemhojeemnatal` specifically — with **no catalog-wide read** (no call
to `venue_dao.list_instagram_handles()`/`group_venue_ids_by_handle`, which
would otherwise repeat the exact per-crawl, per-venue full-catalog-scan
performance trap `event_venue_resolution.py:630-637` already documents
`event_dedup_backlog` having fallen into once). The "no explicit, named,
different address/venue signal in either post's own text" half of the rule
is covered by the SAME data: a dispute would have set `review_reason`
to `location_text_disputes_venue` (`event_attribution_dispute.py:80`,
written by `event_extraction_service.py:1335-1339`) without necessarily
setting `location_resolution` (the default `flag` action), so both columns
must be checked — `location_resolution IS NULL` alone is not sufficient.
Neither target row carries that review reason.

**Conclusion: time reliably distinguishes the two shapes on every case this
session could find, live, today — design the narrower rule using that
signal** (Option A from the task), with the residual, unvalidated "both
sides `time_known=false`" sub-case named explicitly as a narrower Open
Question rather than silently shipped as equally safe.

### Wiring precedent for Part B (`event_dedup.py`/`event_merge.py`)

- `PairDecision`/`evaluate_pair` (`event_dedup.py:577-645`) already add a
  THIRD independent auto-merge reason this exact way:
  `REASON_SINGLE_NIGHT_VENUE` (`:63`) is appended to `reasons` from a
  caller-computed `single_night_venue: bool` parameter
  (`:588`, `:629-630`) — never re-derived inside `evaluate_pair` from a
  config lookup. `DedupConfig` (`:240-287`) carries the admin-config-backed
  state (`single_night_venues`/`single_night_default_enabled`,
  `:262-265`), validated by `validate_single_night_venues_config`/
  `validate_single_night_default_enabled_config` (`:224-237`), loaded by
  `load_dedup_config` (`:367-383`), registered in `app/container.py:714-
  724` and `tests/bdd/environment.py:406-416`.
- `_run_pairwise_pass` (`event_merge.py:997-1071`) computes the venue-level
  `single_night_venue` bool ONCE per venue pass and passes it into every
  pair's `evaluate_pair` call (`:1038-1041`). Part B's signal is PER-PAIR
  (depends on both rows' `source_handle`/`starts_at`/`time_known`), so it is
  computed per-pair inside the same loop from data already on each row dict
  — no new DAO call, no new per-venue precomputation.
- `_edge_blocked_for_absorption` (`event_merge.py:842-879`) already blocks
  absorption on ANY non-lineup-backed auto edge when either side's
  `operator_edited_fields` names `title`. This guard is left UNCHANGED and
  DOES apply to a handle-time-match-only edge (title not in `reasons`
  alongside lineup) — a deliberate choice, not an oversight: the guard
  protects an operator's own editorial decision on a row, a different
  concern from "should title gate this merge," and the user's rule never
  asked to override an operator's own edit.
- `_absorb_title_similarity` (`:933-994`) already separates
  `merged_single_night_venue` from plain `merged` in
  `EVENT_MERGE_TOTAL{identity="title"}` when that reason fired ALONE
  (`:980-984`) — Part B adds the same treatment for its own reason.

## Current Behavior
- **Part A.** Any event whose `location_text` survives the `@handle` strip
  is scored against the WHOLE servable catalog and every venue with
  `score > 0` is persisted as a ranked candidate — in practice 1,400-2,661
  rows per affected event, ~90% of all rows in `event_venue_link_candidate`
  concentrated in 93 events. `GET /admin/events/review` and the (currently
  inert) `EventVenueAdvisorService` both read the full, uncapped list.
- **Part B.** Two posts from the same venue-owned handle about the exact
  same date+time, worded differently enough to share fewer than two lineup
  tokens and no distinctive title words, are refused outright and never
  merge — regardless of how identical their `starts_at` is.

## Desired Behavior
1. `_name_match_candidates` returns at most `top_k` candidates, ranked
   exactly as today, for every caller. No existing `auto`/`queued`/
   `unresolved` verdict changes for any event, proven by the rank-0/rank-1
   invariant above.
2. Two live `post_type='event'` rows at one venue, from the same plain
   (non-promoter, non-ambiguous) Instagram handle, sharing the exact same
   `starts_at` instant (or both genuinely `time_known=false` on the same
   Recife-local date — see Open Questions), with neither side carrying an
   unresolved attribution dispute against the mapped venue, auto-merge
   regardless of title or lineup overlap — gated OFF by default, measured
   before enabling, exactly like every other auto-merge widening this
   pipeline has shipped.
3. Two live rows at one venue, same handle, same day, genuinely DIFFERENT
   clock times (Casa de Jorge Amado's shape) keep refusing exactly as today,
   with or without the new flag enabled.

## Implementation Approach

### Part A — cap at the source

1. **`app/services/event_venue_resolution.py`**: add
   `DEFAULT_NAME_MATCH_TOP_K = 20` alongside `DEFAULT_CONFIDENCE_FLOOR`/
   `DEFAULT_MARGIN` (`:145-146`). Justification for 20, not a round guess:
   every genuine runner-up candidate observed in the 15 auto-linked samples
   sat at rank ≤ 5; 20 is a 4x margin above the deepest real alternative
   this session could find, while still cutting the worst offenders from
   2,661 to 20 (99.2%) and the 1,400-row cluster by 98.6%. `_name_match_
   candidates` gains a `top_k: int = DEFAULT_NAME_MATCH_TOP_K` parameter;
   after the existing `scored.sort(...)` (`:383`), truncate `scored =
   scored[:top_k]` BEFORE building `LinkCandidate` objects (cheaper, and
   keeps the returned list's invariants — sorted, ranked — identical to
   today's, just shorter). Thread `top_k` through `resolve_event_venue`
   and `build_location_text_attribute_fn` as a keyword with the same
   default, wired at the container/settings level exactly like
   `confidence_floor`/`margin` (`app/config.py`: new
   `event_venue_name_match_top_k: int = 20`, passed the same way
   `promoter_link_confidence_floor`/`margin` already are into every caller
   that currently threads those two through). `top_k` must never be
   configured below 2 — `gate_auto_link`'s margin check needs a runner-up
   to compare against; document this as a hard floor in the setting's own
   docstring, and pin it with a unit test rather than a runtime guard,
   since this is a deploy-time constant, not operator input.
2. **No `AdminConfigService` gate.** Deliberately: the rank-0/rank-1 proof
   above shows this change cannot alter any existing `auto`/`queued`/
   `unresolved` decision for any `top_k >= 2`, and the only two readers of
   the full list (the review queue, the still-inert advisor) are strictly
   improved by a shorter, equally-ranked list. This follows the
   `_strip_handles_for_name_match` precedent (a correctness fix to the same
   function, shipped unconditionally) rather than this repo's
   `AdminConfigService`-off-by-default convention, which exists for changes
   that DO alter a decision or withhold content — neither applies here.
   State this reasoning in the PR description so a reviewer does not have
   to re-derive it.
3. **New metric**: `EVENT_VENUE_NAME_MATCH_CANDIDATES_TRUNCATED_TOTAL`
   (Counter, no labels, alongside `EVENT_VENUE_NAME_MATCH_SKIPPED_TOTAL` in
   `app/metrics.py`), incremented once per `_name_match_candidates` call
   where the pre-truncation count exceeded `top_k` — gives an operator a
   live signal of how often the cap actually bites, and by how much, once
   deployed.
4. **Small, optional, same-discipline-as-every-other-backfill repair
   script**: `scripts/backfill_event_venue_candidate_cap.py`. For every
   `event_id` whose current `event_venue_link_candidate` row count exceeds
   `top_k`, re-derive its `location_text` via the same `_location_text_
   input` precedent `scripts/backfill_event_venue_links.py:150-164` already
   uses, re-run the (now-capped) `resolve_event_venue`, and call `replace_
   event_venue_link_candidates` to overwrite the stored rows — the exact
   "a later crawl recomputes the ladder from scratch" contract `replace_
   event_venue_link_candidates`'s own docstring already describes, just run
   once, now, instead of waiting for each post's next crawl. Dry-run
   default, `--apply`, idempotent (a second run touches zero rows once every
   oversized event is capped), resumable via `--since-event-id`, writes
   ONLY this table (never `venue_id`/`location_resolution`). Addresses the
   93 live bloated events directly rather than waiting for their next
   re-crawl — relevant now because 22 of the 44 live `RESOLUTION_QUEUED`
   events already carry oversized rows the (currently inert) advisor would
   read in full the moment it is ever enabled.

### Part B — the handle+time merge path

1. **`app/services/event_dedup.py`**: add `REASON_HANDLE_TIME_MATCH =
   "handle_time_match"` next to `REASON_SINGLE_NIGHT_VENUE` (`:63`). Add
   admin-config key `ADMIN_CONFIG_HANDLE_TIME_MATCH_ENABLED_KEY =
   "admin_config:event_dedup_handle_time_match_enabled"`,
   `DEFAULT_HANDLE_TIME_MATCH_ENABLED = False`, a `validate_handle_time_
   match_enabled_config` validator (bool only, same shape as `validate_
   single_night_default_enabled_config`), a new `DedupConfig.handle_time_
   match_enabled: bool = False` field, and load it in `load_dedup_config`
   the same way every other flag in that function is loaded (`:339-382`).
   Register the key in `app/container.py` (alongside `:714-724`) and
   `tests/bdd/environment.py` (alongside `:406-416`).

   Add a new pure function, duplicating (never importing, to avoid the
   `event_attribution_dispute.py` → `event_dedup.py` import direction
   already in place, `event_attribution_dispute.py:60`) the literal review-
   reason string as a local constant with a docstring note that it must stay
   in lockstep with `event_attribution_dispute.REVIEW_REASON_LOCATION_TEXT_
   DISPUTES_VENUE` (`:80`), pinned by a literal-equality guard test — the
   same pattern `event_venue_resolution.METHOD_VENUE_NOT_IN_CATALOG`'s own
   docstring already uses for exactly this cross-module constraint:

   ```
   handle_time_match_eligible(event_a, event_b) -> bool
     handle = event_a.get("source_handle")
     if not handle or handle != event_b.get("source_handle"): return False
     for e in (event_a, event_b):
       if e.get("location_resolution") is not None: return False
       if _REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE in (e.get("review_reason") or ""):
         return False
     a_time, b_time = event_a.get("starts_at"), event_b.get("starts_at")
     if a_time is None or b_time is None: return False
     a_known, b_known = bool(event_a.get("time_known")), bool(event_b.get("time_known"))
     if a_known and b_known: return a_time == b_time
     if not a_known and not b_known:
       return a_time.astimezone(RECIFE_TZ).date() == b_time.astimezone(RECIFE_TZ).date()
     return False  # one known, one not: cannot confirm, stay conservative
   ```

   The caller (`_run_pairwise_pass`, never `evaluate_pair` itself) already
   guarantees same-`venue_id` and `in_candidate_window_for_rows` before this
   runs, per `evaluate_pair`'s own existing docstring convention — this
   function does not re-check either.
2. **`evaluate_pair`** (`event_dedup.py:586-645`) gains a `handle_time_
   match: bool = False` parameter, appended to `reasons` exactly where
   `single_night_venue` is today (after the lineup/title checks, before the
   `if reasons: band = BAND_AUTO` branch) — an independent sufficient
   condition, never a tie-break on title or lineup.
3. **`_run_pairwise_pass`** (`event_merge.py:997-1071`): inside the existing
   per-pair loop (`:1031-1041`), compute `handle_time_match = config.
   handle_time_match_enabled and event_dedup.handle_time_match_eligible(a,
   b)` and pass it into `evaluate_pair`. No new per-venue precomputation, no
   new DAO call — every field the predicate reads is already on the row
   dicts the loop already holds.
4. **`_absorb_title_similarity`** (`event_merge.py:933-994`): extend the
   `outcome = "merged_single_night_venue" if ... else "merged"` branch
   (`:980-984`) with a third case, `"merged_handle_time_match"`, when
   `decision.reasons == (event_dedup.REASON_HANDLE_TIME_MATCH,)` — kept
   distinct for the same reason `merged_single_night_venue` is: so this
   specific, newer mechanism's adoption and risk can be watched on its own
   series.
5. **Dry-run measurement, before the flag is ever set true in prod** —
   extend `scripts/measure_event_dedup.py` with `--handle-time-match`
   (forces `config.handle_time_match_enabled=True` for the measurement run
   only, mirroring the existing `--recurring-window`/`--lineup-threshold`
   flags exactly). An operator runs this against live data and reviews
   every NEW auto pair the flag would add before flipping `PUT /admin/
   config/event_dedup_handle_time_match_enabled`.

## Data, Config, And API Impact
- **Part A**: new `app/config.py` setting `event_venue_name_match_top_k:
  int = 20` (deploy-time, not `AdminConfigService`). New metric
  `EVENT_VENUE_NAME_MATCH_CANDIDATES_TRUNCATED_TOTAL`. New script
  `scripts/backfill_event_venue_candidate_cap.py` (dry-run default,
  read-then-write only `event_venue_link_candidate`). No migration, no API
  shape change — `GET /admin/events/review` and `EventVenueAdvisorService`
  both already tolerate a shorter candidate list; neither assumes a minimum
  length anywhere (confirmed by reading both call sites above).
- **Part B**: new `AdminConfigService` key `event_dedup_handle_time_match_
  enabled` (bool, default `False`), validated on read and write exactly like
  every other `event_dedup_*` key, registered in `app/container.py` and
  `tests/bdd/environment.py`. New `EVENT_MERGE_TOTAL{identity="title",
  outcome="merged_handle_time_match"}` label value (the inline inventory
  comment at `app/metrics.py:1575-1600` must be updated). New CLI flag
  `--handle-time-match` on `scripts/measure_event_dedup.py`. No migration,
  no admin API shape change (the new reason flows through the EXISTING
  `PairDecision.reasons`/`event_merge_suggestion.reasons` shape, same as
  `single_night_venue` did).

## Error Handling And Observability
- **Part A**: a climbing `EVENT_VENUE_NAME_MATCH_CANDIDATES_TRUNCATED_TOTAL`
  is expected and healthy (it is the cap doing its job), not a failure
  signal. The backfill script logs, per event, the before/after row count
  and never touches `venue_id`/`location_resolution`/`review_reason`; a
  failure on one event is caught, logged, and does not stop the run over
  the rest (matching every other backfill script's degrade-per-row
  posture).
- **Part B**: `EVENT_MERGE_TOTAL{outcome="merged_handle_time_match"}` is
  watched the same way `merged_single_night_venue` already is — a count
  that climbs much faster than the corpus of same-handle/same-instant
  reposts would mean the predicate is over-firing and needs review before
  the flag stays on. Every handle-time-match merge logs both event ids,
  both `starts_at` values and the shared handle at info, joining the
  existing auto-merge log line (`event_merge.py:987-993`) — an operator
  asking "why did these merge" must be able to read the answer without a
  database, same as every other reason.

## Test Plan

Feature file: `tests/bdd/enrichment/event-venue-candidate-cap.feature`
Feature file: `tests/bdd/enrichment/event-dedup-handle-time-match.feature`

### Scenarios — `event-venue-candidate-cap.feature`
- Rank a name-match event's candidates and keep only the top-K, in the same
  order as before capping.
- Never drop the winning candidate or its runner-up, whatever K is set to.
- Leave an identity-rung event (a single handle-mention or location-tag
  candidate) completely unaffected by the cap.
- Leave a bounded neighbourhood-match candidate set (rung 3, already capped
  at `MAX_BRAND_ROOT_VENUES`) completely unaffected by the cap.
- Produce the exact same `auto`/`queued`/`unresolved` verdict, before and
  after capping, for every resolution outcome this ladder can reach.
- Count a truncation when the pre-cap candidate count exceeds K, and never
  count one when it does not.
- The review queue returns at most K candidates for an event whose ladder
  run scored the whole catalog.
- The dry-run backfill reports every event it would shrink and writes
  nothing without `--apply`.
- The backfill is idempotent: a second `--apply` run touches zero rows.
- The backfill never writes `venue_id`, `location_resolution`, or
  `review_reason`.

### Scenarios — `event-dedup-handle-time-match.feature`
- Merge three same-handle posts about one show at the exact same date and
  time into one listing, with the flag enabled — the Boteco Nem A Pau
  Juvenal case, pinned with its real production strings and timestamps.
- Refuse to merge two same-handle posts at the same venue on the same day
  but at different clock times — the Casa de Jorge Amado case, pinned the
  same way.
- Refuse to merge when the flag is disabled (the default), even for an
  otherwise-eligible exact-time pair.
- Refuse to merge across two different source handles, whatever their
  times.
- Refuse to merge when either event already carries a non-null
  `location_resolution` (the promoter/multi-venue-ladder shape —
  `oquetemhojeemnatal`'s real production rows as the pinned counter-
  example).
- Refuse to merge when either event's `review_reason` carries an
  attribution dispute against the mapped venue.
- Refuse to merge when one side's time is known and the other's is not.
- Never bypass an operator's title edit: a handle-time-match-only edge is
  still blocked by `_edge_blocked_for_absorption` exactly like a
  single-night-venue-only edge is.
- Count `merged_handle_time_match` separately from `merged` when the new
  reason is the sole reason a pair reached auto.
- Still merge via the existing title/lineup rules when handle-time-match
  is also true — the three reasons are independent, never exclusive.
- The measurement script's `--handle-time-match` flag reports every new
  auto pair the signal would add without writing anything.

### Pytest unit tests
- `tests/test_event_venue_resolution.py` (extend): `_name_match_candidates`
  truncates to `top_k`, preserves sort order and the proximity tie-break,
  and is a no-op when the unranked list is already `<= top_k`; `top_k=1`
  is documented as invalid for `gate_auto_link`'s margin check (a guard
  test, not a runtime exception); the literal-equality guard between
  `event_dedup`'s duplicated review-reason string and `event_attribution_
  dispute.REVIEW_REASON_LOCATION_TEXT_DISPUTES_VENUE`.
- `tests/test_backfill_event_venue_candidate_cap.py` (new): dry-run vs
  `--apply`; idempotence; resumability; never touches `venue_id`/
  `location_resolution`.
- `tests/test_event_dedup_handle_time_match.py` (new, mirroring `tests/
  test_event_dedup_single_night.py`'s shape): `handle_time_match_eligible`
  pinned against the REAL Boteco (true) and Jorge Amado (false) row shapes
  re-derived in this plan's Evidence; different handles excludes; a non-
  null `location_resolution` on either side excludes; the dispute review-
  reason on either side excludes; one side `time_known` true and the other
  false excludes; both `time_known=false` on the same Recife-local date
  includes (the unvalidated sub-case — see Open Questions); symmetry over
  `(a, b)`/`(b, a)`; `evaluate_pair` appends `REASON_HANDLE_TIME_MATCH`
  only when the caller's bool is true, independent of title/lineup;
  `_edge_blocked_for_absorption` still blocks a handle-time-match-only
  edge when either side's title is operator-edited.
- `tests/test_measure_event_dedup.py` (extend): `--handle-time-match`
  reaches `DedupConfig.handle_time_match_enabled`; the default invocation
  is unchanged.
- **Synthetic, explicitly-labelled regression fixtures for the `260812`
  false-positive corpus** (since the original rows are confirmed gone from
  production and were never time-stamped — see Evidence/Non-goals): for
  each of `Bolinha do Cavaco`/`JB do Cavaco`, the three `Oficina …`
  workshops, and `Ação Leitura … Marcelino Freire`/`… Jeferson Tenório`,
  construct a fixture pair with the SAME handle (the realistic shape — one
  venue's own account posting both) and DIFFERENT plausible set times (two
  different acts/workshops on one night predictably have different
  slots), clearly marked `"synthetic": true` in the fixture, and assert
  `handle_time_match_eligible` returns `False` for each. This is
  regression coverage for the mechanism, not a claim that these are the
  real historical rows.

### Manual or integration checks
- Run `scripts/measure_agentic_mitigation_baseline.py --production-only`
  before and after Part A ships, and confirm the candidate-count histogram
  collapses the 93-event, 1,001+-row tail without changing the
  `RESOLUTION_QUEUED`/`BAND_SUGGEST`/dispute counts it also reports (proof,
  in production, that no decision moved).
- Run `scripts/backfill_event_venue_candidate_cap.py` dry-run against prod,
  review the per-event before/after counts, then `--apply` off-peak.
- Run `scripts/measure_event_dedup.py --handle-time-match --report-json` against
  live production data and manually review every new auto pair it reports
  before ever setting `event_dedup_handle_time_match_enabled=true` in prod.
- Re-confirm live that Boteco's three 2026-09-13 rows and Jorge Amado's two
  2026-09-13 rows still exist with the shapes captured in this plan's
  Evidence immediately before that measurement run (more crawls will have
  happened between planning and execution).

## Acceptance Criteria
- **Part A**: `_name_match_candidates` never returns more than `top_k`
  candidates; every existing `auto`/`queued`/`unresolved` verdict in
  `tests/test_event_venue_resolution.py` (pre-existing tests, unmodified)
  still passes unchanged. `GET /admin/events/review` and `EventVenueAdvisor
  Service` both observably return/consume at most `top_k` candidates for an
  event whose uncapped ladder run would have scored the whole catalog.
  `scripts/measure_agentic_mitigation_baseline.py --production-only`'s
  `RESOLUTION_QUEUED`/`BAND_SUGGEST`/dispute counts are byte-identical
  before and after, run against the same data.
- **Part B**: with `event_dedup_handle_time_match_enabled=false` (the
  shipped default), production behaviour is provably unchanged — Boteco's
  three rows stay three rows. With it set `true` in a test/measurement
  context, Boteco's three rows collapse to one and Casa de Jorge Amado's
  two rows stay two, both pinned with the real production strings and
  timestamps captured in this plan's Evidence. Every false-positive title
  pair named in `260812_event-dedup-fuzzy-title.md`'s Evidence is refused
  by `handle_time_match_eligible` under the synthetic fixtures constructed
  for this plan (see Test Plan) — **this specific criterion must pass
  before `/execute-feature` may enable the flag in any environment**, per
  the task's own instruction, since the original rows cannot be re-measured
  live.
- The "both sides `time_known=false`" sub-case (Open Questions) is resolved
  — either shipped with its own separate metric/log line so its real-world
  frequency can be watched from day one, or deliberately excluded from v1
  (requiring both sides `time_known=true`) with that narrowing stated
  explicitly in the PR — before `/execute-feature` proceeds past that gate.
- Zero change to `evaluate_pair`'s title-containment/shared-lineup bands,
  `in_candidate_window_for_rows`, `_is_protected`, `_edge_blocked_for_
  absorption`'s existing clauses, or any existing `event_dedup_*`/
  `event_venue_resolution.*` admin-config default — confirmed by the full
  existing test suites for those modules passing unmodified.
- `make test-unit`, `make test-bdd` pass; every new pytest file is added to
  the Makefile's `test-unit` list; no `@wip` tag remains on either feature
  file once its steps are implemented.

## Open Questions

**Blocking for Part B's flag enablement (not for `/execute-feature`
starting — see Acceptance Criteria above for exactly what gates what):**

1. **The "both sides `time_known=false`" allowance has zero live precedent,
   in either direction.** The catalog-wide sweep (7 same-handle/same-day
   clusters) found no cluster where either member had `time_known=false`,
   let alone both — so the task's own allowance ("or both sides carrying
   `time_known=False`, if that's how equally-unset times present") is
   neither validated safe nor shown to be a real risk by this session's
   data; it is simply untested. Two honest options, both compatible with
   this plan's design:
   - Ship it as designed above (both-unset-on-the-same-day matches), but
     log/count it on a SEPARATE reason sub-label (e.g.
     `merged_handle_time_match_unset_time`) distinct from the exact-time
     case, so its real frequency and any false positives become visible
     from day one rather than being silently blended into one counter.
   - Or narrow v1 to require `time_known=true` on both sides (the only
     case this session actually measured), deferring the unset-time
     allowance to a follow-up once a real example of it is observed.
   This plan does not pick one — `/execute-feature` must resolve it
   explicitly (per Acceptance Criteria) rather than silently implement the
   task's literal wording without re-reading this caveat.
2. Should `top_k=20` (Part A) be admin-config-tunable after all, rather
   than a deploy-time `settings.*` value? This plan chose `settings.*`
   because the change provably alters no decision and because
   `confidence_floor`/`margin` are the closest precedent for "a ladder
   tuning parameter with no feature-flag semantics" — but if the backfill
   script's real production run (Manual Checks) shows 20 is measurably too
   tight or too loose for some real case, moving it to `AdminConfigService`
   is a small follow-up, not a blocker to shipping this plan as designed.
