# Auto-loaded by Terraform. Committed on purpose: none of this is secret, and a
# `-var` flag typed at the console is not reproducible — the next operator (or
# the next agent) would re-apply without the attachment and not notice until a
# run had already paid Apify for scrapes it could not store.

# The EC2 instance role cs-server actually runs as. Confirmed 2026-08-16:
#
#   instance i-0893fb6d283243480 ("vibes-bot")
#     -> instance-profile/ec2-ssm-profile
#       -> role ec2-ssm-role
#         -> attached: vibesense-datalake-writer, AmazonSSMManagedInstanceCore
#
# The datalake writer policy hangs off this same role, which is the pattern this
# stack follows: a NEW, separately-named policy attached alongside it. The
# datalake policy itself is never read, renamed or re-described — its
# `description` is immutable in AWS, and editing that resource forces a
# destroy/recreate window in which BestTime flushes are dropped, not retried.
#
# This matters more than it looks: `VenueMediaStore` is wired with no access
# keys (app/container.py), so boto3 falls back to the default credential chain
# and, on EC2, that is this instance profile. Without the attachment every
# upload fails with AccessDenied *after* the Apify scrape has already been
# billed — the exact ordering failure infra/media/README.md's "Verify BEFORE
# enabling the job" section exists to prevent.
cs_server_role_name = "ec2-ssm-role"
