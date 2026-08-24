"""The worker that drains the email outbox.

Time-and-state triggered like the follow-up sweep, so it polls PostgreSQL
rather than blocking on a queue (ADR-022): a queued email is a row whose
moment has arrived. Rows are claimed with ``FOR UPDATE SKIP LOCKED``, so two
replicas sweeping at once step over each other rather than sending the same
message twice.

**Delivery is at least once, and the window is the sweep.** The sweep is one
transaction, committed at the end like every other sweep in this process. A
worker that dies after the provider accepted a message but before the commit
rolls its claim back, and the message goes again on the next sweep. That
duplicate is the accepted cost; the alternative - committing "sent" before
sending - silently drops mail, which is worse for every message this system
sends. Exactly-once is not on offer and is not claimed (ADR-042).

Failure handling is per row and per class. A transient refusal backs off
exponentially with jitter and gives up at ``EMAIL_MAX_ATTEMPTS``; a permanent
refusal stops at once; a row that cannot even render is failed and contained,
because one broken row must not strand every other workspace's mail behind
it.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models.email import OutboundEmail
from app.db.session import Database
from app.integrations.email import build_email_provider
from app.integrations.email.base import (
    EmailMessage,
    EmailProvider,
    EmailSendResult,
    EmailSendState,
)
from app.repositories.email_repository import (
    DEFAULT_CLAIM_LIMIT,
    EmailOutboxRepository,
)
from app.services.email_templates import EmailTemplate, render

logger = get_logger(__name__)

# First retry lands half a minute out; the ceiling keeps a long outage from
# pushing retries into next week. Jitter spreads a burst of failures so the
# retries do not arrive as the same thundering herd that just failed.
BASE_BACKOFF_SECONDS: Final = 30.0
MAX_BACKOFF_SECONDS: Final = 3600.0


class EmailWorker:
    """Polls for due outbox rows and sends them through the provider."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        provider: EmailProvider | None = None,
        poll_seconds: float | None = None,
        claim_limit: int = DEFAULT_CLAIM_LIMIT,
    ) -> None:
        self._database = database
        self._settings = settings
        # Built at construction so a missing credential is a container that
        # refuses to boot, not a loop that claims rows it can never send.
        self._provider = provider if provider is not None else build_email_provider(settings)
        self._poll_seconds = (
            poll_seconds if poll_seconds is not None else settings.email_worker_poll_seconds
        )
        self._claim_limit = claim_limit
        self._running = False
        # Set when stop() is called, so a sleeping worker wakes at once instead
        # of finishing its interval.
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        """Sweep until asked to stop."""
        self._running = True
        self._stopping.clear()
        logger.info("email.worker_started")
        while self._running:
            try:
                await self.run_once()
            except Exception:
                # A failed sweep must not kill the loop: every later email
                # would go unsent, and nothing would say why.
                logger.exception("email.sweep_failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue
        logger.info("email.worker_stopped")

    def stop(self) -> None:
        self._running = False
        self._stopping.set()

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Send every email currently due. Returns how many were handled.

        One session for the sweep, committed at the end, like the follow-up
        and billing sweeps. The claimed rows stay locked until the commit,
        which is what keeps another replica off them, so the sweep is bounded
        by `claim_limit` rather than draining the whole backlog under one
        lock.
        """
        moment = now or datetime.now(UTC)
        handled = 0

        async with self._database.session() as session:
            repository = EmailOutboxRepository(session)

            recovered = await repository.recover_stuck(now=moment)
            if recovered:
                # Defence in depth: with a single commit per sweep a crash
                # rolls claims back to pending, so a persisted `sending` row
                # means something unusual happened and is worth a loud line.
                logger.warning(
                    "email.stuck_recovered",
                    extra={"event": "email.stuck_recovered", "recovered": recovered},
                )

            claimed = await repository.claim_due(now=moment, limit=self._claim_limit)
            if not claimed:
                return 0

            for email in claimed:
                try:
                    await self._handle(repository, email, now=moment)
                except Exception:
                    # Contained to the one row. It stays claimed in this
                    # transaction and lands wherever the commit leaves it;
                    # the next sweep's recovery returns it to the queue.
                    logger.exception(
                        "email.dispatch_failed",
                        extra={"email_message_id": str(email.id)},
                    )
                    continue
                handled += 1

        logger.info("email.sweep_completed", extra={"handled": handled})
        return handled

    async def _handle(
        self,
        repository: EmailOutboxRepository,
        email: OutboundEmail,
        *,
        now: datetime,
    ) -> None:
        """One row: render, send, record - with every outcome written down."""
        if await repository.is_suppressed(email.recipient):
            # The mailbox told us to stop. Not an error to retry - retrying
            # into a hard bounce is how a sender reputation dies.
            await repository.mark_failed(
                email,
                now=now,
                error_code="suppressed",
                error_message="recipient is suppressed after a bounce or complaint",
            )
            logger.info(
                "email.suppressed_skipped",
                extra={
                    "event": "email.suppressed_skipped",
                    "email_message_id": str(email.id),
                    "template": email.template,
                },
            )
            return

        try:
            template = EmailTemplate(email.template)
            rendered = render(
                template,
                email.context,
                public_url=self._settings.app_public_url or "",
            )
            message = EmailMessage(
                sender=self._settings.email_from or "",
                to=(email.recipient,),
                subject=rendered.subject,
                text=rendered.text,
                html=rendered.html,
                reply_to=self._settings.email_reply_to,
            )
        except ValueError as error:
            # A row that cannot render will not render tomorrow either.
            await repository.mark_failed(
                email,
                now=now,
                error_code="render_error",
                error_message=str(error),
            )
            logger.error(
                "email.render_failed",
                extra={
                    "event": "email.render_failed",
                    "email_message_id": str(email.id),
                    "template": email.template,
                },
            )
            return

        try:
            result = await self._provider.send(message, idempotency_key=email.idempotency_key)
        except Exception:
            # The adapter classifies everything it understands, so an
            # exception escaping it is a bug in the adapter - treated as
            # transient because the message itself may be fine.
            logger.exception(
                "email.provider_error",
                extra={"email_message_id": str(email.id)},
            )
            result = EmailSendResult(
                state=EmailSendState.TRANSIENT_FAILURE,
                provider=self._provider.name,
                error_code="provider_exception",
            )

        if result.state is EmailSendState.SENT:
            await repository.mark_sent(
                email,
                now=now,
                provider=result.provider,
                provider_message_id=result.provider_message_id,
            )
            logger.info(
                "email.sent",
                extra={
                    "event": "email.sent",
                    "email_message_id": str(email.id),
                    "template": email.template,
                    "provider": result.provider,
                    "provider_message_id": result.provider_message_id,
                    "attempts": email.attempts,
                },
            )
            return

        exhausted = email.attempts >= self._settings.email_max_attempts
        if result.state is EmailSendState.PERMANENT_FAILURE or exhausted:
            await repository.mark_failed(
                email,
                now=now,
                error_code=result.error_code,
                error_message=result.error_message,
            )
            logger.error(
                "email.failed_permanently",
                extra={
                    "event": "email.failed_permanently",
                    "email_message_id": str(email.id),
                    "template": email.template,
                    "attempts": email.attempts,
                    "error_code": result.error_code,
                    "exhausted": exhausted,
                },
            )
            return

        delay = min(
            BASE_BACKOFF_SECONDS * (2 ** (email.attempts - 1)),
            MAX_BACKOFF_SECONDS,
        )
        delay += random.uniform(0, delay * 0.25)  # noqa: S311 - jitter, not cryptography
        await repository.mark_retry(
            email,
            available_at=now + timedelta(seconds=delay),
            error_code=result.error_code,
            error_message=result.error_message,
        )
        logger.warning(
            "email.retry_scheduled",
            extra={
                "event": "email.retry_scheduled",
                "email_message_id": str(email.id),
                "template": email.template,
                "attempts": email.attempts,
                "error_code": result.error_code,
                "delay_seconds": round(delay, 1),
            },
        )


__all__ = ["BASE_BACKOFF_SECONDS", "MAX_BACKOFF_SECONDS", "EmailWorker"]
