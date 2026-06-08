# Eligibility Mirror Rehydration From Rows On Startup

## Branch
feature/eligibility-mirror-rehydration

## Goal
On cs-server startup, rebuild the Redis `admin_config:venue_eligibility` serving
mirror from the `admin.eligibility_rule` rows, so a Redis flush no longer silently
degrades eligibility filtering to the hardcoded defaults until the next admin
write. Make the rows the sole durable truth and stop persisting the now-redundant
derived RDS `admin_config:venue_eligibility` blob.

## Non-goals
- Rehydrating any other `admin_config:*` key (feature_flags, scoring_weights, …).
  Those are vibes_bot-owned and their truth is the RDS admin_config blob, not the
  eligibility rows — out of scope.
- Changing eligibility evaluation/semantics (`evaluate`, the block-list policy,
  the sweep, born-deprecation). Only the mirror's durability changes.
- Any vibes_bot change. Confirmed: vibes_bot has zero references to
  `venue_eligibility` (its own plan `admin_config_via_cs_server_02_06_26.md`
  documents it as cs-server-owned and never PUT from the panel). vibes_bot
  consumes the already-filtered venue list, not the eligibility config.
- Reworking `AdminConfigService` for the other keys.

## Evidence
- Rows are truth + edit surface (Ex2): `app/services/eligibility_rules.py`
  (`EligibilityRuleService`, `_remirror`). `_remirror` reassembles the blob from
  rows and pushes it via `AdminConfigService.set`
  (`app/services/admin_config_service.py:41`), which dual-writes the RDS
  `admin.admin_config` row + the Redis mirror.
- Serving + refresh read ONLY the Redis mirror: `load_eligibility_config`
  (`app/services/venue_eligibility.py:316`) reads `ADMIN_CONFIG_ELIGIBILITY_KEY`
  from Redis with no RDS fallback; on a miss it returns
  `EligibilityConfig.defaults()`. Readers: `app/handlers/venue_handler.py:136`
  (serve-time filter), `app/services/venues_refresher_service.py:209` (refresh
  sweep / born-deprecate), `app/routers/admin_trigger_router.py:343`.
- No startup rehydration of any admin_config key exists today: `startup_essential`
  (`main.py:553`) only builds the container + injects routers. The periodic
  projector (`main.py` job 11, `redis_projection_service.rebuild_redis_from_rds`)
  rebuilds the VENUE serving projection every ~2 min but does not touch the
  eligibility mirror — hence the asymmetry: a flushed venue projection self-heals
  in ~2 min, a flushed eligibility mirror does not.
- All prod eligibility writes flow through `EligibilityRuleService`: single-rule
  add/remove → `_remirror`; full-config PUT (`admin_trigger_router.py:368-375`) →
  `rule_svc.set_full_config` → `_remirror`. The `AdminConfigService.set` and
  direct-Redis branches (`:377-398`) are fallbacks only when the rule service is
  unwired (never in prod).
- Dedicated eligibility GET (`admin_trigger_router.py:341`) already reads rows
  (`effective_config()`). The generic admin GET `/admin/config/{key}`
  (`admin_trigger_router.py:460` → `AdminConfigService.get` → Redis then RDS-row
  fallback) is the only remaining reader of the RDS blob.
- Container wiring: `app/container.py:342-354` builds `AdminConfigService` then
  `EligibilityRuleService(rds_store, admin_config_service)`.

## Current Behavior
- Eligibility rows live in `admin.eligibility_rule`. On every admin write the
  service reassembles a blob and dual-writes it to the RDS `admin_config` row +
  the Redis mirror.
- Serving/refresh read only the Redis mirror; on a miss they fall back to
  hardcoded defaults.
- The Redis mirror is written only on admin writes. There is no rebuild at
  startup or on a schedule. If Redis is flushed (restart/failover/manual), the
  eligibility mirror is gone until the next admin write, and the block-list
  silently reverts to the hardcoded defaults — which may differ from the
  admin-configured rules (e.g. an admin-tuned override is lost).

## Desired Behavior
- On startup, after the container is built and before/while serving, cs-server
  rebuilds the Redis eligibility mirror from the `admin.eligibility_rule` rows.
  After a Redis flush + restart, eligibility filtering reflects the configured
  rules, not the defaults.
- Rehydration is degrade-safe: if RDS is unreachable, it logs a warning and does
  NOT crash startup; serving continues with whatever the mirror/defaults provide.
- The rows are the sole durable truth: eligibility writes (and the rehydration)
  write the Redis mirror directly and no longer persist the redundant RDS
  `admin_config:venue_eligibility` blob; the existing RDS row is removed.
- The dedicated eligibility GET (rows) is unchanged. The generic admin GET for
  this key returns the Redis mirror (no RDS-row fallback for it anymore).

## Implementation Approach
- Add `EligibilityRuleService.rehydrate_mirror()`: read the rows, assemble the
  effective blob, write the Redis mirror (or clear it when no rows remain). This
  is the same projection `_remirror` performs, factored so both share it. Wrap the
  RDS read in defensive handling so an RDS outage at startup logs and returns
  without raising.
- Stop persisting the RDS blob: change `_remirror` (and `rehydrate_mirror`) to
  write the Redis mirror DIRECTLY rather than via `AdminConfigService.set` (which
  also writes the RDS admin_config row). The mirror write reuses the
  `admin_config:venue_eligibility` key + JSON shape the reader expects, so the
  serving read is byte-compatible. Rows are already validated on write, and
  `assemble_eligibility_blob` produces a valid blob by construction, so no
  re-validation is needed on the mirror path. `EligibilityRuleService` gains
  access to the Redis client (via the admin config service or injected) to write
  the key directly.
- Remove the existing RDS `admin_config:venue_eligibility` row (one row, derived)
  so RDS holds only the rows. Mechanism decided in Open Questions (migration vs
  one-time delete-on-rehydrate).
- Hook rehydration into `startup_essential` (`main.py`) after the container is
  built, guarded so a failure logs and continues (never blocks serving).
- Keep the unwired-state fallbacks behavior-compatible: they are not reached in
  prod; document that they are legacy degraded paths.

## Data, Config, And API Impact
- Persistence: the RDS `admin.admin_config` row keyed `venue_eligibility` stops
  being written and is removed. The `admin.eligibility_rule` rows are unchanged
  and become the sole durable source. The Redis `admin_config:venue_eligibility`
  key shape is unchanged (byte-compatible with `load_eligibility_config`).
- API: no request/response change. The dedicated eligibility GET (rows) is
  unchanged. The generic admin GET `/admin/config/venue_eligibility` now reflects
  the Redis mirror only (no RDS-row fallback); documented.
- Config/flags: none.
- Migration: possibly `0008` to delete the existing RDS blob row (see Open
  Questions). Prod alembic head is currently `0007`.

## Error Handling And Observability
- Rehydration must never crash startup: catch RDS/Redis errors, log a WARNING
  with context, and continue. Serving then uses the existing mirror or defaults.
- Log at startup: an INFO line with the rule count rehydrated (or "no override —
  cleared/absent"), and a WARNING on failure.
- Add a Prometheus counter for rehydration outcomes (success/failure) so a silent
  startup degradation is visible. Reuse `app/metrics.py` conventions.
- Never log rule values that could be sensitive (rule values are venue
  types/name keywords — non-secret, but keep logs to counts + status).

## Test Plan
Feature file: `tests/bdd/persistence/eligibility-mirror-rehydration.feature`

Scenarios:
- After eligibility rules are configured and the Redis mirror is then cleared
  (simulating a flush), startup rehydration rebuilds the mirror so a venue
  blocked by a rule is still filtered (not reverted to defaults).
- With configured rules, startup rehydration writes the
  `admin_config:venue_eligibility` mirror whose effective config equals the rows'
  effective config.
- With no eligibility rules, startup rehydration leaves no override mirror and
  filtering uses the hardcoded defaults.
- When RDS is unavailable at startup, rehydration logs a warning and does not
  crash startup; serving continues.
- Eligibility writes (add/remove/full-config) no longer persist an RDS
  `admin_config:venue_eligibility` row — the rows are the sole durable truth, and
  the Redis mirror still reflects the effective config.

Pytest unit tests:
- `EligibilityRuleService.rehydrate_mirror`: rebuilds the mirror from rows;
  clears the mirror when no rows; on an RDS read error returns without raising and
  logs (degrade-safe).
- `_remirror` writes the Redis mirror directly and does NOT write the RDS
  admin_config row; clears the mirror when no rows remain.
- The reassembled mirror is byte/shape-compatible with `load_eligibility_config`
  (round-trip: rows → mirror → `load_eligibility_config` → same effective config).

Manual or integration checks:
- Against a Redis with the eligibility key deleted, start the app (or invoke the
  rehydration path) and confirm the key is repopulated from rows and the public
  eligibility GET matches.

## Acceptance Criteria
- After clearing the Redis eligibility mirror and running startup rehydration,
  `load_eligibility_config` returns the rows' effective config (not defaults).
- Eligibility add/remove/full-config writes leave no RDS
  `admin_config:venue_eligibility` row; the Redis mirror still matches the rows.
- An RDS outage at startup does not crash the app; a warning is logged and a
  rehydration-failure metric increments.
- Offline `make test-unit` + `make test-feature` for the new feature pass; full
  `make test-bdd` stays green.

## Open Questions
- Self-heal scope: rehydrate only at startup (chosen), or also fold the
  rehydration into the periodic projector (`rebuild_redis_from_rds`, ~2 min) so a
  RUNTIME Redis flush (without a restart) also self-heals — matching the venue
  projection's cadence? Startup-only leaves a runtime-flush-without-restart gap.
- RDS blob removal mechanism: a `0008` migration that deletes the existing
  `admin_config` row keyed `venue_eligibility`, or a one-time idempotent
  delete-on-rehydrate in code (no migration). Migration is explicit + alembic-
  tracked; code-delete is simpler but mixes data cleanup into the runtime path.
