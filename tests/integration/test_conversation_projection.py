"""Projection of WhatsApp events into conversations, against PostgreSQL.

These exercise the rules that only a real database can prove: that a replayed
delivery changes nothing, that a late status cannot move a message backwards,
and that two workspaces talking to the same customer stay separate.
"""

from datetime import UTC, datetime

import pytest

from app.db.models.conversation import (
    ConversationStatus,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.conversation_repository import (
    ContactRepository,
    ConversationRepository,
    MessageRepository,
)
from app.services.whatsapp_service import WhatsAppIngestionService

pytestmark = pytest.mark.integration

PHONE_NUMBER_ID = "109876543210"
OTHER_PHONE_NUMBER_ID = "209876543210"
WABA_ID = "555000111"
DISPLAY_NUMBER = "+201000000000"
CUSTOMER = "201234567890"
PROFILE_NAME = "Nour"
WAMID_IN = "wamid.in"
WAMID_SECOND = "wamid.second"
WAMID_OUT = "wamid.out"
SENT_AT = "1786000000"
STATUS_AT = "1786000100"


async def _tenant(session, *, slug):
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()
    return tenant


async def _account(session, *, tenant, phone_number_id):
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=phone_number_id,
        waba_id=WABA_ID,
        display_phone_number=DISPLAY_NUMBER,
    )
    session.add(account)
    await session.flush()
    return account


def _inbound(
    *,
    phone_number_id=PHONE_NUMBER_ID,
    message_id=WAMID_IN,
    text="Hello",
    message_type="text",
    profile_name=PROFILE_NAME,
):
    message = {
        "id": message_id,
        "from": CUSTOMER,
        "type": message_type,
        "timestamp": SENT_AT,
    }
    if message_type == "text":
        message["text"] = {"body": text}

    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [
                                {"wa_id": CUSTOMER, "profile": {"name": profile_name}},
                            ],
                            "messages": [message],
                        }
                    }
                ]
            }
        ]
    }


def _status(*, state, message_id=WAMID_OUT, phone_number_id=PHONE_NUMBER_ID):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "statuses": [
                                {
                                    "id": message_id,
                                    "status": state,
                                    "recipient_id": CUSTOMER,
                                    "timestamp": STATUS_AT,
                                },
                            ],
                        }
                    }
                ]
            }
        ]
    }


async def test_inbound_message_creates_the_whole_aggregate(db_session):
    tenant = await _tenant(db_session, slug="acme")
    account = await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    outcome = await WhatsAppIngestionService(session=db_session).ingest(_inbound())

    assert outcome.stored == 1
    assert outcome.duplicates == 0

    contact = await ContactRepository(db_session, tenant_id=tenant.id).get_by_wa_id(CUSTOMER)
    assert contact is not None
    # Meta only sends the profile name in the contacts block.
    assert contact.display_name == PROFILE_NAME

    conversations = ConversationRepository(db_session, tenant_id=tenant.id)
    conversation = await conversations.get_for_contact(
        contact_id=contact.id,
        account_id=account.id,
    )
    assert conversation is not None
    assert conversation.status is ConversationStatus.OPEN
    # The service window is measured from this, so it must be set on arrival.
    assert conversation.last_inbound_at is not None

    message = await MessageRepository(db_session, tenant_id=tenant.id).get_by_wa_message_id(
        WAMID_IN
    )
    assert message is not None
    assert message.direction is MessageDirection.INBOUND
    assert message.status is MessageStatus.RECEIVED
    assert message.kind is MessageKind.TEXT
    assert message.body == "Hello"
    assert message.conversation_id == conversation.id


async def test_replaying_a_delivery_changes_nothing(db_session):
    """Meta retries until it gets a 200, so replay is the normal case."""
    tenant = await _tenant(db_session, slug="acme")
    account = await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)
    service = WhatsAppIngestionService(session=db_session)

    await service.ingest(_inbound())
    replay = await WhatsAppIngestionService(session=db_session).ingest(_inbound())

    assert replay.stored == 0
    assert replay.duplicates == 1

    contact = await ContactRepository(db_session, tenant_id=tenant.id).get_by_wa_id(CUSTOMER)
    conversations = ConversationRepository(db_session, tenant_id=tenant.id)
    conversation = await conversations.get_for_contact(
        contact_id=contact.id,
        account_id=account.id,
    )
    messages = await MessageRepository(db_session, tenant_id=tenant.id).list_for_conversation(
        conversation_id=conversation.id
    )
    assert len(messages) == 1


async def test_a_second_message_reuses_the_conversation(db_session):
    tenant = await _tenant(db_session, slug="acme")
    account = await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)
    service = WhatsAppIngestionService(session=db_session)

    await service.ingest(_inbound())
    await WhatsAppIngestionService(session=db_session).ingest(
        _inbound(message_id=WAMID_SECOND, text="Still there?")
    )

    contact = await ContactRepository(db_session, tenant_id=tenant.id).get_by_wa_id(CUSTOMER)
    conversations = ConversationRepository(db_session, tenant_id=tenant.id)
    conversation = await conversations.get_for_contact(
        contact_id=contact.id,
        account_id=account.id,
    )
    messages = await MessageRepository(db_session, tenant_id=tenant.id).list_for_conversation(
        conversation_id=conversation.id
    )
    assert len(messages) == 2
    assert len(await conversations.list_open()) == 1


async def test_an_unrecognised_type_is_stored_as_unsupported(db_session):
    """A new Meta message type must not drop the message."""
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    await WhatsAppIngestionService(session=db_session).ingest(
        _inbound(message_type="sticker")
    )

    message = await MessageRepository(db_session, tenant_id=tenant.id).get_by_wa_message_id(
        WAMID_IN
    )
    assert message is not None
    assert message.kind is MessageKind.UNSUPPORTED
    assert message.body is None


async def test_a_late_status_never_moves_a_message_backwards(db_session):
    """Meta does not guarantee ordering; delivered after read must not undo it."""
    tenant = await _tenant(db_session, slug="acme")
    account = await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)
    await WhatsAppIngestionService(session=db_session).ingest(_inbound())

    contact = await ContactRepository(db_session, tenant_id=tenant.id).get_by_wa_id(CUSTOMER)
    conversations = ConversationRepository(db_session, tenant_id=tenant.id)
    conversation = await conversations.get_for_contact(
        contact_id=contact.id,
        account_id=account.id,
    )

    messages = MessageRepository(db_session, tenant_id=tenant.id)
    outbound = await messages.stage_outbound(
        conversation_id=conversation.id,
        kind=MessageKind.TEXT,
        body="On its way",
    )
    await db_session.flush()
    await messages.mark_sent(outbound, wa_message_id=WAMID_OUT, sent_at=datetime.now(UTC))
    await db_session.flush()

    await WhatsAppIngestionService(session=db_session).ingest(_status(state="read"))
    await WhatsAppIngestionService(session=db_session).ingest(_status(state="delivered"))

    projected = await messages.get_by_wa_message_id(WAMID_OUT)
    assert projected.status is MessageStatus.READ
    assert projected.read_at is not None
    # The timestamp is still recorded even though the status did not move.
    assert projected.delivered_at is not None


async def test_a_status_for_an_unknown_message_is_not_an_error(db_session):
    """Normal for traffic sent outside Wasla, so the event still stores."""
    tenant = await _tenant(db_session, slug="acme")
    await _account(db_session, tenant=tenant, phone_number_id=PHONE_NUMBER_ID)

    outcome = await WhatsAppIngestionService(session=db_session).ingest(
        _status(state="delivered", message_id="wamid.never-seen")
    )

    assert outcome.stored == 1
    messages = MessageRepository(db_session, tenant_id=tenant.id)
    assert await messages.get_by_wa_message_id("wamid.never-seen") is None


async def test_the_same_customer_is_separate_in_each_workspace(db_session):
    """One person may be a customer of two businesses on the platform."""
    first = await _tenant(db_session, slug="acme")
    second = await _tenant(db_session, slug="globex")
    await _account(db_session, tenant=first, phone_number_id=PHONE_NUMBER_ID)
    await _account(db_session, tenant=second, phone_number_id=OTHER_PHONE_NUMBER_ID)

    await WhatsAppIngestionService(session=db_session).ingest(_inbound())
    await WhatsAppIngestionService(session=db_session).ingest(
        _inbound(phone_number_id=OTHER_PHONE_NUMBER_ID, message_id=WAMID_SECOND)
    )

    first_contact = await ContactRepository(db_session, tenant_id=first.id).get_by_wa_id(CUSTOMER)
    second_contact = await ContactRepository(db_session, tenant_id=second.id).get_by_wa_id(
        CUSTOMER
    )
    assert first_contact is not None
    assert second_contact is not None
    assert first_contact.id != second_contact.id

    # Neither workspace can see the other's conversation.
    assert len(await ConversationRepository(db_session, tenant_id=first.id).list_open()) == 1
    assert len(await ConversationRepository(db_session, tenant_id=second.id).list_open()) == 1


async def test_an_event_for_an_unknown_number_is_ignored(db_session):
    """Someone else's number, or one that was disconnected."""
    await _tenant(db_session, slug="acme")

    outcome = await WhatsAppIngestionService(session=db_session).ingest(_inbound())

    assert outcome.stored == 0
    assert outcome.unknown_accounts == 1
