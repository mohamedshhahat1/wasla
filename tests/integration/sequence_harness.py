"""The harness of the automatic follow-up tests (FUS-02 .. FUS-09).

Real `FollowUpWorker` sweeps, the real `MessagingService` (the AI's reply goes
through it, so the run starts exactly where it does in production), the real
inbound ingestion and the real channel policy; Meta is replaced at the HTTP
transport, and "sent" and "not sent" are counted there.

The model is faked at the OpenAI transport (`ScriptedModel`): it reads what
the step's request carried - the conversation - and answers the structured
shape a test chooses, slowly, wrongly or not at all.

Time: the dispatch reads the real clock. A test reads the due time the code
computed - and asserts it against the anchor or the previous step's sending -
then makes the step due now and sweeps. Nothing waits five real minutes, and
no fixed date is used.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezones import zone
from app.db.models.agent import Agent, AgentStatus
from app.db.models.conversation import Contact, Conversation, MessageOrigin
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.follow_up_sequence import FollowUpSequenceRun
from app.db.models.follow_up_settings import FollowUpSettings
from app.db.models.tenant import Tenant
from app.db.models.whatsapp import WhatsAppAccount
from app.db.session import Database
from app.integrations.openai.client import ResponsesClient
from app.services.follow_up_writer import ModelStepWriter, StepWriter
from app.services.messaging_service import MessagingService
from app.services.whatsapp_service import WhatsAppIngestionService
from app.workers.follow_up_worker import FollowUpWorker
from tests.integration.ai_harness import text_response
from tests.integration.campaign_harness import FakeMeta, Workspace, harness_settings, seed

#: What the scripted model writes, after the customer's last words.
STEP_PREFIX = "About your question:"


class ScriptedModel:
    """The Responses API at the transport, answering a follow-up step's structured shape.

    `answer(payload)` decides each answer; by default the step's text quotes
    the last thing the customer said, which proves the conversation reached it.
    `fail` failures (500) come first; `delay` seconds pass before answering,
    and `on_call` runs as the call arrives (to race a reply against it).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = 0
        self.delay = 0.0
        self.on_call: Callable[[], Awaitable[None]] | None = None
        self.answer: Callable[[dict[str, Any]], dict[str, Any]] = self._quote

    @staticmethod
    def last_customer_words(payload: dict[str, Any]) -> str:
        users = [item for item in payload.get("input", []) if item.get("role") == "user"]
        content = users[-1]["content"] if users else ""
        return content if isinstance(content, str) else str(content)

    def _quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "send",
            "reason": None,
            "text": f"{STEP_PREFIX} {self.last_customer_words(payload)}",
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        self.calls.append(payload)
        if self.on_call is not None:
            await self.on_call()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail > 0:
            self.fail -= 1
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=text_response(json.dumps(self.answer(payload))))

    @asynccontextmanager
    async def writer(self) -> AsyncIterator[StepWriter]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as http:
            yield ModelStepWriter(ResponsesClient(http=http, api_key="sk-test", max_attempts=1))


@dataclass
class World:
    database: Database
    meta: FakeMeta
    workspace: Workspace
    database_url: str
    phone_number_id: str
    model: ScriptedModel = field(default_factory=ScriptedModel)

    def messaging(self, session: AsyncSession, name: str = "ai") -> MessagingService:
        return MessagingService(
            session=session,
            settings=harness_settings(self.database_url),
            tenant_id=self.workspace.tenant_id,
            http=self.meta.client(name),
        )


def sweeper(world: World, *, name: str = "follow-ups", **overrides: Any) -> FollowUpWorker:
    values: dict[str, Any] = {"follow_up_ai_retry_seconds": 0.05}
    values.update(overrides)
    settings = harness_settings(world.database_url, **values)
    http = world.meta.client(name)

    def messaging(session: AsyncSession, tenant_id: uuid.UUID) -> MessagingService:
        return MessagingService(session=session, settings=settings, tenant_id=tenant_id, http=http)

    return FollowUpWorker(
        database=world.database,
        settings=settings,
        messaging_factory=messaging,
        writer_factory=world.model.writer,
    )


async def build(
    database: Database,
    database_url: str,
    meta: FakeMeta,
    *,
    steps: list[int],
    recipients: int = 1,
    enabled: bool = True,
    quiet_hours_now: bool = True,
    last_inbound_ago: timedelta = timedelta(minutes=1),
    limits: dict[str, int] | None = None,
) -> World:
    """A workspace whose customers just wrote, with reminders `steps` (minutes) on WhatsApp.

    With `quiet_hours_now` the workspace's quiet hours are on and contain the
    real now - steps are not bound by them (FUS-08).
    """
    workspace = await seed(
        database,
        recipients=recipients,
        last_inbound_at=datetime.now(UTC) - last_inbound_ago,
        limits=limits,
    )
    async with database.session_factory() as session:
        session.add(
            FollowUpSettings(
                tenant_id=workspace.tenant_id,
                enabled=enabled,
                channel_types=["whatsapp"],
                steps=[{"delay_minutes": minutes, "guidance": None} for minutes in steps],
                version=1,
            )
        )
        if quiet_hours_now:
            tenant = await session.get(Tenant, workspace.tenant_id)
            assert tenant is not None
            now_local = datetime.now(UTC).astimezone(zone(tenant.timezone))
            tenant.campaign_quiet_hours_enabled = True
            tenant.campaign_quiet_hours_start = _minute(now_local - timedelta(hours=2))
            tenant.campaign_quiet_hours_end = _minute(now_local + timedelta(hours=9))
        session.add(
            Agent(
                tenant_id=workspace.tenant_id,
                name="Sales",
                is_default=True,
                status=AgentStatus.ACTIVE,
                model="gpt-4o-mini",
                system_prompt="You sell kitchen fittings. Answer briefly, in Egyptian Arabic.",
            )
        )
        account = await session.get(WhatsAppAccount, workspace.account_id)
        assert account is not None
        await session.commit()
        return World(database, meta, workspace, database_url, account.phone_number_id)


def _minute(moment: datetime) -> time:
    return moment.time().replace(second=0, microsecond=0)


async def conversations(world: World) -> list[Conversation]:
    async with world.database.session_factory() as session:
        rows = await session.execute(
            select(Conversation)
            .where(Conversation.tenant_id == world.workspace.tenant_id)
            .order_by(Conversation.id)
        )
        return list(rows.scalars())


async def wa_id(world: World, conversation: Conversation) -> str:
    async with world.database.session_factory() as session:
        contact = await session.get(Contact, conversation.contact_id)
        assert contact is not None and contact.wa_id is not None
        return contact.wa_id


async def ai_reply(world: World, conversation: Conversation, text: str = "It costs 100.") -> None:
    """The agent's answer, through the messaging service the orchestrator uses."""
    async with world.database.session_factory() as session:
        await world.messaging(session).send_text(
            conversation_id=conversation.id, body=text, origin=MessageOrigin.AGENT
        )
        await session.commit()


async def customer_writes(world: World, conversation: Conversation, text: str) -> None:
    """The customer's message, through Meta's webhook payload and the real ingestion."""
    number = await wa_id(world, conversation)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "201000000000",
                                "phone_number_id": world.phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Customer"}, "wa_id": number}],
                            "messages": [
                                {
                                    "from": number,
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    async with world.database.session_factory() as session:
        await WhatsAppIngestionService(session=session).ingest(payload)
        await session.commit()


async def steps_of(world: World, conversation: Conversation) -> list[FollowUp]:
    async with world.database.session_factory() as session:
        rows = await session.execute(
            select(FollowUp)
            .where(FollowUp.conversation_id == conversation.id)
            .order_by(FollowUp.created_at, FollowUp.step_index)
        )
        return list(rows.scalars())


async def pending(world: World, conversation: Conversation) -> FollowUp | None:
    found = [
        row for row in await steps_of(world, conversation) if row.status is FollowUpStatus.PENDING
    ]
    assert len(found) <= 1, "one waiting step per conversation"
    return found[0] if found else None


async def runs_of(world: World, conversation: Conversation) -> list[FollowUpSequenceRun]:
    async with world.database.session_factory() as session:
        rows = await session.execute(
            select(FollowUpSequenceRun)
            .where(FollowUpSequenceRun.conversation_id == conversation.id)
            .order_by(FollowUpSequenceRun.created_at)
        )
        return list(rows.scalars())


async def make_due(world: World, conversation: Conversation) -> None:
    """The waiting step's time has come: due a second ago."""
    async with world.database.session_factory() as session:
        await session.execute(
            update(FollowUp)
            .where(
                FollowUp.conversation_id == conversation.id,
                FollowUp.status == FollowUpStatus.PENDING,
            )
            .values(scheduled_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()


async def set_conversation(world: World, conversation: Conversation, **values: Any) -> None:
    async with world.database.session_factory() as session:
        await session.execute(
            update(Conversation).where(Conversation.id == conversation.id).values(**values)
        )
        await session.commit()


def sends_to(world: World, number: str) -> list[dict[str, Any]]:
    return [entry for entry in world.meta.requests if entry["to"] == number]


def step_sends(world: World, number: str) -> list[dict[str, Any]]:
    """The requests to `number` that carried a step's text."""
    return [
        entry
        for entry in sends_to(world, number)
        if (_text_of(entry["payload"]) or "").startswith(STEP_PREFIX)
    ]


def _text_of(payload: dict[str, Any]) -> str | None:
    text = payload.get("text")
    return text.get("body") if isinstance(text, dict) else None


def kinds(entries: list[dict[str, Any]]) -> set[str]:
    return {str(entry["payload"].get("type")) for entry in entries}


Predicate = Callable[[FollowUp], bool]
