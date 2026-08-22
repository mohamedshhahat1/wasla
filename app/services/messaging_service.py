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

import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ExternalServiceError, RateLimitedError, ValidationError
from app.core.logging import get_logger
from app.core.storage import EXTENSIONS, MediaStorage, StorageError
from app.db.models.conversation import Conversation, Message, MessageKind, MessageStatus
from app.db.models.media import MediaStatus
from app.db.models.usage import UsageEventType
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
from app.repositories.media_repository import MediaRepository
from app.repositories.whatsapp_repository import WhatsAppAccountRepository
from app.services.media_service import content_hash as media_content_hash
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

# Meta's rule: a business may send free-form messages for 24 hours after the
# customer's last message. Outside it, only approved templates are accepted.
SERVICE_WINDOW: Final = timedelta(hours=24)

SendCall = Callable[[WhatsAppClient, str, str], Awaitable[SentMessage]]

# Meta groups attachments into four kinds, and they are not the mime families.
# "image/png" is an image, but "application/pdf" is a *document* - so the
# mapping translates rather than splitting on the slash, which is the mistake
# this table exists to prevent.
MEDIA_FAMILIES: Final[dict[str, str]] = {
    "image": "image",
    "audio": "audio",
    "video": "video",
}
# Everything Meta will carry that is not one of the three above.
DOCUMENT_KIND: Final = "document"

MEDIA_KINDS: Final[dict[str, MessageKind]] = {
    "image": MessageKind.IMAGE,
    "document": MessageKind.DOCUMENT,
    "audio": MessageKind.AUDIO,
    "video": MessageKind.VIDEO,
}

# Refused rather than sent as a document. Meta will carry almost anything under
# "document", and a business forwarding an executable to a customer is not a
# feature; the list is what a business plausibly sends on purpose.
SENDABLE_DOCUMENTS: Final = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/csv",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)

# Meta requires a filename on a document, and one supplied by a caller is not
# safe to pass through untouched. Replaced rather than sanitised: a name is a
# convenience for the recipient, and a generated one that is definitely inert
# beats a cleaned-up one that might not be.
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$")


def _whatsapp_kind(mime_type: str) -> str | None:
    """Which of Meta's four attachment kinds this type is sent as.

    None means Wasla will not send it. That is a narrower rule than Meta's own -
    it would carry almost any file as a document - and deliberately so: a
    business forwarding an executable to a customer is not a feature anyone
    asked for.
    """
    normalised = mime_type.strip().lower()
    family = MEDIA_FAMILIES.get(normalised.split("/", 1)[0])
    if family is not None:
        return family
    if normalised in SENDABLE_DOCUMENTS:
        return DOCUMENT_KIND
    return None


def _safe_filename(filename: str | None, *, mime_type: str) -> str:
    """A filename Meta will accept and a filesystem cannot be hurt by.

    A name reaching here came from a request body. It is shown to the recipient
    and is never used to build a path on this side, but it does travel to a
    third party, so anything that is not plainly a filename is replaced.
    """
    if filename and SAFE_FILENAME.match(filename) and ".." not in filename:
        return filename
    return f"attachment{EXTENSIONS.get(mime_type.lower(), '')}"


class MessagingService:
    """Sends messages on behalf of one workspace."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        tenant_id: uuid.UUID,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        """`http` lets a caller sending many messages share one connection pool.

        Without it each send opens and closes its own client, which is right for
        a request handling one message and wrong for a campaign: ten thousand
        sends would mean ten thousand TLS handshakes to the same host. The
        caller that supplies one owns its lifetime.
        """
        self._session = session
        self._settings = settings
        self._http = http
        self._conversations = ConversationRepository(session, tenant_id=tenant_id)
        self._contacts = ContactRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
        self._accounts = WhatsAppAccountRepository(session, tenant_id=tenant_id)
        self._media = MediaRepository(session, tenant_id=tenant_id)
        self._usage = UsageRecorder(session, tenant_id=tenant_id)

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
            kind=MessageKind.TEMPLATE,
            # No body: Meta renders the approved template from its own copy, so
            # the text the customer saw is not ours to record. Claiming
            # otherwise would put a guess in the transcript.
            body=None,
            template_name=name,
            template_language=language,
            sent_by_id=sent_by_id,
            send=send,
            # Templates are the sanctioned way out of the service window.
            require_window=False,
        )

    async def send_media(
        self,
        *,
        conversation_id: uuid.UUID,
        content: bytes,
        mime_type: str,
        filename: str | None = None,
        caption: str | None = None,
        sent_by_id: uuid.UUID | None = None,
        storage: MediaStorage | None = None,
    ) -> Message:
        """Send a file, uploading it to Meta first.

        Uploaded rather than sent by link, deliberately. A link requires every
        attachment to sit behind a publicly reachable URL for as long as Meta
        might fetch it; uploading exposes the bytes to one recipient for one
        send. The upload returns an id that is valid for a single message.

        Free text rules apply: an attachment is a free-form message, so the
        24-hour window is enforced exactly as it is on text. Outside it, only an
        approved template will do.

        `storage` is optional and only used to keep a copy of what was sent.
        Without it the message is still sent and recorded; the record simply
        does not point at a stored file.
        """
        family = _whatsapp_kind(mime_type)
        if family is None:
            raise ValidationError(f"Files of type {mime_type} cannot be sent.")
        kind = MEDIA_KINDS[family]

        upload_name = _safe_filename(filename, mime_type=mime_type)

        async def send(client: WhatsAppClient, phone_number_id: str, to: str) -> SentMessage:
            media_id = await client.upload_media(
                phone_number_id=phone_number_id,
                content=content,
                mime_type=mime_type,
                filename=upload_name,
            )
            return await client.send_media(
                phone_number_id=phone_number_id,
                to=to,
                kind=family,  # type: ignore[arg-type]
                media_id=media_id,
                caption=caption,
                filename=upload_name,
            )

        message = await self._dispatch(
            conversation_id=conversation_id,
            kind=kind,
            # The caption is the text of this message, exactly as it is on an
            # inbound one: it is what the person typed.
            body=caption,
            sent_by_id=sent_by_id,
            send=send,
            require_window=True,
        )

        await self._record_attachment(
            message=message,
            content=content,
            mime_type=mime_type,
            filename=filename,
            storage=storage,
        )
        return message

    async def _record_attachment(
        self,
        *,
        message: Message,
        content: bytes,
        mime_type: str,
        filename: str | None,
        storage: MediaStorage | None,
    ) -> None:
        """Keep a record of what was sent, and the file itself if there is a store.

        Written after the send rather than before, unlike the message row. The
        message row exists early so a failed send still leaves evidence; this
        row describes a file that was actually transmitted, and storing bytes
        for a send that never happened would accumulate files nobody sent.

        A storage failure is swallowed. The customer has the file; losing our
        own copy of it is not worth failing a request that already succeeded.
        """
        if message.status is MessageStatus.FAILED:
            # Nothing was transmitted. Recording an attachment here would claim
            # a file reached the customer that never did, and storing its bytes
            # would accumulate copies of sends that did not happen.
            return

        row, _ = await self._media.record(
            message_id=message.id,
            conversation_id=message.conversation_id,
            wa_media_id=None,
            mime_type=mime_type,
            filename=filename,
            is_voice=False,
        )
        row.byte_size = len(content)
        row.content_hash = media_content_hash(content)
        row.status = MediaStatus.READY

        if storage is None:
            return

        try:
            row.storage_key = await storage.put(
                tenant_id=row.tenant_id,
                data=content,
                mime_type=mime_type,
            )
        except StorageError:
            logger.warning(
                "media.outbound_not_stored",
                extra={"conversation_id": str(message.conversation_id)},
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
        template_name: str | None = None,
        template_language: str | None = None,
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
            template_name=template_name,
            template_language=template_language,
        )
        # Flushed before the network call so the attempt exists as a row even if
        # everything after this fails.
        await self._session.flush()

        async with self._client() as client:
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
        # Metered here and not before the call: a send that Meta refused cost
        # the workspace nothing to deliver, and the failed row above already
        # records that the attempt happened. Everything that leaves this way is
        # counted once - an agent's reply, a person's, a follow-up, a campaign.
        self._usage.record(
            UsageEventType.WHATSAPP_MESSAGE_SENT,
            occurred_at=now,
            meta={"conversation_id": str(conversation_id), "kind": kind.value},
        )
        return message

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[WhatsAppClient]:
        """A WhatsApp client for one send, over a shared pool if there is one."""
        token = self._settings.meta_access_token or ""
        version = self._settings.meta_api_version

        if self._http is not None:
            yield WhatsAppClient(http=self._http, access_token=token, api_version=version)
            return

        async with build_http_client() as http:
            yield WhatsAppClient(http=http, access_token=token, api_version=version)

    def window_open(self, conversation: Conversation) -> bool:
        """Whether free-form messages are still allowed.

        A conversation the customer has never written in has no open window: the
        business may only open it with a template.
        """
        if conversation.last_inbound_at is None:
            return False
        return datetime.now(UTC) - conversation.last_inbound_at <= SERVICE_WINDOW
