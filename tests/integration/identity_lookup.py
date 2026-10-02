"""Finding a test's contact the way the application does: through its identity.

`ContactRepository.get_by_wa_id` read the deprecated `contacts.wa_id` column and
nothing in the application called it any more (OMNI-049); tests that only wanted
"the contact this phone wrote as" ask the identity model instead.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.channel import Channel, IdentityKind, IdentityScope
from app.db.models.conversation import Contact
from app.repositories.channel_repository import ContactIdentityRepository


async def contact_by_phone(
    session: AsyncSession, tenant_id: uuid.UUID, phone: str
) -> Contact | None:
    """The contact a WhatsApp phone identity belongs to in this workspace, or None."""
    identity = await ContactIdentityRepository(session, tenant_id=tenant_id).find(
        channel=Channel.WHATSAPP,
        kind=IdentityKind.PHONE,
        scope=IdentityScope.WORKSPACE,
        scope_ref="",
        value=phone,
    )
    if identity is None:
        return None
    return await session.get(Contact, identity.contact_id)


__all__ = ["contact_by_phone"]
