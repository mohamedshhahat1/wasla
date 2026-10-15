"""Members cannot broadcast marketing templates; nobody sends one after a STOP (CMQ-02).

The audit (section 6) found `POST /conversations/{id}/messages/template` the one
non-reply producer sharing none of a campaign's guarantees: any member, no
opt-out check, never refused - a 300-a-minute broadcast surface that ignored
opt-out. Decision (b): a MARKETING template sent by hand is admin-only and is
refused before staging for a contact opted out on the conversation's channel.
Utility and authentication templates are unchanged for everybody.

Through the real ASGI application with real access tokens; Meta is replaced at
the transport and every request that reaches it is counted.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.core.security import create_access_token
from app.db.models.campaign import OptOutSource, OptOutVia
from app.db.models.channel import Channel
from app.db.models.conversation import Conversation
from app.db.models.enums import MembershipStatus, TenantRole
from app.db.models.membership import Membership
from app.db.models.user import User
from app.db.models.whatsapp_template import TemplateCategory, WhatsAppTemplate
from app.db.session import Database
from app.main import create_app
from app.services.opt_out import record_opt_out
from tests.conftest import FakeDependency
from tests.integration.campaign_harness import (
    PLATFORM_TOKEN,
    USER_DOMAIN,
    FakeMeta,
    Workspace,
    erase,
    seed,
    worker,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def world(
    prepared_database: str,
    settings: Settings,
    fake_redis: FakeDependency,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[FakeMeta, Database, AsyncClient, Settings]]:
    import app.services.messaging_service as messaging_module

    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="manual")
    monkeypatch.setattr(messaging_module, "build_http_client", lambda: meta.client("manual"))
    configured = settings.model_copy(update={"meta_access_token": PLATFORM_TOKEN})
    application: FastAPI = create_app(configured)
    application.state.database = database
    application.state.redis = fake_redis
    application.dependency_overrides[get_settings_from_state] = lambda: configured
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://wasla.test"
        ) as client:
            yield meta, database, client, configured
    finally:
        await erase(database)
        await database.dispose()


async def _person(
    database: Database, settings: Settings, workspace: Workspace, role: TenantRole
) -> dict[str, str]:
    async with database.session_factory() as session:
        user = User(
            email=f"{role.value}-{uuid.uuid4().hex[:8]}{USER_DOMAIN}",
            hashed_password="x",
            is_active=True,
            email_verified_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        session.add(
            Membership(
                tenant_id=workspace.tenant_id,
                user_id=user.id,
                role=role,
                status=MembershipStatus.ACTIVE,
            )
        )
        await session.commit()
        token, _ = create_access_token(
            settings=settings,
            subject=user.id,
            tenant_id=workspace.tenant_id,
            token_version=user.token_version,
        )
    return {"Authorization": f"Bearer {token}"}


async def _conversation(database: Database, workspace: Workspace) -> Conversation:
    async with database.session_factory() as session:
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.account_id == workspace.account_id)
            )
        ).scalar_one()
        return conversation


async def _template(database: Database, workspace: Workspace) -> WhatsAppTemplate:
    async with database.session_factory() as session:
        row = await session.get(WhatsAppTemplate, workspace.template_id)
        assert row is not None
        return row


async def _send(
    client: AsyncClient, conversation: Conversation, template: WhatsAppTemplate, headers: Any
) -> Any:
    return await client.post(
        f"/api/v1/conversations/{conversation.id}/messages/template",
        json={"name": template.name, "language": template.language},
        headers=headers,
    )


async def _opt_out(database: Database, workspace: Workspace, conversation: Conversation) -> None:
    async with database.session_factory() as session:
        await record_opt_out(
            session,
            tenant_id=workspace.tenant_id,
            contact_id=conversation.contact_id,
            channel=Channel.WHATSAPP,
            source=OptOutSource.CUSTOMER,
            via=OptOutVia.MESSAGE,
            at=conversation.created_at,
        )
        await session.commit()


async def test_a_member_cannot_send_a_marketing_template(
    world: tuple[FakeMeta, Database, AsyncClient, Settings],
) -> None:
    meta, database, client, settings = world
    workspace = await seed(database, recipients=1)
    member = await _person(database, settings, workspace, TenantRole.MEMBER)

    response = await _send(
        client,
        await _conversation(database, workspace),
        await _template(database, workspace),
        member,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "marketing_template_admin_only"
    assert len(meta.requests) == 0


async def test_an_admin_cannot_send_a_marketing_template_to_an_opted_out_contact(
    world: tuple[FakeMeta, Database, AsyncClient, Settings],
) -> None:
    meta, database, client, settings = world
    workspace = await seed(database, recipients=1)
    admin = await _person(database, settings, workspace, TenantRole.TENANT_ADMIN)
    conversation = await _conversation(database, workspace)
    await _opt_out(database, workspace, conversation)

    response = await _send(client, conversation, await _template(database, workspace), admin)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "recipient_opted_out"
    assert len(meta.requests) == 0


async def test_an_admin_sends_a_marketing_template_to_a_contact_who_has_not_opted_out(
    world: tuple[FakeMeta, Database, AsyncClient, Settings],
) -> None:
    meta, database, client, settings = world
    workspace = await seed(database, recipients=1)
    admin = await _person(database, settings, workspace, TenantRole.TENANT_OWNER)

    response = await _send(
        client,
        await _conversation(database, workspace),
        await _template(database, workspace),
        admin,
    )

    assert response.status_code == 201, response.text
    assert len(meta.requests) == 1


@pytest.mark.parametrize("category", [TemplateCategory.UTILITY, TemplateCategory.AUTHENTICATION])
async def test_utility_and_authentication_templates_are_unchanged_for_members(
    world: tuple[FakeMeta, Database, AsyncClient, Settings], category: TemplateCategory
) -> None:
    """Not a broadcast surface: an order update to a customer who opted out of marketing."""
    meta, database, client, settings = world
    workspace = await seed(database, recipients=1, category=category)
    member = await _person(database, settings, workspace, TenantRole.MEMBER)
    conversation = await _conversation(database, workspace)
    await _opt_out(database, workspace, conversation)

    response = await _send(client, conversation, await _template(database, workspace), member)

    assert response.status_code == 201, response.text
    assert len(meta.requests) == 1
