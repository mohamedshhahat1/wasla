"""Projection of inbound events into the conversation aggregate, for every channel.

Deliberately separate from ingestion. Storing an event must not fail because a
projection rule is wrong, and a projection bug must be fixable by replaying the
stored log rather than by asking a provider to resend traffic it has already
delivered.

This module reads only neutral events (`app.channels.inbound`); Meta's type and
status maps moved to the WhatsApp adapter (OMNI-006).

**A message is projected only if it is this customer's new message** (OMNI-005).
`screen` asks that before anything is resolved or created: the provider id is
looked up on this connection, direction-aware. The same customer message seen
again is a duplicate; an id that names a message Wasla sent - or any message on
another connection of the workspace while the older workspace-wide key stands -
is a collision. Neither opens a conversation, moves the service window, opens
anything for an agent, cancels a follow-up or opts anybody out. At the audit's
HEAD an event carrying the id of Wasla's own reply opened an empty conversation
with its window marked open and queued an agent turn on another conversation,
triggered by that reply (probe Y1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.inbound import InboundEvent
from app.channels.metering import message_meters
from app.core.logging import get_logger
from app.db.models.channel import ChannelConnection
from app.db.models.conversation import (
    Conversation,
    Message,
    MessageDirection,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.media import MediaLocatorKind
from app.db.models.usage import UsageEventType
from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
)
from app.repositories.media_repository import MediaRepository
from app.services.contact_identity_service import SenderResolution
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)


#: How far Wasla's send clock may run ahead of a provider's for a message the
#: provider never timestamped (OMNI-042); `WATERMARK_CLOCK_TOLERANCE_SECONDS`.
DEFAULT_WATERMARK_TOLERANCE = timedelta(seconds=5)


class ProjectionOutcome(StrEnum):
    """What projecting one inbound message came to. Bounded: it is a metric label."""

    #: A new customer message, stored.
    STORED = "stored"
    #: This customer's message on this connection, seen before.
    DUPLICATE = "duplicate"
    #: The id names something that is not this customer's inbound message on
    #: this connection - Wasla's own send, or another connection's message.
    COLLISION = "collision"


@dataclass(frozen=True, slots=True)
class ProjectedMessage:
    outcome: ProjectionOutcome
    message: Message | None = None
    conversation: Conversation | None = None

    @property
    def is_new(self) -> bool:
        return self.outcome is ProjectionOutcome.STORED


class EchoKind(StrEnum):
    """What an echo turned out to be (OMNI-037)."""

    OWN = "own"
    EXTERNAL = "external"
    ORPHAN = "orphan"


@dataclass(frozen=True, slots=True)
class EchoOutcome:
    kind: EchoKind
    message: Message | None = None
    conversation: Conversation | None = None


class ConversationProjectionService:
    """Turns one stored event into conversation and message rows, in one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        watermark_tolerance: timedelta = DEFAULT_WATERMARK_TOLERANCE,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._watermark_tolerance = watermark_tolerance
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
        self._media = MediaRepository(session, tenant_id=tenant_id)
        # Constructed here rather than injected. Metering is not an optional
        # collaborator: a caller able to leave it out is a path that silently
        # stops counting, and this service is the only writer of the
        # received-message and conversation meters.
        self._usage = UsageRecorder(session, tenant_id=tenant_id)

    async def screen(
        self, *, connection: ChannelConnection, event: InboundEvent
    ) -> ProjectedMessage | None:
        """Why this message must not be projected, or None if it is new.

        Asked first, before a sender is resolved or a conversation opened, so a
        refused message leaves no state behind at all.
        """
        if event.message_id is None:  # pragma: no cover - an adapter refuses these
            return ProjectedMessage(ProjectionOutcome.COLLISION)
        existing = await self._messages.find_provider_message(
            connection_id=connection.id, provider_message_id=event.message_id
        )
        if existing is not None:
            if (
                existing.direction is MessageDirection.INBOUND
                and existing.origin is MessageOrigin.CUSTOMER
            ):
                return ProjectedMessage(ProjectionOutcome.DUPLICATE, message=existing)
            return self._collision(connection, existing)
        # The workspace-wide key stands until the compatibility cleanup
        # (ADR-120). An id it would refuse names another connection's message:
        # never this customer's, so never theirs to reuse.
        elsewhere = await self._messages.get_by_wa_message_id(event.message_id)
        if elsewhere is not None:
            return self._collision(connection, elsewhere)
        return None

    async def project_message(
        self,
        *,
        connection: ChannelConnection,
        event: InboundEvent,
        sender: SenderResolution,
    ) -> ProjectedMessage:
        """Record a new customer message against its conversation, opening one if needed."""
        screened = await self.screen(connection=connection, event=event)
        if screened is not None:
            return screened
        if event.message_id is None:  # pragma: no cover - `screen` refuses it first
            return ProjectedMessage(ProjectionOutcome.COLLISION)

        occurred_at = event.occurred_at or datetime.now(UTC)
        conversation, created = await self._conversations.get_or_create(
            contact_id=sender.contact.id,
            account_id=connection.id,
            channel=connection.channel,
            participant_identity_id=sender.participant.id,
        )
        if created:
            # Primary keys are generated at insert: the conversation's id is
            # only known once the row reaches the database.
            await self._session.flush()
            self._usage.record(
                UsageEventType.CONVERSATION_CREATED,
                occurred_at=occurred_at,
                meta={"conversation_id": str(conversation.id)},
            )

        stored = self._messages.record_inbound(
            conversation_id=conversation.id,
            connection_id=connection.id,
            provider_message_id=event.message_id,
            kind=event.message_kind,
            # The caption, on a media message. What the file turns out to say is
            # recorded separately and never merged into the customer's words.
            body=event.text,
            sent_at=occurred_at,
            action=event.action,
        )
        # Only now, for a message that is new and the customer's: this is what
        # reopens a closed conversation and opens the reply window.
        await self._conversations.touch_inbound(conversation, at=occurred_at)

        meters = message_meters(connection.channel)
        if meters is not None:
            # Metered on the message rather than the delivery: a replay never
            # gets here, and one delivery can carry several messages.
            self._usage.record(
                meters.received,
                occurred_at=occurred_at,
                meta={"conversation_id": str(conversation.id)},
            )

        if event.attachments:
            await self._session.flush()
            for position, attachment in enumerate(event.attachments):
                await self._media.record(
                    message_id=stored.id,
                    conversation_id=conversation.id,
                    position=position,
                    # The WhatsApp handle keeps its compatibility column; the
                    # locator is what fetching reads, whatever the channel.
                    wa_media_id=(
                        attachment.locator
                        if attachment.locator_kind is MediaLocatorKind.HANDLE
                        else None
                    ),
                    locator_kind=attachment.locator_kind,
                    locator=attachment.locator,
                    locator_expires_at=attachment.expires_at,
                    mime_type=attachment.mime_type,
                    filename=attachment.filename,
                    is_voice=attachment.is_voice,
                )
        return ProjectedMessage(ProjectionOutcome.STORED, message=stored, conversation=conversation)

    async def project_echo(
        self,
        *,
        connection: ChannelConnection,
        event: InboundEvent,
        conversation: Conversation | None,
    ) -> EchoOutcome:
        """What an echo is: Wasla's own send, or a reply typed outside Wasla (OMNI-037, ADR-129).

        - **Wasla's own** - its provider id names an outbound message on this
          connection, or a send of the same words is still in flight there:
          confirmed, and nothing changes.
        - **External** - a person answered from the provider's own app: the
          reply is projected as an outbound message with origin `external`, the
          inbox order moves, and the caller hands the conversation to a person.
          The window never moves: only the customer opens it.
        - **Orphan** - it names no conversation Wasla holds: kept as evidence.
        """
        if event.message_id is None:  # pragma: no cover - the event type refuses it
            return EchoOutcome(EchoKind.ORPHAN)
        existing = await self._messages.find_provider_message(
            connection_id=connection.id, provider_message_id=event.message_id
        )
        if existing is not None:
            if existing.direction is MessageDirection.OUTBOUND:
                # Wasla's own send: the echo carries the provider's own time
                # for it, which a read watermark is compared with (OMNI-042).
                if existing.provider_sent_at is None and event.occurred_at is not None:
                    existing.provider_sent_at = event.occurred_at
                return EchoOutcome(EchoKind.OWN, message=existing)
            return EchoOutcome(EchoKind.ORPHAN)
        if conversation is None:
            return EchoOutcome(EchoKind.ORPHAN)
        if await self._messages.requested_with_body(conversation.id, event.text):
            # Wasla's send, echoed before its response named it.
            return EchoOutcome(EchoKind.OWN)
        at = event.occurred_at or datetime.now(UTC)
        message = self._messages.record_external(
            conversation_id=conversation.id,
            connection_id=connection.id,
            provider_message_id=event.message_id,
            kind=event.message_kind,
            body=event.text,
            sent_at=at,
        )
        await self._conversations.touch_outbound(conversation, at=at)
        return EchoOutcome(EchoKind.EXTERNAL, message=message, conversation=conversation)

    async def project_status(
        self,
        *,
        event: InboundEvent,
        connection: ChannelConnection,
        message: Message | None = None,
    ) -> Message | None:
        """Advance the delivery state of a message Wasla sent - per message, or by watermark.

        `message` is the row ingestion already resolved from the provider id,
        across every claim the connection has carried; that resolution is what
        survives a connection changing hands (MSG-04). A status naming no
        message we hold is ordinary traffic and creates nothing.
        """
        update = event.status
        if update is None:
            logger.info(
                "channel.unmapped_delivery_status",
                extra={"event": "channel.unmapped_delivery_status", "channel": event.channel.value},
            )
            return None

        at = event.occurred_at or datetime.now(UTC)
        if message is None and update.message_id is not None:
            found = await self._messages.find_provider_message(
                connection_id=connection.id, provider_message_id=update.message_id
            )
            if found is not None and found.direction is MessageDirection.OUTBOUND:
                message = found
        if message is not None:
            if (
                update.status is MessageStatus.SENT
                and message.provider_sent_at is None
                and event.occurred_at is not None
            ):
                # The provider's own time for the send (OMNI-042).
                message.provider_sent_at = event.occurred_at
            return self._messages.advance_status(message, status=update.status, at=at)
        if update.message_id is not None:
            logger.info(
                "channel.status_for_unknown_message",
                extra={"event": "channel.status_for_unknown_message"},
            )
        return None

    async def project_watermark(
        self,
        *,
        event: InboundEvent,
        connection: ChannelConnection,
        conversation: Conversation,
    ) -> int:
        """Advance a conversation's outbound messages up to a provider watermark (OMNI-011)."""
        update = event.status
        if update is None or update.watermark is None:
            return 0
        return await self._messages.advance_to_watermark(
            conversation_id=conversation.id,
            connection_id=connection.id,
            status=update.status,
            watermark=update.watermark,
            tolerance=self._watermark_tolerance,
        )

    def _collision(self, connection: ChannelConnection, existing: Message) -> ProjectedMessage:
        logger.warning(
            "channel.provider_id_collision",
            extra={
                "event": "channel.provider_id_collision",
                "channel": connection.channel.value,
                "direction": existing.direction.value,
                "same_connection": existing.connection_id == connection.id,
            },
        )
        return ProjectedMessage(ProjectionOutcome.COLLISION, message=existing)


__all__ = [
    "ConversationProjectionService",
    "EchoKind",
    "EchoOutcome",
    "ProjectedMessage",
    "ProjectionOutcome",
]
