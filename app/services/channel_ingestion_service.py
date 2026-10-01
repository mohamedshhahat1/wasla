"""Inbound ingestion, for every channel (OMNI-006).

An adapter parses its provider's webhook into neutral events; this stores them,
projects them and hands them on. It is the logic that used to be
`WhatsAppIngestionService`, moved rather than copied, because it is the most
correctness-dense code in the product and a second channel must inherit it, not
re-derive it:

**The workspace is the one that held the connection when the event happened**
(ADR-101, MSG-01) - resolved from the connection and never from the sender.

**A stored event that still owes work says so** (ADR-102, MSG-02): an enqueue
Redis refused leaves the event `RECEIVED` with a bounded reason for
`InboundRecoveryWorker`; `PROCESSED` means projected *and* handed off.

**One message's refusal is contained** (MEDIA-05): each event is stored and
projected inside its own savepoint, so a value PostgreSQL refuses costs that
event alone.

**Only a new customer message has consequences** (OMNI-005). A duplicate, an
echo of the business's own send, or an id that collides with a message Wasla
sent opens no conversation, touches no window, queues no agent or file,
cancels no follow-up and opts nobody out - and is counted.

**Nothing refused disappears** (OMNI-010, OMNI-021): the adapter's refusals are
counted by channel and reason, and so is every outcome here.

Jobs are enqueued before the request's transaction commits - the lesser of two
evils, argued in ADR-089.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType

from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter, IdentityScopeRef
from app.channels.inbound import Identifier, InboundEvent, InboundKind, ParsedDelivery
from app.core.logging import get_logger
from app.core.telemetry import record_inbound_outcomes, record_inbound_refusals
from app.db.errors import is_data_exception
from app.db.models.campaign import OptOutSource
from app.db.models.channel import ChannelConnection
from app.db.models.channel_event import ChannelEvent, ChannelEventKind
from app.db.models.conversation import Contact, Conversation, Message, MessageKind
from app.repositories.channel_event_repository import ChannelEventRepository
from app.repositories.channel_repository import (
    UNKNOWN_CONNECTION,
    ConnectionDirectory,
    ConnectionResolution,
    ContactIdentityRepository,
)
from app.repositories.conversation_repository import (
    ConversationRepository,
    OutboundMessageDirectory,
)
from app.repositories.media_repository import MediaRepository
from app.services.contact_identity_service import ContactIdentityService
from app.services.conversation_service import ConversationProjectionService, ProjectionOutcome
from app.services.follow_up_service import FollowUpService
from app.services.media_outcomes import MediaReason, status_for, text_for
from app.services.opt_out import is_stop_request
from app.workers.media_queue import MediaJob, MediaQueue
from app.workers.queue import AgentJob, AgentQueue

logger = get_logger(__name__)

# Why a stored event still owes work, or was given up. Bounded tokens: they are
# written to the event's `error`, printed by the operator command and logged.
AGENT_NOT_QUEUED = "agent_enqueue_failed"
MEDIA_NOT_QUEUED = "media_enqueue_failed"
PROVIDER_ID_COLLISION = "provider_id_collision"

_EVENT_KINDS: Mapping[InboundKind, ChannelEventKind] = MappingProxyType(
    {
        InboundKind.MESSAGE: ChannelEventKind.MESSAGE,
        InboundKind.STATUS: ChannelEventKind.STATUS,
        InboundKind.ECHO: ChannelEventKind.ECHO,
    }
)


@dataclass(frozen=True, slots=True)
class _Handoff:
    """A stored event and whatever has to reach a queue before it is finished.

    At most one of the two, because an event is either a conversation to answer
    or files to read, never both. Both `None` means the event is complete the
    moment it is projected.
    """

    event: ChannelEvent
    agent_for_conversation: tuple[uuid.UUID, uuid.UUID] | None = None
    media_for_message: tuple[uuid.UUID, uuid.UUID] | None = None


@dataclass(frozen=True, slots=True)
class IngestionOutcome:
    """What happened to a webhook delivery. Every event lands in exactly one bucket.

    `queued` and `media_queued` are not buckets: they count conversations and
    files handed to a worker. `ignored` is every refusal the adapter counted;
    `refused` says why, by reason.
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
    # Our connection, but not held by anybody at the event's instant (MSG-01).
    unowned: int = 0
    # Refused by PostgreSQL as content, contained in their own savepoints.
    rejected: int = 0
    # Echoes of the business's own sends: stored as evidence, never projected.
    echoes: int = 0
    # Provider ids naming a message that is not this customer's on this
    # connection (OMNI-005): stored as evidence, never projected.
    collisions: int = 0
    # Identifiers attached by provider pairing, and pairings that would have
    # merged two existing contacts and so were not made (ADR-118).
    paired_identities: int = 0
    identity_conflicts: int = 0
    refused: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(slots=True)
class _Step:
    """What one event produced, folded into the delivery only once its savepoint holds."""

    stored: int = 0
    duplicates: int = 0
    unknown: int = 0
    inactive: int = 0
    unowned: int = 0
    cancelled: int = 0
    opted_out: int = 0
    echoes: int = 0
    collisions: int = 0
    paired: int = 0
    conflicts: int = 0
    attachments: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    answering: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = field(default_factory=list)
    owed: list[_Handoff] = field(default_factory=list)


class ChannelIngestionService:
    """Turns one parsed delivery into stored events and conversation rows."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        adapter: ChannelAdapter,
        queue: AgentQueue | None = None,
        media_queue: MediaQueue | None = None,
    ) -> None:
        self._session = session
        self._adapter = adapter
        self._queue = queue
        self._media_queue = media_queue
        self._directory = ConnectionDirectory(session)
        self._outbound = OutboundMessageDirectory(session)
        self._live: dict[str, ChannelConnection | None] = {}
        self._holdings: dict[str, list[ChannelConnection]] = {}
        self._scopes: dict[tuple[uuid.UUID, str], IdentityScopeRef] = {}

    async def ingest(self, delivery: ParsedDelivery) -> IngestionOutcome:
        totals = _Step()
        rejected = 0

        for event in delivery.events:
            step = _Step()
            try:
                # One savepoint per event (MEDIA-05), flushed inside it, so a
                # value PostgreSQL will not store is refused against this event
                # alone and its siblings are kept.
                async with self._session.begin_nested():
                    await self._ingest_one(event, step)
                    await self._session.flush()
            except DBAPIError as error:
                # Only a refusal of the *content* is contained. The database
                # down, a lost connection, a deadlock: not facts about this
                # event, so the delivery still fails and is retried (MSG-03).
                if not is_data_exception(error):
                    raise
                rejected += 1
                logger.error(
                    "channel.event_rejected_by_database",
                    extra={
                        "event": "channel.event_rejected_by_database",
                        "channel": event.channel.value,
                        "kind": event.kind.value,
                    },
                )
                continue
            _fold(totals, step)

        queued_conversations = await self._enqueue(totals.answering)
        queued_media = await self._enqueue_media(totals.attachments)
        self._settle(totals.owed, conversations=queued_conversations, media=queued_media)

        outcome = IngestionOutcome(
            stored=totals.stored,
            duplicates=totals.duplicates,
            unknown_accounts=totals.unknown,
            inactive_accounts=totals.inactive,
            ignored=delivery.refused_total,
            queued=len(queued_conversations),
            cancelled_follow_ups=totals.cancelled,
            media_queued=len(queued_media),
            opt_outs=totals.opted_out,
            unowned=totals.unowned,
            rejected=rejected,
            echoes=totals.echoes,
            collisions=totals.collisions,
            paired_identities=totals.paired,
            identity_conflicts=totals.conflicts,
            refused=MappingProxyType(
                {reason.value: count for reason, count in delivery.refused.items()}
            ),
        )
        channel = self._adapter.channel.value
        await record_inbound_refusals(channel, outcome.refused)
        await record_inbound_outcomes(
            channel,
            {
                "stored": outcome.stored,
                "duplicate": outcome.duplicates,
                "echo": outcome.echoes,
                "collision": outcome.collisions,
                "unknown_connection": outcome.unknown_accounts,
                "inactive_connection": outcome.inactive_accounts,
                "unowned": outcome.unowned,
                "rejected": outcome.rejected,
                "identity_conflict": outcome.identity_conflicts,
            },
        )
        return outcome

    # ------------------------------------------------------------- one event

    async def _ingest_one(self, event: InboundEvent, step: _Step) -> None:
        occurred_at = event.occurred_at or datetime.now(UTC)

        resolved_message: Message | None = None
        if (
            event.kind is InboundKind.STATUS
            and event.status is not None
            and event.status.message_id is not None
        ):
            resolution, resolved_message = await self._status_owner(
                event, event.status.message_id, occurred_at
            )
        else:
            resolution = await self._owner_at(event.connection_key, occurred_at)

        connection = resolution.connection
        if connection is None:
            if resolution.reason == UNKNOWN_CONNECTION:
                # Somebody else's connection, or one connected then removed.
                # Not an error to report: a retry cannot fix it.
                step.unknown += 1
                logger.warning(
                    "channel.unknown_connection",
                    extra={"event": "channel.unknown_connection", "channel": event.channel.value},
                )
            else:
                # Ours, but nobody held it when this happened - and the one
                # thing that must not follow is filing it with today's holder.
                step.unowned += 1
                logger.warning(
                    "channel.event_without_owner",
                    extra={
                        "event": "channel.event_without_owner",
                        "channel": event.channel.value,
                        "reason": resolution.reason,
                    },
                )
            return

        # A paused connection processes nothing; one already given up is
        # history rather than traffic, recorded and not answered (MSG-01).
        historical = connection.released_at is not None
        if not historical and not connection.is_active:
            step.inactive += 1
            logger.info(
                "channel.connection_disabled",
                extra={"event": "channel.connection_disabled", "channel": event.channel.value},
            )
            return

        events = ChannelEventRepository(self._session, tenant_id=connection.tenant_id)
        stored = await events.store(
            connection_id=connection.id,
            channel=connection.channel,
            event_id=event.event_id,
            kind=_EVENT_KINDS[event.kind],
            payload=dict(event.raw),
            # A missing timestamp still needs a value to order by.
            received_at=occurred_at,
        )
        if stored.event is None:
            # The id is another connection's in this workspace (ADR-120).
            step.collisions += 1
            return
        if not stored.created:
            # A replay. Whatever state the stored event is in, the sweeper owns
            # finishing it: two paths racing to queue one turn is the duplicate
            # reply this design exists to prevent (ADR-102).
            step.duplicates += 1
            return

        step.stored += 1
        record = stored.event
        projection = ConversationProjectionService(
            session=self._session, tenant_id=connection.tenant_id
        )

        if event.kind is InboundKind.ECHO:
            # The business's own message, reported back. Evidence only: it is
            # nobody's turn to answer, it opens no window and it is not a
            # customer being active (OMNI-005, OMNI-018).
            step.echoes += 1
            step.owed.append(_Handoff(event=record))
            return

        if event.kind is InboundKind.STATUS:
            await self._project_status(event, connection, projection, resolved_message)
            step.owed.append(_Handoff(event=record))
            return

        await self._project_message(
            event=event,
            record=record,
            connection=connection,
            projection=projection,
            events=events,
            historical=historical,
            step=step,
            occurred_at=occurred_at,
        )

    async def _project_message(
        self,
        *,
        event: InboundEvent,
        record: ChannelEvent,
        connection: ChannelConnection,
        projection: ConversationProjectionService,
        events: ChannelEventRepository,
        historical: bool,
        step: _Step,
        occurred_at: datetime,
    ) -> None:
        # Asked before the sender is resolved, so a message that must not be
        # projected leaves no contact, conversation or window behind.
        screened = await projection.screen(connection=connection, event=event)
        if screened is not None:
            if screened.outcome is ProjectionOutcome.COLLISION:
                step.collisions += 1
                events.mark_failed(record, reason=PROVIDER_ID_COLLISION)
            else:
                step.duplicates += 1
                step.owed.append(_Handoff(event=record))
            return

        scopes = {
            identifier: await self._scope(connection, identifier) for identifier in event.sender
        }
        sender = await ContactIdentityService(
            session=self._session, tenant_id=connection.tenant_id
        ).resolve_sender(
            connection=connection,
            identifiers=event.sender,
            scopes=scopes,
            participant_preference=self._adapter.participant_preference,
            anchor_preference=self._adapter.anchor_preference,
            profile_name=event.profile_name,
            seen_at=occurred_at,
        )
        step.paired += sender.paired
        step.conflicts += int(sender.conflict)

        projected = await projection.project_message(
            connection=connection, event=event, sender=sender
        )
        message = projected.message
        if not projected.is_new or message is None:
            # Lost a race to an identical delivery between the screen and here.
            step.duplicates += 1
            step.owed.append(_Handoff(event=record))
            return

        if historical and event.attachments:
            await self._record_unprocessed_media(connection.tenant_id, message)
        step.owed.append(
            self._message_handoff(
                record=record,
                tenant_id=connection.tenant_id,
                message=message,
                has_attachments=bool(event.attachments),
                historical=historical,
                step=step,
            )
        )
        # The customer has spoken, so a nudge waiting on this conversation has
        # lost its reason - cancelled here, on the inbound path, before the
        # follow-up worker can talk over them.
        step.cancelled += await FollowUpService(
            session=self._session, tenant_id=connection.tenant_id
        ).cancel_for_conversation(conversation_id=message.conversation_id)
        # A customer asking to stop is honoured here rather than by a worker.
        step.opted_out += self._record_opt_out(sender.contact, text=event.text)

    async def _project_status(
        self,
        event: InboundEvent,
        connection: ChannelConnection,
        projection: ConversationProjectionService,
        resolved: Message | None,
    ) -> None:
        """A report about a message the business sent. Nothing is owed beyond projecting it."""
        update = event.status
        if update is not None and update.watermark is not None:
            conversation = await self._conversation_of(connection, event.sender)
            if conversation is not None:
                await projection.project_watermark(
                    event=event, connection=connection, conversation=conversation
                )
            return
        await projection.project_status(event=event, connection=connection, message=resolved)

    async def _conversation_of(
        self, connection: ChannelConnection, sender: tuple[Identifier, ...]
    ) -> Conversation | None:
        """The conversation a watermark reports on, found and never created."""
        identities = ContactIdentityRepository(self._session, tenant_id=connection.tenant_id)
        for identifier in sender:
            scope = await self._scope(connection, identifier)
            identity = await identities.find(
                channel=connection.channel,
                kind=identifier.kind,
                scope=scope.scope,
                scope_ref=scope.scope_ref,
                value=identifier.value,
            )
            if identity is not None:
                return await ConversationRepository(
                    self._session, tenant_id=connection.tenant_id
                ).get_for_contact(contact_id=identity.contact_id, account_id=connection.id)
        return None

    def _message_handoff(
        self,
        *,
        record: ChannelEvent,
        tenant_id: uuid.UUID,
        message: Message,
        has_attachments: bool,
        historical: bool,
        step: _Step,
    ) -> _Handoff:
        """What still has to happen for a new customer message.

        A message on a connection the workspace has released is recorded, not
        answered (MSG-01, MEDIA-08); one Wasla cannot read has no content to
        answer (MSG-19); one carrying files is answered once they are read
        (ADR-092).
        """
        if historical:
            return _Handoff(event=record)
        if has_attachments:
            step.attachments.append((tenant_id, message.id))
            return _Handoff(event=record, media_for_message=(tenant_id, message.id))
        if message.kind is MessageKind.UNSUPPORTED:
            return _Handoff(event=record)
        step.answering.append((tenant_id, message.conversation_id, message.id))
        return _Handoff(event=record, agent_for_conversation=(tenant_id, message.conversation_id))

    async def _record_unprocessed_media(self, tenant_id: uuid.UUID, message: Message) -> None:
        """Keep a released connection's files as history, and never fetch them (MEDIA-08)."""
        files = await MediaRepository(self._session, tenant_id=tenant_id).list_for_message(
            message.id
        )
        for media in files:
            if media.is_resolved:
                continue
            media.status = status_for(MediaReason.CHANNEL_UNAVAILABLE)
            media.last_error = text_for(MediaReason.CHANNEL_UNAVAILABLE)
            media.processed_at = datetime.now(UTC)

    @staticmethod
    def _record_opt_out(contact: Contact, *, text: str | None) -> int:
        """Opt the sender out of campaigns if the whole message is a stop word.

        The sender's own contact, as resolved from what the provider said - not
        a lookup by a phone column, which a username sender does not have.
        """
        if not is_stop_request(text) or contact.marketing_opt_out_at is not None:
            return 0
        contact.marketing_opt_out_at = datetime.now(UTC)
        contact.opt_out_source = OptOutSource.CUSTOMER
        logger.info("campaign.opt_out_requested", extra={"contact_id": str(contact.id)})
        return 1

    # ----------------------------------------------------------- settlement

    def _settle(
        self,
        owed: list[_Handoff],
        *,
        conversations: set[tuple[uuid.UUID, uuid.UUID]],
        media: set[tuple[uuid.UUID, uuid.UUID]],
    ) -> None:
        """Advance each stored event to what actually became of it (MSG-02)."""
        for handoff in owed:
            repository = ChannelEventRepository(self._session, tenant_id=handoff.event.tenant_id)
            pending = handoff.agent_for_conversation
            if pending is not None and pending not in conversations:
                repository.mark_unprocessed(handoff.event, reason=AGENT_NOT_QUEUED)
                continue
            pending = handoff.media_for_message
            if pending is not None and pending not in media:
                repository.mark_unprocessed(handoff.event, reason=MEDIA_NOT_QUEUED)
                continue
            repository.mark_processed(handoff.event)

    async def _enqueue(
        self,
        conversations: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]],
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        """One agent job per conversation, keyed on its first new message (WQ-01).

        A queue failure is logged and swallowed - a non-2xx would make the
        provider retry the whole delivery - and the event is left owing work.
        """
        if self._queue is None or not conversations:
            return set()
        triggers: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}
        for tenant_id, conversation_id, message_id in conversations:
            triggers.setdefault((tenant_id, conversation_id), message_id)

        queued: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for (tenant_id, conversation_id), message_id in triggers.items():
            try:
                await self._queue.enqueue(
                    AgentJob(
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        trigger_message_id=message_id,
                    )
                )
            except RedisError:
                logger.warning(
                    "agent.enqueue_failed", extra={"conversation_id": str(conversation_id)}
                )
                continue
            queued.add((tenant_id, conversation_id))
        return queued

    async def _enqueue_media(
        self,
        attachments: list[tuple[uuid.UUID, uuid.UUID]],
    ) -> set[tuple[uuid.UUID, uuid.UUID]]:
        """One media job per file - every file of every message (OMNI-009).

        A message counts as handed off only when every one of its files reached
        the queue; one refused file leaves the event owing, and the sweeper
        re-enqueues whatever is still pending.
        """
        if self._media_queue is None or not attachments:
            return set()
        queued: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for tenant_id, message_id in attachments:
            files = await MediaRepository(self._session, tenant_id=tenant_id).list_for_message(
                message_id
            )
            complete = bool(files)
            for media in files:
                try:
                    await self._media_queue.enqueue(
                        MediaJob(tenant_id=tenant_id, media_id=media.id)
                    )
                except RedisError:
                    logger.warning("media.enqueue_failed", extra={"media_id": str(media.id)})
                    complete = False
            if complete:
                queued.add((tenant_id, message_id))
        return queued

    # ------------------------------------------------------------ resolution

    async def _owner_at(self, connection_key: str, instant: datetime) -> ConnectionResolution:
        """The workspace that held this connection at `instant` (ADR-101)."""
        live = await self._live_connection(connection_key)
        if live is not None and live.held_at(instant):
            return ConnectionResolution.held_by(live)
        return await self._directory.owner_at(
            self._adapter.channel, connection_key, instant, live=live
        )

    async def _live_connection(self, connection_key: str) -> ChannelConnection | None:
        """The current claim, resolved once per delivery."""
        if connection_key not in self._live:
            self._live[connection_key] = await self._directory.live(
                self._adapter.channel, connection_key
            )
        return self._live[connection_key]

    async def _status_owner(
        self, event: InboundEvent, message_id: str, instant: datetime
    ) -> tuple[ConnectionResolution, Message | None]:
        """A status names a message, so it is resolved by that message first (MSG-04).

        The candidates are the claims this connection has carried, so an id the
        provider invented cannot reach anything; ownership at the status's
        instant is the fallback, and only decides where the event is filed.
        """
        if event.connection_key not in self._holdings:
            self._holdings[event.connection_key] = await self._directory.holders_of(
                self._adapter.channel, event.connection_key
            )
        holders = self._holdings[event.connection_key]
        message = await self._outbound.find_by_provider_message_id(
            message_id, connection_ids=[holder.id for holder in holders]
        )
        if message is not None:
            owner = next((holder for holder in holders if holder.id == message.connection_id), None)
            if owner is not None:
                return ConnectionResolution.held_by(owner), message
        return await self._owner_at(event.connection_key, instant), None

    async def _scope(
        self, connection: ChannelConnection, identifier: Identifier
    ) -> IdentityScopeRef:
        key = (connection.id, identifier.kind.value)
        if key not in self._scopes:
            self._scopes[key] = await self._adapter.identity_scope(
                self._session, connection, identifier
            )
        return self._scopes[key]


def _fold(totals: _Step, step: _Step) -> None:
    """Add one kept event's results to the delivery's."""
    totals.stored += step.stored
    totals.duplicates += step.duplicates
    totals.unknown += step.unknown
    totals.inactive += step.inactive
    totals.unowned += step.unowned
    totals.cancelled += step.cancelled
    totals.opted_out += step.opted_out
    totals.echoes += step.echoes
    totals.collisions += step.collisions
    totals.paired += step.paired
    totals.conflicts += step.conflicts
    totals.attachments.extend(step.attachments)
    totals.answering.extend(step.answering)
    totals.owed.extend(step.owed)


__all__ = [
    "AGENT_NOT_QUEUED",
    "MEDIA_NOT_QUEUED",
    "PROVIDER_ID_COLLISION",
    "ChannelIngestionService",
    "IngestionOutcome",
]
