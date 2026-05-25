---
name: execute-feature
description: Execute an approved CS-Server plan through strict BDD red-green workflow, targeted pytest coverage, verification, and user-gated PR handling.
---

# Execute Feature

Canonical agent workflow for executing an approved CS-Server plan through
strict BDD, targeted pytest coverage, verification, and a user-gated PR
workflow.

Claude mapping: `/execute-feature` maps to
`.claude/skills/execute-feature/SKILL.md`, which delegates to this skill.

Execute only an approved plan produced by `/plan-feature`. Read `AGENTS.md`,
the plan file, and the linked Gherkin feature file before touching code.

## Preconditions

- The user explicitly approved the plan.
- The plan file exists under `plans/`.
- The plan has no unresolved open questions.
- The current branch matches the plan's branch.
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
