# CS-Server Claude Guide

`AGENTS.md` is the canonical agent guide for this repository. Read it first and
follow it for architecture, testing, BDD lifecycle, observability, and security
rules.

Claude-specific local skills are command wrappers around canonical Codex skill
folders:

- `/plan-feature <description>`: explore first, create or switch to the feature
  branch, write the plan under `plans/`, and create or update the matching
  Gherkin file under `tests/bdd/`. Canonical workflow:
  `.agents/skills/plan-feature/SKILL.md`.
- `/execute-feature <plan path>`: execute an approved plan through strict BDD:
  red scenario, minimal implementation, targeted pytest coverage, observability
  and error-handling review, verification, then user-gated commit/PR. Canonical
  workflow: `.agents/skills/execute-feature/SKILL.md`.

Operational setup, endpoints, configuration, Docker, and deployment notes live
in `README.md` and `DEPLOYMENT.md`. Current pytest, Redis integration, and BDD
notes live in `tests/README.md`.
