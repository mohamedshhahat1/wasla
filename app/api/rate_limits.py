"""Where rate limits are applied, and to whom.

Three policies, and the identity each counts by is the decision that matters
more than the numbers (ADR-032).

**Authentication** counts by client address. The caller has no identity yet -
that is what they are trying to establish - so the only thing to count is where
the request came from. It is the weakest identity in the system, and it is still
the right one here: the traffic this stops is a script trying passwords, and a
script has an address.

**Workspace traffic** counts by workspace, not by user. The limit protects
shared platform resources, and a workspace with fifty colleagues legitimately
generates fifty times the load of one with a single person - counting per user
would let a large customer take the platform down while every individual stayed
politely under their limit.

**Campaigns and template syncs** count by workspace too, on their own much
smaller budget, because each request is expensive to serve and rare to make.

And one absence, which is the most important line here: **the WhatsApp webhook
has no limiter at all.** Meta retries anything that is not a 2xx and eventually
disables a subscription that keeps failing, so a 429 there does not shed load -
it loses a customer's message and then the integration.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request

from app.api.dependencies import ActiveWorkspace, ActiveWorkspaceDep
from app.core.dependencies import RedisDep, SettingsDep
from app.core.rate_limit import RateLimitDecision, RateLimiter, RateLimitPolicy

# When a request arrives with no usable client address at all - an ASGI
# transport that reports none, which is what the test client does - it is
# counted under one shared identity rather than skipped. Skipping would make
# "no address" the way around the limit.
UNKNOWN_CLIENT = "unknown"


def client_identity(request: Request) -> str:
    """The address a request came from, as far as this process can tell.

    `X-Forwarded-For` is read only because the deployment puts nginx in front
    (`proxy_set_header X-Forwarded-For`), and the first entry is the client the
    proxy saw. Behind no proxy the header is attacker-controlled and this is
    weaker than it looks - which is why it limits *authentication attempts*
    rather than authorising anything.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = request.client
    return client.host if client and client.host else UNKNOWN_CLIENT


def get_rate_limiter(redis: RedisDep) -> RateLimiter:
    return RateLimiter(redis)


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def _limit_by_client(
    name: str, per_minute: Callable[[SettingsDep], int]
) -> Callable[..., Awaitable[RateLimitDecision | None]]:
    """Build a dependency limiting by client address."""

    async def guard(
        request: Request,
        settings: SettingsDep,
        limiter: RateLimiterDep,
    ) -> RateLimitDecision | None:
        if not settings.rate_limit_enabled:
            return None
        policy = RateLimitPolicy(
            name=name,
            limit=per_minute(settings),
            window_seconds=60,
        )
        return await limiter.enforce(policy, client_identity(request))

    return guard


def _limit_by_workspace(
    name: str, per_minute: Callable[[SettingsDep], int]
) -> Callable[..., Awaitable[RateLimitDecision | None]]:
    """Build a dependency limiting by workspace.

    Resolving the workspace runs the whole authentication chain first, which is
    correct: an unauthenticated request should be refused for being
    unauthenticated, not for being frequent.
    """

    async def guard(
        workspace: ActiveWorkspaceDep,
        settings: SettingsDep,
        limiter: RateLimiterDep,
    ) -> RateLimitDecision | None:
        if not settings.rate_limit_enabled:
            return None
        policy = RateLimitPolicy(
            name=name,
            limit=per_minute(settings),
            window_seconds=60,
        )
        return await limiter.enforce(policy, str(workspace.tenant.id))

    return guard


# The guards themselves, named so a router can depend on one directly. A route
# uses the annotated alias below; a whole router uses the function.
auth_rate_limit = _limit_by_client(
    "auth",
    lambda settings: settings.rate_limit_auth_per_minute,
)
workspace_rate_limit = _limit_by_workspace(
    "workspace",
    lambda settings: settings.rate_limit_workspace_per_minute,
)
campaign_rate_limit = _limit_by_workspace(
    "campaign",
    lambda settings: settings.rate_limit_campaign_per_minute,
)

AuthRateLimit = Annotated[RateLimitDecision | None, Depends(auth_rate_limit)]
WorkspaceRateLimit = Annotated[RateLimitDecision | None, Depends(workspace_rate_limit)]
CampaignRateLimit = Annotated[RateLimitDecision | None, Depends(campaign_rate_limit)]

__all__ = [
    "ActiveWorkspace",
    "AuthRateLimit",
    "CampaignRateLimit",
    "RateLimiterDep",
    "WorkspaceRateLimit",
    "auth_rate_limit",
    "campaign_rate_limit",
    "client_identity",
    "workspace_rate_limit",
]
