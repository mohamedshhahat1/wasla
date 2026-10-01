"""Only a new customer message has consequences (OMNI-005, ADR-120).

The audit's probe Y1, kept as a regression: an inbound event carrying the
provider id of a message Wasla had *sent* was "deduplicated" onto that outbound
row - a conversation was opened on the wrong number, its reply window opened,
and an agent turn was queued with Wasla's own reply as its trigger:

    outcome   stored=1 duplicates=0 queued=1
    job       conversation=d1 trigger_message_id=e1 (Wasla's own outbound)

Echoes are the ordinary case of that shape on Messenger, Instagram and WhatsApp
Coexistence, so it had to be impossible before any of them is built.

The rules pinned here:

- A provider id is identified per connection. The same id on another
  connection of the workspace is a collision - refused, counted, and the
  event kept as failed evidence - never a duplicate of the other connection's
  message (mutants M2 and M9).
- An inbound event naming a message that is not a customer's inbound message
  on this connection is a collision, and nothing is opened, touched or queued
  (M11's collision half; the echo half is in
  `test_omnichannel_second_channel.py`).
- The worker refuses to claim a turn whose trigger is not a customer's
  message in that conversation - the backstop behind all of the above - but a
  trigger it cannot see *yet* is retried, not dropped (ADR-089).
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

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.db.models.agent_turn import AgentTurn
from app.db.models.channel_event import ChannelEvent, ChannelEventState
from app.db.models.conversation import Conversation, Message, MessageOrigin
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.repositories.agent_turn_repository import (
    AgentTurnRepository,
    TriggerNotAnswerableError,
)
from app.services import messaging_service as messaging_module
from app.services.channel_ingestion_service import IngestionOutcome
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.queue import AgentJob, AgentQueue

pytestmark = pytest.mark.integration

CUSTOMER = "201000000201"
OTHER_CUSTOMER = "201000000202"
SENT_ID = f"wamid.sent.{uuid.uuid4().hex}"


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[AgentJob] = []

    async def enqueue(self, job: AgentJob) -> None:
        self.jobs.append(job)


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
def meta(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Meta accepts every send and names it `SENT_ID`."""

    def handle(request: httpx.Request) -> httpx.Response:
        json.loads(request.content)
        return httpx.Response(
            200, json={"messaging_product": "whatsapp", "messages": [{"id": SENT_ID}]}
        )

    monkeypatch.setattr(
        messaging_module,
        "build_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    yield


async def _workspace(session: AsyncSession) -> tuple[Tenant, WhatsAppAccount, WhatsAppAccount]:
    """One workspace with two numbers."""
    tag = uuid.uuid4().hex[:10]
    tenant = Tenant(name=f"Echo {tag}", slug=f"echo-{tag}")
    session.add(tenant)
    await session.flush()
    numbers = []
    for index in range(2):
        account = WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"PN-{tag}-{index}",
            waba_id="waba-echo",
            display_phone_number=f"+20 100 000 00{index}",
            ownership_started_at=datetime.now(UTC) - timedelta(days=1),
        )
        session.add(account)
        numbers.append(account)
    await session.flush()
    return tenant, numbers[0], numbers[1]


def _inbound(account: WhatsAppAccount, *, wamid: str, sender: str = CUSTOMER) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": account.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": account.phone_number_id},
                            "contacts": [{"wa_id": sender}],
                            "messages": [
                                {
                                    "id": wamid,
                                    "from": sender,
                                    "type": "text",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "text": {"body": "hello"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


async def _ingest(
    session: AsyncSession, payload: dict[str, Any], queue: RecordingQueue
) -> IngestionOutcome:
    return await WhatsAppIngestionService(session=session, queue=cast("AgentQueue", queue)).ingest(
        payload
    )


async def _conversations(session: AsyncSession, account: WhatsAppAccount) -> list[Conversation]:
    rows = await session.execute(select(Conversation).where(Conversation.account_id == account.id))
    return list(rows.scalars().all())


async def _event_state(session: AsyncSession, account: WhatsAppAccount, event_id: str) -> Any:
    return (
        await session.execute(
            select(ChannelEvent.state, ChannelEvent.error).where(
                ChannelEvent.account_id == account.id, ChannelEvent.event_id == event_id
            )
        )
    ).one()


async def _sent_on(
    session: AsyncSession, settings: Settings, tenant: Tenant, account: WhatsAppAccount
) -> tuple[Conversation, Message]:
    """A customer wrote on `account`; a colleague replied, and Meta named the reply SENT_ID."""
    queue = RecordingQueue()
    await _ingest(session, _inbound(account, wamid=f"wamid.{uuid.uuid4().hex}"), queue)
    (conversation,) = await _conversations(session, account)
    reply = await MessagingService(
        session=session, settings=settings, tenant_id=tenant.id
    ).send_text(conversation_id=conversation.id, body="We are open.", origin=MessageOrigin.HUMAN)
    assert reply.wa_message_id == SENT_ID
    return conversation, reply


# ------------------------------------------------------------ probe Y1


async def test_an_inbound_event_carrying_our_own_sent_id_on_another_number_is_a_collision(
    db_session: AsyncSession, settings: Settings, meta: None
) -> None:
    """Y1 exactly: our reply on number 1 carries id P; an inbound event on
    number 2 carries P. Nothing is opened on number 2, nothing touched on
    number 1, nothing queued - and the event is kept, failed, as evidence."""
    tenant, first, second = await _workspace(db_session)
    conversation, reply = await _sent_on(db_session, settings, tenant, first)
    window_before = conversation.last_inbound_at
    queue = RecordingQueue()

    outcome = await _ingest(db_session, _inbound(second, wamid=SENT_ID), queue)

    assert (outcome.collisions, outcome.duplicates, outcome.queued) == (1, 0, 0)
    assert queue.jobs == []
    assert await _conversations(db_session, second) == []
    await db_session.refresh(conversation)
    assert conversation.last_inbound_at == window_before
    state, reason = await _event_state(db_session, second, SENT_ID)
    assert state is ChannelEventState.FAILED
    assert reason == "provider_id_collision"
    # And no turn exists for our own reply.
    turns = await db_session.scalar(
        select(func.count()).select_from(AgentTurn).where(AgentTurn.trigger_message_id == reply.id)
    )
    assert turns == 0


async def test_an_inbound_event_carrying_our_own_sent_id_on_the_same_number_is_a_collision(
    db_session: AsyncSession, settings: Settings, meta: None
) -> None:
    """The echo shape on one connection: the id names Wasla's own outbound
    message there. It is not the customer writing, so it opens no window and
    asks nobody to answer."""
    tenant, first, _ = await _workspace(db_session)
    conversation, _ = await _sent_on(db_session, settings, tenant, first)
    window_before = conversation.last_inbound_at
    queue = RecordingQueue()

    outcome = await _ingest(db_session, _inbound(first, wamid=SENT_ID), queue)

    assert (outcome.collisions, outcome.duplicates, outcome.queued) == (1, 0, 0)
    assert queue.jobs == []
    await db_session.refresh(conversation)
    assert conversation.last_inbound_at == window_before


# ---------------------------------------------- per-connection ids (M2, M9)


async def test_a_customer_message_id_seen_on_another_number_is_not_a_duplicate(
    db_session: AsyncSession,
) -> None:
    """M9: the same provider id on a second connection is not that connection's
    replay of the first one's message - which would return the first
    connection's row as "already handled" - and not a new message either while
    the workspace-wide key stands (ADR-120). It is a collision."""
    tenant, first, second = await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"
    await _ingest(db_session, _inbound(first, wamid=wamid), RecordingQueue())
    queue = RecordingQueue()

    elsewhere = await _ingest(
        db_session, _inbound(second, wamid=wamid, sender=OTHER_CUSTOMER), queue
    )

    assert (elsewhere.collisions, elsewhere.duplicates, elsewhere.queued) == (1, 0, 0)
    assert queue.jobs == []
    assert await _conversations(db_session, second) == []


async def test_a_replay_on_the_same_number_is_a_duplicate(db_session: AsyncSession) -> None:
    """The ordinary case the per-connection key exists for: Meta redelivers."""
    tenant, first, _ = await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"
    await _ingest(db_session, _inbound(first, wamid=wamid), RecordingQueue())
    queue = RecordingQueue()

    replay = await _ingest(db_session, _inbound(first, wamid=wamid), queue)

    assert (replay.duplicates, replay.collisions, replay.queued) == (1, 0, 0)
    assert queue.jobs == []
    messages = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.wa_message_id == wamid)
    )
    assert messages == 1


async def test_the_same_id_in_another_workspace_is_that_workspaces_message(
    db_session: AsyncSession,
) -> None:
    """Probe X4 stays true: provider identity never crosses a workspace."""
    _, acme, _ = await _workspace(db_session)
    _, rival, _ = await _workspace(db_session)
    wamid = f"wamid.{uuid.uuid4().hex}"

    first = await _ingest(db_session, _inbound(acme, wamid=wamid), RecordingQueue())
    second = await _ingest(db_session, _inbound(rival, wamid=wamid), RecordingQueue())

    assert (first.stored, second.stored) == (1, 1)
    assert (second.collisions, second.duplicates) == (0, 0)


# ------------------------------------------------- the worker's backstop


async def test_a_turn_is_never_claimed_for_our_own_message(
    db_session: AsyncSession, settings: Settings, meta: None
) -> None:
    """Whatever produced the job, Wasla's reply is not a customer's turn."""
    tenant, first, _ = await _workspace(db_session)
    conversation, reply = await _sent_on(db_session, settings, tenant, first)
    turns = AgentTurnRepository(db_session, tenant_id=tenant.id)

    with pytest.raises(TriggerNotAnswerableError):
        await turns.claim(
            conversation_id=conversation.id, trigger_message_id=reply.id, worker_id="w"
        )
    with pytest.raises(TriggerNotAnswerableError):
        await turns.owe(conversation_id=conversation.id, trigger_message_id=reply.id)

    claimed = await db_session.scalar(
        select(func.count()).select_from(AgentTurn).where(AgentTurn.tenant_id == tenant.id)
    )
    assert claimed == 0


async def test_a_turn_is_never_claimed_for_another_conversations_message(
    db_session: AsyncSession,
) -> None:
    tenant, first, second = await _workspace(db_session)
    await _ingest(db_session, _inbound(first, wamid=f"wamid.{uuid.uuid4().hex}"), RecordingQueue())
    await _ingest(
        db_session,
        _inbound(second, wamid=f"wamid.{uuid.uuid4().hex}", sender=OTHER_CUSTOMER),
        RecordingQueue(),
    )
    (mine,) = await _conversations(db_session, first)
    (theirs,) = await _conversations(db_session, second)
    their_message = await db_session.scalar(
        select(Message.id).where(Message.conversation_id == theirs.id)
    )
    assert their_message is not None

    with pytest.raises(TriggerNotAnswerableError):
        await AgentTurnRepository(db_session, tenant_id=tenant.id).claim(
            conversation_id=mine.id, trigger_message_id=their_message, worker_id="w"
        )


async def test_a_trigger_that_is_not_visible_yet_is_retried_not_dropped(
    db_session: AsyncSession,
) -> None:
    """A job can arrive before the webhook's commit is visible (ADR-089). That
    is `not_found` - retried once, then dead-lettered - never "not
    answerable", which would end the job and leave the customer unanswered."""
    tenant, first, _ = await _workspace(db_session)
    await _ingest(db_session, _inbound(first, wamid=f"wamid.{uuid.uuid4().hex}"), RecordingQueue())
    (conversation,) = await _conversations(db_session, first)

    with pytest.raises(NotFoundError) as refused:
        await AgentTurnRepository(db_session, tenant_id=tenant.id).claim(
            conversation_id=conversation.id, trigger_message_id=uuid.uuid4(), worker_id="w"
        )
    assert not isinstance(refused.value, TriggerNotAnswerableError)


async def test_a_customers_message_is_claimed_once(db_session: AsyncSession) -> None:
    tenant, first, _ = await _workspace(db_session)
    queue = RecordingQueue()
    await _ingest(db_session, _inbound(first, wamid=f"wamid.{uuid.uuid4().hex}"), queue)
    (job,) = queue.jobs
    assert job.trigger_message_id is not None
    turns = AgentTurnRepository(db_session, tenant_id=tenant.id)

    first_claim = await turns.claim(
        conversation_id=job.conversation_id,
        trigger_message_id=job.trigger_message_id,
        worker_id="a",
    )

    assert first_claim is True
