"""A plan that includes the channels a test automates on (ENT-09, ENT-16).

Automation - an AI turn, a campaign copy, a follow-up - runs only on a channel
the plan in force includes. A workspace with no plan at all reads the legacy
set, WhatsApp alone, so a test that answers or nudges a customer on a synthetic
second channel gives its workspace a plan that includes it, exactly as a
customer of that channel would have one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.channel import Channel
from app.services.entitlement_terms import ordered
from app.services.plan_catalog import PlanCatalog

EVERY_CHANNEL: tuple[Channel, ...] = tuple(Channel)


async def plan_with_channels(
    session: AsyncSession,
    *,
    channels: Iterable[Channel] = EVERY_CHANNEL,
    limits: dict[str, int] | None = None,
) -> Plan:
    """A free plan of the test's own including `channels`; its version is published."""
    plan = Plan(
        code=f"chan-{uuid.uuid4().hex[:10]}",
        name="Channels",
        price=Decimal("0.00"),
        currency="EGP",
        interval=BillingInterval.MONTHLY,
        limits=limits or {},
        allowed_channel_types=[channel.value for channel in ordered(channels)],
    )
    session.add(plan)
    await session.flush()
    version = await PlanCatalog(session).current_version(plan)
    assert version is not None
    return plan


async def subscribe(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    plan: Plan,
    *,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
) -> Subscription:
    """Put the workspace on `plan`, for a month that is running now."""
    version = await PlanCatalog(session).current_version(plan)
    assert version is not None
    now = datetime.now(UTC)
    subscription = Subscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        plan_version_id=version.id,
        status=status,
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=29),
        billing_anchor_at=now - timedelta(days=1),
    )
    session.add(subscription)
    await session.flush()
    return subscription


async def allow_channels(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    channels: Iterable[Channel] = EVERY_CHANNEL,
    *,
    limits: dict[str, int] | None = None,
) -> Subscription:
    """The workspace's plan in force includes `channels` - every one by default."""
    plan = await plan_with_channels(session, channels=channels, limits=limits)
    return await subscribe(session, tenant_id, plan)


__all__ = ["EVERY_CHANNEL", "allow_channels", "plan_with_channels", "subscribe"]
