# Model Upgrade — gpt-5.6-luna across every OpenAI path

## Branch
feature/model-upgrade-gpt-5-6-luna

## Goal
Move all five OpenAI model settings to `gpt-5.6-luna`, and make the call sites
survive a model that rejects `temperature` — without changing how any call
behaves when it is pointed at a 5.4-family model.

## Non-goals
- **Prompt or schema changes.** Same prompts, same response schemas, same
  parsing. Only the model and the parameters the model refuses.
- **Batch-size retuning.** `batch_size` stays 10. Whether luna tolerates more is
  a separate measured question, and the last attempt to raise it lost verdicts.
- **Switching to the Responses API.** The clients use `chat.completions`; that
  stays.
- **Events.** Unrelated; this lands before the event plans so they inherit the
  helper rather than reinvent it.

## Evidence

Probed live against the API on 2026-08-05 with the repo's exact call shape.

**The model exists and does the job.** `gpt-5.6-luna` is present in
`GET /v1/models` (alongside `gpt-5.6-sol` and `gpt-5.6-terra`). With a vision
message, `detail: "low"`, and `response_format={"type":"json_object"}` it
returned well-formed JSON and correctly described a synthetic test image.

**It rejects `temperature`, and that is the blocker:**

```
400 invalid_request_error
Unsupported value: 'temperature' does not support 0.1 with this model.
Only the default (1) value is supported.
```

Six call sites pass one today, and every one of them would 400 on a bare config
swap:

| File | Line | Value |
|---|---|---|
| `app/api/openai_menu_client.py` | 96, 221 | 0.1 |
| `app/api/openai_vibe_client.py` | 298, 385 | 0.2, 0.1 |
| `app/api/openai_instagram_judge_client.py` | 66 | 0 |
| `app/api/openai_photo_classifier_client.py` | 253 | 0.1 |

**It is a reasoning model, and reasoning tokens are billed against
`max_completion_tokens`.** At the default batch of 10, with the repo's own
`_output_budget(10) = 2500`:

| Model | finish_reason | output tokens | of which reasoning | verdicts |
|---|---|---|---|---|
| `gpt-5.4-nano` | stop | 611 | 0 | 10/10 |
| `gpt-5.6-luna` | stop | 791 | 184 | 10/10 |

Headroom holds at the default batch, but the margin narrowed and the budget now
covers a class of tokens it was never sized for. `docs/venue-retrieval-storage.md`
§4 records what happens when that budget is wrong: the response stops
mid-string, the JSON fails to parse, and **the whole batch** falls back to no
verdict — a run that classifies nothing, reports success, and pays for every
image it sent.

**Determinism is currently deliberate in one place.** The Instagram judge runs
at `temperature=0` because an adjudicator that answers differently on a re-run
is not an adjudicator. On luna that pinning is not available at all.

## Current Behavior
Every OpenAI path runs a 5.4-family model and pins a low temperature. The
`temperature` kwarg is passed unconditionally.

## Desired Behavior
1. Default all five model settings to `gpt-5.6-luna`.
2. Omit `temperature` when the target model does not accept a custom one, and
   pass it unchanged otherwise, so a 5.4-family override keeps today's exact
   behavior.
3. Make the decision in one shared place, not in four clients.
4. Keep every response schema, parse path and fallback identical.
5. Measure the cost and latency change before the change is merged.
6. Leave the batch size at 10 and confirm 10/10 verdicts still return.

## Implementation Approach

### A. One shared compatibility helper
A small helper — `app/api/openai_compat.py` — exposing something like
`sampling_kwargs(model, temperature)` that returns `{}` for a model that pins
sampling and `{"temperature": t}` otherwise. Every call site spreads it into the
existing `create(...)` call; nothing else changes.

**A helper rather than deleting the kwarg**, because deleting it silently
changes sampling for every 5.4-family path — including the judge's
`temperature=0`, where determinism is the whole point — and those paths are
still reachable by config override. The helper makes the difference a property
of the *model*, which is what it actually is, rather than a property of the call
site.

Detection is an explicit set of model prefixes that pin sampling, defaulting to
"pass temperature through". An allow-list would silently drop temperature for
every future model; this way an unknown model behaves like today, and the one
class we have measured is handled.

### B. Settings
Default `instagram_judge_model`, `photo_classification_model`,
`menu_extraction_model`, `vibe_classifier_stage_a_model` and
`vibe_classifier_stage_b_model` to `gpt-5.6-luna`. `DEFAULT_MODEL` in
`openai_photo_classifier_client.py` moves with them so the constructor default
does not disagree with settings.

Every one stays an override, so a single path can be pinned back to 5.4 without
a redeploy if it regresses.

### C. Cost and quality measurement — a merge gate, not a follow-up
The repo carries a documented spend gate and a measured
**~$9 per ~17k photos** figure for classification on 5.4-nano. That figure is
invalidated by this change and must be re-derived, not assumed: §4 of the
storage doc records an earlier estimate that was wrong by **9x** because it
reused another model's image-token cost.

Before merge, run the existing classification path over a fixed sample on both
models and record input tokens, output tokens, reasoning tokens, wall time and
derived cost per 1,000 photos. If luna is materially more expensive, say so in
the PR and let the operator decide rather than merging a silent bill increase.

`photo_classification_cost_usd` and `openai_tokens_total` already meter this;
reasoning tokens need adding to the output count or they are billed and
invisible.

## Data, Config, And API Impact
- **Migration / persistence / API:** none.
- **Settings:** five model defaults change. No new setting.
- **Behavior:** model outputs change everywhere. Schemas and parsing do not.
- **Rollback:** revert the five defaults. Because each is an override, a single
  regressing path can be pinned back to its 5.4 model **without a redeploy**,
  which is the intended first response to a regression.

## Error Handling And Observability
Parsing, fallbacks and confidence floors are untouched. A model that rejects a
parameter must fail loudly at the call site rather than being retried blindly —
a silent retry would hide exactly the 400 this plan exists to prevent.

Add reasoning tokens to `openai_tokens_total` (a `direction="reasoning"` series
or equivalent) so the billed-but-invisible half of the output is countable. A
reasoning model whose reasoning is not metered makes the cost dashboard
understate the bill by construction.

## Test Plan
Feature file: `tests/bdd/enrichment/model-upgrade-gpt-5-6-luna.feature`

Scenarios:
- Omit temperature for a model that pins sampling.
- Pass temperature unchanged for a 5.4-family model, proving no shipped path
  changes behavior.
- Classify a batch of 10 and return 10 verdicts under the existing output
  budget.
- Surface a parameter-rejection 400 as a clear failure rather than a silent
  fallback.
- Count reasoning tokens in the token metric.
- Keep the judge's verdict schema and adjudication band unchanged.

Pytest unit tests:
- `sampling_kwargs` across: a pinning model, a 5.4 model, an unknown model
  (must pass temperature through), and `temperature=0` (must not be dropped as
  falsy — the judge's value is 0, and a truthiness check would silently discard
  exactly the one that matters most).
- Each of the four clients builds its request with the helper applied.
- Token accounting includes reasoning tokens.
- Every existing client test still passes with the model defaulted to luna.

Manual or integration checks:
- The cost/quality comparison in §C, recorded in the PR body.
- One live call per client path against the real API to confirm no other
  parameter is rejected — the temperature 400 was only found by making the call,
  and the same is true of anything else this model refuses.

## Acceptance Criteria
- All five settings default to `gpt-5.6-luna`.
- No call site passes `temperature` to a model that rejects it, and 5.4-family
  behavior is byte-identical to today.
- `temperature=0` is preserved for a model that accepts it.
- A batch of 10 returns 10 verdicts.
- Reasoning tokens are metered.
- The measured cost comparison is in the PR body, and a material increase is
  called out rather than merged silently.
- `make test-unit` and `make test-bdd` pass.

## Open Questions
None blocking. Whether luna is worth its cost is answered by the measurement in
§C, which is why that measurement gates the merge instead of following it.
