# Force-Update Policy Config Validator

## Branch
feature/force-update-config-validator

## Goal
Validate the new `force_update` admin config key at write time so a malformed
per-platform version policy can never be persisted to RDS or mirrored to the
Redis serving key. Persistence itself is already automatic — `admin.admin_config`
is a generic key/value table and `AdminConfigService` write-through handles any
new key — so this change only adds a per-key validator (the same pattern as
`venue_eligibility`) registered in the container.

This is the cs-server slice of the cross-repo **force-update gate**
(coordination plan: wrapper `plans/260617_force-update-gate.md`). cs-server owns
the durable system of record + Redis mirror for the policy; vibes_bot reads the
mirror and makes the serve-time decision; mobile renders.

## Non-goals
- No version-comparison or `update_required`/`update_type` decision logic — that
  lives in vibes_bot's `GET /config`.
- No RDS schema or Alembic migration — `admin.admin_config` is a generic
  key/value store; `force_update` is just a new row.
- No admin-panel UI — the JSON editor for the key lives in vibes_bot's
  `app/admin/static/admin.html`.
- No vibes_bot or mobile changes.

## Evidence
- `app/services/admin_config_service.py` — `AdminConfigService.set()` runs the
  per-key validator BEFORE any write, then writes RDS (truth) and mirrors Redis
  in the same request; `delete()` removes both.
- `app/container.py:332-346` — the `validators` dict wiring; `venue_eligibility`
  precedent (`_validate_eligibility_config` returns the raw body so the persisted
  bytes stay reader-compatible).
- `app/routers/admin_trigger_router.py:462-486` — `PUT /config/{key}` maps
  `ValueError`/`TypeError` → 400, other exceptions → 502; `DELETE` clears both
  stores.
- `app/dao/rds_venue_store.py:361-384` — generic `admin.admin_config`
  upsert/get/delete (no per-key schema), used by `AdminConfigService`.

## Current Behavior
No validator is registered for `force_update`, so any dict body
`PUT /admin/config/force_update` would be stored verbatim in RDS and mirrored to
`admin_config:force_update`. A typo'd version string, an inverted floor, or a
missing `store_url` would be persisted silently and could brick or mis-gate the
app (a hard gate can block every user).

## Desired Behavior
`PUT /admin/config/force_update` validates the body before any write and rejects
(400, nothing persisted) when:
- the top-level keys are not a subset of `{ios, android}`, or the body is empty;
- a platform block is missing `min_supported_version` or
  `min_recommended_version`;
- any version is not valid semver `MAJOR.MINOR.PATCH` (non-negative integers);
- `min_supported_version` > `min_recommended_version` within a platform;
- `store_url` is missing or not `https://`;
- `hard_message`/`soft_message` are present but not strings.

On valid input, persist the raw body (byte-compatible with the reader) to RDS and
mirror it to `admin_config:force_update`, returning `{key, value}`. `DELETE`
clears both stores; readers then fall back to "no policy" (gate off).

## Implementation Approach
- Add `_validate_force_update(value)` in `app/container.py` next to
  `_validate_eligibility_config`, and register it under the `validators` key
  `"force_update"` passed to `AdminConfigService`.
- The validator iterates the present platform blocks, parses each version into an
  `(int, int, int)` tuple via a small local helper, applies the rules above, and
  raises `ValueError` with a precise message on the first violation; it returns
  the original `value` unchanged on success (persist byte-compatible, matching
  the eligibility precedent).
- If the helper proves reusable, factor it into `app/services/`; otherwise keep
  it inline like the eligibility validator. No router, DAO, or model changes —
  the generic admin-config CRUD already covers PUT/GET/DELETE.

## Data, Config, And API Impact
- New admin config key `force_update`: RDS row in `admin.admin_config` + Redis
  mirror `admin_config:force_update`. **No migration** (generic key/value table).
- Stored shape (the cross-repo contract; per configured platform):
  `{ "min_supported_version": "x.y.z", "min_recommended_version": "x.y.z",
  "store_url": "https://…", "hard_message"?: str, "soft_message"?: str }`. An
  absent platform block means "no policy for that platform".
- HTTP surface unchanged: existing generic `PUT/GET/DELETE /admin/config/{key}`.

## Error Handling And Observability
- Validation failure raises `ValueError` → existing 400 path; a Redis-mirror
  failure after the RDS commit raises → existing 502 (caller retries; the RDS
  upsert is idempotent). No new runtime path beyond the validator.
- Reuse existing AdminConfig write logging. Do not log full payloads.

## Test Plan
Feature file: `tests/bdd/api/force-update-config-validator.feature`

Scenarios:
- Valid two-platform policy is accepted, persisted to the system of record, and
  mirrored to the Redis serving key.
- A policy with an invalid version string is rejected, nothing persisted.
- A policy whose supported floor exceeds its recommended floor is rejected.
- A policy missing `store_url` is rejected.
- A policy with a non-https `store_url` is rejected.
- A policy with an unknown platform key (`web`) is rejected.
- Deleting the policy clears the record and the Redis mirror.

Pytest unit tests:
- `_validate_force_update` table-driven: each accept/reject rule (semver parse,
  floor ordering, store_url scheme, unknown platform, message typing).

Manual or integration checks:
- Round-trip via vibes_bot admin proxy (`PUT /api/config/force_update` →
  cs-server) is covered cross-repo by the coordination plan; not required here.

## Acceptance Criteria
- `PUT /admin/config/force_update` with a valid policy returns 200 and the value
  is readable from both the RDS system of record and the Redis mirror.
- Each malformed variant returns 400 and persists nothing (no RDS row, no mirror
  key written/changed).
- `DELETE /admin/config/force_update` removes the RDS row and the Redis mirror.

## Open Questions
- None.
