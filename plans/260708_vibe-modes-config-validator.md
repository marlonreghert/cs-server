# Vibe Modes Config Validator

## Branch
fix/vibe-modes-config-validator

## Goal
`PUT /admin/config/vibe_modes` must reject malformed vibe-mode payloads before
any write, so the RDS truth and the Redis mirror can never hold a mode that the
serving stack (vibes_bot `/config/vibe-modes` → mobile `VibeModeConfig`) is not
guaranteed to handle. The currently stored payload (mode `role_calmo` missing
`filter.quality_gates`) must be corrected in production after the validator
ships.

## Non-goals
- No change to vibes_bot serving or its mode-eligibility evaluator (it already
  reads defensively; this hardens the write side, matching the established
  "cs-server owns write-time validation" split used for `force_update`).
- No change to the generic `/admin/config/{key}` endpoint contract for other
  keys.
- No admin UI work (vibesadmin already renders `quality_gates` with a `|| []`
  guard).
- No RDS schema or Redis key migration.

## Evidence
- Prod RCA 2026-07-08: 7× `PUT /admin/config/vibe_modes` on 2026-07-07 evening
  stored `role_calmo` with no `filter.quality_gates` — the only mode missing it.
  Served today by vibes_bot `GET /config/vibe-modes`.
- `app/routers/admin_trigger_router.py` — `put_admin_config` accepts
  `Union[dict, list]` and delegates per-key validation to `AdminConfigService`.
- `app/services/admin_config_service.py` — `AdminConfigService.set()` runs
  `self.validators.get(key)` BEFORE the RDS write and Redis mirror; the
  validator's return value is what gets persisted.
- `app/container.py:346-359` — validators registered today:
  `venue_eligibility`, `force_update` (`app/services/force_update.py`
  `validate_force_update_config` is the pattern to mirror).
- `tests/bdd/api/force-update-config-validator.feature` — precedent scenarios.
- Reader contract: vibes_bot `app/services/vibe_modes_service.py` (defaults) and
  `vibe_modes_evaluator.py`; mobile `src/api/vibeModes/types.ts`
  (`VibeModeConfig`, `ModeFilter` — `quality_gates: QualityGate[]` required).
  Mobile `VibeModeConfigProvider` renders `fetched.filter((m) => m.enabled)` and
  falls back to `modes[0]` — an all-disabled or empty list breaks mode selection.

## Current Behavior
`PUT /admin/config/vibe_modes` accepts any JSON object or array. A mode missing
required keys (e.g. `filter.quality_gates`), duplicate mode ids, an empty
array, or an all-disabled list is stored verbatim in RDS and mirrored to Redis,
and is then served to every client.

## Desired Behavior
- The endpoint must reject with HTTP 400 (naming the offending mode id and
  field) any `vibe_modes` payload that is not a non-empty JSON array of mode
  objects, each with: `id` (unique non-empty string), `label`, `emoji`,
  `description` (strings), `is_default`, `enabled` (booleans),
  `busyness_range` (`[min, max]` integers, `0 ≤ min ≤ max ≤ 4`),
  `sort_strategy` (one of `combined_score_desc`, `busyness_desc`,
  `rating_desc`), `affinity` (object of string → number), and `filter` (object).
- `filter` must contain: `allowed_types`, `always_pass_types`,
  `excluded_granular_types` (arrays of strings), `quality_gates` (array of
  objects with `types` array of strings, numeric `min_rating`, integer
  `min_reviews`), `requires_open_late` (boolean), `vibe_label_matchers` (array
  of objects with non-empty string `category` and `labels` array of strings).
- Unknown extra keys (e.g. `requires_family_signal`, `trajectory_weight`) must
  be preserved verbatim — forward-compatible, readers ignore extras.
- The list must contain at least one `enabled: true` mode and at most one
  `is_default: true` mode.
- On rejection, neither RDS nor the Redis mirror changes (guaranteed by the
  existing validate-before-write order in `AdminConfigService.set`).

## Implementation Approach
Add `validate_vibe_modes_config(value)` in a new
`app/services/vibe_modes_config.py` (same module shape as
`app/services/force_update.py`): pure function, raises `ValueError` with a
message naming the mode id (or index) and the failing field, returns the value
to persist unchanged when valid. Register it in `app/container.py` under
`validators={"vibe_modes": ...}`. No handler changes —
`put_admin_config` already maps `ValueError` to HTTP 400.

## Data, Config, And API Impact
- API: `PUT /admin/config/vibe_modes` starts returning 400 for malformed
  payloads (previously 200). GET/DELETE unchanged. Other keys unchanged.
- Data: none at deploy time. One-time production remediation AFTER deploy
  (operator-gated, not part of execution): `GET /admin/config/vibe_modes`, add
  `"quality_gates": []` to `role_calmo.filter`, `PUT` the corrected array back —
  the new validator must accept it.
- Config/flags: none.

## Error Handling And Observability
- Rejections surface as HTTP 400 with the offending mode id and field in
  `detail`; the existing endpoint logging covers writes.
- No new metrics: `http_requests_total{endpoint="/admin/config/vibe_modes",
  status_code="400"}` already exists and distinguishes rejects.

## Test Plan
Feature file: `tests/bdd/api/vibe-modes-config-validator.feature`

Scenarios:
- Well-formed modes array is accepted, persisted, and round-trips via GET.
- Mode missing `filter.quality_gates` is rejected with 400 naming the mode and
  field; stored config is unchanged.
- Mode missing a required top-level field (`busyness_range`) is rejected.
- Unknown extra keys are preserved on an otherwise valid payload.
- Top-level JSON object (not array) is rejected for `vibe_modes`.
- Empty array is rejected.
- Duplicate mode ids are rejected.
- All-disabled list is rejected; two `is_default` modes are rejected.

Pytest unit tests:
- `tests/test_vibe_modes_config.py` — one test per validation rule plus the
  extra-keys-preserved and valid-round-trip cases (mirrors
  `validate_force_update_config` coverage).

Manual or integration checks:
- Post-deploy remediation PUT of the corrected production payload (documented
  above; operator-gated).

## Acceptance Criteria
- A payload equal to today's production value is rejected solely because of
  `role_calmo`'s missing `quality_gates`, with a 400 naming both.
- The same payload with `"quality_gates": []` added is accepted and served
  unchanged by vibes_bot `GET /config/vibe-modes`.
- All existing admin-config keys keep their current behavior.

## Open Questions
- None.
