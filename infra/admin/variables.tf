variable "aws_profile" {
  description = "Local AWS SSO profile used for apply."
  type        = string
  default     = "vibesense"
}

variable "apex_domain" {
  description = "Registered domain, DNS-hosted in Route53 in this account (confirmed via `dig +short apivibesensemiddleware.click NS` -> awsdns nameservers)."
  type        = string
  default     = "apivibesensemiddleware.click"
}

variable "public_hostname" {
  description = "Public host the admin panel is served from — unchanged from today, only its target moves from the EC2 host to this CloudFront distribution."
  type        = string
  default     = "admin.apivibesensemiddleware.click"
}

variable "origin_hostname" {
  description = <<-EOT
    New origin-only hostname for CloudFront's custom origin. Caddy on the EC2
    host keeps serving `admin:9000` here under its own Let's-Encrypt cert,
    independent of the public-facing ACM cert CloudFront terminates with —
    the public hostname's DNS moving to CloudFront would otherwise break
    Caddy's ability to prove domain ownership for its own cert.
  EOT
  type        = string
  default     = "admin-origin.apivibesensemiddleware.click"
}

variable "origin_ip" {
  description = <<-EOT
    Public IP the EC2 host answers on. Observed via `dig +short
    admin.apivibesensemiddleware.click A` (also confirmed consistent across
    vibesbot.* and grafana.* today) at plan time: 54.145.54.177. CONFIRM this
    is an Elastic IP (stable across instance restarts) before applying —
    if it's an ephemeral public IP, pin an EIP first or this record will
    silently go stale on the next instance replacement.
  EOT
  type        = string
  default     = "54.145.54.177"
}

variable "tags" {
  type    = map(string)
  default = { project = "vibesense", component = "admin-panel" }
}
