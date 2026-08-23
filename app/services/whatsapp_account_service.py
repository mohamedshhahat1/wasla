"""Connecting and managing WhatsApp accounts for a workspace.

**A number is claimed by proving control of it, never by naming it** (ADR-037).
The credential supplied with a connect request is checked against Meta before
anything is written: a token that can read the phone number node is a token the
owning business issued, and nothing else can read it. Every identifier stored on
the row - the business account, the display number, the verified name - is taken
from Meta's answer rather than from the request, because the request is exactly
the thing being checked.

The platform credential is deliberately not a route to this. It can read every
number the platform is connected to, so allowing it would let any workspace
claim any of them, which is the hole ownership proof exists to close. A
deployment can therefore connect a number only when the workspace supplies its
own token.

Storing that token is a separate question with a separate answer. Verification
needs the plaintext for the length of one call; storage needs a configured
encryption key (ADR-034). A deployment without a key still connects numbers -
proof happens, the plaintext is discarded, and sending falls back to the
platform credential exactly as before - because refusing to store a secret we do
not need to keep is not a reason to refuse the claim.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DependencyUnavailableError
from app.core.logging import get_logger
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.integrations.whatsapp.ownership import OwnershipVerifier
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
        ownership: OwnershipVerifier | None = None,
        credentials: CredentialService | None = None,
    ) -> None:
        self._session = session
        # Without a verifier nothing can be connected. Optional in the
        # signature only so a caller that never connects - a test exercising
        # enable/disable - need not build one; `connect` refuses rather than
        # proceeding unverified.
        self._ownership = ownership
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
        access_token: str,
        waba_id: str | None = None,
        display_name: str | None = None,
        actor: User | None = None,
    ) -> WhatsAppAccount:
        """Claim a phone number for this workspace, having proven control of it.

        The order is load-bearing. Meta is asked *before* the row is written, so
        a failed proof leaves nothing behind and cannot be used to squat a
        number: a request that cannot be verified never reaches the uniqueness
        index at all.

        `waba_id` is an assertion to check, not a value to store. If Meta names
        a different business account the claim is refused rather than quietly
        corrected, because a mismatch means the person connecting believes
        something untrue about which account this number sits on.
        """
        if self._ownership is None:
            # Our misconfiguration, not the caller's mistake, so 503. Failing
            # closed here is the point of the whole module: the alternative is
            # a deployment that silently accepts unproven claims.
            raise DependencyUnavailableError(
                "WhatsApp number verification is not available in this deployment."
            )

        # Identifiers are stripped because they are copied by hand from the Meta
        # dashboard, and a trailing space would silently break webhook
        # resolution for every inbound message.
        number = phone_number_id.strip()
        claimed_waba = waba_id.strip() if waba_id else None

        verified = await self._ownership.verify(
            access_token=access_token,
            phone_number_id=number,
            claimed_waba_id=claimed_waba,
        )

        account = await self._accounts(tenant_id).connect(
            phone_number_id=verified.phone_number_id,
            # Meta's answer, not the request's claim.
            waba_id=verified.waba_id,
            display_phone_number=verified.display_phone_number,
            display_name=display_name.strip() if display_name else None,
            verified_name=verified.verified_name,
            ownership_verified_at=datetime.now(UTC),
        )

        if self._credentials is not None and self._credentials.can_store:
            # Encrypted before it is anywhere near the session. The plaintext
            # exists only for the length of this call (ADR-034).
            account.access_token_encrypted = self._credentials.seal(
                access_token,
                tenant_id=tenant_id,
            )
        else:
            # Proof happened; the token is simply not kept. Sending falls back
            # to the platform credential, as it did for every workspace before
            # per-workspace tokens existed.
            logger.info(
                "whatsapp.credential_not_stored",
                extra={
                    "event": "whatsapp.credential_not_stored",
                    "reason": "no_encryption_key",
                    "phone_number_id": account.phone_number_id,
                },
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
            # Records that the claim was proven and against which business
            # account, so the trail answers "on what basis did they get it?"
            # No part of the credential appears here or anywhere else.
            meta={"waba_id": account.waba_id, "ownership_verified": True},
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
        workspace is not found rather than refused, and it is restricted to live
        claims: re-enabling a number this workspace has given up would be
        meaningless at best and, once somebody else holds it, wrong.
        """
        if status is WhatsAppAccountStatus.RELEASED:
            # Releasing is its own operation with its own audit action and its
            # own consequences for other workspaces. Reaching it through the
            # status setter would make giving a number away look like pausing
            # it.
            raise ValueError("Use release() to give up a number.")

        account = await self._accounts(tenant_id).require_live_by_id(account_id)
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

    async def release(
        self,
        *,
        tenant_id: uuid.UUID,
        account_id: uuid.UUID,
        actor: User | None = None,
    ) -> WhatsAppAccount:
        """Give the number up, so another workspace may prove and claim it.

        Not a delete. The row carries this workspace's conversations and
        messages by foreign key, and destroying a customer's history is not an
        acceptable price for moving a phone number. Setting `released_at`
        removes the row from the partial uniqueness index instead: the claim
        ends, the history stays.

        Not reversible either. Taking the number back means proving control of
        it again, which is the same bar the next workspace has to clear - and
        the bar has to be the same, or "release" becomes a way to hold a number
        in reserve without holding it.

        The stored credential is dropped on the way out. It belongs to a number
        this workspace no longer has, so keeping it would be a live sending
        capability retained past the point of any authority to use it.
        """
        account = await self._accounts(tenant_id).require_live_by_id(account_id)
        account.status = WhatsAppAccountStatus.RELEASED
        account.released_at = datetime.now(UTC)
        account.access_token_encrypted = None
        await self._session.flush()

        AuditTrail(self._session, tenant_id=tenant_id).record(
            AuditAction.WHATSAPP_ACCOUNT_RELEASED,
            actor=actor,
            actor_kind=AuditActorKind.USER if actor is not None else AuditActorKind.SYSTEM,
            target_type="whatsapp_account",
            target_id=account.id,
            target_label=account.display_phone_number,
        )

        logger.info(
            "whatsapp.account_released",
            extra={
                "event": "whatsapp.account_released",
                "phone_number_id": account.phone_number_id,
            },
        )
        return account
