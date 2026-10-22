"""The draft lifecycle over the real API and database (PG-01 .. PG-05, PG-08).

Edit, delete, duplicate, an idempotent create, the name search and the
activity log - as the campaigns page calls them.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.db.models.campaign import (
    Campaign,
    CampaignCreateKey,
    CampaignRecipient,
    CampaignStatus,
)
from app.db.models.enums import TenantRole
from app.db.models.whatsapp_template import TemplateStatus, WhatsAppTemplate
from app.db.session import Database
from tests.integration.campaign_api import API, client_for, member_of
from tests.integration.campaign_harness import (
    FakeMeta,
    SimClock,
    Workspace,
    compose,
    erase,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration

BODY = "Hello {{1}}."
COMPONENTS: list[dict[str, Any]] = [{"type": "BODY", "text": BODY}]


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    _, built = worker(prepared_database, FakeMeta(), name="drafts")
    await quiesce(built)
    try:
        yield built
    finally:
        await quiesce(built)
        await erase(built)
        await built.dispose()


async def _workspace(database: Database, recipients: int = 3) -> Workspace:
    return await seed(
        database, recipients=recipients, components=COMPONENTS, body_text=BODY, variable_count=1
    )


def _create_body(workspace: Workspace, **fields: Any) -> dict[str, Any]:
    body = {
        "account_id": str(workspace.account_id),
        "template_id": str(workspace.template_id),
        "name": "Spring sale",
        "variables": ["friend"],
        **fields,
    }
    return {key: value for key, value in body.items() if value is not None}


async def _draft(client: Any, workspace: Workspace, **fields: Any) -> dict[str, Any]:
    created = await client.post(f"{API}/campaigns", json=_create_body(workspace, **fields))
    assert created.status_code == 201, created.text
    return dict(created.json())


async def _row(database: Database, campaign_id: str) -> Campaign | None:
    async with database.session_factory() as session:
        return await session.get(Campaign, uuid.UUID(campaign_id))


async def _recipients(database: Database, campaign_id: str) -> int:
    async with database.session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(CampaignRecipient)
                .where(CampaignRecipient.campaign_id == uuid.UUID(campaign_id))
            )
            or 0
        )


# ------------------------------------------------------------------ edit


async def test_a_draft_is_edited_from_the_version_read_and_a_stale_edit_is_refused(
    database: Database,
) -> None:
    workspace = await _workspace(database)
    async with client_for(database, workspace) as client:
        draft = await _draft(client, workspace)
        assert draft["version"] == 1
        path = f"{API}/campaigns/{draft['id']}"

        edited = await client.patch(
            path, headers={"If-Match": '"1"'}, json={"name": "Spring sale, final"}
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["name"] == "Spring sale, final"
        assert edited.json()["version"] == 2
        assert edited.headers["ETag"] == '"2"'

        stale = await client.patch(path, json={"name": "Lost update", "version": 1})
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "campaign_version_conflict"
        assert stale.json()["error"]["details"]["version"] == 2

        unversioned = await client.patch(path, json={"name": "No version"})
        assert unversioned.status_code == 422
        assert unversioned.json()["error"]["code"] == "campaign_version_required"

        status_field = await client.patch(path, json={"status": "running", "version": 2})
        assert status_field.status_code == 422
    row = await _row(database, draft["id"])
    assert row is not None and row.name == "Spring sale, final" and row.version == 2


async def test_a_composition_edit_clears_the_written_list_and_a_name_edit_keeps_it(
    database: Database,
) -> None:
    workspace = await _workspace(database, recipients=4)
    async with client_for(database, workspace) as client:
        draft = await _draft(client, workspace)
        path = f"{API}/campaigns/{draft['id']}"
        audience = await client.post(f"{path}/audience", json={"last_inbound_within_days": 30})
        assert audience.status_code == 200, audience.text
        version = audience.json()["version"]
        assert audience.json()["audience_ready"] is True

        renamed = await client.patch(path, json={"name": "Renamed", "version": version})
        assert renamed.status_code == 200
        assert renamed.json()["audience_cleared"] is False
        assert renamed.json()["audience_size"] == 4
        assert await _recipients(database, draft["id"]) == 4

        revalued = await client.patch(
            path, json={"variables": ["dear customer"], "version": renamed.json()["version"]}
        )
        assert revalued.status_code == 200, revalued.text
        body = revalued.json()
        assert body["audience_cleared"] is True
        assert body["audience_size"] == 0 and body["audience_ready"] is False
        # The rules stay, so the page can write the list again.
        assert body["audience"]["last_inbound_within_days"] == 30
        assert body["variables"] == ["dear customer"]
    assert await _recipients(database, draft["id"]) == 0


async def test_an_edit_is_validated_like_a_create(database: Database) -> None:
    workspace = await _workspace(database)
    async with client_for(database, workspace) as client:
        draft = await _draft(client, workspace)
        path = f"{API}/campaigns/{draft['id']}"
        wrong_count = await client.patch(path, json={"variables": ["a", "b"], "version": 1})
        assert wrong_count.status_code == 422
        created_wrong = await client.post(
            f"{API}/campaigns", json=_create_body(workspace, variables=["a", "b"])
        )
        assert created_wrong.status_code == 422
        assert wrong_count.json()["error"]["code"] == created_wrong.json()["error"]["code"]
        rate = await client.patch(path, json={"messages_per_minute": 0, "version": 1})
        assert rate.status_code == 422
    row = await _row(database, draft["id"])
    assert row is not None and row.version == 1 and row.variables == ["friend"]


async def test_only_a_draft_is_editable_or_deletable(
    database: Database, prepared_database: str
) -> None:
    meta = FakeMeta()
    workspace = await _workspace(database, recipients=2)
    campaign_id = str(await compose(database, workspace, variables=["friend"]))
    async with client_for(database, workspace) as client:
        path = f"{API}/campaigns/{campaign_id}"
        edit = await client.patch(path, json={"name": "Too late", "version": 1})
        assert edit.status_code == 409
        assert edit.json()["error"]["code"] == "campaign_not_editable"
        removed = await client.delete(path)
        assert removed.status_code == 409
        assert removed.json()["error"]["code"] == "campaign_not_deletable"

        replica, own = worker(prepared_database, meta, name="send")
        try:
            await SimClock(now=datetime.now(UTC) + timedelta(seconds=1)).sweep(replica)
        finally:
            await own.dispose()
        assert (await client.get(path)).json()["status"] == "completed"
        assert (await client.delete(path)).status_code == 409


async def test_a_draft_is_deleted_and_one_with_a_sent_copy_never_is(database: Database) -> None:
    workspace = await _workspace(database, recipients=2)
    async with client_for(database, workspace) as client:
        draft = await _draft(client, workspace)
        await client.post(f"{API}/campaigns/{draft['id']}/audience", json={})
        removed = await client.delete(f"{API}/campaigns/{draft['id']}")
        assert removed.status_code == 204
        assert (await client.get(f"{API}/campaigns/{draft['id']}")).status_code == 404
        assert await _recipients(database, draft["id"]) == 0

        # A draft whose list says a copy was ever handed over is not deletable,
        # whatever its status says.
        other = await _draft(client, workspace)
        await client.post(f"{API}/campaigns/{other['id']}/audience", json={})
        async with database.session_factory() as session:
            await session.execute(
                update(CampaignRecipient)
                .where(CampaignRecipient.campaign_id == uuid.UUID(other["id"]))
                .values(attempts=1)
            )
            await session.commit()
        refused = await client.delete(f"{API}/campaigns/{other['id']}")
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "campaign_not_deletable"


# ------------------------------------------------------------- duplicate


async def test_a_duplicate_is_a_new_draft_with_rules_and_values_and_nothing_else(
    database: Database, prepared_database: str
) -> None:
    meta = FakeMeta()
    workspace = await _workspace(database, recipients=3)
    async with client_for(database, workspace) as client:
        source = await _draft(
            client,
            workspace,
            variables=None,
            body_parameters=[{"source": "contact.display_name", "fallback": "friend"}],
        )
        await client.post(
            f"{API}/campaigns/{source['id']}/audience", json={"last_inbound_within_days": 30}
        )
        scheduled = await client.post(f"{API}/campaigns/{source['id']}/schedule", json={})
        assert scheduled.status_code == 200, scheduled.text
        replica, own = worker(prepared_database, meta, name="send")
        try:
            await SimClock(now=datetime.now(UTC) + timedelta(seconds=1)).sweep(replica)
        finally:
            await own.dispose()

        copy = await client.post(f"{API}/campaigns/{source['id']}/duplicate")
        assert copy.status_code == 201, copy.text
        body = copy.json()
        assert body["id"] != source["id"]
        assert body["name"] == "Spring sale (copy)"
        assert body["status"] == "draft"
        assert body["version"] == 1
        assert body["audience"]["last_inbound_within_days"] == 30
        assert body["audience_size"] == 0 and body["audience_ready"] is False
        assert body["template_parameters"]["body_sources"][0]["source"] == "contact.display_name"
        assert body["scheduled_at"] is None and body["started_at"] is None
        assert body["completed_at"] is None and body["status_reason"] is None
        assert await _recipients(database, body["id"]) == 0

        # Withdrawn since: refused with create's own answer.
        async with database.session_factory() as session:
            await session.execute(
                update(WhatsAppTemplate)
                .where(WhatsAppTemplate.id == workspace.template_id)
                .values(status=TemplateStatus.PAUSED)
            )
            await session.commit()
        refused = await client.post(f"{API}/campaigns/{source['id']}/duplicate")
        created = await client.post(f"{API}/campaigns", json=_create_body(workspace))
        assert refused.status_code == created.status_code == 422
        assert refused.json()["error"]["code"] == created.json()["error"]["code"]


# ------------------------------------------------------- idempotent create


async def test_a_retried_create_with_one_key_makes_one_draft(database: Database) -> None:
    workspace = await _workspace(database)
    key = f"step-1-{uuid.uuid4()}"
    async with client_for(database, workspace) as client:
        first = await client.post(
            f"{API}/campaigns", json=_create_body(workspace), headers={"Idempotency-Key": key}
        )
        again = await client.post(
            f"{API}/campaigns", json=_create_body(workspace), headers={"Idempotency-Key": key}
        )
        assert first.status_code == again.status_code == 201
        assert first.json()["id"] == again.json()["id"]

        different = await client.post(
            f"{API}/campaigns",
            json=_create_body(workspace, name="Another"),
            headers={"Idempotency-Key": key},
        )
        assert different.status_code == 409
        assert different.json()["error"]["code"] == "idempotency_key_reused"

        # Two retries at once still make one.
        racing = f"race-{uuid.uuid4()}"
        answers = await asyncio.gather(
            *(
                client.post(
                    f"{API}/campaigns",
                    json=_create_body(workspace, name="Raced"),
                    headers={"Idempotency-Key": racing},
                )
                for _ in range(4)
            )
        )
        assert {answer.status_code for answer in answers} == {201}
        assert len({answer.json()["id"] for answer in answers}) == 1

    async with database.session_factory() as session:
        drafts = await session.scalar(
            select(func.count())
            .select_from(Campaign)
            .where(Campaign.tenant_id == workspace.tenant_id, Campaign.name == "Raced")
        )
        keys = await session.scalar(
            select(func.count())
            .select_from(CampaignCreateKey)
            .where(CampaignCreateKey.tenant_id == workspace.tenant_id)
        )
    assert drafts == 1 and keys == 2


async def test_a_key_older_than_a_day_names_nothing(database: Database) -> None:
    workspace = await _workspace(database)
    key = f"old-{uuid.uuid4()}"
    async with client_for(database, workspace) as client:
        first = await client.post(
            f"{API}/campaigns", json=_create_body(workspace), headers={"Idempotency-Key": key}
        )
        async with database.session_factory() as session:
            await session.execute(
                update(CampaignCreateKey)
                .where(CampaignCreateKey.key == key)
                .values(created_at=datetime.now(UTC) - timedelta(hours=25))
            )
            await session.commit()
        second = await client.post(
            f"{API}/campaigns", json=_create_body(workspace), headers={"Idempotency-Key": key}
        )
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]


# ----------------------------------------------------------------- search


async def test_search_matches_names_literally_across_pages_and_never_another_workspace(
    database: Database,
) -> None:
    workspace = await _workspace(database)
    stranger = await _workspace(database)
    async with client_for(database, stranger) as client:
        await _draft(client, stranger, name="Spring 100% sale")
    async with client_for(database, workspace) as client:
        for name in ("Spring 100% sale", "Spring 100 sale", "spring_promo", "Winter", "SPRING x"):
            await _draft(client, workspace, name=name)

        percent = await client.get(f"{API}/campaigns", params={"q": "100%"})
        assert [item["name"] for item in percent.json()["items"]] == ["Spring 100% sale"]
        underscore = await client.get(f"{API}/campaigns", params={"q": "g_p"})
        assert [item["name"] for item in underscore.json()["items"]] == ["spring_promo"]

        seen: list[str] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"q": "spring", "limit": 1}
            if cursor:
                params["cursor"] = cursor
            page = (await client.get(f"{API}/campaigns", params=params)).json()
            seen += [item["name"] for item in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert sorted(seen) == sorted(
            ["Spring 100% sale", "Spring 100 sale", "spring_promo", "SPRING x"]
        )
        too_long = await client.get(f"{API}/campaigns", params={"q": "x" * 101})
        assert too_long.status_code == 422


# --------------------------------------------------------------- activity


async def test_the_activity_lists_every_transition_once_with_its_actor_and_no_contacts(
    database: Database, prepared_database: str
) -> None:
    meta = FakeMeta()
    workspace = await _workspace(database, recipients=2)
    admin = await member_of(database, workspace)
    async with client_for(database, workspace, user_id=admin) as client:
        draft = await _draft(client, workspace)
        path = f"{API}/campaigns/{draft['id']}"
        await client.patch(path, json={"description": "For the spring", "version": 1})
        await client.post(
            f"{path}/audience", json={"contact_ids": [str(c) for c in workspace.contact_ids]}
        )
        await client.post(f"{path}/schedule", json={"scheduled_at": _later().isoformat()})
        await client.post(f"{path}/pause")
        await client.post(f"{path}/schedule", json={})
        replica, own = worker(prepared_database, meta, name="send")
        try:
            await SimClock(now=datetime.now(UTC) + timedelta(seconds=1)).sweep(replica)
        finally:
            await own.dispose()
        await client.post(f"{path}/duplicate")

        entries: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = await client.get(f"{path}/activity", params=params)
            assert page.status_code == 200, page.text
            entries += page.json()["items"]
            cursor = page.json()["next_cursor"]
            if not cursor:
                break

    actions = [entry["action"] for entry in reversed(entries)]
    assert actions == [
        "created",
        "updated",
        "audience_set",
        "scheduled",
        "paused",
        "resumed",
        "completed",
        "duplicated_to",
    ]
    for entry in entries:
        if entry["action"] == "completed":
            assert entry["actor"] == {"type": "system", "id": None, "name": None}
        else:
            assert entry["actor"]["type"] == "user"
            assert entry["actor"]["id"] == str(admin)
            assert entry["actor"]["name"] == "Page Tester"
        assert entry["occurred_at_local"].endswith(("+02:00", "+03:00"))
    updated = next(entry for entry in entries if entry["action"] == "updated")
    assert updated["metadata"] == {"fields": ["description"], "audience_cleared": False}
    audience = next(entry for entry in entries if entry["action"] == "audience_set")
    assert audience["metadata"]["filters"]["contact_ids"] == 2
    text = str(entries)
    for contact_id in workspace.contact_ids:
        assert str(contact_id) not in text


def _later() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


# ------------------------------------------------------------------ roles


async def test_a_member_reads_the_activity_and_changes_nothing(database: Database) -> None:
    workspace = await _workspace(database)
    async with client_for(database, workspace) as admin:
        draft = await _draft(admin, workspace)
    async with client_for(database, workspace, role=TenantRole.MEMBER) as member:
        path = f"{API}/campaigns/{draft['id']}"
        assert (await member.patch(path, json={"name": "x", "version": 1})).status_code == 403
        assert (await member.delete(path)).status_code == 403
        assert (await member.post(f"{path}/duplicate")).status_code == 403
        assert (await member.get(f"{path}/activity")).status_code == 200
        assert (await member.get(f"{API}/campaigns", params={"q": "spring"})).status_code == 200
    row = await _row(database, draft["id"])
    assert row is not None and row.status is CampaignStatus.DRAFT and row.version == 1


async def test_another_workspace_s_campaign_is_not_found_for_any_draft_route(
    database: Database,
) -> None:
    workspace = await _workspace(database)
    stranger = await _workspace(database)
    async with client_for(database, stranger) as client:
        foreign = await _draft(client, stranger)
    async with client_for(database, workspace) as client:
        path = f"{API}/campaigns/{foreign['id']}"
        assert (await client.patch(path, json={"name": "x", "version": 1})).status_code == 404
        assert (await client.delete(path)).status_code == 404
        assert (await client.post(f"{path}/duplicate")).status_code == 404
        assert (await client.get(f"{path}/activity")).status_code == 404
    row = await _row(database, foreign["id"])
    assert row is not None and row.name == "Spring sale"


async def test_the_retention_sweep_forgets_create_keys_after_a_day(
    database: Database, tmp_path: Any
) -> None:
    from app.core.storage import LocalMediaStorage
    from app.workers.retention_worker import RetentionWorker
    from tests.integration.campaign_harness import harness_settings

    workspace = await _workspace(database)
    async with client_for(database, workspace) as client:
        for key in ("fresh", "stale"):
            await client.post(
                f"{API}/campaigns",
                json=_create_body(workspace, name=key),
                headers={"Idempotency-Key": f"{key}-{workspace.tenant_id}"},
            )
    async with database.session_factory() as session:
        await session.execute(
            update(CampaignCreateKey)
            .where(CampaignCreateKey.key == f"stale-{workspace.tenant_id}")
            .values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )
        await session.commit()
    sweeper = RetentionWorker(
        database=database,
        settings=harness_settings(database.engine.url.render_as_string(hide_password=False)),
        storage=LocalMediaStorage(tmp_path),
    )
    assert await sweeper.prune_campaign_create_keys() >= 1
    async with database.session_factory() as session:
        left = list(
            (
                await session.execute(
                    select(CampaignCreateKey.key).where(
                        CampaignCreateKey.tenant_id == workspace.tenant_id
                    )
                )
            ).scalars()
        )
    assert left == [f"fresh-{workspace.tenant_id}"]
