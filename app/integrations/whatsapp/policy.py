"""WhatsApp's rules for sending, as a `ChannelPolicy` (OMNI-008).

Every value here used to be a constant in a shared service - the 24-hour window
in `MessagingService`, the 4,096-character body in its request schema, the
template escape in `FollowUpService`, "You are replying over WhatsApp" in the
agent prompt. They are WhatsApp's, and now they are only WhatsApp's: a
conversation on any other channel is answered by that channel's policy, and a
channel without one is refused (`ChannelUnavailableError`) rather than handed
these.

Behaviour is byte-for-byte what it was: the same window, the same limit counted
the same way, the same sentences.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from app.channels.policy import (
    ChannelCapabilities,
    FollowUpAction,
    FollowUpDecision,
    OutOfWindow,
    ReceiptModel,
    TextUnit,
    WindowedPolicy,
)
from app.db.models.channel import Channel
from app.db.models.conversation import Conversation

# Meta's own cap on a text message body. One constant, imported by the request
# schema rather than restated there, because two copies of a provider's limit
# drift and the one that drifts low silently refuses valid messages while the
# one that drifts high sends messages the provider rejects.
WHATSAPP_TEXT_MAX_CHARS: Final = 4_096

# Where a shortened agent reply is aimed, continuation included. Below the hard
# limit rather than at it, so the offer to continue always fits (AI-05).
MAX_SAFE_AI_WHATSAPP_REPLY_CHARS: Final = 3_800

# Meta's rule: a business may send free-form messages for 24 hours after the
# customer's last message. Outside it, only approved templates are accepted.
SERVICE_WINDOW: Final = timedelta(hours=24)

CLOSED_WINDOW_REFUSAL: Final = (
    "This conversation is outside the 24-hour service window. Send an approved template instead."
)
FOLLOW_UP_NOTHING_TO_SEND: Final = "The follow-up has no message to send."
FOLLOW_UP_WINDOW_CLOSED: Final = (
    "The 24-hour service window has closed and no approved template is configured."
)

WHATSAPP_CAPABILITIES: Final = ChannelCapabilities(
    text_limit=WHATSAPP_TEXT_MAX_CHARS,
    text_unit=TextUnit.CHARACTERS,
    reply_budget=MAX_SAFE_AI_WHATSAPP_REPLY_CHARS,
    # One file per WhatsApp message; the model holds several (OMNI-009).
    attachments_per_message=1,
    media_families=frozenset({"image", "document", "audio", "video"}),
    receipts=ReceiptModel.PER_MESSAGE,
    # Only WhatsApp Coexistence echoes the business's own sends, and that is
    # not supported: the adapter refuses `smb_message_echoes` as a field.
    echoes=False,
    reply_to=True,
    # Delivered by Meta and stored as unsupported; nothing acts on one.
    reactions=False,
    unsend=False,
    templates=True,
    out_of_window=OutOfWindow.TEMPLATE,
    message_id_scope="connection",
)

# What every agent is told about the channel it is answering on, appended to
# whatever the workspace wrote. Two reasons it is here rather than in the
# workspace's own prompt: a workspace cannot be relied on to know Meta's limit,
# and a workspace that deleted the sentence would get the failure back.
#
# Guidance, not a guarantee. A model asked for brevity usually obliges and
# sometimes does not, and tokens are not characters - a budget in tokens cannot
# bound a length in characters, least of all across languages. So this reduces
# how often a reply has to be shortened; `app.agents.reply` is what guarantees
# the reply that is sent fits (AI-05).
AGENT_INSTRUCTIONS: Final = (
    "\n\nYou are replying over WhatsApp. Keep every reply under "
    f"{WHATSAPP_TEXT_MAX_CHARS} characters - WhatsApp will not deliver a longer "
    "one, and it will not be split for you. Prefer several short paragraphs to "
    "one long message, and offer to go into detail rather than doing it "
    "unasked."
)


class WhatsAppChannelPolicy(WindowedPolicy):
    """The 24-hour customer-service window, with approved templates as the way out."""

    channel = Channel.WHATSAPP
    capabilities = WHATSAPP_CAPABILITIES
    display_name = "WhatsApp"
    window = SERVICE_WINDOW
    closed_window_refusal = CLOSED_WINDOW_REFUSAL

    def follow_up(
        self,
        conversation: Conversation,
        *,
        has_text: bool,
        has_template: bool,
        now: datetime,
    ) -> FollowUpDecision:
        """Free text inside the window, the template outside it, else nothing.

        The template is valid in or out of the window; free text is preferred
        inside it because it is what the nudge was written as.
        """
        window_open = self.standard_window_open(conversation, now=now)
        if window_open and has_text:
            return FollowUpDecision(FollowUpAction.FREE_TEXT)
        if has_template:
            return FollowUpDecision(FollowUpAction.TEMPLATE)
        if window_open:
            # In the window but nothing to say: a template-only follow-up whose
            # template has gone missing.
            return FollowUpDecision(FollowUpAction.SKIP, FOLLOW_UP_NOTHING_TO_SEND)
        return FollowUpDecision(FollowUpAction.SKIP, FOLLOW_UP_WINDOW_CLOSED)

    def agent_instructions(self) -> str:
        return AGENT_INSTRUCTIONS


__all__ = [
    "AGENT_INSTRUCTIONS",
    "CLOSED_WINDOW_REFUSAL",
    "MAX_SAFE_AI_WHATSAPP_REPLY_CHARS",
    "SERVICE_WINDOW",
    "WHATSAPP_CAPABILITIES",
    "WHATSAPP_TEXT_MAX_CHARS",
    "WhatsAppChannelPolicy",
]
