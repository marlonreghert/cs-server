# Data lake stack

One S3 bucket holding every raw response cs-server gets from BestTime, laid out
so a query engine can read it with no ETL.

State lives at `datalake/terraform.tfstate` in the shared state bucket, separate
from the `rds/` and `landing/` stacks — a data lake apply can never plan a change
to the database or the public site.

## Layout

```
s3://vibesense-datalake-<account-id>/
  raw/source=besttime/dataset=<dataset>/dt=<YYYY-MM-DD>/hour=<HH>/part-<writer>-<seq>.ndjson.gz
  curated/          # reserved for a later Parquet compaction layer
  _athena_results/  # query scratch, expires after 30 days
```

Datasets today: `live_forecast`, `week_raw_forecast`, `venue_filter`,
`venue_create`, `account_inventory`.

`dt`/`hour` are **UTC** — partition keys have to be unambiguous and monotonic at
write time. Recife-local date and hour ride inside each record (`recife_date`,
`recife_hour`), so local-time analysis is a column filter.

Every directory below `raw/` is `key=value`, so Glue crawlers, Athena partition
projection, Spark, Trino, and DuckDB all discover partitions with no config.
Define one table per dataset (typed `payload` struct) or a single catch-all table
over `raw/` with `payload` as a string — the layout supports both without moving
any data.

## Storage class

Everything stays in **S3 Standard**, single region, no replication (S3 does not
replicate across regions unless you configure it; we don't). No Glacier and no
Intelligent-Tiering: this data exists to be queried, archival tiers add restore
latency, and Intelligent-Tiering's per-object fee costs *more* on small objects.

The lifecycle rules are housekeeping only — abort stale multipart uploads, expire
noncurrent versions, expire Athena scratch.

The one cost lever, off by default, is `onezone_ia_transition_days`: One Zone-IA
is ~$0.013/GB-month cheaper because it drops to a single AZ. At this volume that
is a few cents a month, and a live-busyness reading for a past hour can never be
re-fetched — so full durability is the default.

The real cost driver is request count, which the writer handles by batching a
partition window into one object (~100–300 objects/day) instead of one per API
response (~50,000/day at the 5-minute refresh cadence).

## Apply

```bash
aws sso login --profile vibesense
terraform init
terraform apply                       # add -var 'existing_instance_role_name=...' if the EC2 has a role
```

Optional, recommended if the EC2 sits behind a NAT gateway:

```bash
terraform apply -var vpc_id=vpc-xxxx -var 'route_table_ids=["rtb-xxxx"]'
```

### Credentials — role, never keys

cs-server writes with the **EC2 instance role**, resolved through boto3's default
credential chain. There is no access key in Terraform state, in `.env`, or in CI.

An EC2 can hold only one instance profile, and this instance was created outside
Terraform, so there are two paths:

**The instance already has a role** — find it and hand it to the stack:

```bash
aws ec2 describe-instances --instance-ids <id> --profile vibesense \
  --query 'Reservations[].Instances[].IamInstanceProfile.Arn'
terraform apply -var existing_instance_role_name=<role-name>
```

**The instance has no role** — the stack creates one, and you attach it once by
hand (same shape as the landing stack's manual DNS step):

```bash
terraform apply
aws ec2 associate-iam-instance-profile --profile vibesense \
  --instance-id <id> \
  --iam-instance-profile Name=$(terraform output -raw instance_profile_name)
```

Then confirm from inside the instance that credentials resolve with nothing in
the environment:

```bash
aws sts get-caller-identity     # must show the vibesense-cs-server role
```

## Turn archival on

After apply, set on cs-server and restart:

```
DATALAKE_ENABLED=true
DATALAKE_BUCKET=$(terraform output -raw bucket_name)
DATALAKE_REGION=us-east-1
```

Leave `DATALAKE_ACCESS_KEY_ID` / `DATALAKE_SECRET_ACCESS_KEY` unset — they exist
only as a local-development escape hatch, and setting them in production defeats
the instance role.

Verify:

```bash
aws s3 ls s3://$(terraform output -raw bucket_name)/raw/source=besttime/ --recursive --profile vibesense | head
curl -s localhost:8080/metrics | grep datalake_
```

`datalake_records_dropped_total` should stay flat and
`datalake_last_success_timestamp` should keep advancing.
