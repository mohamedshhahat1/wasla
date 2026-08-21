"""Outbound messaging.

What this module is careful about:

- The message row is written before Meta is called, so an attempt always leaves
  a trace.
- A rejected send is recorded rather than raised. Raising would roll the request
  back and delete the row that proves the attempt happened.
- The 24-hour service window is enforced on free text only. Writing outside it
  is exactly what approved templates are for.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ExternalServiceError, RateLimitedError, ValidationError
from app.core.logging import get_logger
from app.db.models.conversation import Conversation, Message, MessageKind
from app.integrations.whatsapp.client import (
    SentMessage,
    WhatsAppClient,
    build_http_client,
)
from app.repositories.conversation_repository import (
    ContactRepository,
    ConversationRepository,
    MessageRepository,
)
from app.repositories.whatsapp_repository import WhatsAppAccountRepository

logger = get_logger(__name__)

# Meta's rule: a business may send free-form messages for 24 hours after the
# customer's last message. Outside it, only approved templates are accepted.
SERVICE_WINDOW: Final = timedelta(hours=24)

SendCall = Callable[[WhatsAppClient, str, str], Awaitable[SentMessage]]


class MessagingService:
    """Sends messages on behalf of one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        tenant_id: uuid.UUID,
    ) -> None:
        self._session = session
        self._settings = settings
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._contacts = ContactRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
        self._accounts = WhatsAppAccountRepository(session, tenant_id=tenant_id)

    async def send_text(
        self,
        *,
        conversation_id: uuid.UUID,
        body: str,
        preview_url: bool = False,
        sent_by_id: uuid.UUID | None = None,
    ) -> Message:
        async def send(client: WhatsAppClient, phone_number_id: str, to: str) -> SentMessage:
            return await client.send_text(
                phone_number_id=phone_number_id,
                to=to,
                body=body,
                preview_url=preview_url,
            )

        return await self._dispatch(
            conversation_id=conversation_id,
            kind=MessageKind.TEXT,
            body=body,
            sent_by_id=sent_by_id,
            send=send,
            require_window=True,
        )

    async def send_template(
        self,
        *,
        conversation_id: uuid.UUID,
        name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
        sent_by_id: uuid.UUID | None = None,
    ) -> Message:
        async def send(client: WhatsAppClient, phone_number_id: str, to: str) -> SentMessage:
            return await client.send_template(
                phone_number_id=phone_number_id,
                to=to,
                name=name,
                language=language,
                components=components,
            )

        return await self._dispatch(
            conversation_id=conversation_id,
            kind=MessageKind.TEXT,
            body=f"[template:{name}]",
            sent_by_id=sent_by_id,
            send=send,
            # Templates are the sanctioned way out of the service window.
            require_window=False,
        )

    async def _dispatch(
        self,
        *,
        conversation_id: uuid.UUID,
        kind: MessageKind,
        body: str | None,
        sent_by_id: uuid.UUID | None,
        send: SendCall,
        require_window: bool,
    ) -> Message:
        conversation = await self._conversations.require_by_id(conversation_id)
        if require_window and not self.window_open(conversation):
            raise ValidationError(
                "This conversation is outside the 24-hour service window. "
                "Send an approved template instead."
            )

        account = await self._accounts.require_by_id(conversation.account_id)
        if not account.is_active:
            raise ValidationError("This WhatsApp number is disabled.")
        contact = await self._contacts.require_by_id(conversation.contact_id)

        message = await self._messages.stage_outbound(
            conversation_id=conversation_id,
            kind=kind,
            body=body,
            sent_by_id=sent_by_id,
        )
        # Flushed before the network call so the attempt exists as a row even if
        # everything after this fails.
        await self._session.flush()

        async with build_http_client() as http:
            client = WhatsAppClient(
                http=http,
                access_token=self._settings.meta_access_token or "",
                api_version=self._settings.meta_api_version,
            )
            try:
                sent = await send(client, account.phone_number_id, contact.wa_id)
            except (ExternalServiceError, RateLimitedError) as error:
                # Recorded, not raised: the caller gets the message back in
                # failed state, and the row survives the commit.
                await self._messages.mark_failed(message, reason=str(error))
                logger.warning(
                    "whatsapp.outbound_failed",
                    extra={"conversation_id": str(conversation_id)},
                )
                return message

        now = datetime.now(UTC)
        await self._messages.mark_sent(
            message,
            wa_message_id=sent.message_id,
            sent_at=now,
        )
        conversation.last_message_at = now
        return message

    def window_open(self, conversation: Conversation) -> bool:
        """Whether free-form messages are still allowed.

        A conversation the customer has never written in has no open window: the
        business may only open it with a template.
        """
        if conversation.last_inbound_at is None:
            return False
        return datetime.now(UTC) - conversation.last_inbound_at <= SERVICE_WINDOW
