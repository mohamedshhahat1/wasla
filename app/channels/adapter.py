"""The seam between the shared messaging core and one provider (ADR-121).

An adapter owns everything about its provider that the core must not know: the
webhook's shape, what a sender identifier is scoped by, how a participant is
addressed, how content is rendered on the wire, which credential a connection
sends with, where files are fetched from, and which failures mean what. The core
owns everything else - the delivery protocol, idempotency, metering, AI turns,
the inbox - once, for every channel.

**The address is the conversation's participant identity, and only that**
(OMNI-004). `address` takes the identity the conversation is pinned to and
nothing about the contact; an identity from another channel, or of a kind the
adapter cannot address, is refused rather than coerced.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.inbound import AttachmentLocator, Identifier, ParsedDelivery, ReplyAction
from app.channels.policy import ChannelPolicy, SendMechanism
from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.channel import Channel, ChannelConnection, ContactIdentity, IdentityScope
from app.db.models.conversation import MessageOrigin


class IdentityNotAddressableError(ValidationError):
    """The conversation's participant cannot be addressed on this channel.

    Refused before anything is staged: an identity from another channel - a
    WhatsApp phone on an Instagram conversation - is exactly the wrong-channel
    send this seam exists to make impossible (OMNI-004).
    """

    message = "This conversation's participant cannot be addressed on its channel."


@dataclass(frozen=True, slots=True)
class Recipient:
    """Who a send is addressed to, in the provider's terms."""

    identity_id: uuid.UUID
    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class TextContent:
    body: str
    preview_url: bool = False


@dataclass(frozen=True, slots=True)
class TemplateContent:
    name: str
    language: str
    components: list[dict[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class MediaContent:
    """A file the business is sending: bytes already type-checked by the core."""

    #: The provider-neutral family the bytes were detected as.
    family: str
    content: bytes
    mime_type: str
    filename: str
    caption: str | None = None


OutboundContent = TextContent | TemplateContent | MediaContent


@dataclass(frozen=True, slots=True)
class SendContext:
    """Who is sending and by which mechanism - the policy's decision, carried to the wire.

    OMNI-033. The adapter used to receive only a recipient and content, so a
    Messenger or Instagram adapter could not know that a person, not the AI,
    was replying on day three and must send under `HUMAN_AGENT` - nor refuse
    to let an agent do so. The mechanism is the policy's; the adapter renders
    it and never chooses it.
    """

    origin: MessageOrigin
    mechanism: SendMechanism


@dataclass(frozen=True, slots=True)
class ProviderReceipt:
    """The provider's acknowledgement of one accepted message."""

    message_id: str
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IdentityScopeRef:
    """Where an identifier is unique, as the adapter's provider guarantees it."""

    scope: IdentityScope
    scope_ref: str = ""
    connection_id: uuid.UUID | None = None


class ChannelSender(Protocol):
    """One connection's sending session, holding its credential for one send.

    The two halves ADR-093 separates: `prepare` delivers nothing (an upload) and
    runs while the send is still `CLAIMED`; `send` is the request that may reach
    a customer. Both raise only the neutral outcome types
    (`app.channels.outcomes`), `RateLimitedError` and `ExternalServiceError`.
    """

    async def prepare(self, content: OutboundContent) -> None: ...

    async def send(
        self, recipient: Recipient, content: OutboundContent, context: SendContext
    ) -> ProviderReceipt: ...


@dataclass(frozen=True, slots=True)
class FetchedFile:
    """What a provider returned for one attachment."""

    content: bytes
    mime_type: str | None
    declared_size: int | None = None


@dataclass(frozen=True, slots=True)
class FileProbe:
    """What a provider says about an attachment before it is fetched."""

    mime_type: str | None
    byte_size: int | None


class ChannelMediaFetcher(Protocol):
    """Fetches inbound attachments for one connection.

    The shared pipeline - claim, bounds, hashing, type sniffing, storage,
    reading, retention - is the same for every channel (ADR-110); only this
    edge differs. Raises the media outcome types of the core
    (`app.channels.media`), never provider exceptions.
    """

    async def probe(self, locator: AttachmentLocator) -> FileProbe: ...

    async def fetch(self, locator: AttachmentLocator, *, max_bytes: int) -> FetchedFile: ...


class ChannelAdapter(Protocol):
    """One provider's implementation of a channel."""

    channel: Channel
    policy: ChannelPolicy
    #: The sender identifier kinds a new conversation is pinned to, most
    #: preferred first - WhatsApp pins a phone when Meta names one, and a
    #: business-scoped id when it does not.
    participant_preference: tuple[str, ...]
    #: When one message's identifiers already belong to two different contacts,
    #: the kind whose contact the message goes to - nothing is merged
    #: (ADR-118). WhatsApp anchors on the business-scoped id, which Meta sends
    #: on every message, so a thread does not flip as the phone comes and goes.
    anchor_preference: tuple[str, ...]

    def parse(self, payload: Mapping[str, Any]) -> ParsedDelivery:
        """This provider's webhook as neutral events. Never raises."""
        ...

    async def identity_scope(
        self,
        session: AsyncSession,
        connection: ChannelConnection,
        identifier: Identifier,
    ) -> IdentityScopeRef:
        """Where `identifier` is unique, for a sender on `connection`."""
        ...

    def address(self, identity: ContactIdentity) -> Recipient:
        """The recipient for a conversation pinned to `identity`, or a refusal."""
        ...

    async def marks_opt_out(
        self,
        session: AsyncSession,
        connection: ChannelConnection,
        action: ReplyAction,
    ) -> bool:
        """Whether this tap's payload is one the workspace marked as its opt-out (OMNI-030).

        The stop-phrase match on the tap's words is shared and done by the core;
        this is the part only a provider can answer - on WhatsApp, a payload a
        template on this number is marked with.
        """
        ...

    def sender(
        self,
        *,
        session: AsyncSession,
        connection: ChannelConnection,
        settings: Settings,
        http: Any | None = None,
        credentials: Any | None = None,
    ) -> AbstractAsyncContextManager[ChannelSender]:
        """A sending session holding this connection's credential."""
        ...

    async def media_fetcher(
        self,
        *,
        session: AsyncSession,
        connection: ChannelConnection,
        settings: Settings,
        client_for: Callable[[str], Any] | None,
        credentials: Any,
        media_id: uuid.UUID,
    ) -> ChannelMediaFetcher | None:
        """A fetcher holding this connection's credential, or None if there is none.

        `client_for` builds the provider's client from a credential over the
        caller's connection pool; an adapter that needs none ignores it.
        """
        ...

    def locator_expired(self, locator: AttachmentLocator, *, now: datetime) -> bool:
        """Whether a locator can no longer be fetched."""
        ...


__all__ = [
    "ChannelAdapter",
    "ChannelMediaFetcher",
    "ChannelSender",
    "FetchedFile",
    "FileProbe",
    "IdentityNotAddressableError",
    "IdentityScopeRef",
    "MediaContent",
    "OutboundContent",
    "ProviderReceipt",
    "Recipient",
    "SendContext",
    "TemplateContent",
    "TextContent",
]
