variable "region" {
  description = "AWS region — MUST match the cs-server EC2's region (co-locate so archival PUTs stay in-region: lower latency, no cross-region transfer)."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "Local AWS SSO profile used for apply."
  type        = string
  default     = "vibesense"
}

# ── Credentials: role, never keys ────────────────────────────────────────────
# An EC2 can hold only ONE instance profile, and the cs-server instance was
# created outside Terraform. So:
#   - instance already has a role  -> set this to its name; the writer policy is
#     attached to it and nothing else changes.
#   - instance has no role         -> leave empty; this stack creates the role +
#     instance profile and you attach it once by hand (see README).
# Either way there is no long-lived access key: the app resolves the EC2
# instance role through the default credential chain.
variable "existing_instance_role_name" {
  description = "Name of the EC2 instance role to attach the writer policy to. Empty = create a new role + instance profile."
  type        = string
  default     = ""
}

# ── Storage class ────────────────────────────────────────────────────────────
# Everything stays in S3 STANDARD. Deliberately no Glacier / Intelligent-Tiering
# transitions: this data exists to be queried, and archival tiers either add
# restore latency (Glacier Flexible/Deep Archive) or per-object monitoring fees
# that cost MORE on small objects (Intelligent-Tiering).
#
# The one remaining lever, off by default: One Zone-IA drops from 3-AZ to
# single-AZ redundancy for ~$0.013/GB-month less. At this volume that is a few
# cents a month, and live-busyness observations cannot be re-fetched if that AZ
# is lost — so the default keeps full durability.
variable "onezone_ia_transition_days" {
  description = "Days after which raw/ objects transition to One Zone-IA. 0 = never (everything stays in S3 Standard)."
  type        = number
  default     = 0
}

# ── Optional VPC gateway endpoint for S3 ─────────────────────────────────────
# Free, and worth having: keeps archival traffic off the public internet and
# avoids NAT data-processing charges if the EC2 egresses through a NAT gateway.
# Leave empty to skip (e.g. the instance is in a public subnet).
variable "vpc_id" {
  description = "VPC to attach the S3 gateway endpoint to. Empty = do not create the endpoint."
  type        = string
  default     = ""
}

variable "route_table_ids" {
  description = "Route tables the S3 gateway endpoint is associated with. Required when vpc_id is set."
  type        = list(string)
  default     = []
}

variable "tags" {
  type    = map(string)
  default = { project = "vibesense", component = "datalake" }
}
