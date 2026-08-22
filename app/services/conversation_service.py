"""Projection of WhatsApp events into the conversation aggregate.

Deliberately separate from `WhatsAppIngestionService`. Storing an event must not
fail because a projection rule is wrong, and a projection bug must be fixable by
replaying the stored log rather than by asking Meta to resend traffic it has
already delivered.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.conversation import Message, MessageKind, MessageStatus
from app.db.models.usage import UsageEventType
from app.integrations.whatsapp.payload import DeliveryStatus, InboundMessage
from app.repositories.conversation_repository import (
    ContactRepository,
    ConversationRepository,
    MessageRepository,
)
from app.repositories.media_repository import MediaRepository
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

# Meta's message types mapped onto the kinds Wasla stores. A type that is absent
# becomes UNSUPPORTED rather than an error: the raw event is already stored, so a
# message type Meta ships tomorrow can be replayed once it is understood.
MESSAGE_KINDS: dict[str, MessageKind] = {
    "text": MessageKind.TEXT,
    "image": MessageKind.IMAGE,
    "document": MessageKind.DOCUMENT,
    "audio": MessageKind.AUDIO,
    "voice": MessageKind.AUDIO,
    "video": MessageKind.VIDEO,
    "location": MessageKind.LOCATION,
    "interactive": MessageKind.INTERACTIVE,
    "button": MessageKind.INTERACTIVE,
    # A sticker is a small image and is read as one. Meta gives it its own type
    # rather than folding it into "image", but nothing downstream needs the
    # distinction, and the raw event keeps it for anything that later does.
    "sticker": MessageKind.IMAGE,
}

# Only the four statuses Meta actually reports for a sent message.
DELIVERY_STATUSES: dict[str, MessageStatus] = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}


class ConversationProjectionService:
    """Turns one stored event into contact, conversation and message rows.

    Scoped to a single workspace for its lifetime, like the repositories it owns.
    """

    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self._session = session
        self._contacts = ContactRepository(session, tenant_id=tenant_id)
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
        self._media = MediaRepository(session, tenant_id=tenant_id)
        # Constructed here rather than injected. Metering is not an optional
        # collaborator: a caller able to leave it out is a path that silently
        # stops counting, and this service is the only writer of the two
        # meters below.
        self._usage = UsageRecorder(session, tenant_id=tenant_id)

    async def project_message(
        self,
        *,
        account_id: uuid.UUID,
        message: InboundMessage,
    ) -> Message:
        """Record a customer message against its conversation."""
        occurred_at = message.timestamp or datetime.now(UTC)

        contact = await self._contacts.upsert(
            wa_id=message.from_number,
            display_name=message.profile_name,
            last_seen_at=occurred_at,
        )
        # Flushed because primary keys are generated in Python at insert time:
        # `contact.id` stays None until the row reaches the database, and the
        # conversation needs it.
        await self._session.flush()

        conversation, created = await self._conversations.get_or_create(
            contact_id=contact.id,
            account_id=account_id,
        )
        if created:
            await self._session.flush()
            self._usage.record(
                UsageEventType.CONVERSATION_CREATED,
                occurred_at=occurred_at,
                meta={"conversation_id": str(conversation.id)},
            )

        await self._conversations.touch_inbound(conversation, at=occurred_at)

        stored, is_new = await self._messages.record_inbound(
            conversation_id=conversation.id,
            wa_message_id=message.event_id,
            kind=MESSAGE_KINDS.get(message.message_type, MessageKind.UNSUPPORTED),
            # The caption, on a media message. What the file turns out to say is
            # recorded separately and never merged into the customer's words.
            body=message.text,
            sent_at=occurred_at,
        )
        if is_new:
            # Metered on the message rather than on the webhook delivery. A
            # replayed event does not reach here - the event id already
            # deduplicated it - and one delivery can carry several messages,
            # so counting deliveries would be counting the wrong thing.
            self._usage.record(
                UsageEventType.WHATSAPP_MESSAGE_RECEIVED,
                occurred_at=occurred_at,
                meta={"conversation_id": str(conversation.id)},
            )

        if is_new and message.media is not None:
            # Flushed for the same reason the contact was: `stored.id` is
            # generated in Python and stays None until the row reaches the
            # database, and the media row needs it.
            await self._session.flush()
            await self._media.record(
                message_id=stored.id,
                conversation_id=conversation.id,
                wa_media_id=message.media.media_id,
                mime_type=message.media.mime_type,
                filename=message.media.filename,
                is_voice=message.media.is_voice,
            )
        return stored

    async def project_status(self, *, status: DeliveryStatus) -> Message | None:
        """Advance the delivery state of a message Wasla sent."""
        mapped = DELIVERY_STATUSES.get(status.status)
        if mapped is None:
            logger.info(
                "whatsapp.unmapped_delivery_status",
                extra={"delivery_status": status.status},
            )
            return None

        message = await self._messages.apply_status(
            wa_message_id=status.message_id,
            status=mapped,
            at=status.timestamp or datetime.now(UTC),
        )
        if message is None:
            # Expected, not an error: a template sent from Meta's own console,
            # or traffic that predates this number being connected to Wasla.
            logger.info(
                "whatsapp.status_for_unknown_message",
                extra={"wa_message_id": status.message_id},
            )
        return message
