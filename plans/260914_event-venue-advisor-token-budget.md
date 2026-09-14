# Event Venue Advisor Token Budget

## Branch
fix/event-venue-advisor-token-budget

## Process note
This plan is committed on the feature branch rather than on `main` (the usual
`/plan-feature` placement). `main` is already checked out in another worktree
for this same repo (the wrapper's `cs-server` submodule checkout, which this
task's own instructions say not to touch), so it cannot be checked out here
without disrupting a concurrent session. Pushing the plan straight to `main`
via plumbing (without checking it out) was considered and rejected: it would
land an unreviewed change on `main`, which conflicts with this task's explicit
commit/PR gate ("stop before merging, I will review the diff"). Given the fix
is a single constant plus its tests, bundling plan + implementation + tests
into one reviewable PR is the safer adaptation of the process, not a skipped
step.

## Goal
Raise `EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS`
(`app/api/openai_event_extraction_client.py:104`) from its current `200` to a
value that eliminates the empty-response truncation measured in production,
so `recommend_event_venue` stops silently starving on invisible reasoning
tokens before it can emit its `{"venue_id", "evidence_quote"}` answer.

## Non-goals
- `EventVenueAdvisorService`, its `RESOLUTION_QUEUED` gating, its prompt, its
  validator's rejection reasons, or `set_event_venue_link_candidate_recommendation`'s
  write path.
- `venue_link_audit_enabled`, `event_venue_advisor_enabled`, or any other
  admin-config default.
- `DEFAULT_MAX_COMPLETION_TOKENS` (main single-event extraction call, 6400) or
  `TITLE_PICK_MAX_COMPLETION_TOKENS` (160) — verified these are separate
  constants at separate call sites (lines 744/898 vs 950 respectively), never
  shared with the advisor's own budget, so changing this constant cannot
  silently affect either.
- Anything on `feature/agentic-venue-resolution-fallback` (sibling branch,
  different file, different constant, confirmed no overlap).

## Evidence
- `app/api/openai_event_extraction_client.py:104`:
  `EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS = 200`, confirmed on this branch
  today by direct read, not by paraphrase.
- `app/api/openai_event_extraction_client.py:106-121`: `DEFAULT_MODEL =
  "gpt-5.6-luna"`, documented in-file as a reasoning model whose invisible
  reasoning tokens are billed against `max_completion_tokens` (the comment
  already states this for the sibling `DEFAULT_MAX_COMPLETION_TOKENS`
  budget).
- `app/api/openai_event_extraction_client.py:945-951`: `recommend_event_venue`
  passes `max_completion_tokens=EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS`
  straight into `self.client.chat.completions.create(...)` — the one real
  call site for this constant (confirmed by grep across `app/`, plus the
  `__all__` export).
- `app/services/event_venue_advisor.py` (`EventVenueAdvisorService.
  run_for_events`/`_advise_one`) is the only caller of `recommend_event_venue`,
  gated by admin-config `event_venue_advisor_enabled` — reported live (`True`)
  in production as of 2026-09-14 by a sibling investigation this session.
- `app/services/event_venue_advisor_validator.py:57,135`: an unparseable
  (including empty) response maps to `REJECTION_MALFORMED`, counted under
  `EVENT_VENUE_ADVISOR_OUTCOME_TOTAL{outcome="rejected"}` — indistinguishable
  on a dashboard from a legitimate validator refusal.
- Sibling investigation, measured against real production data today
  (2026-09-14): 120 real `recommend_event_venue` calls at
  `max_completion_tokens=400` produced 9 empty responses (7.5%); the
  identical 120 calls replayed at `max_completion_tokens=3000` produced 0
  empty responses.
- Precedent for testing a config-constant change with plain pytest, not
  Gherkin: `tests/test_event_venue_resolution.py:797-798`
  (`test_default_top_k_is_twenty`, pinning `DEFAULT_NAME_MATCH_TOP_K == 20`)
  and this exact client file's own `tests/test_multi_event_extraction.py`
  (`TestBudgetScaling` pins `DEFAULT_MAX_COMPLETION_TOKENS`;
  `TestExtractEventsTruncationDetection.test_the_call_budget_scales_with_max_events`
  mocks `client.client.chat.completions.create = AsyncMock(...)` and asserts
  on `create.call_args.kwargs["max_completion_tokens"]`).

## Current Behavior
`recommend_event_venue` bounds its single OpenAI call at 200 completion
tokens. Because `gpt-5.6-luna` is a reasoning model, invisible reasoning
tokens are billed against that same 200-token ceiling, so a non-trivial
fraction of calls (measured 7.5% at a looser 400-token ceiling) exhaust the
budget on reasoning alone and return an empty `content` string.
`parse_event_venue_advisor_response` treats an empty string as unparseable,
so the validator rejects it as `REJECTION_MALFORMED` — silently conflated
with genuine "no candidate supported by the text" refusals in the outcome
metric, and the likely explanation for apparent non-determinism observed
earlier this session when calling the advisor's functions directly.

## Desired Behavior
`recommend_event_venue` bounds its call at a budget with headroom to
comfortably finish its reasoning AND emit the small `{"venue_id",
"evidence_quote"}` JSON answer, so truncation-driven empty responses stop
happening in production. The prompt, validator, and everything downstream of
a real (non-empty) response are unchanged.

## Implementation Approach
Change the single constant `EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS` from
`200` to `4096` in `app/api/openai_event_extraction_client.py`. No other
logic changes: `recommend_event_venue` already passes this constant straight
through to `max_completion_tokens=` on its one call site (line 950), so a
value bump alone changes behavior — no wiring needs to change. Update the
comment above the constant to document the measured evidence and the chosen
headroom, matching this file's existing documentation posture for its other
budgets (e.g. the `DEFAULT_MAX_COMPLETION_TOKENS` comment at lines 107-121).

Chosen value — 4096, not exactly 3000: 3000 is the evidence-backed floor
(0/120 empty at that ceiling today), but it is one finite sample against one
day's traffic mix; `gpt-5.6-luna`'s invisible reasoning-token usage is
input-dependent (a longer or more ambiguous location text plausibly reasons
longer), and `max_completion_tokens` is a ceiling, not a floor — raising it
costs nothing extra unless the model actually needs the extra tokens, so a
generous cap has no cost downside, only a reliability upside. 4096 gives
~37% headroom above the measured floor, is a conventional round token
budget, and stays well below `DEFAULT_MAX_COMPLETION_TOKENS` (6400, the main
multi-field extraction call) — preserving the file's existing relative
ordering (title-pick 160 < advisor < main extraction 6400), which reflects
each call's actual output complexity: the advisor emits one `venue_id` plus
one short quoted span, never a multi-field/multi-event object.

Kept as its own constant, not merged into `DEFAULT_MAX_COMPLETION_TOKENS` or
`TITLE_PICK_MAX_COMPLETION_TOKENS`: this file already keeps one constant per
call *shape* (title-pick, single-event extraction, multi-event extraction,
advisor), each tuned to that call's own output size and reasoning load.
Merging them would either starve the advisor again (if pinned to 160) or
hide any future, independent tuning of the advisor's own reliability behind
an unrelated call's constant.

## Data, Config, And API Impact
None. No request/response shape, persistence, admin-config default, or
feature-flag changes — pure numeric constant.

## Error Handling And Observability
No new runtime path. Existing `EVENT_VENUE_ADVISOR_OUTCOME_TOTAL{outcome=
"rejected"}` and the `OPENAI_API_CALLS_TOTAL`/`OPENAI_TOKENS_TOTAL` metrics
at this call site are unchanged; this change should manifest as a drop in
`REJECTION_MALFORMED`-classified rejects going forward, observable on the
existing dashboard without any new metric.

## Test Plan
# bdd-exempt: pure internal config-constant change — no new or altered
API/response shape, persistence, or user-facing behavior; this file's own
precedent (`DEFAULT_NAME_MATCH_TOP_K`; `DEFAULT_MAX_COMPLETION_TOKENS`'s
`TestBudgetScaling`) tests sibling token-budget constants with plain pytest,
never Gherkin. The one behavior a BDD scenario could plausibly claim to
cover — "the advisor no longer returns empty on reasoning-heavy input" — is
not reproducible against a deterministic fake OpenAI client (CLAUDE.md
prohibits live OpenAI calls in BDD): a fake returns whatever content it is
told regardless of the token budget passed, so such a scenario would be
vacuous. That claim was instead verified directly against production today
(120 real calls: 0/120 empty at 3000 vs. 9/120 empty at 400) by a sibling
investigation and is not something this suite re-verifies.

Pytest unit tests (new file `tests/test_event_venue_advisor_client.py`,
mirroring the `test_event_venue_advisor_validator.py`/`_migration.py` family
already scoped to one layer of this feature each):
- Regression guard pinning the real, current value:
  `EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS == 4096`, same style as
  `test_default_top_k_is_twenty` — so this cannot silently drift back toward
  200.
- Mocked-client wiring test: `client.client.chat.completions.create =
  AsyncMock(...)`, call `recommend_event_venue(...)`, assert
  `create.call_args.kwargs["max_completion_tokens"] ==
  EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS` — same style as
  `TestExtractEventsTruncationDetection.test_the_call_budget_scales_with_max_events`,
  confirming the client still passes the (now-larger) budget straight
  through to the real API call.
- A `max_tokens` never-passed guard for this call site, matching this file's
  existing repo-wide `test_openai_call_shape.py` posture (belt-and-suspenders
  at the call-site level, not a duplicate of that generic scan).

Manual or integration checks: None — the evidence (120-call production
measurement) was already gathered by a sibling investigation; re-running it
is out of scope for this fix.

## Acceptance Criteria
- `EVENT_VENUE_ADVISOR_MAX_COMPLETION_TOKENS` equals `4096` in
  `app/api/openai_event_extraction_client.py`, and a pytest test pins that
  value.
- A pytest test proves `recommend_event_venue` passes this constant through
  to `max_completion_tokens=` on the real `chat.completions.create` call.
- No other file, prompt, validator, or admin-config default changed.
- Targeted pytest (and `make test-unit`) passes green.

## Open Questions
None.
