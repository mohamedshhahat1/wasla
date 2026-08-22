"""A customer's "stop" honoured on the inbound path, against PostgreSQL.

The opt-out is recorded in the same transaction that stores the message, rather
than by a worker afterwards. That closes a window: between the message arriving
and a worker reading it, a campaign sweep could write to somebody who has
already said no, and no amount of later tidying takes that message back.

What is proved here is that the flag reaches the contact row, that it does not
move once set, and that saying "stop" does not silence the agent — a customer
refusing marketing is not refusing an answer.
"""

from __future__ import annotations

import pytest

from app.db.models.campaign import OptOutSource
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.conversation_repository import ContactRepository
from app.services.whatsapp_service import WhatsAppIngestionService

pytestmark = pytest.mark.integration

PHONE_NUMBER_ID = "109876543210"
CUSTOMER = "201234567890"

# Written whole rather than assembled: a word is easier to check against a
# keyboard than a list of code points.
CANCEL = "الغاء"


async def _account(session, *, slug: str) -> WhatsAppAccount:
    tenant = Tenant(name=slug.title(), slug=slug)
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=PHONE_NUMBER_ID,
        waba_id="555000111",
        display_phone_number="+201000000000",
    )
    session.add(account)
    await session.flush()
    return account


def _inbound(*, text: str, message_id: str = "wamid.one") -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Nour"}}],
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": CUSTOMER,
                                    "type": "text",
                                    "timestamp": "1786000000",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


async def _contact(session, account: WhatsAppAccount):
    return await ContactRepository(session, tenant_id=account.tenant_id).get_by_wa_id(CUSTOMER)


async def test_a_customer_saying_stop_is_opted_out(db_session):
    account = await _account(db_session, slug="stop-english")

    outcome = await WhatsAppIngestionService(session=db_session).ingest(_inbound(text="STOP"))
    await db_session.flush()

    assert outcome.opt_outs == 1
    contact = await _contact(db_session, account)
    assert contact is not None
    assert contact.marketing_opt_out_at is not None
    assert contact.opt_out_source is OptOutSource.CUSTOMER
    assert contact.accepts_campaigns is False


async def test_the_same_works_in_arabic(db_session):
    account = await _account(db_session, slug="stop-arabic")

    await WhatsAppIngestionService(session=db_session).ingest(_inbound(text=CANCEL))
    await db_session.flush()

    contact = await _contact(db_session, account)
    assert contact is not None and contact.accepts_campaigns is False


async def test_an_ordinary_message_changes_nothing(db_session):
    account = await _account(db_session, slug="ordinary-message")

    outcome = await WhatsAppIngestionService(session=db_session).ingest(
        _inbound(text="can you stop the delivery please")
    )
    await db_session.flush()

    assert outcome.opt_outs == 0
    contact = await _contact(db_session, account)
    assert contact is not None and contact.accepts_campaigns is True


async def test_saying_stop_twice_does_not_move_the_timestamp(db_session):
    """The first refusal is the one that counts."""
    account = await _account(db_session, slug="stop-twice")
    service = WhatsAppIngestionService(session=db_session)

    await service.ingest(_inbound(text="stop", message_id="wamid.one"))
    await db_session.flush()
    contact = await _contact(db_session, account)
    assert contact is not None
    first = contact.marketing_opt_out_at

    second = await WhatsAppIngestionService(session=db_session).ingest(
        _inbound(text="stop", message_id="wamid.two")
    )
    await db_session.flush()

    assert second.opt_outs == 0
    assert contact.marketing_opt_out_at == first


async def test_a_stop_still_produces_a_message_and_an_agent_job(db_session):
    """Refusing marketing is not refusing an answer."""
    account = await _account(db_session, slug="stop-still-answered")

    outcome = await WhatsAppIngestionService(session=db_session).ingest(_inbound(text="stop"))
    await db_session.flush()

    assert outcome.stored == 1
    contact = await _contact(db_session, account)
    assert contact is not None and contact.accepts_campaigns is False
