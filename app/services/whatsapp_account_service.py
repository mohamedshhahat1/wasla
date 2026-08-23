"""Connecting and managing WhatsApp accounts for a workspace.

A workspace may supply its own Meta token when it connects a number, and it is
encrypted before it reaches the session (ADR-034). The service holds a credential
service rather than a cipher so that a deployment with no key configured is a
supported state - the number still connects and sends through the platform token -
and only an attempt to *store* a credential is refused.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.repositories.whatsapp_repository import WhatsAppAccountRepository
from app.services.audit_service import AuditTrail
from app.services.credential_service import CredentialService

logger = get_logger(__name__)


class WhatsAppAccountService:
    """Every method takes the workspace explicitly; nothing is inferred."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        credentials: CredentialService | None = None,
    ) -> None:
        self._session = session
        # Optional: without one, a workspace cannot store its own token and
        # says so, rather than storing it unencrypted.
        self._credentials = credentials

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
        access_token: str | None = None,
        actor: User | None = None,
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

        if access_token:
            # Encrypted before it is anywhere near the session. The plaintext
            # exists only for the length of this call (ADR-034), and a
            # deployment with no key refuses rather than storing it in the
            # clear - which is the failure ADR-009 spent a phase preventing.
            if self._credentials is None:
                raise ValidationError(
                    "This deployment cannot store a workspace credential: "
                    "no credential encryption key is configured."
                )
            account.access_token_encrypted = self._credentials.seal(
                access_token,
                tenant_id=tenant_id,
            )

        # created_at is a server default, and serialising an unrefreshed row
        # would trigger a lazy load outside the async greenlet context.
        await self._session.flush()
        await self._session.refresh(account)

        AuditTrail(self._session, tenant_id=tenant_id).record(
            AuditAction.WHATSAPP_ACCOUNT_CONNECTED,
            actor=actor,
            actor_kind=AuditActorKind.USER if actor is not None else AuditActorKind.SYSTEM,
            target_type="whatsapp_account",
            target_id=account.id,
            # The display number, not the token or the account id: a person
            # reading this needs to recognise which number it was.
            target_label=account.display_phone_number,
        )

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
        actor: User | None = None,
    ) -> WhatsAppAccount:
        """Enable or disable an account.

        The lookup is workspace-scoped, so an account belonging to another
        workspace is not found rather than refused.
        """
        account = await self._accounts(tenant_id).require_by_id(account_id)
        account.status = status
        await self._session.flush()

        AuditTrail(self._session, tenant_id=tenant_id).record(
            (
                AuditAction.WHATSAPP_ACCOUNT_DISABLED
                if status is WhatsAppAccountStatus.DISABLED
                else AuditAction.WHATSAPP_ACCOUNT_ENABLED
            ),
            actor=actor,
            actor_kind=AuditActorKind.USER if actor is not None else AuditActorKind.SYSTEM,
            target_type="whatsapp_account",
            target_id=account.id,
            target_label=account.display_phone_number,
        )

        logger.info(
            "whatsapp.account_status_changed",
            extra={"phone_number_id": account.phone_number_id, "status": status.value},
        )
        return account
