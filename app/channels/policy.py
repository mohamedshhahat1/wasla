"""What a channel allows, asked of the conversation's own channel (OMNI-008).

Shared code used to answer these with WhatsApp's constants: a 24-hour window
with an approved-template escape, a 4,096-character body, an agent told it was
"replying over WhatsApp". The rules on other channels differ in *kind*, not only
in number - Instagram bounds text in UTF-8 **bytes**, Messenger lets a person
reply for seven days where a bot may not, neither has templates - so the answers
come from a `ChannelPolicy` resolved from the conversation's channel, and the
shared code only asks.

Deliberately small. A policy answers the questions today's code actually asks
and declares the capabilities that stop shared code assuming WhatsApp; it is not
a feature matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from app.core.exceptions import ValidationError
from app.db.models.channel import Channel
from app.db.models.conversation import Conversation, MessageOrigin


class TextUnit(StrEnum):
    """What a channel's text limit counts."""

    CHARACTERS = "characters"
    UTF8_BYTES = "utf8_bytes"


class ReceiptModel(StrEnum):
    """How a channel reports that a message was delivered or read."""

    PER_MESSAGE = "per_message"
    WATERMARK = "watermark"
    NONE = "none"


class OutOfWindow(StrEnum):
    """What may still be sent once a channel's standard reply window has closed."""

    #: An approved template (WhatsApp).
    TEMPLATE = "template"
    #: A message tag a *person* may reply under for a while longer - Messenger
    #: and Instagram's `HUMAN_AGENT`, seven days. Never an automated sender's.
    TAG = "tag"
    #: Nothing free-form, and no template mechanism to escape with.
    NOTHING = "nothing"


class SendMechanism(StrEnum):
    """How a permitted send is made - what the adapter must put on the wire (OMNI-033).

    The policy decides it; the adapter only renders it. Messenger needs
    `messaging_type` on every send and `MESSAGE_TAG` + `HUMAN_AGENT` for a
    person's reply after the standard window; WhatsApp's two are implicit in
    the content (free text or an approved template).
    """

    STANDARD_WINDOW = "standard_window"
    TEMPLATE = "template"
    HUMAN_AGENT_TAG = "human_agent_tag"


class ChannelState(StrEnum):
    """Whether Wasla can act on a channel right now (OMNI-031, ADR-126)."""

    OPERATIONAL = "operational"
    #: Registered and deliberately switched off: inbound is still kept.
    PAUSED = "paused"
    #: No adapter in this deployment.
    UNAVAILABLE = "unavailable"


class SendKind(StrEnum):
    """The form of an outbound message, as a policy judges it."""

    TEXT = "text"
    MEDIA = "media"
    TEMPLATE = "template"


class FollowUpAction(StrEnum):
    """What a due follow-up may do on its conversation's channel now."""

    FREE_TEXT = "free_text"
    TEMPLATE = "template"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    """What a channel can carry. Declared by its adapter; read, never assumed."""

    #: The longest text body the provider accepts, in `text_unit`.
    text_limit: int
    text_unit: TextUnit
    #: Where an agent's reply is aimed, in `text_unit`: under the hard limit, so
    #: an offer to continue always fits.
    reply_budget: int
    attachments_per_message: int
    media_families: frozenset[str]
    receipts: ReceiptModel
    echoes: bool
    reply_to: bool
    reactions: bool
    unsend: bool
    templates: bool
    out_of_window: OutOfWindow
    #: Within what the provider guarantees a message id is unique: its
    #: connection (Meta), or one chat (Telegram-shaped providers).
    message_id_scope: Literal["connection", "conversation"]
    #: Whether an automated reply must tell the customer it is automated - at
    #: the start, after a long gap, and after a person hands back to the AI
    #: (OMNI-041). Messenger's and Instagram's policy; off for WhatsApp.
    disclosure_required: bool = False


@dataclass(frozen=True, slots=True)
class SendDecision:
    """Whether a send may go now, and by which mechanism (OMNI-033).

    A refusal carries the sentence the caller sees; an allowance carries what
    the adapter must render, so a channel's rule about *how* reaches the wire.
    """

    allowed: bool
    reason: str | None = None
    mechanism: SendMechanism | None = None

    @classmethod
    def allow(cls, mechanism: SendMechanism = SendMechanism.STANDARD_WINDOW) -> SendDecision:
        return cls(allowed=True, mechanism=mechanism)

    @classmethod
    def refuse(cls, reason: str) -> SendDecision:
        return cls(allowed=False, reason=reason)


@dataclass(frozen=True, slots=True)
class FollowUpDecision:
    """What a due follow-up does. `reason` explains a skip."""

    action: FollowUpAction
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReplyPolicy:
    """What a person may send on a conversation now - the API's `reply_policy`.

    Additive beside the older `service_window_open`, which keeps its WhatsApp
    meaning (ADR-121): a new client reads this instead of inferring the rule.
    """

    free_text_allowed: bool
    window_expires_at: datetime | None
    out_of_window: OutOfWindow
    templates: bool
    text_limit: int
    text_limit_unit: TextUnit
    #: Whether Wasla can act on the channel at all; anything but operational
    #: means nothing may be sent, whatever the window says (OMNI-031).
    state: ChannelState = ChannelState.OPERATIONAL
    #: How a person's free text would go now - the standard window, or a
    #: human-agent tag after it (OMNI-033). None when it may not go at all.
    free_text_mechanism: SendMechanism | None = None
    #: Whether an *agent's* free text may go now. Narrower than a person's on a
    #: channel whose late replies need a human-agent tag, which an agent can
    #: never use (OMNI-033).
    agent_free_text_allowed: bool = False


def inoperable_reply_policy(state: ChannelState, policy: ChannelPolicy | None) -> ReplyPolicy:
    """What a client is told about a conversation on a channel Wasla cannot act on.

    Rendered rather than refused (OMNI-031): one conversation on a paused or
    unregistered channel used to turn the whole inbox page into a 422,
    WhatsApp's threads included. Nothing may be sent - no free text, no
    template - and the limits are the channel's own where its adapter is known.
    """
    capabilities = policy.capabilities if policy is not None else None
    return ReplyPolicy(
        free_text_allowed=False,
        window_expires_at=None,
        out_of_window=OutOfWindow.NOTHING,
        templates=False,
        text_limit=capabilities.text_limit if capabilities is not None else 0,
        text_limit_unit=(
            capabilities.text_unit if capabilities is not None else TextUnit.CHARACTERS
        ),
        state=state,
    )


def text_length(text: str, unit: TextUnit) -> int:
    """How long `text` is in `unit`."""
    if unit is TextUnit.UTF8_BYTES:
        return len(text.encode("utf-8"))
    return len(text)


def longest_prefix(text: str, budget: int, unit: TextUnit) -> int:
    """How many characters of `text` fit in `budget` units - never splitting a character.

    For a byte budget the answer is found character by character, so a
    multi-byte character (every Arabic letter is two bytes, an emoji four) is
    either wholly inside the budget or wholly outside it.
    """
    if budget <= 0:
        return 0
    if unit is TextUnit.CHARACTERS:
        return min(len(text), budget)
    spent = 0
    for index, character in enumerate(text):
        spent += len(character.encode("utf-8"))
        if spent > budget:
            return index
    return len(text)


class ChannelPolicy(Protocol):
    """The questions shared code asks about sending on one channel."""

    channel: Channel
    capabilities: ChannelCapabilities
    #: How the channel is named in a sentence a person reads.
    display_name: str

    def may_send(
        self,
        conversation: Conversation,
        *,
        origin: MessageOrigin,
        kind: SendKind,
        now: datetime,
    ) -> SendDecision:
        """Whether `origin` may send `kind` on this conversation at `now`."""
        ...

    def standard_window_open(self, conversation: Conversation, *, now: datetime) -> bool:
        """Whether the channel's standard free-form window is open."""
        ...

    def follow_up(
        self,
        conversation: Conversation,
        *,
        has_text: bool,
        has_template: bool,
        now: datetime,
    ) -> FollowUpDecision:
        """What a due follow-up may do now."""
        ...

    def reply_policy(self, conversation: Conversation, *, now: datetime) -> ReplyPolicy:
        """What a person may send now, for a client to render."""
        ...

    def agent_instructions(self) -> str:
        """What every agent is told about this channel, after its own prompt."""
        ...


class WindowedPolicy:
    """A policy built on one rule: free text for a while after the customer writes.

    WhatsApp's shape, and a starting point rather than a framework - a channel
    whose rule depends on who is sending overrides `may_send`.
    """

    channel: Channel
    capabilities: ChannelCapabilities
    display_name: str
    #: How long after the customer's last message free-form sends are allowed.
    window: timedelta
    #: The sentence a free-form send outside the window is refused with.
    closed_window_refusal: str
    #: How long after the customer's last message a *person* may still reply
    #: under a human-agent tag (Messenger, Instagram: seven days). None where
    #: the channel has no such tag. An agent never may, whatever this says.
    human_tag_window: timedelta | None = None

    def human_tag_open(self, conversation: Conversation, *, now: datetime) -> bool:
        """Whether a person's late reply may still go under the human-agent tag."""
        if self.human_tag_window is None or conversation.last_inbound_at is None:
            return False
        return now - conversation.last_inbound_at <= self.human_tag_window

    def standard_window_open(self, conversation: Conversation, *, now: datetime) -> bool:
        if conversation.last_inbound_at is None:
            return False
        return now - conversation.last_inbound_at <= self.window

    def window_expires_at(self, conversation: Conversation) -> datetime | None:
        if conversation.last_inbound_at is None:
            return None
        return conversation.last_inbound_at + self.window

    def may_send(
        self,
        conversation: Conversation,
        *,
        origin: MessageOrigin,
        kind: SendKind,
        now: datetime,
    ) -> SendDecision:
        if kind is SendKind.TEMPLATE:
            if not self.capabilities.templates:
                return SendDecision.refuse("This channel has no message templates.")
            return SendDecision.allow(SendMechanism.TEMPLATE)
        if self.standard_window_open(conversation, now=now):
            return SendDecision.allow(SendMechanism.STANDARD_WINDOW)
        # After the window, only a person, only under the human-agent tag, and
        # only for as long as the tag allows. An agent, a follow-up or a
        # campaign never borrows a human agent's permission (OMNI-033).
        if origin is MessageOrigin.HUMAN and self.human_tag_open(conversation, now=now):
            return SendDecision.allow(SendMechanism.HUMAN_AGENT_TAG)
        return SendDecision.refuse(self.closed_window_refusal)

    def reply_policy(self, conversation: Conversation, *, now: datetime) -> ReplyPolicy:
        window_open = self.standard_window_open(conversation, now=now)
        tagged = not window_open and self.human_tag_open(conversation, now=now)
        mechanism = (
            SendMechanism.STANDARD_WINDOW
            if window_open
            else SendMechanism.HUMAN_AGENT_TAG if tagged else None
        )
        return ReplyPolicy(
            free_text_allowed=window_open or tagged,
            window_expires_at=self.window_expires_at(conversation),
            out_of_window=self.capabilities.out_of_window,
            templates=self.capabilities.templates,
            text_limit=self.capabilities.text_limit,
            text_limit_unit=self.capabilities.text_unit,
            free_text_mechanism=mechanism,
            agent_free_text_allowed=window_open,
        )


def require_sendable_text(body: str, policy: ChannelPolicy) -> None:
    """Refuse a body the channel will not accept, before anything is staged.

    Measured in the channel's own unit: a 700-character Arabic reply is about
    1,300 bytes, which fits WhatsApp's 4,096 characters and does not fit
    Instagram's 1,000 bytes. Counting characters for a byte-bounded channel is
    the mistake that sends a reply the provider refuses after the workspace has
    paid for the inference (OMNI-008).
    """
    if not body:
        raise ValidationError("A message needs something to say.")
    capabilities = policy.capabilities
    if text_length(body, capabilities.text_unit) > capabilities.text_limit:
        unit = "characters" if capabilities.text_unit is TextUnit.CHARACTERS else "bytes"
        raise ValidationError(
            f"A {policy.display_name} message may be at most {capabilities.text_limit} {unit}."
        )


__all__ = [
    "ChannelCapabilities",
    "ChannelPolicy",
    "ChannelState",
    "FollowUpAction",
    "FollowUpDecision",
    "OutOfWindow",
    "ReceiptModel",
    "ReplyPolicy",
    "SendDecision",
    "SendKind",
    "SendMechanism",
    "TextUnit",
    "WindowedPolicy",
    "inoperable_reply_policy",
    "longest_prefix",
    "require_sendable_text",
    "text_length",
]
