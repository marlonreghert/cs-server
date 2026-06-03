# Migration flag + scaffolding cleanup (staged)

Status: IN PROGRESS (bucket 1). Owner-approved 2026-06-03 via scoping Q&A.

The RDS-as-system-of-record + Redis-projection-decoupling migration is shipped and
live in prod (step 6 / Pass 1–3 merged + deployed). This plan removes the remaining
**transitional scaffolding** so the codebase reflects the finished architecture
with no migration residue. Done **staged, lowest-blast-radius first**, each bucket
its own PR (Pass 3 proved broad cleanups have non-obvious couplings).

## Decisions (owner)
- **Keep** the plan files under `plans/` (design record) and the `RDS_TEST_URL`
  real-Postgres contract test (only real-PG SQL coverage; not migration cruft).
- **`RDS_ENABLED` is the whole-system kill-switch**, not scaffolding → its own
  **last** bucket, with an explicit go.
- "no traces of the migration local test/blockers" = the `@wip` scenarios/markers
  and the one-time **backfill** jobs (NOT the contract test).

## Buckets (order)
1. **cs-server hygiene (this PR):** remove the one-time backfill tooling; triage
   `@wip`; strip Pass-N / transitional / cutover narrative markers.
2. vibes_bot `ENGAGEMENT_WRITE_THROUGH` — its own PR (DAOs + BDD).
3. vibes_bot `ADMIN_CONFIG_WRITE_THROUGH` — its own PR (admin routes + BDD).
4. `RDS_ENABLED` (cs-server) — LAST, explicit go: hardcode RDS-on, delete the
   `rds_store is None` Redis-only fallback in `VenueRepository`/container + the
   "RDS not enabled" guards. Watch for latent `rds_store can be None` assumptions
   (the Pass-3 shared-harness class of surprise).

## Bucket 1 — scope
`# bdd-exempt: pure removal of one-time migration tooling + WIP/transitional
markers; no new user-visible behavior. Tests/scenarios for the removed code are
deleted with it; the full suite stays green.`

**Remove the one-time backfill (migration import) — KEEP `rebuild_redis` (DR):**
- `app/routers/admin_trigger_router.py`: `backfill_rds` + `admin_config_backfill`
  JOB_REGISTRY entries + `_run_job` branches.
- `app/services/redis_projection_service.py`: `backfill_rds_from_redis()` +
  `_MODEL_PAIRS` (only used by backfill); update the module docstring.
- `app/services/admin_config_service.py`: `backfill_from_redis()`.
- Unit: `test_rds_repository.py::TestProjectionService::test_backfill_*`,
  `test_admin_config.py::test_backfill_*`.
- BDD: the backfill scenarios + steps in `rds_system_of_record.feature` /
  `admin_config_rds.feature`.
- NOT touched: `_backfill_venue_review_signal` (unrelated Google-review backfill),
  the soft-deletion "must not require a backfill migration" assertion.

**`@wip` triage (delete all 5):**
- decoupling "projector reflects RDS" / "enrichment outputs persist+project" /
  "RDS unavailable…" → dups of now-active `rds_system_of_record.feature` scenarios.
- rds_sor "Admin configuration is stored in RDS and mirrored to Redis" → covered
  by the active `admin_config_rds.feature`.
- decoupling "admin venue edit writes RDS only…" → **deferred capability, preserved
  here** (see below), scenario removed.

**Marker hygiene:** strip the "TWO-PASS execution / PASS 2 @wip" NOTE block and the
`# ── PASS 1/2a/2b` section prefixes → plain behavior-based section comments. Keep
`bdd-exempt: infrastructure` markers and substantive why-comments (e.g. the B0
off-loop executor rationale).

## Deferred capability (preserved spec — not yet built)
**Admin venue edit → RDS, surfaced via the projector.** There is no cs-server
admin venue-edit endpoint; editing a venue's fields (e.g. name) is a future
vibes_bot companion that would write RDS-only and become visible in serving after
the next projector run (old value until then). Captured here so removing the `@wip`
scenario doesn't lose the spec; build under its own plan when the endpoint lands.
