"""Conversation memory windows.

No database: memory takes messages, so these tests build them in memory and
assert on what an agent would actually see.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.agents.memory import build_window, estimate_tokens
from app.db.models.conversation import Message, MessageDirection, MessageKind, MessageStatus
from app.db.models.media import MediaStatus, MessageMedia

BASE_TIME = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
ARABIC_LETTER = "\u0645"


def _message(
    *,
    direction: MessageDirection = MessageDirection.INBOUND,
    body: str | None = "hello",
    minutes: int = 0,
    status: MessageStatus = MessageStatus.RECEIVED,
    kind: MessageKind = MessageKind.TEXT,
) -> Message:
    return Message(
        id=uuid.uuid4(),
        direction=direction,
        kind=kind,
        status=status,
        body=body,
        created_at=BASE_TIME + timedelta(minutes=minutes),
    )


def _attachment(
    message: Message,
    *,
    transcript: str | None = None,
    status: MediaStatus = MediaStatus.READY,
    is_voice: bool = False,
    error: str | None = None,
) -> dict[uuid.UUID, MessageMedia]:
    """A media row for `message`, and the map `build_window` takes."""
    media = MessageMedia(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        message_id=message.id,
        conversation_id=uuid.uuid4(),
        status=status,
        transcript=transcript,
        is_voice=is_voice,
        last_error=error,
        byte_size=0,
        attempts=0,
    )
    return {message.id: media}


def test_empty_text_costs_nothing() -> None:
    assert estimate_tokens("") == 0


def test_any_text_costs_at_least_one_token() -> None:
    assert estimate_tokens("a") == 1


def test_non_ascii_text_costs_more_than_ascii_of_the_same_length() -> None:
    """Arabic is roughly twice as expensive per character as English.

    A single divisor would under-count the budget for this product's main
    language, which is the whole reason the estimate is split.
    """
    ascii_estimate = estimate_tokens("a" * 40)
    arabic_estimate = estimate_tokens(ARABIC_LETTER * 40)

    assert arabic_estimate > ascii_estimate


def test_turns_are_chronological_whatever_order_they_arrive_in() -> None:
    newest = _message(body="second", minutes=5)
    oldest = _message(body="first", minutes=0)

    window = build_window([newest, oldest], message_limit=10, token_budget=1000)

    assert [turn.text for turn in window.turns] == ["first", "second"]


def test_direction_decides_the_role() -> None:
    window = build_window(
        [
            _message(body="customer", minutes=0),
            _message(
                body="business",
                minutes=1,
                direction=MessageDirection.OUTBOUND,
                status=MessageStatus.SENT,
            ),
        ],
        message_limit=10,
        token_budget=1000,
    )

    assert [turn.role for turn in window.turns] == ["user", "assistant"]


def test_message_limit_keeps_the_newest_and_reports_the_rest() -> None:
    messages = [_message(body=str(index), minutes=index) for index in range(5)]

    window = build_window(messages, message_limit=2, token_budget=1000)

    assert [turn.text for turn in window.turns] == ["3", "4"]
    assert window.dropped == 3


def test_history_stays_contiguous_when_the_budget_runs_out() -> None:
    """Older messages are dropped together, not selectively.

    A gap in the middle would read to the model as the customer changing
    subject, which is worse than a shorter history.
    """
    long_text = "a" * 400
    messages = [
        _message(body="tiny", minutes=0),
        _message(body=long_text, minutes=1),
        _message(body="newest", minutes=2),
    ]

    window = build_window(messages, message_limit=10, token_budget=30)

    assert [turn.text for turn in window.turns] == ["newest"]
    assert window.dropped == 2


def test_the_newest_message_survives_a_budget_too_small_to_hold_it() -> None:
    window = build_window(
        [_message(body="a" * 4000, minutes=0)],
        message_limit=10,
        token_budget=1,
    )

    assert len(window.turns) == 1


def test_failed_outbound_messages_are_not_shown_to_the_model() -> None:
    """The customer never saw them, so the agent must not think it replied."""
    window = build_window(
        [
            _message(body="question", minutes=0),
            _message(
                body="never arrived",
                minutes=1,
                direction=MessageDirection.OUTBOUND,
                status=MessageStatus.FAILED,
            ),
        ],
        message_limit=10,
        token_budget=1000,
    )

    assert [turn.text for turn in window.turns] == ["question"]


def test_media_without_text_becomes_a_readable_placeholder() -> None:
    window = build_window(
        [_message(body=None, kind=MessageKind.IMAGE)],
        message_limit=10,
        token_budget=1000,
    )

    assert window.turns[0].text == "[image]"


def test_a_window_with_nothing_usable_is_empty() -> None:
    window = build_window([], message_limit=10, token_budget=1000)

    assert window.is_empty
    assert window.estimated_tokens == 0


def test_an_image_reads_as_its_description_not_as_a_placeholder() -> None:
    """The whole point of the phase: an agent must not be shown "[image]"."""
    message = _message(body=None, kind=MessageKind.IMAGE)
    window = build_window(
        [message],
        message_limit=10,
        token_budget=1000,
        media=_attachment(message, transcript="A blue sofa, price tag 4,500 EGP."),
    )

    assert window.turns[0].text == "[image] A blue sofa, price tag 4,500 EGP."


def test_a_caption_and_a_description_stay_distinguishable() -> None:
    """The customer's words are theirs; the description is a machine's.

    They are rendered on separate lines with the machine half labelled, so the
    model is never in a position to quote a transcription back as something the
    customer said.
    """
    message = _message(body="how much is this one?", kind=MessageKind.IMAGE)
    window = build_window(
        [message],
        message_limit=10,
        token_budget=1000,
        media=_attachment(message, transcript="A blue sofa."),
    )

    text = window.turns[0].text
    assert text.startswith("how much is this one?")
    assert "[image] A blue sofa." in text


def test_a_voice_note_says_it_was_transcribed() -> None:
    message = _message(body=None, kind=MessageKind.AUDIO)
    window = build_window(
        [message],
        message_limit=10,
        token_budget=1000,
        media=_attachment(message, transcript="ممكن اعرف السعر؟", is_voice=True),
    )

    assert window.turns[0].text == "[voice note, transcribed] ممكن اعرف السعر؟"


def test_an_unreadable_file_still_produces_a_turn() -> None:
    """Silence would let the agent answer as though nothing had been sent.

    "The customer sent something I could not open" is a far better turn than
    pretending there was no attachment at all.
    """
    message = _message(body=None, kind=MessageKind.DOCUMENT)
    window = build_window(
        [message],
        message_limit=10,
        token_budget=1000,
        media=_attachment(
            message,
            status=MediaStatus.SKIPPED,
            error="This file is larger than the 25 MB limit.",
        ),
    )

    assert "unreadable" in window.turns[0].text
    assert "25 MB" in window.turns[0].text


def test_a_file_still_being_read_says_so() -> None:
    message = _message(body=None, kind=MessageKind.IMAGE)
    window = build_window(
        [message],
        message_limit=10,
        token_budget=1000,
        media=_attachment(message, status=MediaStatus.PENDING),
    )

    assert window.turns[0].text == "[image, not yet read]"


def test_a_message_with_no_attachment_is_unaffected() -> None:
    """Every existing text message must render exactly as it did before."""
    message = _message(body="hello")
    window = build_window([message], message_limit=10, token_budget=1000, media={})

    assert window.turns[0].text == "hello"


def test_media_is_optional() -> None:
    """Callers that never had attachments do not have to pass an empty map."""
    message = _message(body="hello")
    assert build_window([message], message_limit=10, token_budget=1000).turns[0].text == "hello"


def test_a_description_counts_against_the_token_budget() -> None:
    """Otherwise a long transcript would silently blow the context window."""
    message = _message(body=None, kind=MessageKind.IMAGE)
    window = build_window(
        [message],
        message_limit=10,
        token_budget=1000,
        media=_attachment(message, transcript="a" * 400),
    )

    assert window.estimated_tokens > 50
