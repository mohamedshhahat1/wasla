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
from app.core.media_types import SNIFF_BYTES, MediaClass
from app.core.media_types import resolve as resolve_media_type
from app.core.storage import EXTENSIONS, MediaStorage, StorageError, build_key
from app.db.models.billing import LimitKey
from app.db.models.conversation import Conversation, Message, MessageKind, MessageStatus
from app.db.models.media import MediaStatus, MediaStorageState
from app.db.models.usage import UsageEventType
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import released
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
from app.services.credential_service import CredentialService
from app.services.entitlement_service import EntitlementService
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
#
# Keyed on the class the *detector* assigned from the file's own bytes, never
# on the family in a string somebody sent. That is the whole of SEC-09: the
# previous version read `mime_type.split("/")[0]`, which made `image/svg+xml`
# an image and made any invented `image/x-whatever` one too.
MEDIA_FAMILIES: Final[dict[MediaClass, str]] = {
    MediaClass.IMAGE: "image",
    MediaClass.AUDIO: "audio",
    MediaClass.VIDEO: "video",
    MediaClass.DOCUMENT: "document",
}

MEDIA_KINDS: Final[dict[str, MessageKind]] = {
    "image": MessageKind.IMAGE,
    "document": MessageKind.DOCUMENT,
    "audio": MessageKind.AUDIO,
    "video": MessageKind.VIDEO,
}

# Meta requires a filename on a document, and one supplied by a caller is not
# safe to pass through untouched. Replaced rather than sanitised: a name is a
# convenience for the recipient, and a generated one that is definitely inert
# beats a cleaned-up one that might not be.
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}$")


def _whatsapp_kind(kind: MediaClass) -> str:
    """Which of Meta's four attachment kinds a detected class is sent as.

    Total over `MediaClass`, because the refusal now happens earlier and in one
    place: `media_types.resolve` has already refused anything that is not a
    supported format, so by the time a class exists there is a kind for it.

    Wasla's accepted set stays narrower than Meta's, and for the same reason as
    before - a business forwarding an executable to a customer is not a feature
    anyone asked for - but the narrowing is now done by what the bytes are
    rather than by a list of strings a caller could sidestep. Meta's own support
    is narrower again in places, and a file it will not carry comes back as a
    recorded rejection on the message rather than as a guess made here.
    """
    return MEDIA_FAMILIES[kind]


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
        self._credentials = CredentialService(settings)
        self._tenant_id = tenant_id
        self._entitlements = EntitlementService(
            session,
            tenant_id=tenant_id,
            default_plan_code=settings.default_plan_code,
        )

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
        # Nullable, because "the caller said nothing" and "the caller said
        # `application/octet-stream`" are the same statement and the type
        # resolver treats them alike. A route inventing a placeholder to satisfy
        # a signature would be manufacturing a claim nobody made.
        mime_type: str | None,
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

        `mime_type` is what the caller *said*. It is a hint from here on: the
        file's own bytes decide what this is, and a claim that contradicts them
        is refused rather than corrected (SEC-09). Everything downstream - what
        Meta is told, what is stored, what is served back - uses the canonical
        type that came out of that check and never the caller's string.
        """
        detected = resolve_media_type(claimed=mime_type, prefix=content[:SNIFF_BYTES])
        canonical = detected.mime_type
        family = _whatsapp_kind(detected.kind)
        kind = MEDIA_KINDS[family]

        # Refused here, before Meta is asked to do anything.
        #
        # The copy is kept after the send, deliberately - a file recorded for a
        # send that never happened is a file nobody sent. But that ordering
        # makes *this* the only honest place to refuse: discovering the
        # workspace is out of room after the customer already has the
        # attachment leaves a choice between an unrecorded send and an
        # over-quota write, and neither is a thing to do to somebody.
        #
        # `require` rather than `reserve`: nothing is written in this
        # transaction, so there is no claim to hold a lock over. The
        # reservation happens where the intent is committed, in
        # `_record_attachment` below, and this is the early refusal that keeps
        # a doomed upload from reaching the provider at all. A workspace that
        # fills its last megabyte between the two gets an unrecorded copy of a
        # message that was sent, which is the failure the store outage already
        # produces and which reconciliation already understands.
        if storage is not None:
            await self._entitlements.require(LimitKey.STORAGE_BYTES, additional=len(content))

        upload_name = _safe_filename(filename, mime_type=canonical)

        async def send(client: WhatsAppClient, phone_number_id: str, to: str) -> SentMessage:
            media_id = await client.upload_media(
                phone_number_id=phone_number_id,
                content=content,
                # The canonical type, not the caller's. Meta renders an
                # attachment by what it is told it is, so sending the claim
                # would let a mislabelled file be mislabelled to the customer
                # as well.
                mime_type=canonical,
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
            mime_type=canonical,
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

        **The transaction commits here, mid-request**, which is the same
        protocol the inbound path follows (ADR-087): the object's key and
        contents are recorded before the object can exist, so a request that
        dies during the write leaves something that names it. The commit lands
        *after* the send, so it cannot produce a second one - what it does
        produce is a durable record of the send that already happened, which
        today is lost along with everything else if the request fails from
        here on.

        The order of the two external effects is unchanged. Meta first, store
        second, and only for a send that succeeded.
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
            await self._session.flush()
            return

        # TX1: which object, and what will be in it - and, under the same lock
        # that commits with it, this send's claim on the workspace's storage.
        # `send_media` refused an over-quota upload before Meta was asked; this
        # is the claim itself, taken where the row that occupies the space is
        # written.
        capacity = await self._entitlements.reserve(LimitKey.STORAGE_BYTES, additional=len(content))
        if not capacity.allowed:
            # The message is sent and recorded either way. Only the copy is
            # lost, which is the same outcome a store outage produces and which
            # the paragraph above already accepts.
            logger.warning(
                "media.outbound_over_capacity",
                extra={
                    "event": "media.outbound_over_capacity",
                    "tenant_id": str(self._tenant_id),
                },
            )
            await self._session.flush()
            return

        key = build_key(tenant_id=row.tenant_id, mime_type=mime_type)
        row.storage_key = key
        row.storage_state = MediaStorageState.PENDING
        row.upload_started_at = datetime.now(UTC)

        async with released(self._session):
            written = await self._store(storage, key=key, content=content, mime_type=mime_type)

        if not written:
            logger.warning(
                "media.outbound_not_stored",
                extra={"conversation_id": str(message.conversation_id)},
            )
            # The intent stands. Reconciliation asks the store whether the
            # object arrived anyway - a write can fail on the way back - and
            # settles the row either way. There are no bytes left to retry
            # with: they arrived in a request body that is gone.
            return

        # TX2. The flush matters as much as the assignment: the request's
        # commit boundary only commits a session that is in a transaction, and
        # after `released` above this one is not until something touches it.
        row.storage_state = MediaStorageState.STORED
        await self._session.flush()

    @staticmethod
    async def _store(
        storage: MediaStorage,
        *,
        key: str,
        content: bytes,
        mime_type: str,
    ) -> bool:
        """Write the object, reporting refusal rather than raising.

        A `bool` because the caller is inside `released`, where touching the
        session is forbidden - and the row that records what happened is on the
        other side of that block.
        """
        try:
            await storage.put_at(key=key, data=content, mime_type=mime_type)
        except StorageError:
            return False
        return True

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

        async with self._client(account) as client:
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
    async def _client(self, account: WhatsAppAccount) -> AsyncIterator[WhatsAppClient]:
        """A WhatsApp client for one send, over a shared pool if there is one.

        The token belongs to the account rather than to the process: a
        workspace that supplied its own sends as itself, and one that did not
        sends through the platform credential (ADR-034). Resolved per send
        rather than held on the service, so the plaintext lives no longer than
        the call that needs it.
        """
        token = self._credentials.resolve(account).token
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
