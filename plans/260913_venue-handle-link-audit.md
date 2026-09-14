# Venue-Handle Link Audit — Catalog-Wide Sweep For Misattributed `kind='venue'` Crawl Targets

## Branch
feature/venue-handle-link-audit

## Goal
Replace ad-hoc, by-hand checking of individual `kind='venue'` crawl targets
(tonight's manual review of `real.botequim`, `editaisculturape`,
`teatroluizmendonca`) with a systematic, read-only, catalog-wide sweep that
surfaces every `(crawl_target.handle, currently-mapped venue_id)` pair whose
own posts give no corroborating evidence for that venue — the
`editaisculturape` defect shape (force-assigned, real, currently-served
misattribution) — without flagging a handle whose posts genuinely do
describe the venue they are mapped to (the `teatroluizmendonca` control).
Ship it as a gated, always-off-by-default extension of the EXISTING
`GET /admin/events/dedup-backlog` report, never an auto-reattribution path.

## Non-goals
- Auto-reattributing, auto-unlinking, or auto-removing a spurious
  `instagram.handle` row. This sweep FLAGS for operator review only, per the
  convention every sibling mechanism in this pipeline already follows
  (`event_attribution_dispute_action` defaults to `flag`, never
  `reattribute`; `event_venue_advisor` writes only an advisory
  `llm_recommendation` field). An operator remediates by hand (re-point the
  crawl target, clear the spurious venue's `instagram_handle`, or add the
  missing correct venue), exactly as `GET /admin/events/dedup-backlog`'s
  existing `attribution_disputes` section already expects.
- Modifying `resolve_event_venue`, `evaluate_attribution_dispute`,
  `_extract_one`'s force-assign closure, or any existing ladder rung. This
  plan adds a SEPARATE, read-only comparison that runs alongside the ladder,
  never inside it.
- Widening `DISPUTE_METHODS` to include `METHOD_NAME_MATCH` — that was
  considered and rejected by `plans/260913_dedup-agentic-mitigation-
  discovery.md`'s own "recommended follow-up" note (a high-confidence,
  no-brand-overlap `METHOD_NAME_MATCH` widening) for a DIFFERENT reason: this
  plan's own re-verification (see Evidence) found that naive name-match,
  unbounded against the whole catalog, is too noisy to gate even a
  *live-serving* dispute decision — "Torre Malakoff" top-scores `Torra Café`
  (0.6364, a coincidental character collision) ahead of the real landmark
  `Malakoff Tower` (0.6154); a bare `Pernambuco` top-scores `Pernambuco
  Dream` (0.80). This plan's OWN comparison (below) is calibrated
  differently — bounded to the ONE currently-mapped venue, never the whole
  catalog — specifically to avoid this exact noise source.
- `champagne.clubrecife` — the one `kind='venue'` crawl target with ZERO
  venues currently mapped to it (see Evidence) — is an orphaned-target
  defect, not a misattribution; it has no mapped venue to check evidence
  against and is out of scope here.
- A geocoding-based or Google-Places-based distance check. Confirmed
  unnecessary (see Evidence): the 90 venues currently mapped from a
  `kind='venue'` crawl target already carry a 100%-populated structured
  `neighborhood`/`street`/`city` in `venues.address`, which is sufficient
  signal on its own. No new paid API call anywhere in this plan. (Also,
  per `reference_geocoding-api-not-activated`, the Geocoding API is not even
  enabled on this GCP project.)
- Any cross-repo change. This is entirely a cs-server admin-reporting
  change: no new Redis projection field, no `vibes_bot` endpoint, no mobile
  surface. Confirmed against the wrapper's routing table
  (`vibesense_wrapper/CLAUDE.md`): "the Redis serving projection" and
  "system-of-record + engagement persistence" are the only rows that could
  apply, and this plan touches neither — it reads `venues.address` and
  `events.post_item`/`post_item_source`, and reports through an existing
  ADMIN (not serving) endpoint.

## Evidence

### Re-verification of the three seed cases (read-only, live prod, 2026-09-13, `i-0893fb6d283243480`/`vibes_bot-cs-server-1`, same commit this branch is cut from)

All three were re-checked directly against RDS via
`RdsVenueStore`/`group_venue_ids_by_handle` — not trusted from the prior
session's prose. Two of the three re-verifications **changed the root-cause
mechanism** from what was handed in as starting evidence; the conclusions
(flag/don't-flag) held, but the "why" did not, which is exactly why this
plan re-derives the detection signal from the re-verified data rather than
the original description.

1. **`real.botequim`** — **not** a single wrong mapping as described. Its
   `crawl_target` (`kind='venue'`, enabled) currently has **two** venues
   pointing at it via `instagram.handle`: `Bar Real Botequim` (Av.
   Dezessete de Agosto, 1761, Casa Forte, Recife — the correct one) AND
   `Real Bar e Lanches` (Rua Cardeal Arcoverde, Pinheiros, São Paulo — an
   unrelated venue that happens to share the generic tokens
   "Real"/"Bar"/"Botequim"). Because `group_venue_ids_by_handle` returns 2
   venue_ids for this handle, `EventExtractionService._run_handles`
   (`app/services/event_extraction_service.py:828-851`) takes the
   MULTI-venue branch and runs the full
   `event_venue_resolution.build_location_text_attribute_fn` ladder — it is
   NOT force-assigned. All 18 live events' `location_text` read "Real
   Botequim / Av. 17 de agosto, 1761 - Casa Forte" (or a `—`-separated
   variant). Rung 4 scores BOTH candidates — `Bar Real Botequim` 0.4615,
   `Real Bar e Lanches` 0.3881/0.3768 (`event_venue_link_candidate` rows,
   captured live) — and the TOP score (0.4615) is below
   `DEFAULT_CONFIDENCE_FLOOR` (0.55), so every event lands
   `location_resolution='unresolved'`, `venue_id=NULL`,
   `review_reason='unresolved_venue'`, `status='pending_review'`. Safely
   refusing is confirmed, but the mechanism is "both candidates fail the
   floor because a spurious sibling dilutes rung 4," not "zero overlap for
   any rung to latch onto." The spurious `Real Bar e Lanches` link is a
   real catalog-integrity defect in its own right: removing it would very
   likely let rung 4 or rung 3 cleanly auto-link the other 18 events to the
   correct venue (no runner-up left to fail margin against).
2. **`editaisculturape`** — confirmed exactly as described, with full
   detail. `crawl_target` kind='venue', single-mapped to `Casa da Cultura de
   Pernambuco` (São José, Recife). All 14 live events carry
   `venue_id` = that venue (force-assigned via `_extract_one`'s default
   closure, `app/services/event_extraction_service.py:1309-1339`);
   `location_resolution` is NULL on every row (the force-assign closure
   never sets it — see `app/services/event_venue_resolution.py`'s
   `RESOLUTION_QUEUED` comment: "leave the four link columns out ... stays
   NULL" is the queued case, but the FORCE-ASSIGN case's default branch
   simply never touches the column at all). 12 of 14 are `status='accepted'`
   (live, served); the other 2 are `pending_review` for an UNRELATED reason
   (`review_reason='date_range'`), not a venue flag — i.e. those two would
   ALSO auto-accept at the wrong venue the moment their date issue is fixed.
   9 of 14 events have an EMPTY `location_text` (nothing to check either
   way). Of the 5 with text, 2 are decisive, real evidence of a different
   place: `"Casa de Câmara e Cadeia de Brejo da Madre de Deus"` and `"Torre
   Malakoff"` — neither clears `name_similarity` against the mapped venue
   (0.4688 and 0.2791 respectively, both below the 0.55 floor) and neither
   contains the mapped venue's own neighbourhood (`São José`, see below).
   `evaluate_attribution_dispute` never fires on ANY of these 5, confirmed
   by code inspection: `DISPUTE_METHODS`
   (`app/services/event_attribution_dispute.py:86-88`) is `{handle_mention,
   location_tag, neighbourhood_match}` — `METHOD_NAME_MATCH` is deliberately
   excluded by that module's own docstring, and none of these 5 texts
   contain an `@handle`, a location tag, or a brand-root sibling's address
   substring. This is the real, live, currently-served gap: the evidence
   that WOULD disprove the mapping never reaches any check that looks at
   name-shaped evidence at all.
3. **`teatroluizmendonca`** — confirmed BENIGN. `crawl_target` kind='venue',
   single-mapped to `Theater Luís Mendonça` (Av. Boa Viagem, Boa Viagem,
   Recife). 10 live events (not 9 — one more accrued since the prior
   session's count). 9 of 10 `location_text` values are a spelling/
   transliteration variant of the mapped venue's own name ("Teatro Luiz
   Mendonça" vs the catalog's "Theater Luís Mendonça",
   `name_similarity`=0.8649) or that name plus its own address
   (`name_similarity` on the combined string drops to 0.4776 — see Evidence
   §Detection design below for why this matters). The one exception,
   `"Teatro Nacional"` (event `7T5GYM3H`), already carries
   `review_reason='sources_disagree'` and sits in `pending_review` —
   flagged by an EXISTING, unrelated mechanism (cross-source title/date
   disagreement, not venue resolution) — confirming no live misattribution
   here. `name_similarity("Theater Luís Mendonça", "Teatro Nacional")` =
   0.5556, just above the 0.55 floor.

### Detection-design evidence: why raw, catalog-wide name-match is not the signal, and what is

Tested directly against the live catalog (2,694 servable venues) by calling
`event_venue_resolution._name_match_candidates`/`instagram_cascade_service.
name_similarity` on the real seed texts, to answer the task's own question
("what's the right comparison") with measurement instead of assumption:

- Unbounded, catalog-wide name-match is too noisy to use as the primary
  signal. `"Torre Malakoff"` top-scores `Torra Café` (0.6364, a coincidental
  character collision) ahead of the real landmark `Malakoff Tower` (0.6154)
  — a margin of 0.02, decided by luck. A bare `"Pernambuco"` top-scores
  `Pernambuco Dream` (0.80). Even the CORRECT control case (`real.botequim`'s
  own address text against `Bar Real Botequim`) does not rank the correct
  venue in the catalog-wide top 5 at all — `name_similarity` alone is
  unreliable once `location_text` mixes a venue name with its address
  (exactly the real.botequim/teatroluizmendonca-with-address shape, both
  measured above: 0.4615 and 0.4776, both below floor, for text that is
  either fully or mostly CORRECT).
- `venues.address` already carries a STRUCTURED `neighborhood` (plus
  `street`/`city`) column, separate from the free-text `raw_text`
  (migration `0004_address_table`), and it is **100% populated for all 90
  venues currently mapped from a `kind='venue'` crawl target** (measured
  live). Folded substring containment of this neighbourhood inside
  `location_text` — the SAME folding primitive
  `event_venue_resolution._fold_for_containment` already uses, applied in
  the REVERSE containment direction from
  `_neighbourhood_match_candidates` (needle=the venue's own short
  neighbourhood, haystack=the event's often-long combined name+address
  `location_text`, since that existing function assumes the opposite: a
  short location_text fragment inside a longer address) — correctly
  separates every measured case:
  - `real.botequim` vs `Bar Real Botequim` (correct): neighbourhood `"Casa
    Forte"` found in location_text → corroborates.
  - `real.botequim` vs `Real Bar e Lanches` (wrong): neighbourhood
    `"Pinheiros"` NOT found in any of the 18 events' location_text → never
    corroborates.
  - `editaisculturape`'s two decisive texts: neighbourhood `"São José"` not
    found in either → does not corroborate.
  - `teatroluizmendonca`'s address-bearing control: neighbourhood `"Boa
    Viagem"` found → corroborates (recovering the case raw name_similarity
    alone missed, 0.4776).
- **The compound signal this plan ships**: an event's own `location_text`
  (never the caption — same precedence `resolve_event_venue`/
  `evaluate_attribution_dispute` already use, since a post's caption is
  post-level evidence shared by every event the post yields, not per-event)
  CORROBORATES a specific mapped venue when EITHER (a)
  `name_similarity(venue_name, location_text) >= DEFAULT_CONFIDENCE_FLOOR`
  (0.55 — reused, not re-derived), OR (b) the venue's own `neighborhood`
  (folded) is a substring of the folded `location_text`. A `location_text`
  shorter than 3 folded alphabetic characters is not checkable either way
  (reuses the same minimum `_neighbourhood_match_candidates` already
  enforces).
- **The flagging bar is 2, not 1, non-corroborating checkable events for a
  given `(handle, mapped_venue_id)` pair** — deliberately a PATTERN, not a
  single incidental mention, which is exactly the task's own named false-
  positive risk ("a caption mentioning a sponsor, a nearby landmark in
  passing"). Calibrated against real measured data, not picked blind:
  `editaisculturape` has EXACTLY 2 non-corroborating checkable events
  (`"Torre Malakoff"`, `"Casa de Câmara e Cadeia..."`) — a bar of 3
  (mirroring `PromoterRegistryService.DEFAULT_MENTION_THRESHOLD`) would
  miss the one confirmed real case this plan exists to catch, so 2 is used
  instead and stated here as a deliberate deviation, not an oversight.
  `teatroluizmendonca` has 0 non-corroborating checkable events (its one
  weak case, `"Teatro Nacional"` at 0.5556, still clears the floor) — safely
  under the bar regardless of whether it is set to 2 or 3.
  `real.botequim`'s `Real Bar e Lanches` pairing has 18 of 18 — comfortably
  over any reasonable bar.
- Validated against all three re-verified cases with this exact rule:
  `real.botequim`/`Real Bar e Lanches` → **flagged** (18/18
  non-corroborating); `editaisculturape`/`Casa da Cultura de Pernambuco` →
  **flagged** (2/4 checkable non-corroborating, clears the bar of 2);
  `teatroluizmendonca`/`Theater Luís Mendonça` → **not flagged** (0/10
  non-corroborating). `real.botequim`'s OTHER pairing, `Bar Real Botequim`
  (the correct venue), is also correctly NOT flagged (0/18
  non-corroborating) — the sweep isolates the spurious half of a
  double-mapped handle without also flagging the correct half.

### Scope and cost

- `events.crawl_target` has **83** `kind='venue'` rows (78 enabled, 5
  disabled) and 2 `kind='promoter'` rows (both enabled) — measured live via
  `RdsVenueStore.list_crawl_targets()`. Of the 83: **76** currently map to
  exactly one venue (the force-assign population this plan's core defect
  shape lives in), **6** map to 2-3 venues (`bardolula`×3,
  `botecodocaranguejo`×2, `entreamigosobode`×2, `marcellos_music_bar`×2,
  `real.botequim`×2, `tatubola.bar`×3 — already routed through the full
  ladder, per Evidence item 1, but still worth auditing for spurious
  co-mapped venues exactly as `real.botequim` demonstrates), and **1**
  (`champagne.clubrecife`) maps to zero (out of scope — see Non-goals).
  90 distinct venues total across the 82 non-orphaned targets.
- Pure DB reads, no paid API call anywhere: one bulk `venues.address`
  read for the ~90 mapped venue_ids (new, narrow DAO method — see
  Implementation Approach), plus the events already reachable by
  `source_handle` for each of those 82 handles (bounded by the live
  catalog's total event count, measured at 1,043 rows catalog-wide as of
  the prior session, of which the vast majority are `kind='venue'`-sourced
  venue posts). Each event costs one `name_similarity` call plus one
  substring check per its own mapped venue(s) — O(checkable events ×
  ~1.07 mapped venues/handle), never O(whole 2,694-venue catalog) per
  event. That catalog-wide shape is the EXACT one whose cost already bit
  this codebase once — `event_venue_resolution.build_venue_catalog`'s own
  docstring: "`event_dedup_backlog` ... made `GET /admin/events/dedup-
  backlog` take minutes at 85% CPU" — and this plan's design was chosen
  specifically to avoid repeating it (see the catalog-wide noise findings
  above, which made the O(catalog) approach both slower AND less accurate).

### Where to surface it

`GET /admin/events/review` was considered and rejected: its population
(`VenueRepository.list_events_awaiting_decision`,
`app/routers/admin_events_router.py:552-578`) is scoped to events actually
AWAITING a venue decision (`location_resolution IS NULL`/queued or an
attribution dispute) with ranked `LinkCandidateOut` rows to choose between.
`editaisculturape`'s force-assigned events have a non-NULL `venue_id` and
are mostly `status='accepted'` — they are not "awaiting a decision" in that
UI's sense, and this sweep has no ranked alternative candidate to offer (it
knows the mapping is suspect, not what the right venue is, unless an
operator supplies one). `GET /admin/events/dedup-backlog`
(`app/routers/admin_events_router.py:681-695`, `DedupBacklogOut`) is the
better fit: it is ALREADY the report-only "attribution health" aggregate
(`venue_night_groups`, `attribution_disputes`), already paginated, already
documented as "reading ... never changes a row" (`event_dedup_backlog.py`'s
own module docstring), and its existing `attribution_disputes` section is
keyed by `(source_handle, location_text)` with `row_count`/`event_ids` —
structurally close to what this plan needs, but NOT the same population
(attribution_disputes is events that already tripped a rung-1/2/3 dispute;
this plan's population is specifically the one `DISPUTE_METHODS` structurally
cannot reach). Per `plans/260913_dedup-agentic-mitigation-discovery.md`'s
own explicit rule ("reported as two SEPARATE numbers, never blended"), this
plan adds a NEW, separate field on the SAME response rather than widening
`attribution_disputes`'s meaning.

## Current Behavior
- A `kind='venue'` crawl target's events are attributed by whichever venues
  `group_venue_ids_by_handle` currently returns for its handle: force-
  assigned with no ladder call when exactly one venue maps (the common
  case, 76 of 83 live targets), or run through the full resolution ladder
  when more than one does (6 of 83).
- The force-assign path's only safety net, `evaluate_attribution_dispute`,
  is structurally blind to name-shaped evidence (`METHOD_NAME_MATCH`
  excluded from `DISPUTE_METHODS` by deliberate, documented design —
  `event_attribution_dispute.py:36-43`) — so a handle whose crawl-target
  mapping was simply set up wrong (not a sibling-brand chain, just a wrong
  venue) has no automatic check at all today, confirmed live by
  `editaisculturape`.
- Nothing today aggregates "does this handle's own post history actually
  support its catalog mapping" across the 83 `kind='venue'` targets. The
  only way to find a case like `editaisculturape` today is what tonight's
  session did: hand-check one target at a time.

## Desired Behavior
1. A pure, unit-tested comparison — "does this event's own `location_text`
   corroborate this specific mapped venue" — implemented once, reused for
   every `(handle, mapped_venue_id)` pair, never duplicated per caller.
2. A pure aggregation — "does this `(handle, mapped_venue_id)` pair have at
   least 2 checkable events that corroborate NO mapped venue at all" —
   producing one flagged candidate per suspect pair, carrying the handle,
   every currently-mapped venue (so a multi-mapped handle's correct sibling
   is visible alongside the flagged one, as `real.botequim` demonstrates),
   the non-corroborating `location_text` samples, and each one's raw
   `name_similarity` score for operator transparency.
3. A read-only measurement script (`scripts/measure_venue_link_audit.py`),
   mirroring `scripts/measure_event_dedup.py`'s/`measure_agentic_mitigation_
   baseline.py`'s dry-run convention, runnable against real production data,
   validated first against the 3 re-verified seed cases as fixtures before
   being trusted against the other 79 `kind='venue'` targets.
4. The aggregation surfaced through `GET /admin/events/dedup-backlog`, as a
   new, additive, separately-labeled field — gated OFF by default via a new
   `AdminConfigService` key, so enabling it is an explicit operator choice
   and disabling it costs nothing (the new computation is skipped entirely,
   not just hidden).
5. Never a write. No function this plan ships touches `venue_id`,
   `location_resolution`, `instagram.handle`, or any existing admin-config
   default.

## Implementation Approach

**`app/services/venue_link_audit.py` (new, pure core + thin DAO adapter,
mirroring `event_dedup_backlog.py`'s `compute_*`/`collect_*` split).**
- `venue_corroborates_location_text(venue_name, venue_neighborhood,
  location_text) -> bool`: the one comparison, reusing
  `instagram_cascade_service.name_similarity` and
  `event_venue_resolution.DEFAULT_CONFIDENCE_FLOOR` verbatim for the
  name-match half, and the same character-folding primitive
  `event_venue_resolution._fold_for_containment` already defines for the
  neighbourhood-containment half (imported directly, or promoted to a
  shared, non-underscored name during execution — either is acceptable,
  decided during `/execute-feature`). Returns `False` (not checkable, not
  corroborating — callers must treat these differently) when
  `location_text` folds to fewer than 3 alphabetic characters; the caller
  excludes those from the checkable denominator rather than counting them
  as non-corroborating.
- `compute_venue_link_audit(handle_venue_pairs, events_by_handle, *,
  min_non_corroborating=2) -> list[VenueLinkAuditCandidate]`: PURE — takes
  already-fetched `(handle, [mapped_venue rows])` pairs and each handle's
  already-fetched events, never touches a DAO, mirroring
  `compute_dedup_backlog`'s own stated posture. Flags a pair when its count
  of checkable-but-non-corroborating events is `>= min_non_corroborating`
  (named constant, default 2, with the Evidence section's calibration
  reasoning in its docstring so a future reader does not "round it up" to
  3 without re-reading why that would silently un-flag the one confirmed
  case).
- `collect_venue_link_audit(dao, redis_like=None) -> list[VenueLinkAuditCandidate]`:
  the thin adapter. Reads `dao.list_crawl_targets(kind="venue")`,
  `dao.list_instagram_handles()`/`group_venue_ids_by_handle` for the current
  mapping, the new bulk address-components read (below) for every distinct
  mapped venue_id, and `dao.list_events_by_handle(handle)` per target,
  filtering `status != 'superseded'`; delegates to `compute_venue_link_audit`.

**New, narrowly-scoped DAO read** (not a new migration — these columns
already exist): `RdsVenueStore.get_address_components_for_venue_ids(venue_ids)
-> dict[str, dict]` (street/neighborhood/city per venue_id, one bulk `SELECT
... WHERE venue_id = ANY(:ids)` against `venues.address`), exposed through
`VenueRepository` the same way every other bulk venue read already is.
Bounded to the caller-supplied ids (≈90 here) — never a catalog-wide read.

**`app/container.py`**: register a new `AdminConfigService` key,
`venue_link_audit_enabled` (bool, default `False`), alongside
`event_attribution_dispute_action`/`event_venue_advisor_enabled`, with its
own `validate_venue_link_audit_enabled_config` (bool type-check, mirroring
`validate_dispute_withhold_enabled_config`).

**`app/routers/admin_events_router.py`**: `DedupBacklogOut` gains
`venue_link_audit_candidates: list[VenueLinkAuditCandidateOut]` (additive)
and `venue_link_audit_total: int`. A new `VenueLinkAuditCandidateOut`
(handle, `mapped_venues: list[{venue_id, venue_name, corroborated:
bool}]`, `sample_location_texts: list[str]`, `non_corroborating_count`,
`checkable_count`). `get_dedup_backlog()` reads the new config key first;
when `False` (the shipped default), `collect_venue_link_audit` is never
called — the response is byte-identical to today's shape plus two empty/
zero fields, and the expensive half of the computation does not run at
all, the same "disabled costs nothing" discipline
`event_venue_advisor_enabled` already established.

**`scripts/measure_venue_link_audit.py`** (new, read-only CLI): a
`measure()` function callable directly (for the pytest below) plus a thin
CLI wrapper, mirroring `scripts/measure_event_dedup.py`'s and
`measure_agentic_mitigation_baseline.py`'s exact shape (no writes, a
`--corpus-only` flag to replay `tests/fixtures/venue_link_audit/corpus.json`
instead of hitting RDS). Prints, per flagged pair: the handle, every
currently-mapped venue with its corroborated/not status, and the
non-corroborating `location_text` samples with their scores — enough for an
operator to decide the fix without opening a second tool.

**`tests/fixtures/venue_link_audit/corpus.json` + `MANIFEST.md`** (new,
mirroring `tests/fixtures/dedup_agentic_discovery/`'s own precedent): the
three re-verified cases from this plan's Evidence —
`real_botequim_double_mapped_handle` (expected: flag `Real Bar e Lanches`,
do not flag `Bar Real Botequim`), `editaisculturape_force_assigned_wrong_venue`
(expected: flag), `teatroluizmendonca_benign_control` (expected: do not
flag) — with real `location_text` values and scores captured in this plan's
Evidence section, reviewed for PII before commit (none found: these are
venue/municipal-bulletin/theater captions, no personal names).

## Data, Config, And API Impact
- New: `app/services/venue_link_audit.py` (pure functions, no DAO access in
  the `compute_*` half).
- New: one narrow DAO method on `RdsVenueStore`/`VenueRepository` reading
  EXISTING `venues.address.street/neighborhood/city` columns — no
  migration.
- New: `AdminConfigService` key `venue_link_audit_enabled` (bool, default
  `False`).
- `DedupBacklogOut` gains two additive fields
  (`venue_link_audit_candidates`, `venue_link_audit_total`); no existing
  field changes shape or meaning. No new endpoint.
- New: `scripts/measure_venue_link_audit.py` (read-only) and
  `tests/fixtures/venue_link_audit/corpus.json` (data only).
- No change to `resolve_event_venue`, `evaluate_attribution_dispute`,
  `_extract_one`, any existing admin-config default, or any Redis key.

## Error Handling And Observability
- `collect_venue_link_audit` must never write — verified by inspection
  (like `collect_dedup_backlog`) and by a structural pytest guard (no DAO
  method it calls has a write side effect) mirroring
  `measure_agentic_mitigation_baseline.py`'s own "opens no write
  transaction" guard.
- The new DAO read degrades to an empty dict on a venue_id with no
  `venues.address` row (should not happen given the measured 100% coverage,
  but the code must not assume it holds forever as the catalog grows) —
  such a venue's pair is then judged on `name_similarity` alone, never
  raising.
- No new Prometheus metric is required: this is an on-demand report over
  already-stored rows, computed only when an operator calls the endpoint
  with the flag on (mirroring `collect_dedup_backlog`'s own unmetered
  posture) — not a background job or a hot request path.
- Logs: `collect_venue_link_audit` logs one line per newly-flagged pair
  (handle, non-corroborating count) at INFO, the same "venue-acquisition
  backlog" logging posture `_venue_not_in_catalog_result` already uses, so
  an operator scanning logs sees new findings without needing to poll the
  endpoint.

## Test Plan
Feature file: `tests/bdd/enrichment/venue-handle-link-audit.feature`

Scenarios:
- A `kind='venue'` crawl target whose mapped venue's own posts never
  mention it by name or neighbourhood, across at least two posts, is
  surfaced as a venue-link-audit candidate in `GET /admin/events/dedup-
  backlog`.
- A `kind='venue'` crawl target whose mapped venue's posts consistently
  name or neighbourhood-match it is never surfaced, even when one single
  post's caption mentions an unrelated place in passing (the
  `teatroluizmendonca`-shaped false-positive guard).
- A handle mapped to more than one venue (the `real.botequim` shape)
  surfaces ONLY the venue its posts give no evidence for, never the
  sibling its posts do corroborate.
- With `venue_link_audit_enabled=false` (the shipped default), `GET
  /admin/events/dedup-backlog`'s response is unchanged from today except
  for the two new, empty/zero additive fields, and the underlying
  computation is never invoked.
- `scripts/measure_venue_link_audit.py --corpus-only` reproduces the three
  expected verdicts from `tests/fixtures/venue_link_audit/corpus.json`
  without writing to any table.
- An orphaned `kind='venue'` target with zero currently-mapped venues (the
  `champagne.clubrecife` shape) is skipped, not flagged and not errored.

Pytest unit tests:
- `tests/test_venue_link_audit.py` — `venue_corroborates_location_text`
  against real captured values (name-match branch, neighbourhood-
  containment branch, the too-short-to-check branch) and
  `compute_venue_link_audit` against the three fixture cases plus the
  multi-mapped-handle isolation case (flag one sibling, not the other).
- `tests/test_measure_venue_link_audit.py` — `measure()` against the
  corpus, mirroring `test_measure_agentic_mitigation_baseline.py`'s
  structure.

Manual or integration checks:
- Run `scripts/measure_venue_link_audit.py` (no `--corpus-only`) against
  real production data, read-only via the established AWS SSM pattern,
  before enabling the admin-config flag anywhere, and record the real
  per-target findings for the other 79 `kind='venue'` targets not yet
  individually checked (this plan's Scope section covers only the 3
  re-verified seeds).

## Acceptance Criteria
- `venue_corroborates_location_text`/`compute_venue_link_audit` exist, are
  pure, and are unit-tested against the three re-verified real cases in
  `tests/fixtures/venue_link_audit/corpus.json`: `real.botequim` flags
  `Real Bar e Lanches` and does NOT flag `Bar Real Botequim`;
  `editaisculturape` flags `Casa da Cultura de Pernambuco`;
  `teatroluizmendonca` does not flag `Theater Luís Mendonça`.
- `scripts/measure_venue_link_audit.py` exists, is read-only (verified by
  inspection and a structural test), and its output against real
  production data is captured as a follow-up note once run (see Manual
  checks) — not required to land in this plan's own text before merge, but
  required before the admin-config flag is ever flipped on anywhere.
- `GET /admin/events/dedup-backlog` gains the two new fields; with
  `venue_link_audit_enabled=false` (default), a BDD scenario pins the
  response otherwise byte-identical to pre-change behaviour.
- Zero change to `resolve_event_venue`, `evaluate_attribution_dispute`,
  `_extract_one`, or any existing admin-config default — confirmed by the
  existing test suites for those modules passing unmodified.
- No migration; the new DAO read targets only pre-existing
  `venues.address` columns.

## Open Questions
None. The two questions this plan's own investigation needed to resolve
before design — whether a catalog-wide sweep is pure-DB-read (yes,
confirmed: 90 mapped venues, existing `neighborhood` column 100%
populated, no paid API) and what distinguishes the real positive from the
benign control (the neighbourhood-containment-or-name-match corroboration
check, with a pattern bar of 2 non-corroborating checkable events) — are
both answered and validated against live production data in this plan's
Evidence section, not deferred to `/execute-feature`.
