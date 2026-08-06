"""Central configuration for the publications pipeline.

To adapt this pipeline for a different author, change only this file.
"""

AUTHOR_NAME = "Leshem Choshen"
SCHOLAR_USER_ID = "8b8IhUYAAAAJ"

# Optional. Semantic Scholar's unauthenticated access is a rate-limit pool shared
# with every other anonymous caller, so a long run gets throttled almost at once;
# a free key gets 1 request/second reserved and actually completes. It matters
# more than it looks, because the ACL Anthology and OpenReview are both reached
# through Semantic Scholar. Request one at
# https://www.semanticscholar.org/product/api -- or leave this empty and set the
# S2_API_KEY environment variable instead.
S2_API_KEY = ""

# Optional. Sent to OpenAlex as `mailto`, which puts requests in its faster
# "polite pool". Nothing breaks without it.
CONTACT_EMAIL = ""
