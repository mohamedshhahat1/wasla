"""The Coexistence webhook fields and edit / revoke, as parsed (ADR-204, section 14).

Every malformed shape is a counted refusal or nothing at all - never an
exception, so the webhook answers 200 for whatever it cannot act on.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.channels.inbound import InboundKind
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.integrations.whatsapp.payload import (
    account_update_events,
    coexistence_deliveries,
    parse_webhook,
)

NUMBER = "109876543299"


def _delivery(
    field: str, value: Any, *, entry_id: str = "555000111", time: int = 1764000000
) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": entry_id, "time": time, "changes": [{"field": field, "value": value}]}],
    }


def _value(**extra: Any) -> dict[str, Any]:
    return {"messaging_product": "whatsapp", "metadata": {"phone_number_id": NUMBER}, **extra}


def test_hi_a_history_delivery_is_captured_whole_with_a_stable_digest() -> None:
    value = _value(
        history=[{"metadata": {"phase": 1, "chunk_order": 2, "progress": 40}, "threads": []}]
    )

    first = coexistence_deliveries(_delivery("history", value))
    again = coexistence_deliveries(_delivery("history", dict(value)))

    assert len(first) == 1
    assert first[0].field == "history"
    assert first[0].phone_number_id == NUMBER
    assert first[0].event_id == again[0].event_id
    assert first[0].event_id.startswith("history:")


def test_hi_a_delivery_without_a_number_is_not_captured() -> None:
    assert coexistence_deliveries(_delivery("history", {"history": []})) == []


@pytest.mark.parametrize(
    "value",
    [None, [], "text", {"metadata": "x"}, {"metadata": {"phone_number_id": 5}}],
)
def test_section_14_malformed_coexistence_values_never_raise(value: Any) -> None:
    payload = _delivery("smb_app_state_sync", value)

    assert coexistence_deliveries(payload) == []
    assert parse_webhook(payload).refused == {}


def test_au01_partner_events_name_the_customer_in_waba_info_not_entry_id() -> None:
    payload = _delivery(
        "account_update",
        {
            "event": "PARTNER_REMOVED",
            "waba_info": {"waba_id": "980198427658004"},
            "disconnection_info": {"reason": "PRIMARY_INACTIVITY", "initiated_by": "SYSTEM"},
        },
        entry_id="2949482758682047",
    )

    (event,) = account_update_events(payload)

    assert event.waba_id == "980198427658004"
    assert event.event == "PARTNER_REMOVED"
    assert event.reason == "PRIMARY_INACTIVITY"
    assert event.initiated_by == "SYSTEM"


def test_au01_offboarded_names_the_waba_in_entry_id() -> None:
    (event,) = account_update_events(
        _delivery("account_update", {"event": "ACCOUNT_OFFBOARDED"}, entry_id="102290129340398")
    )

    assert event.waba_id == "102290129340398"
    assert event.reason is None


def test_au_an_update_without_an_event_word_is_nothing() -> None:
    assert account_update_events(_delivery("account_update", {"waba_info": {}})) == []
    assert parse_webhook(_delivery("account_update", {"event": "PARTNER_ADDED"})).refused == {}


def test_er_edit_and_revoke_are_revisions_never_customer_messages() -> None:
    payload = _delivery(
        "messages",
        _value(
            messages=[
                {
                    "from": "201234567890",
                    "id": "wamid.e",
                    "timestamp": "1764000000",
                    "type": "edit",
                    "edit": {
                        "original_message_id": "wamid.o",
                        "message": {"type": "text", "text": {"body": "new words"}},
                    },
                },
                {
                    "from": "201234567890",
                    "id": "wamid.r",
                    "timestamp": "1764000001",
                    "type": "revoke",
                    "revoke": {"original_message_id": "wamid.o"},
                },
                {"from": "201234567890", "id": "wamid.bad", "type": "revoke", "revoke": {}},
            ]
        ),
    )

    envelope = parse_webhook(payload)
    events = WhatsAppAdapter().parse(payload).events

    assert envelope.messages == ()
    edit, revoke = envelope.revisions
    assert (edit.original_message_id, edit.revoked, edit.text) == ("wamid.o", False, "new words")
    assert (revoke.original_message_id, revoke.revoked, revoke.text) == ("wamid.o", True, None)
    assert sum(envelope.refused.values()) == 1
    assert [event.kind for event in events] == [InboundKind.REVISION, InboundKind.REVISION]
    assert events[0].revision_of == "wamid.o"


def test_eh_an_echo_names_the_customer_as_its_sender() -> None:
    payload = _delivery(
        "smb_message_echoes",
        _value(
            message_echoes=[
                {
                    "from": "201000000099",
                    "to": "201234567890",
                    "to_user_id": "EG.13491208655302741918",
                    "id": "wamid.echo",
                    "timestamp": "1764000000",
                    "type": "text",
                    "text": {"body": "From the app"},
                },
                {"from": "201000000099", "id": "wamid.nobody"},
            ]
        ),
    )

    (event,) = WhatsAppAdapter().parse(payload).events

    assert event.kind is InboundKind.ECHO
    assert event.message_id == "wamid.echo"
    assert [identifier.value for identifier in event.sender] == [
        "201234567890",
        "EG.13491208655302741918",
    ]
