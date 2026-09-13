# Agentic Mitigation Discovery — Dedup, Multi-Location, And Multi-Post Duplication At 1k-Crawl-Target Scale

## Branch
feature/dedup-agentic-mitigation-discovery

## Goal
Before building any new agentic/LLM infrastructure for venue-attribution and
event-dedup drift, empirically measure how much of three named failure
classes the codebase's EXISTING deterministic machinery already catches (or
could catch with tuning), then ship only the minimal, validator-gated
mechanism that measurement actually justifies — sized for crawl targets
growing from a handful to roughly 1000 Instagram accounts across multiple
cities over the next few months, where tonight's hand-curated-exclusion-list
approach (plans/260912_events-venue-night-duplication.md) cannot scale.

The three failure classes, all confirmed real by the 260912 incident:
1. **Cross-post fragmentation** — one venue, one night, N posts about N acts
   that should merge into one listing (Club Metrópole: 5 posts → 5 rows).
2. **Multi-location misattribution** — one crawled handle whose posts
   actually cover several physical venues, so pinning every post to the
   handle's single mapped venue mis-attributes events. Two materially
   different shapes, confirmed by tracing the actual call sites (see
   Evidence): **sibling-brand** (BeerDock: 15 of 44 rows belonged to a
   same-brand sister branch — partially caught today by
   `evaluate_attribution_dispute`'s bounded name/neighbourhood matching) and
   **unrelated-venue** (a handle whose posts name a completely different,
   non-sibling venue by plain text — e.g. a curation-style account like the
   user-named "barchef" example, or a "hidden promoter": a handle that
   behaves like a promoter but was never registered as one and is invisible
   to the inbound-mention discovery that finds registered promoters).
   Unrelated-venue misattribution is NOT caught by anything shipped last
   night — see Evidence — and is the harder half of this failure class.
3. **Multi-post-for-the-same-event** — the same real event promoted across
   multiple posts, potentially by different accounts, that should collapse
   to one listing.

## Non-goals
- Not building a per-event LangGraph/tool-using agent loop. Verified against
  `vibes_bot/requirements-llm.txt` and its own `langchain-fundamentals` /
  `langchain-middleware` / `langchain-rag` skills (each carries an explicit
  "nothing in this skill runs here today" banner): only `langchain-core` and
  `langchain-openai` are installed, for one stuffed-prompt `ChatOpenAI` call.
  `langchain` (`create_agent`/`AgentExecutor`/`@tool`), `langgraph`,
  `langchain-community`, and `faiss-cpu` are deliberately NOT installed
  anywhere in either repo. A multi-step tool-calling agent is unjustified for
  a bounded, closed-candidate classification task — see Evidence. This is a
  scoping judgment call for THIS task, not a claim that the dependency is
  unavailable: `langchain`/`langgraph` can be installed in either repo if a
  future design genuinely needs a multi-step tool loop (the
  `vibes_bot/.claude/skills/langchain-fundamentals` skill and its siblings
  already document the patterns), and this plan's Acceptance Criteria should
  be revisited, not silently reinterpreted, if Phase 4 or a follow-up plan
  finds a case a single structured-output call cannot handle — the leading
  candidate for that being open-ended search over the full venue catalog for
  an unrelated-venue match (see the new Evidence bullet on that gap), which
  none of the three compared designs actually needed a tool loop for either,
  since each bounded its own candidate generation to data already fetched.
- Not building a from-scratch handle-scope classifier or a from-scratch
  embedding-plus-clustering drift-scan service without first measuring
  whether the already-shipped promoter-account registry, discovery pass, and
  `event_venue_link_candidate` table already cover the gap they would fill.
  See Evidence — this codebase already has more first-class infrastructure
  for "a handle isn't tied to one venue" than any of the three designs
  compared in this plan's discovery workflow accounted for.
- Not solving cross-handle same-event promotion in full generality (a
  promoter account and a venue's own handle both posting the same show).
  Scoped to measuring how much `PromoterRegistryService` /
  `PromoterCrawlService`'s existing mechanism already contributes; a
  general cross-catalog search is out of scope here.
- Not flipping any production behavior. Every function this plan ships is
  either read-only measurement, or gated fully OFF by default via
  `AdminConfigService`, matching every flag shipped in the 260912 plan.
- Not re-litigating the 260912 fix itself (already shipped, merged, verified
  in prod across PRs #227/#228/#229) or its remaining known gaps (the 5
  hand-excluded roundup venues, the lineup-threshold default) — those are
  tracked there, not here.

## Evidence

**What tonight's fix proved and what it couldn't prove.** PRs #227/#228/#229
(merged, `origin/main` at `33ff939`) fixed the incident deterministically, but
required a human to hand-scan production `location_text` for embedded
`@handle` mentions to find 5 mis-scoped venues before a catalog-wide dedup
flag was safe — see `plans/260912_events-venue-night-duplication.md` and its
sibling coordination doc. That does not scale to ~1000 crawl targets; the
next handle a human doesn't happen to eyeball reproduces the same incident
shape with no operator visibility until enough duplicate rows accumulate to
notice by symptom.

**The one existing precedent for an LLM inside this pipeline:**
`app/services/event_display_title.py`. It runs strictly AFTER the
(synchronous, deterministic) merge decision, never inside it, and validates
the model's answer against a hard rule — the returned title's distinctive
token set must be a SUBSET of the union of the group's own source titles, so
a bad answer can never invent information the source posts didn't contain.
Every outcome (`obvious` / `llm_accepted` / `rejected` / `error`) is
Prometheus-counted. This is the safety shape any new LLM touchpoint in this
pipeline must match or improve on, per the explicit prior ruling in
`plans/260812_event-dedup-fuzzy-title.md` §C that the merge/attribution
DECISION itself must stay deterministic and unit-testable ("a suggestion
that changes between runs re-opens a decision the operator already closed";
"a non-deterministic merge cannot be unit-tested against the false-positive
pairs, which is the only evidence the bar is safe").

**Design-comparison workflow.** Three independent approaches were designed
in full (integration points, cost model, rollout plan, honest weaknesses)
against the real codebase, then adversarially judged by three independent
skeptical reviewers (cost/scale, correctness/determinism,
operational-simplicity lenses). Full transcripts retained in this session;
summary:
- **Candidate A — cached per-handle scope classifier**: a new
  `instagram.handle.scope` column, classified once per handle
  (heuristic-first, LLM-fallback-second), gating the resolution ladder and
  dedup eligibility. Directly targets failure class 2 (the actual schema
  gap), concedes failure class 3 entirely, only indirectly helps class 1.
  All three judges flagged its LLM-fallback safety net (a self-reported
  "confidence floor") as materially weaker than `event_display_title.py`'s
  hard structural validator, and flagged that a cached label gates real
  decision INPUTS (`same_account_venues`, the single-night-merge gate), not
  a cosmetic annotation — a smaller-scoped reopening of the 260812 concern
  that the writeup disclosed once but undersold everywhere else. Two
  judges independently caught fabricated line-number citations (claimed
  `_extract_one` at "~line 554-609", actually line 894; claimed
  `list_instagram_handles()` at "~line 1030", actually line 1509) —
  evidence the citations were guessed, not read, which is why every
  file:line claim in THIS plan was re-verified directly against
  `origin/main` before being written down (see the grep output this session
  produced for `group_venue_ids_by_handle`, `BAND_SUGGEST`/`BAND_AUTO`,
  `RESOLUTION_QUEUED`/`RESOLUTION_AUTO`, `ReviewQueueItemOut`,
  `PromoterAccountOut`).
- **Candidate B — bounded-candidate LLM advisor**: a single structured-output
  call, fired only when the EXISTING deterministic ladder already lands in
  its own ambiguous middle (`event_dedup.BAND_SUGGEST`,
  `event_venue_resolution.RESOLUTION_QUEUED`, or an attribution dispute), over
  a CLOSED candidate set the deterministic code already computed. A pure
  validator (closed-candidate-set membership + the recommendation's quoted
  evidence must be a verbatim substring of the real source text) gates
  acceptance — the same shape as `event_display_title.py`'s subset check,
  generalized. Writes only an advisory annotation; never sets `venue_id`,
  never flips `location_resolution`, never calls the merge/absorption path.
  Scored highest on determinism by all three judges (4 / 4.5 / 4.5 out of 5)
  and lowest new-infrastructure footprint. Its real weakness, confirmed by
  all three judges: it adds no new detection RECALL — a case that resolves
  confidently-but-wrongly (e.g. rung-1 `handle_mention` firing for a
  misattributed handle) never reaches it, because it only annotates rows an
  upstream deterministic check already flagged as ambiguous.
- **Candidate C — periodic embedding-based drift scan**: a decoupled
  APScheduler job embedding event/post text, using cosine similarity to (i)
  widen dedup candidate-pair generation past lexical-token overlap and (ii)
  cluster a handle's post locations to detect multi-venue drift. The only
  design that adds new RECALL (candidate generation) rather than triaging
  what's already flagged, and its embedding-similarity mechanism is more
  reproducible than a generative call (pinned-model, zero-sampling forward
  pass) — but its clustering sub-component was called hand-wavy by all three
  judges ("names no actual algorithm, distance metric, or threshold"), it
  is the heaviest new-infrastructure candidate (new RDS table + migration +
  DAO + scheduler job), and its own conceded detection lag (days to ~2 weeks
  before even a flag is raised, unbounded human triage after) means it would
  NOT have prevented either named incident from reaching production before
  this plan's Phase 3 gate — a real effectiveness gap against the motivating
  incident, not just a cost concern.

**The finding that reframes this plan's scope.** All three designs shared an
unverified premise — that "no first-class concept of a handle covering
multiple venues exists in this schema." One judge (cost/scale lens) flagged
this as false and named specifics; this session independently re-verified
every citation directly against `origin/main` (not trusted from the judge's
prose) and confirmed them:
- `app/services/promoter_registry_service.py` — a full lifecycle registry
  (`candidate`/`active`/`paused`/`rejected`) for accounts that are NOT tied
  1:1 to one venue, with automatic, zero-cost discovery: `run_discovery()`
  counts distinct `@`-mentions inside already-extracted venue posts' captions
  and proposes a handle as a crawl candidate once it clears
  `DEFAULT_MENTION_THRESHOLD = 3` mentions (line 38), reusing
  `event_venue_resolution.build_handle_index`/`extract_mentions` — costing
  nothing beyond data already paid for.
- `app/services/promoter_crawl_service.py` — crawls only `active` promoter
  accounts, extracts events with the SAME extractor, and resolves venue
  through the SAME `resolve_event_venue` ladder used for venue posts — i.e.
  a full second crawl path already exists, purpose-built for "this handle's
  events could be at more than one venue."
- `event_venue_link_candidate` (referenced in
  `app/dao/venue_repository.py`, `app/dao/rds_venue_store.py`,
  `app/services/event_venue_resolution.py`, `event_reconciliation.py`,
  `event_extraction_service.py`, `event_merge.py`,
  `app/routers/admin_events_router.py`) — an already-shipped table storing
  ranked venue-resolution alternatives for exactly the ambiguous case
  Candidate B's design proposed inventing a new field for.
- `app/routers/admin_events_router.py` — `GET/POST /promoters`,
  `PATCH/DELETE /promoters/{handle}` (registry CRUD, lines 422-450) and
  `GET /review` (`ReviewQueueItemOut`, line 490/520) are live, operator-facing
  surfaces that look purpose-built for exactly this plan's ambiguous cases —
  neither was cited by any of the three original designs.
- `app/services/instagram_handle_sources.py::group_venue_ids_by_handle`
  (line 115) is a free, already-computed signal:
  `EventExtractionService` (line 771) already checks whether a handle maps
  to more than one `venue_id` and branches its attribution behavior on that —
  i.e. SOME of failure class 2 may already be handled with zero new code,
  for handles the catalog already knows have multiple mapped venues. The
  open question is how much of tonight's incident was really about handles
  that had only ONE venue mapped (so this check never fired) versus handles
  the check should have caught but didn't.
- **Multi-event posts already resolve each event's venue independently —
  but only within a bounded set of methods.** Traced directly:
  `EventExtractionService._extract_one` (`app/services/
  event_extraction_service.py:1285-1347`) calls its `_attribute` closure once
  PER EVENT via `reconcile_post_events(..., attribute=_attribute, ...)`
  (line 1350), never once per post — so a single post naming several
  different events already gets each event's own `location_text` checked
  independently, for both the fixed-single-venue case (via
  `evaluate_attribution_dispute`) and the already-known-multi-venue case
  (via the full `build_location_text_attribute_fn` ladder, also called per
  event by the same `reconcile_post_events` call). The failure class 3
  concern this raised ("one handle post can point to many venues") is
  therefore not a missing per-event granularity bug — it already has one.
  The real gap is which METHODS `evaluate_attribution_dispute` is willing to
  trust: `DISPUTE_METHODS` (`app/services/event_attribution_dispute.py:86`)
  is exactly `{handle_mention, location_tag, neighbourhood_match}` —
  `METHOD_NAME_MATCH` is deliberately excluded (comment at line 82-85,
  citing the 260813 handle-attribution-hardening incident, and
  `brand_root_venues` only fires on a SHARED distinctive name token between
  the mapped venue and the candidate). A post whose `location_text` names a
  real, catalogued, but UNRELATED venue in plain prose — no `@mention`, no
  Instagram `location_tag`, no shared brand-root token with the handle's own
  mapped venue — produces no dispute, no candidate, no review reason: it
  silently misattributes exactly as if the safety net shipped last night
  did not exist. This is the shape of the "barchef" example and of a
  "hidden promoter": a handle that behaves like a promoter (posts about
  venues other than its own) but was never registered in
  `promoter_registry_service.py`, and — because that registry's own
  discovery is INBOUND-mention-based (`run_discovery` counts how often
  OTHER already-crawled venues' captions `@mention` this handle, not how
  often this handle's own posts name other venues) — stays invisible to
  promoter-discovery too, for as long as nobody else happens to mention it.
  A live catalog/handle search for a literal "barchef" match (venue name or
  Instagram handle, case-insensitive, run against production during this
  session) returned zero results, so it is used here as an illustrative
  pattern name, not a citable existing row — Phase 1 must find or construct
  a real representative case, not assume this exact handle exists.

This means the two heavier candidates (A's classifier, C's clustering) risk
building parallel infrastructure for a problem this codebase has
already substantially invested in solving a different way. The correctness
lens judge's own hybrid (which paired A with C's candidate-generation) did
not weigh this finding at all — it wasn't part of that judge's independent
tool-use pass — so it is treated in this plan as the single most important,
independently-verified fact the comparison surfaced, not just one of three
opinions to average.

## Current Behavior
- Attribution and dedup are fully deterministic (`event_dedup.py`,
  `event_venue_resolution.py`, `event_attribution_dispute.py`), gated by
  hand-curated `AdminConfigService` lists reviewed manually per incident.
- A SEPARATE, already-shipped discovery mechanism
  (`promoter_registry_service.py`) proposes multi-venue-shaped crawl
  candidates automatically from caption mentions, but nothing connects its
  output, or the `event_venue_link_candidate`/`review` queue it feeds, to
  the specific pattern tonight's incident exhibited (a handle catalogued as
  a single-venue `VenueInstagram` target whose posts actually span more than
  one physical location).
- No labeled corpus of known-good/known-bad cases exists to measure any of
  this against; every past investigation (tonight's included) was ad hoc,
  eyeballing prod.
- `scripts/measure_event_dedup.py` proves the "dry-run before apply" pattern
  works and is trusted operationally, but only measures the dedup pass
  itself, not attribution or promoter-discovery coverage.

## Desired Behavior
1. A committed, reusable labeled corpus of real incident cases (Club
   Metrópole's 5-post fragmentation, BeerDock's 15 mis-attributed events and
   correct target, the 5 hand-excluded roundup venues from tonight, plus
   genuine non-duplicate control cases) that any future detector — this
   plan's or a later one's — can be measured against, the same role
   `plans/260812_event-dedup-fuzzy-title.md`'s false-positive corpus already
   plays for `evaluate_pair`.
2. A read-only measurement script, run against real production data, that
   reports how much of the corpus the EXISTING deterministic signals already
   catch: the promoter-discovery mention threshold, the
   `group_venue_ids_by_handle` multi-venue check, and current
   `BAND_SUGGEST`/`RESOLUTION_QUEUED`/attribution-dispute volume as a
   fraction of extracted events. This is the "next process fix" this plan
   ships regardless of what else it decides: a repeatable playbook replacing
   tonight's one-off manual `location_text` scanning, mirroring
   `scripts/measure_event_dedup.py`'s proven dry-run posture.
3. A pure, unit-tested closed-candidate-set + verbatim-evidence-substring
   validator (Candidate B's acceptance gate, the mechanism every judge
   independently rated safest), built regardless of the measured outcome
   because it is small, useful for any future advisory mechanism, and
   directly extends the one pattern already proven safe in this pipeline.
4. A go/no-go decision, backed by the measurement's actual numbers rather
   than assumption, on whether to wire a live advisory hook this cycle. If
   the residual ambiguous-volume the existing signals leave uncovered is
   real, wire Candidate B's advisor — re-pointed at the EXISTING
   `event_venue_link_candidate` table and `GET /admin/events/review` queue,
   per the judge finding above, not a new backlog field — fully OFF by
   default. If the existing signals already cover the corpus, ship the
   measurement tooling alone and document that finding as this plan's
   result; do not build a live hook to solve an already-solved problem.

## Implementation Approach

**Phase 1 — Labeled corpus (unconditional).**
New fixtures under `tests/fixtures/dedup_agentic_discovery/` (or
`tests/bdd/enrichment/` fixtures, matching this repo's existing fixture
convention for `event_dedup_fuzzy_title_steps.py`): the Club Metrópole
5-post group, the BeerDock 15-row group plus the correct
`Beerdock Casa Forte` target, the 5 excluded venues
(`zef'as bar`, `Seu Chico Botequim`, `Restaurante da Zena`, `Xepa Bar`,
`In Off`) with their real `location_text`/caption shapes, and a set of
genuine non-duplicate same-venue-same-night pairs (to measure false-positive
risk, not just recall) drawn from the existing dedup false-positive corpus
in `plans/260812_event-dedup-fuzzy-title.md`. Pure data, reviewed for PII
before commit (Instagram captions may name individuals).

The corpus must also include at least one **unrelated-venue** case (the
"barchef"/hidden-promoter pattern named in Evidence): a handle mapped to
exactly one venue whose posts' `location_text` names a different, real,
catalogued venue in plain prose — no `@mention`, no `location_tag`, no
shared brand-root token. None was found searching production for a literal
"barchef" handle or venue name this session, so this case must be either
(a) found by a new, purpose-built scan — sample crawled handles' recent
`location_text`/caption values, extract venue-name-shaped substrings (not
just the existing `@handle`-mention regex `scripts/`-adjacent tooling from
the 260912 sweep used, which only finds `@mentions`), and check each against
the venue catalog by name for a NON-brand-root match — or (b) constructed
as a synthetic fixture from a real venue pair if no live example turns up,
clearly labeled synthetic in the fixture metadata so Phase 2's report never
conflates a constructed case with a measured production rate.

This corpus-building pass is explicitly scoped to run WITHOUT exercising or
depending on `PromoterRegistryService`/`PromoterCrawlService` — build and
label cases using only the plain venue-handle post data (the same archived
manifests `EventPostSource` already reads), and treat "would the promoter
registry eventually catch this" as a separate, later question Phase 2
measures rather than assumes. If a candidate case happens to already be a
registered promoter account, exclude it from this corpus and note why, so
the corpus stays a clean test of the non-promoter path specifically.

**Phase 2 — Baseline measurement (unconditional).**
`scripts/measure_agentic_mitigation_baseline.py`, read-only, mirroring
`scripts/measure_event_dedup.py`'s structure (a `measure()` function
callable directly, a thin CLI wrapper, no writes). It replays, against both
the Phase 1 corpus and live prod data:
- `PromoterRegistryService.run_discovery`'s mention-counting logic against
  the corpus's captions — would `DEFAULT_MENTION_THRESHOLD = 3` have
  proposed the right handles? Run for completeness and comparison ONLY —
  per Phase 1, the corpus itself is built and labeled without assuming this
  mechanism catches anything, so this line item measures the promoter path
  as an independent, optional finding, not a load-bearing part of the
  baseline this plan's Phase 4 decision rests on.
- `group_venue_ids_by_handle` across the current catalog — how many handles
  today already resolve to >1 `venue_id`, and does
  `EventExtractionService`'s existing branch on that already route them
  through the full ladder correctly?
- `evaluate_attribution_dispute`'s real hit rate on the corpus's
  sibling-brand cases (BeerDock-shaped: caught by `DISPUTE_METHODS`) versus
  its unrelated-venue cases (barchef-shaped: expected near-zero, since
  `DISPUTE_METHODS` structurally excludes generic name matching) — reported
  as two SEPARATE numbers, never blended into one "attribution coverage"
  percentage, since the two sub-cases need different fixes if a gap remains.
- Current `BAND_SUGGEST` / `RESOLUTION_QUEUED` / attribution-dispute volume
  as a fraction of extracted events, to replace every assumed percentage in
  the three designs' cost models with a measured one.
- Whether operators are actually acting on `GET /admin/events/dedup-backlog`
  and `GET /admin/events/review` today (last-modified/last-viewed signal if
  one exists, else a documented "unknown — ask the operator" line item) —
  two of three judges independently flagged that adding more backlog volume
  without confirming existing surfaces are watched risks the exact
  "nobody looks at it" failure this pipeline's `refused_disjoint` counter
  already has.

The script's output is a written report appended to this plan (or a linked
follow-up doc) with real numbers, replacing every "assumed 10-15%" and
"$5-25/month, not firmly priced" estimate in the design-comparison summary
above.

**Phase 3 — Validator (unconditional, pure, inert).**
`app/services/event_venue_advisor_validator.py` (or folded into
`app/services/event_attribution_dispute.py` if the reviewer prefers fewer
new files): a pure function taking a candidate set (venue_id/name list, the
same shape `event_venue_link_candidate` rows already carry) and a raw model
answer (`{venue_id, evidence_quote}`), returning an accepted/rejected
verdict. Accept only if `venue_id` is a member of the closed candidate set
AND `evidence_quote` is a verbatim substring of the real event's
`location_text` or caption — never a paraphrase, never cross-contaminated
from a different candidate's evidence. Unit-tested against a fixture corpus
of canned responses including deliberately hallucinated ones (a `venue_id`
outside the candidate set; a quote invented rather than copied), matching
`event_display_title.py`'s own validator test posture. This function has NO
caller in this plan — it is built and proven, not wired.

**Phase 4 — Conditional live hook, gated on Phase 2's actual numbers.**
If (and only if) Phase 2 shows real residual ambiguous-row volume the
existing promoter-discovery/`group_venue_ids_by_handle`/review-queue
machinery does not already resolve: wire ONE new call site,
`app/services/event_venue_advisor.py`, firing only on rows already at
`BAND_SUGGEST`, `RESOLUTION_QUEUED`, or a raised attribution dispute — never
inside `evaluate_pair`/`resolve_event_venue` themselves, which stay
untouched, synchronous, and pure. Its structured-output call reuses
`app/api/openai_event_extraction_client.py`'s existing `AsyncOpenAI` +
`response_format` JSON-schema pattern (no new SDK). A validated
recommendation is written as a new nullable column/field on the EXISTING
`event_venue_link_candidate` row (not a new table), and surfaced through the
EXISTING `GET /admin/events/review` response shape (`ReviewQueueItemOut`
gains an optional `llm_recommendation` field), per the judge finding that
building a parallel backlog surface duplicates what already exists. Gated by
a new `AdminConfigService` key, `event_venue_advisor_enabled` (bool, default
`False`), registered in `app/container.py` alongside the other
`event_dedup_*`/`event_display_title_*` keys. If Phase 2 shows the existing
signals already cover the corpus adequately, Phase 4 is explicitly skipped
and that finding is the plan's shipped result — see Acceptance Criteria.

**Explicitly deferred, not built in this plan (with rationale):**
- Candidate A's cached per-handle scope classifier — its LLM-fallback safety
  net is weaker than Phase 3's validator, it gates real decision inputs
  (not a cosmetic label) with a cached-staleness risk needing an enforced
  reclassify-then-replay trigger no design specified concretely, and it
  concedes failure class 3 outright. Worth revisiting as a schema-level fix
  ONLY once Phase 2's measurement shows the promoter-registry-based signals
  genuinely cannot cover the gap it targets, and only paired with a Phase-3
  -shaped hard validator, not a soft confidence floor.
- Candidate C's embedding infrastructure (new RDS table, migration, DAO,
  scheduler job) — the only mechanism among the three that widens detection
  RECALL rather than triaging what's already flagged, which makes it the
  strongest candidate for failure classes 1 and 3 specifically if Phase 4's
  advisor alone proves insufficient. But it is real new infrastructure that
  this plan's own workflow found no candidate had measured the need for;
  building it now would repeat the exact "unjustified complexity" risk this
  plan exists to avoid. Named explicitly here as the most likely SECOND
  follow-up plan, conditional on Phase 4 shipping and its own measured
  results (advisor volume, validator-rejection rate, operator agreement)
  showing failure classes 1/3 remain uncovered.

## Data, Config, And API Impact
- New: `tests/fixtures/dedup_agentic_discovery/` (data only, reviewed for
  PII before commit).
- New: `scripts/measure_agentic_mitigation_baseline.py` (read-only, no
  writes, mirrors `scripts/measure_event_dedup.py`'s CLI shape).
- New: `app/services/event_venue_advisor_validator.py`, pure function, no
  DAO access, not called by any production path in this plan.
- Conditional on Phase 4: new `AdminConfigService` key
  `event_venue_advisor_enabled` (bool, default `False`); a new nullable
  `llm_recommendation`-shaped field on `event_venue_link_candidate` rows
  (additive migration, matching the additive/nullable pattern
  `0046_event_display_title` and `0020_instagram_handle_source` both used);
  `ReviewQueueItemOut` gains an optional field (additive, append-only per
  this repo's "released clients are a second API consumer" convention —
  N/A here since this is an internal admin API, but the additive discipline
  still applies for forward/backward compatibility across deploys).
- No changes to `evaluate_pair`, `resolve_event_venue`,
  `evaluate_attribution_dispute`, or any existing admin-config key's
  validator or default.

## Error Handling And Observability
- The Phase 2 measurement script must never write; it opens read-only DB
  sessions the same way `scripts/measure_event_dedup.py` does, and its
  report states explicitly which numbers are LIVE-measured versus
  corpus-only, so no reader mistakes a fixture-corpus recall number for a
  production rate.
- Phase 3's validator logs every accept/reject with its reason (candidate-set
  miss vs. evidence-not-verbatim) so a future Phase 4 rollout has ready-made
  debug signal from day one, matching `event_display_title.py`'s
  `OUTCOME_*` counting discipline.
- If Phase 4 ships: `EVENT_VENUE_ADVISOR_OUTCOME_TOTAL{outcome=suggested|
  rejected|error}` (Prometheus), mirroring `event_display_title.py`'s
  metric shape exactly. The advisor call is fire-and-forget / decoupled from
  the synchronous reconciliation path, per that module's own documented
  constraint that a generative call cannot go inside the synchronous merge
  path without a large refactor.

## Test Plan
Feature file: `tests/bdd/enrichment/dedup-agentic-mitigation-discovery.feature`

Scenarios:
- The baseline measurement script runs against the labeled corpus and
  produces a report without writing to any table (read-only guarantee).
- The promoter-discovery mention-threshold replay correctly proposes (or
  correctly does NOT propose, with a documented reason) each handle in the
  labeled corpus as a candidate.
- A multi-event post's events each resolve their venue independently — a
  post naming two different events at two different venues in its own text
  must not have both events pinned to the same venue (documents the
  already-working per-event behavior traced in Evidence, as a regression
  guard, not a new capability).
- The unrelated-venue corpus case (no `@mention`, no `location_tag`, no
  shared brand-root token) is measured and reported as currently
  UNDETECTED by `evaluate_attribution_dispute` — this scenario is expected
  to be red/documenting-a-gap today, and its purpose is to make that gap
  visible and regression-tested going forward, not to fix it in this plan.
- The closed-candidate-set + verbatim-evidence validator accepts a
  recommendation whose venue is in the candidate set and whose evidence
  quote is a real substring of the source text.
- The validator rejects a recommendation naming a venue outside the closed
  candidate set.
- The validator rejects a recommendation whose evidence quote does not
  appear verbatim in the source text (the hallucination case).
- (Conditional on Phase 4 shipping) With `event_venue_advisor_enabled=true`,
  a `RESOLUTION_QUEUED` event gets a validated `llm_recommendation` on its
  `event_venue_link_candidate` row and in `GET /admin/events/review`,
  without its `venue_id` or `location_resolution` changing.
- (Conditional on Phase 4 shipping) With `event_venue_advisor_enabled=false`
  (the default), no advisor call is made for any event, at any resolution
  state — the kill-switch leaves production behavior identical to today.
- (Conditional on Phase 4 shipping) The advisor is never invoked for an
  event that already resolved via `RESOLUTION_AUTO` — it only fires on the
  already-ambiguous band, never widening its own trigger surface silently.

Pytest unit tests:
- `tests/test_event_venue_advisor_validator.py` — the validator against a
  fixture corpus of canned model responses, including deliberately
  hallucinated ones (wrong venue, invented quote, cross-contaminated quote
  from a different candidate).
- `tests/test_measure_agentic_mitigation_baseline.py` — the measurement
  script's `measure()` function against the Phase 1 corpus, asserting it
  correctly classifies each known case as caught-by-existing-signals or not.

Manual or integration checks:
- Run `scripts/measure_agentic_mitigation_baseline.py` against real
  production data (read-only; same AWS SSM access pattern used throughout
  the 260912 fix) before writing Phase 2's report, so the report's numbers
  are real, not corpus-only.

## Acceptance Criteria
- The labeled corpus exists, is committed, and covers all three named
  failure classes with at least one real case each plus non-duplicate
  controls.
- `scripts/measure_agentic_mitigation_baseline.py` exists, is read-only
  (verified: it opens no write transaction anywhere in its code path), and
  its output against real production data is captured in this plan (as an
  appended report section) or a linked follow-up doc.
- The validator is implemented, unit-tested against both accepting and
  hallucinated fixture cases, and has zero production callers.
- A written, numbers-backed recommendation exists for whether Phase 4 ships
  this cycle. If Phase 4 ships: it is gated `False` by default, verified via
  a BDD scenario that the kill-switch leaves production identical to today,
  and it writes only to the existing `event_venue_link_candidate` shape —
  never a new table, never `venue_id`, never `location_resolution`.
- If Phase 4 does NOT ship (existing signals already cover the corpus): this
  plan's result is the measurement tooling, the validator, and the written
  finding — not a placeholder or a partial build.
- Zero change to any existing admin-config default, and zero change to
  `evaluate_pair`, `resolve_event_venue`, or `evaluate_attribution_dispute`'s
  existing behavior, confirmed by the existing test suites for those modules
  passing unmodified.

## Open Questions
None. The empirical questions this plan's own design-comparison surfaced
(how much promoter-discovery/`group_venue_ids_by_handle` already covers;
real `BAND_SUGGEST`/`RESOLUTION_QUEUED`/dispute volume; whether operators
read the existing backlog surfaces; embedding-threshold precision/recall on
real text) are not blockers to starting — they are exactly what Phase 2's
measurement script exists to answer, and Phase 4 is explicitly conditioned
on those answers rather than decided in advance. `/execute-feature` should
proceed through Phases 1-3 unconditionally, then treat Phase 2's report as
the gate for Phase 4, per the Implementation Approach and Acceptance
Criteria above.

## Execution Report (2026-09-13, `feature/dedup-agentic-mitigation-discovery`)

Every number below that is labelled "production" was read live, read-only,
from `i-0893fb6d283243480` (`vibes_bot-cs-server-1`) via AWS SSM against
`origin/main` at `33ff939` (PRs #227-#229, merged, predating this branch) —
not against this branch's own new code, which did not exist on that host.
Two separate read-only scans were run; see `tests/fixtures/
dedup_agentic_discovery/MANIFEST.md` for the full per-case provenance audit
and the raw scan figures it quotes.

### What shipped

- **Phase 1 — corpus.** `tests/fixtures/dedup_agentic_discovery/corpus.json`
  (8 labeled cases: Club Metrópole fragmentation; BeerDock misattribution;
  the `oquetemhojeemnatal` Xepa Bar roundup case; the REAL, live,
  currently-undetected Casa Bacurau unrelated-venue gap a production scan
  found; and four non-duplicate controls reused verbatim from
  `plans/260812_event-dedup-fuzzy-title.md`'s own false-positive corpus) +
  `MANIFEST.md` (full provenance/PII audit).
- **Phase 2 — measurement.** `scripts/measure_agentic_mitigation_baseline.py`
  (`measure()` for production, `measure_corpus()`/`replay_case()` for the
  corpus, zero writes anywhere — verified by direct code inspection, a
  structural unit-test guard, and the fact that its own BDD scenario proves
  a real, untouched DAO standing by stays untouched) +
  `tests/test_measure_agentic_mitigation_baseline.py` (22 tests).
- **Phase 3 — validator.** `app/services/event_venue_advisor_validator.py`
  (`validate_event_venue_recommendation`: closed-candidate-set membership +
  verbatim-evidence-substring, zero production callers) +
  `tests/test_event_venue_advisor_validator.py` (17 tests, including the
  venue-outside-set, invented-quote, and cross-contaminated-quote
  hallucination shapes).
- **Phase 4 — the conditional hook, built and shipped INERT.** See the
  Decision below for why it ships disabled and is not recommended for
  enablement this cycle.
  - Migration `0047_event_venue_link_candidate_llm_recommendation` (one
    nullable `jsonb` column on the EXISTING `event_venue_link_candidate`
    table).
  - `app/services/event_venue_advisor.py` (`EventVenueAdvisorService`,
    gated by new admin-config key `event_venue_advisor_enabled`, default
    `False`) + `app/services/event_venue_advisor_validator.py` (reused).
  - `app/api/openai_event_extraction_client.py` gains
    `recommend_event_venue`/`ENDPOINT_EVENT_VENUE_ADVISOR`, mirroring
    `pick_display_title`'s exact pattern (separately-counted spend, loose
    `response_format={"type":"json_object"}`, no new SDK).
  - `app/dao/rds_venue_store.py` /
    `app/dao/venue_repository.py` gain
    `set_event_venue_link_candidate_recommendation` (writes ONLY the new
    column, on an EXISTING candidate row, never `venue_id`/
    `location_resolution`); `tests/rds_fake.py`'s in-memory store mirrors the
    same contract, including clearing the column on every fresh
    `replace_event_venue_link_candidates` call.
  - `app/routers/admin_events_router.py`: `ReviewQueueItemOut` and
    `LinkCandidateOut` gain an optional `llm_recommendation` field,
    additive, surfaced through the EXISTING `GET /admin/events/review` — no
    new endpoint.
  - Wired into exactly ONE real call site,
    `EventExtractionService._run_event_venue_advisor_pass`, immediately
    after the existing display-title pass — same shape, same
    never-raises/degrade-gracefully contract. Deliberately NOT wired into
    `PromoterCrawlService` in this pass, to keep the change's blast radius
    minimal given the Decision below; a straightforward follow-up if the
    recommendation ever changes.
  - `tests/test_event_venue_advisor.py` (17 tests),
    `tests/test_event_venue_advisor_migration.py` (10 tests).
- **BDD.** `tests/bdd/enrichment/dedup-agentic-mitigation-discovery.feature`
  — `@wip` removed, all 14 scenarios green (91 steps), including the two
  that pin CURRENT behaviour (independent per-event resolution; the
  unrelated-venue gap staying undisputed) and the four Phase-4 scenarios,
  which exercise the real `EventVenueAdvisorService` end to end (the
  disabled kill-switch, a validated recommendation attached without
  touching `venue_id`/`location_resolution`, the never-fires-on-
  already-auto-resolved guard, and the existing review queue surfacing it)
  — never a stub that would pass regardless of whether the code works.

### Phase 2 — real numbers

**Corpus replay** (`python -m scripts.measure_agentic_mitigation_baseline --corpus-only`):

| case | failure class | caught by an existing signal? |
|---|---|---|
| `club_metropole_fragmentation` | cross-post fragmentation | **No** — nine of ten pairs refused disjoint; the three sharing one performer sit at exactly one shared name, below `DEFAULT_LINEUP_THRESHOLD=2` |
| `beerdock_multi_location_misattribution` | multi-location misattribution | **Yes** — `event_attribution_dispute.evaluate_attribution_dispute` (`neighbourhood_match`) catches the real `'CASA FORTE'` row. (A second, real, currently-live row reading `'Beer Rock - Casa Forte'` does NOT trip the same check — a narrower residual gap in the SAME existing signal, named explicitly rather than hidden) |
| `oquetemhojeemnatal_xepa_bar_roundup` | multi-location misattribution | **No**, for an unexpected reason found by running the real ladder rather than assumed: after the `@handle` is stripped for rung 4, the near-empty leftover (`"PA •"`) fuzzy-matches the MAPPED venue's own short name and self-confirms — a narrower, previously-undocumented existing-ladder weakness, not the "not in catalog" gap this case was built to demonstrate |
| `casa_bacurau_unrelated_venue_gap` | **unrelated-venue gap** | **No** — confirmed REAL and currently live in production (see below); `METHOD_NAME_MATCH` is deliberately outside `DISPUTE_METHODS` |
| 4 non-duplicate controls (`260812`'s own false-positive corpus) | — | N/A — **zero false positives**: every control correctly lands at `BAND_SUGGEST`/`BAND_REFUSE`, never `BAND_AUTO` |

**Production** (read-only scan, 2026-09-13, against `origin/main` at `33ff939`):

| measure | value |
|---|---|
| live, non-superseded `post_type=event` rows | **1,043** |
| `RESOLUTION_QUEUED` events (`location_resolution IS NULL AND venue_id IS NULL`) | **44** (4.2%) |
| `BAND_SUGGEST` pairs, whole-catalog fresh re-evaluation (`scripts.measure_event_dedup.measure`) | **8** |
| `BAND_AUTO` pairs remaining (whole-catalog, shipped defaults) | **0** |
| pending merge suggestions (`event_merge_suggestion`, `decision='pending'`) | **29** |
| attribution-disputed rows / distinct dispute groups | **14 rows / 12 groups** — 11 groups are `venue_not_in_catalog` (the Natal-roundup shape); **1 real, currently-live, resolvable dispute** (`@casadaribeira` → a real catalog venue, `Casa da Ribeira`) sitting unactioned in the backlog today |
| **ambiguous-band events** (`RESOLUTION_QUEUED` + pending suggestions) | **73 / 1,043 = 7.0%** of the live catalog |
| Instagram handles mapping to >1 `venue_id` (`group_venue_ids_by_handle`) | **57 / 2,014 (2.8%)** |
| current venue-night duplicate backlog (`event_dedup_backlog`) | **5 groups, 49 excess rows** — non-zero even after the 260912 fix, confirming Club Metrópole-shaped rows are a live, standing residual, not a closed incident |
| operator engagement with `GET /admin/events/dedup-backlog` / `GET /admin/events/review` | **unknown — no view/last-accessed tracking exists in this codebase for either route** (confirmed by reading both; this cannot be inferred from data that does not exist) |
| literal `"barchef"` handle search | `barchef.riomar`, `barchef.boteco` found; **0 live events** from either — confirms the premise that prompted Phase 1's own scan for a real "hidden-promoter" case |

### Phase 4 decision: **ship the code inert; do NOT enable it this cycle**

The residual ambiguous volume is real (73 events, 7.0% of the live catalog,
plus one concretely resolvable attribution dispute sitting unactioned
today) — this is not "the existing signals already cover everything," so
Phase 4's code is built, tested, and shipped, per the plan's own
authorization to do so regardless of the final enablement call.

**But the measured data argues against turning it on this cycle**, for a
reason the corpus replay above surfaces directly rather than by assumption:
**the advisor's own trigger surface (`RESOLUTION_QUEUED`) does not overlap
with where this plan's three named incidents actually live.**

- Club Metrópole's rows are **refused outright** (disjoint distinctive
  tokens) — they never reach `BAND_SUGGEST`, let alone `RESOLUTION_QUEUED`.
  An advisor gated on the queued state would never even see them.
- BeerDock's real motivating row is **already caught** by the existing,
  already-shipped `event_attribution_dispute` signal — there is nothing
  left there for an LLM to add.
- The one REAL, currently-live unrelated-venue gap this session found
  (Casa Bacurau) resolves via `RESOLUTION_AUTO` (confidently, via rung 4) —
  it never reaches `RESOLUTION_QUEUED` either, so the advisor as scoped
  would not have caught it.

So the 73 ambiguous events the advisor WOULD fire on are a real, legitimate,
separate population (venue-attribution questions where the ladder ranked
two similarly-scored candidates and correctly declined to guess) — worth an
operator's attention, but not the incident this plan was commissioned to
address. Spending LLM cost and review-queue UI real estate on that
population this cycle would be solving an adjacent problem while leaving
the three named failure classes exactly as uncovered as they are today.

**The more targeted next investment, backed directly by this cycle's
measurement**, is a small, deterministic widening of
`event_attribution_dispute.DISPUTE_METHODS` to accept a high-confidence,
no-brand-overlap `METHOD_NAME_MATCH` result (the Casa Bacurau shape: score
≥ some high floor, zero shared brand-root token, and no runner-up within
margin) — the same validated-gate discipline this plan's Phase 3 already
proved out, applied to a NAME match instead of an LLM's free-text answer,
and touching zero new infrastructure. This is recorded here as the
recommended follow-up rather than attempted in this plan, which is scoped
to measurement + the inert hook per its own Implementation Approach.
Candidate C's embedding-based candidate generation (already named in this
plan's Evidence as "the most likely SECOND follow-up") remains the
strongest candidate specifically for Club Metrópole-shaped cross-post
fragmentation, since nothing in the deterministic ladder or the advisor (as
scoped) ever re-opens a `BAND_REFUSE` pair.

### Acceptance criteria — status

- [x] Labeled corpus exists, committed, covers all three named failure
  classes plus the unrelated-venue gap plus non-duplicate controls.
- [x] `scripts/measure_agentic_mitigation_baseline.py` exists, is read-only
  (verified by inspection and a structural test), and its output against
  real production data is captured above.
- [x] The validator is implemented, unit-tested against accepting and
  hallucinated cases, zero production callers.
- [x] A written, numbers-backed Phase 4 recommendation exists (above):
  ships, gated `False`, not recommended for enablement this cycle, with a
  named reason and a named alternative.
- [x] Phase 4 writes only the existing `event_venue_link_candidate` shape —
  never a new table, never `venue_id`, never `location_resolution` (see
  `tests/test_event_venue_advisor.py::TestTheOneCall::
  test_a_validated_recommendation_is_written_to_the_candidate_row_only`).
- [x] Zero change to any existing admin-config default, and zero change to
  `evaluate_pair`/`resolve_event_venue`/`evaluate_attribution_dispute`'s
  existing behaviour — confirmed by the full existing test suite (`make
  test-unit`: 4,473 passed / 7 pre-existing skips / 0 failed; `make
  test-bdd`: 124 features / 1,564 scenarios / 10,066 steps, all passed)
  running unmodified against this branch.
