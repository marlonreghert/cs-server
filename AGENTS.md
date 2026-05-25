# CS-Server Agent Guide

This is the canonical guide for Codex and other coding agents working in this
repository. `CLAUDE.md` delegates here to avoid duplicated instructions.

For setup, endpoints, configuration, Docker, and deployment pointers, read
`README.md` and `DEPLOYMENT.md`. For current pytest, Redis integration, and BDD
command details, read `tests/README.md`. Do not duplicate those docs here.

## Working Style

- Think before coding. Inspect the current implementation, write down the
  behavior to verify, and avoid assumptions that can be answered by reading the
  repo.
- Keep changes surgical. Touch only the files required for the requested
  behavior and match the existing Python/FastAPI style.
- Prefer simple, readable code over clever abstractions. Add an abstraction only
  when it removes real duplication or clarifies a complex path.
- Convert every non-trivial request into verifiable behavior before
  implementation.
- Do not copy secrets from prompts, `.env`, logs, local config, or tracked files
  into new docs or code. If a secret is exposed, tell the user to rotate it.

## Lifecycle

Use this workflow for non-trivial features, bug fixes, production hardening, and
behavior changes:

1. `/plan-feature <description>`
2. User approval of the generated plan
3. `/execute-feature <plan path>`

Canonical lifecycle skill workflows live in Codex skill folders under
`.agents/skills/<skill-name>/SKILL.md`. Claude slash commands under
`.claude/skills/` are wrappers that map to those Codex skill folders.

Trivial changes such as typo fixes, comment corrections, or obvious one-line
mechanical edits can skip the lifecycle.

## BDD Policy

- User-visible behavior changes are BDD-first.
- `/plan-feature` must create or update a Gherkin file under `tests/bdd/`.
- Plans must include `Feature file:` in the test plan. If a change is truly not
  user-visible, the plan must include `# bdd-exempt: <reason>`.
- User stories and scenarios should be written imperatively: describe what the
  system must do, not what it might do.
- Gherkin scenarios are more important than code snippets in plans. Avoid code
  blocks unless they document a specific API shape, critical validation,
  performance constraint, or monitoring decision.
- `/execute-feature` must drive a true red BDD failure before production code:
  undefined steps, syntax errors, missing imports, and environment errors are
  not acceptable red states.

## Testing Strategy

- BDD validates functional behavior and externally observable outcomes.
- Pytest unit tests validate critical internal business logic, edge cases, and
  failure handling.
- Keep tests close to the behavior they protect. Prefer handler/service tests
  for business rules, DAO tests for persistence boundaries, and BDD for API and
  end-to-end functional contracts.
- Use the Makefile targets documented in `tests/README.md`.
- Do not require live BestTime, Google Places, Apify, S3, OpenAI, or external
  network calls in BDD. Use deterministic fakes.

## Architecture Guardrails

- FastAPI routers define HTTP contracts, handlers shape request behavior,
  services own business logic and refresh/enrichment orchestration, DAOs own
  Redis persistence, and models define request/response/data boundaries.
- Preserve Redis key compatibility unless a plan explicitly defines a migration.
- Preserve BestTime day-index and Recife timezone behavior unless a plan proves
  the current behavior is wrong.
- Keep enrichment paths optional and dependency-aware. Missing optional API keys
  should disable optional enrichment paths without breaking core venue serving.
- Do not bind new business logic directly to low-level Redis calls when an
  existing DAO/model boundary is available.

## Reliability And Observability

- New runtime paths need intentional error handling. Degrade gracefully when a
  dependency is optional; fail clearly when the request cannot be served.
- Background jobs must log failures with enough context to troubleshoot and must
  not fail silently.
- Add or update Prometheus metrics for new endpoints, external calls,
  background jobs, and critical service paths when they affect production
  behavior.
- Logs should include request or operation context, but never tokens, API keys,
  credentials, or user PII.

## Security

- Validate HTTP inputs through FastAPI/Pydantic boundaries.
- Treat API keys, Redis credentials, S3 credentials, and OpenAI keys as
  sensitive.
- Do not expose or weaken debug/admin-style paths without explicit approval.
- Do not log raw external API payloads if they can include secrets or sensitive
  user/location data.

## Local Sources Of Truth

- Setup, run commands, endpoints, and configuration: `README.md`
- Deployment details: `DEPLOYMENT.md`
- Test commands and integration prerequisites: `tests/README.md`
- App wiring, lifecycle, and background jobs: `main.py`
- Settings and config loading: `app/config.py`
- Dependency wiring: `app/container.py`
- Public venue API: `app/routers/venue_router.py`
- Venue response behavior: `app/handlers/venue_handler.py`
- Redis persistence and key formats: `app/dao/redis_venue_dao.py`
- Refresh business logic: `app/services/venues_refresher_service.py`
- Metrics definitions: `app/metrics.py`
