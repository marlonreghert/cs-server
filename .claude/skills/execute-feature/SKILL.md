---
name: execute-feature
description: Execute an approved CS-Server plan through strict BDD red-green workflow, targeted pytest coverage, verification, and user-gated PR handling.
---

# Execute Feature

Canonical agent workflow for executing an approved CS-Server plan through
strict BDD, targeted pytest coverage, verification, and a user-gated PR
workflow.

Codex mapping: `.agents/skills/execute-feature/SKILL.md` is a thin ref that
points to this canonical workflow.

Execute only an approved plan produced by `/plan-feature`. Read `CLAUDE.md`
first. Then run **Sync** (below) before anything else — it checks out the plan's
branch so the plan file and the linked Gherkin feature file are on disk; read
both before touching code.

## Sync to the plan's branch

Run this **first** — before the preconditions and before reading the plan. The
plan doc lives on `main` and the `@wip` feature file lives on the feature branch,
both pushed by `/plan-feature`. A fresh `git clone --recurse-submodules` leaves
this repo detached at the pinned commit with neither file on disk, so sync from
origin and check the branch out rather than assume it is already current.

1. `git fetch origin --prune`.
2. Resolve the branch name from the plan's `## Branch` line. The invocation gives
   `plans/<YYMMDD>_<slug>.md` (the slug, not the `fix/`|`feature/`|`chore/`
   prefix). If the plan file is on disk, read it; otherwise read it from origin
   without touching the tree: `git show origin/main:plans/<YYMMDD>_<slug>.md`. If
   it exists in neither, stop and tell the user to run `/plan-feature`.
3. Land on `<prefix>/<slug>`:
   - If `git rev-parse --abbrev-ref HEAD` already equals it, this is a warm
     re-run — the `@wip` file is already present. Proceed; **do not** pull over
     local work.
   - Otherwise require a clean tree (`git status --short` empty of tracked/staged
     changes; if dirty, stop and ask the user to commit or stash), then
     `git checkout <prefix>/<slug>`. When the branch exists only on origin this
     creates a local tracking branch from `origin/<prefix>/<slug>`, materializing
     the plan doc and the `@wip` feature file.
   - If `git checkout` fails because the branch exists nowhere (not local, not on
     origin), stop and tell the user to run `/plan-feature` — there is no `@wip`
     feature file to execute against.

Do not merge or rebase `origin/main` into the branch here, and never force — the
feature branch already contains the plan commit.

## Preconditions

- The user explicitly approved the plan.
- The plan file exists under `plans/` (established by Sync).
- The plan has no unresolved open questions.
- HEAD is on the plan's branch (established by Sync).
- `git status --short` has no unrelated tracked or staged changes.
- The plan's test plan contains `Feature file:` or `# bdd-exempt: <reason>`.

If any precondition fails, stop and report the exact blocker.

## Phase 1: BDD Red

If the plan has a `Feature file:` entry:

1. Read every scenario.
2. Add or update Behave step definitions only as needed to make scenarios run.
3. Run `make test-feature FEATURE=<feature-file-path>`.
4. Do not write production code until the scenario reaches true red:
   - `FAILED` on a meaningful assertion is true red.
   - `UNDEFINED`, syntax errors, missing imports, environment errors, and setup
     errors are not true red.

If the plan is BDD-exempt, state the exemption and proceed to targeted pytest
coverage first.

## Phase 2: Implement

- Make the smallest production change that satisfies the plan.
- Follow existing router, handler, service, DAO, and model boundaries.
- Do not expand scope beyond the plan.
- If the plan is wrong or incomplete, stop and ask for a plan revision.
- Do not weaken the Gherkin scenario to make implementation easier.

## Phase 3: Unit Coverage

Add or update pytest tests for critical internal logic touched by the change.

Prioritize:

- Request validation and response shaping.
- Venue sorting, filtering, mapping, refresh, and enrichment rules.
- Redis key formats, DAO serialization, and cache boundaries through mocks.
- Error and degraded paths.
- Metrics/logging behavior for new runtime paths.

Do not duplicate BDD assertions in pytest unless the unit test protects a lower
level edge case or failure mode.

## Phase 4: Observability And Error Handling

For every new runtime path, verify whether the change needs:

- Structured logs with request or operation context.
- Prometheus counters, gauges, or histograms.
- Graceful degradation for optional dependencies.
- Clear failures for invalid requests or required dependency failures.
- Protection against silent background-task failures.

Record what was added or why no new observability was needed.

## Phase 5: Green And Refactor

Run:

- `make test-feature FEATURE=<feature-file-path>` when a feature file exists.
- `make test-unit`.
- `make test-bdd` when multiple feature files may be affected.

When tests are green, refactor only the files touched by this feature:

- Prefer existing helpers over new helpers.
- Remove dead code introduced during the change.
- Keep naming clear and behavior unchanged.
- Re-run affected tests after refactoring.

## Phase 6: Diagnose Failures

If a test fails after implementation, diagnose before editing again:

- Expected behavior from the plan or scenario.
- Observed failure.
- Likely origin with file reference.
- Proposed fix.

If the fix changes scope or acceptance criteria, stop and ask for approval.

## Phase 7: User-Gated Commit And PR

Before committing:

- Re-check `git status --short`.
- List the files that will be staged.
- Ask the user for explicit approval to commit and open a PR.

On approval:

- Stage only files related to this feature.
- Commit with a concise subject under 72 characters.
- Push the current branch.
- Open a PR against `main`.

Never push directly to `main`. Never use `git add -A`, `git add .`, `--force`,
or `--no-verify`.
