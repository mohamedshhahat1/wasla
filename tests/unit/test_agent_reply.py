"""An agent's reply always fits WhatsApp, as one message (AI-05).

No database and no provider: what is asserted is the text that would be sent.
"""

from __future__ import annotations

import time
import unicodedata

from app.agents.reply import (
    ARABIC_CONTINUATION,
    ARABIC_FALLBACK,
    ENGLISH_CONTINUATION,
    ENGLISH_FALLBACK,
    MAX_SAFE_AI_WHATSAPP_REPLY_CHARS,
    fallback_reply,
    prepare_channel_reply,
)
from app.core.config import Settings
from app.db.models.agent import DEFAULT_MAX_OUTPUT_TOKENS
from app.services.messaging_service import WHATSAPP_TEXT_MAX_CHARS

PARAGRAPH = "Our finishing packages cover walls, floors and ceilings in detail. " * 8


def _paragraphs(count: int) -> str:
    return "\n\n".join(f"Section {index}. {PARAGRAPH.strip()}" for index in range(count))


def test_a_reply_that_fits_is_sent_exactly_as_written() -> None:
    reply = prepare_channel_reply("  Yes, we are open until nine.  ")

    assert reply.text == "Yes, we are open until nine."
    assert reply.truncated is False


def test_a_reply_at_the_hard_limit_is_not_shortened() -> None:
    text = "a" * WHATSAPP_TEXT_MAX_CHARS

    reply = prepare_channel_reply(text)

    assert reply.text == text
    assert reply.truncated is False


def test_a_long_reply_is_one_message_within_the_limit() -> None:
    text = _paragraphs(20)
    assert len(text) > 6_000  # non-vacuity: this genuinely overflows

    reply = prepare_channel_reply(text)

    assert reply.truncated is True
    assert len(reply.text) <= WHATSAPP_TEXT_MAX_CHARS
    assert len(reply.text) <= MAX_SAFE_AI_WHATSAPP_REPLY_CHARS
    assert reply.text.startswith("Section 0.")
    assert reply.original_length == len(text)


def test_a_long_reply_ends_at_a_paragraph_with_an_offer_to_continue() -> None:
    reply = prepare_channel_reply(_paragraphs(20))

    head, _, offer = reply.text.rpartition("\n\n")
    assert offer == ENGLISH_CONTINUATION
    assert head.endswith(PARAGRAPH.strip()), "the kept text ends on a whole paragraph"


def test_without_paragraphs_the_cut_falls_on_a_sentence() -> None:
    text = "This sentence explains one more detail of the offer. " * 200

    reply = prepare_channel_reply(text)

    head = reply.text.rpartition("\n\n")[0]
    assert head.endswith("offer.")


def test_an_arabic_reply_is_offered_continuation_in_arabic() -> None:
    text = "نقدم باقات تشطيب كاملة للشقق والفلل بأسعار مناسبة. " * 200

    reply = prepare_channel_reply(text)

    assert reply.text.endswith(ARABIC_CONTINUATION)
    assert len(reply.text) <= WHATSAPP_TEXT_MAX_CHARS


def test_a_url_is_not_cut_in_half() -> None:
    url = "https://example.com/brochures/finishing-packages-2026.pdf"
    words = ("word " * 740) + url + " " + ("tail " * 400)

    reply = prepare_channel_reply(words)

    assert len(reply.text) <= WHATSAPP_TEXT_MAX_CHARS
    assert "https://" not in reply.text or url in reply.text


def test_text_with_no_break_is_cut_hard_but_still_fits() -> None:
    reply = prepare_channel_reply("x" * 9_000)

    assert len(reply.text) <= WHATSAPP_TEXT_MAX_CHARS
    assert reply.text.endswith(ENGLISH_CONTINUATION)


def test_a_hard_cut_never_splits_a_character_from_its_marks() -> None:
    """A combining mark may end the kept text only together with its base.

    What must not happen is the opposite: a base kept and its mark cut away, or
    a joiner left dangling at the end of the message.
    """
    text = "e\u0301" * 3_000 + "\U0001f468\u200d\U0001f469" * 1_000

    reply = prepare_channel_reply(text)
    head = reply.text.rpartition("\n\n")[0]

    assert head, "something was kept"
    assert text.startswith(head)
    following = text[len(head)]
    assert unicodedata.combining(following) == 0, "the next character starts a new cluster"
    assert head[-1] != "\u200d"


def test_a_two_million_character_reply_is_bounded_quickly() -> None:
    text = "word " * 400_000
    started = time.perf_counter()

    reply = prepare_channel_reply(text)

    assert len(reply.text) <= WHATSAPP_TEXT_MAX_CHARS
    assert time.perf_counter() - started < 1.0


def test_the_same_reply_is_always_shortened_the_same_way() -> None:
    text = _paragraphs(30)

    assert prepare_channel_reply(text) == prepare_channel_reply(text)


def test_the_backfilled_agent_ceiling_matches_the_deployment_default() -> None:
    """Migration 0060 and the model default restate a configured number; pin it."""
    assert Settings.model_fields["openai_max_output_tokens"].default == DEFAULT_MAX_OUTPUT_TOKENS


def test_a_customer_writing_arabic_is_told_in_arabic_that_a_colleague_will_follow_up() -> None:
    assert fallback_reply("عايز اعرف الأسعار لو سمحت") == ARABIC_FALLBACK


def test_a_customer_writing_english_is_told_in_english() -> None:
    assert fallback_reply("Do you finish apartments?") == ENGLISH_FALLBACK


def test_a_mixed_message_follows_the_script_most_of_it_is_in() -> None:
    assert fallback_reply("عايز اعرف سعر تشطيب الشقة 150 متر please") == ARABIC_FALLBACK


def test_a_message_with_no_words_is_answered_in_english() -> None:
    assert fallback_reply("") == ENGLISH_FALLBACK
    assert len(ARABIC_FALLBACK) < 200
    assert len(ENGLISH_FALLBACK) < 200
