# Musical Events Scope Gate

## Branch
feature/musical-events-scope

## Goal
Going forward, non-musical events must (1) never trigger the pipeline's
per-event OpenAI/agentic calls, and (2) never reach the Redis serving
projection — "avoid llm costs, agentic review, etc" and "won't be surfaced or
considered," in the operator's own words. Music-category, off-vocabulary, and
null-category events must keep behaving exactly as they do today (fail-open:
uncertainty is never read as "exclude").

## Non-goals
- Re-processing or deleting the ~1236 historical `events.post_item` rows
  already extracted. This plan changes behavior for new/ongoing rows and for
  what the *serving projection* selects from existing rows; it does not
  backfill, re-classify, or purge anything.
- Gating `VenueLinkAuditReviewerService`/`collect_venue_link_audit` (the K=10
  handle-venue-link reviewer). See Evidence and Implementation Approach for
  why this is a different axis and stays out of scope here.
- Changing `DEFAULT_CATEGORY_VOCABULARY` itself, or re-litigating extraction
  prompt behavior.
- Building a full curated list of every observed off-vocabulary string
  (~90 distinct raw values exist in production today). The mechanism is
  admin config that grows from evidence, matching this repo's established
  pattern (see Evidence); this plan seeds it conservatively, not exhaustively.

## Evidence

**The motivating example is NOT caught by the agentic-cost passes at all.**
The 4 "FÉRIAS AMIGOS PARK" events at venue "Entre Amigos O Bode" were
deterministically venue-resolved from a single source each (per the
operator's own report), so they were never `RESOLUTION_QUEUED`
(`app/services/event_venue_advisor.py:180`, `_is_resolution_queued`) and never
had 2+ source titles to reconcile
(`app/services/event_display_title.py:299`, `len(source_titles) < 2`). Neither
agentic pass would have touched them. The only mechanism that actually stops
this example from being served is a category check inside the serving
projection's own selection predicate — `is_selectable`
(`app/services/event_projection_selection.py`). This reframes the plan: the
LLM-cost gate and the surfacing gate are two separate, both-necessary
mechanisms, not one.

**Category IS reliably populated.** Queried production directly (read-only,
via SSM on `i-0893fb6d283243480` / container `vibes_bot-cs-server-1`,
`RdsVenueStore(settings.rds_sqlalchemy_url)`): of 1236 non-superseded
`post_type='event'` rows (any status), only 27 (2.18%) have `category IS
NULL`. Null-category gating can safely default to "still eligible" without
losing meaningful coverage either way.

**The 17-value seeded vocabulary is already stale on contact with real
data — an allow-list would be unsafe.** The same query found **90 distinct
raw `category` values** in production against the 17-value
`DEFAULT_CATEGORY_VOCABULARY` (`app/models/post_category.py:38`). Off-vocabulary
values include unambiguous MUSIC genres never in the seed list: `reggae` (8),
`brega` (8), `forró / sertanejo` (6), `choro / baião` (1), `choro` (1),
`frevo` (1), `hip hop` (1), `metal` (1), `techno` (1). An allow-list ("only run
agentic passes when category exactly matches a known music value") would
silently kill LLM cost-avoidance's *complement* — i.e. wrongly suppress
agentic help for real music events — for this entire tail. A deny-list that
defaults unlisted/off-vocabulary/null values to "still eligible" is the only
safe shape, and it matches `post_category.py`'s own stated philosophy
verbatim: "An off-vocabulary answer is NEVER rejected... a miss is counted,
not refused."

**Real "live" blast radius (approximate — mirrors `is_selectable`'s row-local
rule; recurring-source-freshness join, not the weekday-pattern expansion),
queried the same way:** 43 rows currently pass `status IN
('accepted','confirmed')`, `venue_id IS NOT NULL`, and the temporal bound.
Breakdown: `party` 10, `samba / pagode` 5, `live music` 5, `forró` 4, `rock` 4,
`comedy` 4, `DJ / club night` 2, and one each of `happy hour`, `sertanejo`,
`karaoke`, `workshop`, `excursion`, `teatro`, `brega`, `regatta viewing`,
`Rap / Trap`. Under the seed deny-list proposed below (see Desired Behavior),
5 of these 43 (`workshop` ×1, `comedy` ×4 — 11.6%) would stop being selected.
Across the full 1236-row historical population, the same seed deny-list
matches 123 rows (9.9%): `food festival` 12, `sports screening` 12, `kids /
family` 31, `workshop` 59, `comedy` 9, `quiz / trivia` 0.

**Both agentic passes fire at one late, shared choke point — after category
is already known.** `category` is computed once per post during extraction
(`app/services/event_extraction_service.py:1199`, `canonicalize_category`),
long before either agentic pass runs. Both passes fire only at the very end of
a full extraction run, over the SAME `self._run_touched_event_ids` list:
`_run_display_title_pass` (`event_extraction_service.py:734`) and
`_run_event_venue_advisor_pass` (`event_extraction_service.py:745`), both
called from lines 707/716. Each service's `run_for_events` then re-fetches the
full row per event id via `self.venue_dao.get_event(event_id)` — which already
includes `category` (`RdsVenueStore._EVENT_SELECT`,
`rds_venue_store.py:927`) — before deciding whether to spend a model call:
- `EventDisplayTitleService.run_for_events`
  (`app/services/event_display_title.py:270`) tries a FREE deterministic path
  first (`obvious_canonical_title`, line 303) and only escalates to the paid
  `_pick_with_model` (line 311, the actual OpenAI call) when that fails.
- `EventVenueAdvisorService.run_for_events`
  (`app/services/event_venue_advisor.py:163`) only reaches its OpenAI call
  (`_advise_one`, line 190) for a row that is both `RESOLUTION_QUEUED`
  (line 180) and has candidate rows (line 186).

Note: the user's brief located the "dedup fuzzy title-pick LLM call" in
`app/services/event_dedup.py` — that file holds only PURE grouping/matching
helpers (`evaluate_pair`, `distinctive_set`, etc.) with no OpenAI call at all.
The actual LLM call is `EventDisplayTitleService._pick_with_model` in
`app/services/event_display_title.py`; this plan corrects that pointer.

**The venue-link-audit reviewer (K=10 pass) is a different axis and does not
fit this gate.** `VenueLinkAuditReviewerService.run`
(`app/services/venue_link_audit_reviewer.py:434`) is not wired into the
extraction pipeline at all (no call site in `event_extraction_service.py` or
`container.py`'s background jobs) — it is operator-triggered
(`scripts/review_venue_link_audit.py`,
`app/routers/admin_events_router.py`). More importantly,
`collect_venue_link_audit` (`app/services/venue_link_audit.py:285`) and
`build_review_evidence` (`venue_link_audit_reviewer.py:262`) operate per
**Instagram handle / venue pair**, folding ALL of a handle's live events'
`location_text` together as corroborating/non-corroborating evidence for one
venue-identity question. A single handle can post both musical and
non-musical events; the venue-link question ("is this handle correctly mapped
to this venue?") is orthogonal to any one event's genre. There is no
per-event category hook here without conflating two unrelated questions —
confirmed out of scope, not silently skipped.

**Reversibility precedent: hide, never delete, when a row-local field can
already answer the question.** `app/models/promoter_event_visibility.py`
hides promoter-sourced events from the admin console via an admin-config
boolean specifically because "hiding is reversible while re-statusing 587
rows would not be" — and that plan needed a NEW flag because promoter-only
sourcing isn't a field `is_selectable` can see (it needs a grouped read across
`post_item_source` rows). Category is different: it is ALREADY a native,
row-local column `is_selectable` can see directly, the same way it already
checks `post_type`/`status`
(`app/services/event_projection_selection.py:101-107`). No new "hidden" flag
and no delete are needed at all — extending `is_selectable`'s own row-local
predicate makes exclusion inherently reversible (edit the admin-config
deny-list, the next ~2-minute projection cycle reflects it) without a backfill
or an "un-delete." The operator's "scrap" wording is read here as "don't
serve it," not literally "destroy the row" — the row stays fully visible in
the admin console, which queries independently of `is_selectable`
(`app/routers/admin_events_router.py:360`, `list_events` — confirmed to have
no `is_selectable` dependency).

## Current Behavior
- `category` is extracted, canonicalized against an admin-config vocabulary,
  and stored on every `events.post_item` row, but nothing downstream reads it
  as a gate.
- The display-title and venue-advisor OpenAI passes run for every touched
  event whose OWN criteria (multi-source titles; `RESOLUTION_QUEUED`) are met,
  regardless of category.
- `is_selectable` selects any row that is `post_type='event'`, has an
  `accepted`/`confirmed` status, a resolved `venue_id`, no `superseded_by`,
  and passes its temporal rule — regardless of category.

## Desired Behavior
- A new pure predicate, `is_music_category(category, non_music_categories)`,
  added to `app/models/post_category.py` (alongside `is_in_vocabulary`,
  reusing the same `casefold()` match rule): returns `False` only when
  `category` case-insensitively matches an entry in `non_music_categories`;
  returns `True` for a `None`/blank category, an off-vocabulary category, or
  any category not on the list. Fail-open by construction.
- A new admin-config key, `admin_config:event_non_music_categories` (list of
  strings), mirroring the exact Redis-mirror-with-fallback shape
  `post_category_vocabulary` and `hide_promoter_events` already use
  (`load_*`/`validate_*` pair, `fallback_reason` on read, defaults on any
  read/shape problem, never raises). Seeded default —
  `("kids / family", "workshop", "food festival", "tasting",
  "sports screening", "comedy", "quiz / trivia")`.
  - Boundary reasoning for the vocabulary's 17 values: `live music`, `samba /
    pagode`, `forró`, `rock`, `MPB`, `jazz`, `sertanejo`, `funk`, `karaoke`
    are unambiguous music — excluded from the deny-list (i.e. stay eligible).
    `kids / family`, `workshop`, `food festival`, `tasting`, `sports
    screening` are unambiguous non-music — seeded onto the deny-list.
    `DJ / club night` and `party` are judged MUSIC here: this app's whole
    product is nightlife/venue busyness, and in that domain a "festa"/club
    night is overwhelmingly music-driven (a DJ literally plays music); `party`
    is also production's single largest ambiguous bucket (94 historical
    rows, 10 of the 43 "live" rows) — excluding it would gut coverage for a
    plausibly-music-heavy slice on a guess. `comedy` and `quiz / trivia` are
    judged NON-MUSIC: both are explicit non-musical entertainment formats
    (like the already-agreed `kids / family`/`workshop`), and the cost of
    being wrong is low — `comedy` is 9 historical / 4 live rows, `quiz /
    trivia` is 0 in production today.
  - The wider off-vocabulary tail (`teatro`, `exhibition`, `feira`, `reading`,
    etc.) is deliberately NOT pre-enumerated here — it defaults to eligible
    (fail-open) and grows onto the deny-list from operator evidence, the same
    way `post_category_vocabulary` itself is documented to grow.
- `EventDisplayTitleService.run_for_events` and
  `EventVenueAdvisorService.run_for_events` each load
  `admin_config:event_non_music_categories` once per call (mirroring how
  `category_vocabulary` is already loaded once per extraction run at
  `event_extraction_service.py:1035`) and skip a non-music row immediately
  after the existing `row is None`/`status == "superseded"` check — before
  `obvious_canonical_title`/candidate-row lookup, so no free OR paid work
  happens for it. Each records a new outcome label on its EXISTING counter
  (`EVENT_DISPLAY_TITLE_TOTAL`, `EVENT_VENUE_ADVISOR_OUTCOME_TOTAL`) rather
  than adding a new metric family.
- `is_selectable` (`app/services/event_projection_selection.py`) gains a new
  row-local check, alongside its `post_type`/`status`/`venue_id`/
  `superseded_by` checks: a row whose `category` is on the (live,
  caller-supplied) non-music deny-list is not selectable. Threaded through
  exactly like `max_recurring_source_age` already is — a keyword parameter
  with a pure-module default, loaded live by each caller. Because
  `is_selectable`'s own docstring states the real store's SQL WHERE clause is
  a direct transliteration of this function, `RdsVenueStore`'s projection
  query gains the equivalent `category NOT IN (...)` (case-insensitive)
  condition, kept in lockstep with the Python predicate exactly as every
  other `is_selectable` criterion already is.
- Existing already-served non-music rows (if any) simply stop being selected
  on the next projection cycle after deploy — no backfill, no delete, no new
  flag on the row itself. Reversible by editing the admin-config list.

## Implementation Approach
1. `app/models/post_category.py`: add
   `ADMIN_CONFIG_NON_MUSIC_CATEGORIES_KEY`, `DEFAULT_NON_MUSIC_CATEGORIES`,
   `validate_non_music_categories_config`, `load_non_music_categories`,
   `is_music_category` — same shape as the existing vocabulary functions in
   this module, so extraction-time and projection-time callers share one
   matching pass (this module's own stated contract for
   `canonicalize_category`/`classify_category`).
2. `app/container.py`: register `event_non_music_categories` in the
   admin-config validator map (mirrors the two existing entries at
   `container.py:783-786`).
3. `app/services/event_display_title.py` and
   `app/services/event_venue_advisor.py`: load the deny-list once at the top
   of `run_for_events`; add the category check immediately after the
   existing `row is None`/superseded guard in each per-event loop; bump a new
   outcome label on each service's existing counter.
4. `app/services/event_projection_selection.py`: add the `category`
   deny-list parameter and check to `is_selectable`, following the same
   pattern `max_recurring_source_age` already establishes for a
   live-config-backed, pure-function parameter.
5. `app/dao/rds_venue_store.py`: thread the live deny-list into
   `list_events_for_projection`'s call site the same way the live recurring
   max-source-age setting already is, and add the equivalent SQL condition to
   the real query so the two stay in lockstep (mirrors how
   `venue_eligibility.evaluate()`/`serving.eligible_venue` are already kept in
   lockstep by hand).
6. The in-memory fake store used by BDD/pytest applies `is_selectable`
   directly, so it needs no separate SQL-equivalent change — this is exactly
   the parity property `is_selectable`'s own docstring describes.

## Data, Config, And API Impact
- New admin-config key `admin_config:event_non_music_categories` (list of
  strings), validated/persisted through the existing `AdminConfigService`
  dispatch mechanism — same shape as `post_category_vocabulary` and
  `hide_promoter_events`.
- `is_selectable`'s signature gains one new keyword parameter with a
  pure-module default (non-breaking for existing callers/tests that don't
  pass it).
- No Redis key format change. No app-facing API response field is removed or
  renamed (append-only concern from `CLAUDE.md`/released-clients precedent
  does not apply — this changes which ROWS are served, not any row's shape).
- The Redis serving projection will contain fewer rows for non-music
  categories going forward; vibes_bot and mobile need no code change (they
  already only ever see what the projection selects).
- Admin console listing (`admin_events_router.list_events`) is unaffected —
  it does not depend on `is_selectable` and will continue showing excluded
  non-music rows for operator review.

## Error Handling And Observability
- `load_non_music_categories` never raises: missing key, invalid JSON, or
  invalid shape all fall back to `DEFAULT_NON_MUSIC_CATEGORIES`, logged as a
  warning with a `fallback_reason`, matching every other admin-config loader
  in this module family.
- `EVENT_DISPLAY_TITLE_TOTAL`/`EVENT_VENUE_ADVISOR_OUTCOME_TOTAL` each gain a
  new `outcome` label value (e.g. `skipped_non_music`) so an operator can see
  the scope filter's actual effect and cost avoidance directly, without a new
  metric family.
- `is_selectable`'s new check needs no new metric on its own (it is a pure
  predicate with no I/O), but the existing events-projection category-outcome
  counter (`events_projection_category_total`, `app/models/post_category.py`
  §Outcome labels) already exists as the standing monitor for category
  distribution drift and needs no change.

## Test Plan
Feature file: `tests/bdd/enrichment/musical-events-scope.feature`

Scenarios:
- A post extracted with a deny-listed category (e.g. `kids / family`) never
  triggers the display-title model call, even when its group has 2+ source
  titles and no obvious canonical pick.
- A post extracted with a deny-listed category never triggers the
  venue-advisor model call, even when it is `RESOLUTION_QUEUED` with
  candidate venues.
- A post with a music category (e.g. `rock`), an off-vocabulary category
  (e.g. `reggae`), and a null category all remain eligible for both passes
  (regression guard against an accidental allow-list).
- The serving projection never selects an event whose category is
  deny-listed, even when every other `is_selectable` criterion (status,
  venue_id, temporal window) passes — covering the motivating
  deterministically-resolved-venue case directly.
- The serving projection's behavior for music, off-vocabulary, and null
  categories is unchanged (regression guard).
- Editing `admin_config:event_non_music_categories` changes which categories
  are excluded on the next read, without any code change (mirrors the
  existing `hide_promoter_events`/`post_category_vocabulary` live-edit
  scenarios' shape).

Pytest unit tests:
- `app/models/post_category.py::is_music_category` — deny-list match is
  case-insensitive via `casefold`; `None`/blank, off-vocabulary, and
  not-on-the-list categories all return `True`; `validate_non_music_
  categories_config` rejects a non-list/non-string-list shape.
- `app/services/event_projection_selection.py::is_selectable` — a row that
  passes every existing criterion is excluded when its category is
  deny-listed; unaffected when category is `None`, off-vocabulary, or not
  deny-listed.
- `EventDisplayTitleService.run_for_events` /
  `EventVenueAdvisorService.run_for_events` — a deny-listed-category row
  never reaches `_pick_with_model`/`_advise_one` (assert the openai client's
  mock is never called for that row) while a music-category row in the same
  batch is processed normally.

Manual or integration checks:
- After implementation, re-run the same read-only SSM production query used
  for this plan's blast-radius evidence against the deployed deny-list to
  confirm actual excluded-row counts match expectations before/after.

## Acceptance Criteria
- A newly-extracted deny-listed-category event triggers zero OpenAI calls in
  either the display-title pass or the venue-advisor pass.
- A deny-listed-category event with a deterministically-resolved `venue_id`
  (the motivating "Férias Amigos Park" shape) never appears in the Redis
  serving projection.
- Music-category, off-vocabulary-category, and null-category events are
  unaffected in both the agentic-pass and serving-projection behavior
  (verified by the regression-guard scenarios above).
- `admin_config:event_non_music_categories` can be edited without a deploy
  and the change is reflected on the next extraction run (agentic passes) and
  next projection cycle (serving), respectively.
- The venue-link-audit reviewer's behavior is verifiably unchanged by this
  plan (no code path in `venue_link_audit_reviewer.py`/`venue_link_audit.py`
  is touched).
- Excluded non-music rows remain visible in the admin console (no row is
  deleted or hidden from `admin_events_router.list_events`).

## Open Questions
- None.
