"""Conversation memory for one agent turn.

An agent sees recent history, not the whole conversation. Two limits apply
together because either alone is insufficient: a message count keeps the prompt
predictable, and a token budget stops one very long message from filling the
context anyway.

Token counts are estimated from character length. There is no tokeniser in the
dependency set, and adding one to trim a prompt would be a large dependency for
an approximation. Non-ASCII text is estimated at half the characters per token,
because Arabic costs roughly twice as many tokens per character as English and a
single divisor would under-count the budget for this product's main language.

The result is deliberately a window rather than a bare list, so a worker can log
what it spent and how much history it left behind.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil
from typing import Final

from app.db.models.conversation import (
    Message,
    MessageDirection,
    MessageStatus,
)
from app.db.models.media import UNRESOLVED_MEDIA_STATUSES, MessageMedia
from app.integrations.openai.types import Turn

CHARACTERS_PER_TOKEN: Final = 4.0
NON_ASCII_CHARACTERS_PER_TOKEN: Final = 2.0


def estimate_tokens(text: str) -> int:
    """Approximate the token cost of a string.

    Deliberately an estimate, and deliberately pessimistic on non-Latin scripts.
    It is used to decide how much history fits, never to report usage: real
    counts come back from the provider.
    """
    if not text:
        return 0

    ascii_characters = sum(1 for character in text if character.isascii())
    other_characters = len(text) - ascii_characters
    estimate = (
        ascii_characters / CHARACTERS_PER_TOKEN + other_characters / NON_ASCII_CHARACTERS_PER_TOKEN
    )
    return max(1, ceil(estimate))


@dataclass(frozen=True, slots=True)
class MemoryWindow:
    """The history handed to one inference, and what it cost to assemble."""

    turns: tuple[Turn, ...]
    estimated_tokens: int
    dropped: int

    @property
    def is_empty(self) -> bool:
        return not self.turns


def build_window(
    messages: Sequence[Message],
    *,
    message_limit: int,
    token_budget: int,
    media: Mapping[uuid.UUID, MessageMedia] | None = None,
) -> MemoryWindow:
    """Select the most recent history that fits both limits.

    Messages may arrive in any order: they are sorted here rather than trusting
    a caller's query, because reversed history reads as a different conversation
    and the mistake is invisible in the output.

    `media` maps message id to the file attached to it, and is passed in rather
    than reached through a relationship on purpose. Lazy loading inside an async
    session raises rather than working, and even where it worked it would issue
    one query per message in the window; the caller fetches them in one.
    """
    ordered = sorted(messages, key=lambda message: message.created_at)
    attachments = media or {}
    turns: list[Turn] = []
    spent = 0
    dropped = 0

    for index, message in enumerate(reversed(ordered)):
        turn = _turn(message, attachments)
        if turn is None:
            continue

        cost = estimate_tokens(turn.text)
        over_count = len(turns) >= message_limit
        # The newest message is always included, even if it alone exceeds the
        # budget: an agent with no context cannot answer at all.
        over_budget = bool(turns) and spent + cost > token_budget
        if over_count or over_budget:
            # Everything older is dropped together, so history stays
            # contiguous. A gap in the middle reads as a change of subject.
            dropped = len(ordered) - index
            break

        turns.append(turn)
        spent += cost

    turns.reverse()
    return MemoryWindow(turns=tuple(turns), estimated_tokens=spent, dropped=dropped)


def _turn(
    message: Message,
    media: Mapping[uuid.UUID, MessageMedia],
) -> Turn | None:
    """Render one stored message as the model should see it, or skip it."""
    text = _text(message, media.get(message.id))
    if message.direction is MessageDirection.OUTBOUND:
        if message.status is MessageStatus.FAILED:
            # Never delivered, so the customer never saw it. Including it would
            # convince the model it had already answered.
            return None
        return Turn(role="assistant", text=text)
    return Turn(role="user", text=text)


def _text(message: Message, media: MessageMedia | None) -> str:
    """What the model should read for this message.

    A media message contributes up to two things, and they stay apart in the
    rendering exactly as they do in the database. The caption is the customer's
    own words and reads as ordinary text. What was found inside the file is
    labelled, so the model is never in a position to quote a machine
    transcription back to the customer as something they said.

    A file that could not be read still produces a line. Silence would let the
    agent answer as though nothing had been sent, and "the customer sent a
    photograph I could not open" is a far better turn than pretending there was
    no photograph.
    """
    caption = message.body or ""
    described = _described(message, media)

    if caption and described:
        return f"{caption}\n{described}"
    return described or caption or f"[{message.kind.value}]"


def _described(message: Message, media: MessageMedia | None) -> str:
    """The attached file, rendered for the model."""
    if media is None:
        return ""

    label = message.kind.value
    if media.transcript:
        if media.is_voice:
            return f"[voice note, transcribed] {media.transcript}"
        return f"[{label}] {media.transcript}"

    if media.status in UNRESOLVED_MEDIA_STATUSES:
        # Not normally reached: an agent job is not enqueued while anything on
        # the conversation is unresolved. An older message in the window can
        # still be in this state, and saying so beats pretending the file is
        # not there.
        return f"[{label}, not yet read]"

    reason = media.last_error or "it could not be read"
    return f"[{label}, unreadable: {reason}]"
