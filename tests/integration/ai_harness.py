"""Scaffolding for agent turns driven end to end.

Real PostgreSQL, real Redis and the real `AgentWorker`. The only fakes are the
two outbound hosts, and they are faked at the *transport*, so the request bytes,
headers and status handling the application produces are the real ones and what
gets counted is what genuinely left the process.

The transport dispatches through a closure rather than a bound method, and that
is load-bearing. A test that swaps a handler after the client was built has to
change what the client does; binding `self._handle` at construction silently
kept the old one, which is how an earlier probe measured an ordinary send while
believing it measured a refused one.

Every assertion about an *absence* in these suites - no send, no inference, no
row - should be paired with a presence proving the path was reached. The counters
here exist so that pairing is always one attribute away.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import delete, func, select

from app.core.config import Settings
from app.db.models.agent import Agent, AgentStatus, AgentTool
from app.db.models.agent_turn import AgentTurn
from app.db.models.billing import BillingInterval, Plan
from app.db.models.conversation import Conversation, Message, MessageDirection
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEvent
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.db.session import Database
from app.integrations.whatsapp.payload import InboundMessage
from app.services.conversation_service import ConversationProjectionService
from app.workers.ai_worker import AgentWorker
from app.workers.queue import AgentJob, AgentQueue

# A logical database of its own by default, and overridable so a run can point
# the whole AI suite at a disposable container instead of a developer's Redis.
REDIS_URL = os.environ.get("WASLA_TEST_REDIS_URL", "redis://localhost:6379/15")

# The structured-output name the sentiment reader asks for. How the fake tells a
# classification request from an agent request without trusting prose.
SENTIMENT_FORMAT = "customer_sentiment"
DEFAULT_REPLY = "Yes, we are here."

JsonObject = dict[str, Any]
AgentHandler = Callable[[JsonObject], Awaitable[JsonObject | httpx.Response]]


def text_response(
    text: str | None,
    *,
    input_tokens: int = 11,
    output_tokens: int = 5,
    usage: object = None,
    response_id: str | None = None,
) -> JsonObject:
    """A Responses API body carrying `text`, or no message at all for None."""
    output: list[JsonObject] = []
    if text is not None:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    return {
        "id": response_id or "resp_" + uuid.uuid4().hex[:12],
        "object": "response",
        "status": "completed",
        "output": output,
        "usage": (
            usage
            if usage is not None
            else {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
        ),
    }


def tool_call_response(
    name: str,
    arguments: JsonObject,
    *,
    text: str | None = None,
    call_id: str | None = None,
) -> JsonObject:
    """A body in which the model asks for one tool, optionally saying something too."""
    body = text_response(text)
    body["output"].append(
        {
            "type": "function_call",
            "call_id": call_id or "call_" + uuid.uuid4().hex[:8],
            "name": name,
            "arguments": json.dumps(arguments),
        }
    )
    return body


def scripted(*bodies: JsonObject) -> AgentHandler:
    """An agent handler answering each round with the next body, then the last."""
    queue = list(bodies)

    async def handle(_request: JsonObject) -> JsonObject:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handle


class FakeProviders:
    """OpenAI and Meta, counted separately and programmable per test."""

    def __init__(self) -> None:
        self.sentiment_requests: list[JsonObject] = []
        self.agent_requests: list[JsonObject] = []
        self.sends: list[JsonObject] = []
        self.reading: JsonObject = {
            "sentiment": "neutral",
            "score": 0.0,
            "intent": None,
            "confidence": 0.9,
        }
        self.agent: AgentHandler = self._reply
        # Awaited inside the sentiment call, so a test can hold several turns at
        # the classifier until all of them have genuinely reached it.
        self.sentiment_gate: Callable[[], Awaitable[object]] | None = None
        self.meta_status = 200

    @property
    def inference(self) -> int:
        return len(self.agent_requests)

    @property
    def sentiment(self) -> int:
        return len(self.sentiment_requests)

    def transport(self) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            return await self._dispatch(request)

        return httpx.MockTransport(handle)

    def inputs(self, index: int = -1) -> list[str]:
        """The text of each plain input item of one agent request, in order."""
        return [
            item["content"]
            for item in self.agent_requests[index]["input"]
            if isinstance(item.get("content"), str)
        ]

    async def _dispatch(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        body: JsonObject = json.loads(request.content or b"{}")
        if "openai" in host:
            fmt = body.get("text", {}).get("format", {})
            if isinstance(fmt, dict) and fmt.get("name") == SENTIMENT_FORMAT:
                self.sentiment_requests.append(body)
                if self.sentiment_gate is not None:
                    await self.sentiment_gate()
                reading = text_response(json.dumps(self.reading), input_tokens=3, output_tokens=2)
                return httpx.Response(200, json=reading)
            self.agent_requests.append(body)
            answer = await self.agent(body)
            if isinstance(answer, httpx.Response):
                return answer
            return httpx.Response(200, json=answer)
        if "facebook" in host:
            self.sends.append(body)
            if self.meta_status != 200:
                return httpx.Response(
                    self.meta_status,
                    json={"error": {"message": "refused", "code": 100}},
                )
            return httpx.Response(
                200,
                json={
                    "messages": [{"id": "wamid.OUT" + uuid.uuid4().hex[:12]}],
                    "contacts": [{"wa_id": "201555000222"}],
                },
            )
        raise AssertionError(f"the turn reached an unexpected host: {host}")

    async def _reply(self, _request: JsonObject) -> JsonObject:
        return text_response(DEFAULT_REPLY)


@dataclass(frozen=True, slots=True)
class Workspace:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    agent_id: uuid.UUID
    phone_number_id: str


def settings_for(url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": url,
        "redis_url": REDIS_URL,
        "openai_api_key": "sk-test-not-real",
        "meta_access_token": "test-platform-token-not-real",
        **overrides,
    }
    return Settings(_env_file=None, **values)


@dataclass
class TurnRunner:
    """Seeds workspaces, runs real workers against them, and cleans up after."""

    url: str
    database: Database
    redis: Redis
    namespace: str
    settings: Settings
    tenants: list[uuid.UUID] = field(default_factory=list)
    plans: list[str] = field(default_factory=list)

    def configure(self, **overrides: Any) -> None:
        """Replace the settings every worker built from here on will read."""
        self.settings = settings_for(self.url, **overrides)

    async def workspace(self, *, grants: Sequence[str] = (), **agent_fields: Any) -> Workspace:
        async with self.database.session() as session:
            tenant = Tenant(name="AI turns", slug=f"ai-{uuid.uuid4().hex[:10]}")
            session.add(tenant)
            await session.flush()
            account = WhatsAppAccount(
                tenant_id=tenant.id,
                phone_number_id=f"PN-{uuid.uuid4().hex[:10]}",
                waba_id="waba-ai",
                display_phone_number="+20 100 000 0001",
                status=WhatsAppAccountStatus.ACTIVE,
                ownership_started_at=datetime.now(UTC) - timedelta(days=2),
                ownership_verified_at=datetime.now(UTC) - timedelta(days=2),
            )
            session.add(account)
            values: dict[str, Any] = {
                "name": "Helper",
                "is_default": True,
                "status": AgentStatus.ACTIVE,
                "model": "gpt-4o-mini",
                "system_prompt": "Answer briefly.",
                **agent_fields,
            }
            agent = Agent(tenant_id=tenant.id, **values)
            session.add(agent)
            await session.flush()
            for name in grants:
                session.add(
                    AgentTool(tenant_id=tenant.id, agent_id=agent.id, name=name, enabled=True)
                )
            workspace = Workspace(
                tenant_id=tenant.id,
                account_id=account.id,
                agent_id=agent.id,
                phone_number_id=account.phone_number_id,
            )
        self.tenants.append(workspace.tenant_id)
        return workspace

    async def plan(self, limits: dict[str, int]) -> str:
        """A catalogue row of this test's own, used as the default plan."""
        code = f"ai-{uuid.uuid4().hex[:10]}"
        async with self.database.session() as session:
            session.add(
                Plan(
                    code=code,
                    name="AI test plan",
                    price=Decimal("0.00"),
                    currency="EGP",
                    interval=BillingInterval.MONTHLY,
                    limits=limits,
                )
            )
        self.plans.append(code)
        self.configure(default_plan_code=code)
        return code

    async def write(
        self,
        workspace: Workspace,
        texts: Sequence[str],
        *,
        wa_id: str = "201555000111",
    ) -> tuple[uuid.UUID, list[uuid.UUID]]:
        """Project `texts` exactly as one webhook delivery would.

        One transaction, one Meta timestamp, payload order - the production
        write path, and the shape in which several messages share a
        `created_at`.
        """
        moment = datetime.now(UTC).replace(microsecond=0)
        async with self.database.session() as session:
            projection = ConversationProjectionService(
                session=session, tenant_id=workspace.tenant_id
            )
            stored: list[Message] = []
            for text in texts:
                stored.append(
                    await projection.project_message(
                        account_id=workspace.account_id,
                        message=InboundMessage(
                            event_id=f"wamid.{uuid.uuid4().hex}",
                            phone_number_id=workspace.phone_number_id,
                            from_number=wa_id,
                            message_type="text",
                            timestamp=moment,
                            text=text,
                            raw={"type": "text"},
                        ),
                    )
                )
            await session.flush()
            return stored[0].conversation_id, [message.id for message in stored]

    def worker(self, database: Database | None = None) -> AgentWorker:
        worker = AgentWorker(
            database=database or self.database,
            redis=_redis_client(self.settings),
            settings=self.settings,
        )
        worker._queue = AgentQueue(
            self.redis, namespace=self.namespace, visibility_timeout_seconds=60
        )
        return worker

    async def enqueue(
        self,
        workspace: Workspace,
        conversation_id: uuid.UUID,
        trigger_message_id: uuid.UUID | None,
    ) -> None:
        queue = AgentQueue(self.redis, namespace=self.namespace, visibility_timeout_seconds=60)
        await queue.enqueue(
            AgentJob(
                tenant_id=workspace.tenant_id,
                conversation_id=conversation_id,
                trigger_message_id=trigger_message_id,
            )
        )

    async def answer(
        self,
        workspace: Workspace,
        conversation_id: uuid.UUID,
        trigger_message_id: uuid.UUID | None,
    ) -> int:
        """Enqueue one turn and drain the queue with one worker."""
        await self.enqueue(workspace, conversation_id, trigger_message_id)
        return await self.drain()

    async def drain(self, *, budget: int = 10) -> int:
        worker = self.worker()
        consumed = 0
        while consumed < budget and await worker.run_once(wait_seconds=1):
            consumed += 1
        return consumed

    async def race(self, workers: int) -> int:
        """Run `workers` workers at once, each on a connection pool of its own."""
        databases = [Database(self.settings) for _ in range(workers)]
        try:
            results = await asyncio.gather(
                *(self.worker(database).run_once(wait_seconds=1) for database in databases)
            )
        finally:
            for database in databases:
                await database.dispose()
        return sum(1 for result in results if result)

    async def turn_states(self, tenant_id: uuid.UUID) -> list[str]:
        async with self.database.session() as session:
            rows = await session.scalars(
                select(AgentTurn.state).where(AgentTurn.tenant_id == tenant_id)
            )
            return sorted(str(state) for state in rows)

    async def outbound(self, tenant_id: uuid.UUID) -> list[Message]:
        async with self.database.session() as session:
            rows = await session.scalars(
                select(Message)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.direction == MessageDirection.OUTBOUND,
                )
                .order_by(Message.created_at)
            )
            return list(rows)

    async def conversation(self, conversation_id: uuid.UUID) -> Conversation:
        async with self.database.session() as session:
            found = await session.get(Conversation, conversation_id)
            assert found is not None
            return found

    async def usage(self, tenant_id: uuid.UUID) -> dict[str, int]:
        async with self.database.session() as session:
            rows = await session.execute(
                select(UsageEvent.event_type, func.sum(UsageEvent.quantity))
                .where(UsageEvent.tenant_id == tenant_id)
                .group_by(UsageEvent.event_type)
            )
            return {str(kind): int(total) for kind, total in rows.all()}

    async def cleanup(self) -> None:
        async with self.database.session() as session:
            if self.tenants:
                await session.execute(delete(Tenant).where(Tenant.id.in_(self.tenants)))
            if self.plans:
                await session.execute(delete(Plan).where(Plan.code.in_(self.plans)))
        async for key in self.redis.scan_iter(match=f"{self.namespace}*"):
            await self.redis.delete(key)


def _redis_client(settings: Settings) -> Any:
    from app.core.redis import RedisClient

    return RedisClient(settings)


@pytest.fixture
def ai_providers(monkeypatch: pytest.MonkeyPatch) -> FakeProviders:
    """Point the agent worker and the messaging service at the fake hosts."""
    import app.services.messaging_service as messaging_module
    import app.workers.ai_worker as ai_worker_module

    providers = FakeProviders()
    transport = providers.transport()

    def openai(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="https://api.openai.com")

    def meta(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, base_url="https://graph.facebook.com")

    monkeypatch.setattr(ai_worker_module, "build_http_client", openai)
    monkeypatch.setattr(messaging_module, "build_http_client", meta)
    return providers


@pytest_asyncio.fixture
async def ai_turns(prepared_database: str) -> AsyncIterator[TurnRunner]:
    settings = settings_for(prepared_database)
    database = Database(settings)
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    runner = TurnRunner(
        url=prepared_database,
        database=database,
        redis=redis,
        namespace=f"test:ai:{uuid.uuid4().hex[:10]}",
        settings=settings,
    )
    try:
        yield runner
    finally:
        try:
            await runner.cleanup()
        finally:
            await redis.aclose()
            await database.dispose()
