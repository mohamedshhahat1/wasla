"""The one guard every path that makes a channel connection active goes through (ENT-07, ENT-08).

A connection takes a channel slot from the moment it is active - connected,
enabled again, or reconnected - and gives it back when it is disabled or
released. The guard is asked before each of those activations, in this order,
which is ENT-08's:

1. the workspace (the caller's, never one a request names);
2. its effective entitlements (`EntitlementService`, the only reader of limits);
3. its active connections, by channel;
4. whether one more of *this* channel fits - general and typed slots, the fit
   rule in `channel_fit` - and, while a downgrade is scheduled, whether it
   fits the scheduled plan too (ENT-14);
5. whether the plan allows this channel type at all (ENT-09).

Capacity is judged before type, so a request that fails both is told about
capacity: a workspace at its limit learns it is full before it learns a type is
not on its plan.

**Twice, deliberately.** `precheck` runs with no lock before anything leaves
Wasla - before the Graph ownership read - so a workspace at capacity never makes
Wasla call Meta. `reserve_or_refuse` is the authoritative answer, under the
workspace's `channel_connections` advisory lock (BILL-08), inside the
transaction that inserts or enables the row; the lock is held to commit, so N
simultaneous activations against N free slots leave at most N active. The lock
is taken after the provider call, never across it.

**Refusals are 409, not 402** (ADR-131): `channel_capacity_exceeded` and
`channel_type_not_allowed`. Disabling, releasing, listing, reading, inbound
traffic and re-authorising an active connection are never refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.core.telemetry import record_entitlement_refusal
from app.db.models.billing import LimitKey
from app.db.models.channel import Channel
from app.services.channel_fit import ChannelCapacity
from app.services.entitlement_service import EntitlementService, hold_limit_lock
from app.services.entitlement_terms import ordered

logger = get_logger(__name__)


class ChannelCapacityExceededError(ConflictError):
    """Every channel slot this workspace has is taken (ENT-08)."""

    error_code = "channel_capacity_exceeded"
    message = (
        "This workspace has no free channel slot. Disable or release a connection, "
        "upgrade the plan, or buy a channel top-up."
    )


class ChannelTypeNotAllowedError(ConflictError):
    """The workspace's plan does not include this channel type (ENT-09)."""

    error_code = "channel_type_not_allowed"
    message = "This workspace's plan does not include this channel. Upgrade the plan to use it."


@dataclass(frozen=True, slots=True)
class ChannelSlot:
    """The guard's answer that one activation of `channel` fits, in this transaction.

    Writers of an active connection take one as a parameter, so an activation
    without the guard does not type-check - and `tests/unit/test_channel_activation
    _writers.py` refuses a new writer that sidesteps it.
    """

    tenant_id: uuid.UUID
    channel: Channel
    capacity: ChannelCapacity


def capacity_details(
    capacity: ChannelCapacity, channel: Channel, *, scheduled: bool = False
) -> dict[str, Any]:
    """What a refusal says about the slots, in closed, countable terms."""
    details: dict[str, Any] = {
        "effective_limit": capacity.total,
        "active": capacity.active_total,
        "channel": channel.value,
        "typed_capacity": {
            item.value: count
            for item, count in sorted(capacity.typed.items(), key=lambda pair: pair[0].value)
        },
        "over_limit": capacity.over_limit,
    }
    if scheduled:
        details["scheduled_change"] = True
    return details


def type_details(
    allowed: frozenset[Channel], channel: Channel, *, scheduled: bool = False
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "channel": channel.value,
        "allowed_channel_types": [item.value for item in ordered(allowed)],
    }
    if scheduled:
        details["scheduled_change"] = True
    return details


class ChannelCapacityGuard:
    """Asks whether one more connection of a channel may become active, for one workspace."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        default_plan_code: str | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._default_plan_code = default_plan_code
        self._clock = clock

    def _entitlements(self) -> EntitlementService:
        # A fresh service per question: the authoritative check must not read a
        # plan or subscription cached before the lock was taken.
        return EntitlementService(
            self._session,
            tenant_id=self._tenant_id,
            default_plan_code=self._default_plan_code,
            clock=self._clock,
        )

    async def refusal(self, channel: Channel) -> ConflictError | None:
        """Why one more `channel` connection may not become active, or None."""
        refused, _ = await self._judge(channel)
        return refused

    async def _judge(self, channel: Channel) -> tuple[ConflictError | None, ChannelCapacity]:
        entitlements = self._entitlements()
        capacity = await entitlements.channel_capacity()
        if not capacity.fits_with(channel):
            return (
                ChannelCapacityExceededError(details=capacity_details(capacity, channel)),
                capacity,
            )
        scheduled = await entitlements.scheduled_channel_capacity()
        if scheduled is not None and not scheduled.fits_with(channel):
            # A downgrade is scheduled: a connection that would not survive the
            # boundary is not added now (ENT-14).
            return (
                ChannelCapacityExceededError(
                    details=capacity_details(scheduled, channel, scheduled=True)
                ),
                capacity,
            )
        if channel not in capacity.allowed:
            return (
                ChannelTypeNotAllowedError(details=type_details(capacity.allowed, channel)),
                capacity,
            )
        if scheduled is not None and channel not in scheduled.allowed:
            return (
                ChannelTypeNotAllowedError(
                    details=type_details(scheduled.allowed, channel, scheduled=True)
                ),
                capacity,
            )
        return None, capacity

    async def precheck(self, channel: Channel) -> None:
        """Refuse before any provider is asked anything. No lock: a fast, early answer."""
        refused, _ = await self._judge(channel)
        if refused is not None:
            await self._refused(refused, channel, stage="precheck")
            raise refused

    async def reserve_or_refuse(self, channel: Channel) -> ChannelSlot:
        """The authoritative answer, under the workspace's lock, held to commit.

        The caller writes the active row in this same transaction. Two
        activations racing for the last slot serialise here; the second counts
        the first's row and is refused.
        """
        await hold_limit_lock(
            self._session, tenant_id=self._tenant_id, key=LimitKey.CHANNEL_CONNECTIONS
        )
        refused, capacity = await self._judge(channel)
        if refused is not None:
            await self._refused(refused, channel, stage="reserve")
            raise refused
        return ChannelSlot(tenant_id=self._tenant_id, channel=channel, capacity=capacity)

    async def _refused(self, refused: ConflictError, channel: Channel, *, stage: str) -> None:
        """Log and count one refusal; a pre-check refusal never reaches the second check."""
        if isinstance(refused, ChannelTypeNotAllowedError):
            await record_entitlement_refusal("allowed_channel_types", "type_not_allowed")
        else:
            await record_entitlement_refusal("channel_connections", "capacity_exceeded")
        logger.info(
            "billing.channel_activation_refused",
            extra={
                "event": "billing.channel_activation_refused",
                "tenant_id": str(self._tenant_id),
                "channel": channel.value,
                "reason": refused.error_code,
                "stage": stage,
            },
        )


__all__ = [
    "ChannelCapacityExceededError",
    "ChannelCapacityGuard",
    "ChannelSlot",
    "ChannelTypeNotAllowedError",
    "capacity_details",
    "type_details",
]
