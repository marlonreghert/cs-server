# Wire The Instagram Judge

## Branch
feature/wire-the-instagram-judge

## Goal
Give the cascade a way to settle candidates the cheap signals cannot, so venues
whose only evidence is a plausible-looking search result stop being discarded.

## Non-goals
- Restoring probe reachability (needs an egress change).
- A web-search source (needs a new paid provider and a separate decision).
- Changing provenance weights or the existence-bonus rule.

## Evidence

`InstagramJudge` and its prompt were built in #121 and have never run. There is
no client for it, `instagram_judge_enabled` in `app/config.py` is read by
nothing, the `judge_enabled` run-config key is read by nothing, and
`app/container.py` passes `judge=None` with the comment "opt-in; wired when a
judge client is configured". It never was.

Meanwhile the top-250 Recife run leaves candidates on the floor that the judge
exists to settle:

| venue | source | confidence | outcome |
|---|---|---|---|
| Tio Pepe | venue_website | 0.624 | rejected, bar 0.65 |
| Bercy Boa Viagem | (measured) | 0.769 | ambiguous by design |
| Subway | apify_search | 0.428 | never even adjudicated |
| Chiwake Recife | apify_search | 0.371 | never even adjudicated |

Two distinct gaps. The first is that no judge exists to call. The second is that
adjudication only fires between `ambiguous_low` (0.50) and the bar, so a paid
search result — provenance 0.20, so at most 0.60 even with a perfect name match —
usually falls under the floor and is discarded without anyone looking.

The paid tier can NEVER reach the bar on its own while the probe is blocked:
0.20 + 0.40 x 1.0 = 0.60 < 0.65. Without the judge that whole tier is dead
weight, and it is the only tier that reaches the 76 top-250 venues with no
website at all.

## Current Behavior
`judge=None`, so `_adjudicate` records `unavailable` and returns the candidate
untouched. Anything between the floor and the bar is persisted `low_confidence`;
anything below the floor is persisted `not_found` and its handle discarded.

## Desired Behavior
1. Build a judge when an OpenAI key is configured and the judge is enabled.
2. Let a run turn the judge off, and let a run turn it on when the deployment
   default is off.
3. Adjudicate candidates from a configurable floor up to the bar, so a
   low-provenance candidate with a plausible name is looked at rather than
   dropped.
4. Keep the text-only ceiling: a verdict reached without images can raise a
   candidate to at most 0.80, never to certainty.
5. Never let the judge fail a venue: no key, a refusal, a malformed reply, a
   timeout — each degrades to the unjudged confidence and the run continues.
6. Never call the judge for a candidate already above the bar, or one whose
   profile is confirmed absent. Both are already decided.

## Implementation Approach

A thin client alongside the existing OpenAI clients, exposing the one method
`InstagramJudge` already calls. It sends the venue and profile text, attaches
images only when they exist, and asks for a JSON object.

The cascade gains a judge floor separate from `ambiguous_low`, because the two
mean different things: `ambiguous_low` is "good enough to store as a weak
result", while the judge floor is "worth paying a fraction of a cent to settle".
Defaulting the floor below `ambiguous_low` is what lets a paid-search candidate
reach the judge at all.

Per-run `judge_enabled` overrides the deployment default in both directions.

## Data, Config, And API Impact
- Persistence, API, migrations: none. `discovery.judge_mode` and
  `judge_reason` already exist and are already persisted.
- New settings: the judge model and the judge floor. `instagram_judge_enabled`
  finally does something.
- Cost: one small-model call per adjudicated candidate. Bounded by the floor and
  by the fact that only unresolved candidates reach it.

## Error Handling And Observability
`INSTAGRAM_JUDGE_TOTAL{mode,verdict}` already distinguishes `unavailable` from a
real verdict, so a misconfigured key is visible rather than silent. Every judge
failure is already caught inside `InstagramJudge.judge` and returns None; this
plan does not widen that surface.

## Test Plan
Feature file: `tests/bdd/enrichment/wire-the-instagram-judge.feature`

Scenarios:
- Accept a candidate the judge confirms.
- Reject a candidate the judge rejects.
- Cap a text-only verdict below certainty.
- Leave a candidate alone when the judge is unavailable.
- Leave a candidate alone when the judge errors.
- Adjudicate a low-provenance candidate that the old floor would have dropped.
- Never adjudicate a candidate already above the bar.
- Never adjudicate a profile confirmed absent.
- Turn the judge off for a single run.
- Turn the judge on for a single run when the deployment default is off.

Pytest unit tests:
- `tests/test_instagram_judge_client.py` — prompt carries venue name and handle;
  images attached only when present; a malformed reply, a refusal and a timeout
  each yield None rather than raising.
- `tests/test_judge_adjudication_band.py` — the floor decides who is judged; the
  bar and the confirmed-absent case short-circuit; per-run overrides both ways.

Manual or integration checks:
- Re-run the scoped top-250 Recife job with the judge on and compare the
  accepted count against the same run with it off.

## Acceptance Criteria
- A confirmed candidate is accepted; a rejected one is not.
- A text-only verdict cannot exceed 0.80.
- No key, an error, or a refusal leaves the venue exactly as it was.
- A candidate above the bar is never sent to the judge.
- `make test-bdd` and `make test-unit` pass; `@wip` removed.

## Open Questions
- None.
