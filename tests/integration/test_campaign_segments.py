"""Saved segments and the contact picker over the real API (PG-06, PG-07)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from app.db.models.campaign import Campaign, OptOutSource
from app.db.models.campaign_segment import CampaignSegment
from app.db.models.channel import Channel
from app.db.models.consent import ContactChannelConsent
from app.db.models.conversation import Contact, Conversation
from app.db.models.enums import TenantRole
from app.db.models.lead import Lead, LeadStatus
from app.db.session import Database
from tests.integration.campaign_api import API, client_for
from tests.integration.campaign_harness import FakeMeta, Workspace, erase, quiesce, seed, worker

pytestmark = pytest.mark.integration

BODY = "Hello."


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    _, built = worker(prepared_database, FakeMeta(), name="segments")
    await quiesce(built)
    try:
        yield built
    finally:
        await quiesce(built)
        async with built.session_factory() as session:
            await session.execute(
                update(Campaign)
                .where(Campaign.audience_segment_id.is_not(None))
                .values(audience_segment_id=None)
            )
            await session.commit()
        await erase(built)
        await built.dispose()


async def _people(database: Database, count: int, **seeded: Any) -> Workspace:
    return await seed(database, recipients=count, **seeded)


async def _wrote(database: Database, contact_id: uuid.UUID, *, days_ago: float) -> None:
    async with database.session_factory() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.contact_id == contact_id)
            .values(last_inbound_at=datetime.now(UTC) - timedelta(days=days_ago))
        )
        await session.commit()


async def _lead(
    database: Database, workspace: Workspace, contact_id: uuid.UUID, status: LeadStatus, **kw: Any
) -> None:
    async with database.session_factory() as session:
        session.add(Lead(tenant_id=workspace.tenant_id, contact_id=contact_id, status=status, **kw))
        await session.commit()


async def _segment(client: Any, **body: Any) -> dict[str, Any]:
    created = await client.post(f"{API}/campaign-segments", json=body)
    assert created.status_code == 201, created.text
    return dict(created.json())


async def _draft(client: Any, workspace: Workspace) -> str:
    created = await client.post(
        f"{API}/campaigns",
        json={
            "account_id": str(workspace.account_id),
            "template_id": str(workspace.template_id),
            "name": "Segmented",
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


# --------------------------------------------------------------- segments


async def test_a_segment_s_rules_are_copied_and_later_edits_change_nothing(
    database: Database,
) -> None:
    workspace = await _people(database, 4)
    a, b, c, d = workspace.contact_ids
    for contact_id, days in ((a, 3), (b, 10), (c, 45), (d, 3)):
        await _wrote(database, contact_id, days_ago=days)
    await _lead(database, workspace, d, LeadStatus.LOST)
    async with client_for(database, workspace) as client:
        segment = await _segment(
            client, name="Wrote in 30 days", rules={"last_inbound_within_days": 30}
        )
        campaign = await _draft(client, workspace)
        written = await client.post(
            f"{API}/campaigns/{campaign}/audience", json={"segment_id": segment["id"]}
        )
        assert written.status_code == 200, written.text
        body = written.json()
        assert body["audience_size"] == 3
        assert body["audience"]["last_inbound_within_days"] == 30

        edited = await client.patch(
            f"{API}/campaign-segments/{segment['id']}",
            headers={"If-Match": '"1"'},
            json={"rules": {"last_inbound_within_days": 5}},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["version"] == 2
        after_edit = (await client.get(f"{API}/campaigns/{campaign}")).json()
        assert after_edit["audience"]["last_inbound_within_days"] == 30
        assert after_edit["audience_size"] == 3

        removed = await client.delete(f"{API}/campaign-segments/{segment['id']}")
        assert removed.status_code == 204
        after_delete = (await client.get(f"{API}/campaigns/{campaign}")).json()
        assert after_delete["audience"]["last_inbound_within_days"] == 30
    async with database.session_factory() as session:
        row = await session.get(Campaign, uuid.UUID(campaign))
        assert row is not None and row.audience_segment_id is None


async def test_filters_with_a_segment_only_narrow_it(database: Database) -> None:
    workspace = await _people(database, 4)
    a, b, c, d = workspace.contact_ids
    for contact_id in workspace.contact_ids:
        await _wrote(database, contact_id, days_ago=3)
    await _wrote(database, d, days_ago=20)
    await _lead(database, workspace, a, LeadStatus.QUALIFIED)
    await _lead(database, workspace, b, LeadStatus.NEW)
    async with client_for(database, workspace) as client:
        segment = await _segment(
            client,
            name="Warm",
            rules={"last_inbound_within_days": 30, "lead_statuses": ["qualified", "new"]},
        )
        preview = await client.post(
            f"{API}/campaigns/audience/preview",
            json={"account_id": str(workspace.account_id), "segment_id": segment["id"]},
        )
        assert preview.json()["size"] == 2
        narrower = await client.post(
            f"{API}/campaigns/audience/preview",
            json={
                "account_id": str(workspace.account_id),
                "segment_id": segment["id"],
                "lead_statuses": ["qualified"],
                "last_inbound_within_days": 365,
            },
        )
        assert narrower.json()["size"] == 1
        disjoint = await client.post(
            f"{API}/campaigns/audience/preview",
            json={
                "account_id": str(workspace.account_id),
                "segment_id": segment["id"],
                "lead_statuses": ["won"],
            },
        )
        assert disjoint.json()["size"] == 0


async def test_a_segment_holds_rules_never_people_and_is_bounded(database: Database) -> None:
    workspace = await _people(database, 1)
    async with client_for(database, workspace) as client:
        people = await client.post(
            f"{API}/campaign-segments",
            json={"name": "Hand-picked", "rules": {"contact_ids": [str(workspace.contact_ids[0])]}},
        )
        assert people.status_code == 422
        recent = await _segment(client, name="Recent", rules={"last_inbound_within_days": 7})
        same = await client.post(f"{API}/campaign-segments", json={"name": "recent", "rules": {}})
        assert same.status_code == 409
        assert same.json()["error"]["code"] == "segment_name_taken"
        stale = await client.patch(
            f"{API}/campaign-segments/{recent['id']}",
            json={"name": "x", "version": 7},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "segment_version_conflict"

    async with database.session_factory() as session:
        for index in range(98):
            session.add(
                CampaignSegment(
                    tenant_id=workspace.tenant_id, name=f"filler {index}", rules={}, version=1
                )
            )
        await session.commit()
    async with client_for(database, workspace) as client:
        racing = await asyncio.gather(
            *(
                client.post(f"{API}/campaign-segments", json={"name": f"last {n}", "rules": {}})
                for n in range(3)
            )
        )
        assert sorted(answer.status_code for answer in racing) == [201, 409, 409]
        refused = [answer for answer in racing if answer.status_code == 409]
        assert {answer.json()["error"]["code"] for answer in refused} == {"segment_limit_reached"}
    async with database.session_factory() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(CampaignSegment)
            .where(CampaignSegment.tenant_id == workspace.tenant_id)
        )
    assert total == 100


async def test_a_segment_for_another_number_or_workspace_is_refused(database: Database) -> None:
    workspace = await _people(database, 2)
    other = await _people(database, 1)
    async with client_for(database, other) as client:
        foreign = await _segment(client, name="Theirs", rules={})
    async with client_for(database, workspace) as client:
        campaign = await _draft(client, workspace)
        stolen = await client.post(
            f"{API}/campaigns/{campaign}/audience", json={"segment_id": foreign["id"]}
        )
        assert stolen.status_code == 404
        assert (await client.get(f"{API}/campaign-segments/{foreign['id']}")).status_code == 404
        listed = (await client.get(f"{API}/campaign-segments")).json()["items"]
        assert foreign["id"] not in {item["id"] for item in listed}
        wrong_number = await client.post(
            f"{API}/campaign-segments",
            json={"name": "Elsewhere", "rules": {}, "account_id": str(other.account_id)},
        )
        assert wrong_number.status_code == 404


async def test_a_member_reads_segments_and_writes_none(database: Database) -> None:
    workspace = await _people(database, 1)
    async with client_for(database, workspace) as admin:
        segment = await _segment(admin, name="Mine", rules={})
    async with client_for(database, workspace, role=TenantRole.MEMBER) as member:
        assert (await member.get(f"{API}/campaign-segments")).status_code == 200
        assert (await member.get(f"{API}/campaign-segments/{segment['id']}")).status_code == 200
        assert (
            await member.post(f"{API}/campaign-segments", json={"name": "No", "rules": {}})
        ).status_code == 403
        assert (
            await member.patch(
                f"{API}/campaign-segments/{segment['id']}", json={"name": "No", "version": 1}
            )
        ).status_code == 403
        assert (await member.delete(f"{API}/campaign-segments/{segment['id']}")).status_code == 403


# ----------------------------------------------------------------- picker


async def _named(database: Database, contact_id: uuid.UUID, name: str) -> None:
    async with database.session_factory() as session:
        await session.execute(
            update(Contact).where(Contact.id == contact_id).values(display_name=name)
        )
        await session.commit()


async def _opted_out(database: Database, workspace: Workspace, contact_id: uuid.UUID) -> None:
    async with database.session_factory() as session:
        session.add(
            ContactChannelConsent(
                tenant_id=workspace.tenant_id,
                contact_id=contact_id,
                channel=Channel.WHATSAPP,
                marketing_opt_out_at=datetime.now(UTC),
                opt_out_source=OptOutSource.CUSTOMER,
            )
        )
        await session.commit()


async def test_the_picker_searches_names_and_numbers_and_says_who_is_eligible(
    database: Database,
) -> None:
    workspace = await _people(database, 4)
    a, b, c, d = workspace.contact_ids
    await _named(database, a, "Ahmed Samir")
    await _named(database, b, "Mona")
    await _lead(database, workspace, c, LeadStatus.NEW, name="Omar the lead")
    await _opted_out(database, workspace, d)
    for index, contact_id in enumerate(workspace.contact_ids):
        await _wrote(database, contact_id, days_ago=index + 1)
    async with database.session_factory() as session:
        b_number = await session.scalar(select(Contact.wa_id).where(Contact.id == b))
    assert b_number is not None
    async with client_for(database, workspace, role=TenantRole.MEMBER) as client:
        everyone = await client.get(
            f"{API}/contacts", params={"account_id": str(workspace.account_id)}
        )
        assert everyone.status_code == 200, everyone.text
        items = everyone.json()["items"]
        assert [item["id"] for item in items] == [str(c_) for c_ in (a, b, c, d)]
        by_id = {item["id"]: item for item in items}
        assert by_id[str(d)]["eligible_for_campaign"] is False
        assert by_id[str(d)]["ineligible_reason"] == "opted_out"
        assert by_id[str(d)]["opted_out_at"] is not None
        assert by_id[str(a)]["eligible_for_campaign"] is True
        assert by_id[str(c)]["lead"]["name"] == "Omar the lead"

        eligible = await client.get(
            f"{API}/contacts",
            params={"account_id": str(workspace.account_id), "eligible_for_campaign": "true"},
        )
        assert str(d) not in {item["id"] for item in eligible.json()["items"]}
        name = await client.get(f"{API}/contacts", params={"q": "samir"})
        assert [item["id"] for item in name.json()["items"]] == [str(a)]
        lead_name = await client.get(f"{API}/contacts", params={"q": "omar"})
        assert [item["id"] for item in lead_name.json()["items"]] == [str(c)]
        digits = await client.get(f"{API}/contacts", params={"q": b_number[-6:]})
        assert [item["id"] for item in digits.json()["items"]] == [str(b)]
        needs_number = await client.get(f"{API}/contacts", params={"eligible_for_campaign": "true"})
        assert needs_number.status_code == 422
        assert needs_number.json()["error"]["code"] == "account_required"

        pages: list[str] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"account_id": str(workspace.account_id), "limit": 1}
            if cursor:
                params["cursor"] = cursor
            page = (await client.get(f"{API}/contacts", params=params)).json()
            pages += [item["id"] for item in page["items"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert pages == [str(c_) for c_ in (a, b, c, d)]
        assert (await client.get(f"{API}/contacts", params={"limit": 51})).status_code == 422


async def test_the_picker_never_shows_another_workspace(database: Database) -> None:
    workspace = await _people(database, 2)
    other = await _people(database, 3)
    await _named(database, other.contact_ids[0], "Stranger")
    async with client_for(database, workspace) as client:
        mine = (await client.get(f"{API}/contacts", params={"limit": 50})).json()["items"]
        assert {item["id"] for item in mine} == {str(c) for c in workspace.contact_ids}
        assert (await client.get(f"{API}/contacts", params={"q": "Stranger"})).json()["items"] == []
        foreign_number = await client.get(
            f"{API}/contacts", params={"account_id": str(other.account_id)}
        )
        assert foreign_number.status_code == 404
