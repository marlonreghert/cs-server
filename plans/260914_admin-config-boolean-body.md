# Admin Config Boolean Body

## Branch
fix/admin-config-boolean-body

## Goal
`PUT /admin/config/{key}` must accept a bare JSON boolean body (`true` /
`false`) and hand the key's validator a native Python `bool`, for every
boolean-typed admin-config key — while continuing to accept top-level JSON
object bodies for dict-typed keys and top-level JSON array bodies for
list-typed keys exactly as it does today.

## Non-goals
- No change to what any specific flag DOES: `event_dedup_handle_time_match_enabled`,
  `venue_link_audit_enabled`, `event_venue_advisor_enabled`, or any other
  admin-config key's downstream behavior/business logic.
- No new admin-config keys.
- No change to any config key's default value.
- No change to the `{"value": true}` wrapped-object shape's outcome — it stays
  rejected for boolean keys. Only the bare-boolean shape the runbook documents
  is being fixed; wrapping a boolean in an object is not a documented or
  supported call shape for this route.
- No change to `GET`/`DELETE /admin/config/{key}`, or to any other admin
  router endpoint (`/config/geofence`, `/venues/eligibility-config`,
  `/venues/category-map`, etc.) — their body types are untouched.

## Evidence
- `app/routers/admin_trigger_router.py:1317-1318` — the actual route today:
  `async def put_admin_config(key: str, value: Union[dict, list] = Body(...))`.
- `app/routers/admin_trigger_router.py:1327-1335` — the handler forwards
  `value` straight to `svc.set(key, value, updated_by="admin")`; there is no
  `.get("value")` unwrapping anywhere in the function. `value` is a single
  `Body(...)` parameter without `embed=True`, so FastAPI/Pydantic parses the
  ENTIRE raw JSON request body into `value` — a body of `{"value": true}`
  becomes `value == {"value": True}` (a dict), not an unwrapped bool.
- `app/services/admin_config_service.py:41-53` — `AdminConfigService.set()`
  calls `validator(value)` (if a validator is registered for the key) before
  any RDS/Redis write; an invalid value never reaches persistence.
- Three real boolean validators confirmed by reading the code, each doing
  exactly `if not isinstance(value, bool): raise TypeError(...)`:
  `app/services/event_dedup.py:245-248` (`validate_handle_time_match_enabled_config`),
  `app/services/venue_link_audit.py:109-112` (`validate_venue_link_audit_enabled_config`),
  `app/services/event_venue_advisor.py:80-83` (`validate_event_venue_advisor_enabled_config`).
- Repo-wide grep for `isinstance(value, bool)` in an admin-config validator
  finds at least 9 boolean-typed keys, all subject to the same bug:
  `venue_link_audit_enabled`, `event_attribution_dispute_withhold_enabled`,
  `event_display_title_enabled`, `event_dedup_auto_merge_enabled`,
  `event_dedup_recurring_window_enabled`, `event_dedup_handle_time_match_enabled`,
  `event_dedup_single_night_default_enabled`, `event_venue_advisor_enabled`,
  `hide_promoter_events`.
- A confirmed dict-typed validator for the regression guard:
  `app/models/venue_category.py:373-388` (`validate_category_map_config`,
  key `venue_category_map`) — `if not isinstance(value, dict): raise
  TypeError(...)`.
- A confirmed list-typed validator/key for the regression guard:
  `app/services/vibe_modes_config.py:238` (`validate_vibe_modes_config`, key
  `vibe_modes`); already has pytest coverage
  (`tests/test_admin_config.py:127-136`,
  `test_put_accepts_top_level_list_value`) that must stay green.
- `docs/events-venue-night-repair-runbook.md:166` documents the exact call
  this bug breaks: `PUT /admin/config/event_dedup_single_night_default_enabled
  body: true` — a bare boolean body.
- `app/container.py:651-772` registers every validator (booleans included) on
  the production `AdminConfigService`.
- `tests/bdd/environment.py:383-430` mirrors the same validators dict for BDD
  scenarios, and `event_dedup_handle_time_match_enabled` is already registered
  there (line 420) — usable directly for the new BDD scenario with no harness
  change.
- `tests/test_admin_config.py` — existing pytest coverage
  (`test_put_get_delete_list_endpoints`, `test_put_accepts_top_level_list_value`,
  `test_put_invalid_eligibility_returns_400`, geofence/eligibility tests); no
  existing test PUTs a boolean-typed key.
- `tests/bdd/api/vibe-modes-config-validator.feature` +
  `tests/bdd/steps/vibe_modes_config_validator_steps.py` — the precedent
  pattern for a validator-focused BDD feature in this domain, and for
  `context.client.put(url, json=payload)` driving the real router + real
  `AdminConfigService` + fakeredis + in-memory RDS stack end to end.
- `requirements.txt` pins `fastapi==0.115.0`, `pydantic==2.9.2` — Pydantic v2
  smart-mode union validation, which discriminates JSON `true`/`false` (bool)
  from a JSON object (dict) and a JSON array (list) unambiguously by JSON
  type, so adding a `bool` arm to the `Union` should not perturb how existing
  dict/list bodies resolve. Flagged as an open question below for a live
  confirmation during execute-feature rather than asserted from memory.

## Current Behavior
- `PUT /admin/config/{key}` with a bare JSON boolean body (`true`) fails
  Pydantic validation against `Union[dict, list]` before `put_admin_config`
  ever runs: **422**, `"Input should be a valid dictionary"` /
  `"...valid list"`.
- `PUT /admin/config/{key}` with body `{"value": true}` passes the
  `Union[dict, list]` gate (it IS a dict) and reaches the handler as
  `value == {"value": True}`. The handler passes this dict straight to
  `svc.set(key, value)`. For a boolean-typed key, the validator's
  `isinstance(value, bool)` check sees a dict and raises `TypeError` →
  **400** `invalid config for <key>: ...`. Nothing is persisted (validation
  runs before any write).
- Net effect: there is currently NO way to set any boolean-typed admin-config
  key through this endpoint, even though the repo's own runbook documents
  doing exactly that.
- Object-typed and array-typed keys (e.g. `venue_category_map`, `vibe_modes`)
  work correctly today — this bug is specific to the missing `bool` arm.

## Desired Behavior
`PUT /admin/config/{key}` accepts a bare JSON boolean body in addition to the
existing JSON object and JSON array bodies. For a boolean-typed key, the
validator receives a native Python `bool` and the write succeeds (200) with
`{"key": key, "value": true|false}`. Object-typed and array-typed keys behave
byte-identically to today — same request shapes accepted, same 200/400/502
status mapping.

## Implementation Approach
Widen the route's body-type annotation in
`app/routers/admin_trigger_router.py`, `put_admin_config`, from
`value: Union[dict, list] = Body(...)` to
`value: Union[bool, dict, list] = Body(...)`.

No other change to the handler body is needed: it already forwards `value`
untouched to `svc.set(key, value, updated_by="admin")` with no unwrapping
logic, so once Pydantic accepts a top-level `true`/`false`, `value` is already
the exact Python `bool` every boolean validator expects — this is a one-line
type-annotation widen, not a handler rewrite. Confirm at execute time (see
Open Questions) that Pydantic v2.9.2's union resolution for this exact
`Body(...)` parameter picks the `bool` arm cleanly for a JSON boolean without
a surprising coercion into the `dict`/`list` arms (it must not — a JSON `true`
cannot parse as either) and, symmetrically, that an object/array body still
resolves to `dict`/`list` and not to `bool` (also should not — Pydantic does
not implicitly coerce a dict/list to bool).

No changes to `AdminConfigService.set()`, to any validator, to
`app/container.py`'s or `tests/bdd/environment.py`'s validator registrations,
or to any other route.

## Data, Config, And API Impact
- `PUT /admin/config/{key}` request-body OpenAPI/Swagger schema widens from
  `dict|list` to `bool|dict|list`. No snapshot test pins the old schema
  (confirmed by repo grep) — no test needs updating for the schema shape
  itself.
- No RDS schema change: `admin.admin_config` already stores whatever the
  validator returns via `jsonb`, and a bare Python `bool` is a valid `jsonb`
  scalar. No Redis key-format change: the mirror already `json.dumps`s
  whatever value is stored, and `json.dumps(True)` → `"true"`, which the
  existing `get()` `json.loads` already round-trips correctly (booleans have
  always been a legal *value* in this system — only the HTTP *write* path was
  blocked).
- No change to `GET`/`DELETE /admin/config/{key}`.
- No default-value changes for any key.

## Error Handling And Observability
No new runtime paths. The existing exception mapping in `put_admin_config`
(`ValueError`/`TypeError` → 400 "invalid config for {key}", any other
exception → 502 "config write failed; retry") is unchanged and continues to
apply: a bare-boolean body sent for a non-boolean-typed key still 400s via
that key's own validator raising `TypeError` on `isinstance(value, dict)` (or
`list`) being false. No new metrics or logs are needed — this is a request-
parsing fix to an existing, already-instrumented code path, not a new
behavior.

## Test Plan
Feature file: `tests/bdd/api/admin-config-boolean-body.feature`

Scenarios:
- A bare `true` body sets a boolean-typed key
  (`event_dedup_handle_time_match_enabled`, already registered in
  `tests/bdd/environment.py`) and `GET /admin/config/{key}` returns `true`.
- A bare `false` body sets the same boolean-typed key and round-trips as
  `false`.
- An existing dict-typed key (`venue_category_map`, a well-formed category
  map object) PUT still succeeds unchanged — regression guard proving the
  fix does not disturb the object-body path.
- An existing list-typed key (`vibe_modes`, a well-formed modes array) PUT
  still succeeds unchanged — regression guard proving the fix does not
  disturb the array-body path, over the real HTTP route (the current pytest
  coverage of this is service/router-level only, not a BDD scenario).

Pytest unit tests (`tests/test_admin_config.py`):
- Red state first (written and run BEFORE any production-code change, per
  this repo's BDD-first / red-green discipline): assert today's actual
  broken behavior as a genuine failing-when-fixed pair —
  `client.put("/admin/config/event_dedup_handle_time_match_enabled",
  json=True)` returns 422 today, and
  `client.put(..., json={"value": True})` returns 400 today. These
  assertions must be flipped to the fixed expectations (200, not 422/400) as
  part of the same change that lands the fix, so the suite ends up encoding
  the CORRECT behavior, not the bug — never left as permanent "prove the bug
  exists" assertions.
- Post-fix: `client.put("/admin/config/<bool-key>", json=True)` → 200 and
  `resp.json()["value"] is True`; same for `json=False` → 200 and `... is
  False`.
- A non-boolean-typed key (`venue_category_map`) PUT with a bare boolean body
  still fails (400, via the dict validator's own `TypeError`) — proves the
  widened Union does not let a boolean silently satisfy a dict/list-typed
  key's validator.
- Existing `test_put_accepts_top_level_list_value` and the dict-shaped PUT
  tests continue to pass unmodified.

Manual or integration checks: None — pure local pytest/BDD against fakeredis
+ the in-memory RDS fake; no prod, Redis, or external-API contact required or
permitted for this change.

## Acceptance Criteria
- `PUT /admin/config/{key}` with a bare JSON `true`/`false` body succeeds
  (200) for every boolean-typed admin-config key, and the stored and
  returned value is the Python `bool` (not a wrapped dict, not a string).
- `PUT /admin/config/{key}` with a JSON object body still succeeds unchanged
  for object-typed keys, and with a JSON array body still succeeds unchanged
  for array-typed keys — byte-identical to today's behavior.
- The runbook's documented call
  (`PUT /admin/config/event_dedup_single_night_default_enabled body: true`)
  succeeds against the real route.
- All BDD scenarios in `tests/bdd/api/admin-config-boolean-body.feature` pass
  with the `@wip` tag removed; the new and updated pytest assertions in
  `tests/test_admin_config.py` pass; the full existing admin-config test
  suite (BDD + pytest) stays green.

## Open Questions
- None blocking. One item to confirm mechanically during execute-feature
  rather than asserted here from memory: that Pydantic v2.9.2's smart-mode
  union resolution for `Union[bool, dict, list]` on this exact `Body(...)`
  parameter picks the `bool` arm for a JSON `true`/`false` body and the
  `dict`/`list` arms for object/array bodies, with no cross-coercion in
  either direction. This is expected behavior (the JSON types are mutually
  exclusive) and is exactly what the new red/green pytest pair in the Test
  Plan proves either way — no design decision is gated on it.
