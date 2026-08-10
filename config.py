"""Central configuration for the publications pipeline.

To adapt this pipeline for a different author, change only this file.
"""

AUTHOR_NAME = "Leshem Choshen"
SCHOLAR_USER_ID = "8b8IhUYAAAAJ"

# Optional, and do not fill this in here. This file is tracked, and the upstream
# repository is public, so a key typed below is one `git add -A` from being
# published -- tests/test_no_secrets.py fails the build if one ever is. Put it in
#   ~/.config/publications/s2_api_key
# which is outside every worktree and is also the only source the weekly
# unattended run can see. The slot stays for a fork that keeps its config private.
#
# Worth having: Semantic Scholar's unauthenticated access is a rate-limit pool
# shared with every other anonymous caller, so a long run gets throttled almost at
# once, while a free key gets 1 request/second reserved and actually completes. It
# matters more than it looks, because the ACL Anthology and OpenReview are both
# reached through Semantic Scholar. Request one at
# https://www.semanticscholar.org/product/api
S2_API_KEY = ""

# Optional. Sent to OpenAlex as `mailto`, which puts requests in its faster
# "polite pool". Nothing breaks without it.
CONTACT_EMAIL = ""
