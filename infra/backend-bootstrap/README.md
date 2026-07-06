# Terraform remote-state bootstrap

The one-time setup that gives every Terraform stack in this account a **durable,
versioned, shared** state backend — so we never again keep the only copy of
production state as a gitignored local file.

## What it creates

An S3 bucket `vibesense-tfstate-<account-id>` (`us-east-1`) with:

- **Versioning ON** — every state write keeps the previous version. This is the
  real safety net.
- **Default encryption** (SSE-S3 / AES256).
- **Public access fully blocked** + a **TLS-only** bucket policy.

State keys inside the bucket:

| Stack           | Key                        |
|-----------------|----------------------------|
| `infra/rds`     | `rds/terraform.tfstate`    |
| `infra/landing` | `landing/terraform.tfstate`|

Separate keys mean a `landing` apply can **never** plan a change to the RDS.

State locking uses the S3 backend's **native lockfile** (`use_lockfile = true`,
Terraform ≥ 1.11) — no DynamoDB table required.

## Run it (once)

```bash
aws sso login --profile vibesense
cd infra/backend-bootstrap
./create-state-bucket.sh
```

The bucket holds no Terraform state of its own (it can't — chicken-and-egg), so
it is created with the AWS CLI, not Terraform. The script is safe to re-run.

## Then migrate the existing RDS state

> ### ⚠️ Run this in the copy that HOLDS the local state.
> The live `terraform.tfstate` lives **only** in the *standalone* checkout
> `~/projects/cs-server/infra/rds/` (it's gitignored, so the wrapper submodule
> copy does **not** have it). `backend.tf` has been placed in **both** copies.
>
> **Migrate from the standalone copy** — it has the state *and* an initialized
> `.terraform/` with providers:
>
> ```bash
> cd ~/projects/cs-server/infra/rds
> terraform init -migrate-state   # answer "yes" to copy existing state to S3
> terraform plan                  # <<< GO/NO-GO GATE >>>
> ```
>
> `terraform plan` **must print "No changes."** If it wants to create or replace
> `aws_db_instance.this` (or any of the 8 resources), **STOP** — the state did
> not migrate; you're in the wrong directory or the local state is missing.

`init -migrate-state` touches **zero** AWS resources — it only moves where state
is stored. It is safe. (Any later `apply` is what to review carefully.)

### Closing step — reconnect the wrapper copy

Once migrated and verified, commit `backend.tf`. In the wrapper submodule copy
just run a plain init (no `-migrate-state`) — it connects to the same S3 state:

```bash
cd <wrapper>/cs-server/infra/rds
terraform init      # connects to S3; `terraform plan` should also show No changes
```

After this the "two copies on disk" problem disappears: state lives in S3, not in
either folder.
