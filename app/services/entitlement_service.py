"""The one authority on "is this workspace allowed to do that".

Every limit question in the product comes here, and nothing outside this module
knows what a plan contains. That is the whole point of the phase: a limit
compared inline somewhere is a limit that will disagree with the plan a customer
is paying for, and nobody will notice until they complain.

Two kinds of question, answered by two different queries:

- **Resource limits** count rows that exist now - channel connections, agents,
  colleagues, documents. A workspace over one stays over it until something is
  deleted, which is the correct behaviour: downgrading a plan does not delete
  anybody's work, it stops them adding more. (Channel capacity alone has a
  resolution beyond that - the owner's selection or, after a week, the oldest
  kept and the rest disabled, never deleted: ENT-14, `channel_capacity_lifecycle`.)
- **Channel policy** - which channel types the plan allows (ENT-09) - is a set,
  not a number, and is read here too.
- **Period limits** count what was consumed in the current *usage cycle*, read
  from `usage_events`. The cycle is always one calendar month - on a yearly
  price as on a monthly one (ADR-116) - so "100,000 messages" on an annual plan
  means 100,000 each month, never twelve months' worth up front and never one
  allowance for the year. The billing term decides what is paid for and when;
  it never decides how much may be used.

**Limits come from the subscription's pinned plan version** (BILL-12), never
from the live `plans` row. A catalogue edit reaches an existing subscriber only
through an explicit migration, so publishing "Pro now allows 3 agents" cannot
silently take two agents' worth of capacity away from somebody who bought 5.

**Every count-based limit is checked under a lock** (BILL-08). Two requests
creating the last agent both used to read "one left" and both succeeded; the
guard now takes a per-workspace advisory lock in the creating transaction, so
the second one counts the first one's row and is refused.

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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PlanLimitExceededError
from app.core.logging import get_logger
from app.db.models.agent import Agent
from app.db.models.agent_turn import AgentTurn, AITurnChargeState
from app.db.models.billing import (
    ACCOUNT_LIMITS,
    RESOURCE_LIMITS,
    TOPUP_LIMITS,
    LimitKey,
    Plan,
    PlanVersion,
    Subscription,
)
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.enums import InvitationStatus, MembershipStatus
from app.db.models.invitation import TenantInvitation
from app.db.models.knowledge import Document
from app.db.models.media import OCCUPYING_STORAGE_STATES, MessageMedia
from app.db.models.membership import Membership
from app.db.models.topup import TopupSource
from app.db.models.usage import UsageEvent, UsageEventType
from app.repositories.billing_repository import PlanRepository, SubscriptionRepository
from app.repositories.topup_repository import ActiveTotal, TopupPurchaseRepository
from app.repositories.usage_repository import UsageEventRepository
from app.services.billing_calendar import current_usage_period
from app.services.channel_fit import ChannelCapacity
from app.services.entitlement_terms import (
    LEGACY_CHANNEL_TYPES,
    term_channel_types,
    term_limit,
    topup_slot_channel,
)
from app.services.plan_catalog import PlanCatalog
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

#: How long an AI turn's hold counts when the caller names no TTL (ENT-03):
#: the deployment's `AI_TURN_HOLD_TTL_SECONDS`, whose default this matches.
DEFAULT_AI_TURN_HOLD_TTL: Final = timedelta(seconds=900)

# Which meters each period limit adds up. Messages count in both directions:
# a conversation is two-sided, every WhatsApp platform prices it that way, and
# counting only what the business sends would let a workspace be billed nothing
# for a hundred thousand inbound messages it still had to store and process.
PERIOD_METERS: Final[dict[LimitKey, tuple[UsageEventType, ...]]] = {
    LimitKey.PERIOD_MESSAGES: (
        UsageEventType.WHATSAPP_MESSAGE_SENT,
        UsageEventType.WHATSAPP_MESSAGE_RECEIVED,
    ),
    # Turns, never provider requests (AI-02). `AI_REQUEST` is still recorded for
    # every call a turn makes - sentiment, each inference round - because that
    # is what the platform pays for; it is cost accounting, and it is not what
    # the customer bought.
    LimitKey.PERIOD_AI_TURNS: (UsageEventType.AI_TURN,),
    LimitKey.PERIOD_CAMPAIGN_MESSAGES: (UsageEventType.CAMPAIGN_MESSAGE,),
}


@dataclass(frozen=True, slots=True)
class Entitlement:
    """What a workspace is allowed for one key, and where it currently stands.

    `limit` is the **effective** limit, and None for unlimited. `remaining` is
    None for the same reason rather than a large number, because a client that
    renders "999999 left" has been told something false.

    The effective limit is built from three parts (ADR-113), each kept so a
    billing page can show where the allowance comes from:

        limit = base_limit + topup_limit + grant_limit

    `base_limit` is the pinned plan version's; `topup_limit` what live paid
    top-ups add; `grant_limit` what live platform grants add. An unlimited base
    stays unlimited - a top-up cannot make unlimited more unlimited.

    `channel_connections` also carries its `capacity` (ENT-05): `limit` is then
    every slot, general and typed, `used` every active connection, and
    `remaining` the general slots left for the next connection of a type with
    no typed slot of its own - which is what a person about to connect one
    needs to know.

    `held` is AI turns engaged and not yet settled (ENT-03): they are not usage
    yet, but they are spoken for, so `remaining` leaves them out.
    """

    key: LimitKey
    limit: int | None
    used: int
    allowed: bool = True
    plan_code: str | None = None
    base_limit: int | None = None
    topup_limit: int = 0
    grant_limit: int = 0
    period_start: datetime | None = None
    period_end: datetime | None = None
    capacity: ChannelCapacity | None = None
    held: int = 0

    @property
    def is_unlimited(self) -> bool:
        return self.limit is None

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        if self.capacity is not None:
            return self.capacity.general_remaining
        return max(self.limit - self.used - self.held, 0)

    @property
    def over_limit(self) -> bool:
        """Whether the workspace already holds or used more than it is allowed.

        The state a capacity reaches when a top-up expires or a plan shrinks:
        nothing is deleted, `remaining` reads zero rather than a negative
        number, and adding more is refused until usage fits again. For channel
        capacity it is the fit rule's answer, typed slots included.
        """
        if self.capacity is not None:
            return self.capacity.over_limit
        return self.limit is not None and self.used > self.limit


def _refusal(entitlement: Entitlement) -> str:
    """A message that tells somebody what to do, not merely what went wrong."""
    noun = entitlement.key.value.removeprefix("period_").replace("_", " ")
    if entitlement.topup_limit or entitlement.grant_limit:
        return (
            f"This workspace's plan and top-ups allow {entitlement.limit} {noun}"
            + (" per monthly usage cycle" if entitlement.key not in RESOURCE_LIMITS else "")
            + f", and {entitlement.used} have been used. Upgrade the plan or buy a "
            "top-up to continue."
        )
    return (
        f"This workspace's plan allows {entitlement.limit} {noun}"
        + (" per monthly usage cycle" if entitlement.key not in RESOURCE_LIMITS else "")
        + f", and {entitlement.used} have been used. Upgrade the plan to continue."
    )


# The single-key form of PostgreSQL's advisory lock, whose key space is
# separate from the two-key form every other lock here uses
# (`app.db.advisory_locks`), so these cannot collide with those. The key is a
# hash of the workspace and the limit being consumed. (A two-key namespace was
# declared here once and never used; it was removed - DB-016.)


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


async def hold_limit_lock(session: AsyncSession, *, tenant_id: uuid.UUID, key: LimitKey) -> None:
    """Take the workspace's advisory lock on one limit until the transaction ends.

    The lock `consume`, `reserve` and `reserve_period` take. A top-up grant
    takes it too (ADR-113), so a grant and the consumption it races with are
    ordered: a turn counted before the grant was checked against the old limit,
    one counted after it against the new, and none against a half-applied one.
    """
    await session.execute(select(func.pg_advisory_xact_lock(_lock_id(tenant_id, key))))


class EntitlementService:
    """Answers limit questions for one workspace."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        default_plan_code: str | None = None,
        clock: Callable[[], datetime] | None = None,
        ai_turn_hold_ttl: timedelta | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._default_plan_code = default_plan_code
        # Past this age an AI turn's hold stops counting (ENT-03): its worker
        # died, and the billing sweep will release it.
        self._hold_ttl = ai_turn_hold_ttl or DEFAULT_AI_TURN_HOLD_TTL
        # What "now" is for top-up expiry. Injected only by tests that need to
        # stand either side of a period boundary; production reads the clock.
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._subscriptions = SubscriptionRepository(session, tenant_id=tenant_id)
        self._plans = PlanRepository(session)
        self._catalog = PlanCatalog(session)
        self._usage = UsageEventRepository(session, tenant_id=tenant_id)
        self._topups = TopupPurchaseRepository(session, tenant_id=tenant_id)
        # Resolved at most once per request: every check needs the same plan,
        # and a page rendering five of them should not read it five times.
        self._resolved: tuple[Plan | None, Subscription | None] | None = None
        self._terms: PlanVersion | None = None

    async def terms(self) -> PlanVersion | None:
        """The plan version this workspace is enforced against right now."""
        await self._resolve()
        return self._terms

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
        elif subscription is not None:
            self._terms = await self._catalog.pinned_version(subscription)
        else:
            self._terms = await self._catalog.current_version(plan)
        self._resolved = (plan, subscription)
        return self._resolved

    async def check(self, key: LimitKey, *, additional: int = 1) -> Entitlement:
        """Where this workspace stands against one limit.

        `additional` is what the caller is about to do. Asking "may I add one
        more" and "am I at the limit" are different questions at the boundary,
        and only the caller knows which one it means.
        """
        if key in ACCOUNT_LIMITS:
            # Not a refusal of the caller - a refusal of the question. This
            # service resolves one workspace's plan and counts one workspace's
            # rows; an account limit spans every tenant a person owns, and there
            # is no tenant here that could answer it. Raised rather than
            # silently allowed, because "allowed" from a service that cannot
            # evaluate the limit is the shape of a bypass.
            raise ValueError(f"{key} is an account limit; use WorkspaceEntitlementService.")

        plan, subscription = await self._resolve()
        if plan is None:
            # Unenforced, and already logged in `_resolve`.
            return Entitlement(key=key, limit=None, used=0, allowed=True)
        if key is LimitKey.CHANNEL_CONNECTIONS:
            return await self._channel_entitlement(plan, additional=additional)

        terms = self._terms
        # Through the one reader of plan terms, which knows the retired keys.
        base = term_limit(terms if terms is not None else plan, key)
        purchased, granted = await self._topped_up(key)
        # The effective limit (ADR-113). Unlimited stays unlimited; otherwise
        # the plan's figure plus whatever live top-ups and grants add. Nothing
        # here writes to the plan version - a top-up is an addition beside it.
        limit = None if base is None else base + purchased + granted
        used, held = await self._used_and_held(key, subscription=subscription)
        # Holds are spoken for (ENT-03): a turn engaged and still generating
        # has not been charged yet, and a concurrent turn that ignored it would
        # oversell the allowance by every turn in flight.
        allowed = limit is None or used + held + max(additional, 0) <= limit
        since, until = _period(subscription, self._clock())
        return Entitlement(
            key=key,
            limit=limit,
            used=used,
            allowed=allowed,
            plan_code=plan.code,
            base_limit=base,
            topup_limit=purchased,
            grant_limit=granted,
            period_start=since if key not in RESOURCE_LIMITS else None,
            period_end=until if key not in RESOURCE_LIMITS else None,
            held=held,
        )

    # ------------------------------------------------------------- AI turns

    async def hold_ai_turn(self) -> Entitlement:
        """Decide, under the workspace's lock, whether one more AI turn may hold a unit.

        ENT-03. Taken in the transaction that engages the turn, which is what
        writes the hold (`AgentTurnRepository.engage(hold=True)`): the lock is
        held until that transaction commits, so the next turn's count sees this
        one's hold, and N turns racing an allowance of N leave at most N holds.
        The lock is released at that commit - never held across an inference
        (ADR-080). `allowed` false means no hold: no provider is called, and the
        conversation goes to a person (ENT-04).

        `used + held + 1 <= limit`, where `used` is the cycle's `ai_turn` charges
        and `held` the turns engaged and not yet settled, taken in one statement
        so a settle committing between two reads can never be counted in
        neither.
        """
        await self._session.execute(
            select(func.pg_advisory_xact_lock(_lock_id(self._tenant_id, LimitKey.PERIOD_AI_TURNS)))
        )
        entitlement = await self.check(LimitKey.PERIOD_AI_TURNS, additional=1)
        if not entitlement.allowed:
            logger.info(
                "billing.ai_turn_hold_refused",
                extra={
                    "event": "billing.ai_turn_hold_refused",
                    "tenant_id": str(self._tenant_id),
                    "used": entitlement.used,
                    "held": entitlement.held,
                },
            )
        return entitlement

    async def ai_turns_by_channel(self) -> dict[Channel | None, int]:
        """This cycle's AI turn charges, by the channel each was taken on (ENT-01).

        For display only. The allowance is one per workspace, and no code path
        compares a channel's figure with a limit. Charges recorded before the
        channel dimension existed are under None.
        """
        _, subscription = await self._resolve()
        since, until = _period(subscription, self._clock())
        rows = await self._session.execute(
            select(UsageEvent.channel, func.coalesce(func.sum(UsageEvent.quantity), 0))
            .where(UsageEvent.tenant_id == self._tenant_id)
            .where(UsageEvent.event_type == UsageEventType.AI_TURN)
            .where(UsageEvent.occurred_at >= since)
            .where(UsageEvent.occurred_at < until)
            .group_by(UsageEvent.channel)
        )
        return {channel: int(total) for channel, total in rows.all()}

    async def _used_and_held(
        self, key: LimitKey, *, subscription: Subscription | None
    ) -> tuple[int, int]:
        """Usage against `key`, and for AI turns the open holds, in one snapshot."""
        if key is not LimitKey.PERIOD_AI_TURNS:
            return await self._used(key, subscription=subscription), 0
        now = self._clock()
        since, until = _period(subscription, now)
        used = (
            select(func.coalesce(func.sum(UsageEvent.quantity), 0))
            .where(UsageEvent.tenant_id == self._tenant_id)
            .where(UsageEvent.event_type == UsageEventType.AI_TURN)
            .where(UsageEvent.occurred_at >= since)
            .where(UsageEvent.occurred_at < until)
            .scalar_subquery()
        )
        # A hold counts only inside the usage cycle it was taken in, and only
        # until its TTL: a hold whose worker died stops counting by the clock,
        # not when the sweep gets round to it.
        held = (
            select(func.count())
            .select_from(AgentTurn)
            .where(AgentTurn.tenant_id == self._tenant_id)
            .where(AgentTurn.charge_state == AITurnChargeState.HELD)
            .where(AgentTurn.held_at >= since)
            .where(AgentTurn.held_at < until)
            .where(AgentTurn.held_at > now - self._hold_ttl)
            .scalar_subquery()
        )
        row = (await self._session.execute(select(used, held))).one()
        return int(row[0]), int(row[1])

    async def _topped_up(self, key: LimitKey) -> tuple[int, int]:
        """What live top-ups add to `key`: (purchased, granted by the platform).

        Read afresh on every check rather than cached for the request: `consume`
        and `reserve` call `check` *under* the workspace's advisory lock, and a
        figure remembered from before the lock would be one a concurrent grant
        or expiry had already changed.
        """
        if key not in TOPUP_LIMITS:
            return 0, 0
        purchased = granted = 0
        for total in await self._topups.active_totals(at=self._clock()):
            if total.entitlement.limit_key is not key:
                continue
            if total.source is TopupSource.PLATFORM_GRANT:
                granted += total.quantity
            else:
                purchased += total.quantity
        return purchased, granted

    # ------------------------------------------------------ channel capacity

    async def _channel_entitlement(self, plan: Plan, *, additional: int) -> Entitlement:
        """`channel_connections` as an `Entitlement`, with its capacity attached.

        `allowed` answers "may `additional` more connections of a type with no
        typed slot be added" - the generic question. Connecting one particular
        channel is the guard's question (`ChannelCapacityGuard`), which asks
        the capacity itself with that channel's typed slots in view.
        """
        capacity = await self.channel_capacity()
        general = capacity.general
        allowed = general is None or capacity.overflow() + max(additional, 0) <= general
        return Entitlement(
            key=LimitKey.CHANNEL_CONNECTIONS,
            limit=capacity.total,
            used=capacity.active_total,
            allowed=allowed,
            plan_code=plan.code,
            base_limit=capacity.base,
            topup_limit=capacity.purchased,
            grant_limit=capacity.granted,
            capacity=capacity,
        )

    async def channel_capacity(self, *, at: datetime | None = None) -> ChannelCapacity:
        """This workspace's channel slots and what occupies them (ENT-05, ENT-11).

        General slots = the pinned version's `channel_connections` plus live
        general channel top-ups and platform grants; typed slots by channel from
        live typed top-ups and grants. Read fresh, like every limit: the guard
        asks this under the workspace's advisory lock. With no plan at all,
        capacity is unenforced (ADR-029) and the channel types are the legacy
        set - unenforced never opens every channel (ENT-09).
        """
        plan, _ = await self._resolve()
        if plan is None:
            return ChannelCapacity(
                base=None,
                active=await self.active_connections(),
                allowed=LEGACY_CHANNEL_TYPES,
            )
        terms: PlanVersion | Plan = self._terms if self._terms is not None else plan
        return await self._capacity(terms, at=at if at is not None else self._clock())

    async def scheduled_channel_capacity(self) -> ChannelCapacity | None:
        """The capacity a scheduled plan change leaves at its boundary (ENT-14).

        None without one. Top-ups and grants count only if they are still live
        at the boundary - capacity ones end with the billing term, which is the
        boundary - so this is what the workspace will hold once the change
        applies, and a new connection must fit it too.
        """
        plan, subscription = await self._resolve()
        if plan is None or subscription is None or subscription.scheduled_plan_version_id is None:
            return None
        version = await self._catalog.get_version(subscription.scheduled_plan_version_id)
        if version is None:  # pragma: no cover - RESTRICT keeps a scheduled version
            return None
        return await self._capacity(version, at=subscription.current_period_end)

    async def allowed_channel_types(self) -> frozenset[Channel]:
        """The channel types the plan in force allows (ENT-09); WhatsApp alone if unstated."""
        plan, _ = await self._resolve()
        if plan is None:
            return LEGACY_CHANNEL_TYPES
        return term_channel_types(self._terms if self._terms is not None else plan)

    async def active_connections(self) -> dict[Channel, int]:
        """The connections that take a slot, by channel: active and unreleased (ENT-07).

        Disabled and released connections free their slot; a connection being
        connected (no row yet) takes none. Every channel weighs one (ENT-06).
        """
        rows = await self._session.execute(
            select(ChannelConnection.channel, func.count())
            .where(ChannelConnection.tenant_id == self._tenant_id)
            .where(ChannelConnection.status == ConnectionStatus.ACTIVE)
            .where(ChannelConnection.released_at.is_(None))
            .group_by(ChannelConnection.channel)
        )
        return {channel: int(count) for channel, count in rows.all()}

    async def _capacity(self, terms: PlanVersion | Plan, *, at: datetime) -> ChannelCapacity:
        general_purchased = general_granted = 0
        typed_purchased: dict[Channel, int] = {}
        typed_granted: dict[Channel, int] = {}
        totals: list[ActiveTotal] = await self._topups.active_totals(at=at)
        for total in totals:
            if total.entitlement.limit_key is not LimitKey.CHANNEL_CONNECTIONS:
                continue
            granted = total.source is TopupSource.PLATFORM_GRANT
            slot = topup_slot_channel(total.entitlement, total.channel_type)
            if slot is None:
                if granted:
                    general_granted += total.quantity
                else:
                    general_purchased += total.quantity
                continue
            bucket = typed_granted if granted else typed_purchased
            bucket[slot] = bucket.get(slot, 0) + total.quantity
        return ChannelCapacity(
            base=term_limit(terms, LimitKey.CHANNEL_CONNECTIONS),
            general_purchased=general_purchased,
            general_granted=general_granted,
            typed_purchased=typed_purchased,
            typed_granted=typed_granted,
            active=await self.active_connections(),
            allowed=term_channel_types(terms),
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
        meta: dict[str, Any] | None = None,
    ) -> Entitlement:
        """Reserve `amount` against a limit and record it, atomically.

        `meta` is written onto the usage row, so a reservation can say what it
        was taken for - which conversation an AI turn answered - without a
        second row to reconcile.

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
        worker takes its AI turns' holds in a short transaction of its own
        (`hold_ai_turn`) for exactly this reason: holding a workspace's lock
        across an inference would serialise every conversation that workspace
        is having. Agent turns are held and settled rather than consumed: one
        is charged only when it produces a usable outcome (ENT-02).

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
        UsageRecorder(self._session, tenant_id=self._tenant_id).record(
            event_type, quantity=amount, meta=meta
        )
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
        if key is LimitKey.CHANNEL_CONNECTIONS:
            # Which channel is being connected decides whether a typed slot can
            # take it, and its type must be allowed - a question only the guard
            # asks, under this same lock (ENT-08).
            raise ValueError("Channel capacity is reserved by ChannelCapacityGuard.")
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

    async def reserve_or_refuse(self, key: LimitKey, *, additional: int = 1) -> Entitlement:
        """`reserve` for a resource limit, raising when the plan does not allow it.

        What every creating route's guard calls (BILL-08). The lock is held
        until the request's transaction ends - which is after the route has
        written the new row - so N simultaneous creations against a limit of N
        leave at most N rows. Serialises only the one workspace and the one
        limit that are actually contended.
        """
        entitlement = await self.reserve(key, additional=additional)
        if not entitlement.allowed:
            raise PlanLimitExceededError(_refusal(entitlement))
        return entitlement

    async def reserve_period(self, key: LimitKey, *, additional: int, reserved: int) -> Entitlement:
        """Take the lock on a period limit and answer, counting work already promised.

        For a period allowance whose usage is recorded later than it is
        committed to - a campaign's audience is sent over minutes, and each
        send is metered when it happens. `reserved` is what is already
        scheduled and not yet sent, so two campaigns launched together cannot
        both fit into one remaining allowance.
        """
        await self._session.execute(
            select(func.pg_advisory_xact_lock(_lock_id(self._tenant_id, key)))
        )
        entitlement = await self.check(key, additional=additional + max(reserved, 0))
        if not entitlement.allowed:
            logger.info(
                "billing.reservation_refused",
                extra={
                    "event": "billing.reservation_refused",
                    "tenant_id": str(self._tenant_id),
                    "key": key.value,
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
        """Where this workspace stands against every limit that applies to it.

        Account limits are excluded from the default set rather than raising,
        and the distinction matters: `check` raises because a caller asking a
        tenant-scoped service about an account limit has made a mistake, while
        a *snapshot* is "tell me about this workspace" and an account limit is
        simply not part of that answer. Iterating `LimitKey` and refusing on one
        member would make the obvious call site an error.

        An explicit `keys` argument is honoured as given, so a caller who names
        an account limit still gets `check`'s refusal.
        """
        selected = (
            list(keys)
            if keys is not None
            else [key for key in LimitKey if key not in ACCOUNT_LIMITS]
        )
        return [await self.check(key, additional=0) for key in selected]

    async def _used(self, key: LimitKey, *, subscription: Subscription | None) -> int:
        if key in RESOURCE_LIMITS:
            return await self._resource_count(key)
        return await self._period_usage(key, subscription=subscription)

    async def _resource_count(self, key: LimitKey) -> int:
        """How many of this resource the workspace has right now.

        The rule is "does this still occupy something?", not "does a row
        exist". Two cases where those differ (channel connections, the third,
        are counted by `active_connections`: a disabled or released one frees
        its slot, ENT-07):

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
            case LimitKey.CHANNEL_CONNECTIONS:
                return sum((await self.active_connections()).values())
            case LimitKey.AGENTS:
                statement = (
                    select(func.count())
                    .select_from(Agent)
                    .where(Agent.tenant_id == self._tenant_id)
                )
            case LimitKey.TEAM_MEMBERS:
                # Active members plus the open invitations that will become
                # members: an invitation reserves its seat (BILL-08). Counting
                # only memberships let any number of invitations be issued
                # against the last seat, and every one of them accepted.
                members = (
                    select(func.count())
                    .select_from(Membership)
                    .where(Membership.tenant_id == self._tenant_id)
                    .where(Membership.status == MembershipStatus.ACTIVE)
                    .scalar_subquery()
                )
                invited = (
                    select(func.count())
                    .select_from(TenantInvitation)
                    .where(TenantInvitation.tenant_id == self._tenant_id)
                    .where(TenantInvitation.status == InvitationStatus.PENDING)
                    .where(TenantInvitation.expires_at > func.now())
                    .scalar_subquery()
                )
                statement = select(members + invited)
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
        """How much was consumed in the current usage cycle.

        Without a subscription there is no cycle, so the window falls back to
        the calendar month. That keeps a limit meaningful for a workspace on the
        default plan instead of summing since the beginning of time, which would
        refuse everybody eventually.
        """
        meters = PERIOD_METERS.get(key)
        if not meters:
            return 0

        since, until = _period(subscription, self._clock())
        totals = await self._usage.totals(since=since, until=until, event_types=meters)
        return sum(total.quantity for total in totals)


def _period(subscription: Subscription | None, now: datetime) -> tuple[datetime, datetime]:
    """The window a period limit is counted over: the usage cycle in force now.

    The subscription's *usage* cycle, never its billing term (ADR-116), and
    the one the clock is in even if the sweep has not recorded it yet - so an
    annual customer's allowance resets on the first second of each month.
    """
    if subscription is not None:
        return current_usage_period(subscription, now)

    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # The first instant of next month, so the window stays half-open like every
    # other window in this system.
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end
