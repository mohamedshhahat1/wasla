"""Webhook payload parsing.

The parser must never raise: Meta adds fields and message types continuously,
and a webhook that fails on the unfamiliar drops legitimate traffic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.integrations.whatsapp.payload import parse_webhook

PHONE_NUMBER_ID = "109876543210"


def _envelope(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }


def _value(**extra: Any) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "+201000000", "phone_number_id": PHONE_NUMBER_ID},
        **extra,
    }


def test_a_text_message_is_parsed() -> None:
    payload = _envelope(
        _value(
            messages=[
                {
                    "from": "201234567890",
                    "id": "wamid.one",
                    "timestamp": "1767225600",
                    "type": "text",
                    "text": {"body": "hello"},
                }
            ]
        )
    )

    envelope = parse_webhook(payload)

    assert len(envelope.messages) == 1
    message = envelope.messages[0]
    assert message.event_id == "wamid.one"
    assert message.phone_number_id == PHONE_NUMBER_ID
    assert message.from_number == "201234567890"
    assert message.text == "hello"
    assert message.timestamp == datetime(2026, 1, 1, tzinfo=UTC)
    # The whole message is kept, not just the fields we read.
    assert message.raw["type"] == "text"


def test_a_non_text_message_is_kept_without_text() -> None:
    payload = _envelope(
        _value(
            messages=[
                {
                    "from": "201234567890",
                    "id": "wamid.image",
                    "type": "image",
                    "image": {"id": "media-1", "mime_type": "image/jpeg"},
                }
            ]
        )
    )

    envelope = parse_webhook(payload)

    assert envelope.messages[0].message_type == "image"
    assert envelope.messages[0].text is None
    assert envelope.messages[0].raw["image"]["id"] == "media-1"


def test_statuses_for_one_message_get_distinct_event_ids() -> None:
    payload = _envelope(
        _value(
            statuses=[
                {"id": "wamid.sent", "status": "sent", "recipient_id": "2012"},
                {"id": "wamid.sent", "status": "delivered", "recipient_id": "2012"},
                {"id": "wamid.sent", "status": "read", "recipient_id": "2012"},
            ]
        )
    )

    envelope = parse_webhook(payload)

    # Keyed on the message id alone, two of these three would be lost as
    # duplicates of the first.
    assert [status.event_id for status in envelope.statuses] == [
        "wamid.sent:sent",
        "wamid.sent:delivered",
        "wamid.sent:read",
    ]
    assert {status.message_id for status in envelope.statuses} == {"wamid.sent"}


def test_messages_and_statuses_arrive_together() -> None:
    payload = _envelope(
        _value(
            messages=[{"from": "2012", "id": "wamid.in", "type": "text", "text": {"body": "hi"}}],
            statuses=[{"id": "wamid.out", "status": "delivered"}],
        )
    )

    envelope = parse_webhook(payload)

    assert len(envelope.messages) == 1
    assert len(envelope.statuses) == 1
    assert envelope.ignored == 0
    assert not envelope.is_empty


def test_an_entry_without_a_phone_number_id_is_counted_not_dropped_silently() -> None:
    payload = _envelope({"messaging_product": "whatsapp", "messages": [{"id": "x"}]})

    envelope = parse_webhook(payload)

    assert envelope.is_empty
    assert envelope.ignored == 1


def test_entries_missing_required_identifiers_are_counted() -> None:
    payload = _envelope(
        _value(
            messages=[{"id": "wamid.no.sender"}, {"from": "2012"}],
            statuses=[{"status": "delivered"}],
        )
    )

    envelope = parse_webhook(payload)

    assert envelope.is_empty
    assert envelope.ignored == 3


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"entry": None},
        {"entry": "nonsense"},
        {"entry": [None]},
        {"entry": [{"changes": "nonsense"}]},
        {"entry": [{"changes": [{"value": None}]}]},
        {"entry": [{"changes": [{"value": {"metadata": "nonsense"}}]}]},
    ],
)
def test_junk_never_raises(payload: dict[str, Any]) -> None:
    envelope = parse_webhook(payload)

    assert envelope.is_empty


def test_an_unparseable_timestamp_does_not_lose_the_message() -> None:
    payload = _envelope(
        _value(messages=[{"from": "2012", "id": "wamid.x", "type": "text", "timestamp": "soon"}])
    )

    envelope = parse_webhook(payload)

    assert len(envelope.messages) == 1
    assert envelope.messages[0].timestamp is None


def test_several_entries_are_flattened() -> None:
    payload = {
        "entry": [
            {"changes": [{"value": _value(messages=[{"from": "1", "id": "a", "type": "text"}])}]},
            {"changes": [{"value": _value(messages=[{"from": "2", "id": "b", "type": "text"}])}]},
        ]
    }

    envelope = parse_webhook(payload)

    assert [message.event_id for message in envelope.messages] == ["a", "b"]


def test_an_image_carries_its_media_descriptor() -> None:
    payload = _envelope(
        _value(
            messages=[
                {
                    "from": "201234567890",
                    "id": "wamid.image",
                    "timestamp": "1767225600",
                    "type": "image",
                    "image": {
                        "id": "media-1",
                        "mime_type": "image/jpeg",
                        "sha256": "abc123",
                    },
                }
            ]
        )
    )

    media = parse_webhook(payload).messages[0].media

    assert media is not None
    assert media.media_id == "media-1"
    assert media.kind == "image"
    assert media.mime_type == "image/jpeg"
    assert media.sha256 == "abc123"
    assert media.is_voice is False


def test_a_caption_is_the_message_text() -> None:
    """The customer's own sentence, and often the whole question.

    Dropping it would leave the agent with a photograph and no idea what was
    being asked about it.
    """
    payload = _envelope(
        _value(
            messages=[
                {
                    "from": "201234567890",
                    "id": "wamid.captioned",
                    "type": "image",
                    "image": {"id": "media-2", "caption": "how much is this one?"},
                }
            ]
        )
    )

    message = parse_webhook(payload).messages[0]

    assert message.text == "how much is this one?"
    assert message.media is not None


def test_a_document_keeps_the_filename_it_arrived_with() -> None:
    payload = _envelope(
        _value(
            messages=[
                {
                    "from": "201234567890",
                    "id": "wamid.doc",
                    "type": "document",
                    "document": {
                        "id": "media-3",
                        "mime_type": "application/pdf",
                        "filename": "quote.pdf",
                    },
                }
            ]
        )
    )

    media = parse_webhook(payload).messages[0].media

    assert media is not None
    assert media.filename == "quote.pdf"


def test_a_hostile_filename_is_passed_through_untouched() -> None:
    """The parser does not sanitise it, because the parser is not what protects.

    Storage derives its own key from a generated identifier and never consults
    this value, so the safe thing is to record exactly what arrived rather than
    a cleaned-up version that hides what the sender tried.
    """
    payload = _envelope(
        _value(
            messages=[
                {
                    "from": "201234567890",
                    "id": "wamid.evil",
                    "type": "document",
                    "document": {"id": "media-4", "filename": "../../etc/passwd"},
                }
            ]
        )
    )

    media = parse_webhook(payload).messages[0].media

    assert media is not None
    assert media.filename == "../../etc/passwd"


def test_a_voice_note_is_distinguished_from_an_audio_file() -> None:
    """Both are transcribed; only one is somebody speaking to the business."""
    payload = _envelope(
        _value(
            messages=[
                {
                    "from": "201234567890",
                    "id": "wamid.voice",
                    "type": "audio",
                    "audio": {
                        "id": "media-5",
                        "mime_type": "audio/ogg; codecs=opus",
                        "voice": True,
                    },
                },
                {
                    "from": "201234567890",
                    "id": "wamid.audio",
                    "type": "audio",
                    "audio": {"id": "media-6", "mime_type": "audio/mpeg"},
                },
            ]
        )
    )

    voice, attached = parse_webhook(payload).messages

    assert voice.media is not None
    assert voice.media.is_voice is True
    # Codec parameters are stripped: they matter to a decoder, not to us, and
    # keeping them would make two identical types compare unequal.
    assert voice.media.mime_type == "audio/ogg"
    assert attached.media is not None
    assert attached.media.is_voice is False


def test_a_media_message_without_an_id_still_parses() -> None:
    """There is nothing to download, but the message itself is worth storing."""
    payload = _envelope(
        _value(
            messages=[
                {
                    "from": "201234567890",
                    "id": "wamid.idless",
                    "type": "image",
                    "image": {"caption": "look"},
                }
            ]
        )
    )

    envelope = parse_webhook(payload)

    assert len(envelope.messages) == 1
    assert envelope.messages[0].media is None
    assert envelope.messages[0].text == "look"


def test_a_text_message_carries_no_media() -> None:
    payload = _envelope(
        _value(
            messages=[
                {
                    "from": "201234567890",
                    "id": "wamid.text",
                    "type": "text",
                    "text": {"body": "hello"},
                }
            ]
        )
    )

    assert parse_webhook(payload).messages[0].media is None
