"""A campaigns page polling an unchanged campaign spends no campaign budget (RT-07).

The real route, the real limiter on a real Redis (this stage's own, database
15) and a real campaign. The campaign bucket is two a minute here, so a 304
that cost a token would be refused by the third poll; the workspace bucket is
sized to the polls exactly, so the poll after them proves 304s still count
there.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.core.redis import RedisClient
from app.db.models.enums import TenantRole
from app.main import create_app
from tests.integration.campaign_harness import (
    PLATFORM_TOKEN,
    FakeMeta,
    Workspace,
    compose,
    erase,
    seed,
    worker,
)
from tests.integration.test_manual_marketing_templates import _person

pytestmark = pytest.mark.integration

PROGRESS = "/api/v1/campaigns/progress"
POLLS = 60


@pytest_asyncio.fixture
async def page(prepared_database: str) -> AsyncIterator[tuple[AsyncClient, dict[str, str], str]]:
    url = os.environ.get("WASLA_TEST_REDIS_URL")
    if not url:
        pytest.skip("No test Redis: set WASLA_TEST_REDIS_URL.")
    meta = FakeMeta()
    _, database = worker(prepared_database, meta, name="progress-budget")
    configured = Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=True,
        rate_limit_campaign_per_minute=2,
        # The polls, the two answers that changed, nothing more.
        rate_limit_workspace_per_minute=POLLS + 3,
        database_url=prepared_database,
        redis_url=url,
        meta_access_token=PLATFORM_TOKEN,
    )
    application = create_app(configured)
    application.state.database = database
    application.state.redis = RedisClient(configured)
    application.dependency_overrides[get_settings_from_state] = lambda: configured
    workspace: Workspace = await seed(database, recipients=2)
    campaign_id = await compose(database, workspace, schedule=False)
    headers = await _person(database, configured, workspace, TenantRole.TENANT_ADMIN)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://wasla.test"
        ) as client:
            yield client, headers, str(campaign_id)
    finally:
        await _forget(url, workspace)
        await application.state.redis.close()
        await erase(database)
        await database.dispose()


async def _forget(url: str, workspace: Workspace) -> None:
    redis = Redis.from_url(url, decode_responses=True)
    try:
        async for key in redis.scan_iter(match=f"*{workspace.tenant_id}*"):
            await redis.delete(key)
    finally:
        await redis.aclose()


async def test_unchanged_answers_cost_no_campaign_token(
    page: tuple[AsyncClient, dict[str, str], str],
) -> None:
    client, headers, campaign_id = page
    params = {"ids": campaign_id}

    first = await client.get(PROGRESS, params=params, headers=headers)
    assert first.status_code == 200, first.text  # campaign token 1 of 2
    tag = first.headers["ETag"]

    statuses = [
        (
            await client.get(PROGRESS, params=params, headers={**headers, "If-None-Match": tag})
        ).status_code
        for _ in range(POLLS)
    ]
    assert statuses == [304] * POLLS, "a conditional hit is never refused by the campaign bucket"

    assert (await client.get(PROGRESS, params=params, headers=headers)).status_code == 200  # 2 of 2
    third = await client.get(PROGRESS, params=params, headers=headers)
    assert third.status_code == 429, "a changed answer still costs a campaign token"
    assert int(third.headers["Retry-After"]) > 0


async def test_unchanged_answers_still_count_in_the_workspace_bucket(
    page: tuple[AsyncClient, dict[str, str], str],
) -> None:
    client, headers, campaign_id = page
    params = {"ids": campaign_id}
    tag = (await client.get(PROGRESS, params=params, headers=headers)).headers["ETag"]
    for _ in range(POLLS + 2):
        answer = await client.get(
            PROGRESS, params=params, headers={**headers, "If-None-Match": tag}
        )
        assert answer.status_code == 304
    # 1 + 62 = 63 = the workspace's minute: the next request of any route is refused.
    refused = await client.get("/api/v1/conversations", headers=headers)
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "rate_limited"


async def test_a_stale_etag_is_charged_like_any_answer(
    page: tuple[AsyncClient, dict[str, str], str],
) -> None:
    client, headers, campaign_id = page
    params = {"ids": campaign_id}
    stale = {**headers, "If-None-Match": f'"{uuid.uuid4().hex}"'}
    assert (await client.get(PROGRESS, params=params, headers=stale)).status_code == 200
    assert (await client.get(PROGRESS, params=params, headers=stale)).status_code == 200
    assert (await client.get(PROGRESS, params=params, headers=stale)).status_code == 429
