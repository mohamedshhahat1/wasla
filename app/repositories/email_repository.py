"""The outbox rows, and the claim that lets many workers share them.

Delivery is **at least once**, stated here rather than discovered in an
incident. A worker that dies between the provider accepting a message and
this table recording it leaves a `sending` row behind; recovery returns that
row to `pending` and the message goes again. Exactly-once would require the
provider and PostgreSQL to commit together, which nothing offers - so the
system is honest about the duplicate instead, and every message is one whose
second delivery is an annoyance rather than a harm.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Final, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.email import (
    MAX_ERROR_CODE_LENGTH,
    MAX_ERROR_MESSAGE_LENGTH,
    MAX_SUPPRESSION_REASON_LENGTH,
    EmailStatus,
    EmailSuppression,
    OutboundEmail,
)


def normalise_recipient(recipient: str) -> str:
    """The form an address is stored and compared in.

    Mail domains are case-insensitive and every address this system holds is
    already lower-cased on the way into `users` and `tenant_invitations`. The
    same rule is applied here rather than assumed, because suppression is a
    string comparison and a single mixed-case caller would otherwise write to
    an address a bounce had already closed.
    """
    return recipient.strip().lower()


DEFAULT_CLAIM_LIMIT: Final = 50
# A `sending` row older than this was claimed by a process that died between
# claiming and finishing. Comfortably longer than any honest send, so a slow
# provider is never mistaken for a dead worker.
STUCK_AFTER_SECONDS: Final = 600


class EmailOutboxRepository:
    """Every query the outbox needs, and no other module writes these tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        recipient: str,
        template: str,
        subject: str,
        context: Mapping[str, str],
        idempotency_key: str,
        available_at: datetime,
        tenant_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> OutboundEmail | None:
        """Insert one pending row, or nothing if its key already exists.

        `ON CONFLICT DO NOTHING` on the idempotency key rather than a
        check-then-insert: two transactions enqueueing the same logical email
        both see it absent, and the constraint - not the check - is what makes
        exactly one of them win. Returns None for the loser, which callers
        treat as success: the email exists, which is what they asked for.
        """
        statement = (
            pg_insert(OutboundEmail)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                recipient=normalise_recipient(recipient),
                template=template,
                subject=subject,
                context=dict(context),
                status=EmailStatus.PENDING,
                attempts=0,
                available_at=available_at,
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(OutboundEmail)
        )
        result = await self._session.execute(statement)
        return result.scalars().one_or_none()

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int = DEFAULT_CLAIM_LIMIT,
    ) -> list[OutboundEmail]:
        """Take a bounded batch of due rows, invisibly to other workers.

        ``FOR UPDATE SKIP LOCKED``, the same shape the follow-up sweep uses: a
        second replica sweeping at the same instant steps over what this one
        holds rather than sending the same message twice. Claimed rows are
        marked `sending` with the attempt counted, so the state is durable the
        moment the caller commits - before any network is touched.
        """
        statement = (
            select(OutboundEmail)
            .where(
                OutboundEmail.status == EmailStatus.PENDING,
                OutboundEmail.available_at <= now,
            )
            .order_by(OutboundEmail.available_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self._session.execute(statement)).scalars())
        for row in rows:
            row.status = EmailStatus.SENDING
            row.claimed_at = now
            row.attempts += 1
        await self._session.flush()
        return rows

    async def recover_stuck(self, *, now: datetime) -> int:
        """Return crashed claims to the queue. The at-least-once half.

        A row still `sending` after the cutoff belongs to a process that died
        mid-send. Whether the provider accepted the message before the crash
        is unknowable from here, so the row goes again - a duplicate email is
        the accepted cost, a silently dropped one is not.
        """
        cutoff = now - timedelta(seconds=STUCK_AFTER_SECONDS)
        statement = (
            update(OutboundEmail)
            .where(
                OutboundEmail.status == EmailStatus.SENDING,
                OutboundEmail.claimed_at < cutoff,
            )
            .values(status=EmailStatus.PENDING, available_at=now)
        )
        result = cast("CursorResult[Any]", await self._session.execute(statement))
        return int(result.rowcount or 0)

    async def mark_sent(
        self,
        email: OutboundEmail,
        *,
        now: datetime,
        provider: str,
        provider_message_id: str | None,
    ) -> None:
        email.status = EmailStatus.SENT
        email.sent_at = now
        email.provider = provider
        email.provider_message_id = provider_message_id
        email.last_error_code = None
        email.last_error_message = None
        # The context has done its job. For reset and invitation emails it
        # carried the link being delivered, and a sent message keeps no copy.
        email.context = {}
        await self._session.flush()

    async def mark_retry(
        self,
        email: OutboundEmail,
        *,
        available_at: datetime,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        email.status = EmailStatus.PENDING
        email.available_at = available_at
        email.last_error_code = (error_code or "")[:MAX_ERROR_CODE_LENGTH] or None
        email.last_error_message = (error_message or "")[:MAX_ERROR_MESSAGE_LENGTH] or None
        await self._session.flush()

    async def mark_failed(
        self,
        email: OutboundEmail,
        *,
        now: datetime,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        email.status = EmailStatus.FAILED
        email.failed_at = now
        email.last_error_code = (error_code or "")[:MAX_ERROR_CODE_LENGTH] or None
        email.last_error_message = (error_message or "")[:MAX_ERROR_MESSAGE_LENGTH] or None
        email.context = {}
        await self._session.flush()

    async def mark_delivered(self, email: OutboundEmail, *, now: datetime) -> None:
        """Record the provider's word that the message arrived.

        Hearsay, and recorded as such: it upgrades `sent`, never resurrects
        `failed`, and nothing anywhere treats it as proof a person read
        anything.
        """
        if email.status is EmailStatus.SENT:
            email.status = EmailStatus.DELIVERED
            await self._session.flush()

    async def get_claimed(self, email_id: uuid.UUID) -> OutboundEmail | None:
        """Re-read one claimed row in a fresh transaction, for dispatch.

        The worker claims a batch and commits, then delivers each message in
        its own transaction; this is how that second transaction gets its
        row. Restricted to `sending` so a row another sweep already recovered
        and re-queued is not sent twice over.
        """
        statement = select(OutboundEmail).where(
            OutboundEmail.id == email_id,
            OutboundEmail.status == EmailStatus.SENDING,
        )
        return (await self._session.execute(statement)).scalars().first()

    async def get_by_provider_message_id(self, provider_message_id: str) -> OutboundEmail | None:
        statement = select(OutboundEmail).where(
            OutboundEmail.provider_message_id == provider_message_id
        )
        return (await self._session.execute(statement)).scalars().first()

    async def is_suppressed(self, recipient: str) -> bool:
        statement = select(EmailSuppression.id).where(
            EmailSuppression.recipient == normalise_recipient(recipient)
        )
        return (await self._session.execute(statement)).first() is not None

    async def suppress(self, recipient: str, *, reason: str) -> None:
        """Record that an address must not be written to again. Repeat-safe."""
        statement = (
            pg_insert(EmailSuppression)
            .values(
                id=uuid.uuid4(),
                recipient=normalise_recipient(recipient),
                reason=reason[:MAX_SUPPRESSION_REASON_LENGTH],
            )
            .on_conflict_do_nothing(index_elements=["recipient"])
        )
        await self._session.execute(statement)
