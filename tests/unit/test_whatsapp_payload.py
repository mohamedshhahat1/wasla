"""Webhook payload parsing.

The parser must never raise: Meta adds fields and message types continuously,
and a webhook that fails on the unfamiliar drops legitimate traffic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.integrations.whatsapp.payload import parse_webhook

PHONE_NUMBER_ID = "109876543210"


def _envelope(value):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }


def _value(**extra):
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "+201000000", "phone_number_id": PHONE_NUMBER_ID},
        **extra,
    }


def test_a_text_message_is_parsed():
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


def test_a_non_text_message_is_kept_without_text():
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


def test_statuses_for_one_message_get_distinct_event_ids():
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


def test_messages_and_statuses_arrive_together():
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


def test_an_entry_without_a_phone_number_id_is_counted_not_dropped_silently():
    payload = _envelope({"messaging_product": "whatsapp", "messages": [{"id": "x"}]})

    envelope = parse_webhook(payload)

    assert envelope.is_empty
    assert envelope.ignored == 1


def test_entries_missing_required_identifiers_are_counted():
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
def test_junk_never_raises(payload):
    envelope = parse_webhook(payload)

    assert envelope.is_empty


def test_an_unparseable_timestamp_does_not_lose_the_message():
    payload = _envelope(
        _value(messages=[{"from": "2012", "id": "wamid.x", "type": "text", "timestamp": "soon"}])
    )

    envelope = parse_webhook(payload)

    assert len(envelope.messages) == 1
    assert envelope.messages[0].timestamp is None


def test_several_entries_are_flattened():
    payload = {
        "entry": [
            {"changes": [{"value": _value(messages=[{"from": "1", "id": "a", "type": "text"}])}]},
            {"changes": [{"value": _value(messages=[{"from": "2", "id": "b", "type": "text"}])}]},
        ]
    }

    envelope = parse_webhook(payload)

    assert [message.event_id for message in envelope.messages] == ["a", "b"]
