"""StatusAdapterContract: a provider's receipts move only our messages, only forwards.

Through the production path - an adapter's parse, `ChannelIngestionService`,
the neutral projection - on a real PostgreSQL. WhatsApp receipts name a message;
the synthetic channel's read receipt is a watermark, as Messenger's is
(OMNI-011). Either way the internal lifecycle is the core's:

- normalised: Meta's words map onto `MessageStatus`; a word with no mapping is
  stored as evidence and moves nothing;
- harmless twice: a duplicate receipt moves no timestamp;
- harmless out of order: `delivered` after `read` leaves the message read;
- unknown message: a receipt for something Wasla never sent creates nothing;
- connection-scoped: a receipt arriving on one connection never advances a
  message sent on another, even in the same workspace (ADR-120);
- a watermark advances only what was sent at or before it, never backwards.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.channel_event import ChannelEvent, ChannelEventState
from app.db.models.conversation import Conversation, Message, MessageOrigin, MessageStatus
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.services import messaging_service as messaging_module
from app.services.channel_ingestion_service import ChannelIngestionService
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from tests.channel_fakes import SyntheticAdapter, synthetic_payload

pytestmark = pytest.mark.integration

CUSTOMER = "201000000901"


class Meta:
    def __init__(self) -> None:
        self.ids: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            json.loads(request.content)
            sent = f"wamid.status.{uuid.uuid4().hex}"
            self.ids.append(sent)
            return httpx.Response(
                200, json={"messaging_product": "whatsapp", "messages": [{"id": sent}]}
            )

        return httpx.MockTransport(handle)


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


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> Iterator[Meta]:
    fake = Meta()
    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=fake.transport()),
    )
    yield fake


async def _tenant(session: AsyncSession) -> Tenant:
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Status {tag}", slug=f"status-{tag}")
    session.add(tenant)
    await session.flush()
    return tenant


async def _number(session: AsyncSession, tenant: Tenant) -> WhatsAppAccount:
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"PN-{uuid.uuid4().hex[:12]}",
        waba_id="waba-status",
        display_phone_number="+20 100 000 0000",
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    session.add(account)
    await session.flush()
    return account


def _envelope(account: WhatsAppAccount, value: dict[str, Any]) -> dict[str, Any]:
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
                            "metadata": {"phone_number_id": account.phone_number_id},
                            **value,
                        },
                    }
                ],
            }
        ],
    }


def _inbound(account: WhatsAppAccount, customer: str = CUSTOMER) -> dict[str, Any]:
    return _envelope(
        account,
        {
            "contacts": [{"wa_id": customer}],
            "messages": [
                {
                    "id": f"wamid.{uuid.uuid4().hex}",
                    "from": customer,
                    "type": "text",
                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                    "text": {"body": "hello"},
                }
            ],
        },
    )


def _status(account: WhatsAppAccount, wamid: str, status: str, *, at: datetime) -> dict[str, Any]:
    return _envelope(
        account,
        {
            "statuses": [
                {
                    "id": wamid,
                    "status": status,
                    "timestamp": str(int(at.timestamp())),
                    "recipient_id": CUSTOMER,
                }
            ]
        },
    )


async def _ingest(session: AsyncSession, payload: dict[str, Any]) -> None:
    await WhatsAppIngestionService(session=session).ingest(payload)


async def _sent(
    session: AsyncSession, settings: Settings, tenant: Tenant, account: WhatsAppAccount
) -> Message:
    await _ingest(session, _inbound(account))
    conversation = await session.scalar(
        select(Conversation).where(Conversation.account_id == account.id)
    )
    assert conversation is not None
    return await MessagingService(
        session=session, settings=settings, tenant_id=tenant.id
    ).send_text(conversation_id=conversation.id, body="Ours.", origin=MessageOrigin.HUMAN)


async def _fresh(session: AsyncSession, message: Message) -> Message:
    found = await session.scalar(
        select(Message).where(Message.id == message.id).execution_options(populate_existing=True)
    )
    assert found is not None
    return found


async def test_meta_statuses_map_onto_the_cores_lifecycle_in_order(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    tenant = await _tenant(db_session)
    number = await _number(db_session, tenant)
    sent = await _sent(db_session, settings, tenant, number)
    assert sent.wa_message_id is not None
    now = datetime.now(UTC)

    await _ingest(db_session, _status(number, sent.wa_message_id, "delivered", at=now))
    delivered = await _fresh(db_session, sent)
    assert delivered.status is MessageStatus.DELIVERED

    await _ingest(db_session, _status(number, sent.wa_message_id, "read", at=now))
    read = await _fresh(db_session, sent)
    assert read.status is MessageStatus.READ
    assert read.read_at is not None


async def test_a_duplicate_and_a_late_receipt_are_harmless(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    tenant = await _tenant(db_session)
    number = await _number(db_session, tenant)
    sent = await _sent(db_session, settings, tenant, number)
    assert sent.wa_message_id is not None
    earlier = datetime.now(UTC) - timedelta(minutes=5)
    later = datetime.now(UTC)

    await _ingest(db_session, _status(number, sent.wa_message_id, "read", at=later))
    first = await _fresh(db_session, sent)
    read_at = first.read_at
    # The same receipt again, and a `delivered` that Meta delivered late.
    await _ingest(db_session, _status(number, sent.wa_message_id, "read", at=later))
    await _ingest(db_session, _status(number, sent.wa_message_id, "delivered", at=earlier))

    after = await _fresh(db_session, sent)
    assert after.status is MessageStatus.READ
    assert after.read_at == read_at


async def test_a_receipt_with_no_mapping_is_evidence_not_a_transition(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    tenant = await _tenant(db_session)
    number = await _number(db_session, tenant)
    sent = await _sent(db_session, settings, tenant, number)
    assert sent.wa_message_id is not None

    await _ingest(db_session, _status(number, sent.wa_message_id, "warning", at=datetime.now(UTC)))

    assert (await _fresh(db_session, sent)).status is MessageStatus.SENT
    state = await db_session.scalar(
        select(ChannelEvent.state).where(
            ChannelEvent.account_id == number.id, ChannelEvent.kind == "status"
        )
    )
    assert state is ChannelEventState.PROCESSED


async def test_a_receipt_for_a_message_never_sent_creates_nothing(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    number = await _number(db_session, tenant)
    before = await db_session.scalar(select(func.count()).select_from(Message))

    await _ingest(
        db_session, _status(number, f"wamid.never.{uuid.uuid4().hex}", "read", at=datetime.now(UTC))
    )

    assert await db_session.scalar(select(func.count()).select_from(Message)) == before


async def test_a_receipt_on_one_connection_never_moves_anothers_message(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    """Same workspace, two numbers: the provider id is the first number's."""
    tenant = await _tenant(db_session)
    first, second = await _number(db_session, tenant), await _number(db_session, tenant)
    sent = await _sent(db_session, settings, tenant, first)
    assert sent.wa_message_id is not None

    await _ingest(db_session, _status(second, sent.wa_message_id, "read", at=datetime.now(UTC)))

    assert (await _fresh(db_session, sent)).status is MessageStatus.SENT


async def test_another_workspaces_receipt_never_moves_this_ones_message(
    db_session: AsyncSession, settings: Settings, meta: Meta
) -> None:
    acme = await _tenant(db_session)
    rival = await _tenant(db_session)
    ours = await _number(db_session, acme)
    theirs = await _number(db_session, rival)
    sent = await _sent(db_session, settings, acme, ours)
    assert sent.wa_message_id is not None

    await _ingest(db_session, _status(theirs, sent.wa_message_id, "read", at=datetime.now(UTC)))

    assert (await _fresh(db_session, sent)).status is MessageStatus.SENT


async def test_a_watermark_never_moves_a_message_backwards(
    db_session: AsyncSession, settings: Settings
) -> None:
    """A watermark older than a per-message `read` leaves it read."""
    tenant = await _tenant(db_session)
    adapter = SyntheticAdapter(Channel.INSTAGRAM)
    registry = ChannelRegistry(
        {
            Channel.WHATSAPP: cast(ChannelAdapter, WhatsAppAdapter()),
            Channel.INSTAGRAM: cast(ChannelAdapter, adapter),
        },
    )
    connection = ChannelConnection(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        channel=Channel.INSTAGRAM,
        external_account_id=f"syn-{uuid.uuid4().hex[:12]}",
        status=ConnectionStatus.ACTIVE,
        ownership_started_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(connection)
    await db_session.flush()
    ingestion = ChannelIngestionService(session=db_session, adapter=cast(ChannelAdapter, adapter))
    now = int(datetime.now(UTC).timestamp())
    await ingestion.ingest(
        adapter.parse(
            synthetic_payload(
                connection.external_account_id,
                {"type": "message", "id": "syn.w.1", "from": "igsid-w", "at": now, "text": "hi"},
            )
        )
    )
    conversation = await db_session.scalar(
        select(Conversation).where(Conversation.account_id == connection.id)
    )
    assert conversation is not None
    sent = await MessagingService(
        session=db_session, settings=settings, tenant_id=tenant.id, channels=registry
    ).send_text(conversation_id=conversation.id, body="Ours.", origin=MessageOrigin.HUMAN)
    assert sent.wa_message_id is not None and sent.sent_at is not None
    # Read, per message.
    await ingestion.ingest(
        adapter.parse(
            synthetic_payload(
                connection.external_account_id,
                {"type": "status", "id": sent.wa_message_id, "status": "read", "at": now},
            )
        )
    )
    read_at = (await _fresh(db_session, sent)).read_at
    assert read_at is not None
    # Then a `delivered`-level watermark from before it was even sent.
    stale = int((sent.sent_at - timedelta(minutes=1)).timestamp())
    await ingestion.ingest(
        adapter.parse(
            synthetic_payload(
                connection.external_account_id,
                {"type": "read", "from": "igsid-w", "watermark": stale, "at": now},
            )
        )
    )

    after = await _fresh(db_session, sent)
    assert after.status is MessageStatus.READ
    assert after.read_at == read_at
