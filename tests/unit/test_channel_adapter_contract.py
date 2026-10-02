"""The contract every channel adapter keeps, run once per adapter (OMNI-R10).

Run against WhatsApp and against the synthetic channel in `tests/channel_fakes`,
so a rule proved here is a rule of the seam rather than of one provider - and a
second real adapter inherits the suite by being added to `ADAPTERS`.

- `parse` never raises, whatever arrives: a webhook endpoint that throws on a
  shape it did not expect turns one odd delivery into retries and, eventually,
  a disabled subscription.
- Nothing is dropped silently: what an adapter does not turn into an event is
  counted under a closed reason.
- Every event is labelled with the adapter's own channel.
- An adapter addresses only identities of its own channel, and of kinds it can
  address - the wrong-channel send is refused at the seam (OMNI-004, M6).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from app.channels.adapter import ChannelAdapter, IdentityNotAddressableError
from app.channels.inbound import InboundKind, RefusalReason
from app.db.models.channel import (
    Channel,
    ContactIdentity,
    IdentityKind,
    IdentityScope,
    IdentitySource,
)
from app.db.models.conversation import MessageStatus, ReplyActionSource
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from tests.channel_fakes import SyntheticAdapter, synthetic_payload

# Synthetic identifiers only: nobody's phone number, nobody's account.
PHONE = "201000000007"
BSUID = "EG.0contract0sender"
IGSID = "igsid-0contract0sender"


def _whatsapp_message() -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-contract",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PN-contract"},
                            "contacts": [{"wa_id": PHONE, "user_id": BSUID}],
                            "messages": [
                                {
                                    "id": "wamid.contract",
                                    "from": PHONE,
                                    "from_user_id": BSUID,
                                    "type": "text",
                                    "timestamp": "1790000000",
                                    "text": {"body": "hello"},
                                }
                            ],
                            "statuses": [
                                {
                                    "id": "wamid.out",
                                    "status": "delivered",
                                    "recipient_id": PHONE,
                                    "recipient_user_id": BSUID,
                                    "timestamp": "1790000001",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _synthetic_message() -> dict[str, Any]:
    return synthetic_payload(
        "syn-account",
        {"type": "message", "id": "syn.m.1", "from": IGSID, "at": 1790000000, "text": "hello"},
        {"type": "status", "id": "syn.out.1", "status": "delivered", "at": 1790000001},
    )


@dataclass(frozen=True)
class Case:
    name: str
    build: Callable[[], ChannelAdapter]
    valid: Callable[[], dict[str, Any]]
    #: The foreign payload: the *other* case's delivery.
    foreign: Callable[[], dict[str, Any]]
    own_identity: tuple[IdentityKind, str]
    other_channel: Channel


ADAPTERS = [
    Case(
        name="whatsapp",
        build=WhatsAppAdapter,
        valid=_whatsapp_message,
        foreign=_synthetic_message,
        own_identity=(IdentityKind.BSUID, BSUID),
        other_channel=Channel.INSTAGRAM,
    ),
    Case(
        name="synthetic",
        build=SyntheticAdapter,
        valid=_synthetic_message,
        foreign=_whatsapp_message,
        own_identity=(IdentityKind.IGSID, IGSID),
        other_channel=Channel.WHATSAPP,
    ),
]


@pytest.fixture(params=ADAPTERS, ids=lambda case: case.name)
def case(request: pytest.FixtureRequest) -> Case:
    return request.param  # type: ignore[no-any-return]


def _identity(channel: Channel, kind: IdentityKind, value: str) -> ContactIdentity:
    return ContactIdentity(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        channel=channel,
        kind=kind,
        scope=IdentityScope.WORKSPACE,
        scope_ref="",
        value=value,
        source=IdentitySource.PROVIDER,
    )


HOSTILE: list[Any] = [
    {},
    {"object": None},
    {"object": 7, "entry": "nope"},
    {"object": "whatsapp_business_account", "entry": None},
    {"object": "whatsapp_business_account", "entry": [None, 1, "x", {"changes": None}]},
    {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": ["x"]}]}]},
    {"object": "synthetic", "account": 3, "events": [None]},
    {"object": "synthetic", "account": "a", "events": [{"type": "message"}, {"id": ""}, 5]},
    {"object": "synthetic", "account": "a", "events": "not a list"},
]


@pytest.mark.parametrize("payload", HOSTILE)
def test_parse_never_raises_and_counts_what_it_refuses(case: Case, payload: Any) -> None:
    adapter = case.build()

    delivery = adapter.parse(payload)

    assert all(isinstance(reason, RefusalReason) for reason in delivery.refused)
    assert all(count > 0 for count in delivery.refused.values())
    # Whatever came back is labelled with this adapter's channel.
    assert all(event.channel is adapter.channel for event in delivery.events)


def test_another_products_payload_is_refused_as_foreign(case: Case) -> None:
    delivery = case.build().parse(case.foreign())

    assert delivery.events == ()
    assert set(delivery.refused) == {RefusalReason.FOREIGN_OBJECT}
    assert delivery.message_loss == delivery.refused_total


def test_a_valid_delivery_becomes_a_message_and_a_status(case: Case) -> None:
    adapter = case.build()

    delivery = adapter.parse(case.valid())

    kinds = [event.kind for event in delivery.events]
    assert kinds == [InboundKind.MESSAGE, InboundKind.STATUS]
    message, status = delivery.events
    assert all(event.channel is adapter.channel for event in delivery.events)
    assert message.message_id and message.event_id
    assert message.sender, "a message names who sent it"
    assert {identifier.kind for identifier in message.sender} >= {case.own_identity[0]}
    assert status.status is not None and status.status.status is MessageStatus.DELIVERED
    assert delivery.refused_total == 0


def test_the_policy_is_the_adapters_own_channels(case: Case) -> None:
    adapter = case.build()

    assert adapter.policy.channel is adapter.channel
    for kind in (*adapter.participant_preference, *adapter.anchor_preference):
        assert kind in {member.value for member in IdentityKind}


def test_an_adapter_addresses_only_its_own_channel(case: Case) -> None:
    """M6 at the seam: an identity of another channel is never coerced into an
    address - a WhatsApp phone is not sent to over another channel, and the
    other way round."""
    adapter = case.build()
    kind, value = case.own_identity

    recipient = adapter.address(_identity(adapter.channel, kind, value))
    assert (recipient.kind, recipient.value) == (kind.value, value)

    with pytest.raises(IdentityNotAddressableError):
        adapter.address(_identity(case.other_channel, kind, value))


@pytest.mark.parametrize("kind", [IdentityKind.PSID, IdentityKind.IGSID])
def test_whatsapp_refuses_a_kind_it_cannot_address(kind: IdentityKind) -> None:
    with pytest.raises(IdentityNotAddressableError):
        WhatsAppAdapter().address(_identity(Channel.WHATSAPP, kind, "someone"))


def test_an_echo_is_never_a_customers_message() -> None:
    """The synthetic channel echoes the business's own sends. The adapter says
    so in the event's kind, which is what ingestion reads (OMNI-005)."""
    delivery = SyntheticAdapter().parse(
        synthetic_payload(
            "syn-account",
            {"type": "echo", "id": "syn.m.9", "to": IGSID, "at": 1790000000, "text": "Hi!"},
        )
    )

    (echo,) = delivery.events
    assert echo.kind is InboundKind.ECHO


def _field_values(payload: Mapping[str, Any]) -> list[str]:
    return [
        change.get("field")
        for entry in payload.get("entry", [])
        for change in entry.get("changes", [])
    ]


def test_the_whatsapp_fixture_is_metas_documented_shape() -> None:
    """The discriminators this contract relies on are in the fixture, as Meta sends them."""
    payload = _whatsapp_message()

    assert payload["object"] == "whatsapp_business_account"
    assert _field_values(payload) == ["messages"]


def test_the_providers_timestamp_and_connection_survive_normalisation(case: Case) -> None:
    """What routing and ordering are decided by is carried, not re-derived."""
    adapter = case.build()

    message, status = adapter.parse(case.valid()).events

    expected_key = "PN-contract" if case.name == "whatsapp" else "syn-account"
    assert message.connection_key == status.connection_key == expected_key
    assert message.occurred_at is not None
    assert int(message.occurred_at.timestamp()) == 1790000000
    assert status.occurred_at is not None
    assert int(status.occurred_at.timestamp()) == 1790000001


def test_identifiers_asserted_together_arrive_together() -> None:
    """A phone and a business-scoped id in one message are one sender (ADR-118)."""
    message, _ = WhatsAppAdapter().parse(_whatsapp_message()).events

    assert [(identifier.kind, identifier.value) for identifier in message.sender] == [
        (IdentityKind.PHONE, PHONE),
        (IdentityKind.BSUID, BSUID),
    ]


def test_every_attachment_survives_in_the_providers_order() -> None:
    delivery = SyntheticAdapter().parse(
        synthetic_payload(
            "syn-account",
            {
                "type": "message",
                "id": "syn.m.files",
                "from": IGSID,
                "at": 1790000000,
                "attachments": [
                    {"url": "https://cdn.synthetic.test/1.jpg", "kind": "image"},
                    {"url": "https://cdn.synthetic.test/2.jpg", "kind": "image"},
                    {"url": "https://cdn.synthetic.test/3.mp4", "kind": "video"},
                ],
            },
        )
    )

    (message,) = delivery.events
    assert [attachment.locator for attachment in message.attachments] == [
        "https://cdn.synthetic.test/1.jpg",
        "https://cdn.synthetic.test/2.jpg",
        "https://cdn.synthetic.test/3.mp4",
    ]


def test_a_whatsapp_file_is_a_handle_locator() -> None:
    payload = _whatsapp_message()
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message.update({"type": "image", "image": {"id": "media-handle-1", "mime_type": "image/jpeg"}})

    event, _ = WhatsAppAdapter().parse(payload).events

    (attachment,) = event.attachments
    assert (attachment.locator_kind.value, attachment.locator) == ("handle", "media-handle-1")


@pytest.mark.parametrize("message_type", ["reaction", "order", "system", "something_new"])
def test_a_message_type_nobody_maps_is_kept_not_dropped(message_type: str) -> None:
    """Stored as unsupported - visible, replayable once understood - never refused."""
    payload = _whatsapp_message()
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message["type"] = message_type

    delivery = WhatsAppAdapter().parse(payload)

    event, _ = delivery.events
    assert event.kind is InboundKind.MESSAGE
    assert event.message_kind.value == "unsupported"
    assert delivery.refused_total == 0


# ---------------------------------------------------------- reply actions (OMNI-030)


def _whatsapp_tap(message: dict[str, Any]) -> dict[str, Any]:
    """Meta's messages webhook reference, re-read 2026-10-02: a tap replaces the text."""
    payload = _whatsapp_message()
    original = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    original.pop("text")
    original.update(message)
    return payload


WHATSAPP_TAPS: list[tuple[str, dict[str, Any], ReplyActionSource, str, str]] = [
    (
        "template_quick_reply",
        {
            "type": "button",
            "button": {"payload": "STOP-PAYLOAD", "text": "Stop promotions"},
            "context": {"from": "201000000000", "id": "wamid.template"},
        },
        ReplyActionSource.BUTTON,
        "STOP-PAYLOAD",
        "Stop promotions",
    ),
    (
        "interactive_button_reply",
        {
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "book-yes", "title": "Yes, book it"},
            },
            "context": {"from": "201000000000", "id": "wamid.buttons"},
        },
        ReplyActionSource.BUTTON_REPLY,
        "book-yes",
        "Yes, book it",
    ),
    (
        "interactive_list_reply",
        {
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": "plan-2", "title": "Pro plan", "description": "Monthly"},
            },
            "context": {"from": "201000000000", "id": "wamid.list"},
        },
        ReplyActionSource.LIST_REPLY,
        "plan-2",
        "Pro plan",
    ),
]


@pytest.mark.parametrize(
    ("name", "message", "source", "payload", "title"),
    WHATSAPP_TAPS,
    ids=[tap[0] for tap in WHATSAPP_TAPS],
)
def test_a_whatsapp_tap_keeps_its_words_and_its_payload(
    name: str, message: dict[str, Any], source: ReplyActionSource, payload: str, title: str
) -> None:
    """The words become the text; the payload is kept beside them; the context survives."""
    event, _ = WhatsAppAdapter().parse(_whatsapp_tap(message)).events

    assert event.text == title
    assert event.action is not None
    assert (event.action.source, event.action.id_or_payload, event.action.title) == (
        source,
        payload,
        title,
    )
    assert event.message_kind.value == "interactive"
    assert event.reply_to == message["context"]["id"]


def test_a_reply_action_survives_normalisation_on_another_channel() -> None:
    """The carrier is the seam's, not WhatsApp's: a Messenger-shaped quick reply keeps it."""
    adapter = SyntheticAdapter()
    payload = synthetic_payload(
        "syn-account",
        {
            "type": "message",
            "id": "syn.m.tap",
            "from": IGSID,
            "at": 1790000000,
            "quick_reply": {"payload": "SIZE_M", "title": "Medium"},
        },
    )

    (event,) = adapter.parse(payload).events

    assert event.text == "Medium"
    assert event.action is not None
    assert (event.action.source, event.action.id_or_payload) == (
        ReplyActionSource.QUICK_REPLY,
        "SIZE_M",
    )


@pytest.mark.parametrize(
    "message",
    [
        {"type": "button", "button": {"payload": "x" * 1001, "text": "y" * 300}},
        {"type": "interactive", "interactive": {"type": "nfm_reply", "nfm_reply": {}}},
        {"type": "interactive", "interactive": {"type": ["list"]}},
        {"type": "button", "button": "not an object"},
    ],
    ids=["over_long", "unknown_reply_type", "unhashable_type", "malformed"],
)
def test_a_tap_meta_did_not_issue_is_kept_without_an_action(message: dict[str, Any]) -> None:
    """Over-long ids are dropped, not cut; unknown shapes keep the message, with no action."""
    delivery = WhatsAppAdapter().parse(_whatsapp_tap(message))

    event, _ = delivery.events
    assert event.action is None
    assert event.text is None
    assert delivery.refused_total == 0
