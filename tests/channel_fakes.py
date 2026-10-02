"""A synthetic second channel, for proving the neutral core without WhatsApp (OMNI-R10).

Nothing here is a provider. It is the smallest adapter the `ChannelAdapter`
protocol admits, shaped like the channels the audit walked through - an
Instagram-style connection-scoped sender id, a 1,000-byte text limit, no
templates, several attachments per message, echoes of the business's own sends,
and read receipts that arrive as a watermark - so a contract suite can drive the
shared ingestion, projection, identity, media and sending code through a channel
that is not WhatsApp and see that none of it reaches for WhatsApp's rules.

The application never registers it: `default_registry()` operates WhatsApp
only, and `ChannelRegistry` refuses a channel whose meter is undecided unless a
test passes `unmetered=True` (ADR-122).

The payload is this module's own shape, not any provider's:

    {"object": "synthetic", "account": "<connection key>", "events": [
        {"type": "message", "id": "...", "from": "...", "at": 1790000000,
         "text": "...", "attachments": [{"url": "...", "kind": "image",
         "mime": "image/jpeg"}]},
        {"type": "echo", "id": "...", "to": "...", "at": ..., "text": "..."},
        {"type": "status", "id": "...", "status": "delivered", "at": ...},
        {"type": "read", "from": "...", "watermark": ..., "at": ...},
    ]}
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import (
    ChannelMediaFetcher,
    ChannelSender,
    FetchedFile,
    FileProbe,
    IdentityNotAddressableError,
    IdentityScopeRef,
    OutboundContent,
    ProviderReceipt,
    Recipient,
    SendContext,
)
from app.channels.inbound import (
    AttachmentLocator,
    Identifier,
    InboundEvent,
    InboundKind,
    ParsedDelivery,
    RefusalReason,
    ReplyAction,
    StatusUpdate,
    tally,
)
from app.channels.media import locator_expired
from app.channels.policy import (
    ChannelCapabilities,
    ChannelPolicy,
    FollowUpAction,
    FollowUpDecision,
    OutOfWindow,
    ReceiptModel,
    TextUnit,
    WindowedPolicy,
)
from app.core.config import Settings
from app.db.models.channel import (
    Channel,
    ChannelConnection,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
)
from app.db.models.conversation import (
    Conversation,
    MessageKind,
    MessageStatus,
    ReplyActionSource,
)
from app.db.models.media import MediaLocatorKind

PAYLOAD_OBJECT: Final = "synthetic"
#: The one quick-reply payload the synthetic provider treats as an opt-out.
OPT_OUT_PAYLOAD: Final = "SYNTHETIC-STOP"
BYTE_LIMIT: Final = 1_000
SYNTHETIC_INSTRUCTIONS: Final = "\n\nYou are replying over the synthetic test channel."

SYNTHETIC_CAPABILITIES: Final = ChannelCapabilities(
    text_limit=BYTE_LIMIT,
    text_unit=TextUnit.UTF8_BYTES,
    reply_budget=900,
    attachments_per_message=4,
    media_families=frozenset({"image", "video"}),
    receipts=ReceiptModel.WATERMARK,
    echoes=True,
    reply_to=True,
    reactions=True,
    unsend=True,
    templates=False,
    out_of_window=OutOfWindow.NOTHING,
    message_id_scope="connection",
)

_KINDS: Final = {"text": MessageKind.TEXT, "image": MessageKind.IMAGE, "video": MessageKind.VIDEO}
_STATUSES: Final = {
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}


class SyntheticPolicy(WindowedPolicy):
    """A seven-day window, bytes, no templates - nothing like WhatsApp's."""

    capabilities = SYNTHETIC_CAPABILITIES
    display_name = "Synthetic"
    window = timedelta(days=7)
    closed_window_refusal = "The synthetic reply window has closed."

    def __init__(self, channel: Channel) -> None:
        self.channel = channel

    def follow_up(
        self,
        conversation: Conversation,
        *,
        has_text: bool,
        has_template: bool,
        now: datetime,
    ) -> FollowUpDecision:
        if self.standard_window_open(conversation, now=now) and has_text:
            return FollowUpDecision(FollowUpAction.FREE_TEXT)
        return FollowUpDecision(FollowUpAction.SKIP, "Nothing may be sent on this channel now.")

    def agent_instructions(self) -> str:
        return SYNTHETIC_INSTRUCTIONS


#: A Messenger-shaped channel: 24 hours for anybody, then seven more days for a
#: person under a human-agent tag, and never for an agent (OMNI-033).
TAGGED_CAPABILITIES: Final = ChannelCapabilities(
    text_limit=BYTE_LIMIT,
    text_unit=TextUnit.UTF8_BYTES,
    reply_budget=900,
    attachments_per_message=4,
    media_families=frozenset({"image", "video"}),
    receipts=ReceiptModel.WATERMARK,
    echoes=True,
    reply_to=True,
    reactions=True,
    unsend=True,
    templates=False,
    out_of_window=OutOfWindow.TAG,
    message_id_scope="connection",
)


class TaggedPolicy(SyntheticPolicy):
    """The synthetic channel with Messenger's rules: a 24-hour window and a 7-day human tag."""

    capabilities = TAGGED_CAPABILITIES
    window = timedelta(hours=24)
    human_tag_window = timedelta(days=7)
    closed_window_refusal = "The tagged reply window has closed."


@dataclass
class SendLog:
    """Everything the synthetic provider was asked to do."""

    sent: list[tuple[Recipient, OutboundContent]] = field(default_factory=list)
    contexts: list[SendContext] = field(default_factory=list)
    prepared: list[OutboundContent] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)


@dataclass
class _Sender:
    log: SendLog

    async def prepare(self, content: OutboundContent) -> None:
        self.log.prepared.append(content)

    async def send(
        self, recipient: Recipient, content: OutboundContent, context: SendContext
    ) -> ProviderReceipt:
        self.log.sent.append((recipient, content))
        self.log.contexts.append(context)
        return ProviderReceipt(message_id=f"syn.out.{uuid.uuid4().hex}")


@dataclass
class _Fetcher:
    log: SendLog
    content: bytes

    async def probe(self, locator: AttachmentLocator) -> FileProbe:
        return FileProbe(mime_type=locator.mime_type, byte_size=len(self.content))

    async def fetch(self, locator: AttachmentLocator, *, max_bytes: int) -> FetchedFile:
        self.log.fetched.append(locator.locator)
        return FetchedFile(content=self.content[:max_bytes], mime_type=locator.mime_type)


def _at(raw: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class SyntheticAdapter:
    """The synthetic channel, end to end. `channel` defaults to Instagram's label."""

    participant_preference: tuple[str, ...] = (IdentityKind.IGSID.value,)
    anchor_preference: tuple[str, ...] = (IdentityKind.IGSID.value,)

    def __init__(
        self,
        channel: Channel = Channel.INSTAGRAM,
        *,
        file: bytes = b"",
        tagged: bool = False,
    ) -> None:
        self.channel = channel
        self.policy: ChannelPolicy = TaggedPolicy(channel) if tagged else SyntheticPolicy(channel)
        self.log = SendLog()
        self._file = file

    # ------------------------------------------------------------- inbound

    def parse(self, payload: Mapping[str, Any]) -> ParsedDelivery:
        refused: Counter[RefusalReason] = Counter()
        if not isinstance(payload, Mapping) or payload.get("object") != PAYLOAD_OBJECT:
            refused[RefusalReason.FOREIGN_OBJECT] += 1
            return ParsedDelivery(events=(), refused=tally(refused))
        account = payload.get("account")
        raw_events = payload.get("events")
        if not isinstance(account, str) or not account:
            refused[RefusalReason.MISSING_CONNECTION] += 1
            return ParsedDelivery(events=(), refused=tally(refused))
        if not isinstance(raw_events, list):
            refused[RefusalReason.MALFORMED] += 1
            return ParsedDelivery(events=(), refused=tally(refused))

        events: list[InboundEvent] = []
        for raw in raw_events:
            event = self._event(account, raw, refused)
            if event is not None:
                events.append(event)
        return ParsedDelivery(events=tuple(events), refused=tally(refused))

    def _event(
        self, account: str, raw: Any, refused: Counter[RefusalReason]
    ) -> InboundEvent | None:
        if not isinstance(raw, Mapping):
            refused[RefusalReason.MALFORMED] += 1
            return None
        kind = raw.get("type")
        at = _at(raw.get("at"))
        if kind == "read":
            reader = raw.get("from")
            watermark = _at(raw.get("watermark"))
            if not isinstance(reader, str) or not reader or watermark is None:
                refused[RefusalReason.MALFORMED] += 1
                return None
            return InboundEvent(
                channel=self.channel,
                connection_key=account,
                kind=InboundKind.STATUS,
                event_id=f"read:{reader}:{int(watermark.timestamp())}",
                occurred_at=at,
                sender=(Identifier(IdentityKind.IGSID, reader),),
                status=StatusUpdate(
                    status=MessageStatus.READ, provider_status="read", watermark=watermark
                ),
                raw=dict(raw),
            )
        event_id = raw.get("id")
        if not isinstance(event_id, str) or not event_id:
            refused[RefusalReason.MISSING_EVENT_ID] += 1
            return None
        if kind == "status":
            status = _STATUSES.get(str(raw.get("status")))
            if status is None:
                refused[RefusalReason.MISSING_STATUS] += 1
                return None
            return InboundEvent(
                channel=self.channel,
                connection_key=account,
                kind=InboundKind.STATUS,
                event_id=f"{event_id}:{raw.get('status')}",
                occurred_at=at,
                status=StatusUpdate(
                    status=status, provider_status=str(raw.get("status")), message_id=event_id
                ),
                raw=dict(raw),
            )
        if kind not in ("message", "echo"):
            refused[RefusalReason.UNSUPPORTED_FIELD] += 1
            return None
        party = raw.get("from" if kind == "message" else "to")
        if not isinstance(party, str) or not party:
            refused[RefusalReason.MISSING_SENDER] += 1
            return None
        attachments = tuple(
            AttachmentLocator(
                locator_kind=MediaLocatorKind.URL,
                locator=str(item["url"]),
                media_kind=str(item.get("kind", "image")),
                mime_type=item.get("mime"),
            )
            for item in raw.get("attachments", ())
            if isinstance(item, Mapping) and isinstance(item.get("url"), str)
        )
        message_kind = (
            _KINDS.get(attachments[0].media_kind, MessageKind.UNSUPPORTED)
            if attachments
            else MessageKind.TEXT
        )
        # A Messenger-shaped quick reply: words beside a payload (OMNI-030).
        action = None
        quick = raw.get("quick_reply")
        if isinstance(quick, Mapping):
            payload = quick.get("payload")
            title = quick.get("title")
            action = ReplyAction(
                source=ReplyActionSource.QUICK_REPLY,
                id_or_payload=payload if isinstance(payload, str) else None,
                title=title if isinstance(title, str) else None,
            )
            message_kind = MessageKind.INTERACTIVE
        text = raw.get("text") if isinstance(raw.get("text"), str) else None
        if text is None and action is not None:
            text = action.title
        return InboundEvent(
            channel=self.channel,
            connection_key=account,
            kind=InboundKind.MESSAGE if kind == "message" else InboundKind.ECHO,
            event_id=event_id,
            occurred_at=at,
            sender=(Identifier(IdentityKind.IGSID, party),),
            message_id=event_id,
            message_kind=message_kind,
            text=text,
            attachments=attachments,
            action=action,
            raw=dict(raw),
        )

    async def identity_scope(
        self,
        session: AsyncSession,
        connection: ChannelConnection,
        identifier: Identifier,
    ) -> IdentityScopeRef:
        """Unique per connection, the way Instagram and Page-scoped ids are."""
        if identifier.kind is not IdentityKind.IGSID or connection.channel is not self.channel:
            raise IdentityNotAddressableError()
        return IdentityScopeRef(
            scope=IdentityScope.CONNECTION,
            scope_ref=str(connection.id),
            connection_id=connection.id,
        )

    async def marks_opt_out(
        self,
        session: AsyncSession,
        connection: ChannelConnection,
        action: ReplyAction,
    ) -> bool:
        """The synthetic provider marks one payload, as a workspace's template would."""
        return action.id_or_payload == OPT_OUT_PAYLOAD

    # ------------------------------------------------------------ outbound

    def address(self, identity: ContactIdentity) -> Recipient:
        if identity.channel is not self.channel or identity.kind is not IdentityKind.IGSID:
            raise IdentityNotAddressableError()
        return Recipient(identity_id=identity.id, kind=identity.kind.value, value=identity.value)

    @asynccontextmanager
    async def sender(
        self,
        *,
        session: AsyncSession,
        connection: ChannelConnection,
        settings: Settings,
        http: Any | None = None,
        credentials: Any | None = None,
    ) -> AsyncIterator[ChannelSender]:
        if connection.channel is not self.channel:
            raise IdentityNotAddressableError()
        yield _Sender(self.log)

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
        return _Fetcher(self.log, self._file)

    def locator_expired(self, locator: AttachmentLocator, *, now: datetime) -> bool:
        return locator_expired(locator, now=now)


def synthetic_payload(account: str, *events: Mapping[str, Any]) -> dict[str, Any]:
    """One synthetic delivery for connection key `account`."""
    return {"object": PAYLOAD_OBJECT, "account": account, "events": list(events)}


__all__ = [
    "BYTE_LIMIT",
    "OPT_OUT_PAYLOAD",
    "PAYLOAD_OBJECT",
    "SYNTHETIC_CAPABILITIES",
    "SYNTHETIC_INSTRUCTIONS",
    "TAGGED_CAPABILITIES",
    "SendLog",
    "SyntheticAdapter",
    "SyntheticPolicy",
    "TaggedPolicy",
    "synthetic_payload",
]
