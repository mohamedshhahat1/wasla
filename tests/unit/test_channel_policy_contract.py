"""What a channel allows is its own policy's answer, never WhatsApp's by default (OMNI-008).

Three things are pinned here, without a database:

- **WhatsApp behaves exactly as it did.** The policy extracted from the shared
  services answers every question the services used to answer with constants -
  the same window, the same boundary, the same sentences, the same limit in
  the same unit. Parity, not a redesign.
- **A byte-bounded channel is measured in bytes.** A synthetic channel with a
  1,000-byte limit - the shape Instagram has - refuses and bounds text by UTF-8
  bytes. Arabic is two bytes a letter, so counting characters there sends what
  the provider refuses (mutant M12).
- **No channel falls back to WhatsApp's rules or adapter.** A channel the
  deployment has no adapter for is refused by the registry; its conversation is
  never judged by the 24-hour window or answered through WhatsApp's sender
  (mutants M5 and M6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from app.agents.orchestrator import _reply_instructions
from app.agents.reply import prepare_channel_reply
from app.channels import registry as registry_module
from app.channels.adapter import ChannelAdapter
from app.channels.metering import RECEIVED, SENT, message_meters
from app.channels.policy import (
    ChannelCapabilities,
    FollowUpAction,
    FollowUpDecision,
    OutOfWindow,
    ReceiptModel,
    SendKind,
    SendMechanism,
    TextUnit,
    WindowedPolicy,
    longest_prefix,
    require_sendable_text,
    text_length,
)
from app.channels.registry import ChannelRegistry, ChannelUnavailableError, default_registry
from app.core.exceptions import ValidationError
from app.db.models.channel import Channel
from app.db.models.conversation import Conversation, MessageOrigin
from app.db.models.usage import UsageEventType
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.integrations.whatsapp.policy import (
    AGENT_INSTRUCTIONS,
    CLOSED_WINDOW_REFUSAL,
    WHATSAPP_CAPABILITIES,
    WHATSAPP_TEXT_MAX_CHARS,
    WhatsAppChannelPolicy,
)
from tests.channel_fakes import TaggedPolicy

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
ARABIC = "مرحبا بك في متجرنا، كيف يمكنني مساعدتك اليوم؟ "

# The sentence every WhatsApp agent was told at the audit's HEAD, restated
# literally so the extraction cannot drift from it unseen.
LEGACY_AGENT_INSTRUCTIONS = (
    "\n\nYou are replying over WhatsApp. Keep every reply under "
    "4096 characters - WhatsApp will not deliver a longer "
    "one, and it will not be split for you. Prefer several short paragraphs to "
    "one long message, and offer to go into detail rather than doing it "
    "unasked."
)


def _conversation(*, last_inbound_at: datetime | None, channel: Channel) -> Conversation:
    return Conversation(id=uuid.uuid4(), last_inbound_at=last_inbound_at, channel=channel)


# ------------------------------------------------------------ a byte channel

BYTE_LIMIT = 1_000
BYTE_CAPABILITIES = ChannelCapabilities(
    text_limit=BYTE_LIMIT,
    text_unit=TextUnit.UTF8_BYTES,
    reply_budget=900,
    attachments_per_message=4,
    media_families=frozenset({"image", "video"}),
    receipts=ReceiptModel.PER_MESSAGE,
    echoes=True,
    reply_to=True,
    reactions=True,
    unsend=True,
    templates=False,
    out_of_window=OutOfWindow.NOTHING,
    message_id_scope="connection",
)
BYTE_INSTRUCTIONS = "\n\nYou are replying over a synthetic byte channel."


class ByteBoundedPolicy(WindowedPolicy):
    """An Instagram-shaped policy: bytes, no templates. Test-only; never registered."""

    channel = Channel.INSTAGRAM
    capabilities = BYTE_CAPABILITIES
    display_name = "Synthetic"
    window = timedelta(hours=24)
    closed_window_refusal = "The synthetic window has closed."

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
        return FollowUpDecision(FollowUpAction.SKIP, "Nothing may be sent now.")

    def agent_instructions(self) -> str:
        return BYTE_INSTRUCTIONS


@dataclass
class SyntheticAdapter:
    """Just enough of an adapter to be registered; nothing here sends."""

    channel: Channel = Channel.INSTAGRAM
    policy: Any = None

    def __post_init__(self) -> None:
        self.policy = ByteBoundedPolicy()


# --------------------------------------------------- WhatsApp parity (OMNI-008)


def test_the_whatsapp_window_is_twenty_four_hours_inclusive() -> None:
    policy = WhatsAppChannelPolicy()
    at_the_edge = _conversation(last_inbound_at=NOW - timedelta(hours=24), channel=Channel.WHATSAPP)
    past_it = _conversation(
        last_inbound_at=NOW - timedelta(hours=24, seconds=1), channel=Channel.WHATSAPP
    )
    never = _conversation(last_inbound_at=None, channel=Channel.WHATSAPP)

    assert policy.standard_window_open(at_the_edge, now=NOW) is True
    assert policy.standard_window_open(past_it, now=NOW) is False
    assert policy.standard_window_open(never, now=NOW) is False


def test_whatsapp_refuses_free_text_outside_the_window_with_the_same_sentence() -> None:
    policy = WhatsAppChannelPolicy()
    closed = _conversation(last_inbound_at=NOW - timedelta(days=2), channel=Channel.WHATSAPP)

    text = policy.may_send(closed, origin=MessageOrigin.HUMAN, kind=SendKind.TEXT, now=NOW)
    media = policy.may_send(closed, origin=MessageOrigin.AGENT, kind=SendKind.MEDIA, now=NOW)
    template = policy.may_send(
        closed, origin=MessageOrigin.CAMPAIGN, kind=SendKind.TEMPLATE, now=NOW
    )

    assert (text.allowed, text.reason) == (False, CLOSED_WINDOW_REFUSAL)
    assert CLOSED_WINDOW_REFUSAL == (
        "This conversation is outside the 24-hour service window. "
        "Send an approved template instead."
    )
    assert media.allowed is False
    # Templates are the sanctioned way out of the window, in or out of it.
    assert template.allowed is True


def test_whatsapp_follow_ups_decide_exactly_as_the_service_did() -> None:
    policy = WhatsAppChannelPolicy()
    open_ = _conversation(last_inbound_at=NOW - timedelta(hours=1), channel=Channel.WHATSAPP)
    closed = _conversation(last_inbound_at=NOW - timedelta(days=3), channel=Channel.WHATSAPP)

    def decide(conversation: Conversation, *, text: bool, template: bool) -> FollowUpDecision:
        return policy.follow_up(conversation, has_text=text, has_template=template, now=NOW)

    assert decide(open_, text=True, template=True).action is FollowUpAction.FREE_TEXT
    assert decide(open_, text=False, template=True).action is FollowUpAction.TEMPLATE
    assert decide(closed, text=True, template=True).action is FollowUpAction.TEMPLATE
    assert decide(open_, text=False, template=False) == FollowUpDecision(
        FollowUpAction.SKIP, "The follow-up has no message to send."
    )
    assert decide(closed, text=True, template=False) == FollowUpDecision(
        FollowUpAction.SKIP,
        "The 24-hour service window has closed and no approved template is configured.",
    )


def test_whatsapp_agents_are_told_what_they_were_always_told() -> None:
    assert AGENT_INSTRUCTIONS == LEGACY_AGENT_INSTRUCTIONS
    assert WhatsAppChannelPolicy().agent_instructions() == LEGACY_AGENT_INSTRUCTIONS
    assert _reply_instructions("Be kind.", WhatsAppChannelPolicy()) == (
        "Be kind." + LEGACY_AGENT_INSTRUCTIONS
    )


def test_whatsapp_bounds_text_in_characters_at_4096() -> None:
    policy = WhatsAppChannelPolicy()
    arabic = ("ب" * WHATSAPP_TEXT_MAX_CHARS)[:WHATSAPP_TEXT_MAX_CHARS]

    # 4,096 Arabic characters are 8,192 bytes, and WhatsApp accepts them.
    require_sendable_text(arabic, policy)
    with pytest.raises(ValidationError, match="at most 4096 characters"):
        require_sendable_text(arabic + "ب", policy)


def test_the_reply_policy_states_the_whatsapp_rule() -> None:
    last = NOW - timedelta(hours=3)
    policy = WhatsAppChannelPolicy().reply_policy(
        _conversation(last_inbound_at=last, channel=Channel.WHATSAPP), now=NOW
    )

    assert policy.free_text_allowed is True
    assert policy.window_expires_at == last + timedelta(hours=24)
    assert policy.out_of_window is OutOfWindow.TEMPLATE
    assert policy.templates is True
    assert (policy.text_limit, policy.text_limit_unit) == (4096, TextUnit.CHARACTERS)


# ------------------------------------------------- bytes are bytes (M12)


def test_a_byte_channel_refuses_arabic_that_fits_whatsapp() -> None:
    """700 Arabic characters are about 1,400 bytes: inside WhatsApp's limit,
    far outside a 1,000-byte one. Counting characters would accept it."""
    body = (ARABIC * 20)[:700]
    assert len(body) == 700 and text_length(body, TextUnit.UTF8_BYTES) > BYTE_LIMIT

    require_sendable_text(body, WhatsAppChannelPolicy())
    with pytest.raises(ValidationError):
        require_sendable_text(body, ByteBoundedPolicy())


def test_a_byte_channel_accepts_exactly_its_limit_in_bytes() -> None:
    policy = ByteBoundedPolicy()
    exactly = "ب" * (BYTE_LIMIT // 2)

    require_sendable_text(exactly, policy)
    with pytest.raises(ValidationError):
        require_sendable_text(exactly + "a", policy)


def test_an_agent_reply_is_bounded_in_the_channels_unit() -> None:
    reply = (ARABIC * 60).strip()
    assert text_length(reply, TextUnit.UTF8_BYTES) > 5 * BYTE_LIMIT

    bounded = prepare_channel_reply(reply, BYTE_CAPABILITIES)

    assert bounded.truncated is True
    assert text_length(bounded.text, TextUnit.UTF8_BYTES) <= BYTE_LIMIT
    # Nothing split mid-character: the text round-trips through UTF-8.
    assert bounded.text.encode("utf-8").decode("utf-8") == bounded.text


def test_the_same_reply_is_untouched_on_whatsapp() -> None:
    reply = (ARABIC * 20).strip()
    assert len(reply) < WHATSAPP_TEXT_MAX_CHARS

    assert prepare_channel_reply(reply, WHATSAPP_CAPABILITIES).text == reply
    assert prepare_channel_reply(reply, BYTE_CAPABILITIES).truncated is True


def test_a_byte_prefix_never_splits_a_character() -> None:
    text = "a" + "ب" + "😀" + "c"

    # 1 + 2 + 4 + 1 bytes. A budget that ends inside a character stops before it.
    assert longest_prefix(text, 2, TextUnit.UTF8_BYTES) == 1
    assert longest_prefix(text, 3, TextUnit.UTF8_BYTES) == 2
    assert longest_prefix(text, 6, TextUnit.UTF8_BYTES) == 2
    assert longest_prefix(text, 7, TextUnit.UTF8_BYTES) == 3
    assert longest_prefix(text, 3, TextUnit.CHARACTERS) == 3


def test_an_agent_is_told_about_the_conversations_channel_not_whatsapp() -> None:
    """M5's prompt half: the agent on another channel is never told it is on WhatsApp."""
    instructions = _reply_instructions("Be kind.", ByteBoundedPolicy())

    assert instructions == "Be kind." + BYTE_INSTRUCTIONS
    assert "WhatsApp" not in instructions


def test_a_channel_without_templates_refuses_a_template() -> None:
    policy = ByteBoundedPolicy()
    open_ = _conversation(last_inbound_at=NOW, channel=Channel.INSTAGRAM)

    decision = policy.may_send(
        open_, origin=MessageOrigin.CAMPAIGN, kind=SendKind.TEMPLATE, now=NOW
    )

    assert decision.allowed is False


# ------------------------------------------- no fallback to WhatsApp (M5, M6)


@pytest.mark.parametrize("channel", [Channel.INSTAGRAM, Channel.MESSENGER])
def test_a_channel_with_no_adapter_is_refused_not_handed_whatsapps(channel: Channel) -> None:
    """The default registry operates WhatsApp only. Every other channel's
    policy and adapter are refused - the wrong-channel send is impossible,
    rather than quietly WhatsApp's."""
    registry = default_registry()

    assert registry.channels == frozenset({Channel.WHATSAPP})
    with pytest.raises(ChannelUnavailableError):
        registry.policy_for(channel)
    with pytest.raises(ChannelUnavailableError):
        registry.adapter_for(channel)


def test_an_adapter_cannot_be_registered_under_another_channel() -> None:
    with pytest.raises(ValueError, match="registered for"):
        ChannelRegistry({Channel.INSTAGRAM: cast(ChannelAdapter, WhatsAppAdapter())})


def test_every_channel_in_the_vocabulary_has_its_meters_decided() -> None:
    """ENT-22: one received and one sent meter per channel. WhatsApp keeps its
    own two, the neutral meters' WhatsApp instance; every other channel writes
    the neutral two. No channel is metered as WhatsApp."""
    for channel in Channel:
        meters = message_meters(channel)
        assert meters is not None, channel
        whatsapp = channel is Channel.WHATSAPP
        assert (meters.received is UsageEventType.WHATSAPP_MESSAGE_RECEIVED) is whatsapp
        assert (meters.sent is UsageEventType.WHATSAPP_MESSAGE_SENT) is whatsapp
        assert meters.received in RECEIVED and meters.sent in SENT


def test_a_decided_meter_registers_a_second_channel_but_does_not_operate_it() -> None:
    """A test registry may operate the synthetic channel; the application's own
    registry still operates WhatsApp alone - a meter is not an adapter."""
    registry = ChannelRegistry({Channel.INSTAGRAM: cast(ChannelAdapter, SyntheticAdapter())})

    assert isinstance(registry.policy_for(Channel.INSTAGRAM), ByteBoundedPolicy)
    with pytest.raises(ChannelUnavailableError):
        registry.policy_for(Channel.WHATSAPP)
    assert default_registry().channels == frozenset({Channel.WHATSAPP})


def test_a_channel_without_a_decided_meter_cannot_be_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-E31's killer. A label added to the vocabulary with no meter must not
    go live uncounted: the registry refuses its adapter."""
    decided = registry_module.message_meters
    monkeypatch.setattr(
        registry_module,
        "message_meters",
        lambda channel: None if channel is Channel.INSTAGRAM else decided(channel),
    )
    adapter = cast(ChannelAdapter, SyntheticAdapter())

    with pytest.raises(ValueError, match="no decided usage meter"):
        ChannelRegistry({Channel.INSTAGRAM: adapter})
    ChannelRegistry({Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter())})


# ------------------------------------------- a character channel that is not WhatsApp

CHAR_LIMIT = 2_000
CHAR_CAPABILITIES = ChannelCapabilities(
    text_limit=CHAR_LIMIT,
    text_unit=TextUnit.CHARACTERS,
    reply_budget=1_900,
    attachments_per_message=1,
    media_families=frozenset({"image"}),
    receipts=ReceiptModel.WATERMARK,
    echoes=True,
    reply_to=False,
    reactions=False,
    unsend=False,
    templates=False,
    out_of_window=OutOfWindow.NOTHING,
    message_id_scope="connection",
)


class CharacterBoundedPolicy(WindowedPolicy):
    """A Messenger-shaped policy: characters, seven days, no templates. Test-only."""

    channel = Channel.MESSENGER
    capabilities = CHAR_CAPABILITIES
    display_name = "Synthetic Pages"
    window = timedelta(days=7)
    closed_window_refusal = "The synthetic page window has closed."

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
        return FollowUpDecision(FollowUpAction.SKIP, "Nothing may be sent now.")

    def agent_instructions(self) -> str:
        return "\n\nYou are replying over a synthetic page channel."


def test_a_character_channel_counts_characters_not_bytes() -> None:
    """1,500 Arabic characters are about 3,000 bytes: within a 2,000-character
    limit, so counting bytes here would wrongly refuse them."""
    body = (ARABIC * 60)[:1_500]
    assert text_length(body, TextUnit.UTF8_BYTES) > CHAR_LIMIT

    require_sendable_text(body, CharacterBoundedPolicy())
    with pytest.raises(ValidationError):
        require_sendable_text("x" * (CHAR_LIMIT + 1), CharacterBoundedPolicy())


def test_policy_answers_depend_on_the_channel_and_the_time_not_on_whatsapps_constants() -> None:
    """Day three: WhatsApp's window has closed, this channel's has not; and a
    template is WhatsApp's escape, refused where a channel has none."""
    now = datetime.now(UTC)
    three_days = now - timedelta(days=3)
    on_pages = _conversation(last_inbound_at=three_days, channel=Channel.MESSENGER)
    on_whatsapp = _conversation(last_inbound_at=three_days, channel=Channel.WHATSAPP)

    pages = CharacterBoundedPolicy()
    whatsapp = WhatsAppChannelPolicy()
    for origin in (MessageOrigin.HUMAN, MessageOrigin.AGENT):
        assert pages.may_send(on_pages, origin=origin, kind=SendKind.TEXT, now=now).allowed
        assert not whatsapp.may_send(
            on_whatsapp, origin=origin, kind=SendKind.TEXT, now=now
        ).allowed
    assert not pages.may_send(
        on_pages, origin=MessageOrigin.CAMPAIGN, kind=SendKind.TEMPLATE, now=now
    ).allowed
    assert whatsapp.may_send(
        on_whatsapp, origin=MessageOrigin.CAMPAIGN, kind=SendKind.TEMPLATE, now=now
    ).allowed
    assert pages.follow_up(on_pages, has_text=True, has_template=True, now=now).action is (
        FollowUpAction.FREE_TEXT
    )
    assert whatsapp.follow_up(on_whatsapp, has_text=True, has_template=True, now=now).action is (
        FollowUpAction.TEMPLATE
    )


def test_every_adapter_fits_the_request_ceilings() -> None:
    """The API's pre-channel ceilings are the largest any channel accepts (OMNI-044)."""
    from app.channels.policy import REQUEST_TEXT_CEILING
    from tests.channel_fakes import SyntheticAdapter

    for adapter in (WhatsAppAdapter(), SyntheticAdapter(), SyntheticAdapter(tagged=True)):
        # A byte limit is at least as many characters as it is bytes.
        assert adapter.policy.capabilities.text_limit <= REQUEST_TEXT_CEILING


# ------------------------------------------------ the human-agent tag (OMNI-033)
#
# The policy itself must refuse an AI tag, not only the choke point behind it:
# a later adapter's policy is written against this contract, and `_dispatch`'s
# second check is a backstop, not the rule (M-O16).


@pytest.mark.parametrize(
    "origin",
    [MessageOrigin.AGENT, MessageOrigin.FOLLOW_UP, MessageOrigin.CAMPAIGN, MessageOrigin.SYSTEM],
)
def test_only_a_person_is_allowed_the_human_agent_tag(origin: MessageOrigin) -> None:
    policy = TaggedPolicy(Channel.MESSENGER)
    now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    day_three = Conversation(last_inbound_at=now - timedelta(days=3))

    refused = policy.may_send(day_three, origin=origin, kind=SendKind.TEXT, now=now)
    person = policy.may_send(day_three, origin=MessageOrigin.HUMAN, kind=SendKind.TEXT, now=now)
    day_eight = policy.may_send(
        Conversation(last_inbound_at=now - timedelta(days=8)),
        origin=MessageOrigin.HUMAN,
        kind=SendKind.TEXT,
        now=now,
    )

    assert not refused.allowed and refused.mechanism is None
    assert person.allowed and person.mechanism is SendMechanism.HUMAN_AGENT_TAG
    assert not day_eight.allowed
    assert policy.reply_policy(day_three, now=now).agent_free_text_allowed is False
