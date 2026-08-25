"""Version 1 of the API.

Rate limits are attached here rather than route by route, because *which*
routers are limited is the decision worth being able to read in one place
(ADR-032). Three groups:

- **Workspace routers** carry the per-workspace limit. Everything a signed-in
  colleague does lands here.
- **Campaign and template routers** carry a second, much smaller budget on top
  of it: each request is expensive to serve and rare to make legitimately.
- **The webhook, authentication and platform routers carry neither**, each for
  its own reason. A webhook must never be refused - a provider retries a
  non-2xx and eventually disables the subscription, so a 429 loses a customer's
  message, or a delivery report, and then the integration. Authentication is
  limited by client address inside its own routes instead, since a caller who
  has not signed in has no workspace to count against. Platform administration
  is a handful of staff.

Both webhook routers are unauthenticated and both defend themselves with an
HMAC over the raw request body rather than with a credential: Meta's
`X-Hub-Signature-256` and Resend's Svix headers respectively.
"""

from fastapi import APIRouter, Depends

from app.api.rate_limits import campaign_rate_limit, workspace_rate_limit
from app.api.route import CommittingRoute
from app.api.v1 import (
    agents,
    analytics,
    audit,
    auth,
    billing,
    campaigns,
    contacts,
    conversations,
    email_verification,
    email_webhooks,
    follow_ups,
    google_oauth,
    invitations,
    invoices,
    knowledge,
    leads,
    members,
    platform,
    templates,
    usage,
    webhooks,
    whatsapp,
)

# Declared once, so the same policy cannot drift between the routers it is
# applied to.
_WORKSPACE_LIMIT = Depends(workspace_rate_limit)
_CAMPAIGN_LIMIT = Depends(campaign_rate_limit)

# Everything a signed-in colleague does.
WORKSPACE_ROUTERS = (
    agents.router,
    analytics.router,
    audit.router,
    billing.router,
    contacts.router,
    conversations.router,
    follow_ups.router,
    invoices.router,
    knowledge.router,
    leads.router,
    members.router,
    usage.router,
    whatsapp.router,
)

# Routers where the limit cannot be applied to the whole router, because not
# every route on them is authenticated. Their limits are declared per route.
#
# `invitations` is the only one, and it is here because of a real defect this
# structure now prevents: applying the workspace limit at router level resolved
# `ActiveWorkspaceDep` - and therefore the entire authentication chain - for
# *every* route on the router, including `/accept`, which exists precisely for
# somebody who has no account yet. Onboarding answered 401 in production while
# a test that overrode the workspace dependency reported it working. A guard
# whose signature pulls in authentication cannot be attached to a group that
# contains an unauthenticated route.
MIXED_ROUTERS = (invitations.router,)

# Expensive and rare: a broadcast, a template sync against Meta.
CAMPAIGN_ROUTERS = (
    campaigns.router,
    templates.router,
)

# Deliberately unlimited *at router level*. See the module docstring - each for
# a different reason, and the webhooks' is the one that would cost customer
# messages and delivery reports.
#
# `email_verification` is here for the reason recorded above `MIXED_ROUTERS`,
# not because it is unguarded. Both its routes require a session, and both
# carry their own per-account limits applied inside the service. What it must
# not carry is the workspace limit: that guard resolves `ActiveWorkspaceDep`,
# so attaching it would make proving your own email address require a selected
# workspace and an active membership - neither of which has anything to do with
# owning an inbox, and both of which a person may legitimately lack.
#
# `google_oauth` is here for the same reason and is emphatically not unguarded:
# every one of its five routes declares `GoogleOAuthRateLimit`, counted by
# client address through the trusted-proxy logic. It cannot take the workspace
# limit because two of its routes are reachable by somebody who has no account
# at all - which is the point of them.
UNLIMITED_ROUTERS = (
    auth.router,
    email_verification.router,
    google_oauth.router,
    platform.router,
    webhooks.router,
    email_webhooks.router,
)

api_router = APIRouter(route_class=CommittingRoute)

for router in WORKSPACE_ROUTERS:
    api_router.include_router(router, dependencies=[_WORKSPACE_LIMIT])

for router in CAMPAIGN_ROUTERS:
    api_router.include_router(router, dependencies=[_WORKSPACE_LIMIT, _CAMPAIGN_LIMIT])

# No router-level dependency: each route declares its own, because one of them
# must stay reachable without credentials.
for router in MIXED_ROUTERS:
    api_router.include_router(router)

for router in UNLIMITED_ROUTERS:
    api_router.include_router(router)

__all__ = ["api_router"]
