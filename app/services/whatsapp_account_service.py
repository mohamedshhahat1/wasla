"""Connecting and managing WhatsApp accounts for a workspace."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.repositories.whatsapp_repository import WhatsAppAccountRepository

logger = get_logger(__name__)


class WhatsAppAccountService:
    """Every method takes the workspace explicitly; nothing is inferred."""

    def __init__(self, *, session: AsyncSession) -> None:
        self._session = session

    def _accounts(self, tenant_id: uuid.UUID) -> WhatsAppAccountRepository:
        return WhatsAppAccountRepository(self._session, tenant_id=tenant_id)

    async def connect(
        self,
        *,
        tenant_id: uuid.UUID,
        phone_number_id: str,
        waba_id: str,
        display_phone_number: str,
        display_name: str | None = None,
    ) -> WhatsAppAccount:
        """Claim a phone number for this workspace.

        Identifiers are stripped because they are copied by hand from the Meta
        dashboard, and a trailing space would silently break webhook resolution
        for every inbound message.
        """
        account = await self._accounts(tenant_id).connect(
            phone_number_id=phone_number_id.strip(),
            waba_id=waba_id.strip(),
            display_phone_number=display_phone_number.strip(),
            display_name=display_name.strip() if display_name else None,
        )

        # created_at is a server default, and serialising an unrefreshed row
        # would trigger a lazy load outside the async greenlet context.
        await self._session.flush()
        await self._session.refresh(account)

        logger.info(
            "whatsapp.account_connected",
            extra={"phone_number_id": account.phone_number_id},
        )
        return account

    async def list_accounts(self, *, tenant_id: uuid.UUID) -> list[WhatsAppAccount]:
        return await self._accounts(tenant_id).list_all()

    async def set_status(
        self,
        *,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        status: WhatsAppAccountStatus,
    ) -> WhatsAppAccount:
        """Enable or disable an account.

        The lookup is workspace-scoped, so an account belonging to another
        workspace is not found rather than refused.
        """
        account = await self._accounts(tenant_id).require_by_id(account_id)
        account.status = status
        await self._session.flush()

        logger.info(
            "whatsapp.account_status_changed",
            extra={"phone_number_id": account.phone_number_id, "status": status.value},
        )
        return account
