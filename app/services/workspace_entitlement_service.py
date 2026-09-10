"""How many workspaces one account may own.

The only entitlement in the product that belongs to a **person** rather than to
a workspace, which is why it is not in `EntitlementService`. That service
resolves one tenant's subscription and counts one tenant's rows; this question
spans every tenant somebody owns, and there is no single tenant that could
answer it.

It replaces a global setting. `MAX_OWNED_WORKSPACES_PER_USER = 10` was a
judgement call made when `POST /workspaces` was added and there was no
entitlement mechanism reaching across tenants - a number that applied equally to
a free account and to an enterprise customer, which is not a pricing model, it
is a stand-in for one. The limit now comes from `plans.limits`, alongside every
other thing a plan sells.

## Which plan applies to a person

The question a per-account limit has to answer and a per-workspace limit never
does: somebody who owns three workspaces may be on three different plans.

**The most generous limit among the plans they are actually paying for wins**,
and an absent limit on any of them means unlimited. The alternatives are worse
in ways that show up immediately:

- *The default plan always* makes the entitlement unsellable - upgrading buys
  nothing.
- *The plan of the workspace being created* is circular; it does not exist yet.
- *The lowest* means an enterprise customer who also keeps a free sandbox is
  held to the sandbox's ceiling, and the fix is to delete the sandbox.
- *A designated primary workspace* invents a concept the data model does not
  have, and somebody would then have to choose one.

Taking the highest is the reading a customer expects - "I pay for Business, so I
get Business's workspace allowance" - and it fails safe in the direction of
serving a paying customer rather than refusing one.

**Only serving subscriptions count.** A cancelled or suspended workspace's plan
does not grant an allowance, for exactly the reason `EntitlementService` stopped
honouring one: otherwise cancelling would be a way to keep the entitlement and
stop the invoices.

## What counts against the limit

Live workspaces the account **owns**: active and suspended, never deleted.

- *Suspended counts* because ownership still exists - the workspace, its data
  and its subscription are all still there, and a suspension is usually
  temporary. Excluding it would make suspension a way to create beyond the plan.
- *Deleted does not count*, because nothing is there to own. The slug stays
  reserved, but the workspace is gone.
- *Membership without ownership never counts.* Being a colleague in somebody
  else's business is not something a person's own plan should have to pay for,
  and counting it would let one account's plan limit be exhausted by other
  people's invitations.

## The unresolved half

**The per-plan numbers are not set here, and are not set anywhere yet.** No
seeded plan carries `owned_workspaces`, so every plan is currently unlimited by
the absent-means-unlimited rule, and the only ceiling in force is the technical
one below. That is a deliberate product gap rather than an oversight: choosing
what Starter, Pro and Business allow is a pricing decision, and inventing
numbers here would put a made-up pricing model into the enforcement path where
it would look authoritative. Operators set them in plan data when the product
decides; the mechanism is ready and the values are theirs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.billing import LimitKey, Plan
from app.db.models.user import User
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.repositories.membership_repository import UserMembershipRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WorkspaceAllowance:
    """Where one account stands against its workspace-ownership limit."""

    # None means unlimited, matching `Plan.limit_for` and the absent-key rule.
    limit: int | None
    owned: int
    # Which plan the limit came from, for the error a customer reads. None when
    # no plan resolved at all, which is a misconfigured deployment rather than a
    # customer state.
    plan_code: str | None

    @property
    def allowed(self) -> bool:
        """Whether one more workspace may be created."""
        return self.limit is None or self.owned < self.limit


class WorkspaceEntitlementService:
    """Resolves and enforces the per-account workspace-ownership limit."""

    def __init__(self, session: AsyncSession, *, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings if settings is not None else get_settings()
        self._plans = PlanRepository(session)
        self._memberships = UserMembershipRepository(session)

    async def allowance(self, *, user: User) -> WorkspaceAllowance:
        """What this account is entitled to, and what it is using.

        Two reads and no writes, so it is safe to call for display as well as
        before a creation. `WorkspaceService.create` calls it under the caller's
        transaction with the count taken afterwards - see the note there about
        why the count alone is not what stops a concurrent bypass.
        """
        owned = await self._memberships.list_owned_live_workspaces(user.id)
        plans = await self._plans_for(owned_tenant_ids=[tenant.id for _, tenant in owned])

        limit = self._most_generous(plans)
        plan_code = self._source_plan_code(plans, limit)
        return WorkspaceAllowance(limit=limit, owned=len(owned), plan_code=plan_code)

    async def _plans_for(self, *, owned_tenant_ids: list[uuid.UUID]) -> list[Plan]:
        """The serving plan behind each owned workspace, plus the default.

        The default is always included, so an account that owns nothing - the
        Google-first case, and the common one - still resolves an allowance
        rather than falling through to unlimited by accident. An account whose
        every subscription has lapsed lands in the same place, which is the
        behaviour `EntitlementService` already has for workspace limits.
        """
        plans: list[Plan] = []
        for tenant_id in owned_tenant_ids:
            subscription = await SubscriptionRepository(self._session, tenant_id=tenant_id).get()
            if subscription is None or not subscription.is_serving:
                continue
            plan = await self._plans.get_by_id(subscription.plan_id)
            if plan is not None:
                plans.append(plan)

        if self._settings.default_plan_code:
            fallback = await self._plans.get_by_code(self._settings.default_plan_code)
            if fallback is not None:
                plans.append(fallback)
        return plans

    @staticmethod
    def _most_generous(plans: list[Plan]) -> int | None:
        """The highest ceiling among these plans, or None for unlimited.

        A single plan without the key makes the whole answer unlimited, which is
        the absent-means-unlimited rule applied consistently: "no ceiling" is
        more generous than any number, so it wins a max.

        No plans at all is also unlimited, and that case is a deployment with an
        empty catalogue. It is logged by the caller rather than refused, for the
        reason `EntitlementService` gives: a missing catalogue row must not lock
        customers out of a product they are paying for.
        """
        if not plans:
            return None
        best = 0
        for plan in plans:
            limit = plan.limit_for(LimitKey.OWNED_WORKSPACES)
            if limit is None:
                return None
            best = max(best, limit)
        return best

    @staticmethod
    def _source_plan_code(plans: list[Plan], limit: int | None) -> str | None:
        """Which plan the winning limit came from, for the customer's message."""
        if not plans:
            return None
        if limit is None:
            for plan in plans:
                if plan.limit_for(LimitKey.OWNED_WORKSPACES) is None:
                    return plan.code
            return plans[0].code
        for plan in plans:
            if plan.limit_for(LimitKey.OWNED_WORKSPACES) == limit:
                return plan.code
        return plans[0].code
