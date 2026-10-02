"""Reading and seeding per-channel marketing consent in tests (ENT-19).

`opted_out` seeds a refusal directly, as a fixture; anything a test is *about*
goes through `app.services.opt_out`, the one writer. `consent_of` reads the row
back as committed, never from the identity map.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel import Channel
from app.db.models.consent import ContactChannelConsent


async def consent_of(
    session: AsyncSession,
    contact_id: uuid.UUID,
    channel: Channel = Channel.WHATSAPP,
) -> ContactChannelConsent | None:
    """The contact's consent row on `channel`, re-read, or None if never written."""
    await session.flush()
    consent: ContactChannelConsent | None = await session.scalar(
        select(ContactChannelConsent)
        .where(
            ContactChannelConsent.contact_id == contact_id,
            ContactChannelConsent.channel == channel,
        )
        .execution_options(populate_existing=True)
    )
    return consent


async def opted_out_at(
    session: AsyncSession, contact_id: uuid.UUID, channel: Channel = Channel.WHATSAPP
) -> datetime | None:
    """When the contact opted out on `channel`, or None."""
    consent = await consent_of(session, contact_id, channel)
    return consent.marketing_opt_out_at if consent is not None else None


async def seed_opt_out(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    channel: Channel = Channel.WHATSAPP,
    source: OptOutSource = OptOutSource.CUSTOMER,
    via: OptOutVia | None = OptOutVia.MESSAGE,
    at: datetime | None = None,
) -> ContactChannelConsent:
    """A refusal already on record, as a fixture."""
    consent = ContactChannelConsent(
        tenant_id=tenant_id,
        contact_id=contact_id,
        channel=channel,
        marketing_opt_out_at=at or datetime.now(UTC),
        opt_out_source=source,
        opt_out_via=via,
    )
    session.add(consent)
    await session.flush()
    return consent


__all__ = ["consent_of", "opted_out_at", "seed_opt_out"]
