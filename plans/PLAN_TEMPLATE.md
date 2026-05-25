# <Feature Title>

## Branch
<prefix>/<slug>

## Goal
State the behavior this change must deliver.

## Non-goals
List adjacent work that must stay out of scope.

## Evidence
Reference current code, tests, configs, metrics, or docs that informed the plan.

## Current Behavior
Describe what the system does today.

## Desired Behavior
Describe the target behavior in imperative terms.

## Implementation Approach
Describe the subsystem-level changes. Avoid code blocks unless a specific API,
validation rule, monitoring decision, or performance-sensitive design must be
preserved exactly.

## Data, Config, And API Impact
List any request/response, persistence, config, feature-flag, or migration
impact. Write "None" if there is no impact.

## Error Handling And Observability
State how failures will be handled and what logs or Prometheus metrics must be
added or updated. Write "No new runtime path" only when that is true.

## Test Plan
Feature file: `tests/bdd/<domain>/<slug>.feature`

Scenarios:
- <Scenario name and what it validates>

Pytest unit tests:
- <Test file or target behavior for critical internal logic>

Manual or integration checks:
- <Any required app/Redis/external-service checks, or "None">

## Acceptance Criteria
- <Observable outcome that must be true before completion>

## Open Questions
- <Questions that must be resolved before `/execute-feature`, or "None">
