"""The one authority on "is this workspace allowed to do that".

Every limit question in the product comes here, and nothing outside this module
knows what a plan contains. That is the whole point of the phase: a limit
compared inline somewhere is a limit that will disagree with the plan a customer
is paying for, and nobody will notice until they complain.

Two kinds of question, answered by two different queries:

- **Resource limits** count rows that exist now - connected numbers, agents,
  colleagues, documents. A workspace over one stays over it until something is
  deleted, which is the correct behaviour: downgrading a plan does not delete
  anybody's work, it stops them adding more.
- **Period limits** count what was consumed since `current_period_start`, read
  from `usage_events`. They reset when the period rolls over, which is what
  makes "1,000 messages a month" mean anything at all.

What happens when a workspace has no subscription is a decision, not an
oversight. It is treated as being on the configured default plan, because every
workspace that predates billing has none and a product that stopped working for
them the day this shipped would be a worse outcome than any limit. If that plan
is missing too, limits are **not enforced** and the fact is logged at warning:
taking a working deployment offline over an absent catalogue row is not a
failure mode a limit check should have.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PlanLimitExceededError
from app.core.logging import get_logger
from app.db.models.agent import Agent
from app.db.models.billing import (
    RESOURCE_LIMITS,
    LimitKey,
    Plan,
    Subscription,
)
from app.db.models.knowledge import Document
from app.db.models.membership import Membership
from app.db.models.usage import UsageEventType
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.repositories.usage_repository import UsageEventRepository

logger = get_logger(__name__)

# Which meters each period limit adds up. Messages count in both directions:
# a conversation is two-sided, every WhatsApp platform prices it that way, and
# counting only what the business sends would let a workspace be billed nothing
# for a hundred thousand inbound messages it still had to store and process.
PERIOD_METERS: Final[dict[LimitKey, tuple[UsageEventType, ...]]] = {
    LimitKey.PERIOD_MESSAGES: (
        UsageEventType.WHATSAPP_MESSAGE_SENT,
        UsageEventType.WHATSAPP_MESSAGE_RECEIVED,
    ),
    LimitKey.PERIOD_AI_REQUESTS: (UsageEventType.AI_REQUEST,),
    LimitKey.PERIOD_CAMPAIGN_MESSAGES: (UsageEventType.CAMPAIGN_MESSAGE,),
}


@dataclass(frozen=True, slots=True)
class Entitlement:
    """What a workspace is allowed for one key, and where it currently stands.

    `limit` is None for unlimited. `remaining` is None for the same reason
    rather than a large number, because a client that renders "999999 left" has
    been told something false.
    """

    key: LimitKey
    limit: int | None
    used: int
    allowed: bool = True
    plan_code: str | None = None

    @property
    def is_unlimited(self) -> bool:
        return self.limit is None

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(self.limit - self.used, 0)


def _refusal(entitlement: Entitlement) -> str:
    """A message that tells somebody what to do, not merely what went wrong."""
    noun = entitlement.key.value.removeprefix("period_").replace("_", " ")
    return (
        f"This workspace's plan allows {entitlement.limit} {noun}"
        + (" per billing period" if entitlement.key not in RESOURCE_LIMITS else "")
        + f", and {entitlement.used} have been used. Upgrade the plan to continue."
    )


class EntitlementService:
    """Answers limit questions for one workspace."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        default_plan_code: str | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._default_plan_code = default_plan_code
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._plans = PlanRepository(session)
        self._usage = UsageEventRepository(session, tenant_id=tenant_id)
        # Resolved at most once per request: every check needs the same plan,
        # and a page rendering five of them should not read it five times.
        self._resolved: tuple[Plan | None, Subscription | None] | None = None

    async def _resolve(self) -> tuple[Plan | None, Subscription | None]:
        if self._resolved is not None:
            return self._resolved

        subscription = await self._subscriptions.get()
        plan: Plan | None = None
        if subscription is not None:
            plan = await self._plans.get_by_id(subscription.plan_id)
        elif self._default_plan_code:
            plan = await self._plans.get_by_code(self._default_plan_code)

        if plan is None:
            logger.warning(
                "billing.no_plan_resolved",
                extra={
                    "event": "billing.no_plan_resolved",
                    "tenant_id": str(self._tenant_id),
                },
            )
        self._resolved = (plan, subscription)
        return self._resolved

    async def check(self, key: LimitKey, *, additional: int = 1) -> Entitlement:
        """Where this workspace stands against one limit.

        `additional` is what the caller is about to do. Asking "may I add one
        more" and "am I at the limit" are different questions at the boundary,
        and only the caller knows which one it means.
        """
        plan, subscription = await self._resolve()
        if plan is None:
            # Unenforced, and already logged in `_resolve`.
            return Entitlement(key=key, limit=None, used=0, allowed=True)

        limit = plan.limit_for(key)
        used = await self._used(key, subscription=subscription)
        allowed = limit is None or used + max(additional, 0) <= limit
        return Entitlement(
            key=key,
            limit=limit,
            used=used,
            allowed=allowed,
            plan_code=plan.code,
        )

    async def require(self, key: LimitKey, *, additional: int = 1) -> Entitlement:
        """Refuse the action if the plan does not allow it.

        Raises `PlanLimitExceededError`, which answers 402 rather than 403: a
        permission error tells a caller to ask an administrator, and this one
        tells them to upgrade.
        """
        entitlement = await self.check(key, additional=additional)
        if not entitlement.allowed:
            logger.info(
                "billing.limit_refused",
                extra={
                    "event": "billing.limit_refused",
                    "tenant_id": str(self._tenant_id),
                    "limit": key.value,
                    "used": entitlement.used,
                },
            )
            raise PlanLimitExceededError(_refusal(entitlement))
        return entitlement

    async def allows(self, key: LimitKey, *, additional: int = 1) -> bool:
        """Whether the action is allowed, without raising.

        For the callers that must not fail loudly - a worker deciding whether to
        run an agent turn is not a request anybody is waiting on an error from.
        """
        return (await self.check(key, additional=additional)).allowed

    async def snapshot(self, keys: Iterable[LimitKey] | None = None) -> list[Entitlement]:
        """Every limit and its current standing, for a settings page."""
        selected = list(keys) if keys is not None else list(LimitKey)
        return [await self.check(key, additional=0) for key in selected]

    async def _used(self, key: LimitKey, *, subscription: Subscription | None) -> int:
        if key in RESOURCE_LIMITS:
            return await self._resource_count(key)
        return await self._period_usage(key, subscription=subscription)

    async def _resource_count(self, key: LimitKey) -> int:
        """How many of this resource the workspace has right now.

        A disabled *number* does not occupy a slot: it is connected to nothing,
        and charging for it would be charging for nothing. Agents and documents
        are counted whatever their state, and the asymmetry is deliberate - a
        draft agent is still a configured agent, and a limit that ignored them
        would be satisfied by twenty agents somebody toggles.
        """
        statement: Select[tuple[int]]
        match key:
            case LimitKey.WHATSAPP_NUMBERS:
                statement = (
                    select(func.count())
                    .select_from(WhatsAppAccount)
                    .where(WhatsAppAccount.tenant_id == self._tenant_id)
                    .where(WhatsAppAccount.status != WhatsAppAccountStatus.DISABLED)
                )
            case LimitKey.AGENTS:
                statement = (
                    select(func.count())
                    .select_from(Agent)
                    .where(Agent.tenant_id == self._tenant_id)
                )
            case LimitKey.TEAM_MEMBERS:
                statement = (
                    select(func.count())
                    .select_from(Membership)
                    .where(Membership.tenant_id == self._tenant_id)
                )
            case LimitKey.KNOWLEDGE_DOCUMENTS:
                statement = (
                    select(func.count())
                    .select_from(Document)
                    .where(Document.tenant_id == self._tenant_id)
                )
            case _:  # pragma: no cover - RESOURCE_LIMITS is exhaustive here
                raise ValueError(f"{key} is not a resource limit.")

        return int(await self._session.scalar(statement) or 0)

    async def _period_usage(self, key: LimitKey, *, subscription: Subscription | None) -> int:
        """How much was consumed in the current billing period.

        Without a subscription there is no period, so the window falls back to
        the calendar month. That keeps a limit meaningful for a workspace on the
        default plan instead of summing since the beginning of time, which would
        refuse everybody eventually.
        """
        meters = PERIOD_METERS.get(key)
        if not meters:
            return 0

        since, until = _period(subscription)
        totals = await self._usage.totals(since=since, until=until, event_types=meters)
        return sum(total.quantity for total in totals)


def _period(subscription: Subscription | None) -> tuple[datetime, datetime]:
    """The window a period limit is counted over."""
    if subscription is not None:
        return subscription.current_period_start, subscription.current_period_end

    now = datetime.now(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # The first instant of next month, so the window stays half-open like every
    # other window in this system.
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end
