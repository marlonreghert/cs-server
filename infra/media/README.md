# VibeSense app media — S3 + CloudFront runbook

Serves media the **app is allowed to display** from a **private S3 bucket**
behind **CloudFront**, at `media.apivibesensemiddleware.click`. First (and
currently only) consumer: venue Instagram profile photos, the venue-list hero
(`plans/260816_instagram-profile-photo-hero.md`).

```
venue-profile-photos/<venue_id>/<sha256[:16]>.jpg
```

## This is NOT the data lake

`infra/datalake` holds `retrieved/`, which is **internal-use-only by design**
(`docs/venue-retrieval-storage.md` §8) — public access blocked *and* the writer
role deliberately denied `s3:GetObject`, so nothing there can ever reach a
user. This stack is the opposite: its whole purpose is to be read by end users.
Keeping them apart is a compliance boundary, not tidiness.

**Nothing in this stack touches `infra/datalake`.** Separate bucket, separate
state key (`media/terraform.tfstate`), and a **new, separately-named** IAM
policy. In particular it does not edit `aws_iam_policy.description` on the
datalake writer: that field is immutable in AWS, so changing it forces a
destroy-and-recreate window in which cs-server holds no `PutObject`, and a lake
flush in that window is **dropped, not retried**.

## Prerequisites

- `aws sso login --profile vibesense`
- The shared state bucket exists — see [`../backend-bootstrap`](../backend-bootstrap).
- Confirm the Route53 zone for `apivibesensemiddleware.click` is in this
  account:
  `aws route53 list-hosted-zones --profile vibesense --query "HostedZones[?Name=='apivibesensemiddleware.click.']"`
  If it is elsewhere, the `data "aws_route53_zone"` lookup and the ACM DNS
  validation fail loudly at plan/apply time — they cannot silently
  misconfigure anything.
- Decide `cs_server_role_name`. Confirm the actual role with:
  ```bash
  aws ec2 describe-instances --profile vibesense \
    --query "Reservations[].Instances[].IamInstanceProfile"
  ```
  Left empty, the policy is still created and you attach it by hand once.

## Apply

Single-phase: the Route53 zone is in this account, so ACM validation completes
inline (same as `infra/admin/`; unlike `infra/landing/`, whose DNS is at
GoDaddy).

```bash
cd infra/media
terraform init
terraform plan     # confirm: ZERO changes outside this module's own resources
terraform apply
```

## Verify BEFORE enabling the job

This ordering is load-bearing. A write outside the IAM policy fails **after the
Apify scrape has already been paid for**, so the infrastructure is proven
first and the job is switched on second.

```bash
# 1. The bucket is NOT publicly readable.
aws s3api get-public-access-block --profile vibesense \
  --bucket "$(terraform output -raw media_bucket)"
curl -sSI "https://$(terraform output -raw media_bucket).s3.amazonaws.com/x" | head -1   # expect 403

# 2. An object IS readable through CloudFront, with a year-long cache-control.
KEY="venue-profile-photos/_smoke/0000000000000000.jpg"
aws s3api put-object --profile vibesense \
  --bucket "$(terraform output -raw media_bucket)" --key "$KEY" \
  --body /path/to/any.jpg --content-type image/jpeg \
  --cache-control "public, max-age=31536000, immutable"
curl -sSI "$(terraform output -raw media_cdn_base_url)/$KEY"
# expect: HTTP/2 200 + cache-control: public, max-age=31536000, immutable

# 3. cs-server itself can write (run FROM the EC2 host, using its role).
```

## Then turn the job on

```
MEDIA_BUCKET=<terraform output -raw media_bucket>
MEDIA_CDN_BASE_URL=<terraform output -raw media_cdn_base_url>
INSTAGRAM_PROFILE_PHOTO_ENABLED=true
```

Start with a small `INSTAGRAM_PROFILE_PHOTO_MAX_VENUES_PER_RUN` and trigger a
run manually (`POST /admin/trigger/instagram_profile_photos`) before letting
the schedule take over. Confirm stored objects, `instagram.profile_photo` rows
and `venue_profile_photo_v1:*` keys agree.

`MEDIA_CDN_BASE_URL` is a **contract**: it is baked into every stored
`photo_url`, served by vibes_bot, and rendered by released mobile clients.
Changing it later strands every URL already written.

## Safety

- Own state key (`media/…`). A media apply can never plan a change to the data
  lake, the database, the landing site or the admin panel.
- The bucket is private; only this distribution can read it (OAC bucket policy
  + full public-access block).
- The write grant is `s3:PutObject` on `venue-profile-photos/*` in this bucket
  only — no `GetObject`, no `DeleteObject`, no `ListBucket`, no wildcards. A
  fully compromised cs-server can add profile photos and nothing else.
- `aws_iam_policy.description` on both this policy and the datalake writer is
  **immutable**: never edit either string.
