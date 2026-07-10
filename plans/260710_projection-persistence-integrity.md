# Projection And Persistence Integrity Hardening

## Branch
fix/projection-persistence-integrity

## Goal
The Redis projector must survive any single bad venue/enrichment row and must
propagate RDS deletions to the shared Redis projection; venue removal must
cover every per-venue key family; the engagement write path must be idempotent
under its own mandated retry contract and must refuse to run with an unset
pseudonymization key.

## Non-goals
- Bulk-read performance of the projector (in flight on
  `feature/projector-and-serving-bulk-reads`; this plan rebases on it after it
  merges and applies the same isolation/deletion semantics to the bulk shape).
- The verbose-mode live-freshness gate in `venue_handler` (deferred: verbose
  is not read by product consumers; tracked in wrapper
  `plans/260710_bug-assessment.md` cs-server M1).
- Favorites disaster-recovery/rehydration design (open product decision,
  wrapper bug assessment cross-repo 1.5).
- Enrichment, budget, and scheduler-lock fixes (companion plan
  `260710_enrichment-and-budget-hardening.md`).

## Evidence
Verified in code (wrapper `plans/260710_bug-assessment.md` cs-server H1, M1,
L2, M5, 3.2 — all CONFIRMED except 3.2 PLAUSIBLE):

- `app/services/redis_projection_service.py:102-133`: the per-venue
  try/except covers only `venue_from_row` + `upsert_venue`; the
  `rds_store.get_venue` read, the `_REBUILD_MODELS` enrichment loop (with
  `model_cls.model_validate(rec["payload"])`), `_project_photos`, the weekly
  loop, and the live read/write run outside it. One payload that fails
  Pydantic validation aborts the entire run — including the reconcile/removal
  pass at `:141-151` — every cycle, deterministically.
- Same file `:112-117` and `:128-133`: soft-deleted enrichment rows and absent
  live rows are skipped, never deleted from Redis.
  `app/dao/venue_repository.py:147-149` deletes live forecasts in RDS only;
  `app/dao/redis_venue_dao.py:277-285` writes `live_forecast_v1:{id}` with no
  TTL. Traced consequence: a temporarily-closed venue's deleted RDS live row
  leaves the Redis live key serving indefinitely; an invalidated Instagram
  handle keeps serving up to its 30-day TTL; no-TTL enrichment keys
  (vibe_attributes, reviews, menu, vibe_profile, opening_hours) linger forever
  after RDS soft-deletion.
- `app/dao/redis_venue_dao.py:151-217`: `delete_venue` removes ~10 associated
  key families but not `VENUE_IG_POSTS_KEY_FORMAT` or
  `VENUE_PHOTOS_FRESH_KEY_FORMAT`.
- `app/routers/engagement_router.py:71-80` mandates vibes_bot retry on 5xx
  and documents the write-through as idempotent, but
  `app/services/engagement_service.py:50-56` commits the RDS insert before
  the Redis projection, and `app/dao/rds_venue_store.py:402-406`
  (`add_hot_like_event`) is an unconditioned INSERT — a Redis blip during one
  tap yields up to 3 event rows. (`record_app_session` at `:409-414` already
  models the dedupe.) Serving counts are unaffected (`SADD` is idempotent).
- `app/config.py:106` + `app/services/engagement_service.py:28-31`: the HMAC
  pseudonymization accepts an empty key (`b""`) silently; compose defaults the
  env var to empty. Setting or rotating the key later orphans every existing
  RDS engagement row with no migration path.

## Current Behavior
One invalid enrichment payload kills every projection cycle at the same venue,
so later venues never re-project and deprecated venues are never removed. RDS
deletions of live/enrichment data for still-servable venues never reach Redis.
`delete_venue` leaves IG-posts and fresh-photos blobs behind. Retried hot-like
writes duplicate RDS event rows. Engagement writes run silently with an empty
pseudonymization key.

## Desired Behavior
- The projector must isolate each venue: any exception while reading or
  projecting one venue's row, enrichment, photos, weekly, or live data
  increments the error summary/metric, logs the venue id and stage, and
  continues; the reconcile/removal pass must run even when venues errored.
- For each servable venue, when an enrichment record is absent or
  soft-deleted in RDS, the projector must delete the corresponding Redis key
  (per `_REBUILD_MODELS` setter family, plus weekly day keys and the live
  forecast key). Redis converges to RDS in one cycle, both directions.
- `delete_venue` must also delete the venue's IG-posts and fresh-photos keys.
- `add_hot_like_event` must be idempotent per user/venue/business-period:
  a retried write must not create a second RDS row (unique constraint +
  ON CONFLICT DO NOTHING, Alembic migration; same pattern as
  `record_app_session`).
- When the engagement pseudonymization key is unset/empty and engagement
  persistence is enabled, startup must fail loudly (or engagement writes must
  be refused with a clear error) rather than silently HMAC-ing with `b""`.

## Implementation Approach
- `redis_projection_service.rebuild_redis_from_rds`: widen the per-venue
  try/except to cover the venue read and all per-entity projection stages;
  add an else-branch per entity: absent-or-deleted RDS record → call the
  matching redis-only delete (extend `_REBUILD_MODELS` rows with their delete
  method names; add per-day weekly delete and live delete). Keep the existing
  "orphans untouched / failed listing skips removal" reconcile semantics.
- `redis_venue_dao.delete_venue`: add the two missing key deletes.
- Alembic migration: unique index on
  `engagement.hot_like_event (user_pseudo, venue_id, business_period)` —
  confirm the exact period column/derivation from the current schema before
  writing the migration; insert switches to ON CONFLICT DO NOTHING.
- `engagement_service` (or config validation at container wiring): refuse
  empty pseudonymization key when engagement writes are enabled.

## Data, Config, And API Impact
- Redis: deletions now propagate — key *presence* changes for stale data, key
  formats unchanged. This is the documented intent of the projection
  invariant, not a migration.
- RDS: one Alembic migration adding a unique index to
  `engagement.hot_like_event`. Pre-migration duplicate rows must be collapsed
  in the migration (keep-first) so the index can build.
- Config: startup guard on `ENGAGEMENT_PSEUDONYMIZATION_KEY`.
  **Precondition:** verify the key currently deployed on EC2 prod is
  non-empty (AWS SSO session) before merging — if prod runs empty today, the
  guard would block deploy and key-setting is an identity migration that
  needs its own decision.
- API: none.

## Error Handling And Observability
- Per-venue projector failures: keep `summary["errors"]`; add a stage label
  to the existing warning log; add counter
  `redis_projection_entity_deletes_total{entity}` for propagated deletions so
  a deletion storm is visible.
- Duplicate hot-like conflicts: increment an
  `engagement_hot_like_dedup_total` counter on conflict-suppressed inserts.
- Startup key guard: a single clear ERROR log naming the env var.

## Test Plan
Feature file: `tests/bdd/persistence/projection-persistence-integrity.feature`

Scenarios:
- A corrupt enrichment payload isolates to its venue: with venue A carrying an
  invalid vibe-profile payload and venue B valid, a projection run projects B
  fully, reports one error, and still executes the removal pass.
- An RDS-deleted live forecast disappears from Redis on the next cycle.
- A soft-deleted enrichment row deletes its Redis key on the next cycle while
  the venue's other keys remain.
- Venue deletion removes IG-posts and fresh-photos keys along with the rest.
- A retried hot-like write persists exactly one RDS event row and returns
  success.
- With an empty pseudonymization key and engagement enabled, the service
  refuses to start (or refuses engagement writes) with a clear error.

Pytest unit tests:
- Projector: per-stage exception paths (venue read, enrichment validate,
  photos, weekly, live) each isolate and count; delete propagation called per
  entity family including weekly day keys and live.
- DAO: `delete_venue` key coverage (all families enumerated).
- Engagement: ON CONFLICT insert path; empty-key guard.

Manual or integration checks:
- AWS SSO (before merge): read the deployed `ENGAGEMENT_PSEUDONYMIZATION_KEY`
  on the EC2 host (compose env) — must be non-empty.
- After deploy: watch `redis_projection_entity_deletes_total` for an initial
  convergence burst, then steady near-zero; confirm projection cycle duration
  and error counts in Grafana.

## Acceptance Criteria
- A projection run with one poisoned venue completes, projects all other
  venues, and executes the removal pass.
- Deleting an RDS enrichment/live row removes the matching Redis key within
  one cycle; deleting a venue removes every per-venue key family.
- Replaying a hot-like write yields one RDS row and a success response.
- Startup (or engagement writes) fail loudly on an empty pseudonymization key.
- Alembic migration applies cleanly on a copy of prod schema with existing
  duplicate hot-like rows.

## Open Questions
- None blocking plan approval, but execution must not start the migration
  work before the AWS SSO prod check on the pseudonymization key (see
  preconditions above) and must rebase after
  `feature/projector-and-serving-bulk-reads` merges.
