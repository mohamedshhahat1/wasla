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

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.telemetry import CallOutcome, Provider, ProviderCall, record_provider_call
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

# What a delivery attempt is counted under. `deliver` covers every attempt at
# sending; `suppress` is the separate story of a message not attempted at all,
# and keeping the two apart is what stops a suppression list looking like an
# outage on a dashboard.
DELIVER: Final = "deliver"
SUPPRESS: Final = "suppress"


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

        Two phases, and the split is what bounds the duplicate window.

        The claim is committed *before* any network call: recovery and
        ``claim_due`` share one transaction that marks a batch `sending` with
        the attempt counted, and it commits on its own. Only then does
        anything reach a provider. A worker that dies mid-batch therefore
        leaves the rows it had not reached still `sending` rather than
        rolling the whole claim back.

        Each message is then delivered in a transaction of its own, so the
        crash window is one message rather than the batch. Doing the whole
        sweep under a single commit - which this once did - meant a process
        killed on the fiftieth message re-sent the forty-nine before it, and
        that is duplicate mail measured in batches instead of in ones.

        The cost is a transaction per message, which is the right trade at
        this volume: transactional email is low-rate and a duplicated
        password reset is worth more than a saved round trip. Delivery stays
        at-least-once either way (ADR-042) - the window is narrowed, not
        closed.
        """
        moment = now or datetime.now(UTC)

        async with self._database.session() as session:
            repository = EmailOutboxRepository(session)

            recovered = await repository.recover_stuck(now=moment)
            if recovered:
                # A row left `sending` by a process that died between the
                # claim and its result. Expected after a crash, worth a loud
                # line at any other time.
                logger.warning(
                    "email.stuck_recovered",
                    extra={"event": "email.stuck_recovered", "recovered": recovered},
                )

            due = await repository.claim_due(now=moment, limit=self._claim_limit)
            claimed = [row.id for row in due]

        if not claimed:
            return 0

        handled = 0
        for email_id in claimed:
            try:
                async with self._database.session() as session:
                    repository = EmailOutboxRepository(session)
                    email = await repository.get_claimed(email_id)
                    if email is None:
                        # Recovered and re-claimed by another sweep, or the
                        # account it belonged to was deleted underneath us.
                        continue
                    await self._handle(repository, email, now=moment)
            except Exception:
                # Contained to the one row, which rolls back to `sending` and
                # returns to the queue through recovery. One poisonous
                # message must not strand every other workspace's mail.
                logger.exception(
                    "email.dispatch_failed",
                    extra={"email_message_id": str(email_id)},
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
            await record_provider_call(
                provider=Provider.EMAIL, operation=SUPPRESS, outcome=CallOutcome.SUCCESS
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
            # Counted, and deliberately not timed: nothing was sent. See the
            # note beside the send below.
            await record_provider_call(
                provider=Provider.EMAIL, operation=DELIVER, outcome=CallOutcome.FAILURE
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

        # The one statement in this method that leaves the process, and so the
        # only one whose duration belongs in a latency histogram. The two exits
        # above - a suppressed recipient, a template that will not render - are
        # counted as provider outcomes because that is what an operator reads
        # them as, but neither made a call, and timing a decision not to send
        # would put a microsecond in the same distribution as a fifteen-second
        # timeout.
        call = ProviderCall(provider=Provider.EMAIL, operation=DELIVER)
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
            await call.record(CallOutcome.SUCCESS)
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
            await call.record(CallOutcome.FAILURE)
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
        await call.record(CallOutcome.UNAVAILABLE)
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
