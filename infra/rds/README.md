# RDS provisioning runbook (system of record)

Standalone RDS Postgres, independent of the cs-server deploy. cs-server is the
sole application that connects programmatically; engineers connect with DBeaver
via SSM port-forward (no VPN, no public endpoint). Full design:
`plans/rds_system_of_record_01_06_26.md`.

> **Do this step when the cs-server code (Phases 0–1) is merged.** Provisioning
> RDS is the manual gate between the cs-server changes and the vibes_bot changes.

## 0. Prerequisites
- AWS **IAM Identity Center (SSO)** enabled; a local profile configured so
  `aws sso login --profile <profile>` opens the browser. Verify:
  `aws sts get-caller-identity --profile <profile>`.
- `terraform >= 1.6` installed.
- The cs-server EC2's **region**, **VPC id**, **>=2 private subnet ids**, and
  **security-group id** (RDS co-locates in this VPC/region).
- The EC2 has the **SSM agent** + an instance profile allowing SSM (for the
  port-forward and for running migrations).

## 1. Provision (Terraform, SSO auth)
```bash
aws sso login --profile <profile>
cd infra/rds
terraform init
terraform apply \
  -var region=<region> -var aws_profile=<profile> \
  -var vpc_id=<vpc> -var 'private_subnet_ids=["<subnet-a>","<subnet-b>"]' \
  -var ec2_security_group_id=<ec2-sg>
```
Outputs: `rds_endpoint`, `credentials_secret_arn`. The master password lives only
in Secrets Manager (`vibesense/rds/credentials`), never in code/state output.

## 2. Run the baseline migration (manual, via SSM — never on container boot)
From the EC2 (or an SSM session) with the repo + venv and the DB env exported
from the secret:
```bash
export RDS_HOST=<endpoint> RDS_PORT=5432 RDS_DB=vibesense \
       RDS_USER=<user> RDS_PASSWORD=<pw> RDS_SSLMODE=require
alembic upgrade head            # preview first with: alembic upgrade head --sql
```
Re-run before any deploy that ships a new migration.

## 3. Engineer access from home (DBeaver via SSM, no VPN)
```bash
aws sso login --profile <profile>
aws ssm start-session --target <ec2-instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<rds-endpoint>"],"portNumber":["5432"],"localPortNumber":["5432"]}'
# then point DBeaver at localhost:5432 (SSL required), creds from Secrets Manager
```

## 3b. Validate the real store SQL against Postgres (BEFORE backfill)
The offline suite exercises the in-memory fake store only. Validate the real
`RdsVenueStore` SQL against the migrated scratch/RDS DB before the prod backfill:
```bash
RDS_TEST_URL=postgresql+psycopg://<user>:<pw>@<endpoint>:5432/<db> \
  .venv/bin/python -m pytest tests/test_rds_store_contract.py -v
```
The `[rds]` params run only when RDS_TEST_URL is set. Use a disposable DB/schema
(the test writes rows and does not clean up).

## 4. Cut over cs-server to RDS (after code deploy)
Set `rds_enabled=true` + the `RDS_*` env (from the secret) +
`engagement_pseudonymization_key`, deploy, then run the one-time backfill
(`POST /admin/trigger/backfill_rds`). Verify counts, then validate a rebuild on
staging (`POST /admin/trigger/rebuild_redis`) restores serving incl. the geo
index. Reads keep serving from Redis throughout.

## 5. Then: vibes_bot
Only after RDS is live + cs-server cut over — see `VIBES_BOT_HANDOFF.md` (repo
root) for the vibes_bot changes (admin-via-API, favorites/hot_likes-via-API).
