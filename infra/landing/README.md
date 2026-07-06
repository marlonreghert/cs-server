# VibeSense landing site — runbook

Static institutional site served at **https://www.vibesense.live** (apex
`vibesense.live` redirects to it). Private S3 origin → CloudFront (OAC, HTTPS) →
ACM cert in us-east-1. DNS stays at **GoDaddy**; we only add records there.

This site exists so the company has a real, published institutional website for
Apple Developer **org enrollment / DUNS**. The domain matches the enrollment
email (`@vibesense.live`).

## Prerequisites

- `aws sso login --profile vibesense`
- The shared state bucket exists — run [`../backend-bootstrap`](../backend-bootstrap) first.
- You control DNS for `vibesense.live` at GoDaddy.

## Why two-phase apply

DNS is authoritative at GoDaddy, not Route53, so ACM's validation CNAMEs must be
added by hand. CloudFront also refuses a cert that isn't `ISSUED` yet. So:

### Phase A — create the certificate, get its validation records

```bash
cd infra/landing
terraform init
terraform apply -target=aws_acm_certificate.site
terraform output acm_validation
```

`acm_validation` prints one or two CNAMEs like:

```
record_name  = "_a1b2c3....www.vibesense.live."
record_value = "_x9y8z7....acm-validations.aws."
```

At **GoDaddy → Domain → DNS → Records**, add each as **type CNAME**:
- **Name** = the host part only — everything before `.vibesense.live` (GoDaddy
  appends the domain). e.g. `_a1b2c3....www`
- **Value** = the `record_value` **without** the trailing dot.
- TTL: 1 hour (default is fine).

Wait until the cert is issued (usually minutes):

```bash
aws acm list-certificates --profile vibesense --region us-east-1 \
  --query "CertificateSummaryList[?DomainName=='www.vibesense.live'].CertificateArn" --output text
aws acm describe-certificate --profile vibesense --region us-east-1 \
  --certificate-arn <arn> --query "Certificate.Status"    # -> "ISSUED"
```

### Phase B — build CloudFront + bucket policy + upload

```bash
terraform apply
terraform output cloudfront_domain_name     # e.g. d1234abcd.cloudfront.net
```

## Point the domain at the site (GoDaddy)

1. **`www` → CloudFront.** GoDaddy → DNS → Records → add **CNAME**:
   - **Name** = `www`
   - **Value** = the `cloudfront_domain_name` output (no trailing dot)
2. **Apex → www.** GoDaddy → Domain → **Forwarding** → forward `vibesense.live`
   to `https://www.vibesense.live` (permanent 301, forward with masking OFF).
   GoDaddy manages the apex A record for this automatically.

DNS/CDN propagation: a few minutes to ~1 hour. Verify:

```bash
curl -sSI https://www.vibesense.live | head -n 20     # expect HTTP/2 200
```

## Updating the site later

Edit files under `site/`, then:

```bash
terraform apply                              # re-uploads changed objects
# optional immediate cache bust:
aws cloudfront create-invalidation --profile vibesense \
  --distribution-id <id> --paths "/*"
```

## Safety

- This stack has its **own state key** (`landing/…`), completely separate from
  `rds/…`. A `terraform apply` here can never plan a change to the database.
- The origin bucket is private; only this CloudFront distribution can read it
  (enforced by the OAC bucket policy + full public-access block).
