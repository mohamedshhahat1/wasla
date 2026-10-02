"""Outbound messaging.

The delivery protocol, which is what this module is mostly about (ADR-093):

    TX1   the send intent, state CLAIMED                      -> COMMIT
    --    (media only) upload the file. Delivers nothing.
    TX2   state REQUESTED                                     -> COMMIT
    --    ask Meta to deliver. No transaction, no connection held.
    TX3   record what came back

The message row used to be *flushed* before the call and committed after it,
which is not the same thing at all: a process that stopped between the send and
the commit left a customer holding a message this system had no record of, and
held a pooled connection for the length of a Graph API round trip while it did.

What this module is careful about:

- The send intent is **committed** before Meta can deliver anything, so a crash
  leaves a row that says a message may have gone out rather than no row at all.
- A refusal and an unknown are different outcomes. Meta declining the request
  is recorded as failed; a timeout or a 5xx leaves the row in `REQUESTED`, and
  nothing may send that message again on its own initiative.
- A rejected send is recorded rather than raised. Raising would roll the request
  back and delete the row that proves the attempt happened.
- What may be sent, when and how long it may be is the conversation's channel
  policy's to say - on WhatsApp, the 24-hour window with approved templates as
  the way out (OMNI-008).
- **A send goes where the conversation is, to whom it is pinned** (OMNI-004):
  conversation -> connection -> policy -> participant identity -> adapter.
  Nothing here reads a contact's phone number, and a participant the adapter
  cannot address on its channel is refused before anything is staged.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import (
    ChannelSender,
    MediaContent,
    OutboundContent,
    PreparedContent,
    ProviderReceipt,
    Recipient,
    SendContext,
    TemplateContent,
    TextContent,
)
from app.channels.metering import message_meters
from app.channels.outcomes import (
    ProviderAuthError,
    ProviderConnectionRefusedError,
    UncertainDeliveryError,
)
from app.channels.policy import (
    ChannelState,
    PolicyRefusalError,
    ReplyPolicy,
    SendKind,
    SendMechanism,
    inoperable_reply_policy,
    require_sendable_media,
    require_sendable_text,
)
from app.channels.registry import ChannelRegistry, ChannelUnavailableError, default_registry
from app.channels.throughput import (
    PROVIDER_THROTTLE_BACKOFF,
    THROTTLED_ORIGINS,
    ConnectionThrottledError,
    ProviderThrottledError,
)
from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    ExternalServiceError,
    RateLimitedError,
    ValidationError,
)
from app.core.filenames import require_storable_filename
from app.core.logging import get_logger
from app.core.media_types import SNIFF_BYTES, MediaClass
from app.core.media_types import resolve as resolve_media_type
from app.core.storage import EXTENSIONS, MediaStorage, StorageError, build_key
from app.db.models.audit import AuditAction, AuditActorKind
from app.db.models.billing import LimitKey
from app.db.models.channel import Channel, ConnectionHealth
from app.db.models.conversation import (
    Conversation,
    Message,
    MessageDeliveryState,
    MessageKind,
    MessageOrigin,
    MessageStatus,
)
from app.db.models.media import MediaStatus, MediaStorageState
from app.db.models.user import User
from app.db.models.whatsapp_template import TemplateStatus
from app.db.session import released
from app.integrations.whatsapp.client import TemplateWithdrawnError, build_http_client
from app.integrations.whatsapp.policy import SERVICE_WINDOW, WHATSAPP_TEXT_MAX_CHARS
from app.repositories.channel_repository import (
    ChannelConnectionRepository,
    ContactIdentityRepository,
)
from app.repositories.conversation_repository import (
    ConversationRepository,
    MessageRepository,
)
from app.repositories.media_repository import MediaRepository
from app.repositories.template_repository import WhatsAppTemplateRepository
from app.services.audit_service import AuditTrail
from app.services.credential_service import CredentialService
from app.services.entitlement_service import EntitlementService
from app.services.media_service import content_hash as media_content_hash
from app.services.template_service import refusal_reason_for
from app.services.usage_service import UsageRecorder

logger = get_logger(__name__)

# WhatsApp's limit and window live with WhatsApp's policy now (OMNI-008), and
# are importable from here under the names the request schema and tests use.
__all__ = ["SERVICE_WINDOW", "WHATSAPP_TEXT_MAX_CHARS", "MessagingService"]

# What a message records when the provider refused the connection's credential.
# A fixed sentence: the failure reason is returned by the API and read by a
# person, and nothing about a credential belongs in either.
CREDENTIAL_REFUSED = "WhatsApp refused this number's credentials."
CONNECTION_CREDENTIAL_REFUSED = "The provider refused this connection's credentials."
# A connection-level refusal (OMNI-035), and a provider throttle. Fixed
# sentences, for the same reason.
CONNECTION_SEND_REFUSED = "The provider refused to send through this connection."
PROVIDER_THROTTLED = "The provider is rate limiting this connection."

# The sentence a send on a paused connection is refused with, per channel.
_DISABLED: Final[dict[Channel, str]] = {Channel.WHATSAPP: "This WhatsApp number is disabled."}

# A caller tying its own row to this send, inside the transaction that commits
# the intent. Synchronous and staging-only, like `UsageRecorder.record`: it must
# not touch the session, because the commit that follows is the point.
LinkCall = Callable[[Message], None]


async def _attempt(
    sender: ChannelSender,
    recipient: Recipient,
    prepared: PreparedContent,
    context: SendContext,
) -> ProviderReceipt | Exception:
    """Ask the provider to deliver, returning the failure rather than raising it.

    A value rather than an exception because the caller is inside `released`,
    where nothing may touch the session - and the row that records what
    happened is on the other side of that block. The same shape
    `MediaService._write` uses, for the same reason.
    """
    try:
        return await sender.send(recipient, prepared, context)
    except (ExternalServiceError, RateLimitedError) as error:
        return error


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


def _require_same_request(
    message: Message,
    *,
    conversation_id: uuid.UUID,
    kind: MessageKind,
    body: str | None,
    template_name: str | None,
    template_language: str | None,
) -> None:
    """Refuse a key that is being reused for a different message.

    Replaying a key must return what it produced the first time. Reusing one
    for different content is a caller bug - usually a key generated once and
    then held across an edit - and silently returning the *old* message would
    tell them their new one was sent when it was not. A conflict says so.

    Compared on what the customer would see, not on every field: `sent_by_id`
    is deliberately absent, because the same key retried by the same client
    after a token refresh is still the same send.
    """
    same = (
        message.conversation_id == conversation_id
        and message.kind is kind
        and message.body == body
        and message.template_name == template_name
        and message.template_language == template_language
    )
    if not same:
        raise ConflictError("That idempotency key was already used for a different message.")


def _media_family(kind: MediaClass) -> str:
    """Which attachment family a detected class is sent as.

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
        channels: ChannelRegistry | None = None,
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
        # The route a send takes (OMNI-004): the conversation's connection and
        # the identity it is pinned to, and the channel's adapter.
        self._connections = ChannelConnectionRepository(session, tenant_id=tenant_id)
        self._identities = ContactIdentityRepository(session, tenant_id=tenant_id)
        self._channels = channels or default_registry()
        # The registry every template send is measured against, so the check
        # lives at the choke point rather than in each caller (MSG-10).
        self._templates = WhatsAppTemplateRepository(session, tenant_id=tenant_id)
        self._messages = MessageRepository(session, tenant_id=tenant_id)
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
        link: LinkCall | None = None,
        idempotency_key: str | None = None,
        origin: MessageOrigin,
    ) -> Message:
        """Send free text, refusing anything the conversation's channel will not carry.

        The length check is here rather than only in the request schema, and
        that is the point of it. The API schema caps `body` at Meta's own
        limit, but the agent reply path does not go through a request schema -
        so an agent configured with a large output budget composed a reply of
        nine thousand characters, it was sent whole, Meta refused it with a
        400, and the customer received nothing at all while the workspace had
        already paid for the inference (MSG-25).

        Refused rather than truncated or split. Truncating puts words in a
        business's mouth and cuts them off mid-sentence; splitting reintroduces
        chunk ordering, partial failure and duplicate chunks, none of which
        this system currently has to reason about because one logical message
        is one provider message. Refusing costs one reply and produces a
        recorded, alertable failure, which is the smallest correct answer.
        """
        if not body:
            raise ValidationError("A message needs something to say.")
        return await self._dispatch(
            conversation_id=conversation_id,
            kind=MessageKind.TEXT,
            body=body,
            sent_by_id=sent_by_id,
            content=TextContent(body=body, preview_url=preview_url),
            send_kind=SendKind.TEXT,
            link=link,
            idempotency_key=idempotency_key,
            origin=origin,
        )

    async def send_template(
        self,
        *,
        conversation_id: uuid.UUID,
        name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
        sent_by_id: uuid.UUID | None = None,
        link: LinkCall | None = None,
        idempotency_key: str | None = None,
        origin: MessageOrigin,
    ) -> Message:
        """Send an approved template, which is valid outside the service window.

        The registry is consulted here so that every caller inherits it. It was
        checked by the campaign and follow-up services and by nothing else, so
        `POST /conversations/{id}/messages/template` could send a template the
        registry records as `PAUSED`, `REJECTED` or `DISABLED` (MSG-10). Meta
        refuses those, so no policy violation reaches a customer - but the
        account accrues exactly the rejected-template attempts the automated
        paths are careful to avoid, and those attempts are what costs a
        workspace its number.

        A template the registry has never heard of is still allowed through.
        That asymmetry is deliberate and is argued in `refusal_reason_for`: a
        workspace that has not synced cannot be told apart from one whose
        template does not exist, and refusing both would lose every
        template-bearing message the first workspace has. Campaigns keep their
        stricter rule - the template must exist locally and be approved - on
        top of this one, because setting a campaign up is a deliberate act that
        can afford to require a sync first.
        """
        refusal = refusal_reason_for(
            await self._templates.find_anywhere(name=name, language=language)
        )
        if refusal is not None:
            raise ValidationError(refusal)

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
            content=TemplateContent(name=name, language=language, components=components),
            # Templates are WhatsApp's sanctioned way out of the service window;
            # the policy says so, and refuses them on a channel without any.
            send_kind=SendKind.TEMPLATE,
            link=link,
            idempotency_key=idempotency_key,
            origin=origin,
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
        idempotency_key: str | None = None,
        origin: MessageOrigin,
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
        if idempotency_key is not None:
            # Checked here rather than left to `_dispatch`, which would catch
            # the replay but only after this method had re-read the file and
            # re-charged the storage allowance - and would then go on to record
            # the attachment a second time. An attachment send has work either
            # side of the dispatch, so the replay has to short-circuit the
            # whole method (MSG-15).
            replayed = await self._messages.get_by_idempotency_key(idempotency_key)
            if replayed is not None:
                _require_same_request(
                    replayed,
                    conversation_id=conversation_id,
                    kind=replayed.kind,
                    body=caption,
                    template_name=None,
                    template_language=None,
                )
                return replayed

        # Before anything reaches Meta (MEDIA-06). The name used to be stored
        # raw after the send, so one over 300 characters, or carrying a NUL,
        # was delivered to the customer and then failed to record: the request
        # errored, the message stayed `pending`, and a retry sent it twice. A
        # name that cannot be stored as given is refused here, with nothing
        # sent; one that can is brought to the canonical form every later step
        # uses - the row, and the Meta-safe name derived from it.
        display_name = require_storable_filename(filename)

        detected = resolve_media_type(claimed=mime_type, prefix=content[:SNIFF_BYTES])
        canonical = detected.mime_type
        family = _media_family(detected.kind)
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

        upload_name = _safe_filename(display_name, mime_type=canonical)

        message = await self._dispatch(
            conversation_id=conversation_id,
            kind=kind,
            # The caption is the text of this message, exactly as it is on an
            # inbound one: it is what the person typed.
            body=caption,
            sent_by_id=sent_by_id,
            # Two halves: uploading the file delivers nothing and runs while the
            # intent is still `CLAIMED`; only the send can reach a customer
            # (ADR-093). The canonical type, never the caller's claim.
            content=MediaContent(
                family=family,
                content=content,
                mime_type=canonical,
                filename=upload_name,
                caption=caption,
            ),
            send_kind=SendKind.MEDIA,
            idempotency_key=idempotency_key,
            origin=origin,
        )

        await self._record_attachment(
            message=message,
            content=content,
            mime_type=canonical,
            filename=display_name,
            storage=storage,
        )
        await self._audit_attachment_sent(message, sent_by_id=sent_by_id, origin=origin)
        return message

    async def _audit_attachment_sent(
        self,
        message: Message,
        *,
        sent_by_id: uuid.UUID | None,
        origin: MessageOrigin,
    ) -> None:
        """Record a colleague sending a customer a file (MEDIA-17).

        Only a person's send, and only one that may have reached the customer:
        a send that provably failed delivered nothing. Internal identifiers
        only - never the filename, the caption or the file.
        """
        if origin is not MessageOrigin.HUMAN or sent_by_id is None:
            return
        if message.status is MessageStatus.FAILED:
            return
        actor = await self._session.get(User, sent_by_id)
        if actor is None:
            return
        media = await self._media.get_for_message(message.id)
        meta = {
            "conversation_id": str(message.conversation_id),
            "message_id": str(message.id),
        }
        if media is not None:
            meta["media_id"] = str(media.id)
        AuditTrail(self._session, tenant_id=self._tenant_id).record(
            AuditAction.MEDIA_SENT,
            actor=actor,
            actor_kind=AuditActorKind.USER,
            target_type="message",
            target_id=message.id,
            meta=meta,
        )
        await self._session.flush()

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
        content: OutboundContent,
        send_kind: SendKind,
        template_name: str | None = None,
        template_language: str | None = None,
        link: LinkCall | None = None,
        idempotency_key: str | None = None,
        origin: MessageOrigin,
    ) -> Message:
        """One outbound message, under the delivery protocol in ADR-093.

            TX1   the send intent, and whatever the caller ties to it   -> COMMIT
            --    (media only) upload the file. Provably not delivered.
            TX2   state REQUESTED                                       -> COMMIT
            --    ask the provider to deliver it. No transaction, no connection.
            TX3   record what came back

        The commits before the provider call are the point. Both orders fail;
        only one of them fails recoverably - send-then-commit leaves a customer
        holding a message this system has no record of, which is unfindable,
        while commit-then-send leaves a row that says a message may have gone
        out, which a person can read a conversation and settle.

        **Where it goes is decided here, once, for every sender** (OMNI-004,
        OMNI-027): the conversation's connection, its channel's policy, the
        identity the conversation is pinned to, and that channel's adapter. A
        channel with no adapter is refused (`ChannelUnavailableError`) rather
        than handed WhatsApp's, and a participant the adapter cannot address is
        refused before anything is staged.

        `link` lets a caller tie its own row - a follow-up, a campaign
        recipient - to this send inside TX1, so that a worker which dies mid-send
        leaves something naming the message rather than a row that looks
        untouched and gets sent again.
        """
        conversation = await self._conversations.require_by_id(conversation_id)
        adapter = self._channels.adapter_for(conversation.channel)
        policy = adapter.policy
        if isinstance(content, TextContent):
            require_sendable_text(content.body, policy)
        if isinstance(content, MediaContent):
            # The family, and the provider's own type and size limits for it,
            # before anything is staged: an over-limit file is refused here,
            # never uploaded to be refused by the provider (OMNI-045).
            require_sendable_media(
                family=content.family,
                mime_type=content.mime_type,
                byte_size=len(content.content),
                policy=policy,
            )
        decision = policy.may_send(
            conversation, origin=origin, kind=send_kind, now=datetime.now(UTC)
        )
        if not decision.allowed:
            raise PolicyRefusalError(decision.reason or "This message cannot be sent now.")
        # The policy's decision travels to the adapter (OMNI-033). Held here as
        # well as in the policy: a human-agent tag is a person's permission,
        # and no other origin may carry one whatever a policy said.
        mechanism = decision.mechanism or SendMechanism.STANDARD_WINDOW
        if mechanism is SendMechanism.HUMAN_AGENT_TAG and origin is not MessageOrigin.HUMAN:
            raise PolicyRefusalError("Only a person may reply under a human-agent tag.")
        context = SendContext(origin=origin, mechanism=mechanism)

        connection = await self._connections.require_by_id(conversation.account_id)
        if connection.channel is not conversation.channel:  # pragma: no cover - keyed
            raise ChannelUnavailableError()
        if not connection.is_active:
            raise ValidationError(_DISABLED.get(connection.channel, "This connection is disabled."))
        participant = await self._identities.require_by_id(conversation.participant_identity_id)
        recipient = adapter.address(participant)
        # The connection's allowance, shared by every sender on it (ADR-123).
        # Before anything is staged: a refusal leaves no row to reconcile.
        await self._take_allowance(connection.id, origin=origin)

        if idempotency_key is not None:
            message, claimed = await self._messages.claim_idempotency_key(
                conversation_id=conversation_id,
                kind=kind,
                body=body,
                sent_by_id=sent_by_id,
                template_name=template_name,
                template_language=template_language,
                idempotency_key=idempotency_key,
                origin=origin,
            )
            if not claimed:
                # A repeat of a request already handled. The caller gets the
                # original message back and the provider is not asked a second
                # time - which is the entire point, because no provider in scope
                # has an idempotency key of its own and a second call is a
                # second notification on somebody's phone (MSG-15).
                _require_same_request(
                    message,
                    conversation_id=conversation_id,
                    kind=kind,
                    body=body,
                    template_name=template_name,
                    template_language=template_language,
                )
                logger.info(
                    "whatsapp.outbound_replayed",
                    extra={
                        "event": "whatsapp.outbound_replayed",
                        "conversation_id": str(conversation_id),
                    },
                )
                return message
        else:
            message = await self._messages.stage_outbound(
                conversation_id=conversation_id,
                kind=kind,
                body=body,
                sent_by_id=sent_by_id,
                template_name=template_name,
                template_language=template_language,
                origin=origin,
            )
        await self._session.flush()
        if link is not None:
            link(message)

        async with (
            self._pool() as http,
            adapter.sender(
                session=self._session,
                connection=connection,
                settings=self._settings,
                http=http,
                credentials=self._credentials,
            ) as sender,
        ):
            prepared = PreparedContent(content=content)
            if isinstance(content, MediaContent):
                # TX1. Nothing has been asked of the provider, so a failure in
                # the upload below is provably not a delivery. What comes back
                # is the provider's reference to the file (OMNI-040).
                async with released(self._session):
                    try:
                        prepared = await sender.prepare(content)
                    except (ExternalServiceError, RateLimitedError) as error:
                        prepared_failure: Exception | None = error
                    else:
                        prepared_failure = None
                if prepared_failure is not None:
                    return await self._undelivered(message, reason=str(prepared_failure))

            # TX1 for a text or a template, TX2 for a file already uploaded.
            # Either way the row says "the provider may have this" *before* it
            # can. The gap between this commit and the socket resolves the safe
            # way by construction: the row claims a send that did not happen,
            # which costs somebody a look at the conversation rather than
            # costing a customer a second message.
            message.delivery_state = MessageDeliveryState.REQUESTED
            await self._session.flush()
            async with released(self._session):
                outcome = await _attempt(sender, recipient, prepared, context)

        if isinstance(outcome, UncertainDeliveryError):
            # Left exactly as it is. `REQUESTED` with `PENDING` is the honest
            # record of a message that may be on somebody's phone, and the one
            # thing that must not follow is another send (ADR-093).
            logger.warning(
                "whatsapp.outbound_uncertain",
                extra={
                    "event": "whatsapp.outbound_uncertain",
                    "conversation_id": str(conversation_id),
                },
            )
            await self._session.flush()
            return message

        if isinstance(outcome, TemplateWithdrawnError) and template_name and template_language:
            # Recorded before the row is, because the registry fact outlives
            # this send. Every later follow-up and campaign using this template
            # is now refused by `refusal_reason_for` without asking Meta, which
            # is the difference between one rejection and one per recipient
            # (MSG-24). The send itself is an ordinary undelivered one: Meta
            # declined it before reading it, so nothing reached the customer.
            await self._withdraw_template(
                name=template_name,
                language=template_language,
                code=outcome.code,
            )
            return await self._undelivered(message, reason=str(outcome))

        if isinstance(outcome, ProviderAuthError):
            # Nothing was delivered, so the row is recorded honestly as
            # undelivered - and then the failure is let out, because it is not
            # a fact about this message. Every other recipient this workspace
            # has queued fails the same way until somebody reconnects, and a
            # sweep that swallowed this would discover that once per person
            # (MSG-18). The connection says so too, for anybody looking at it
            # rather than at a message (OMNI-012).
            await self._undelivered(
                message,
                reason=(
                    CREDENTIAL_REFUSED
                    if connection.channel is Channel.WHATSAPP
                    else CONNECTION_CREDENTIAL_REFUSED
                ),
            )
            await self._connections.record_health(
                connection.id, ConnectionHealth.AUTH_FAILED, reason="credential_refused"
            )
            logger.error(
                f"{connection.channel.value}.credential_refused",
                extra={
                    "event": f"{connection.channel.value}.credential_refused",
                    "account_id": str(connection.id),
                },
            )
            raise outcome

        if isinstance(outcome, ProviderConnectionRefusedError):
            # The connection cannot send - a permission revoked, an account
            # restricted (OMNI-035). Recorded on the message and on the
            # connection's health; a bulk sender is stopped rather than left to
            # spend every recipient's attempts on it. A person or an agent gets
            # the undelivered message back, as with any refusal.
            await self._undelivered(message, reason=CONNECTION_SEND_REFUSED)
            await self._connections.record_health(
                connection.id, ConnectionHealth(outcome.health), reason=outcome.reason
            )
            logger.error(
                "channel.connection_refused",
                extra={
                    "event": "channel.connection_refused",
                    "channel": connection.channel.value,
                    "account_id": str(connection.id),
                    "reason": outcome.reason,
                },
            )
            if origin in THROTTLED_ORIGINS:
                raise outcome
            return message

        if isinstance(outcome, RateLimitedError):
            # The provider throttled the connection: declined before reading,
            # so provably undelivered (OMNI-035). A bulk sender waits for the
            # throttle to pass instead of failing this recipient - and the next,
            # and the next; anyone else gets the undelivered message back.
            await self._undelivered(message, reason=PROVIDER_THROTTLED)
            await self._connections.record_health(
                connection.id, ConnectionHealth.RATE_LIMITED, reason="provider_throttled"
            )
            if origin in THROTTLED_ORIGINS:
                now = datetime.now(UTC)
                raise ProviderThrottledError(
                    connection_id=connection.id,
                    retry_at=now + PROVIDER_THROTTLE_BACKOFF,
                    now=now,
                )
            return message

        if isinstance(outcome, Exception):
            return await self._undelivered(message, reason=str(outcome))

        now = datetime.now(UTC)
        await self._messages.mark_sent(
            message,
            wa_message_id=outcome.message_id,
            sent_at=now,
            # The provider's own time for it, where its answer gives one
            # (OMNI-042); an echo or a `sent` status may fill it in later.
            provider_sent_at=outcome.sent_at,
        )
        # Forward only (OMNI-036): a send never moves the inbox order back.
        await self._conversations.touch_outbound(conversation, at=now)
        # A credential that works again clears a recorded refusal. Conditional:
        # a healthy connection writes nothing here, on any send.
        await self._connections.record_health(connection.id, ConnectionHealth.OK)
        # Metered here and not before the call: a send the provider refused
        # cost the workspace nothing to deliver, and the failed row above
        # already records that the attempt happened. Everything that leaves this
        # way is counted once - an agent's reply, a person's, a follow-up, a
        # campaign - under the channel's decided meter (ADR-122).
        meters = message_meters(connection.channel)
        if meters is not None:
            self._usage.record(
                meters.sent,
                occurred_at=now,
                meta={"conversation_id": str(conversation_id), "kind": kind.value},
            )
        await self._session.flush()
        return message

    async def _withdraw_template(self, *, name: str, language: str, code: int) -> None:
        """Write Meta's refusal back onto the registry row, if we hold one.

        Only a template the registry already knows is updated. Creating a row
        for one it has never heard of would turn a single refusal into a
        permanent local block on a name this workspace may never have synced,
        and "unknown" is deliberately allowed to send (`refusal_reason_for`).

        `PAUSED` rather than `REJECTED` or `DISABLED`, whatever the code:
        pausing is the reversible state, and a sync is what establishes which
        of the three Meta actually means. Overstating the refusal would make a
        template that Meta un-pauses look permanently dead until somebody
        noticed.
        """
        template = await self._templates.find_anywhere(name=name, language=language)
        if template is None:
            return
        template.status = TemplateStatus.PAUSED
        template.rejection_reason = f"WhatsApp refused this template (code {code})."
        logger.warning(
            "whatsapp.template_withdrawn",
            extra={
                "event": "whatsapp.template_withdrawn",
                "template_id": str(template.id),
                "meta_code": code,
            },
        )

    async def _undelivered(self, message: Message, *, reason: str) -> Message:
        """Nothing was delivered, and that is known rather than assumed.

        Recorded, not raised: the caller gets the message back in failed state,
        and the row survives the commit. A caller that wants to try again may -
        as a *new* message, because this one is finished.
        """
        await self._messages.mark_failed(message, reason=reason)
        logger.warning(
            "whatsapp.outbound_failed",
            extra={"conversation_id": str(message.conversation_id)},
        )
        # The request's commit boundary only commits a session that is in a
        # transaction, and after `released` above this one is not until
        # something touches it.
        await self._session.flush()
        return message

    @asynccontextmanager
    async def _pool(self) -> AsyncIterator[httpx.AsyncClient]:
        """The connection pool a send goes out over: the caller's, or one for this send.

        Owned here, as it was before the adapter seam existed, so a campaign's
        ten thousand sends share one pool whatever the channel, and a request
        sending one message opens and closes its own. The adapter only borrows
        it: the credential it holds is resolved per send.
        """
        if self._http is not None:
            yield self._http
            return
        async with build_http_client() as http:
            yield http

    async def _take_allowance(self, connection_id: uuid.UUID, *, origin: MessageOrigin) -> None:
        """Spend one unit of the connection's sending allowance, or refuse (OMNI-017).

        A no-op unless `CONNECTION_SENDS_PER_MINUTE` is configured, so a
        deployment that has not chosen a limit behaves exactly as before. Only
        a bulk sender is ever refused (`THROTTLED_ORIGINS`); a reply is counted.
        """
        per_minute = self._settings.connection_sends_per_minute
        if per_minute is None:
            return
        now = datetime.now(UTC)
        retry_at = await self._connections.take_send_allowance(
            connection_id,
            per_window=per_minute,
            now=now,
            may_refuse=origin in THROTTLED_ORIGINS,
        )
        if retry_at is not None:
            logger.info(
                "channel.connection_throttled",
                extra={
                    "event": "channel.connection_throttled",
                    "connection_id": str(connection_id),
                },
            )
            raise ConnectionThrottledError(connection_id=connection_id, retry_at=retry_at, now=now)

    def window_open(self, conversation: Conversation) -> bool:
        """Whether the channel's standard free-form window is open (`service_window_open`).

        The meaning is unchanged for WhatsApp - the 24-hour customer-service
        window - and is now the conversation's channel policy's to answer
        (ADR-121). A conversation the customer has never written in has no open
        window: the business may only open it with a template.

        False, never an error, on a channel Wasla cannot act on (OMNI-031).
        """
        if self._channels.state_for(conversation.channel) is not ChannelState.OPERATIONAL:
            return False
        policy = self._channels.policy_for(conversation.channel)
        return policy.standard_window_open(conversation, now=datetime.now(UTC))

    def reply_policy(self, conversation: Conversation) -> ReplyPolicy:
        """What a person may send on this conversation now - the API's `reply_policy`.

        Presentation tolerates every channel state (OMNI-031): a paused or
        unregistered channel's conversation is rendered with nothing sendable
        rather than failing the page it is on. Sending still refuses.
        """
        state = self._channels.state_for(conversation.channel)
        if state is not ChannelState.OPERATIONAL:
            return inoperable_reply_policy(state, self._channels.known_policy(conversation.channel))
        return self._channels.policy_for(conversation.channel).reply_policy(
            conversation, now=datetime.now(UTC)
        )
