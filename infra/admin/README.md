# VibeSense admin panel — S3 + CloudFront hosting runbook

Serves the admin panel's React frontend from a **private S3 bucket** behind
**CloudFront**, at the existing **`admin.apivibesensemiddleware.click`**
host — same-origin with the FastAPI admin API (unchanged, still on
`admin:9000` behind Caddy), so the existing signed session cookie keeps
working with no rewrite. Companion plan:
`vibes_bot/plans/260807_admin-panel-react-migration.md` (the React app +
deploy script + Caddyfile change). Coordination plan:
`plans/260807_admin-panel-react-s3-migration.md` (wrapper root).

## Prerequisites

- `aws sso login --profile vibesense`
- The shared state bucket exists — see [`../backend-bootstrap`](../backend-bootstrap).
- **Confirm the Route53 zone for `apivibesensemiddleware.click` is in this
  account** before applying:
  `aws route53 list-hosted-zones --profile vibesense --query "HostedZones[?Name=='apivibesensemiddleware.click.']"`
  This stack assumes it is (confirmed via public `dig` NS lookup showing
  `awsdns-*` nameservers at plan time, but that only proves Route53 hosts it
  *somewhere* — not that it's in *this* account). If it's a different
  account, this stack's `data "aws_route53_zone"` lookup and the ACM DNS
  validation will fail loudly at apply time — it will not silently
  misconfigure anything.
- **Confirm `54.145.54.177` (the `origin_ip` variable default) is a stable
  Elastic IP**, not an ephemeral public IP that changes on instance
  replacement — `aws ec2 describe-addresses --profile vibesense
  --query "Addresses[?PublicIp=='54.145.54.177']"`. If it isn't allocated as
  an EIP, allocate and associate one first, or the new origin record will go
  stale the next time the EC2 instance restarts.

## Why this is single-phase (unlike `infra/landing/`)

`infra/landing/` needed a two-phase apply because DNS for `vibesense.live`
is authoritative at GoDaddy — a human had to add ACM's validation CNAMEs by
hand between phases. `apivibesensemiddleware.click` is Route53-hosted in
this account, so this stack creates the validation records itself and
`aws_acm_certificate_validation` waits inline. One `terraform apply` builds
the cert, the bucket, the new origin DNS record, and CloudFront.

```bash
cd infra/admin
terraform init
terraform plan     # confirm: no changes to landing/ or rds/ state (separate keys)
terraform apply
```

## What this stack deliberately does NOT do

It does **not** touch the existing `admin.apivibesensemiddleware.click`
record — today an `A` record pointing straight at the EC2 host, created
outside this stack. Adopting an unowned production DNS record into Terraform
state risks drift or accidental deletion on a future `destroy`. The traffic
cutover is a **manual, reviewed step**, done once the distribution is
verified working on its own domain:

### 1. Verify the stack directly

```bash
terraform output cloudfront_domain_name        # d123....cloudfront.net
curl -sSI https://$(terraform output -raw cloudfront_domain_name)/ \
  -H "Host: admin.apivibesensemiddleware.click"
```

### 2. Verify the API origin is reachable

Requires the companion `vibes_bot` plan's Caddyfile change (new
`admin-origin.apivibesensemiddleware.click` block) to be deployed first:

```bash
curl -sSI https://admin-origin.apivibesensemiddleware.click/health   # expect 200
```

### 3. Cut over (manual, deliberate — this changes live production traffic)

Replace the existing `admin.apivibesensemiddleware.click` A record with an
ALIAS record targeting this distribution:

```bash
terraform output cloudfront_domain_name
terraform output cloudfront_hosted_zone_id
terraform output route53_zone_id
```

Via the AWS Console (Route53 → the zone → the `admin` record) or
`aws route53 change-resource-record-sets`, change it from `A -> 54.145.54.177`
to an `ALIAS -> <cloudfront_domain_name>` (target hosted zone
`<cloudfront_hosted_zone_id>`). Confirm before doing this: the React app has
been deployed to the S3 bucket (see the `vibes_bot` plan's deploy step) —
otherwise the cutover points production at an empty bucket.

Verify:

```bash
curl -sSI https://admin.apivibesensemiddleware.click/          # expect the React index.html
curl -sSI https://admin.apivibesensemiddleware.click/health    # expect 200 from the API origin
```

If anything looks wrong, revert the record back to the direct EC2 `A` value
— Caddy on the EC2 host is untouched and still serves the old path.

## Publishing the app after infra exists

Not this stack's job — see the `vibes_bot` plan's deploy script
(`aws s3 sync` + `aws cloudfront create-invalidation --distribution-id
$(terraform output -raw cloudfront_distribution_id) --paths "/*"`).

## Safety

- Own state key (`admin/…`), separate from `landing/…` and `rds/…`. An
  `admin` apply can never plan a change to the landing site or the database.
- The origin bucket is private; only this CloudFront distribution can read
  it (OAC bucket policy + full public-access block).
- The pre-existing `admin.apivibesensemiddleware.click` record is untouched
  by Terraform — see "What this stack deliberately does NOT do" above.
