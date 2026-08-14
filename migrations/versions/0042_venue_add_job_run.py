"""Add `admin.venue_add_job_run` — the RDS system of record for venue-add job
tracking (single-add via AddVenueJobService, batch-add via BatchAddService).
See plans/260814_venue-add-job-rds-tracking.md.

Both job services persist their job doc straight to Redis today, TTL'd (7
days for batch, 24h for single). A crash inside the owning process's
_run_job coroutine is handled in-process, but if the WHOLE PROCESS is
replaced (container restart, redeploy, OOM) mid-job, the last-written Redis
doc is left exactly as it was — typically "running" — forever, or until the
TTL silently erases all trace it ever happened. This migration moves job
tracking onto RDS (the system of record per CLAUDE.md), which (a) never
expires, giving durable audit history, and (b) lets startup deterministically
reconcile any row still "running" from a process that no longer exists to
"interrupted" (a pure status UPDATE — never a resumed/re-run add; see
main.py:startup_essential's own "no pipeline runs on startup" policy from the
2026-07-01 incident, which this reconciliation does not conflict with).

Nearest existing precedent for an "operational tracking" table in this
schema style is `events.crawl_target` (migration 0030): raw-SQL `CREATE
TABLE IF NOT EXISTS <schema>.<table>`, `timestamptz NOT NULL DEFAULT now()`
audit columns, a CHECK constraint on an enum-like text column. `admin`
already exists (0001_baseline_schemas.py; holds admin.geo_fence,
admin.geo_fence_city, admin.admin_config, admin.venue_closure_signal).

One shared table backs BOTH job types, discriminated by `job_type` — the
two services' Redis docs already overlapped almost entirely (job_id, status,
started_at, finished_at, stopped_reason) and a single durable table with one
reconciliation query is simpler than two near-identical ones. Columns:
  - `job_id` — reuses the existing `uuid.uuid4().hex` ids unchanged, no
    format migration on the producing side.
  - `job_type` — 'single' | 'batch'. Storage-layer only; never surfaced in
    either service's API response (see app/dao/venue_add_job_store.py's
    shape_job_row, which strips it and reconstructs each job_type's exact
    historical Redis-JSON shape so the five job-related HTTP endpoints stay
    byte-for-byte unchanged).
  - `label`, `resolve_coords`, `total`, `processed`, `summary`, `results`,
    `budget_before`, `budget_after` — batch-only; always NULL for a single
    row (summary/results default to their empty container rather than NULL
    since BatchAddService always initializes them, even for a job with zero
    processed rows yet).
  - `venue_name`, `venue_address`, `http_status`, `result`, `error` —
    single-only; always NULL for a batch row.
  - `status` — 'running' | 'done' | 'stopped' | 'failed' | 'interrupted'.
    'interrupted' is new: it exists ONLY for the boot-time reconciliation
    outcome, never set by either service's own run loop.
  - `stopped_reason` — batch's own early-stop reason (quota/cap/bad-response)
    OR the reconciliation's fixed explanation for an 'interrupted' row.
  - `started_at` / `finished_at` — timestamptz, but the Python layer on both
    sides always works in epoch floats (time.time(), matching what both
    services have always persisted and what the API has always returned);
    app/dao/venue_add_job_store.py converts at the boundary so the response
    contract never changes shape (JSON numbers, never ISO strings).
  - `created_at` / `updated_at` — standard audit columns; `created_at` is
    set once on the initial INSERT and never touched by the upsert's
    `DO UPDATE SET`.

No back-fill: RDS tracking starts from this feature's rollout forward, not
retroactively (Non-goals) — a row already sitting in Redis at deploy time
simply ages out via its existing TTL.

`ix_venue_add_job_run_status` supports the boot-time reconciliation's own
read pattern (`WHERE status = 'running'`), mirroring `ix_crawl_target_enabled`'s
precedent of indexing the column a periodic/startup job filters on.
`ix_venue_add_job_run_started_at` supports `list_recent`'s `ORDER BY
started_at DESC LIMIT`, which replaces the old `admin:add_venue_job_recent_v1`
Redis LIST outright — no separate index structure needed.

Additive only: one new table, two new indexes, two new CHECK constraints.
The downgrade drops exactly what this migration created and touches no other
table. Safe: this table holds no venue content and no engagement data —
losing it only means losing job HISTORY, never a venue/catalog record.

Revision ID: 0042_venue_add_job_run
Revises: 0041_crawl_target_reels_seeded
"""
from alembic import op

revision = "0042_venue_add_job_run"
down_revision = "0041_crawl_target_reels_seeded"
branch_labels = None
depends_on = None


UPGRADE = r"""
CREATE TABLE IF NOT EXISTS admin.venue_add_job_run (
  job_id          text PRIMARY KEY,
  job_type        text NOT NULL,
  label           text,
  status          text NOT NULL,
  total           integer NOT NULL DEFAULT 1,
  processed       integer NOT NULL DEFAULT 0,
  venue_name      text,
  venue_address   text,
  http_status     integer,
  result          jsonb,
  error           text,
  resolve_coords  boolean,
  summary         jsonb NOT NULL DEFAULT '{}'::jsonb,
  results         jsonb NOT NULL DEFAULT '[]'::jsonb,
  stopped_reason  text,
  budget_before   jsonb,
  budget_after    jsonb,
  started_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_venue_add_job_run_job_type CHECK (job_type IN ('single', 'batch')),
  CONSTRAINT ck_venue_add_job_run_status CHECK (
    status IN ('running', 'done', 'stopped', 'failed', 'interrupted')
  )
);

CREATE INDEX IF NOT EXISTS ix_venue_add_job_run_status
    ON admin.venue_add_job_run (status);

CREATE INDEX IF NOT EXISTS ix_venue_add_job_run_started_at
    ON admin.venue_add_job_run (started_at DESC);
"""

DOWNGRADE = r"""
DROP INDEX IF EXISTS admin.ix_venue_add_job_run_started_at;
DROP INDEX IF EXISTS admin.ix_venue_add_job_run_status;
DROP TABLE IF EXISTS admin.venue_add_job_run;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
