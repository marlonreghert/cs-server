# Dormant Versus Broken Targets — an account that stopped posting is not a fault

## Branch
fix/dormant-vs-broken-targets

## Goal
An operator can tell a crawl target that is **dormant** (alive, public, but
nothing inside the lookback window) from one that is **broken** (wrong handle,
blocked, timing out) without opening Instagram.

## Non-goals
- **Changing how `no_items` is classified.** It is correct — see Evidence. Do
  not "fix" it.
- **Promoter accounts.** Out of scope, both targets disabled.
- **Deleting or disabling dormant targets automatically.**

## Evidence

### The three "never seeded" targets are all dormant, not broken
`260813_crawl-transport-failure-visibility.md` §E added `posts_never_seeded`,
which immediately surfaced three targets that had never produced a post. They
were assumed to be wrong handles. **They are not.** Probed against Apify on
2026-08-13 with the date bound removed:

| target | posts returned unbounded | newest observed post | age at probe |
|---|---|---|---|
| `armazem14.recifeantigo` | 5, `error=None` | 2025-05-31 | ~15 months |
| `downtownrecife` | 5, `error=None` | 2024-04-23 | ~28 months |
| `burburinhobar` | 5, `error=None` | **2026-05-08** | **3 months, 5 days** |

All three are alive and public. Every bounded run returns `no_items` with
`requestErrorMessages: []` because `onlyPostsNewerThan: "3 months"` genuinely
excludes everything they have. **The classification is right; the reporting is
what misleads.**

### `burburinhobar` misses the window by five days
Its newest observed post is 3 months and 5 days old. A seed lookback marginally
longer than the steady-state one would have caught it. That is one target today,
but it is the general shape: a seed is a one-time, historical read and has no
reason to share the steady-state window.

### `posts_never_seeded` currently reads as an accusation
It is true for a dormant account and true for a broken one, and an operator
cannot tell which without doing what I did — an out-of-band Apify probe. That is
the gap.

## Current Behavior
A dormant target and a broken target are both reported as never seeded, with an
`empty` outcome and no failure. Distinguishing them requires a manual probe.

## Desired Behavior
1. Report a target as **dormant** when the account answers but has nothing in
   the window.
2. Keep reporting a genuinely broken target as failed, exactly as today.
3. Give a seed a longer reach than a steady-state run.
4. Never auto-disable a dormant target.

## Implementation Approach

### A. Record dormancy as its own outcome
`OUTCOME_EMPTY` with an Apify error item present, on a stream whose cursor has
never advanced, is the dormant signature. Record it distinctly — an outcome
label and a field on the target — so it reaches the admin read model.

**It is not a failure.** It must not increment `consecutive_failures` and must
not auto-disable. A venue that stops posting for a season is a normal state, and
turning that into a fault is how a healthy target gets switched off.

Keep `posts_never_seeded` as it is; pair it with dormancy so the console can say
"never seeded — account dormant" rather than leaving the operator to guess.

### B. Give the seed its own lookback
Add a seed-specific lookback, defaulting longer than the steady-state window —
**12 months** is the starting proposal, and it would have caught `burburinhobar`
and `armazem14.recifeantigo` while still excluding `downtownrecife`.

Put it in **admin config** (runtime-editable, like `menu_expiry_days`), and make
it per-target overridable the same way `seed_results_limit` already is — the
existing seed-versus-steady-state split is the precedent to follow, not a new
idea.

**Cost is the constraint, and it is bounded by the results cap, not the window.**
A longer window does not fetch more posts; it changes *which* posts are eligible,
and `seed_results_limit` still caps the count. Say that explicitly in the PR so
nobody assumes a 4x window means 4x spend.

### C. Do not probe automatically
Resist adding an unbounded "is this account alive?" call. It costs Apify results
per target per run to answer a question that changes rarely. §A's dormancy
signal is derived from data we already have.

## Data, Config, And API Impact
- **Migration** — a nullable column on `events.crawl_target` for the dormancy
  signal, if one does not already serve. Check `last_failure_kind` and
  `posts_never_seeded`'s derivation first and say which you found.
- **Config (admin, runtime)** — a seed lookback, default 12 months.
- **Admin API** — additive dormancy reporting. The console is a released client;
  nothing may be removed.
- **Rollback:** revert. Nullable column, unread by older code.

## Error Handling And Observability
- Count dormant outcomes by target. **A target that flips from dormant to
  producing is the signal worth celebrating; one that flips the other way is
  worth noticing** — a venue going quiet is a real-world fact the product should
  know about.
- Do not log a dormant run at warning. It is not a problem, and warning-level
  noise for a normal state is how real warnings get ignored.

## Test Plan
Feature file: `tests/bdd/enrichment/dormant-vs-broken-targets.feature`

Scenarios:
- Report a target as dormant when its account answers but has nothing in the window.
- Do not increment the failure counter for a dormant target.
- Do not disable a dormant target.
- Keep reporting a blocked target as blocked.
- Keep reporting a non-existent handle as permanently failed.
- Surface dormancy alongside never-seeded on the admin read model.
- Use the seed lookback on a target whose cursor is unset.
- Use the steady-state lookback once the cursor is set.
- Honour a per-target seed lookback override.
- Stop reporting dormancy once the target produces a post.

Pytest unit tests:
- The dormancy predicate: error item present + empty + cursor unset; error item
  present + cursor set; no error item; a blocked error item (must not be dormant).
- Lookback selection: seed versus steady state, with and without a per-target
  override.
- `consecutive_failures` is untouched by a dormant run.

Manual or integration checks:
- After deploy, re-run `burburinhobar` and confirm the longer seed window
  reaches its 2026-05-08 post. One target, once — it costs Apify results.

## Acceptance Criteria
- A dormant target is reported distinctly from a broken one.
- Dormancy never increments the failure counter and never disables a target.
- Blocked and not-found classification is provably unchanged.
- A seed uses the longer lookback; a steady-state run does not.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None.
