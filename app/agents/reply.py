"""Fitting an agent's reply to the channel it is sent on.

WhatsApp refuses a text body over 4096 characters, and a model cannot be relied
on to stay under it however politely it is asked: tokens are not characters, and
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
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from app.core.logging import get_logger
from app.services.messaging_service import WHATSAPP_TEXT_MAX_CHARS

logger = get_logger(__name__)

# Where a shortened reply is aimed, continuation included. Below the hard limit
# rather than at it, so the offer to continue always fits and nothing downstream
# that counts characters slightly differently is left at the edge.
MAX_SAFE_AI_WHATSAPP_REPLY_CHARS: Final = 3_800

# Egyptian Arabic, matching the product's main market and the register the
# classification prompt already uses. Offered, never assumed: the customer asks.
ARABIC_CONTINUATION: Final = "لو حابب أكمل لك باقي التفاصيل قولي."
ENGLISH_CONTINUATION: Final = "Let me know if you would like me to continue with the rest."

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


def prepare_channel_reply(text: str) -> ChannelReply:
    """Return one WhatsApp-deliverable reply for `text`.

    A reply that already fits is sent exactly as written (trimmed of surrounding
    whitespace). Only a reply over the hard limit is shortened, and the result
    is always within it.
    """
    body = text.strip()
    if len(body) <= WHATSAPP_TEXT_MAX_CHARS:
        return ChannelReply(text=body, truncated=False, original_length=len(body))

    continuation = _continuation(body)
    budget = MAX_SAFE_AI_WHATSAPP_REPLY_CHARS - len(continuation) - len("\n\n")
    bounded = f"{_cut(body, budget)}\n\n{continuation}"
    logger.warning(
        "agent.reply_truncated",
        extra={
            "event": "agent.reply_truncated",
            "original_length": len(body),
            "sent_length": len(bounded),
        },
    )
    return ChannelReply(text=bounded, truncated=True, original_length=len(body))


def _continuation(text: str) -> str:
    """The offer to continue, in whichever script most of the reply is written in."""
    sample = text[:MAX_SAFE_AI_WHATSAPP_REPLY_CHARS]
    arabic = sum(1 for character in sample if _is_arabic(character))
    latin = sum(1 for character in sample if character.isascii() and character.isalpha())
    return ARABIC_CONTINUATION if arabic > latin else ENGLISH_CONTINUATION


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
