"""The contact picker behind the campaigns page (PG-07, ADR-149).

Choosing people by hand for a campaign starts here: search a workspace's
contacts by name or number and see, for a chosen number, who a campaign could
reach and who opted out. Reading only - choosing somebody still goes through
the audience, which is filtered by the base population whatever was picked.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.core.pagination import Cursor, Page
from app.repositories.contact_picker_repository import ContactPickerRepository, PickedContact
from app.repositories.whatsapp_repository import WhatsAppAccountRepository

ACCOUNT_REQUIRED_CODE = "account_required"


class ContactPicker:
    """One workspace's contacts, a page at a time."""

    def __init__(self, session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        self._contacts = ContactPickerRepository(session, tenant_id=tenant_id)
        self._accounts = WhatsAppAccountRepository(session, tenant_id=tenant_id)

    async def page(
        self,
        *,
        q: str | None,
        account_id: uuid.UUID | None,
        eligible_for_campaign: bool,
        limit: int,
        cursor: str | None,
    ) -> Page[PickedContact]:
        """Newest activity first; another workspace's number is not found."""
        if eligible_for_campaign and account_id is None:
            # Eligibility is a property of a number: who can be reached from it.
            raise ValidationError(
                "Choose the WhatsApp number to ask who a campaign could reach.",
                error_code=ACCOUNT_REQUIRED_CODE,
            )
        if account_id is not None:
            await self._accounts.require_by_id(account_id)
        rows = await self._contacts.page(
            account_id=account_id,
            q=(q or "").strip() or None,
            eligible_only=eligible_for_campaign,
            limit=limit,
            after=Cursor.decode(cursor) if cursor else None,
        )
        items = [contact for contact, _ in rows]
        following = rows[-1][1].encode() if len(rows) == limit else None
        return Page(items=items, next_cursor=following)


__all__ = ["ACCOUNT_REQUIRED_CODE", "ContactPicker"]
