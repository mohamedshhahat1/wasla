"""Taps keep their words, and a "stop" tap is honoured (OMNI-030), against PostgreSQL.

Meta delivers a template quick-reply tap as `"type": "button"` with
`"button": {"payload", "text"}`, and an interactive reply as
`"type": "interactive"` with `interactive.button_reply` or
`interactive.list_reply`, each `{id, title}` (WhatsApp Cloud API messages
webhook reference and interactive reply buttons / list messages, re-read
2026-10-02 - OMNICHANNEL_READINESS_AUDIT_FINAL.md section 4a, M12/M13). Both used
to be stored with no text: the agent read `[interactive]`, and a customer tapping
"Stop promotions" under a marketing template was never opted out - although
WhatsApp Business Policy requires every opt-out to be respected.

Every case enters through `WhatsAppIngestionService` - the parser, the adapter
and the neutral ingestion - never through a hand-built event, so a fix that
lived in one layer only would fail here.

Mutants this suite kills: M-O01 (taps mapped to no text again), M-O02 (the
payload match dropped) and M-O03 (an opt-out tap queuing an agent turn).
`test_opt_out_recovery.py` covers the replay of evidence the old code lost.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.memory import build_window
from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models import Membership, TenantRole, User
from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel_event import ChannelEvent, ChannelEventState
from app.db.models.conversation import Contact, Message, ReplyActionSource
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateStatus, WhatsAppTemplate
from app.main import create_app
from app.schemas.conversation import MessageRead
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.queue import AgentJob, AgentQueue
from tests.conftest import AllowingEntitlements, FakeDependency

pytestmark = pytest.mark.integration

CUSTOMER = "201000000321"


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[AgentJob] = []

    async def enqueue(self, job: AgentJob) -> None:
        self.jobs.append(job)


async def _number(session: AsyncSession) -> WhatsAppAccount:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Taps {tag}", slug=f"taps-{tag}")
    session.add(tenant)
    await session.flush()
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"pn-{tag}",
        waba_id=f"waba-{tag}",
        display_phone_number="+201000000000",
    )
    session.add(account)
    await session.flush()
    return account


def _delivery(account: WhatsAppAccount, *messages: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": account.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "201000000000",
                                "phone_number_id": account.phone_number_id,
                            },
                            "contacts": [{"wa_id": CUSTOMER, "profile": {"name": "Nour"}}],
                            "messages": [
                                {
                                    "from": CUSTOMER,
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    **message,
                                }
                                for message in messages
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _button(text: str, payload: str) -> dict[str, Any]:
    return {
        "type": "button",
        "button": {"text": text, "payload": payload},
        "context": {"from": "201000000000", "id": "wamid.marketing-template"},
    }


def _button_reply(identifier: str, title: str) -> dict[str, Any]:
    return {
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": identifier, "title": title}},
        "context": {"from": "201000000000", "id": "wamid.buttons"},
    }


def _list_reply(identifier: str, title: str) -> dict[str, Any]:
    return {
        "type": "interactive",
        "interactive": {
            "type": "list_reply",
            "list_reply": {"id": identifier, "title": title, "description": "Monthly"},
        },
    }


async def _ingest(
    session: AsyncSession, account: WhatsAppAccount, *messages: dict[str, Any]
) -> tuple[Any, RecordingQueue]:
    queue = RecordingQueue()
    outcome = await WhatsAppIngestionService(session=session, queue=cast(AgentQueue, queue)).ingest(
        _delivery(account, *messages)
    )
    await session.flush()
    return outcome, queue


async def _contact(session: AsyncSession, account: WhatsAppAccount) -> Contact:
    contact = await session.scalar(select(Contact).where(Contact.tenant_id == account.tenant_id))
    assert contact is not None
    return contact


async def _messages(session: AsyncSession, account: WhatsAppAccount) -> list[Message]:
    return list(
        (
            await session.scalars(
                select(Message)
                .where(Message.tenant_id == account.tenant_id)
                .order_by(Message.sequence)
            )
        ).all()
    )


def _model_sees(message: Message) -> str:
    window = build_window([message], message_limit=10, token_budget=4_000)
    return window.turns[0].text


async def _mark(
    session: AsyncSession, account: WhatsAppAccount, payloads: list[str]
) -> WhatsAppTemplate:
    template = WhatsAppTemplate(
        tenant_id=account.tenant_id,
        account_id=account.id,
        name="spring_sale",
        language="en",
        status=TemplateStatus.APPROVED,
        variable_count=0,
        opt_out_payloads=payloads,
    )
    session.add(template)
    await session.flush()
    return template


# ------------------------------------------------------------ the live path


async def test_a_stop_promotions_tap_is_an_opt_out_and_is_not_answered(
    db_session: AsyncSession,
) -> None:
    account = await _number(db_session)

    outcome, queue = await _ingest(db_session, account, _button("Stop promotions", "STOP-1"))

    (message,) = await _messages(db_session, account)
    assert message.body == "Stop promotions"
    assert (message.action_source, message.action_payload, message.action_title) == (
        ReplyActionSource.BUTTON,
        "STOP-1",
        "Stop promotions",
    )
    contact = await _contact(db_session, account)
    assert contact.marketing_opt_out_at is not None
    assert contact.opt_out_source is OptOutSource.CUSTOMER
    assert contact.opt_out_via is OptOutVia.REPLY_ACTION
    assert outcome.opt_outs == 1
    # Honoured, and not answered: the only reply an agent has is the sale.
    assert queue.jobs == []
    event = await db_session.scalar(
        select(ChannelEvent).where(ChannelEvent.tenant_id == account.tenant_id)
    )
    assert event is not None and event.state is ChannelEventState.PROCESSED


async def test_a_payload_the_workspace_marked_opts_out_whatever_the_words(
    db_session: AsyncSession,
) -> None:
    account = await _number(db_session)
    await _mark(db_session, account, ["MKT_OPT_OUT"])

    outcome, queue = await _ingest(db_session, account, _button("No thanks", "MKT_OPT_OUT"))

    contact = await _contact(db_session, account)
    assert contact.marketing_opt_out_at is not None
    assert contact.opt_out_via is OptOutVia.REPLY_ACTION
    assert outcome.opt_outs == 1
    assert queue.jobs == []


async def test_a_payload_marked_on_another_number_means_nothing_here(
    db_session: AsyncSession,
) -> None:
    account = await _number(db_session)
    other = WhatsAppAccount(
        tenant_id=account.tenant_id,
        phone_number_id=f"pn-other-{uuid.uuid4().hex[:8]}",
        waba_id=account.waba_id,
        display_phone_number="+201000000001",
    )
    db_session.add(other)
    await db_session.flush()
    await _mark(db_session, other, ["MKT_OPT_OUT"])

    _, queue = await _ingest(db_session, account, _button("No thanks", "MKT_OPT_OUT"))

    assert (await _contact(db_session, account)).marketing_opt_out_at is None
    assert len(queue.jobs) == 1


async def test_an_ordinary_tap_is_answered_and_the_model_reads_what_was_tapped(
    db_session: AsyncSession,
) -> None:
    account = await _number(db_session)

    outcome, queue = await _ingest(db_session, account, _button_reply("book-yes", "Yes, book it"))

    (message,) = await _messages(db_session, account)
    assert message.body == "Yes, book it"
    assert message.action_source is ReplyActionSource.BUTTON_REPLY
    assert message.action_payload == "book-yes"
    assert _model_sees(message) == "[tapped: Yes, book it]"
    assert (await _contact(db_session, account)).marketing_opt_out_at is None
    assert outcome.opt_outs == 0
    (job,) = queue.jobs
    assert job.trigger_message_id == message.id


async def test_a_list_choice_is_read_the_same_way(db_session: AsyncSession) -> None:
    account = await _number(db_session)

    _, queue = await _ingest(db_session, account, _list_reply("plan-2", "Pro plan"))

    (message,) = await _messages(db_session, account)
    assert message.body == "Pro plan"
    assert message.action_source is ReplyActionSource.LIST_REPLY
    assert message.action_payload == "plan-2"
    assert _model_sees(message) == "[tapped: Pro plan]"
    assert len(queue.jobs) == 1


async def test_the_api_reports_the_action_beside_the_words(db_session: AsyncSession) -> None:
    account = await _number(db_session)
    await _ingest(db_session, account, _list_reply("plan-2", "Pro plan"))
    (message,) = await _messages(db_session, account)

    read = MessageRead.from_model(message)

    assert read.body == "Pro plan"
    assert read.action is not None
    assert (read.action.id_or_payload, read.action.title, read.action.source) == (
        "plan-2",
        "Pro plan",
        ReplyActionSource.LIST_REPLY,
    )


async def test_a_typed_stop_is_unchanged_honoured_and_still_answered(
    db_session: AsyncSession,
) -> None:
    """The control: a customer refusing marketing is not refusing an answer."""
    account = await _number(db_session)

    outcome, queue = await _ingest(
        db_session, account, {"type": "text", "text": {"body": "Stop promotions"}}
    )

    (message,) = await _messages(db_session, account)
    assert message.action_source is None
    contact = await _contact(db_session, account)
    assert contact.marketing_opt_out_at is not None
    assert contact.opt_out_via is OptOutVia.MESSAGE
    assert outcome.opt_outs == 1
    assert len(queue.jobs) == 1


async def test_a_location_is_unchanged(db_session: AsyncSession) -> None:
    account = await _number(db_session)

    _, queue = await _ingest(
        db_session,
        account,
        {"type": "location", "location": {"latitude": 30.0, "longitude": 31.2, "name": "Cairo"}},
    )

    (message,) = await _messages(db_session, account)
    assert (message.body, message.action_source) == (None, None)
    assert _model_sees(message) == "[location]"
    assert len(queue.jobs) == 1


async def test_the_numbers_every_stop_tap_is_recorded_none_answered_none_unreadable(
    db_session: AsyncSession,
) -> None:
    """The brief's required numbers, measured on one delivery of mixed taps."""
    account = await _number(db_session)
    other_customer_taps = 5
    outcomes = []
    for index in range(other_customer_taps):
        # Five customers, each tapping "stop" once.
        payload = _delivery(account, _button("Stop promotions", f"STOP-{index}"))
        value = payload["entry"][0]["changes"][0]["value"]
        phone = f"20100000{index:04d}"
        value["contacts"][0]["wa_id"] = phone
        value["messages"][0]["from"] = phone
        queue = RecordingQueue()
        outcomes.append(
            (
                await WhatsAppIngestionService(
                    session=db_session, queue=cast(AgentQueue, queue)
                ).ingest(payload),
                queue,
            )
        )
    await db_session.flush()

    messages = await _messages(db_session, account)
    opted_out = await db_session.scalars(
        select(Contact).where(
            Contact.tenant_id == account.tenant_id, Contact.marketing_opt_out_at.is_not(None)
        )
    )
    assert sum(outcome.opt_outs for outcome, _ in outcomes) == other_customer_taps
    assert len(opted_out.all()) == other_customer_taps
    assert sum(len(queue.jobs) for _, queue in outcomes) == 0
    assert sum(_model_sees(message) == "[interactive]" for message in messages) == 0


async def test_tapping_stop_twice_does_not_move_the_timestamp(db_session: AsyncSession) -> None:
    account = await _number(db_session)
    await _ingest(db_session, account, _button("Stop promotions", "STOP-1"))
    first = (await _contact(db_session, account)).marketing_opt_out_at

    outcome, queue = await _ingest(db_session, account, _button("Stop promotions", "STOP-1"))

    assert outcome.opt_outs == 0
    assert (await _contact(db_session, account)).marketing_opt_out_at == first
    assert queue.jobs == []


# ------------------------------------------------- marking payloads over HTTP


@pytest.fixture
def marks_app(
    settings: Settings, db_session: AsyncSession, fake_redis: FakeDependency
) -> Iterator[FastAPI]:
    application = create_app(settings)
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = fake_redis

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


async def _headers(
    session: AsyncSession, settings: Settings, tenant_id: uuid.UUID, role: TenantRole
) -> dict[str, str]:
    user = User(
        email=f"{uuid.uuid4().hex[:8]}@marks.example",
        hashed_password="x",
        is_active=True,
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    session.add(Membership(tenant_id=tenant_id, user_id=user.id, role=role))
    await session.flush()
    token, _ = create_access_token(
        settings=settings, subject=user.id, tenant_id=tenant_id, token_version=user.token_version
    )
    return {"Authorization": f"Bearer {token}"}


async def test_an_admin_marks_the_opt_out_payloads_and_a_member_cannot(
    db_session: AsyncSession, settings: Settings, marks_app: FastAPI
) -> None:
    account = await _number(db_session)
    template = await _mark(db_session, account, [])
    rival = await _number(db_session)
    rivals = await _mark(db_session, rival, [])
    owner = await _headers(db_session, settings, account.tenant_id, TenantRole.TENANT_OWNER)
    member = await _headers(db_session, settings, account.tenant_id, TenantRole.MEMBER)
    path = f"/api/v1/templates/{template.id}/opt-out-payloads"

    async with AsyncClient(transport=ASGITransport(app=marks_app), base_url="http://t") as http:
        refused = await http.put(path, json={"payloads": ["X"]}, headers=member)
        marked = await http.put(
            path, json={"payloads": ["MKT_OPT_OUT", "MKT_OPT_OUT", "STOP_2"]}, headers=owner
        )
        foreign = await http.put(
            f"/api/v1/templates/{rivals.id}/opt-out-payloads",
            json={"payloads": ["X"]},
            headers=owner,
        )
        too_many = await http.put(
            path, json={"payloads": [str(i) for i in range(11)]}, headers=owner
        )

    assert refused.status_code == 403
    assert marked.status_code == 200
    assert marked.json()["opt_out_payloads"] == ["MKT_OPT_OUT", "STOP_2"]
    assert foreign.status_code == 404
    assert too_many.status_code == 422
    assert rivals.opt_out_payloads == []
