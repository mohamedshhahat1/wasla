"""WhatsApp as a channel adapter - the only one there is (ADR-121).

Everything the shared core must not know about WhatsApp lives here or beside it
in this package:

- **the webhook**, parsed by `payload.parse_webhook` and normalised here into
  `InboundEvent`s - the Meta message-type and status maps used to sit in the
  shared projection (OMNI-006);
- **identity scope**: a phone number names one person across the workspace; a
  business-scoped user id is unique per business portfolio, and the WhatsApp
  Business Account a message arrived through belongs to exactly one portfolio,
  so that account is the scope (ADR-118);
- **addressing**: a conversation pinned to a phone is sent `to` it; one pinned
  to a business-scoped id is sent to its `recipient`. Never both - Meta lets
  `to` win, which would make the pin a suggestion (OMNI-004);
- **the credential**: the number's own token, or - WhatsApp only - the platform
  token when the number stores none. That fallback is WhatsApp's policy (a
  system-user token can send as the platform's numbers) and is decided here, so
  a connection on any other channel can never inherit it (OMNI-012);
- **files**, fetched in Meta's two steps from a handle.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import (
    ChannelMediaFetcher,
    ChannelSender,
    FetchedFile,
    FileProbe,
    IdentityNotAddressableError,
    IdentityScopeRef,
    MediaContent,
    OutboundContent,
    ProviderReceipt,
    Recipient,
    TemplateContent,
    TextContent,
)
from app.channels.inbound import (
    AttachmentLocator,
    Identifier,
    InboundEvent,
    InboundKind,
    ParsedDelivery,
    ReplyAction,
    StatusUpdate,
)
from app.channels.media import MalformedMediaDescriptorError, locator_expired
from app.channels.policy import ChannelPolicy
from app.core.config import Settings
from app.core.crypto import CredentialDecryptionError
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
)
from app.db.models.conversation import MessageKind, MessageStatus
from app.db.models.media import MediaLocatorKind
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.whatsapp.client import WhatsAppClient, build_http_client
from app.integrations.whatsapp.payload import DeliveryStatus, InboundMessage, parse_webhook
from app.integrations.whatsapp.policy import WhatsAppChannelPolicy
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.repositories.whatsapp_repository import WhatsAppAccountRepository
from app.services.credential_service import CredentialService, ResolvedCredential

logger = get_logger(__name__)

# Meta's message types mapped onto the kinds Wasla stores. A type that is absent
# becomes UNSUPPORTED rather than an error: the raw event is already stored, so a
# message type Meta ships tomorrow can be replayed once it is understood.
MESSAGE_KINDS: Final[dict[str, MessageKind]] = {
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
DELIVERY_STATUSES: Final[dict[str, MessageStatus]] = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}

# Meta's four attachment families for an outbound file.
_MEDIA_FAMILIES: Final = frozenset({"image", "document", "audio", "video"})

RecipientKind = Literal["phone", "bsuid"]


def _identifiers(message: InboundMessage) -> tuple[Identifier, ...]:
    """What Meta asserted about the sender, phone first.

    A phone first because it is what a conversation is pinned to when Meta
    names one - every conversation Wasla has ever had is addressed that way,
    and Meta itself lets `to` win over `recipient`. The parent business-scoped
    id is not an identity: it spans portfolios, and nothing is linked by it.
    """
    found: list[Identifier] = []
    if message.from_number is not None:
        found.append(Identifier(kind=IdentityKind.PHONE, value=message.from_number))
    if message.from_user_id is not None:
        found.append(Identifier(kind=IdentityKind.BSUID, value=message.from_user_id))
    return tuple(found)


def message_event(message: InboundMessage) -> InboundEvent:
    """One WhatsApp customer message as a neutral event."""
    attachments: tuple[AttachmentLocator, ...] = ()
    if message.media is not None:
        attachments = (
            AttachmentLocator(
                locator_kind=MediaLocatorKind.HANDLE,
                locator=message.media.media_id,
                media_kind=message.media.kind,
                mime_type=message.media.mime_type,
                filename=message.media.filename,
                is_voice=message.media.is_voice,
                sha256=message.media.sha256,
            ),
        )
    return InboundEvent(
        channel=Channel.WHATSAPP,
        connection_key=message.phone_number_id,
        kind=InboundKind.MESSAGE,
        event_id=message.event_id,
        occurred_at=message.timestamp,
        sender=_identifiers(message),
        message_id=message.event_id,
        message_kind=MESSAGE_KINDS.get(message.message_type, MessageKind.UNSUPPORTED),
        text=message.text,
        attachments=attachments,
        reply_to=message.context_id,
        action=message.action,
        profile_name=message.profile_name,
        raw=message.raw,
    )


def status_event(status: DeliveryStatus) -> InboundEvent:
    """One WhatsApp delivery status as a neutral event.

    A status Meta reports that Wasla has no word for is still an event - stored,
    settled, never projected - rather than a refusal: it was well formed.
    """
    mapped = DELIVERY_STATUSES.get(status.status)
    return InboundEvent(
        channel=Channel.WHATSAPP,
        connection_key=status.phone_number_id,
        kind=InboundKind.STATUS,
        event_id=status.event_id,
        occurred_at=status.timestamp,
        message_id=status.message_id,
        status=(
            StatusUpdate(status=mapped, provider_status=status.status, message_id=status.message_id)
            if mapped is not None
            else None
        ),
        raw=status.raw,
    )


@dataclass(frozen=True, slots=True)
class WhatsAppSender:
    """One send's session: the number's client and the number to send from."""

    client: WhatsAppClient
    phone_number_id: str
    uploaded: list[str]

    async def prepare(self, content: OutboundContent) -> None:
        """Upload a file to Meta. Creates a handle for one message; delivers nothing."""
        if not isinstance(content, MediaContent):
            return
        self.uploaded.append(
            await self.client.upload_media(
                phone_number_id=self.phone_number_id,
                content=content.content,
                mime_type=content.mime_type,
                filename=content.filename,
            )
        )

    async def send(self, recipient: Recipient, content: OutboundContent) -> ProviderReceipt:
        address = _address_arguments(recipient)
        if isinstance(content, TextContent):
            sent = await self.client.send_text(
                phone_number_id=self.phone_number_id,
                body=content.body,
                preview_url=content.preview_url,
                **address,
            )
        elif isinstance(content, TemplateContent):
            sent = await self.client.send_template(
                phone_number_id=self.phone_number_id,
                name=content.name,
                language=content.language,
                components=content.components,
                **address,
            )
        else:
            if content.family not in _MEDIA_FAMILIES or not self.uploaded:
                raise ValidationError("This file cannot be sent over WhatsApp.")
            sent = await self.client.send_media(
                phone_number_id=self.phone_number_id,
                kind=cast("Any", content.family),
                media_id=self.uploaded[0],
                caption=content.caption,
                filename=content.filename,
                **address,
            )
        return ProviderReceipt(message_id=sent.message_id, raw=sent.raw)


def _address_arguments(recipient: Recipient) -> dict[str, str]:
    """`to` for a phone, `recipient` for a business-scoped id - exactly one."""
    if recipient.kind == IdentityKind.PHONE.value:
        return {"to": recipient.value}
    if recipient.kind == IdentityKind.BSUID.value:
        return {"recipient_user_id": recipient.value}
    raise IdentityNotAddressableError()


@dataclass(frozen=True, slots=True)
class WhatsAppMediaFetcher:
    """Fetches files by the handle Meta put in the webhook, in Meta's two steps."""

    client: WhatsAppClient

    async def probe(self, locator: AttachmentLocator) -> FileProbe:
        handle = self._handle(locator)
        descriptor = await self.client.probe_media(handle)
        return FileProbe(mime_type=descriptor.mime_type, byte_size=descriptor.byte_size)

    async def fetch(self, locator: AttachmentLocator, *, max_bytes: int) -> FetchedFile:
        downloaded = await self.client.fetch_media(self._handle(locator), max_bytes=max_bytes)
        return FetchedFile(
            content=downloaded.content,
            mime_type=downloaded.mime_type,
            declared_size=downloaded.declared_size,
        )

    @staticmethod
    def _handle(locator: AttachmentLocator) -> str:
        if locator.locator_kind is not MediaLocatorKind.HANDLE:
            # WhatsApp names files by handle only; a URL here is not Meta's.
            raise MalformedMediaDescriptorError()
        return locator.locator


class WhatsAppAdapter:
    """The WhatsApp channel, end to end."""

    channel = Channel.WHATSAPP
    policy: ChannelPolicy = WhatsAppChannelPolicy()
    participant_preference: tuple[str, ...] = (IdentityKind.PHONE.value, IdentityKind.BSUID.value)
    anchor_preference: tuple[str, ...] = (IdentityKind.BSUID.value, IdentityKind.PHONE.value)

    def parse(self, payload: Mapping[str, Any]) -> ParsedDelivery:
        """A webhook delivery as neutral events, with every refusal counted."""
        envelope = parse_webhook(payload)
        events = [message_event(message) for message in envelope.messages]
        events.extend(status_event(status) for status in envelope.statuses)
        return ParsedDelivery(events=tuple(events), refused=envelope.refused)

    async def identity_scope(
        self,
        session: AsyncSession,
        connection: ChannelConnection,
        identifier: Identifier,
    ) -> IdentityScopeRef:
        if identifier.kind is IdentityKind.PHONE:
            return IdentityScopeRef(scope=IdentityScope.WORKSPACE)
        if identifier.kind is IdentityKind.BSUID:
            account = await self.account(session, connection)
            return IdentityScopeRef(scope=IdentityScope.PROVIDER_ACCOUNT, scope_ref=account.waba_id)
        raise IdentityNotAddressableError()

    def address(self, identity: ContactIdentity) -> Recipient:
        """The recipient for a conversation pinned to `identity`.

        Refused for an identity of another channel or a kind WhatsApp cannot
        address: the send stops before anything is staged.
        """
        if identity.channel is not Channel.WHATSAPP or identity.kind not in (
            IdentityKind.PHONE,
            IdentityKind.BSUID,
        ):
            raise IdentityNotAddressableError()
        return Recipient(identity_id=identity.id, kind=identity.kind.value, value=identity.value)

    async def marks_opt_out(
        self,
        session: AsyncSession,
        connection: ChannelConnection,
        action: ReplyAction,
    ) -> bool:
        """Whether a template on this number marks the tap's payload as the opt-out."""
        if action.id_or_payload is None or connection.channel is not Channel.WHATSAPP:
            return False
        templates = WhatsAppTemplateRepository(session, tenant_id=connection.tenant_id)
        return await templates.marks_opt_out_payload(
            account_id=connection.id, payload=action.id_or_payload
        )

    @staticmethod
    async def account(session: AsyncSession, connection: ChannelConnection) -> WhatsAppAccount:
        """The WhatsApp side of a connection: the number that shares its id."""
        if connection.channel is not Channel.WHATSAPP:
            raise IdentityNotAddressableError()
        numbers = WhatsAppAccountRepository(session, tenant_id=connection.tenant_id)
        return await numbers.require_by_id(connection.id)

    @staticmethod
    def credential(
        account: WhatsAppAccount, *, credentials: CredentialService
    ) -> ResolvedCredential:
        """The number's own token, or the platform's when it stores none - WhatsApp only.

        Raises `CredentialDecryptionError` for a stored token this process
        cannot read: it is never downgraded to the platform's, because sending
        as the platform when the workspace asked to send as itself is a
        different act (ADR-034).
        """
        return credentials.resolve(account)

    @asynccontextmanager
    async def sender(
        self,
        *,
        session: AsyncSession,
        connection: ChannelConnection,
        settings: Settings,
        http: httpx.AsyncClient | None = None,
        credentials: CredentialService | None = None,
    ) -> AsyncIterator[ChannelSender]:
        """A sending session with this number's credential, over a shared pool if given.

        The token is resolved per send rather than held, so its plaintext lives
        no longer than the call that needs it.
        """
        account = await self.account(session, connection)
        resolver = credentials or CredentialService(settings)
        token = self.credential(account, credentials=resolver).token
        version = settings.meta_api_version
        if http is not None:
            yield WhatsAppSender(
                client=WhatsAppClient(http=http, access_token=token, api_version=version),
                phone_number_id=account.phone_number_id,
                uploaded=[],
            )
            return
        async with build_http_client() as owned:
            yield WhatsAppSender(
                client=WhatsAppClient(http=owned, access_token=token, api_version=version),
                phone_number_id=account.phone_number_id,
                uploaded=[],
            )

    async def media_fetcher(
        self,
        *,
        session: AsyncSession,
        connection: ChannelConnection,
        settings: Settings,
        client_for: Callable[[str], WhatsAppClient] | None,
        credentials: CredentialService,
        media_id: uuid.UUID,
    ) -> ChannelMediaFetcher | None:
        """A fetcher holding this number's credential, or None if there is none to use.

        The same authority model outbound sends use (ADR-034, MEDIA-13). A token
        this process cannot decrypt is not downgraded to the platform's; the
        file ends without a fetch. The token lives only in the client built here.
        """
        if client_for is None:
            return None
        account = await self.account(session, connection)
        try:
            resolved = self.credential(account, credentials=credentials)
        except CredentialDecryptionError:
            logger.error(
                "media.credential_unreadable",
                extra={
                    "event": "media.credential_unreadable",
                    "tenant_id": str(connection.tenant_id),
                    "media_id": str(media_id),
                },
            )
            return None
        if not resolved.token:
            logger.warning(
                "media.credential_missing",
                extra={
                    "event": "media.credential_missing",
                    "tenant_id": str(connection.tenant_id),
                    "media_id": str(media_id),
                },
            )
            return None
        logger.info(
            "media.credential_resolved",
            extra={
                "event": "media.credential_resolved",
                "tenant_id": str(connection.tenant_id),
                "media_id": str(media_id),
                "workspace_credential": resolved.is_own,
            },
        )
        return WhatsAppMediaFetcher(client=client_for(resolved.token))

    @staticmethod
    def locator_expired(locator: AttachmentLocator, *, now: datetime) -> bool:
        return locator_expired(locator, now=now)


__all__ = [
    "DELIVERY_STATUSES",
    "MESSAGE_KINDS",
    "WhatsAppAdapter",
    "WhatsAppMediaFetcher",
    "WhatsAppSender",
    "message_event",
    "status_event",
]
