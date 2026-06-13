# Restore (reactivate) eligibility-deprecated venues

> **SUPERSEDED (2026-06-13)** by `plans/260613_eligibility-serving-view.md`.
> Eligibility becomes a dynamic gold-layer **SQL view** that the Redis projection
> reads, so blocking/unblocking is reversible by design and no soft-delete is
> involved — making per-venue restore/reactivation unnecessary. This plan is kept
> for history only; do not execute it.

## Branch
feature/venue-restore

## Goal
Add a way to **reactivate a soft-deleted venue** so an eligibility-sweep false
positive is recoverable, instead of requiring a raw RDS edit. Expose
`POST /admin/venues/{venue_id}/reactivate`: it flips the venue back to active in
RDS (the projector re-adds it to the serving projection on its next cycle),
**only for venues the eligibility sweep deprecated** (`deprecated_source =
'eligibility_filter'`), and returns the venue's live eligibility verdict so the
operator knows whether the next sweep would re-deprecate it. Also close an audit
gap: lifecycle soft-delete currently writes no audit row.

## Non-goals
- **No restore for non-eligibility deprecations.** Venues deprecated by the
  Google "permanently closed" path (or any source other than
  `eligibility_filter`) are refused with HTTP 409 — a real-world closure is not a
  false positive. (Decision: refuse, not a force-override.)
- **No single-venue Redis projection.** Re-appearance relies on the existing
  off-loop projector (`REDIS_PROJECTION_MINUTES`, =2 in prod). No new projection
  code path is added.
- **No auto-editing of eligibility rules.** Restore never changes block-lists; it
  only reports whether the venue still matches one. Rule editing stays in the
  admin eligibility surface.
- **No change to the sweep, discovery, or `_preserve_deprecation`.** They already
  behave correctly: the sweep skips deprecated venues; `_preserve_deprecation`
  only guards `upsert_venue` re-adds, never an explicit reactivate.
- vibes_bot proxy + admin UI are a **separate, dependent plan** (this repo only
  ships the endpoint + persistence + audit + metric).

## Evidence
- `app/dao/rds_venue_store.py` — `soft_delete_venue` does
  `UPDATE venues.venue SET lifecycle_status='deprecated', deprecated_reason,
  deprecated_source, deprecated_at=now(), google_business_status=COALESCE(...)`.
  There is **no** method setting `lifecycle_status='active'` (grep: only SELECTs
  read active). It writes **no** `audit.enrichment_history` row (the `_history`
  helper exists and is used by `soft_delete_enrichment`, not by the venue
  lifecycle path).
- `app/dao/venue_repository.py` — wraps the store; `soft_delete_venue` delegates
  to `rds_store.soft_delete_venue`. This is the DAO the admin router resolves to
  via `_get_venue_dao_from_container()` (→ `redis_venue_dao` = the repository).
- `app/services/venues_refresher_service.py` `run_eligibility_sweep` — computes a
  venue's verdict with `evaluate(venue_name, venue_type, google_type, config)`
  (`evaluate_eligibility`), where `google_type = get_vibe_attributes(venue_id)
  .google_primary_type` and `config = self._eligibility_config()`
  (`load_eligibility_config` over the Redis mirror). `result.soft_deletable`
  (ineligible + high confidence) is exactly what the sweep acts on. Vibe
  attributes have their own `deleted_at`, independent of venue lifecycle, so a
  deprecated venue's Google type is still readable → the verdict is recomputable
  here.
- `app/routers/admin_trigger_router.py` — admin endpoints (router prefix
  `/admin`); the just-merged `POST /venues/eligibility-config` is the shape to
  mirror. `_get_venue_dao_from_container()` resolves the repository DAO.
- `app/services/redis_projection_service.py` `rebuild_redis_from_rds` — projects
  `list_active_venue_ids()` and deletes `list_deprecated_venue_ids()` each cycle.
  A venue flipped to active is in the active list and out of the deprecated list,
  so it re-projects automatically next cycle (no extra work needed here).
- `app/metrics.py` — `VENUES_SOFT_DELETED_TOTAL{reason, source}` is the symmetric
  precedent for a reactivation counter; `update_data_quality_metrics` recomputes
  active/deprecated gauges.

## Current Behavior
A venue soft-deleted by the eligibility sweep stays `deprecated` forever: nothing
flips `lifecycle_status` back to active, the sweep skips it, discovery won't
resurrect it, and the projector keeps it out of Redis. The only recovery is a
manual RDS `UPDATE`. Soft-deletes also leave no audit trail.

## Desired Behavior
- `POST /admin/venues/{venue_id}/reactivate`:
  - **404** when no venue row exists for `venue_id`.
  - **200 `{"status":"already_active", ...}`** when the venue is already active
    (idempotent no-op; rowcount-safe against races).
  - **409 `{"status":"refused", "detail": "..."}`** when the venue is deprecated
    but `deprecated_source != 'eligibility_filter'` (e.g. Google permanently
    closed). The venue stays deprecated.
  - **200 `{"status":"reactivated", "venue_id", "previous": {deprecated_reason,
    deprecated_source, deprecated_at}, "eligibility": {eligible, reason,
    confidence, would_be_reswept}}`** on a real flip. `would_be_reswept` =
    `evaluate(...).soft_deletable` against the live config — true means the next
    sweep would re-deprecate it (the operator should fix the rule first); the
    reactivation still succeeds (warn, don't block).
- The flip clears `deprecated_reason`, `deprecated_source`, `deprecated_at` and
  sets `lifecycle_status='active'`, `updated_at=now()`. `google_business_status`
  is left untouched (only eligibility-deprecated venues are restorable, where it
  is not a closure flag).
- The venue reappears in the Redis serving projection on the next projector cycle
  (~2 min); no inline Redis write.
- **Audit (both directions):** reactivation appends an `audit.enrichment_history`
  row `operation='reactivate'`; and `soft_delete_venue` is backfilled to append
  `operation='soft_delete'`, so the full deprecate→reactivate lifecycle is
  auditable. Both rows carry `schema_name='venues'`, `table_name='venue'`, the
  `venue_id`, and a payload capturing the reason/source/status involved.

## Implementation Approach
1. **DAO (`app/dao/rds_venue_store.py`):** add `reactivate_venue(venue_id) ->
   bool`. In one transaction: `UPDATE venues.venue SET lifecycle_status='active',
   deprecated_reason=NULL, deprecated_source=NULL, deprecated_at=NULL,
   updated_at=now() WHERE venue_id=:v AND lifecycle_status='deprecated'`; if a row
   changed, append the `operation='reactivate'` history row (capturing the prior
   reason/source via a pre-read or `RETURNING`). Return `rowcount > 0`. Backfill
   `soft_delete_venue` to append an `operation='soft_delete'` history row in its
   existing transaction. Reuse the `_history` helper.
2. **Repository (`app/dao/venue_repository.py`):** add `reactivate_venue(venue_id)
   -> bool` delegating to the store (mirrors `soft_delete_venue`).
3. **Router (`app/routers/admin_trigger_router.py`):** add
   `POST /venues/{venue_id}/reactivate`. Resolve the repository DAO; `get_venue`
   → 404/already_active/409-by-source branching as above; on the restorable case
   call `reactivate_venue`, recompute the verdict with the sweep's
   `evaluate`+`EligibilityConfig` path (`google_primary_type` from
   `get_vibe_attributes`, config from `load_eligibility_config`), increment the
   metric, and return the body. Keep the handler thin; reuse existing helpers.
4. **Metric (`app/metrics.py`):** `VENUES_REACTIVATED_TOTAL{source, result}`
   where `result ∈ {reactivated, already_active, refused_source, not_found}` and
   `source` is the prior `deprecated_source` (or `none`). Recompute the
   active/deprecated gauges after a successful flip (call
   `update_data_quality_metrics` or the equivalent). Log at INFO with venue_id,
   prior source/reason, and `would_be_reswept`.

## Data, Config, And API Impact
- **New endpoint:** `POST /admin/venues/{venue_id}/reactivate`. No request body.
- **RDS:** no schema change. `venues.venue` lifecycle columns are flipped/cleared
  on reactivate; a new `audit.enrichment_history` row per reactivate **and** per
  soft-delete (operation values `reactivate` / `soft_delete`).
- **Redis:** no direct write; the venue re-projects on the next projector cycle.
- **Config:** none. No new env/flag.

## Error Handling And Observability
- 404 (missing), 409 (non-eligibility source — venue unchanged), 200
  (reactivated or already_active). The DAO `WHERE lifecycle_status='deprecated'`
  guard + rowcount make concurrent double-restores a safe no-op.
- `VENUES_REACTIVATED_TOTAL{source, result}` covers every outcome; INFO log with
  context. Audit rows give a durable trail. No secrets/PII logged.

## Test Plan
Feature file: `tests/bdd/api/venue-restore.feature`

Scenarios:
- Reactivate an eligibility-deprecated venue → `lifecycle_status` becomes active,
  `deprecated_*` cleared, response `status=reactivated` with the eligibility
  verdict; an `operation='reactivate'` audit row is written.
- The verdict warns when still blocked: a restored venue whose `google_primary_type`
  still matches a block rule returns `would_be_reswept=true` with the predicted
  reason; one that no longer matches returns `would_be_reswept=false`.
- Refuse a non-eligibility deprecation: a Google-permanently-closed venue
  (`deprecated_source != 'eligibility_filter'`) returns 409 and stays deprecated.
- Idempotent: reactivating an already-active venue returns 200 `already_active`
  and writes no audit/flip.
- Missing venue returns 404.
- Soft-delete now writes an `operation='soft_delete'` audit row (the backfill).
- After reactivation the venue is in `list_active_venue_ids()` and absent from
  `list_deprecated_venue_ids()` (so the projector re-adds it).

Pytest unit tests:
- `RdsVenueStore.reactivate_venue`: SQL effect (flip + clear), rowcount-driven
  idempotency, history row payload; `soft_delete_venue` history backfill.
- Router handler branches: 404 / already_active(200) / refused_source(409) /
  reactivated(200); verdict computation (`would_be_reswept` from
  `soft_deletable`); `VENUES_REACTIVATED_TOTAL` labels per outcome.

Manual or integration checks:
- Against a non-prod DB: deprecate a test venue via the sweep, reactivate it,
  confirm RDS flip + audit rows, and that it reappears in Redis after the next
  projection (~2 min). Do not run against prod from verification.

## Acceptance Criteria
- An eligibility-deprecated venue can be reactivated via the endpoint; RDS shows
  it active with `deprecated_*` cleared, and it reappears in the Redis projection
  on the next cycle.
- Non-eligibility (e.g. Google-closed) deprecations are refused with 409 and
  remain deprecated.
- The response reports `would_be_reswept` correctly against the live config.
- Idempotent no-op 200 for already-active; 404 for missing.
- `audit.enrichment_history` gains a `reactivate` row per restore and a
  `soft_delete` row per soft-delete; `VENUES_REACTIVATED_TOTAL{source,result}` is
  emitted.
- `make test-unit` and `make test-bdd` pass, including the new feature file.

## Open Questions
- None blocking. Decisions taken (user-confirmed): restore is restricted to
  `deprecated_source='eligibility_filter'` (refuse others, no force override);
  re-appearance relies on the 2-min projector (no single-venue projection); audit
  is added for both reactivate and the previously-unaudited soft-delete;
  already-active is an idempotent 200; `google_business_status` is left untouched.
