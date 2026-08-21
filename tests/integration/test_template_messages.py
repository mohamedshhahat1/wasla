"""Template sends, against PostgreSQL and a mocked Meta transport.

A template is the only message a business may send outside the 24-hour service
window, and it is the one message whose text Wasla never sees: Meta renders it
from its own approved copy. These assert both halves - that the window rule
distinguishes the two, and that what lands in the transcript identifies the
template rather than inventing its wording.

The HTTP boundary is faked, not the client: the real `WhatsAppClient` builds and
parses the request, so a change to the Cloud API payload still fails here.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import ValidationError
from app.db.models.conversation import (
    Contact,
    Conversation,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.services import messaging_service as messaging_module
from app.services.messaging_service import MessagingService

pytestmark = pytest.mark.integration

PHONE_NUMBER_ID = "109876543210"
WABA_ID = "555000111"
DISPLAY_NUMBER = "+201000000000"
CUSTOMER = "201234567890"
WAMID = "wamid.template.sent"
TEMPLATE_NAME = "appointment_reminder"
TEMPLATE_LANGUAGE = "ar_EG"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        meta_access_token="test-access-token",
    )


class Recorder:
    """Captures the outbound request and answers as Meta would."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "messaging_product": "whatsapp",
                    "contacts": [{"wa_id": CUSTOMER}],
                    "messages": [{"id": WAMID}],
                },
            )

        return httpx.MockTransport(handle)


@pytest.fixture
def meta(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=recorder.transport()),
    )
    return recorder


async def _conversation(session, *, last_inbound_at):
    tenant = Tenant(name="Acme", slug="acme")
    session.add(tenant)
    await session.flush()

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=PHONE_NUMBER_ID,
        waba_id=WABA_ID,
        display_phone_number=DISPLAY_NUMBER,
    )
    contact = Contact(tenant_id=tenant.id, wa_id=CUSTOMER)
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        last_inbound_at=last_inbound_at,
    )
    session.add(conversation)
    await session.flush()
    return tenant, conversation


async def test_a_template_send_is_recorded_as_a_template(db_session, settings, meta):
    # Deliberately stale: a template must not need an open window.
    tenant, conversation = await _conversation(
        db_session,
        last_inbound_at=datetime.now(UTC) - timedelta(days=3),
    )
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    message = await service.send_template(
        conversation_id=conversation.id,
        name=TEMPLATE_NAME,
        language=TEMPLATE_LANGUAGE,
    )

    assert message.kind is MessageKind.TEMPLATE
    assert message.template_name == TEMPLATE_NAME
    assert message.template_language == TEMPLATE_LANGUAGE
    assert message.direction is MessageDirection.OUTBOUND
    assert message.status is MessageStatus.SENT
    assert message.wa_message_id == WAMID


async def test_the_body_stays_empty_because_meta_renders_the_text(db_session, settings, meta):
    tenant, conversation = await _conversation(
        db_session,
        last_inbound_at=datetime.now(UTC) - timedelta(days=3),
    )
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    message = await service.send_template(
        conversation_id=conversation.id,
        name=TEMPLATE_NAME,
        language=TEMPLATE_LANGUAGE,
    )

    # Recording a guess at the wording would put words in the transcript that
    # the customer never saw.
    assert message.body is None


async def test_the_template_reaches_meta_in_the_cloud_api_shape(db_session, settings, meta):
    tenant, conversation = await _conversation(
        db_session,
        last_inbound_at=datetime.now(UTC) - timedelta(days=3),
    )
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    await service.send_template(
        conversation_id=conversation.id,
        name=TEMPLATE_NAME,
        language=TEMPLATE_LANGUAGE,
    )

    assert len(meta.requests) == 1
    payload = json.loads(meta.requests[0].content)
    assert payload["type"] == "template"
    assert payload["template"]["name"] == TEMPLATE_NAME
    assert payload["template"]["language"] == {"code": TEMPLATE_LANGUAGE}
    assert payload["to"] == CUSTOMER


async def test_free_text_outside_the_window_is_refused_where_a_template_is_not(
    db_session,
    settings,
    meta,
):
    tenant, conversation = await _conversation(
        db_session,
        last_inbound_at=datetime.now(UTC) - timedelta(days=3),
    )
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    with pytest.raises(ValidationError):
        await service.send_text(conversation_id=conversation.id, body="are you still there?")

    # The same conversation accepts a template, which is the whole distinction.
    message = await service.send_template(
        conversation_id=conversation.id,
        name=TEMPLATE_NAME,
        language=TEMPLATE_LANGUAGE,
    )
    assert message.status is MessageStatus.SENT


async def test_a_text_send_leaves_the_template_columns_empty(db_session, settings, meta):
    tenant, conversation = await _conversation(db_session, last_inbound_at=datetime.now(UTC))
    service = MessagingService(session=db_session, settings=settings, tenant_id=tenant.id)

    message = await service.send_text(conversation_id=conversation.id, body="hello")

    assert message.kind is MessageKind.TEXT
    assert message.template_name is None
    assert message.template_language is None
