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

import hashlib
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
from app.db.models.enums import MembershipStatus
from app.db.models.knowledge import Document
from app.db.models.media import OCCUPYING_STORAGE_STATES, MessageMedia
from app.db.models.membership import Membership
from app.db.models.usage import UsageEventType
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.repositories.usage_repository import UsageEventRepository
from app.services.usage_service import UsageRecorder

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


# PostgreSQL advisory locks take two 32-bit integers. The first is a namespace
# constant so this application's locks cannot collide with anything else using
# the same mechanism on the same database; the second identifies the workspace
# and the limit being consumed.
_ADVISORY_NAMESPACE: Final = 0x5741_534C  # "WASL"


def _lock_id(tenant_id: uuid.UUID, key: LimitKey) -> int:
    """A stable 32-bit identifier for one workspace's hold on one limit.

    Signed, because PostgreSQL's advisory lock functions take `int4`. The
    derivation only has to be stable and well spread - a collision between two
    unrelated (workspace, limit) pairs costs a little needless serialisation
    and never a wrong answer, because the check under the lock is still the
    real one.
    """
    digest = hashlib.blake2b(
        f"{tenant_id}:{key.value}".encode(),
        digest_size=4,
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


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

        stored = await self._subscriptions.get()
        # A subscription only grants its plan while it is *serving*. Before this
        # check existed, `SERVING_STATUSES` was defined, exported and read by
        # nothing: a cancelled or expired subscription still resolved its plan,
        # so a workspace that cancelled an expensive plan kept that plan's
        # limits for as long as the row existed. Cancelling was a way to keep
        # the entitlements and stop the invoices.
        #
        # `PAST_DUE` is deliberately inside the serving set (see the model): a
        # failed payment is a conversation, not a cut-off, and the platform
        # decides separately when that grace has run out.
        subscription = stored if stored is not None and stored.is_serving else None

        plan: Plan | None = None
        if subscription is not None:
            plan = await self._plans.get_by_id(subscription.plan_id)
        elif self._default_plan_code:
            # Including the case just filtered out. A workspace whose
            # subscription has ended falls back to the default plan rather than
            # losing access outright - the product keeps working at free-tier
            # limits, which is what "cancelled" should mean and is a great deal
            # easier to recover from than a lockout.
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

    async def consume(
        self,
        key: LimitKey,
        *,
        event_type: UsageEventType,
        amount: int = 1,
    ) -> Entitlement:
        """Reserve `amount` against a limit and record it, atomically.

        The primitive every limit should use before doing something the plan
        pays for. :meth:`check` and :meth:`require` answer a question; this one
        takes the allowance, and the difference matters exactly when two
        workers ask at once.

        **Why a lock at all.** `check` reads a total and the caller then writes
        to it, which is a read-then-act sequence over a value another
        transaction may be changing. Two workers holding the last remaining
        request both read "one left", both are told yes, and both spend it. The
        window is small and entirely real: it is open for the length of a
        database round trip, on the path taken by every provider call this
        product makes.

        **Why an advisory lock rather than SERIALIZABLE or a counter table.**
        `usage_events` is append-only and is the single source of truth for
        what a workspace has spent (ADR-030). A counter column beside it would
        be a second source that can disagree, and disagreeing about billing is
        worse than serialising. SERIALIZABLE would push retry handling into
        every caller for a conflict that is rare. An advisory lock keyed on
        (workspace, limit) serialises only the workspaces actually contending,
        leaves the data model alone, and is released by the transaction ending
        whether it commits or aborts - there is no lock to leak.

        **Hold it briefly.** The lock lives until this transaction ends, so a
        caller must not keep the transaction open across slow work. The agent
        worker reserves in a short transaction of its own for exactly this
        reason: holding a workspace's lock across an inference would serialise
        every conversation that workspace is having.

        Returns the entitlement. When `allowed` is false nothing was recorded
        and the caller must not proceed; usage is append-only, so there is no
        refund for work that is reserved and then abandoned.
        """
        if key not in PERIOD_METERS:
            # Resource limits count rows that already exist - agents, numbers,
            # seats - so there is no meter to increment and nothing to reserve.
            # Those callers want `require`, and saying so is better than
            # silently locking and recording nothing.
            raise ValueError(f"{key.value} is a resource limit and cannot be consumed")
        if event_type not in PERIOD_METERS[key]:
            raise ValueError(f"{event_type.value} does not count toward {key.value}")
        if amount <= 0:
            return await self.check(key, additional=0)

        await self._session.execute(
            select(func.pg_advisory_xact_lock(_lock_id(self._tenant_id, key)))
        )

        entitlement = await self.check(key, additional=amount)
        if not entitlement.allowed:
            logger.info(
                "billing.reservation_refused",
                extra={
                    "event": "billing.reservation_refused",
                    "tenant_id": str(self._tenant_id),
                    "key": key.value,
                },
            )
            return entitlement

        # The caller names the meter rather than the key implying it: a key can
        # be fed by more than one meter - `PERIOD_MESSAGES` counts sent *and*
        # received - and incrementing all of them for one event would bill a
        # workspace twice for a message it only sent once.
        UsageRecorder(self._session, tenant_id=self._tenant_id).record(event_type, quantity=amount)
        # Flushed inside the lock so the next holder's count sees it. Without
        # this the row would still be pending in this session and the whole
        # exercise would serialise nothing.
        await self._session.flush()
        return entitlement

    async def reserve(self, key: LimitKey, *, additional: int) -> Entitlement:
        """Take this workspace's lock on a capacity limit and answer under it.

        The sibling of :meth:`consume`, for the limits whose ledger is the rows
        themselves rather than `usage_events`. `consume` records what it
        reserved; this cannot, because what occupies storage capacity is a
        media row somebody else is about to write - and writing it here would
        put the media protocol inside the entitlement service.

        So the contract is stated rather than implied: **the caller writes the
        occupying row in this same transaction.** The advisory lock is held
        until that transaction ends, which is what makes the next caller's
        `SUM` see it. Two uploads racing for the last megabyte serialise here,
        the first commits its intent, and the second counts those bytes and is
        refused.

        A caller that takes this and then commits nothing has serialised for
        nothing and granted nothing, which is the safe direction. A caller that
        holds the transaction open across a slow write holds the workspace's
        lock with it - the media path commits the intent immediately and does
        the object write outside the transaction, for exactly that reason
        (ADR-080, ADR-087).
        """
        if key not in RESOURCE_LIMITS:
            # A period limit's ledger is `usage_events`, and reserving against
            # it without recording anything would grant an allowance nobody
            # spent. Those callers want `consume`.
            raise ValueError(f"{key.value} is a period limit; use consume()")
        if additional <= 0:
            return await self.check(key, additional=0)

        await self._session.execute(
            select(func.pg_advisory_xact_lock(_lock_id(self._tenant_id, key)))
        )

        entitlement = await self.check(key, additional=additional)
        if not entitlement.allowed:
            logger.info(
                "billing.reservation_refused",
                extra={
                    "event": "billing.reservation_refused",
                    "tenant_id": str(self._tenant_id),
                    "key": key.value,
                },
            )
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

        The rule is "does this still occupy something?", not "does a row
        exist". Three cases where those differ:

        A **disabled number** is connected to nothing, and a **released** one
        has been handed back to the platform (ADR-037) - the row survives only
        because a customer's conversations hang off it. Charging for either
        would be charging for nothing, and counting a released number would
        make giving a number up cost a slot forever.

        A **revoked membership** is somebody who no longer has access
        (ADR-038). Counting it would mean a workspace on a two-seat plan that
        removed a colleague could never hire a replacement - the seat would be
        consumed by a person who cannot sign in. Worse, it turns removal into a
        one-way door: the fix is an upgrade, for capacity nobody is using.

        Agents and documents are counted whatever their state, and the
        asymmetry is deliberate - a draft agent is still a configured agent,
        and a limit that ignored them would be satisfied by twenty agents
        somebody toggles.
        """
        statement: Select[tuple[int]]
        match key:
            case LimitKey.WHATSAPP_NUMBERS:
                statement = (
                    select(func.count())
                    .select_from(WhatsAppAccount)
                    .where(WhatsAppAccount.tenant_id == self._tenant_id)
                    .where(WhatsAppAccount.status != WhatsAppAccountStatus.DISABLED)
                    # Belt and braces with the status check above: `released_at`
                    # is what the uniqueness index reads, so it is the column
                    # that decides whether the number is actually held.
                    .where(WhatsAppAccount.released_at.is_(None))
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
                    .where(Membership.status == MembershipStatus.ACTIVE)
                )
            case LimitKey.KNOWLEDGE_DOCUMENTS:
                statement = (
                    select(func.count())
                    .select_from(Document)
                    .where(Document.tenant_id == self._tenant_id)
                )
            case LimitKey.STORAGE_BYTES:
                # A SUM rather than a COUNT, and over the media rows that still
                # name an object rather than over `usage_events`.
                #
                # `usage_events` is authoritative for what a workspace has
                # *consumed* and is append-only by design (ADR-030), so
                # `STORAGE_USED` records bytes when they are written and never
                # subtracts when retention deletes them. That is the right
                # shape for a meter and the wrong shape for a capacity: a
                # workspace that uploaded a gigabyte and purged it has consumed
                # a gigabyte and is holding nothing.
                #
                # So capacity is read from the rows themselves, which are the
                # only durable record of what is currently held. No second
                # counter, nothing to drift, and nothing to reconcile - a state
                # transition that frees the object frees the capacity in the
                # same statement.
                statement = select(func.coalesce(func.sum(MessageMedia.byte_size), 0)).where(
                    MessageMedia.tenant_id == self._tenant_id,
                    MessageMedia.storage_state.in_(OCCUPYING_STORAGE_STATES),
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
