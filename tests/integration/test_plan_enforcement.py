"""What a full plan actually stops, and what it deliberately does not.

The second half matters as much as the first. A limit that refused a customer's
inbound message, or dead-lettered an agent job, would turn a billing problem
into lost words and stuck queues — so those paths are asserted to keep working
while the plan is exhausted (ADR-030).

The guard is a dependency, so these drive it through the real routes with the
real dependency wiring. Only the entitlement service is replaced, by one that
refuses.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    ActiveWorkspace,
    get_active_workspace,
    get_entitlement_service,
)
from app.core.exceptions import PlanLimitExceededError
from app.db.models import Membership, Tenant, TenantRole, TenantStatus, User
from app.db.models.billing import LimitKey
from app.services.entitlement_service import Entitlement
from tests.fake_queue_redis import FakeQueueRedis
from tests.fakes import as_database, as_redis_client

pytestmark = pytest.mark.integration

TENANT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class ExhaustedEntitlements:
    """Refuses everything, the way a workspace on a full plan is refused."""

    def __init__(self) -> None:
        self.asked: list[LimitKey] = []

    async def check(self, key: LimitKey, *, additional: int = 1) -> Entitlement:
        self.asked.append(key)
        return Entitlement(key=key, limit=1, used=1, allowed=False, plan_code="starter")

    async def require(self, key: LimitKey, *, additional: int = 1) -> Entitlement:
        # Recorded, then refused: the real service does the same.
        await self.check(key, additional=additional)
        raise PlanLimitExceededError(
            f"This workspace's plan allows 1 {key.value}, and 1 has been used. "
            "Upgrade the plan to continue."
        )

    async def allows(self, key: LimitKey, *, additional: int = 1) -> bool:
        self.asked.append(key)
        return False

    async def snapshot(self, keys: Sequence[LimitKey] | None = None) -> list[Entitlement]:
        return [await self.check(key, additional=0) for key in LimitKey]


def _workspace(role: TenantRole = TenantRole.TENANT_OWNER) -> ActiveWorkspace:
    return ActiveWorkspace(
        user=User(id=USER_ID, email="owner@example.com", is_active=True),
        membership=Membership(id=uuid.uuid4(), user_id=USER_ID, tenant_id=TENANT_ID, role=role),
        tenant=Tenant(id=TENANT_ID, name="Acme", slug="acme", status=TenantStatus.ACTIVE),
    )


@pytest.fixture
def exhausted(app: FastAPI) -> ExhaustedEntitlements:
    stub = ExhaustedEntitlements()
    app.dependency_overrides[get_entitlement_service] = lambda: stub
    app.dependency_overrides[get_active_workspace] = lambda: _workspace()
    return stub


# ----------------------------------------------------------------- refusals


async def test_a_full_plan_refuses_another_agent(
    client: AsyncClient, app: FastAPI, exhausted: ExhaustedEntitlements
) -> None:
    response = await client.post(
        "/api/v1/agents",
        json={"name": "Second", "system_prompt": "Be helpful."},
    )

    assert response.status_code == 402
    assert response.json()["error"]["code"] == "plan_limit_exceeded"
    assert exhausted.asked == [LimitKey.AGENTS]


async def test_the_refusal_says_what_to_do_about_it(
    client: AsyncClient, app: FastAPI, exhausted: ExhaustedEntitlements
) -> None:
    """402 rather than 403 because the answer is "upgrade", not "ask an
    administrator" - a client that cannot tell them apart shows the wrong
    dialogue to somebody trying to give us money."""
    response = await client.post(
        "/api/v1/agents",
        json={"name": "Second", "system_prompt": "Be helpful."},
    )

    assert "Upgrade" in response.json()["error"]["message"]


async def test_a_full_plan_refuses_another_number(
    client: AsyncClient, app: FastAPI, exhausted: ExhaustedEntitlements
) -> None:
    response = await client.post(
        "/api/v1/whatsapp/accounts",
        json={
            "phone_number_id": "109876543210",
            "waba_id": "555000111",
            "display_phone_number": "+201000000000",
        },
    )

    assert response.status_code == 402
    assert exhausted.asked == [LimitKey.WHATSAPP_NUMBERS]


async def test_a_full_plan_refuses_another_colleague(
    client: AsyncClient, app: FastAPI, exhausted: ExhaustedEntitlements
) -> None:
    """Checked when the invitation is issued rather than when it is accepted:
    refusing somebody at the moment they click is worse than telling the
    inviter now."""
    response = await client.post(
        "/api/v1/invitations",
        json={"email": "colleague@example.com", "role": "member"},
    )

    assert response.status_code == 402
    assert exhausted.asked == [LimitKey.TEAM_MEMBERS]


async def test_a_full_plan_refuses_another_document(
    client: AsyncClient, app: FastAPI, exhausted: ExhaustedEntitlements
) -> None:
    response = await client.post(
        f"/api/v1/knowledge/bases/{uuid.uuid4()}/documents",
        json={"title": "Prices", "content": "Everything costs money."},
    )

    assert response.status_code == 402
    assert exhausted.asked == [LimitKey.KNOWLEDGE_DOCUMENTS]


async def test_the_guard_runs_before_the_handler_touches_anything(
    client: AsyncClient, app: FastAPI, exhausted: ExhaustedEntitlements
) -> None:
    """The refusal is a dependency, so nothing in the handler has run - which
    is why an unbound session in these tests never gets queried."""
    response = await client.post(
        "/api/v1/agents",
        json={"name": "Second", "system_prompt": "Be helpful."},
    )

    assert response.status_code == 402
    assert len(exhausted.asked) == 1


# ------------------------------------------------------- deliberately allowed


class StubAgents:
    """Only the read the next test performs."""

    async def list_agents(self, *, limit: int = 50) -> list[Any]:
        return []


async def test_reading_is_never_refused(
    client: AsyncClient, app: FastAPI, exhausted: ExhaustedEntitlements
) -> None:
    """A workspace over its limit can still see what it has. Locking somebody
    out of their own data over a bill is not a limit, it is a hostage."""
    from app.api.dependencies import get_agent_service

    app.dependency_overrides[get_agent_service] = StubAgents

    response = await client.get("/api/v1/agents")

    assert response.status_code == 200
    assert exhausted.asked == []


async def test_a_customers_message_is_never_refused_for_a_billing_reason(
    client: AsyncClient, app: FastAPI, exhausted: ExhaustedEntitlements
) -> None:
    """The inbound path carries no limit check at all, and this is the test that
    says so. Meta retries a non-2xx until it disables the subscription, and the
    words belong to a customer who owes us nothing (ADR-030).
    """
    response = await client.post(
        "/api/v1/webhooks/whatsapp",
        json={"entry": []},
        headers={"X-Hub-Signature-256": "sha256=unverified"},
    )

    # Whatever the signature check decides, no limit was consulted on the way.
    assert exhausted.asked == []
    assert response.status_code != 402


# ------------------------------------------------------------- the AI worker


class SessionHandle:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        yield self._session


class FakeRedis:
    @property
    def client(self) -> FakeQueueRedis:
        return FakeQueueRedis()


async def test_an_exhausted_ai_allowance_stops_the_turn_without_failing_the_job(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workspace out of AI requests has a billing problem; its customer has a
    question. Raising here would dead-letter the job and lose the second."""
    from app.core.config import Settings
    from app.db.models.tenant import Tenant as TenantModel
    from app.workers import ai_worker as worker_module
    from app.workers.ai_worker import _TurnProgress
    from app.workers.queue import AgentJob

    tenant = TenantModel(name="Acme", slug="acme")
    db_session.add(tenant)
    await db_session.flush()

    refused = ExhaustedEntitlements()
    monkeypatch.setattr(worker_module, "EntitlementService", lambda *a, **k: refused)

    def _never(**kwargs: object) -> None:
        raise AssertionError("The turn should not have been composed.")

    monkeypatch.setattr(worker_module, "AgentOrchestrator", _never)

    settings = Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        openai_api_key="test-key",
    )
    worker = worker_module.AgentWorker(
        database=as_database(SessionHandle(db_session)),
        redis=as_redis_client(FakeRedis()),
        settings=settings,
    )

    # Returns rather than raises: the job is released, not dead-lettered.
    progress = _TurnProgress()
    await worker._handle(AgentJob(tenant_id=tenant.id, conversation_id=uuid.uuid4()), progress)

    # And it never reached the provider, so had it failed instead of returning
    # it would still have been retryable (ADR-068). A workspace out of allowance
    # is a billing problem, not a reason to burn a retry budget.
    assert progress.engaged is False
    assert refused.asked == [LimitKey.PERIOD_AI_REQUESTS]
