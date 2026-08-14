# Seeded State And Config Validation — stop reading absence as an answer

## Branch
fix/seeded-state-and-config-validation

## Goal
Two bookkeeping facts stop lying. A reels stream that ran and found nothing is
recorded as **seeded**, so its one-time seed is never re-purchased. An admin
config value is **validated on write**, so turning a switch off cannot turn it
on.

## Non-goals
- **Changing what the reels stream fetches, or its caps and budget.**
  `260811_reels-on-seed-only.md` owns that and its decision stands: reels are
  worth paying for on the seed and never again.
- **Re-enabling reels on targets where `crawl_reels` is off.** Untouched.
- **Changing any admin config VALUE.** `event_dedup_auto_merge_enabled` was set
  to `true` on 2026-08-14 and stays true; this plan only makes future writes
  type-safe.
- **A generic config-schema framework.** Register the validators that already
  exist. Nothing more.
- **Retrying a genuinely failed seed differently.** A blocked or timed-out
  reels run must stay retryable — that is correct today and must remain so.

## Evidence

### §A. A reels stream that finds nothing re-buys its seed forever
`reels_already_seeded` gates on the cursor
(`instagram_crawl_service.py:937`):

```
return _as_utc_dt(target.get("cursor_reels_at")) is not None
```

`cursor_reels_at` is written **only** when a stream returns
`OUTCOME_SUCCESS` (line 1303). A run with no kept items returns early at line
1246 with an outcome of `empty`/`blocked`/`not_found` and **no `new_cursor` key
at all**. So a reels run that succeeds and legitimately returns nothing never
records a cursor, `reels_already_seeded()` stays False, and the "one-time" seed
runs again on the next schedule — forever.

Measured in production, 2026-08-13:

| target | reels fetched | `cursor_reels_at` | correct? |
|---|---|---|---|
| `burburinhobar` | 0 | NULL | **defect** — account alive, has no reels |
| `downtownrecife` | 0 | NULL | **defect** — `posts_dormant=true`, genuinely empty |
| `armazem14.recifeantigo` | 0 | NULL | correct — blocked, must retry |
| `champagne.clubrecife` | — | NULL | correct — has not run since its handle was fixed |
| `conchittasbar` | 41 | set | correct |
| `downtownbeergarden_` | 12 | set | correct |
| `casabacurau` / `beerdock_recife` / `saladerebocorecife` / `entreamigosobode` | 50/37/50/3 | set | correct |

**4 enabled targets** sit in the never-seeded state, all on cron
`0 3 * * 5,6,0` — three runs a week each, indefinitely, and it never
self-heals. The Apify run log for one of them says it plainly:
`NO RESULTS: zero public reels for .../burburinhobar/ within the given
requirements`, `2 requests: 2 succeeded, 0 failed`. The scrape worked. We just
refuse to write down that it did.

**The bug is worst on the accounts that give us least** — the emptier the
account, the more often we pay for its seed.

#### Why it was missed, and why the guard did not catch it
`reels_already_seeded`'s own docstring reasons carefully about exactly one of
the two null cases:

> *Gated on the CURSOR itself — never a separate "has run" flag — because the
> cursor stays null until a run's bookkeeping write actually succeeds. A flag
> would have to be unset by hand on every failure path to keep a failed seed
> retryable.*

That is right **for failures**, and it was written to protect a real prior
incident. It never considered a *successful empty* run, which lands in the same
null state and inherits "retry forever" — the opposite of what it needs.

The root cause is one field answering two questions: *what timestamp did we
reach?* and *has the seed happened?* For a non-empty result those coincide; for
an empty one they diverge, because **there is no timestamp with which to record
that something happened.**

The waste is also invisible to the guard built to catch overspend
(`instagram_crawl_service.py:1180`):

```python
if result_count:
    CRAWL_RESULTS_TOTAL.labels(result_type=stream).inc(result_count)
    new_spent = self.budget_dao.increment_month(year_month, result_count)
```

A zero-result run increments nothing, so the internal ledger reports `$0.00`
for a run that consumed real Apify compute (~30s wall clock in the log above).
`CRAWL_RUNS_TOTAL{result_type="reels", outcome="empty"}` does record it, but
the counter is in-process and resets on every deploy, so no historical total
survives.

#### A second route to the same dead end
`kept` comes from `_split_kept_and_dropped(raw_posts, cutoff)` — the
pinned/cutoff split, **before** cross-stream dedupe. That is a relief: a target
whose reels are all duplicates of its posts (the reels-on-seed plan measured 32
fetched → 1 new) still has a non-empty `kept` and seeds correctly.

But a run whose returned reels are **all pinned, or all older than the cutoff**
also produces `kept == []` with a real, billed result set, and takes the same
early return. An inline comment at line 1236 asserts that case "is NOT this
branch"; reading `_split_kept_and_dropped`, that comment appears to be wrong.
**Confirm it during execution and fix the comment or the code accordingly** —
do not take either on trust.

### §B. Six config validators exist and none of them is registered
`AdminConfigService.set` validates only if a validator is registered for the
key (`admin_config_service.py:49`):

```python
validator = self.validators.get(key)
to_store = validator(value) if validator is not None else value
```

`container.py:608` registers eleven keys: `venue_eligibility`,
`force_update`, `vibe_modes`, `venue_category_map`,
`event_candidate_categories`, `post_category_vocabulary`, `menu_expiry_days`,
`date_year_roll_grace_days`, `instagram_discovery`, `hide_promoter_events`,
`crawl_seed_lookback`.

Sixteen `validate_*_config` functions exist across the codebase. The five…
in fact **exactly the six `event_dedup_*` ones are the only validators never
registered**:

| key | validator (exists, unwired) |
|---|---|
| `event_dedup_generic_vocabulary` | `validate_generic_vocabulary_config` |
| `event_dedup_stopwords` | `validate_stopwords_config` |
| `event_dedup_lineup_threshold` | `validate_lineup_threshold_config` |
| `event_dedup_candidate_window_hours` | `validate_candidate_window_hours_config` |
| `event_dedup_undated_window_days` | `validate_undated_window_days_config` |
| `event_dedup_auto_merge_enabled` | `validate_auto_merge_enabled_config` |

Every other module's validator is wired. This is a single, contained omission,
not a systemic pattern.

#### The reader coerces, so an unvalidated value becomes a wrong answer
`load_dedup_config` (`event_dedup.py:200-202`):

```python
generic_vocabulary=tuple(generic), stopwords=tuple(stopwords),
lineup_threshold=int(threshold), candidate_window_hours=int(window_hours),
undated_window_days=int(undated_days), auto_merge_enabled=bool(auto_enabled),
```

- **`bool(auto_enabled)` is the dangerous one.** Storing the *string* `"false"`
  through the generic CRUD route yields `bool("false")` → **True**. Turning
  auto-merge off through the console would turn it on — on a flag that now
  merges rows.
- `tuple(generic)` on a string silently yields a tuple of single characters,
  producing a vocabulary of letters rather than words.
- `int(threshold)` on non-numeric text raises inside the crawl path.

This matters more since 2026-08-14, when `event_dedup_auto_merge_enabled` was
set to `true` and the dedup sweep absorbed 12 rows. The flag is now
load-bearing.

## Current Behavior
A reels run that returns nothing leaves `cursor_reels_at` NULL and is
re-attempted on every subsequent scheduled crawl. Admin writes to any
`event_dedup_*` key are stored unvalidated, and the reader coerces whatever it
finds.

## Desired Behavior
1. A reels stream that **completed** — including one that legitimately returned
   nothing — is recorded as seeded and never re-purchased.
2. A reels stream that **failed** (blocked, timeout, not-found, transport
   error) stays unseeded and is retried, exactly as today.
3. Whether a stream returned zero items is visible without reading code.
4. Every admin-config key with a validator has it registered, and a value that
   fails validation is rejected before it reaches RDS or Redis.
5. A stored config value is used as validated, not coerced into a different
   meaning.

## Implementation Approach

### A. Separate "seeded" from "what we reached"
Record seeding as its own fact rather than inferring it from a timestamp that
an empty result cannot produce. Either a dedicated
`reels_seeded_at`/`reels_seeded` column written whenever the reels stream
reaches a terminal **non-failure** outcome, or make `reels_already_seeded`
consult the recorded outcome instead of the cursor.

Whichever shape: the set of outcomes that count as "seeded" must be defined
against `FAILURE_OUTCOMES`, not enumerated by hand, so a future outcome added
to that set cannot silently start counting as a successful seed.

`cursor_reels_at` keeps its own meaning — the newest reel reached — and stays
NULL when nothing was reached. Two facts, two fields.

**The failure path must not regress.** `OUTCOME_BOOKKEEPING_FAILED` exists
because a bookkeeping write once failed and left a target looking permanently
healthy (2026-08-09, `entreamigos.praia`). A seed marker written before its
own bookkeeping commits would reintroduce exactly that. It must be written in
the same commit as the rest of the run's bookkeeping.

### B. Back-fill the four already-stuck targets
A forward fix leaves `burburinhobar` and `downtownrecife` re-buying their seed,
because nothing re-touches them except the schedule that keeps paying. This
project has now shipped that mistake twice (`260812_history-repair-dates.md`
exists for the same reason).

Mark as seeded exactly those targets whose reels stream **completed** and
returned nothing. `armazem14.recifeantigo` (blocked) and
`champagne.clubrecife` (never run) must stay unseeded so their seed is still
retried. Small enough to be a one-off script or a data migration; the
distinction between the two groups is the part that must be got right, and it
is recoverable from `last_run_reels_fetched` together with the target's failure
bookkeeping — confirm both are sufficient before writing, and if they are not,
report which targets are ambiguous rather than guessing.

### C. Register every existing config validator
Add the six `event_dedup_*` entries to `container.py`'s validator map. No new
validators are written — all six already exist and are already tested.

Add a test that fails when a `validate_*_config` function exists with no
registration, so the next added key cannot repeat this. That test is the real
deliverable of §C; the six entries are a one-time consequence of it.

### D. Trust the validated value instead of coercing it
`load_dedup_config` must not turn a wrong type into a plausible answer. Reject
a value whose type is wrong and fall back to the shipped default — the
per-key independent fallback `_load_json_config` already implements — rather
than coercing it. Count the fallback so a bad stored value is visible instead
of silent.

`bool()` on a config value is the specific coercion to remove.

## Data, Config, And API Impact
- **Migration** — one, if §A adds a column to `events.crawl_target`
  (nullable, no backfill in the migration itself; §B is a separate, reviewable
  step). Additive only.
- **Admin config** — no VALUE changes. Writes to the six dedup keys become
  validated, so a previously-accepted malformed write now fails loudly. That is
  the point.
- **Admin API** — the crawl-target admin read should expose whether reels are
  seeded, alongside the existing `reels_skip_reason`, so the operator can see
  why a reels run was skipped. Additive; the console is a released N-1 client.
- **Serving projection** — untouched.
- **Rollback** — revert. §B's marking is data and would need its own reversal,
  so its script must report exactly which targets it touched.

## Error Handling And Observability
- Count reels runs by seeded-vs-skipped reason. The signal that matters:
  **a target whose reels stream has run more than once should not exist** once
  §A lands, so alert on a second reels run for the same target rather than on
  the zero-result outcome itself.
- Keep `CRAWL_RUNS_TOTAL{result_type="reels", outcome=...}`. Note in the code
  that it is in-process and resets on deploy — it answers "is this happening
  now", never "how often has this happened".
- Count config-validation rejections by key. A rejection is an operator typing
  something wrong, which they should learn about immediately from the API
  response, not from behaviour changing days later.
- Count `load_dedup_config` type-fallbacks by key (§D).

## Test Plan
Feature file: `tests/bdd/enrichment/seeded-state-and-config-validation.feature`

Scenarios:
- Record a reels stream that returned nothing as seeded.
- Never run the reels seed twice for a target whose stream completed empty.
- Leave a blocked reels stream unseeded so its seed is retried.
- Leave a timed-out reels stream unseeded.
- Leave a handle-not-found reels stream unseeded.
- Keep seeding a reels stream that returned items, exactly as today.
- Keep `cursor_reels_at` NULL when nothing was reached.
- Do not mark a target seeded when its bookkeeping write failed.
- Report whether reels are seeded on the crawl-target admin read.
- Reject a non-boolean auto-merge flag before it reaches RDS or Redis.
- Reject a malformed value for each of the other five dedup keys.
- Keep a valid write working for every dedup key.
- Fall back to the shipped default when a stored dedup value has the wrong
  type, and count the fallback.
- Never let the string "false" enable auto-merge.

Pytest unit tests:
- The seeded predicate across every outcome in `FAILURE_OUTCOMES` and every
  non-failure outcome — asserting the two sets are derived, not enumerated.
- A registration test: every `validate_*_config` in the codebase is present in
  the container's validator map.
- `load_dedup_config` against a wrong-typed stored value per key, asserting the
  default is used and no coercion occurs.
- Whether an all-pinned or all-older-than-cutoff reels run reaches the same
  early return (§A's open item) — pin whichever answer the code actually gives,
  and fix the misleading comment at `instagram_crawl_service.py:1236`.

Manual or integration checks:
- Before §B, list the four never-seeded targets and their reels outcome; after,
  confirm `burburinhobar` and `downtownrecife` are seeded and
  `armazem14.recifeantigo` and `champagne.clubrecife` are not.
- After deploy, confirm the next scheduled crawl runs **no** reels stream for
  the two back-filled targets.

## Acceptance Criteria
- A reels stream that completes with zero items is never re-run for that
  target.
- A failed reels stream is still retried.
- `burburinhobar` and `downtownrecife` stop re-buying their seed;
  `armazem14.recifeantigo` and `champagne.clubrecife` keep theirs pending.
- All six `event_dedup_*` validators are registered, and a test fails if any
  future validator is left unregistered.
- The string `"false"` cannot enable auto-merge through any path.
- No admin-config value is changed by this work.
- `make test-feature`, `make test-unit`, `make test-bdd` pass.

## Open Questions
None. The one unresolved factual question — whether an all-pinned reels run
takes the same early return as an empty one — is a code-reading task for
execution, listed in the test plan, not a decision needing the operator.
