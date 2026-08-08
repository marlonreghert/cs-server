# Admin panel — S3 + CloudFront hosting infra

## Branch
chore/admin-panel-s3-cloudfront-infra

## Goal
Provision the AWS infrastructure to host the admin panel's frontend (currently
`vibes_bot/app/admin/static/admin.html`, being rewritten as a React app in a
companion `vibes_bot` plan) as a static build in a **private S3 bucket**,
served through **CloudFront**, at the existing `admin.apivibesensemiddleware.click`
host — with the existing FastAPI admin API (`admin:9000`, unchanged) still
reachable at the same public host via a CloudFront path-based behavior. This
lets the frontend and API stay same-origin so the current cookie-session auth
(`vibes_bot/app/admin/auth.py`) keeps working with no rewrite.

## Non-goals
- No changes to `vibes_bot` application code, the admin API, or admin auth —
  those are out of scope for this repo and covered by the companion
  `vibes_bot` plan.
- No change to the `landing/` or `rds/` Terraform stacks or their state.
- No IP allowlisting, WAF, or Lambda@Edge/CloudFront Function auth layer in
  front of the distribution — "private bucket" here means the S3 origin is
  unreadable except via CloudFront OAC (mirrors `infra/landing/`); the
  existing app-level password/cookie login remains the sole access gate. If
  the operator wants network-level hardening on top of that, it's a follow-up.
- No Route53 / DNS automation — DNS for `apivibesensemiddleware.click` is not
  in this AWS account (no existing Route53 zone found for it, unlike nothing
  vs. the GoDaddy-managed `vibesense.live`); its registrar/DNS host is
  currently undocumented in this repo. All DNS record changes are manual
  operator steps, same pattern as `infra/landing/`'s GoDaddy CNAMEs.

## Evidence
- `infra/landing/main.tf`, `variables.tf`, `outputs.tf`, `backend.tf` —
  established pattern for this exact shape (private S3 + CloudFront OAC +
  ACM in us-east-1 + S3-backed Terraform state), built for
  `plans/260706_landing-site-and-tfstate-s3.md` (wrapper root). This plan
  reuses that pattern almost verbatim, swapping the origin from a static
  marketing page to a proxied API + a different static build.
- `infra/backend-bootstrap/` — the shared state bucket
  `vibesense-tfstate-839287955684` (us-east-1) both stacks write to, under
  separate keys.
- `vibes_bot/Caddyfile:41` — current `admin.apivibesensemiddleware.click`
  block: `tls marlonreghert@gmail.com` (Let's Encrypt via Caddy's own
  ACME client) + `reverse_proxy admin:9000`. This implies DNS for
  `admin.apivibesensemiddleware.click` currently points directly at the EC2
  host's public IP so Caddy's ACME challenge can complete.
- `vibes_bot/docker-compose.yml` — `admin` service, profile `admin`, port
  9000, gunicorn/uvicorn. Unchanged by this plan.
- No Route53 hosted zone or other DNS-automation references to
  `apivibesensemiddleware.click` found anywhere in this repo or `vibes_bot`.

## Current Behavior
`admin.apivibesensemiddleware.click` DNS resolves straight to the EC2 host.
Caddy holds the TLS cert for that hostname and reverse-proxies everything —
both the static `admin.html` page and every `/api/*`, `/login`, `/logout`,
`/health` call — to the `admin:9000` container. There is one origin, one
hostname, no CDN, no S3.

## Desired Behavior
`admin.apivibesensemiddleware.click` resolves to a CloudFront distribution.
CloudFront serves the React build's static assets (JS/CSS/index.html) from a
private S3 bucket for the default `/*` behavior, and forwards `/login`,
`/logout`, `/health`, and `/api/*` to a **custom origin** that is Caddy on
the same EC2 host as today — reachable at a **new, origin-only hostname**
(`admin-origin.apivibesensemiddleware.click`) so Caddy can keep its own
Let's-Encrypt-issued cert independent of the public-facing CloudFront cert.
End users and the browser only ever see `admin.apivibesensemiddleware.click`;
the app's `fetch()` calls stay relative (same origin), so the existing
`httponly`/`samesite=lax` session cookie keeps working unmodified.

## Implementation Approach
New Terraform stack at `infra/admin/` (own state key, isolated from
`landing/` and `rds/`), mirroring `infra/landing/`'s structure:

- `aws_s3_bucket.site` — `vibesense-admin-${account_id}`, versioned, all
  public access blocked (same as `infra/landing/main.tf`'s bucket).
- `aws_s3_bucket_public_access_block` + `aws_cloudfront_origin_access_control`
  — bucket readable only via CloudFront OAC.
- No `aws_s3_object` `for_each`-over-`fileset` upload block like `landing/`
  uses: the admin app will be redeployed far more often than the landing
  page, so object upload is **not** Terraform's job here — the `vibes_bot`
  plan owns a deploy step (`aws s3 sync` + CloudFront invalidation) that
  runs independently of `terraform apply`. Terraform only owns the bucket
  and its policy, so infra changes and app deploys don't fight over drift.
- `aws_acm_certificate` (us-east-1, DNS-validated) for
  `admin.apivibesensemiddleware.click` — single alias, no apex/www split
  like `landing/` needed (this is already a subdomain).
- `aws_cloudfront_distribution`:
  - Origin 1: the S3 bucket via OAC (default cache behavior, `/*`).
  - Origin 2: custom origin `admin-origin.apivibesensemiddleware.click`,
    HTTPS-only, forwarding to Caddy/`admin:9000` unchanged. Ordered cache
    behaviors for `/login`, `/logout`, `/health`, `/api/*` target this
    origin with the `CachingDisabled` managed policy and forward all
    headers/cookies/query strings (session auth and JSON bodies must pass
    through untouched).
  - `default_root_object = "index.html"`. No SPA 404→index.html rewrite is
    needed: the current admin UI (and its React port) is a single
    unrouted page that switches views with JS state, not client-side URL
    routes, so there's nothing else under `/*` to redirect.
- Terraform backend: same state bucket, key `admin/terraform.tfstate` (new,
  isolated key — an `admin` apply can never plan a change to `landing` or
  `rds`).

## Data, Config, And API Impact
None on cs-server's own application, DB, or Redis. This is AWS infrastructure
only, in a repo that already owns IaC (per `plans/260706_landing-site-and-
tfstate-s3.md`'s precedent that AWS infra changes not tied to the RDS→Redis
data flow live under `infra/`, not the mobile/vibes_bot/cs-server routing
table). Cross-repo contract with `vibes_bot` (bucket name, origin hostname,
distribution domain) is recorded in the wrapper coordination plan.

## Error Handling And Observability
No new runtime path in cs-server's application. Operational risk is the DNS
cutover itself (see Open Questions) — until the operator adds the
`admin-origin.apivibesensemiddleware.click` A record and re-points
`admin.apivibesensemiddleware.click` to the CloudFront domain, `terraform
apply` succeeds but the distribution's non-default behaviors 502 (origin
unreachable by name). Verification below catches this before it's called done.

## Test Plan
# bdd-exempt: infrastructure-only change (Terraform: S3 bucket, CloudFront
distribution, ACM cert). No cs-server FastAPI route, pipeline, or persistence
behavior changes — nothing for `tests/bdd/` to exercise.

Pytest unit tests: None (no application code).

Manual or integration checks:
- `terraform validate` and `terraform fmt -check` pass in `infra/admin/`.
- Two-phase apply, mirroring `infra/landing/`'s runbook: Phase A
  (`-target=aws_acm_certificate.site`) → add ACM validation CNAME at the
  `apivibesensemiddleware.click` registrar → wait for `ISSUED` → Phase B
  (full apply).
- After the operator adds the `admin-origin.apivibesensemiddleware.click` A
  record (→ the same EC2 IP Caddy already answers on) and vibes_bot's
  Caddyfile change ships (companion plan), confirm
  `curl -sSI https://admin-origin.apivibesensemiddleware.click/health`
  returns `200` with a valid cert.
- After the operator re-points `admin.apivibesensemiddleware.click` to the
  CloudFront domain, confirm `curl -sSI https://admin.apivibesensemiddleware.click/`
  returns the S3-served `index.html`, and
  `curl -sSI https://admin.apivibesensemiddleware.click/health` returns `200`
  from the proxied origin (proves both cache behaviors route correctly).

## Acceptance Criteria
- `terraform plan` in `infra/admin/` shows no changes to `infra/landing/` or
  `infra/rds/` state (separate keys, confirmed clean).
- Private S3 bucket exists, publicly unreadable, readable only via the
  CloudFront OAC.
- CloudFront distribution serves `/*` from S3 and `/login`, `/logout`,
  `/health`, `/api/*` from the `admin-origin.apivibesensemiddleware.click`
  custom origin, both over a valid ACM cert for
  `admin.apivibesensemiddleware.click`.
- `outputs.tf` exposes the bucket name, the CloudFront distribution ID and
  domain name, and the ACM validation records — the values the `vibes_bot`
  deploy step and the operator's DNS changes both need.

## Open Questions
- ~~Registrar/DNS host for `apivibesensemiddleware.click` is unknown~~ —
  **resolved during execution**: `dig +short apivibesensemiddleware.click NS`
  returns `awsdns-*` nameservers, i.e. the zone is Route53-hosted, not at an
  external registrar like `vibesense.live`'s GoDaddy. This let the stack drop
  `infra/landing/`'s two-phase manual-CNAME apply — ACM validation is fully
  Terraform-automated via `aws_route53_record`. **Residual, apply-time-only
  check**: confirm the zone is in *this* AWS account (839287955684), not just
  Route53-hosted somewhere — `aws sso login --profile vibesense` was expired
  during planning/execution, so this wasn't queried live. See
  `infra/admin/README.md` Prerequisites; the stack's `data
  "aws_route53_zone"` lookup fails loudly at apply time if it's wrong, it
  will not silently misconfigure anything.
- ~~Confirm the EC2 host's current public IP~~ — **resolved**: `dig +short`
  against `admin.`, `vibesbot.`, and `grafana.apivibesensemiddleware.click`
  all resolve to `54.145.54.177` today, so that's the address the new
  `admin-origin.apivibesensemiddleware.click` record targets
  (`infra/admin/variables.tf`'s `origin_ip`). **Residual, apply-time-only
  check**: confirm it's allocated as an Elastic IP (stable across instance
  restarts) rather than an ephemeral public IP — see `infra/admin/README.md`
  Prerequisites.
