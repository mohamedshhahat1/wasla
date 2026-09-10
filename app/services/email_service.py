"""Queueing email inside the transaction that decides it should exist.

The one door domain code queues email through. It never sends anything and
never talks to a provider: it writes an outbox row on the caller's session,
so the row commits or rolls back with the domain action itself - which is the
entire transactional-outbox guarantee (ADR-042).

Recipients come from rows this application wrote - a user's email column, a
membership join - never from request input, and there is deliberately no
generic send-to-anyone method: a platform that will address arbitrary strings
is a spam relay with extra steps.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import CredentialCipher
from app.core.logging import get_logger
from app.db.models import Membership, MembershipStatus, TenantRole, User
from app.db.models.email import OutboundEmail
from app.integrations.email.base import validate_address
from app.repositories.email_repository import EmailOutboxRepository
from app.services.email_templates import EmailTemplate, subject_for

logger = get_logger(__name__)

SEALED_CONTEXT_KEY: Final = "sealed"
SENSITIVE_EMAIL_TEMPLATES: Final = frozenset(
    {
        EmailTemplate.WORKSPACE_INVITATION,
        EmailTemplate.PASSWORD_RESET,
        EmailTemplate.EMAIL_VERIFICATION,
    }
)


class EmailContextError(ValueError):
    """A sensitive outbox context is missing, malformed, or undecryptable."""


def _context_aad(idempotency_key: str) -> str:
    return f"email-outbox:{idempotency_key}"


def seal_email_context(
    *,
    template: EmailTemplate,
    context: Mapping[str, object],
    idempotency_key: str,
    settings: Settings,
) -> dict[str, str]:
    """Encrypt credential-bearing template context before persistence."""
    values = {key: str(value) for key, value in context.items()}
    if template not in SENSITIVE_EMAIL_TEMPLATES:
        return values
    plaintext = json.dumps(values, separators=(",", ":"), sort_keys=True)
    sealed = CredentialCipher(settings.credential_encryption_keys).encrypt(
        plaintext,
        context=_context_aad(idempotency_key),
    )
    return {SEALED_CONTEXT_KEY: sealed}


def open_email_context(email: OutboundEmail, settings: Settings) -> dict[str, str]:
    """Decrypt sensitive context only in the worker process.

    Plaintext context on a credential-bearing template is rejected rather than
    treated as a legacy format. This project is pre-production and deliberately
    has no plaintext compatibility path.
    """
    try:
        template = EmailTemplate(email.template)
    except ValueError as error:
        raise EmailContextError("The email context could not be opened.") from error

    if template not in SENSITIVE_EMAIL_TEMPLATES:
        return {str(key): str(value) for key, value in email.context.items()}

    if set(email.context) != {SEALED_CONTEXT_KEY}:
        raise EmailContextError("The email context could not be opened.")
    stored = email.context.get(SEALED_CONTEXT_KEY)
    if not isinstance(stored, str):
        raise EmailContextError("The email context could not be opened.")
    try:
        plaintext = CredentialCipher(settings.credential_encryption_keys).decrypt(
            stored,
            context=_context_aad(email.idempotency_key),
        )
        decoded = json.loads(plaintext)
    except Exception as error:
        # The ciphertext and parser detail must never reach logs or the row's
        # error message. The underlying cipher already emits a bounded event.
        raise EmailContextError("The email context could not be opened.") from error
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
    ):
        raise EmailContextError("The email context could not be opened.")
    return decoded


class EmailOutbox:
    """Enqueue transactional email on the caller's own session."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._repository = EmailOutboxRepository(session)

    async def enqueue(
        self,
        *,
        template: EmailTemplate,
        recipient: str,
        idempotency_key: str,
        context: Mapping[str, object] | None = None,
        tenant_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> OutboundEmail | None:
        """Queue one email, or nothing if it is already queued or email is off.

        A disabled deployment is a deliberate no-op rather than a row that
        waits forever: enabling email later should start delivering *new*
        notices, not a backlog of stale ones. A duplicate idempotency key is
        treated as success - the email the caller wanted exists.

        An invalid recipient is logged (without the address) and skipped
        rather than raised: the recipient came from a database row, so a bad
        one is data corruption to investigate, not a reason to fail the
        domain action carrying it.
        """
        if not self._settings.email_enabled:
            logger.debug(
                "email.disabled_skipping",
                extra={"event": "email.disabled_skipping", "template": template.value},
            )
            return None

        try:
            address = validate_address(recipient)
        except ValueError:
            logger.warning(
                "email.recipient_invalid",
                extra={
                    "event": "email.recipient_invalid",
                    "template": template.value,
                    "tenant_id": str(tenant_id) if tenant_id else None,
                    "user_id": str(user_id) if user_id else None,
                },
            )
            return None

        stored_context = seal_email_context(
            template=template,
            context=context or {},
            idempotency_key=idempotency_key,
            settings=self._settings,
        )
        row = await self._repository.enqueue(
            recipient=address,
            template=template.value,
            subject=subject_for(template),
            context=stored_context,
            idempotency_key=idempotency_key,
            available_at=datetime.now(UTC),
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if row is None:
            logger.info(
                "email.duplicate_suppressed",
                extra={
                    "event": "email.duplicate_suppressed",
                    "template": template.value,
                },
            )
        else:
            logger.info(
                "email.queued",
                extra={
                    "event": "email.queued",
                    "email_message_id": str(row.id),
                    "template": template.value,
                    "tenant_id": str(tenant_id) if tenant_id else None,
                    "user_id": str(user_id) if user_id else None,
                },
            )
        return row

    async def enqueue_for_tenant_owners(
        self,
        *,
        tenant_id: uuid.UUID,
        template: EmailTemplate,
        idempotency_prefix: str,
        context: Mapping[str, object] | None = None,
    ) -> int:
        """Queue one email per active owner of a workspace.

        Owners rather than every member, because billing and lifecycle notices
        are for the people who can act on them. The idempotency key is the
        caller's prefix plus the owner's id, so the same event notifies each
        owner exactly once however often it is replayed - and an owner added
        after the event does not receive history.
        """
        statement = (
            select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.tenant_id == tenant_id,
                Membership.role == TenantRole.TENANT_OWNER,
                Membership.status == MembershipStatus.ACTIVE,
                User.is_active.is_(True),
            )
        )
        owners = list((await self._session.execute(statement)).scalars())
        queued = 0
        for owner in owners:
            row = await self.enqueue(
                template=template,
                recipient=owner.email,
                idempotency_key=f"{idempotency_prefix}:{owner.id}",
                context=context,
                tenant_id=tenant_id,
                user_id=owner.id,
            )
            if row is not None:
                queued += 1
        return queued
