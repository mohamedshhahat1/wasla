"""WhatsApp's own consent signals reach the contact (OMNI-046).

Meta reports a person stopping or resuming marketing messages in WhatsApp itself
through the `user_preferences` webhook (`category: marketing_messages`, `value:
stop | resume`; webhook reference, read 2026-10-02), and refuses a marketing send
to such a person with error 131050 ("Don't retry"). The first was refused as an
unsupported field and the second recorded only on the message, so the CRM showed
consent it did not have and campaigns kept trying.

Subscribing the field in the Meta App is an external action; this proves Wasla
handles it once it arrives.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel_event import ChannelEvent, ChannelEventKind
from app.db.models.conversation import Contact, MessageOrigin, MessageStatus
from app.services import messaging_service as messaging_module
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from tests.integration.test_omnichannel_operations import _customer, _number, _tenant

pytestmark = pytest.mark.integration

PHONE = "201000000661"


def _preferences(account: Any, *entries: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": account.waba_id,
                "changes": [
                    {
                        "field": "user_preferences",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": account.phone_number_id},
                            "contacts": [{"wa_id": PHONE}],
                            "user_preferences": list(entries),
                        },
                    }
                ],
            }
        ],
    }


def _preference(value: str, at: datetime) -> dict[str, Any]:
    return {
        "wa_id": PHONE,
        "detail": "User requested to stop marketing messages",
        "category": "marketing_messages",
        "value": value,
        "timestamp": str(int(at.timestamp())),
    }


async def _contact(session: AsyncSession, contact_id: uuid.UUID) -> Contact:
    contact = await session.get(Contact, contact_id, populate_existing=True)
    assert contact is not None
    return contact


async def test_a_stop_in_whatsapp_opts_the_contact_out(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    contact, _ = await _customer(db_session, tenant, account, PHONE)
    at = (datetime.now(UTC) - timedelta(minutes=3)).replace(microsecond=0)

    outcome = await WhatsAppIngestionService(session=db_session).ingest(
        _preferences(account, _preference("stop", at))
    )
    await db_session.flush()

    assert outcome.refused == {}
    assert outcome.opt_outs == 1
    stopped = await _contact(db_session, contact.id)
    assert stopped.marketing_opt_out_at == at
    assert stopped.opt_out_source is OptOutSource.CUSTOMER
    assert stopped.opt_out_via is OptOutVia.PROVIDER_PREFERENCE
    event = await db_session.scalar(select(ChannelEvent).where(ChannelEvent.tenant_id == tenant.id))
    assert event is not None and event.kind is ChannelEventKind.PREFERENCE


async def test_a_resume_lifts_only_the_providers_own_opt_out(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    contact, _ = await _customer(db_session, tenant, account, PHONE)
    stop = datetime.now(UTC) - timedelta(minutes=10)
    resume = datetime.now(UTC) - timedelta(minutes=1)

    await WhatsAppIngestionService(session=db_session).ingest(
        _preferences(account, _preference("stop", stop))
    )
    await WhatsAppIngestionService(session=db_session).ingest(
        _preferences(account, _preference("resume", resume))
    )
    await db_session.flush()

    resumed = await _contact(db_session, contact.id)
    assert resumed.marketing_opt_out_at is None
    assert resumed.marketing_resumed_at is not None


async def test_a_resume_never_undoes_the_customers_own_stop_word(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    contact, _ = await _customer(db_session, tenant, account, PHONE)
    contact.marketing_opt_out_at = datetime.now(UTC) - timedelta(days=1)
    contact.opt_out_source = OptOutSource.CUSTOMER
    contact.opt_out_via = OptOutVia.MESSAGE
    await db_session.flush()

    await WhatsAppIngestionService(session=db_session).ingest(
        _preferences(account, _preference("resume", datetime.now(UTC)))
    )
    await db_session.flush()

    kept = await _contact(db_session, contact.id)
    assert kept.marketing_opt_out_at is not None
    assert kept.marketing_resumed_at is not None


async def test_a_preference_from_somebody_who_never_wrote_changes_nothing(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)

    outcome = await WhatsAppIngestionService(session=db_session).ingest(
        _preferences(account, _preference("stop", datetime.now(UTC)))
    )
    await db_session.flush()

    assert (outcome.stored, outcome.opt_outs) == (1, 0)
    contacts = await db_session.scalar(select(Contact).where(Contact.tenant_id == tenant.id))
    assert contacts is None, "a preference never creates a contact"


async def test_a_131050_refusal_records_the_opt_out(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _tenant(db_session)
    account = await _number(db_session, tenant)
    contact, conversation = await _customer(db_session, tenant, account, PHONE)

    def refusing(request: httpx.Request) -> httpx.Response:
        body = {"error": {"message": "(Meta's words)", "code": 131050}}
        return httpx.Response(400, content=json.dumps(body).encode())

    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refusing)),
    )
    settings = Settings(_env_file=None, environment="test", meta_access_token="token")

    message = await MessagingService(
        session=db_session, settings=settings, tenant_id=tenant.id
    ).send_template(
        conversation_id=conversation.id, name="promo", language="en", origin=MessageOrigin.CAMPAIGN
    )

    assert message.status is MessageStatus.FAILED
    refused = await _contact(db_session, contact.id)
    assert refused.marketing_opt_out_at is not None
    assert refused.opt_out_via is OptOutVia.PROVIDER_REFUSAL
