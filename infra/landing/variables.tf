variable "aws_profile" {
  description = "Local AWS SSO profile used for apply."
  type        = string
  default     = "vibesense"
}

variable "domain" {
  description = "Apex domain the company owns (registered at GoDaddy)."
  type        = string
  default     = "vibesense.live"
}

variable "subdomain" {
  description = "Canonical host the site is served from. Must be a subdomain so it can be CNAME'd at GoDaddy (apex cannot CNAME to CloudFront)."
  type        = string
  default     = "www.vibesense.live"
}

variable "tags" {
  type    = map(string)
  default = { project = "vibesense", component = "landing-site" }
}

variable "release_repository" {
  description = "GitHub repository allowed to assume the landing release role, as owner/name."
  type        = string
  default     = "marlonreghert/cs-server"
}

variable "release_branch" {
  description = "The only branch whose workflow runs may assume the landing release role."
  type        = string
  default     = "main"
}
