"""Inbound WhatsApp ingestion.

The webhook's only job: resolve the workspace, store the event once, project it,
ask a worker to answer, return. No inference happens here. A model call takes
longer than Meta's retry window, so running it on this path would duplicate the
work rather than deliver it.

Jobs are enqueued before the request's transaction commits, which is the lesser
of two evils. A job whose transaction then rolled back names a conversation that
does not exist, and the worker dead-letters it with a log. Enqueueing after the
commit would instead risk a stored message that no worker was ever told about,
and that is the failure a customer notices.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.campaign import OptOutSource
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppEventKind
from app.integrations.whatsapp.payload import (
    DeliveryStatus,
    InboundMessage,
    parse_webhook,
)
from app.repositories.conversation_repository import ContactRepository
from app.repositories.media_repository import MediaRepository
from app.repositories.whatsapp_repository import (
    WhatsAppAccountDirectory,
    WhatsAppEventRepository,
)
from app.services.conversation_service import ConversationProjectionService
from app.services.follow_up_service import FollowUpService
from app.services.opt_out import is_stop_request
from app.workers.media_queue import MediaJob, MediaQueue
from app.workers.queue import AgentJob, AgentQueue

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """What happened to a webhook delivery. Every event lands in exactly one.

    `queued` is not one of those buckets. It counts conversations handed to a
    worker, which is at most the number of messages stored and often fewer.

    `media_queued` counts files handed to the media worker. A conversation that
    received one is deliberately *not* counted in `queued`: its agent job is
    enqueued by that worker once the file has been read, because answering a
    photograph before looking at it produces a reply about nothing.
    """

    stored: int = 0
    duplicates: int = 0
    unknown_accounts: int = 0
    inactive_accounts: int = 0
    ignored: int = 0
    queued: int = 0
    cancelled_follow_ups: int = 0
    media_queued: int = 0
    opt_outs: int = 0


class WhatsAppIngestionService:
    """Turns one webhook delivery into stored events and conversation rows.

    The queue is optional. Without it nothing is asked to answer, but events are
    still stored and projected, which is all the projection tests need.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        queue: AgentQueue | None = None,
        media_queue: MediaQueue | None = None,
    ) -> None:
        self._session = session
        self._queue = queue
        self._media_queue = media_queue
        self._directory = WhatsAppAccountDirectory(session)
        self._repositories: dict[uuid.UUID, WhatsAppEventRepository] = {}
        self._projections: dict[uuid.UUID, ConversationProjectionService] = {}
        self._follow_ups: dict[uuid.UUID, FollowUpService] = {}
        self._media: dict[uuid.UUID, MediaRepository] = {}
        self._contacts: dict[uuid.UUID, ContactRepository] = {}
        self._accounts: dict[str, WhatsAppAccount | None] = {}

    async def ingest(self, payload: Mapping[str, Any]) -> IngestionOutcome:
        envelope = parse_webhook(payload)
        stored = duplicates = unknown = inactive = cancelled = opted_out = 0
        attachments: list[tuple[uuid.UUID, uuid.UUID]] = []
        ignored = envelope.ignored
        answering: list[tuple[uuid.UUID, uuid.UUID]] = []

        # Messages and statuses are handled by the same loop because the storage
        # rules are identical; only the projection differs.
        sources: list[InboundMessage | DeliveryStatus] = [
            *envelope.messages,
            *envelope.statuses,
        ]

        for source in sources:
            account = await self._account(source.phone_number_id)
            if account is None:
                # Someone else's number, or one connected then removed. Not an
                # error for us to report: Meta cannot fix it by retrying.
                unknown += 1
                logger.warning(
                    "whatsapp.unknown_phone_number_id",
                    extra={"phone_number_id": source.phone_number_id},
                )
                continue

            if not account.is_active:
                inactive += 1
                logger.info(
                    "whatsapp.account_disabled",
                    extra={"phone_number_id": source.phone_number_id},
                )
                continue

            is_message = isinstance(source, InboundMessage)
            repository = self._repository(account.tenant_id)
            _, created = await repository.record(
                account_id=account.id,
                event_id=source.event_id,
                kind=WhatsAppEventKind.MESSAGE if is_message else WhatsAppEventKind.STATUS,
                payload=source.raw,
                # A missing timestamp still needs a value to order by; arrival
                # time is the honest fallback.
                received_at=source.timestamp or datetime.now(UTC),
            )
            if not created:
                # A replay. Projecting again would duplicate a message or
                # re-advance a status, so the event stops here.
                duplicates += 1
                continue

            stored += 1
            projection = self._projection(account.tenant_id)
            if isinstance(source, InboundMessage):
                message = await projection.project_message(account_id=account.id, message=source)
                if source.media is not None:
                    # Read first, answered after. The media worker enqueues the
                    # agent job once there is something to answer with, so this
                    # conversation is deliberately left out of `answering`.
                    attachments.append((account.tenant_id, message.id))
                else:
                    answering.append((account.tenant_id, message.conversation_id))
                # The customer has spoken, so any nudge waiting on this
                # conversation has lost its reason. Cancelled here, on the
                # inbound path, rather than left for the follow-up worker to
                # notice: the worker may sweep before this transaction's effects
                # are visible to it, and a message that talks over someone who
                # is already talking is exactly what a follow-up must never do.
                cancelled += await self._follow_up_service(
                    account.tenant_id
                ).cancel_for_conversation(conversation_id=message.conversation_id)
                # A customer asking to stop is honoured here rather than by a
                # worker. It costs one string comparison, and the alternative is
                # a window in which a campaign sweep could write to somebody who
                # has already said no.
                opted_out += await self._record_opt_out(
                    tenant_id=account.tenant_id,
                    wa_id=source.from_number,
                    text=source.text,
                )
            else:
                # A delivery status tells us about our own message. There is
                # nothing for an agent to reply to.
                await projection.project_status(status=source)

        if stored:
            await self._session.flush()

        queued = await self._enqueue(answering)
        media_queued = await self._enqueue_media(attachments)

        return IngestionOutcome(
            stored=stored,
            duplicates=duplicates,
            unknown_accounts=unknown,
            inactive_accounts=inactive,
            ignored=ignored,
            queued=queued,
            cancelled_follow_ups=cancelled,
            media_queued=media_queued,
            opt_outs=opted_out,
        )

    async def _record_opt_out(
        self,
        *,
        tenant_id: uuid.UUID,
        wa_id: str,
        text: str | None,
    ) -> int:
        """Mark the sender as opted out of campaigns if that is what they said.

        Deliberately narrow: only a message that is *entirely* a stop word
        counts. See `app.services.opt_out` for why the matcher is this crude.

        This does not silence the agent. A customer writing "stop" mid-
        conversation is refusing marketing, not refusing an answer, and deciding
        otherwise from one word would leave people talking to nobody.
        """
        if not is_stop_request(text):
            return 0

        contact = await self._contact_repository(tenant_id).get_by_wa_id(wa_id)
        if contact is None or contact.marketing_opt_out_at is not None:
            # Already opted out: the first refusal is the one that counts, and
            # moving the timestamp would make it look freshly decided.
            return 0

        contact.marketing_opt_out_at = datetime.now(UTC)
        contact.opt_out_source = OptOutSource.CUSTOMER
        logger.info("campaign.opt_out_requested", extra={"contact_id": str(contact.id)})
        return 1

    def _contact_repository(self, tenant_id: uuid.UUID) -> ContactRepository:
        repository = self._contacts.get(tenant_id)
        if repository is None:
            repository = ContactRepository(self._session, tenant_id=tenant_id)
            self._contacts[tenant_id] = repository
        return repository

    async def _enqueue_media(self, attachments: list[tuple[uuid.UUID, uuid.UUID]]) -> int:
        """Ask the media worker to read each file that arrived.

        One job per file rather than per conversation, unlike agent jobs: two
        photographs are two things to read, and collapsing them would leave one
        unread.

        A queue failure is logged and swallowed for the same reason it is on the
        agent path - a non-2xx answer would make Meta retry the whole delivery
        and eventually disable the subscription, so a Redis outage must not
        become a webhook outage. The cost here is a file that stays PENDING and
        a conversation whose reply waits for someone to requeue it, which is
        recoverable; the message itself is stored.
        """
        if self._media_queue is None or not attachments:
            return 0

        media_rows = await self._media_for(attachments)
        queued = 0
        for tenant_id, media_id in media_rows:
            try:
                await self._media_queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))
            except RedisError:
                logger.warning("media.enqueue_failed", extra={"media_id": str(media_id)})
                continue
            queued += 1
        return queued

    async def _media_for(
        self,
        attachments: list[tuple[uuid.UUID, uuid.UUID]],
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Resolve message ids to the media rows just written for them."""
        resolved: list[tuple[uuid.UUID, uuid.UUID]] = []
        for tenant_id, message_id in attachments:
            repository = self._media_repository(tenant_id)
            media = await repository.get_for_message(message_id)
            if media is not None:
                resolved.append((tenant_id, media.id))
        return resolved

    def _media_repository(self, tenant_id: uuid.UUID) -> MediaRepository:
        repository = self._media.get(tenant_id)
        if repository is None:
            repository = MediaRepository(self._session, tenant_id=tenant_id)
            self._media[tenant_id] = repository
        return repository

    async def _enqueue(self, conversations: list[tuple[uuid.UUID, uuid.UUID]]) -> int:
        """Ask a worker to look at each conversation that received a message.

        One job per conversation however many messages arrived: the worker reads
        the conversation fresh, so a second job would only repeat the first.

        Whether an agent should answer at all is not decided here. The
        orchestrator refuses a conversation a human has taken over, and keeping
        that judgement in one place is worth the occasional wasted job.

        A queue failure is logged and swallowed. The messages are already stored,
        and a non-2xx answer would make Meta retry the whole delivery and
        eventually disable the subscription, so a Redis outage must not become a
        webhook outage.
        """
        if self._queue is None or not conversations:
            return 0

        queued = 0
        for tenant_id, conversation_id in dict.fromkeys(conversations):
            job = AgentJob(tenant_id=tenant_id, conversation_id=conversation_id)
            try:
                await self._queue.enqueue(job)
            except RedisError:
                logger.warning(
                    "agent.enqueue_failed",
                    extra={"conversation_id": str(conversation_id)},
                )
                continue
            queued += 1
        return queued

    async def _account(self, phone_number_id: str) -> WhatsAppAccount | None:
        """Resolve once per delivery; a batch usually shares one number."""
        if phone_number_id not in self._accounts:
            self._accounts[phone_number_id] = await self._directory.get_by_phone_number_id(
                phone_number_id
            )
        return self._accounts[phone_number_id]

    def _repository(self, tenant_id: uuid.UUID) -> WhatsAppEventRepository:
        repository = self._repositories.get(tenant_id)
        if repository is None:
            repository = WhatsAppEventRepository(self._session, tenant_id=tenant_id)
            self._repositories[tenant_id] = repository
        return repository

    def _follow_up_service(self, tenant_id: uuid.UUID) -> FollowUpService:
        """Cached per tenant, like the projection: one delivery can carry several
        messages for the same workspace.

        No settings are passed because nothing on this path sends anything - only
        `dispatch` needs them, and that runs in the worker.
        """
        service = self._follow_ups.get(tenant_id)
        if service is None:
            service = FollowUpService(session=self._session, tenant_id=tenant_id)
            self._follow_ups[tenant_id] = service
        return service

    def _projection(self, tenant_id: uuid.UUID) -> ConversationProjectionService:
        projection = self._projections.get(tenant_id)
        if projection is None:
            projection = ConversationProjectionService(
                session=self._session,
                tenant_id=tenant_id,
            )
            self._projections[tenant_id] = projection
        return projection
