"""Telling a customer they are talking to an automated assistant (OMNI-041, ADR-127).

Meta's Messenger Platform and Instagram Messaging policy: "Automated chat
experiences must disclose that a person is interacting with an automated
service" - at the start of a conversation, after significant lapses of time, and
when a conversation moves from a person back to automation (read 2026-10-02; the
final audit's F4). Wasla's product is an AI employee, so on those channels this
is an obligation on every AI conversation, and a stateful one: a static sentence
in the agent's prompt cannot know whether it has already been said.

The rules, decided for this remediation (section 8 of the brief):

- **Required where the channel's policy says so** (`disclosure_required`):
  Messenger and Instagram. Not WhatsApp, whose capability flag is off.
- **Due** on the first AI reply of a conversation, on the first after a gap
  (`AUTOMATION_DISCLOSURE_GAP_HOURS`, default 24), and on the first after a
  colleague hands the conversation back to the AI.
- **Part of the reply**, prepended - not a separate message - and counted toward
  the channel's limit in the channel's own unit; the reply is bounded so the two
  together always fit, without splitting a character.
- **Recorded only once delivered**: `automation_disclosed_at` is written when the
  send reaches `SENT`, never when it is staged, so an undelivered reply leaves
  the next one still owing the disclosure.
- **Worded per language**, English or Arabic by the reply's script. A workspace
  may override the wording; it cannot switch the obligation off.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Final

from app.agents.reply import is_mostly_arabic, prepare_channel_reply
from app.channels.policy import ChannelCapabilities, text_length

DEFAULT_DISCLOSURES: Final[Mapping[str, str]] = {
    "en": "You're chatting with an automated assistant. You can ask for a person at any time.",
    "ar": "أنت تتحدث مع مساعد آلي، ويمكنك طلب التحدث مع شخص في أي وقت.",
}
#: Between the disclosure and the reply it opens.
SEPARATOR: Final = "\n\n"
#: The longest wording a workspace may set, so the reply keeps its room.
MAX_DISCLOSURE_LENGTH: Final = 200
DISCLOSURE_LANGUAGES: Final = frozenset(DEFAULT_DISCLOSURES)


def disclosure_due(
    *,
    disclosed_at: datetime | None,
    resumed_at: datetime | None,
    now: datetime,
    gap: timedelta,
) -> bool:
    """Whether the next AI reply on this conversation must disclose automation."""
    if disclosed_at is None:
        return True
    if now - disclosed_at > gap:
        return True
    # Handed back to the AI after the last disclosure: the customer may have
    # been talking to a person since.
    return resumed_at is not None and resumed_at > disclosed_at


def disclosure_for(reply: str, overrides: Mapping[str, str] | None = None) -> str:
    """The disclosure in the reply's language, the workspace's wording if it set one."""
    language = "ar" if is_mostly_arabic(reply) else "en"
    custom = (overrides or {}).get(language)
    return custom if custom else DEFAULT_DISCLOSURES[language]


def compose(reply: str, disclosure: str, capabilities: ChannelCapabilities) -> str:
    """The disclosure, then the reply bounded so the two never pass the channel's limit.

    The room the disclosure takes is measured in the channel's own unit and
    taken from both the hard limit and the reply budget before the reply is
    bounded, so the reply is shortened - at a sentence, with an offer to
    continue - rather than the message refused, and no character is split.
    """
    prefix = f"{disclosure}{SEPARATOR}"
    used = text_length(prefix, capabilities.text_unit)
    room = replace(
        capabilities,
        text_limit=max(1, capabilities.text_limit - used),
        reply_budget=max(1, min(capabilities.reply_budget, capabilities.text_limit) - used),
    )
    return f"{prefix}{prepare_channel_reply(reply, room).text}"


__all__ = [
    "DEFAULT_DISCLOSURES",
    "DISCLOSURE_LANGUAGES",
    "MAX_DISCLOSURE_LENGTH",
    "SEPARATOR",
    "compose",
    "disclosure_due",
    "disclosure_for",
]
