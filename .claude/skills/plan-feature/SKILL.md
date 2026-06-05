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
artifacts, or runtime configuration while using this skill. The required outputs
are: the plan file committed on `main`, a feature branch, and the Gherkin
feature file committed on that branch (unless the plan is explicitly
BDD-exempt). Do not push.

Read `CLAUDE.md` first. It is the source of truth for this repository's agent
rules.

## Artifact placement (read first)

This workflow deliberately splits the two artifacts:

- **The plan file lives on `main`.** Plans are committed straight to `main` so
  several in-flight plans stay visible in one place and are never stranded on a
  branch. A plan file is documentation only — it changes no behavior.
- **The Gherkin feature file lives on the feature branch**, never on `main`. A
  scenario on `main` without its implementation would fail the suite, so the
  feature file only reaches `main` later, through the feature's PR, alongside the
  code that makes it pass.
- The plan file (on `main`) is the pointer to the branch: it records the branch
  name and the feature-file path.

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

## Phase 3: Write The Plan On `main`

The plan file is committed to `main`, before the feature branch exists.

Preflight:

- `git rev-parse --abbrev-ref HEAD` must be `main`. If not, stop and ask how to
  proceed — the plan is committed on `main`.
- `git status --short` must have no uncommitted tracked changes. If it does, stop
  and ask the user to commit or stash first. Untracked files are fine.

Then:

1. Derive `<slug>` (kebab-case from the feature title) and `<YYMMDD>` (two-digit
   year, month, day — e.g. 2026-06-05 → `260605`).
2. Create `plans/<YYMMDD>_<slug>.md` from `plans/PLAN_TEMPLATE.md`.
3. Fill the plan. It MUST include:
   - `## Branch`: `<prefix>/<slug>` — the branch created in Phase 4. Use `fix/`
     for bugs and regressions, `feature/` for new behavior, `chore/` for tooling
     or lifecycle-only changes.
   - Goal and non-goals; evidence with file references; current vs desired
     behavior; implementation approach; data/config/API impact; error-handling
     and observability; test plan with `Feature file:
     tests/bdd/<domain>/<slug>.feature` or `# bdd-exempt: <reason>`; targeted
     pytest plan for critical internal logic; acceptance criteria; open
     questions (`/execute-feature` must not proceed while any remain).
   - Avoid code blocks unless they record a specific design decision (API schema,
     critical validation, monitoring labels, performance-sensitive algorithm).
4. Stage only the plan file by explicit path and commit on `main` with
   `docs: plan <slug>`. Never `git add .` or `git add -A`. Do not push.

## Phase 4: Branch

Create the feature branch from `main` so it inherits the plan file you just
committed:

- The branch name is the `## Branch` value from the plan (`<prefix>/<slug>`).
- If the branch already exists locally or on origin, stop and ask whether to
  resume it or pick another slug. Never force-overwrite.
- Run `git checkout -b <prefix>/<slug>`.

## Phase 5: Write Gherkin On The Branch

On the feature branch, create or update `tests/bdd/<domain>/<slug>.feature`
(domain from Phase 2). The feature file lives only on the branch.

Rules:

- Use English Gherkin keywords.
- Write imperative scenarios: the system must do the behavior described.
- Prefer behavior and outputs over implementation details.
- Cover main path, edge cases, error/degraded paths, and empty states when they
  are relevant.
- Include observable assertions: response fields, ordering, filtering, emitted
  status, metrics, logs, or persisted state.
- Do not put code snippets in the feature file.

Stage only the feature file by explicit path and commit on the branch with
`test(bdd): <slug> scenarios`. Do not push.

## Phase 6: Stop

Report:

- Plan path on `main`: `plans/<YYMMDD>_<slug>.md`.
- Feature branch: `<prefix>/<slug>`.
- Feature-file path on the branch, or the BDD exemption.
- Open questions, if any.
- Next command (run on the branch): `/execute-feature plans/<YYMMDD>_<slug>.md`.

Do not implement production code, run tests, push, or open a PR.
