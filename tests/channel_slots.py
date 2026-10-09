"""The channel capacity guard's slot, for tests that write a number directly.

`WhatsAppAccountRepository.connect` writes a number active, so it takes the
guard's `ChannelSlot` (ENT-08). A test that exercises the repository itself
asks the real guard for one, exactly as the service does.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.channel import Channel
from app.services.channel_capacity import ChannelCapacityGuard, ChannelSlot


async def whatsapp_slot(session: AsyncSession, tenant_id: uuid.UUID) -> ChannelSlot:
    """The guard's slot for one WhatsApp number in `tenant_id`, in this transaction."""
    return await ChannelCapacityGuard(
        session, tenant_id=tenant_id, default_plan_code=None
    ).reserve_or_refuse(Channel.WHATSAPP)


__all__ = ["whatsapp_slot"]
