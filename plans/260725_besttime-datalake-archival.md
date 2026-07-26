# BestTime Raw-Response Data Lake (S3)

## Branch
feature/besttime-datalake-archival

## Goal
Archive every response cs-server receives from BestTime — live forecasts, weekly
raw forecasts, venue filter searches, venue creates, and account inventory — as
immutable, Hive-partitioned, gzipped NDJSON in a Terraform-managed S3 bucket, so
the full history of what BestTime told us is queryable later (Athena / Hive /
Spark / DuckDB) without any further ingestion work.

The archival path must be strictly non-blocking: no BestTime fetch, refresh job,
or enrichment pipeline may fail, slow down, or change behavior because an S3
write failed, timed out, or fell behind. Every drop must be logged and counted
so Grafana can see it.

## Non-goals
- **Other sources.** Google Places, Apify, SerpAPI, and OpenAI responses are out
  of scope. The writer stays source-generic and the key layout reserves
  `source=<name>`, so adding them later is a call-site change, not a migration.
- **User-facing / serving events.** End-user searches, favorites, and engagement
  live in vibes_bot's serving path and are a separate pipeline.
- **The `curated/` Parquet layer.** The prefix is reserved and the raw layout is
  designed for it, but no compaction job is built here.
- **Glue catalog / Athena tables / dashboards.** The layout makes them trivial to
  add; provisioning them is follow-up work. Grafana alert rules live in the
  vibes_bot repo and are tracked separately.
- **Migrating the existing menu-photo bucket into Terraform.** Pre-existing gap,
  unrelated blast radius.
- **Backfill.** Only fetches made after the flag is enabled are archived; there
  is no historical BestTime data to replay.

## Evidence
- Every BestTime call funnels through two methods: `_request()`
  (`app/api/besttime_client.py:275`) for `/venues/filter`, `/forecasts/live`,
  `/forecasts/week/raw2`, and `/venues`; and `add_venue_to_account()`
  (`app/api/besttime_client.py:478`) for the `POST /forecasts` create. Two taps
  cover the whole surface.
- `list_account_inventory()` (`app/api/besttime_client.py:591`) paginates through
  `_request()`, so it is covered by the same tap, one record per page.
- **Auth travels in query parameters.** `_request` is called with
  `query_params["api_key_private"] = ...` (`app/api/besttime_client.py:367`,
  `:433`, `:467`, `:497`) and `api_key_public` for the weekly forecast. Writing a
  raw request block to S3 would persist live credentials. `app/log_redaction.py`
  already carries the exact patterns (`pri_[0-9a-f]{16,}`, `AIza…`, `key=`).
- `app/api/s3_client.py` proves the boto3 +`asyncio.to_thread` pattern this repo
  uses, and that it currently **requires** explicit static keys
  (`app/api/s3_client.py:34`) — the new writer must not.
- `app/container.py:219-232` shows the established optional-dependency wiring:
  build the client only when its settings are present, log and skip otherwise.
- Terraform convention: `infra/{landing,rds,backend-bootstrap}/`, each a separate
  stack with its own state key in `vibesense-tfstate-839287955684`,
  `use_lockfile`, `profile = "vibesense"`, `aws ~> 5.0`
  (`infra/rds/backend.tf`, `infra/landing/versions.tf`).
  `infra/landing/main.tf:37` shows the `vibesense-<component>-${account_id}`
  bucket-naming and public-access-block pattern.
- Refresh cadence: `venues_live_refresh_minutes: int = 5` (`app/config.py:128`)
  — 288 live-refresh runs/day, which is what makes per-response objects the wrong
  choice and batching a requirement (see Implementation Approach).
- Metric conventions: snake_case names, label lists commented inline
  (`app/metrics.py:58-95`); `S3_UPLOADS_TOTAL{status}` and
  `S3_UPLOAD_DURATION_SECONDS` already exist but are menu-photo-specific and must
  not be reused for the lake.
- Scheduler jobs are wrapped by a shared instrumentation skeleton
  (`main.py:69`, `main.py:311`), which is where `run_id` / `job` come from.

## Current Behavior
BestTime responses are parsed into Pydantic models, used to update RDS and the
Redis projection, and then discarded. Nothing retains the raw payloads, so no
historical analysis of BestTime's own data is possible — a live busyness reading
for a given venue-hour is unrecoverable once the refresh cycle passes. S3 is used
only for menu photos, through a client that requires static access keys, against
a bucket that Terraform does not manage.

## Desired Behavior
1. When cs-server receives any BestTime response (success **or** failure), it
   records one envelope containing the verbatim payload into an in-memory queue,
   without awaiting anything.
2. A background flusher batches queued records by `(dataset, dt, hour)` and
   uploads each batch as one gzipped NDJSON object to the data-lake bucket.
3. Object keys are Hive-style `key=value` partitions so query engines discover
   partitions with no configuration.
4. Credentials never reach S3: request parameters are redacted before the
   envelope is built.
5. Any archival failure — queue full, serialization error, S3 error, S3 timeout —
   is swallowed, logged with context, and counted in a Prometheus metric. The
   BestTime call and its calling pipeline proceed exactly as they do today.
6. The feature is off by default and does nothing until `datalake_enabled` is
   set, so merging changes no runtime behavior.
7. On shutdown, buffered records are flushed within a bounded timeout.

## Implementation Approach

### A. Terraform — new stack `infra/datalake/`
A separate stack (own dir, own state key `datalake/terraform.tfstate` in the
existing state bucket) so a lake apply can never plan a change to RDS or the
landing site. Files mirror the existing stacks: `versions.tf`, `backend.tf`,
`variables.tf`, `main.tf`, `iam.tf`, `outputs.tf`, `README.md`.

- Bucket `vibesense-datalake-${account_id}`, region **us-east-1**, matching the
  cs-server EC2 and the other stacks.
- **S3 Standard only. No lifecycle storage-class transitions, no
  Intelligent-Tiering, no Glacier, no replication.** Storage class is a single
  variable (`var.storage_class`, default `STANDARD`) if One Zone-IA is ever
  wanted; the default keeps 3-AZ durability because live-busyness observations
  cannot be re-fetched. Cross-region replication is opt-in in S3 and stays off.
- Public access block (all four true); SSE-S3 `AES256` (not KMS — per-request
  KMS charges on a high-object-count bucket buy nothing here); versioning enabled
  (near-zero cost on append-only unique keys, and the data is irreplaceable).
- Housekeeping lifecycle rules only: abort incomplete multipart uploads after
  7 days, expire noncurrent versions after 30 days, expire `_athena_results/`
  after 30 days.
- Bucket policy denying any request with `aws:SecureTransport = false`.
- **VPC Gateway Endpoint for S3** — free, keeps ingestion traffic off the public
  internet, and avoids NAT data-processing charges if the EC2 egresses via NAT.

### B. IAM — role, not keys
No long-lived credentials anywhere: none in Terraform state, none in `.env`, none
in CI.

- `aws_iam_policy.datalake_writer`: `s3:PutObject` on
  `arn:aws:s3:::<bucket>/raw/*` **only** — no Get, no Delete, no ListBucket. A
  fully compromised cs-server can append to the lake and nothing else.
- `aws_iam_policy.datalake_analytics`: `GetObject` on `raw/*` + `curated/*`,
  `ListBucket`, and `PutObject` on `_athena_results/*`, for human/Athena use.
- `variable "existing_instance_role_name"` — an EC2 can hold only one instance
  profile and the instance was created outside Terraform. When the variable is
  set, the stack attaches the policy to that existing role. When it is empty, the
  stack creates the role + instance profile and the README documents the one-time
  `aws ec2 associate-iam-instance-profile`, mirroring the manual-step runbook
  style the landing stack already uses for GoDaddy DNS.

### C. `app/dao/datalake_writer.py` — the write path
A DAO-boundary component (per the architecture guardrails: DAOs own persistence,
business logic never binds to raw storage calls). Built in `app/container.py`
behind `settings.datalake_enabled`, following the existing optional-dependency
pattern; when disabled, callers hold `None` and every tap is a no-op.

- `record(...)` builds the envelope and calls `queue.put_nowait()` on a bounded
  `asyncio.Queue`. It never awaits and never blocks the caller.
- A background flusher task drains the queue, groups by `(dataset, dt, hour)`,
  and flushes a group when it reaches `datalake_flush_max_bytes`, when
  `datalake_flush_max_seconds` elapses, or on shutdown.
- Upload is `asyncio.to_thread(s3.put_object, …)` with a botocore `Config`
  setting connect/read timeouts to 5s and `max_attempts=2`.
- The boto3 client is constructed **without** explicit credentials when none are
  configured, so the default chain resolves (IMDSv2 on EC2, the `vibesense` SSO
  profile locally). Optional `datalake_access_key_id` / `datalake_secret_access_key`
  settings remain as a local-dev escape hatch only.
- Registered in the FastAPI lifespan so shutdown drains the queue within a bounded
  timeout.

### D. Batching is a cost decision, not just a reliability one
S3 Standard-IA and Glacier tiers bill a **128 KB minimum per object**, and every
`PutObject` costs money regardless of size. At a 5-minute live-refresh cadence, one
object per API response would produce ~50,000 objects/day; one object per
`(dataset, hour)` window produces ~100–300/day. The flush thresholds
(~256 KB gzipped / 15 minutes) are chosen so objects clear the 128 KB floor while
bounding crash-loss to at most one flush window. Query engines also degrade badly
on millions of tiny files, so the same choice serves both goals.

### E. Taps in `app/api/besttime_client.py`
`BestTimeAPIClient.__init__` takes an optional `datalake` recorder (default
`None`). `_request()` and `add_venue_to_account()` each call it once per response
— on the success path and inside every `except` block — mapping endpoint to
dataset:

| Call | Endpoint | `dataset` |
|---|---|---|
| `get_live_forecast` | `POST /forecasts/live` | `live_forecast` |
| `get_week_raw_forecast` | `GET /forecasts/week/raw2` | `week_raw_forecast` |
| `venue_filter` | `GET /venues/filter` | `venue_filter` |
| `add_venue_to_account` | `POST /forecasts` | `venue_create` |
| `list_account_inventory` | `GET /venues` | `account_inventory` (one record/page) |

Each tap is individually wrapped so a recorder bug can never alter BestTime error
handling — in particular the 404-empty-envelope path
(`app/api/besttime_client.py:376`) and the monthly-cap 429 path must behave
identically with the recorder enabled, disabled, or raising.

### F. S3 key layout
Recorded exactly, because it is the contract every future query engine binds to:

```
s3://vibesense-datalake-<account_id>/
  raw/source=besttime/dataset=<dataset>/dt=<YYYY-MM-DD>/hour=<HH>/part-<run_id>-<seq>.ndjson.gz
  curated/          # reserved for Parquet compaction; nothing written here yet
  _athena_results/  # query scratch, 30-day expiry
```

`dt` and `hour` are **UTC** — partition keys must be unambiguous and monotonic at
write time. Recife-local date and hour are carried as record fields instead, so
"which Friday night was busy" stays a column filter and the repo's Recife
timezone behavior is untouched. `source` and `dataset` are partition keys too, so
one table can be defined per dataset (typed `payload` struct) or a single
catch-all table over `raw/` (with `payload` as `string`) without moving data.

### G. Record envelope (schema_version 1)
Identical across every dataset, so one table definition holds forever and
`payload` keeps whatever we did not think to extract today:

```json
{ "record_id": "uuid", "schema_version": 1,
  "ingested_at_utc": "2026-07-25T17:04:03.221Z",
  "recife_date": "2026-07-25", "recife_hour": 14,
  "source": "besttime", "dataset": "live_forecast",
  "endpoint": "/forecasts/live", "http_status": 200,
  "latency_ms": 412, "outcome": "success",
  "run_id": "<job run id>", "job": "live_refresh", "venue_id": "ven_…",
  "request": { "<redacted params>": "…" },
  "payload": { "<verbatim BestTime JSON>": "…" },
  "error": null }
```

Failed calls are archived too (`outcome: "error"`, `http_status`, `error`, and a
null `payload`) — outage history is exactly the kind of question this lake exists
to answer.

### H. Redaction
The envelope's `request` block is produced by a redaction helper reusing the
patterns in `app/log_redaction.py`. `api_key_private`, `api_key_public`, and any
`AIza…` / `pri_…` value are dropped or replaced before the record is queued. This
is enforced by unit test, not by convention.

## Data, Config, And API Impact
- **API:** none. No route, request, or response changes.
- **Persistence:** no RDS schema change, no Alembic migration, no Redis key
  change. S3 is a new, additive, write-only-append store.
- **New settings** (`app/config.py` + `config.example.json` + `.env.example`),
  all optional and inert by default:
  `datalake_enabled` (bool, `false`), `datalake_bucket` (str, `""`),
  `datalake_region` (str, `"us-east-1"`), `datalake_queue_maxsize` (int, `10000`),
  `datalake_flush_max_bytes` (int, `262144`), `datalake_flush_max_seconds`
  (int, `900`), `datalake_shutdown_flush_seconds` (int, `10`), and the optional
  local-dev `datalake_access_key_id` / `datalake_secret_access_key`.
- **Feature flag:** `datalake_enabled` is the single kill switch. With it false,
  the recorder is never constructed and every tap is a no-op — merging this
  changes nothing in production until it is deliberately turned on.
- **Infrastructure:** one new Terraform stack, applied independently of the app
  deploy. `boto3` is already a dependency.

## Error Handling And Observability

Failure isolation, in order of the paths that can fail:

| Failure | Behavior | Counted as |
|---|---|---|
| Feature disabled | Tap is a no-op | — |
| Envelope build / JSON serialization raises | Caught, record dropped, `ERROR` log | `reason="serialize_error"` |
| Queue full (flusher behind or S3 down) | `put_nowait` drops immediately, never blocks | `reason="queue_full"` |
| S3 error / timeout after bounded retry | Batch dropped, `ERROR` log with dataset, run_id, record count | `reason="flush_failed"` |
| Any unexpected exception in tap or flusher | Blanket `except Exception`, logged, swallowed | `reason="unexpected"` |

The taps and the flusher are each wrapped in a blanket `except Exception`. No
archival failure can propagate into a BestTime call, a refresh job, or an
enrichment pipeline. Logs carry dataset / run_id / record counts and never carry
payload contents or credentials.

New metrics in `app/metrics.py` (existing `S3_UPLOADS_TOTAL` is menu-photo
specific and is deliberately not reused):

```
datalake_records_enqueued_total{source,dataset}
datalake_records_dropped_total{source,dataset,reason}
    reason: queue_full | serialize_error | flush_failed | unexpected
datalake_flush_total{dataset,status}          status: success | error
datalake_flush_duration_seconds{dataset}
datalake_flush_bytes_total{dataset}
datalake_queue_depth                          Gauge
datalake_last_success_timestamp               Gauge (unix seconds)
```

`datalake_queue_depth` and `datalake_last_success_timestamp` are the two Grafana
needs for alerting: any sustained `datalake_records_dropped_total` increase is a
warning, and `time() - datalake_last_success_timestamp > 3600` while enabled is a
critical. The alert rules themselves are provisioned in the vibes_bot repo
(`config/grafana/provisioning/alerting/`) and are follow-up work, not part of
this branch.

## Test Plan
Feature file: `tests/bdd/persistence/besttime-datalake-archival.feature`

Scenarios:
- Archive a successful live-forecast fetch — the record lands in the
  `dataset=live_forecast` partition with the payload verbatim.
- Map each BestTime endpoint to its dataset — weekly forecast, venue filter,
  venue create, and account inventory each archive under their own dataset.
- Partition by UTC while carrying Recife local time — a fetch at 21:00 Recife on
  25 July is written under `dt=2026-07-26/hour=00` and carries
  `recife_date=2026-07-25`, `recife_hour=21`.
- Never write credentials — the archived request block contains no
  `api_key_private`, `api_key_public`, or `AIza…` value.
- Archive failed fetches — a BestTime timeout is recorded with `outcome=error`
  and a null payload, and the refresh still completes.
- Survive an S3 outage — every upload fails, the refresh job completes normally,
  venues are still written to RDS and the projection, drops are counted and
  logged.
- Never block on a full queue — with the queue saturated, the refresh completes
  in normal time and the excess is counted as `queue_full`.
- Batch a refresh run into one object — many venue fetches in one window produce
  a single gzipped NDJSON object with one line per response.
- Stay inert when disabled — with `datalake_enabled` false, a full refresh writes
  nothing to S3 and emits no datalake metrics.
- Flush on shutdown — buffered records are uploaded when the app stops.

Pytest unit tests:
- `tests/test_datalake_writer.py` — envelope field construction and
  `schema_version`; UTC partition-key derivation across a Recife day boundary;
  S3 key format; gzip NDJSON framing (one JSON object per line); flush triggered
  by bytes, by age, and by shutdown; `put_nowait` drop when the queue is full;
  `ClientError` and timeout during upload swallowed and counted; blanket
  exception safety.
- `tests/test_datalake_redaction.py` — `api_key_private`, `api_key_public`, and
  `AIza…` values are absent from the serialized record for every BestTime call
  shape.
- `tests/test_besttime_client_datalake_tap.py` — each endpoint maps to the
  correct dataset; a recorder that raises does not change any BestTime return
  value or exception, specifically for the 404-empty-envelope filter path and the
  monthly-cap 429 create path; `datalake=None` performs no work.
- `tests/test_datalake_client_credentials.py` — the boto3 client is built with no
  explicit credentials when none are configured (default chain), and with them
  when the local-dev settings are present.

Manual or integration checks:
- `terraform plan` in `infra/datalake/` against the real account; confirm no
  resource outside the new stack is touched.
- After apply with the flag on in staging: confirm objects appear under
  `raw/source=besttime/dataset=live_forecast/dt=…/hour=…/`, that a downloaded
  object gunzips to valid NDJSON, and that no key material appears anywhere in
  it.
- Confirm the EC2 reaches S3 with no static credentials in the container
  environment.
- Confirm `GET /metrics` exposes every `datalake_*` series.

## Acceptance Criteria
- `terraform apply` in `infra/datalake/` creates the bucket, its policies, and
  the IAM policies, with its own state key and no change to the RDS or landing
  stacks.
- The bucket is private, encrypted, versioned, single-region, **S3 Standard with
  no storage-class transition rules**, and rejects non-TLS requests.
- cs-server writes to S3 using instance-role credentials, with no access key in
  the container environment.
- Every one of the five BestTime datasets archives to its own
  `dataset=` partition, with the payload byte-identical to what BestTime returned.
- No archived object contains `api_key_private`, `api_key_public`, or any
  `AIza…` / `pri_…` value.
- With S3 unreachable, a full live-refresh cycle completes with unchanged RDS and
  Redis-projection results, and `datalake_records_dropped_total` increases.
- With `datalake_enabled` false, no S3 call is made and no datalake metric moves.
- One live-refresh window produces a single object, not one per venue.
- All `datalake_*` metrics are exposed on `GET /metrics`.
- `make test-bdd` and `make test-unit` pass, and the feature file's `@wip` tag is
  removed.

## Open Questions
- None. Region (us-east-1), scope (BestTime only), storage class (S3 Standard, no
  Glacier, no replication), and credential strategy (instance role, no static
  keys) are all decided.
