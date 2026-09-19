"""NUL in the remaining free-text surfaces is a 422, not a 500 (SEC-04).

PostgreSQL's `text` cannot hold `U+0000`, and a NUL that reaches it - stored or
merely compared - fails the statement with a `DBAPIError`: a 500, an alert, and
a rollback. The audit found four authenticated inputs that still did that after
the CRM and knowledge fields had been closed. Each is driven here through the
real route against a real database, and each case asserts that nothing was
written and that the refusal was not an unhandled error.

The rule is the existing `StorableText` contract (`app.core.text_safety`), not a
second one. It refuses NUL and unencodable text and keeps everything else a
person can type - so the same tests pin that Arabic, emoji and SQL
metacharacters still work, at the length limits.

`tests/unit/test_request_text_storability.py` makes the rule structural for
every request field; this file proves it where a caller meets it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.telemetry import UNHANDLED_ERRORS
from app.db.models import Tenant
from app.db.models.conversation import Message
from app.schemas.auth import MAXIMUM_NAME_LENGTH
from tests.conftest import AllowingEntitlements, FakeDependency
from tests.integration.test_crm_integrity import Desk, _desk

pytestmark = pytest.mark.integration

API = "/api/v1"
NUL = "a\x00b"


@pytest.fixture
def app(
    settings: Settings, db_session: AsyncSession, fake_redis: FakeDependency
) -> Iterator[FastAPI]:
    from app.main import create_app

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


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def desk(db_session: AsyncSession, settings: Settings) -> Desk:
    return await _desk(db_session, settings, slug=f"nul-{uuid.uuid4().hex[:8]}")


async def _count(session: AsyncSession, model: type[Tenant] | type[Message]) -> int:
    counted = await session.scalar(select(func.count()).select_from(model))
    return int(counted or 0)


# ------------------------------------------------------ the four audited inputs


async def test_nul_in_a_new_workspace_name_is_refused(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    before, crashed = await _count(db_session, Tenant), UNHANDLED_ERRORS.value()

    response = await http.post(
        f"{API}/workspaces",
        json={"name": NUL, "slug": f"nul-new-{uuid.uuid4().hex[:6]}"},
        headers=desk.headers,
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["errors"][0]["loc"][-1] == "name"
    assert await _count(db_session, Tenant) == before
    assert UNHANDLED_ERRORS.value() == crashed


async def test_nul_in_a_workspace_rename_is_refused(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    crashed = UNHANDLED_ERRORS.value()

    response = await http.patch(f"{API}/workspace", json={"name": NUL}, headers=desk.headers)

    assert response.status_code == 422, response.text
    await db_session.refresh(desk.tenant)
    assert "\x00" not in desk.tenant.name
    assert UNHANDLED_ERRORS.value() == crashed


async def test_nul_in_an_outbound_message_is_refused(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    before, crashed = await _count(db_session, Message), UNHANDLED_ERRORS.value()

    response = await http.post(
        f"{API}/conversations/{desk.conversation.id}/messages",
        json={"body": NUL},
        headers=desk.member_headers,
    )

    assert response.status_code == 422, response.text
    assert await _count(db_session, Message) == before
    assert UNHANDLED_ERRORS.value() == crashed


@pytest.mark.parametrize("parameter", ["search", "tag"])
async def test_nul_in_a_lead_filter_is_refused(
    http: AsyncClient, desk: Desk, parameter: str
) -> None:
    crashed = UNHANDLED_ERRORS.value()

    response = await http.get(f"{API}/leads", params={parameter: NUL}, headers=desk.headers)

    assert response.status_code == 422, response.text
    assert UNHANDLED_ERRORS.value() == crashed


# --------------------------------------------- what a person may type is kept


@pytest.mark.parametrize(
    "search",
    ["محمد", "🙂", "' OR 1=1-- %_", "a\\b", "x" * 200],
    ids=["arabic", "emoji", "sql-metacharacters", "backslash", "max-length"],
)
async def test_ordinary_search_text_is_still_searched(
    http: AsyncClient, desk: Desk, search: str
) -> None:
    response = await http.get(f"{API}/leads", params={"search": search}, headers=desk.headers)

    assert response.status_code == 200, response.text


async def test_a_search_one_character_too_long_is_refused(http: AsyncClient, desk: Desk) -> None:
    response = await http.get(f"{API}/leads", params={"search": "x" * 201}, headers=desk.headers)

    assert response.status_code == 422


async def test_an_arabic_and_emoji_workspace_name_at_the_limit_is_kept(
    http: AsyncClient, desk: Desk, db_session: AsyncSession
) -> None:
    name = ("شركة وصلة 🙂 " * 40)[:MAXIMUM_NAME_LENGTH]

    response = await http.patch(f"{API}/workspace", json={"name": name}, headers=desk.headers)

    assert response.status_code == 200, response.text
    await db_session.refresh(desk.tenant)
    assert desk.tenant.name == name


async def test_a_workspace_name_one_character_too_long_is_refused(
    http: AsyncClient, desk: Desk
) -> None:
    response = await http.patch(
        f"{API}/workspace", json={"name": "ع" * (MAXIMUM_NAME_LENGTH + 1)}, headers=desk.headers
    )

    assert response.status_code == 422
