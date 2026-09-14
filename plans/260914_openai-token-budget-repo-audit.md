# OpenAI Token-Budget Repo Audit

## Branch
fix/openai-token-budget-audit (already checked out in this worktree, based on
`origin/main` at `f8c5099`, which already includes the
`EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS` fix, PR #236).

## Goal
`EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS` was raised today from `200` to
`4096` because `gpt-5.6-luna` is a reasoning model whose invisible reasoning
tokens bill against `max_completion_tokens` before a single visible output
token is written — measured against real production traffic: 9/120 calls
(7.5%) came back empty at 400 tokens, 0/120 at 3000. That fix touched one
constant. This plan audits every other OpenAI call site in the repo for the
same failure mode — a token budget sized only for its visible JSON output,
with no headroom for a reasoning model's invisible reasoning tokens, or no
headroom for a realistic large-input edge case — and raises every constant
the audit finds genuinely under-provisioned.

## Non-goals
- `EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS` itself (today's fix, PR #236) —
  re-verified below and left untouched.
- Any prompt text, retry/error-handling logic, or admin-config default.
- Model selection (`DEFAULT_MODEL`/`*_model` settings values) — several are
  notable (see "Aside" below) but this plan is about token budgets, not which
  model each client calls.
- `feature/agentic-venue-resolution-fallback` and
  `fix/event-venue-advisor-token-budget` — separate, already-merged-or-in-flight
  branches, no files in common with this plan's diff.
- `compute_multi_event_max_completion_tokens`/`MULTI_EVENT_BASE_/PER_EVENT_
  COMPLETION_TOKENS` (`app/api/openai_event_extraction_client.py`) — audited
  below, left unchanged (already self-scaling and already reasoning-tax-aware
  across three prior plan iterations).
- `_output_budget`/`OUTPUT_TOKENS_PER_PHOTO`/`MIN_OUTPUT_TOKENS`
  (`app/api/openai_photo_classifier_client.py`) — audited below, left
  unchanged (the one call site in this repo whose budget is already both
  dynamic and derived from a stated real-production measurement of
  `completion_tokens`, which already includes reasoning tokens).
- `classify_menu_photos`'s `max_completion_tokens=1024`
  (`app/api/openai_menu_client.py`) — audited below, left unchanged (confirmed
  non-reasoning model, small realistic worst case).

## Evidence

### AWS / live measurement
AWS SSO is expired in this environment (`aws sts get-caller-identity` and
`aws sts get-caller-identity --profile vibesense` both fail with "Token has
expired and refresh failed"; `aws sso list-accounts` fails the same way).
Per this task's own safety notes, this was not fought — no interactive login
is possible in a non-interactive session. Every conclusion below is
reasoned-from-code (prompts, wiring, realistic input bounds), not a live
production sample, and is labeled as such. The one number in this plan that
IS a live measurement (3000 tokens -> 0/120 empty on `gpt-5.6-luna`) is
`EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS`'s own evidence from PR #236,
reused below as the best available reasoning-tax floor for this model when a
call site has no measurement of its own.

### Which model each call site actually runs on (verified by reading the
actual constructor default AND the actual container.py wiring — several
clients' own file-level defaults look like a cheap non-reasoning model, but
production overrides them via settings)

| Client method | Class/file default | Settings field (container.py) | Actual value in `.env` today | Reasoning model? |
|---|---|---|---|---|
| `pick_display_title` | `DEFAULT_MODEL="gpt-5.6-luna"` (openai_event_extraction_client.py:118) | `settings.event_extraction_model` | unset -> default `gpt-5.6-luna` | **YES** |
| `recommend_event_venue` (advisor, already fixed) | same | same | same | **YES** |
| `extract`/`extract_events` | same | same | same | **YES** |
| `OpenAIMenuClient.extract_menu_from_photos` | `model="gpt-5.4-nano"` (openai_menu_client.py:65, looks non-reasoning) | `settings.menu_extraction_model` (config.py:741), passed at container.py:517 | unset -> default **`gpt-5.6-luna`** | **YES — overridden away from the file's own non-reasoning-looking default** |
| `OpenAIMenuClient.classify_menu_photos` | `model="gpt-5.4-nano"` (method's own default param) | not overridden — call site (menu_extraction_service.py:228) passes no `model=` | `gpt-5.4-nano` (the method default really is used) | **NO** |
| `OpenAIVibeClient.classify_venue_vibes_stage_a` | `model="gpt-5.4-nano"` (method default, looks non-reasoning) | `settings.vibe_classifier_stage_a_model` (config.py:760), passed at container.py:558 -> vibe_classifier_service.py:150 | unset -> default **`gpt-5.6-luna`** | **YES — overridden** |
| `OpenAIVibeClient.classify_venue_vibes_stage_b` | `model="gpt-5.4-mini"` (method default) | `settings.vibe_classifier_stage_b_model` (config.py:761), passed at container.py:559 -> vibe_classifier_service.py:181 | unset -> default **`gpt-5.6-luna`** | **YES — overridden** |
| `OpenAIInstagramJudgeClient.judge_instagram_match` | `model` is a required call-time param, no default in this file | `InstagramJudge(model=settings.instagram_judge_model)` (container.py:112-114); `InstagramJudge.__init__`'s OWN default (`instagram_judge.py:87`) is `"gpt-5.4-mini"`, but it is always constructed with the setting | `instagram_judge_model` (config.py:414) unset -> default **`gpt-5.6-luna`** | **YES — overridden** |
| `OpenAIPhotoClassifierClient._one_batch` | `DEFAULT_MODEL="gpt-5.6-luna"` (openai_photo_classifier_client.py:56) | `settings.photo_classification_model` (config.py:711), passed at container.py:1212-1218 | unset -> default `gpt-5.6-luna` | **YES — no drift here, file and settings already agree** |

Confirmed by direct `grep -i` against the tracked `.env` for
`MENU_EXTRACTION_MODEL`, `VIBE_CLASSIFIER_STAGE`, `INSTAGRAM_JUDGE_MODEL`,
`PHOTO_CLASSIFICATION_MODEL`, `EVENT_EXTRACTION_MODEL`,
`EVENT_EXTRACTION_MAX_TOKENS` — none are set, so every client above runs on
the Pydantic `Settings` default in `app/config.py`, not a deploy-time
override. This means the two clients whose own source file reads as
"obviously a cheap non-reasoning model" (`openai_menu_client.py`,
`openai_vibe_client.py`) are misleading on their own — the actual model is
only visible by tracing `container.py`'s wiring through to `config.py`.

**Aside (not acted on, per non-goals):** it is notable that `menu_extraction_
model`, `vibe_classifier_stage_a/b_model`, and `instagram_judge_model` all
independently default to `gpt-5.6-luna` even though several of these
clients' own hardcoded per-method fallback defaults (`gpt-5.4-nano`,
`gpt-5.4-mini`) were seemingly never updated to match — suggesting the
model migration happened at the settings layer without the client files'
own documentation/defaults being revisited. This plan does not touch model
selection; flagged only because it is exactly why "check the file's own
`DEFAULT_MODEL`" is not sufficient — the settings default must be traced
through `container.py` too.

### Per-site output-shape analysis (worst realistic case)

- **`pick_display_title`** (`TITLE_PICK_MAX_COMPLETION_TOKENS = 160`,
  openai_event_extraction_client.py:62): output is `{"display_title": "..."}`,
  capped at 120 characters by the prompt's own rule. Visible output is tiny —
  but 160 is smaller than the 200 that PR #236 already proved unsafe on this
  exact model for a comparably small/simple decision task. High suspicion
  confirmed: this call is wired to `gpt-5.6-luna` (table above).

- **`extract`/single-event path** (`DEFAULT_MAX_COMPLETION_TOKENS = 6400`,
  constructor param `max_completion_tokens` defaulting to it): grepping every
  call of `.extract(` and `.extract_events(` across `app/` shows the two real
  runtime paths (`app/services/event_extraction_service.py:952`,
  `app/services/promoter_crawl_service.py:477`) both call `.extract_events(...)`,
  never `.extract(...)`. `.extract(` is exercised only by
  `tests/test_promoter_crawl_service.py` (a mocked fake) and
  `tests/test_kind_eval_live.py`/`scripts/eval_kind_on_captions.py` (a manual
  live-eval script, which constructs the client with no override and so
  actually uses `DEFAULT_MAX_COMPLETION_TOKENS`, not the settings value).
  **Finding: `.extract()` is unreachable from production request traffic
  today.** The constructor parameter it depends on (`self.max_completion_tokens`)
  is fed in production (container.py:819) from `settings.event_extraction_max_tokens`,
  which defaults to `4096` in `app/config.py:775` — LOWER than the module's
  own `DEFAULT_MAX_COMPLETION_TOKENS = 6400` (already raised across three
  earlier plans per its own comment). Because `.extract()` is not called
  today, this drift has zero live-traffic impact right now — but it is a
  silent landmine: if `.extract()` is ever wired up again (the method's own
  docstring already describes it as backing "the confirmed-event
  re-extraction path"), it would silently run at the lower, stale 4096
  instead of the already-reasoned 6400, on the exact reasoning model this
  whole audit is about.

- **`extract_events`/multi-event path (the real production per-post call)**:
  budget is `compute_multi_event_max_completion_tokens(max_events)`, and both
  call sites pass `max_events=self.max_events_per_post`, itself
  `settings.event_extraction_max_events_per_post` (default `20`,
  config.py:782) — the CONFIGURED CEILING, not the actual per-post event
  count (which is unknown before the call — the function's own docstring
  says so). At the default ceiling: `2304 + 550 * 20 = 13304` tokens. This
  is the REAL budget in effect for the main per-post extraction call in
  production today, not `DEFAULT_MAX_COMPLETION_TOKENS`. It is generous, and
  it self-corrects for a single large-lineup event: when a post announces
  fewer events than the ceiling, the whole per-post budget is still available
  to whichever events ARE present, so a one-event post with an unusually
  large lineup still gets the full 13304-token ceiling, not a
  per-event-capped slice of it. Left unchanged.

- **`extract_menu_from_photos`** (`app/api/openai_menu_client.py`, flat
  `max_completion_tokens=4096`): output is `menu_sections[].items[]`, each
  item carrying `name`, `description`, a `prices` array (size/label
  variants), `dietary_tags`, `modifiers` — a materially richer per-entry
  schema than a photo-classifier verdict. `settings.menu_photos_per_venue`
  (config.py:455) is `20` — up to 20 photos go into ONE call (no internal
  batching, unlike the photo classifier). A worst-case venue with a large,
  multi-page menu spread across most of those 20 photos (the task's own
  example: "a restaurant with 80 menu items needs a very different budget
  than one with 12") can plausibly need several thousand tokens of visible
  JSON alone, on TOP of `gpt-5.6-luna`'s invisible reasoning tokens for an
  OCR-plus-hierarchy-inference task that is at least as reasoning-heavy as
  the advisor's simple text-match task. A flat, non-scaling 4096 is
  under-provisioned for this call's own worst case, and the task explicitly
  calls out that a budget like this "actually needs to scale with something
  (item count...) the way `compute_multi_event_max_completion_tokens`/
  `_output_budget` already do" — this one currently does not, unlike its two
  siblings in this same codebase. **Verdict: raise, and make it scale with
  `len(photo_urls)`**, mirroring the established pattern.

- **`classify_menu_photos`** (flat `max_completion_tokens=1024`): confirmed
  non-reasoning (`gpt-5.4-nano`, no override — table above). Output is one
  small object per photo (`{"index", "is_menu", "confidence"}`), realistic
  worst case ~20-30 photos (bounded by the same `menu_photos_per_venue=20`
  order of magnitude) at roughly 20-25 tokens/entry -> ~500-750 tokens,
  comfortably inside 1024 with margin, and there is no reasoning-tax risk on
  this model. **Verdict: leave unchanged.**

- **`classify_venue_vibes_stage_a`** (flat `max_completion_tokens=3072`):
  wired to `gpt-5.6-luna` (table above). Output schema is the richest single
  object in this whole audit: a `photos[]` array (one entry per photo, up to
  `vibe_classifier_target_photos=10`, config.py ~753), 8 fixed-taxonomy
  category blocks (each with `labels[]`, `confidence`, `evidence.
  photo_indices[]`, `evidence.review_quotes[]`), `top_vibes[6]`,
  `overall_confidence`, `notes`, and 4 blurb strings (short+long,
  Portuguese+English). Rough visible-content estimate alone (10 photo
  entries + 8 category blocks + blurbs + JSON scaffolding) is already in the
  ~1800-2200 token range before any reasoning tokens are counted — leaving at
  most ~900-1200 tokens of the current 3072 ceiling for `gpt-5.6-luna`'s
  invisible reasoning over a 10-image, 8-taxonomy classification task, well
  under the 3000-token reasoning-tax floor PR #236 measured for a much
  simpler single-candidate text match. **Verdict: raise.**

- **`classify_venue_vibes_stage_b`** (flat `max_completion_tokens=3072`,
  same call structure, wired to `gpt-5.6-luna`): narrower than Stage A —
  `refined_categories` covers only the categories `_should_escalate` flagged
  as uncertain (a subset of the 8), over `vibe_classifier_stage_b_photos=5`
  higher-detail photos (config.py ~757), but it still regenerates
  `top_vibes`/blurbs every call. Visible content is smaller than Stage A's
  (fewer category blocks), but the reasoning load (5 high-detail photos, a
  focused-but-real classification task) is not trivially smaller. **Verdict:
  raise, to a value below Stage A's — its own output is structurally
  smaller.**

- **`judge_instagram_match`** (`app/api/openai_instagram_judge_client.py`,
  inline `max_completion_tokens=200`, no named constant in this file today):
  wired to `gpt-5.6-luna` in production (table above) despite the class's own
  constructor default reading as `gpt-5.4-mini`. This is the smallest budget
  of any reasoning-model call site in the repo — AT the exact value PR #236
  already measured at 7.5% empty on this same model for a comparably small
  decision task. Output is `{"is_match", "confidence", "reason"}` (reason
  capped at 200 chars by the caller, `instagram_judge.py:119`) — genuinely
  small, but the whole point of PR #236 is that visible-output size does not
  predict invisible reasoning-token cost on this model. **Verdict: raise,
  urgently — highest-priority finding in this audit** (smallest budget,
  confirmed reasoning model, matches the exact already-proven-unsafe number).

- **`_output_budget` (photo classifier)**: `MIN_OUTPUT_TOKENS=1024`,
  `OUTPUT_TOKENS_PER_PHOTO=250`, wired to `gpt-5.6-luna` with no drift
  (table above). This is the one call site in the repo whose own comment
  states the per-photo figure came from a REAL measurement ("~150 output
  tokens measured on real runs"), and `_record_usage`'s own docstring
  confirms `completion_tokens` (what that measurement reads) already
  includes reasoning tokens as a subset, not an addition — so the 150-token
  figure already reflects whatever reasoning was actually spent on those
  real runs, not a blind visible-output guess. This file's docstrings also
  document two prior production incidents this exact budget was tuned
  against (a 41%-classified run from zero retries, a 9x-low per-image
  estimate). **Verdict: leave unchanged** — this is the one call site with
  its own production-measurement provenance already, unlike the sites above.
  Flagged as a candidate for a future live check specifically on small
  batches (1-3 photos, where `MIN_OUTPUT_TOKENS`'s floor dominates and the
  call most resembles the advisor/title-pick/judge shape), but not changed
  here without evidence, per this task's own instruction not to touch a site
  without a concrete reason.

## Current Behavior
Five call sites bound a reasoning-model (`gpt-5.6-luna`) OpenAI call with a
`max_completion_tokens` ceiling sized only for the call's own visible JSON
output, with no accounted headroom for the model's invisible reasoning
tokens (which bill against the same ceiling): `pick_display_title` (160),
`judge_instagram_match` (200), `extract_menu_from_photos` (flat 4096, also
not scaled to its own up-to-20-photo worst case), `classify_venue_vibes_
stage_a` (3072), `classify_venue_vibes_stage_b` (3072). A sixth,
`settings.event_extraction_max_tokens` (4096), has drifted below its own
sibling module constant (`DEFAULT_MAX_COMPLETION_TOKENS = 6400`) and is a
latent landmine on the currently-unreachable `.extract()` path. On the exact
model already measured to silently return empty content (never an error,
never a distinguishable rejection) when its reasoning alone exhausts the
budget, every one of these is a candidate for the same silent-empty-response
failure PR #236 just fixed for the advisor.

## Desired Behavior
Every reasoning-model call site in the repo carries a `max_completion_tokens`
ceiling with real, documented headroom for both `gpt-5.6-luna`'s invisible
reasoning tokens and a realistic worst-case input/output shape for that call
— never a budget sized only for the typical case. Call sites confirmed to run
on a non-reasoning model, or already carrying their own production-measured
headroom, are left unchanged, with the reasoning documented in-line so the
next person auditing this codebase does not have to re-derive it. Relative
ordering between sibling constants in the same file continues to reflect
each call's own structural output size (a call that structurally needs less
visible output still costs less, even after every budget in this plan is
raised) — `max_completion_tokens` is a ceiling, not a floor, so headroom
beyond what a call actually needs costs nothing unless the model uses it.

## Implementation Approach

### 1. `app/api/openai_event_extraction_client.py`
Raise `TITLE_PICK_MAX_COMPLETION_TOKENS` from `160` to `4096` — matching
`EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS`'s own value and evidence, since
no call-site-specific live sample exists for title-pick and it runs on the
identical model for a comparably small/simple text-only decision task (pick
or lightly combine among a handful of given titles). Reusing the ONE number
this codebase has actually validated against production for this exact
model/task class is more defensible than guessing a smaller number with no
supporting evidence — which is exactly the unproven assumption ("a title is
a small, easy answer") the original 200/160 values already got wrong once.
Update the constant's comment to state this reasoning and cite PR #236's
measurement as the basis.

### 2. `app/config.py`
Raise `event_extraction_max_tokens` from `4096` to `6400`, matching
`DEFAULT_MAX_COMPLETION_TOKENS` in `openai_event_extraction_client.py`, so
the two constants cannot silently drift apart again. Comment notes this
setting only feeds the currently-unreachable `.extract()` path today (real
per-post extraction traffic uses `compute_multi_event_max_completion_tokens`
instead) and exists purely to remove a landmine for whenever that path is
next exercised.

### 3. `app/api/openai_instagram_judge_client.py`
Introduce a module-level `MAX_COMPLETION_TOKENS = 4096` constant (this file
currently has no named constant for it at all — an inline `200`), reference
it from `judge_instagram_match`, and document why: wired to `gpt-5.6-luna` in
production despite the class's own non-reasoning-looking default, at exactly
the value PR #236 already measured unsafe for this model. Matches the
advisor's evidence-reuse logic in §1 above.

### 4. `app/api/openai_menu_client.py`
- Add `MENU_EXTRACTION_BASE_COMPLETION_TOKENS = 4096` (reasoning-tax floor —
  reused from PR #236's measured value, applied conservatively since OCR-plus-
  hierarchy-inference over multiple images is reasoned to need at least as
  much reasoning budget as a single text match, absent this call's own live
  measurement) and `MENU_EXTRACTION_PER_PHOTO_COMPLETION_TOKENS = 768`
  (visible-JSON headroom per photo: a densely packed menu page can show
  10-15 items at up to ~70 tokens of JSON each once section/price/tag
  structure is included).
- Add `compute_menu_extraction_max_completion_tokens(photo_count: int) -> int`
  mirroring `compute_multi_event_max_completion_tokens`'s shape exactly
  (`BASE + PER_UNIT * max(1, count)`), scaling by `len(photo_urls)` — the
  input signal actually known before the call, the same justification this
  repo's own sibling formula already uses for scaling by a configured/known
  ceiling rather than an unknown output count.
- Change `extract_menu_from_photos` to pass
  `max_completion_tokens=compute_menu_extraction_max_completion_tokens(len(photo_urls))`
  instead of the flat `4096`.
- Leave `classify_menu_photos`'s flat `max_completion_tokens=1024` unchanged;
  add a short comment recording why (confirmed non-reasoning model, small
  realistic worst case) so a future reader does not have to re-derive it.

### 5. `app/api/openai_vibe_client.py`
- Add `STAGE_A_MAX_COMPLETION_TOKENS = 6144` and
  `STAGE_B_MAX_COMPLETION_TOKENS = 5120` as module constants (this file
  currently hardcodes `3072` inline at both call sites).
- Wire `classify_venue_vibes_stage_a`/`_stage_b` to reference them.
- Comment cites: wired to `gpt-5.6-luna` in production (table above), the
  per-site output-shape estimate above (Stage A's 8-category-plus-photos
  schema is visibly richer than Stage B's uncertain-categories-only refinement,
  hence Stage A > Stage B, preserving the file's relative-output-size
  ordering), and that neither figure is a live measurement.

## Data, Config, And API Impact
None. No request/response shape, persistence, admin-config default, or
feature-flag change in any file — every edit is a token-budget constant or
the formula that computes one from an already-known input (`photo_urls`
length). `event_extraction_max_tokens` is a `Settings` field but its value
only changes a `max_completion_tokens` ceiling passed to OpenAI, same
category as every other edit here.

## Error Handling And Observability
No new runtime path in any file. Existing `OPENAI_API_CALLS_TOTAL`/
`OPENAI_TOKENS_TOTAL` metrics at each call site are unchanged. This change is
expected to reduce silent empty/malformed-response outcomes at each raised
call site going forward (mirroring PR #236's expected effect on
`EVENT_VENUE_ADVISOR_OUTCOME_TOTAL{outcome="rejected"}`), observable on each
site's existing dashboard without any new metric.

## Test Plan
# bdd-exempt: pure internal config-constant changes (plus one new formula
function with the exact same shape as the already-tested
`compute_multi_event_max_completion_tokens`) — no new or altered API/response
shape, persistence, or user-facing behavior. Same precedent as
`plans/260914_event-venue-advisor-token-budget.md`'s own BDD-exemption: the
one behavior a Gherkin scenario could plausibly claim to cover ("this call no
longer returns empty on reasoning-heavy input") is not reproducible against a
deterministic fake OpenAI client, and CLAUDE.md prohibits live OpenAI calls in
BDD.

Pytest unit tests, one new file per client that currently has none, plus one
extension of an existing file (matching this task's own instruction: extend
coverage where a module already has it, add focused new coverage matching
established style where it does not):

- **`tests/test_event_title_pick_client.py`** (new — `openai_event_extraction_
  client.py` has no dedicated title-pick test file; mirrors
  `tests/test_event_venue_advisor_client.py` exactly, same module, same bug
  class, written today):
  - Regression guard: `TITLE_PICK_MAX_COMPLETION_TOKENS == 4096`.
  - Mocked-client wiring test: `client.client.chat.completions.create =
    AsyncMock(...)`, call `pick_display_title(...)`, assert
    `create.call_args.kwargs["max_completion_tokens"] ==
    TITLE_PICK_MAX_COMPLETION_TOKENS`.
  - `max_tokens` never passed at this call site.
  - Empty `content` still returns `""`, not an exception (same shape as the
    advisor's own test — proves the client's own contract is unaffected by
    the budget change).

- **`tests/test_multi_event_extraction.py`** (extend existing
  `TestBudgetScaling`): add
  `test_event_extraction_max_tokens_setting_matches_the_client_default` —
  imports `Settings` from `app.config` and `DEFAULT_MAX_COMPLETION_TOKENS`
  from this module, asserts `Settings().event_extraction_max_tokens ==
  DEFAULT_MAX_COMPLETION_TOKENS` — the regression guard for the drift this
  plan found and fixed, in the same class that already asserts relationships
  between this module's own budget constants.

- **`tests/test_openai_instagram_judge_client.py`** (new — no dedicated file
  exists today):
  - Regression guard: `MAX_COMPLETION_TOKENS == 4096`.
  - Mocked-client wiring test on `judge_instagram_match`, same shape as
    above.
  - `max_tokens` never passed.
  - `Container._build_instagram_judge()`'s existing coverage
    (`tests/test_container_judge_wiring.py`) already proves
    `settings.instagram_judge_model` reaches the constructed judge; add one
    assertion there confirming the DEFAULT (unconfigured) `Settings()` value
    is `"gpt-5.6-luna"` — `test_every_judge_setting_is_readable` currently
    only checks the field is a non-empty string, not which model it defaults
    to, so today's actual production wiring (the reasoning model) is not
    itself pinned anywhere.

- **`tests/test_openai_menu_client.py`** (new — no dedicated file exists
  today):
  - `TestBudgetScaling`-style class (mirrors
    `test_multi_event_extraction.py`'s own, same shape): budget increases
    with photo count; budget matches the documented `BASE + PER_PHOTO * n`
    formula; a misconfigured/empty photo list still gets at least one
    photo's worth of headroom (`max(1, photo_count)`); arithmetic pinned at
    1, 10, and 20 photos (the configured `menu_photos_per_venue` ceiling).
  - Mocked-client wiring test: `extract_menu_from_photos` passes
    `compute_menu_extraction_max_completion_tokens(len(photo_urls))` through
    to `max_completion_tokens=` on the real call.
  - `max_tokens` never passed on either `extract_menu_from_photos` or
    `classify_menu_photos`.
  - Regression guard: `classify_menu_photos`'s own call still passes exactly
    `1024` (proves this plan's "leave unchanged" call site was not touched
    by accident while editing the same file).

- **`tests/test_openai_vibe_client.py`** (new — no dedicated file exists
  today):
  - Regression guards: `STAGE_A_MAX_COMPLETION_TOKENS == 6144`,
    `STAGE_B_MAX_COMPLETION_TOKENS == 5120`, and
    `STAGE_A_MAX_COMPLETION_TOKENS > STAGE_B_MAX_COMPLETION_TOKENS` (the
    relative-ordering guarantee this plan documents — Stage A's schema is
    structurally richer).
  - Mocked-client wiring tests for both `classify_venue_vibes_stage_a` and
    `classify_venue_vibes_stage_b`, same shape as above.
  - `max_tokens` never passed on either call.

`tests/test_openai_call_shape.py`'s existing repo-wide guards
(`test_no_client_passes_max_tokens`, `test_every_client_bounds_its_output`)
already cover every file this plan touches generically and need no change —
the new tests above are about the SIZE of each bound, which that file
deliberately does not check (per its own docstring, it is a "cheapest
possible guard", not a completeness check).

Manual or integration checks: none — AWS SSO is expired in this environment
(see Evidence), so no live-traffic replay was possible for any site in this
plan beyond reusing PR #236's own measurement as the shared reasoning-tax
floor.

## Acceptance Criteria
- `TITLE_PICK_MAX_COMPLETION_TOKENS == 4096` (was 160), pinned by a pytest
  test.
- `Settings().event_extraction_max_tokens == DEFAULT_MAX_COMPLETION_TOKENS`
  (both `6400`), pinned by a pytest test.
- `openai_instagram_judge_client.MAX_COMPLETION_TOKENS == 4096` (was inline
  `200`), pinned by a pytest test, and `judge_instagram_match` passes it
  through to the real call.
- `openai_menu_client.compute_menu_extraction_max_completion_tokens` exists,
  scales with photo count per the documented formula, and
  `extract_menu_from_photos` passes its result through to the real call.
  `classify_menu_photos` still passes exactly `1024`.
- `openai_vibe_client.STAGE_A_MAX_COMPLETION_TOKENS == 6144` and
  `STAGE_B_MAX_COMPLETION_TOKENS == 5120`, both pinned, both wired through to
  their respective real calls, `STAGE_A > STAGE_B` also asserted.
- `EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS`,
  `compute_multi_event_max_completion_tokens`/`MULTI_EVENT_*`, and
  `_output_budget`/`OUTPUT_TOKENS_PER_PHOTO`/`MIN_OUTPUT_TOKENS` are all
  byte-for-byte unchanged.
- No prompt text, retry/error-handling logic, or admin-config default
  changed in any file.
- Targeted pytest for every new/extended file, plus `make test-unit`, passes
  green.

## Open Questions
None — every call site in the task's own list has been traced to its actual
production model and given an explicit leave-alone-or-raise verdict above.
