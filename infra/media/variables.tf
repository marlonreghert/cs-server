variable "region" {
  description = "AWS region for the media bucket. Co-located with the cs-server EC2 so uploads stay in-region (lower latency, no cross-region transfer)."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "Local AWS SSO profile used for plan/apply."
  type        = string
  default     = "vibesense"
}

variable "apex_domain" {
  description = "Registered domain, DNS-hosted in Route53 in this account. Because the zone is here, ACM DNS validation completes inside a single apply — no phased apply (this is what infra/landing/ needed and infra/admin/ did not)."
  type        = string
  default     = "apivibesensemiddleware.click"
}

variable "public_hostname" {
  description = <<-EOT
    The public host every stored media URL is built from. This value is a
    CONTRACT, not a preference: cs-server bakes it into `photo_url` on every
    `instagram.profile_photo` row, vibes_bot serves those rows, and released
    mobile clients render them. Changing it later strands every URL already
    written, so it is fixed here and mirrored by MEDIA_CDN_BASE_URL in
    cs-server's settings.
  EOT
  type        = string
  default     = "media.apivibesensemiddleware.click"
}

variable "cs_server_role_name" {
  description = <<-EOT
    Name of the EC2 instance role cs-server runs as — the SAME role
    infra/datalake attaches its writer policy to. This stack attaches a NEW,
    separately-named policy to it and never reads, renames, re-describes or
    otherwise touches the datalake policy: `aws_iam_policy.description` is
    immutable in AWS, so editing that resource forces a destroy/recreate window
    in which BestTime data-lake flushes are dropped rather than retried.

    Empty (the default) means "do not manage the attachment here" — the bucket
    and distribution are still created, and the policy is still created, so an
    operator can attach it once by hand. That default exists because the role
    name is deployment-specific: confirm it with
    `aws ec2 describe-instances --profile vibesense --query
    "Reservations[].Instances[].IamInstanceProfile"` before setting it.
  EOT
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = { project = "vibesense", component = "app-media" }
}
