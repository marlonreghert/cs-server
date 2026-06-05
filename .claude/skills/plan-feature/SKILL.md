---
name: plan-feature
description: Plan a CS-Server feature or bug fix with BDD-first acceptance criteria. Use before implementation for non-trivial behavior changes in this repository.
---

# Plan Feature

Canonical agent workflow for planning a CS-Server feature, bug fix, production
hardening change, or behavior change with BDD-first acceptance criteria.

Codex mapping: `.agents/skills/plan-feature/SKILL.md` is a thin ref that points
to this canonical workflow.

Plan only. Do not edit production code, test code, dependencies, generated
artifacts, or runtime configuration while using this skill. The required
outputs are a branch, one plan file, and one Gherkin feature file unless the
plan is explicitly BDD-exempt.

Read `CLAUDE.md` first. It is the source of truth for this repository's agent
rules.

## When To Use

Use for non-trivial features, bug fixes, behavior changes, API changes,
configuration changes, production hardening, observability changes, persistence
changes, and enrichment/refresh behavior.

Skip for typo fixes, comment-only edits, and obvious one-line mechanical edits.

## Phase 1: Explore

- Read the user's request and identify the behavior being changed.
- Inspect relevant code before asking questions. Prefer concrete file evidence
  over assumptions.
- Check likely boundaries:
  - `main.py` for app wiring, lifecycle, and background jobs.
  - `app/routers/` for API route contracts.
  - `app/handlers/` for request behavior and response shaping.
  - `app/services/` for refresh, enrichment, classification, and business
    logic.
  - `app/dao/` and `app/db/` for Redis persistence and key formats.
  - `app/models/` for Pydantic boundaries and serialization compatibility.
  - `app/config.py`, `config.example.json`, and `.env.example` for settings.
  - `app/metrics.py` and current logging for observability.
  - Existing pytest files and `tests/bdd/` for related coverage.
- Check `git status --short --branch`.
- Read existing plans when they are relevant.

Do not proceed with unclear behavior. Ask only questions that cannot be answered
from the repo.

## Phase 2: Classify The Change

Pick one primary domain for the feature file:

- `tests/bdd/api/` for HTTP contracts, request/response behavior, validation,
  and health/debug endpoints.
- `tests/bdd/refresh/` for BestTime venue discovery, live forecast refresh,
  weekly forecast refresh, scheduling, and startup refresh behavior.
- `tests/bdd/enrichment/` for Google Places, Instagram, menu photo, menu
  extraction, and vibe classifier behavior.
- `tests/bdd/persistence/` for Redis key compatibility, DAO behavior, data
  migrations, and cache boundaries.
- `tests/bdd/observability/` for metrics, tracing, logging, and background job
  failure visibility.

If no user-visible or externally observable behavior exists, add
`# bdd-exempt: <reason>` in the plan's test plan instead of inventing a weak
scenario.

## Phase 3: Branch

Always create or switch to the feature branch before writing the plan file or
Gherkin feature file. Do not write planning artifacts on `main`.

- Use `fix/<slug>` for bugs and regressions.
- Use `feature/<slug>` for new behavior.
- Use `chore/<slug>` for tooling, documentation, or lifecycle-only changes.

Preflight:

- If there are uncommitted changes, including untracked files, ask the user how
  to proceed before branching or writing files.
- If the current branch is not `main`, stop and ask how to proceed.
- If the target branch already exists locally or remotely, stop and ask whether
  to resume it or choose another slug.

Then create the branch with `git checkout -b <prefix>/<slug>`.

## Phase 4: Write Gherkin

Create or update `tests/bdd/<domain>/<slug>.feature`.

Rules:

- Use English Gherkin keywords.
- Write imperative scenarios: the system must do the behavior described.
- Prefer behavior and outputs over implementation details.
- Cover main path, edge cases, error/degraded paths, and empty states when they
  are relevant.
- Include observable assertions: response fields, ordering, filtering, emitted
  status, metrics, logs, or persisted state.
- Do not put code snippets in the feature file.

## Phase 5: Write The Plan

Create `plans/<slug>_<DD_MM_YY>.md` using `plans/PLAN_TEMPLATE.md`.

The plan must include:

- Goal and non-goals.
- Evidence from the current codebase with file references.
- Current behavior and desired behavior.
- Implementation approach.
- Error-handling and observability requirements.
- Data/config/API impacts.
- Test plan with `Feature file:` or `# bdd-exempt: <reason>`.
- Targeted pytest unit-test plan for critical internal business logic.
- Acceptance criteria.
- Open questions. `/execute-feature` must not proceed while any remain.

Avoid code blocks unless they record a specific design decision that must not be
lost, such as an API schema, critical validation rule, monitoring label set, or
performance-sensitive algorithm.

## Phase 6: Stop

Report:

- Branch name.
- Plan path.
- Feature-file path or BDD exemption.
- Open questions, if any.
- Next command: `/execute-feature <plan path>`.

Do not implement production code, run tests, commit, push, or open a PR.
