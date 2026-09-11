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

Two properties this module is responsible for, and both were absent:

**The workspace is the one that held the number when the event happened**, not
the one holding it now. Meta retries an undelivered webhook for up to seven
days, so the two differ for as long as a week after a number changes hands -
and resolving to the current holder puts one business's customer conversations
in another's inbox (MSG-01, ADR-101).

**A stored event that still owes work says so.** Enqueue failures are swallowed
here on purpose: a non-2xx would make Meta retry the whole delivery and
eventually disable the subscription, so a Redis outage must not become a
webhook outage. What was missing is the other half - the event stays
`RECEIVED` with a bounded reason, and `InboundRecoveryWorker` finishes it later
(MSG-02, ADR-102). `PROCESSED` means the projection landed *and* every handoff
it needed was accepted.
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
from app.db.models.conversation import Message, MessageKind
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppEvent, WhatsAppEventKind
from app.integrations.whatsapp.payload import (
    DeliveryStatus,
    InboundMessage,
    parse_webhook,
)
from app.repositories.conversation_repository import (
    ContactRepository,
    OutboundMessageDirectory,
)
from app.repositories.media_repository import MediaRepository
from app.repositories.whatsapp_repository import (
    UNKNOWN_NUMBER,
    OwnershipResolution,
    WhatsAppAccountDirectory,
    WhatsAppEventRepository,
)
from app.services.conversation_service import ConversationProjectionService
from app.services.follow_up_service import FollowUpService
from app.services.opt_out import is_stop_request
from app.workers.media_queue import MediaJob, MediaQueue
from app.workers.queue import AgentJob, AgentQueue

logger = get_logger(__name__)

# Why a stored event still owes work. Bounded tokens rather than sentences:
# they are written to `whatsapp_events.error`, printed by the operator command
# and carried in logs, so none of them may hold a payload fragment.
AGENT_NOT_QUEUED = "agent_enqueue_failed"
MEDIA_NOT_QUEUED = "media_enqueue_failed"


@dataclass(frozen=True, slots=True)
class _Handoff:
    """A stored event and whatever has to reach a queue before it is finished.

    At most one of the two, because an event is either a conversation to answer
    or a file to read, never both. Both `None` means the event is complete the
    moment it is projected - a delivery status, a reaction, a message on a
    number the workspace has released.
    """

    event: WhatsAppEvent
    agent_for_conversation: tuple[uuid.UUID, uuid.UUID] | None = None
    media_for_message: tuple[uuid.UUID, uuid.UUID] | None = None


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
    # Events whose owning workspace could not be established: a number nobody
    # has connected, an instant nobody held it, or two claims overlapping.
    # Counted apart from `unknown_accounts` because that bucket means "not our
    # number" and this one means "our number, but not now, and we will not
    # guess" (MSG-01).
    unowned: int = 0


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
        self._outbound = OutboundMessageDirectory(session)
        self._repositories: dict[uuid.UUID, WhatsAppEventRepository] = {}
        self._projections: dict[uuid.UUID, ConversationProjectionService] = {}
        self._follow_ups: dict[uuid.UUID, FollowUpService] = {}
        self._media: dict[uuid.UUID, MediaRepository] = {}
        self._contacts: dict[uuid.UUID, ContactRepository] = {}
        self._accounts: dict[str, WhatsAppAccount | None] = {}
        self._holdings: dict[str, list[WhatsAppAccount]] = {}

    async def ingest(self, payload: Mapping[str, Any]) -> IngestionOutcome:
        envelope = parse_webhook(payload)
        stored = duplicates = unknown = inactive = cancelled = opted_out = unowned = 0
        attachments: list[tuple[uuid.UUID, uuid.UUID]] = []
        ignored = envelope.ignored
        answering: list[tuple[uuid.UUID, uuid.UUID]] = []
        # What each stored event still owes, so its state can be advanced once
        # the enqueues below have said whether they landed.
        owed: list[_Handoff] = []

        # Messages and statuses are handled by the same loop because the storage
        # rules are identical; only the projection and the resolver differ.
        sources: list[InboundMessage | DeliveryStatus] = [
            *envelope.messages,
            *envelope.statuses,
        ]

        for source in sources:
            occurred_at = source.timestamp or datetime.now(UTC)
            is_message = isinstance(source, InboundMessage)

            reconciled: Message | None = None
            if isinstance(source, InboundMessage):
                resolution = await self._owner_at(source.phone_number_id, occurred_at)
            else:
                # A status names a message rather than a moment, so it is
                # resolved by that name first. Ownership is the fallback, for a
                # status whose message this deployment never sent.
                resolution, reconciled = await self._status_owner(source)

            account = resolution.account
            if account is None:
                if resolution.reason == UNKNOWN_NUMBER:
                    # Someone else's number, or one connected then removed. Not
                    # an error for us to report: Meta cannot fix it by retrying.
                    unknown += 1
                    logger.warning(
                        "whatsapp.unknown_phone_number_id",
                        extra={"phone_number_id": source.phone_number_id},
                    )
                else:
                    # Our number, but nobody held it when this happened. The
                    # one thing that must not follow is attributing it to
                    # whoever holds it now (MSG-01).
                    unowned += 1
                    logger.warning(
                        "whatsapp.event_without_owner",
                        extra={
                            "event": "whatsapp.event_without_owner",
                            "phone_number_id": source.phone_number_id,
                            "reason": resolution.reason,
                        },
                    )
                continue

            # A claim the workspace still holds may be paused, and a paused
            # number processes nothing. A claim it has already given up is a
            # different thing: the event belongs to a period when the number
            # was live, and recording it is history rather than traffic.
            historical = account.released_at is not None
            if not historical and not account.is_active:
                inactive += 1
                logger.info(
                    "whatsapp.account_disabled",
                    extra={"phone_number_id": source.phone_number_id},
                )
                continue

            repository = self._repository(account.tenant_id)
            event, created = await repository.record(
                account_id=account.id,
                event_id=source.event_id,
                kind=WhatsAppEventKind.MESSAGE if is_message else WhatsAppEventKind.STATUS,
                payload=source.raw,
                # A missing timestamp still needs a value to order by; arrival
                # time is the honest fallback.
                received_at=occurred_at,
            )
            if not created:
                # A replay. Projecting again would duplicate a message or
                # re-advance a status, so the event stops here - and it stops
                # here whatever state the stored event is in. A redelivery is
                # not a recovery mechanism: if the first delivery left work
                # owing, the sweeper owns finishing it, and enqueueing from
                # here as well would be two paths racing to queue one agent
                # turn (ADR-102).
                duplicates += 1
                continue

            stored += 1
            projection = self._projection(account.tenant_id)
            if isinstance(source, InboundMessage):
                message = await projection.project_message(account_id=account.id, message=source)
                owed.append(
                    self._message_handoff(
                        event=event,
                        tenant_id=account.tenant_id,
                        message=message,
                        source=source,
                        historical=historical,
                        attachments=attachments,
                        answering=answering,
                    )
                )
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
                # nothing for an agent to reply to, so the event owes nothing
                # beyond the projection it just received - including when the
                # message is unknown, which is ordinary traffic rather than a
                # failure and must not leave the event owing work for ever.
                await projection.project_status(status=source, message=reconciled)
                owed.append(_Handoff(event=event))

        if stored:
            await self._session.flush()

        queued_conversations = await self._enqueue(answering)
        queued_media = await self._enqueue_media(attachments)
        self._settle(owed, conversations=queued_conversations, media=queued_media)

        return IngestionOutcome(
            stored=stored,
            duplicates=duplicates,
            unknown_accounts=unknown,
            inactive_accounts=inactive,
            ignored=ignored,
            queued=len(queued_conversations),
            cancelled_follow_ups=cancelled,
            media_queued=len(queued_media),
            opt_outs=opted_out,
            unowned=unowned,
        )

    def _message_handoff(
        self,
        *,
        event: WhatsAppEvent,
        tenant_id: uuid.UUID,
        message: Message,
        source: InboundMessage,
        historical: bool,
        attachments: list[tuple[uuid.UUID, uuid.UUID]],
        answering: list[tuple[uuid.UUID, uuid.UUID]],
    ) -> _Handoff:
        """What still has to happen for this message, after it is stored.

        Three messages owe nothing, and each for its own reason.

        **A message on a number the workspace has since released** is being
        recorded, not answered. The workspace cannot send through that number
        any more - `MessagingService._dispatch` refuses a claim that is not
        live - so an agent turn could only end in a refusal, after paying for
        an inference (MSG-01).

        **A message Wasla cannot read** - a reaction, an order, a system
        notice - has no content to answer. It was still being handed to an
        agent, which was told a customer had sent `[unsupported]` and answered
        anyway: one billed inference and possibly one reply to nothing, three
        times over for somebody tapping a thumbs-up on three messages (MSG-19).

        **A message carrying a file** is answered by the media worker once the
        file has been read, because answering a photograph before looking at it
        produces a reply about nothing (ADR-092). Its debt is the download, not
        the turn.
        """
        if source.media is not None:
            attachments.append((tenant_id, message.id))
            return _Handoff(event=event, media_for_message=(tenant_id, message.id))
        if historical or message.kind is MessageKind.UNSUPPORTED:
            return _Handoff(event=event)
        answering.append((tenant_id, message.conversation_id))
        return _Handoff(event=event, agent_for_conversation=(tenant_id, message.conversation_id))

    def _settle(
        self,
        owed: list[_Handoff],
        *,
        conversations: set[tuple[uuid.UUID, uuid.UUID]],
        media: set[tuple[uuid.UUID, uuid.UUID]],
    ) -> None:
        """Advance each stored event to what actually became of it.

        This is the half of the Redis-outage trade that was missing. Swallowing
        an enqueue failure is right - a non-2xx would make Meta retry the whole
        delivery and eventually disable the subscription - but swallowing it
        *silently* left a message stored, visible in the inbox, and owed an
        answer nobody would ever give, with no query, no metric and no command
        able to find it (MSG-02).

        An event that owes nothing more is `PROCESSED`. One whose handoff was
        refused stays `RECEIVED` carrying a bounded reason, which is what
        `InboundRecoveryWorker` claims and what the unprocessed gauge counts.
        """
        for handoff in owed:
            repository = self._repository(handoff.event.tenant_id)
            pending = handoff.agent_for_conversation
            if pending is not None and pending not in conversations:
                repository.mark_unprocessed(handoff.event, reason=AGENT_NOT_QUEUED)
                continue
            pending = handoff.media_for_message
            if pending is not None and pending not in media:
                repository.mark_unprocessed(handoff.event, reason=MEDIA_NOT_QUEUED)
                continue
            repository.mark_processed(handoff.event)

    async def _status_owner(
        self,
        status: DeliveryStatus,
    ) -> tuple[OwnershipResolution, Message | None]:
        """Which workspace a delivery status belongs to, and the message it names.

        **By provider message id first.** A status is a fact about a message
        that already exists, and that message carries its own workspace - which
        is the only resolution that survives the number moving to somebody else
        while the status is in flight (MSG-04). The candidate workspaces are
        the ones that have held this number, so an id Meta invented cannot
        reach anything.

        Ownership at the status's own instant is the fallback, and it decides
        only where the raw event is filed. A status naming no message we hold
        projects nothing either way - a template sent from Meta's own console,
        or traffic predating this number being connected - so the fallback
        exists to keep the event log tidy rather than to find anything.
        """
        holders = await self._holders(status.phone_number_id)
        message = await self._outbound.find_by_wa_message_id(
            status.message_id,
            tenant_ids=[holder.tenant_id for holder in holders],
        )
        if message is not None:
            owner = next(
                (holder for holder in holders if holder.tenant_id == message.tenant_id),
                None,
            )
            if owner is not None:
                return OwnershipResolution.held_by(owner), message

        instant = status.timestamp or datetime.now(UTC)
        return await self._owner_at(status.phone_number_id, instant), None

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

    async def _enqueue_media(
        self,
        attachments: list[tuple[uuid.UUID, uuid.UUID]],
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        """Ask the media worker to read each file that arrived.

        One job per file rather than per conversation, unlike agent jobs: two
        photographs are two things to read, and collapsing them would leave one
        unread.

        A queue failure is logged and swallowed for the same reason it is on the
        agent path - a non-2xx answer would make Meta retry the whole delivery
        and eventually disable the subscription, so a Redis outage must not
        become a webhook outage. Returns the messages whose file actually
        reached the queue, so `_settle` can leave the rest owing work for the
        sweeper rather than marking them finished (MSG-02).
        """
        if self._media_queue is None or not attachments:
            return set()

        media_rows = await self._media_for(attachments)
        queued: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for tenant_id, message_id, media_id in media_rows:
            try:
                await self._media_queue.enqueue(MediaJob(tenant_id=tenant_id, media_id=media_id))
            except RedisError:
                logger.warning("media.enqueue_failed", extra={"media_id": str(media_id)})
                continue
            queued.add((tenant_id, message_id))
        return queued

    async def _media_for(
        self,
        attachments: list[tuple[uuid.UUID, uuid.UUID]],
    ) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]]:
        """Resolve message ids to the media rows just written for them.

        The message id is carried through alongside the media id because the
        event that owes this download is identified by its message, and a file
        that produced no media row owes nothing that can be queued.
        """
        resolved: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
        for tenant_id, message_id in attachments:
            repository = self._media_repository(tenant_id)
            media = await repository.get_for_message(message_id)
            if media is not None:
                resolved.append((tenant_id, message_id, media.id))
        return resolved

    def _media_repository(self, tenant_id: uuid.UUID) -> MediaRepository:
        repository = self._media.get(tenant_id)
        if repository is None:
            repository = MediaRepository(self._session, tenant_id=tenant_id)
            self._media[tenant_id] = repository
        return repository

    async def _enqueue(
        self,
        conversations: list[tuple[uuid.UUID, uuid.UUID]],
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        """Ask a worker to look at each conversation that received a message.

        One job per conversation however many messages arrived: the worker reads
        the conversation fresh, so a second job would only repeat the first.

        Whether an agent should answer at all is not decided here. The
        orchestrator refuses a conversation a human has taken over, and keeping
        that judgement in one place is worth the occasional wasted job.

        A queue failure is logged and swallowed. The messages are already stored,
        and a non-2xx answer would make Meta retry the whole delivery and
        eventually disable the subscription, so a Redis outage must not become a
        webhook outage. Returns the conversations that were genuinely queued, so
        an event whose turn was refused stays `RECEIVED` and is recoverable
        rather than being marked finished and forgotten (MSG-02).
        """
        if self._queue is None or not conversations:
            return set()

        queued: set[tuple[uuid.UUID, uuid.UUID]] = set()
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
            queued.add((tenant_id, conversation_id))
        return queued

    async def _owner_at(self, phone_number_id: str, instant: datetime) -> OwnershipResolution:
        """The workspace that held this number when the event happened.

        The live claim is cached and checked here rather than inside the
        directory, so ordinary traffic - a number nobody has handed over,
        several messages in one delivery - costs one query for the whole
        delivery and never touches the history walk. Only an event predating
        the live claim, or arriving on a number with no live claim at all, pays
        for the fuller lookup (ADR-101).
        """
        live = await self._live_account(phone_number_id)
        if live is not None and live.held_at(instant):
            return OwnershipResolution.held_by(live)
        return await self._directory.owner_at(phone_number_id, instant)

    async def _live_account(self, phone_number_id: str) -> WhatsAppAccount | None:
        """The current claim, resolved once per delivery."""
        if phone_number_id not in self._accounts:
            self._accounts[phone_number_id] = await self._directory.get_by_phone_number_id(
                phone_number_id
            )
        return self._accounts[phone_number_id]

    async def _holders(self, phone_number_id: str) -> list[WhatsAppAccount]:
        """Every claim this number has ever carried, cached per delivery."""
        if phone_number_id not in self._holdings:
            self._holdings[phone_number_id] = await self._directory.holders_of(phone_number_id)
        return self._holdings[phone_number_id]

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
