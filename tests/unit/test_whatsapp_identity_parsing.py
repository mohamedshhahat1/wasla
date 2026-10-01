"""WhatsApp senders by business-scoped id, and payloads not WhatsApp's (OMNI-002, OMNI-010).

The audit's parser probes, kept as regressions. At the audit's HEAD:

- **P2** - a username sender (`from_user_id`, no `from`, no `wa_id`) parsed to
  `messages: 0, ignored: 1`: the endpoint answered 200, nothing was stored, and
  the customer's message was lost for good;
- **P3** - a message carrying both identifiers kept the phone and dropped the
  business-scoped id;
- **P5 / P6** - a Messenger or Instagram delivery parsed to an empty envelope
  with `ignored: 0`, indistinguishable from a quiet success.

Provider shapes follow Meta's business-scoped user id documentation, checked
2026-09-29. Every identifier here is synthetic.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.channels.inbound import MESSAGE_LOSS_REASONS, RefusalReason
from app.integrations.whatsapp.payload import MAX_PHONE_LENGTH, parse_webhook

PHONE_NUMBER_ID = "100000000000001"
# The documented maximum: a country code, a dot and 128 characters.
LONGEST_BSUID = "EG." + "7" * 128
PARENT_BSUID = "EG.ENT.11815799212886844830"


def _delivery(value: dict[str, Any], *, field: str = "messages") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "200000000000002", "changes": [{"field": field, "value": value}]}],
    }


def _value(**extra: Any) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": PHONE_NUMBER_ID},
        **extra,
    }


def test_p2_a_username_sender_is_parsed_not_dropped() -> None:
    """The audit's P2 input, verbatim in shape: no `from`, no `wa_id`."""
    payload = _delivery(
        _value(
            contacts=[{"profile": {"name": "Synthetic"}, "user_id": LONGEST_BSUID}],
            messages=[
                {
                    "from_user_id": LONGEST_BSUID,
                    "id": "wamid.SYNTH2",
                    "timestamp": "1790000001",
                    "type": "text",
                    "text": {"body": "I want a quote"},
                }
            ],
        )
    )

    envelope = parse_webhook(payload)

    assert envelope.ignored == 0
    assert envelope.refused == {}
    (message,) = envelope.messages
    assert message.event_id == "wamid.SYNTH2"
    assert message.from_number is None
    # 131 characters, the documented maximum, carried whole.
    assert message.from_user_id == LONGEST_BSUID
    assert len(message.from_user_id) == 131
    assert message.text == "I want a quote"
    # The name comes from the contacts block, keyed by the id it names.
    assert message.profile_name == "Synthetic"
    # The stored evidence is the message object, which carries the id.
    assert message.raw["from_user_id"] == LONGEST_BSUID


def test_p3_a_phone_and_a_business_scoped_id_arrive_together() -> None:
    """Meta asserting, in one signed payload, that both name one person."""
    payload = _delivery(
        _value(
            contacts=[
                {"profile": {"name": "Paired"}, "wa_id": "15551230000", "user_id": "EG.1234"}
            ],
            messages=[
                {
                    "from": "15551230000",
                    "from_user_id": "EG.1234",
                    "from_parent_user_id": PARENT_BSUID,
                    "id": "wamid.SYNTH3",
                    "timestamp": "1790000001",
                    "type": "text",
                    "text": {"body": "hi"},
                    "context": {"id": "wamid.earlier"},
                }
            ],
        )
    )

    (message,) = parse_webhook(payload).messages

    assert message.from_number == "15551230000"
    assert message.from_user_id == "EG.1234"
    # Carried, never used to link anything (ADR-118).
    assert message.from_parent_user_id == PARENT_BSUID
    assert message.context_id == "wamid.earlier"
    assert message.profile_name == "Paired"


def test_a_phone_sender_parses_exactly_as_it_always_did() -> None:
    payload = _delivery(
        _value(
            contacts=[{"profile": {"name": "Old"}, "wa_id": "201234567890"}],
            messages=[
                {"from": "201234567890", "id": "wamid.one", "type": "text", "text": {"body": "x"}}
            ],
        )
    )

    (message,) = parse_webhook(payload).messages

    assert message.from_number == "201234567890"
    assert message.from_user_id is None
    assert message.profile_name == "Old"


def test_a_message_with_no_sender_at_all_is_refused_by_reason() -> None:
    payload = _delivery(_value(messages=[{"id": "wamid.nobody", "type": "text"}]))

    envelope = parse_webhook(payload)

    assert envelope.messages == ()
    assert envelope.refused == {RefusalReason.MISSING_SENDER: 1}
    assert envelope.ignored == 1


def test_an_identifier_past_any_documented_form_is_refused_and_counted() -> None:
    """Not stored - it is not an id Meta issued - but never silent either."""
    payload = _delivery(
        _value(
            messages=[
                {"from_user_id": "EG." + "7" * 400, "id": "wamid.long", "type": "text"},
                {"from": "2" * (MAX_PHONE_LENGTH + 1), "id": "wamid.longphone", "type": "text"},
            ]
        )
    )

    envelope = parse_webhook(payload)

    assert envelope.messages == ()
    assert envelope.refused == {RefusalReason.IDENTIFIER_TOO_LONG: 2}


def test_one_unusable_identifier_does_not_lose_a_message_with_another() -> None:
    payload = _delivery(
        _value(
            messages=[
                {
                    "from": "2" * (MAX_PHONE_LENGTH + 1),
                    "from_user_id": "EG.5678",
                    "id": "wamid.half",
                    "type": "text",
                }
            ]
        )
    )

    envelope = parse_webhook(payload)

    (message,) = envelope.messages
    assert message.from_number is None
    assert message.from_user_id == "EG.5678"
    assert envelope.ignored == 0


def test_a_status_keeps_the_recipient_business_scoped_id() -> None:
    payload = _delivery(
        _value(
            statuses=[
                {
                    "id": "wamid.out",
                    "status": "read",
                    "recipient_user_id": "EG.1234",
                    "recipient_parent_user_id": PARENT_BSUID,
                }
            ]
        )
    )

    (status,) = parse_webhook(payload).statuses

    assert status.recipient is None
    assert status.recipient_user_id == "EG.1234"
    assert status.event_id == "wamid.out:read"


@pytest.mark.parametrize(
    ("obj", "entry"),
    [
        # P5: a Messenger delivery posted to the WhatsApp endpoint.
        (
            "page",
            {
                "id": "PAGE1",
                "time": 1,
                "messaging": [
                    {
                        "sender": {"id": "PSID1"},
                        "recipient": {"id": "PAGE1"},
                        "message": {"mid": "m_1", "text": "hi"},
                    }
                ],
            },
        ),
        # P6: an Instagram one.
        (
            "instagram",
            {
                "id": "IG1",
                "time": 1,
                "messaging": [
                    {
                        "sender": {"id": "IGSID1"},
                        "recipient": {"id": "IG1"},
                        "message": {"mid": "m_2", "text": "hi"},
                    }
                ],
            },
        ),
    ],
)
def test_p5_p6_another_products_delivery_is_refused_and_counted(
    obj: str, entry: dict[str, Any]
) -> None:
    envelope = parse_webhook({"object": obj, "entry": [entry]})

    assert envelope.is_empty
    assert envelope.refused == {RefusalReason.FOREIGN_OBJECT: 1}
    # A foreign delivery can be a customer's message lost to a misrouted
    # subscription - it is what the loss alert fires on.
    assert RefusalReason.FOREIGN_OBJECT in MESSAGE_LOSS_REASONS
    assert envelope.ignored == 1


@pytest.mark.parametrize(
    "field",
    ["smb_message_echoes", "history", "smb_app_state_sync", "message_template_status_update"],
)
def test_a_change_this_adapter_does_not_process_is_refused_by_rule(field: str) -> None:
    """Coexistence fields used to be ignored by key-name coincidence (OMNI-010).

    An `smb_message_echoes` value carries `messages` - the business's own - and
    must never be read as a customer's.
    """
    payload = _delivery(
        _value(
            message_echoes=[{"from": "15550000000", "to": "201234567890", "id": "wamid.echo"}],
            messages=[{"from": "201234567890", "id": "wamid.looks.inbound", "type": "text"}],
        ),
        field=field,
    )

    envelope = parse_webhook(payload)

    assert envelope.is_empty
    assert envelope.refused == {RefusalReason.UNSUPPORTED_FIELD: 1}
    assert RefusalReason.UNSUPPORTED_FIELD not in MESSAGE_LOSS_REASONS


def test_a_payload_with_no_object_is_malformed_not_an_empty_success() -> None:
    envelope = parse_webhook({"entry": []})

    assert envelope.refused == {RefusalReason.MALFORMED: 1}
    assert envelope.ignored == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages"}]}]},
        {"object": "whatsapp_business_account", "entry": [{"changes": [None, 7, "x"]}]},
        {"object": 7, "entry": "nonsense"},
        {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": ["messages"]}]}]},
        _delivery(_value(messages=[None, 3, {"id": 7, "from": 9}], statuses=[None])),
        _delivery(_value(messages=[{"id": "x", "from_user_id": {"nested": True}}])),
    ],
)
def test_the_parser_still_never_raises(payload: dict[str, Any]) -> None:
    envelope = parse_webhook(payload)

    assert envelope.is_empty
    assert envelope.ignored == sum(envelope.refused.values())
