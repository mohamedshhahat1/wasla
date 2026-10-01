"""Fitting an agent's reply to the channel it is sent on.

A channel refuses a text body over its limit - WhatsApp's is 4096 characters,
Instagram's 1,000 UTF-8 *bytes* - and a model cannot be relied on to stay under
it however politely it is asked: tokens are not characters, and
the default 2,048-token budget of English is roughly twice the limit. Refusing at
the send was correct for a person typing into a form and wrong for an agent: the
turn had already engaged, so the refusal stranded it `ENGAGED` for ever, the job
dead-lettered, the inference's tokens rolled back unmetered, and the customer -
usually asking for the most detail - received nothing (AI-05).

**One message, never several.** Automatic splitting was decided against for
Messaging (MSG-25) and that decision stands: chunks reintroduce ordering, partial
failure and duplicate delivery, none of which one-message-per-turn has to reason
about. A reply that does not fit is shortened instead.

**Shortened where a person would stop.** The cut is made at the last paragraph
break, else the last sentence end, else the last line or word break, inside a
budget below the hard limit - and followed by a short offer to continue, in the
language the reply was written in. A word or URL is not cut in half unless the
text contains no break at all, and a cut never leaves a combining mark or a
joiner dangling at the end.

**Measured in the channel's unit** (OMNI-008). A 900-character Arabic reply is
about 1,700 bytes: inside a character limit of 4096, far outside a byte limit of
1,000. The budget, the continuation and the result are all counted in the unit
the conversation's channel policy declares, and a character is never split.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from app.channels.policy import ChannelCapabilities, longest_prefix, text_length
from app.core.logging import get_logger
from app.integrations.whatsapp.policy import (
    MAX_SAFE_AI_WHATSAPP_REPLY_CHARS,
    WHATSAPP_CAPABILITIES,
    WHATSAPP_TEXT_MAX_CHARS,
)

logger = get_logger(__name__)

__all__ = [
    "MAX_SAFE_AI_WHATSAPP_REPLY_CHARS",
    "WHATSAPP_TEXT_MAX_CHARS",
    "ChannelReply",
    "fallback_reply",
    "prepare_channel_reply",
]

_SEPARATOR: Final = "\n\n"

# Egyptian Arabic, matching the product's main market and the register the
# classification prompt already uses. Offered, never assumed: the customer asks.
ARABIC_CONTINUATION: Final = "لو حابب أكمل لك باقي التفاصيل قولي."
ENGLISH_CONTINUATION: Final = "Let me know if you would like me to continue with the rest."

# Sent when the model answered with no words at all, alongside a handoff.
ARABIC_FALLBACK: Final = "شكرا لرسالتك. حد من فريقنا هيتواصل معاك في أقرب وقت."
ENGLISH_FALLBACK: Final = (
    "Thanks for your message. A member of our team will get back to you shortly."
)

# A sentence end is punctuation followed by whitespace, so the dot inside a URL,
# a decimal or a domain name is not mistaken for one.
_SENTENCE_END: Final = re.compile(r"[.!?\u061f\u06d4\u2026](?=\s)")
# Characters that bind to what precedes them. A cut must not leave one at the end.
_JOINERS: Final = frozenset({"\u200d", "\ufe0e", "\ufe0f"})


@dataclass(frozen=True, slots=True)
class ChannelReply:
    """The text to send, and whether it had to be shortened to get there."""

    text: str
    truncated: bool
    original_length: int


def prepare_channel_reply(
    text: str, capabilities: ChannelCapabilities = WHATSAPP_CAPABILITIES
) -> ChannelReply:
    """Return one reply the channel will deliver, for `text`.

    A reply that already fits is sent exactly as written (trimmed of surrounding
    whitespace). Only a reply over the hard limit is shortened, and the result
    is always within it - in the channel's own unit. The default is WhatsApp's
    capabilities, for callers written before the channel was a parameter.
    """
    body = text.strip()
    unit = capabilities.text_unit
    if text_length(body, unit) <= capabilities.text_limit:
        return ChannelReply(text=body, truncated=False, original_length=len(body))

    continuation = _continuation(body)
    budget = (
        capabilities.reply_budget - text_length(continuation, unit) - text_length(_SEPARATOR, unit)
    )
    # The budget in characters: every character that fits whole, in `unit`.
    bounded = f"{_cut(body, longest_prefix(body, budget, unit))}{_SEPARATOR}{continuation}"
    logger.warning(
        "agent.reply_truncated",
        extra={
            "event": "agent.reply_truncated",
            "original_length": len(body),
            "sent_length": len(bounded),
        },
    )
    return ChannelReply(text=bounded, truncated=True, original_length=len(body))


def fallback_reply(customer_text: str) -> str:
    """What the customer is told when the model produced no reply at all (PD-3).

    Neutral on purpose: it promises a person, because the worker hands the
    conversation to one in the same breath, and it says nothing about why - a
    customer is owed an answer, not a description of somebody's AI provider. In
    the language the customer wrote in, since there is no reply to take it from.
    """
    return ARABIC_FALLBACK if _mostly_arabic(customer_text) else ENGLISH_FALLBACK


def _continuation(text: str) -> str:
    """The offer to continue, in whichever script most of the reply is written in."""
    return ARABIC_CONTINUATION if _mostly_arabic(text) else ENGLISH_CONTINUATION


def _mostly_arabic(text: str) -> bool:
    sample = text[:MAX_SAFE_AI_WHATSAPP_REPLY_CHARS]
    arabic = sum(1 for character in sample if _is_arabic(character))
    latin = sum(1 for character in sample if character.isascii() and character.isalpha())
    return arabic > latin


def _is_arabic(character: str) -> bool:
    return (
        "\u0600" <= character <= "\u06ff"
        or "\u0750" <= character <= "\u077f"
        or "\u08a0" <= character <= "\u08ff"
        or "\ufb50" <= character <= "\ufdff"
        or "\ufe70" <= character <= "\ufeff"
    )


def _cut(text: str, budget: int) -> str:
    """The longest prefix within `budget` that ends where a reader would stop.

    A break is only taken if it keeps at least half the budget - a paragraph
    break three lines in would throw away most of an answer to avoid ending
    mid-paragraph, which is the worse trade.
    """
    window = text[:budget]
    floor = budget // 2

    paragraph = window.rfind("\n\n")
    if paragraph >= floor:
        return _tidy(window[:paragraph])

    sentence_ends = [match.end() for match in _SENTENCE_END.finditer(window)]
    if sentence_ends and sentence_ends[-1] >= floor:
        return _tidy(window[: sentence_ends[-1]])

    for separator in ("\n", " "):
        index = window.rfind(separator)
        if index >= floor:
            return _tidy(window[:index])

    # No break anywhere: a hard cut, stepped back off any character that belongs
    # to the one after the budget.
    end = budget
    while 0 < end < len(text) and _attaches(text[end]):
        end -= 1
    return _tidy(text[:end])


def _attaches(character: str) -> bool:
    return (
        character in _JOINERS
        or unicodedata.combining(character) != 0
        or unicodedata.category(character) == "Mn"
    )


def _tidy(fragment: str) -> str:
    trimmed = fragment.rstrip()
    while trimmed and trimmed[-1] in _JOINERS:
        trimmed = trimmed[:-1].rstrip()
    return trimmed
