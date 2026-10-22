"""Each person's own values in a campaign (PER-01 .. PER-05), counted at the transport.

Real worker, real commits, the fake Meta. "Their own value" is read from the
request Meta received for that person - never from Wasla's rows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.db.models.campaign import CampaignRecipient, RecipientStatus
from app.db.models.conversation import Contact
from app.db.models.lead import Lead, LeadStatus
from app.db.session import Database
from tests.integration.campaign_api import API, client_for
from tests.integration.campaign_harness import (
    FakeMeta,
    SimClock,
    Workspace,
    erase,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration

BODY = "Hello {{1}}, about {{2}}."
COMPONENTS: list[dict[str, Any]] = [
    {"type": "HEADER", "format": "TEXT", "text": "For {{1}}"},
    {"type": "BODY", "text": BODY},
    {
        "type": "BUTTONS",
        "buttons": [{"type": "URL", "text": "Shop", "url": "https://shop.example/{{1}}"}],
    },
]
NAME = {"source": "contact.display_name", "fallback": "عميلنا"}
INTEREST = {"source": "lead.interest", "fallback": "our offer"}
BUTTONS = [{"index": 0, "value": "spring"}]


@pytest_asyncio.fixture
async def database(prepared_database: str) -> AsyncIterator[Database]:
    _, built = worker(prepared_database, FakeMeta(), name="personal")
    await quiesce(built)
    try:
        yield built
    finally:
        await quiesce(built)
        await erase(built)
        await built.dispose()


async def _people(database: Database, recipients: int) -> Workspace:
    return await seed(
        database,
        recipients=recipients,
        components=COMPONENTS,
        body_text=BODY,
        variable_count=2,
    )


async def _set_name(database: Database, contact_id: uuid.UUID, name: str | None) -> None:
    async with database.session_factory() as session:
        await session.execute(
            update(Contact).where(Contact.id == contact_id).values(display_name=name)
        )
        await session.commit()


async def _lead(
    database: Database,
    workspace: Workspace,
    contact_id: uuid.UUID,
    *,
    status: LeadStatus = LeadStatus.NEW,
    **fields: Any,
) -> None:
    async with database.session_factory() as session:
        session.add(
            Lead(tenant_id=workspace.tenant_id, contact_id=contact_id, status=status, **fields)
        )
        await session.commit()


async def _wa_id(database: Database, contact_id: uuid.UUID) -> str:
    async with database.session_factory() as session:
        found = await session.scalar(select(Contact.wa_id).where(Contact.id == contact_id))
        assert found is not None
        return found


def _values(entry: dict[str, Any], part: str) -> list[str]:
    for component in entry["payload"]["template"].get("components") or []:
        if component["type"] == part:
            return [parameter["text"] for parameter in component["parameters"]]
    return []


async def _create(client: Any, workspace: Workspace, **body: Any) -> Any:
    return await client.post(
        f"{API}/campaigns",
        json={
            "account_id": str(workspace.account_id),
            "template_id": str(workspace.template_id),
            "name": "Spring sale",
            "messages_per_minute": 600,
            "button_parameters": BUTTONS,
            **body,
        },
    )


async def _run(
    database: Database, prepared_database: str, meta: FakeMeta, campaign_id: str
) -> None:
    replica, own = worker(prepared_database, meta, name="send")
    clock = SimClock(now=datetime.now(UTC) + timedelta(seconds=1))
    try:
        for _ in range(10):
            await clock.sweep(replica)
            if not await _pending(database, campaign_id):
                return
            clock.now += timedelta(minutes=1)
    finally:
        await own.dispose()


async def _pending(database: Database, campaign_id: str) -> int:
    async with database.session_factory() as session:
        rows = (
            await session.execute(
                select(CampaignRecipient.id).where(
                    CampaignRecipient.campaign_id == uuid.UUID(campaign_id),
                    CampaignRecipient.status == RecipientStatus.PENDING,
                )
            )
        ).all()
    return len(rows)


async def _compose_and_send(
    database: Database,
    prepared_database: str,
    workspace: Workspace,
    meta: FakeMeta,
    *,
    before_send: Any = None,
    **body: Any,
) -> str:
    async with client_for(database, workspace) as client:
        created = await _create(client, workspace, **body)
        assert created.status_code == 201, created.text
        campaign_id = created.json()["id"]
        audience = await client.post(f"{API}/campaigns/{campaign_id}/audience", json={})
        assert audience.status_code == 200, audience.text
        if before_send is not None:
            await before_send()
        scheduled = await client.post(f"{API}/campaigns/{campaign_id}/schedule", json={})
        assert scheduled.status_code == 200, scheduled.text
    await _run(database, prepared_database, meta, campaign_id)
    return str(campaign_id)


async def test_every_person_is_sent_their_own_values_and_the_fallback_where_they_have_none(
    database: Database, prepared_database: str
) -> None:
    meta = FakeMeta()
    workspace = await _people(database, 4)
    named, nameless, interested, custom = workspace.contact_ids
    await _set_name(database, named, "Ahmed")
    await _set_name(database, interested, "Mona")
    await _lead(database, workspace, interested, interest="apartment finishing")
    await _set_name(database, custom, "Omar")
    await _lead(
        database,
        workspace,
        custom,
        interest="villa",
        custom_fields={"area": "150m"},
    )

    await _compose_and_send(
        database,
        prepared_database,
        workspace,
        meta,
        body_parameters=[NAME, INTEREST],
        header_parameters=[
            {"source": "lead.custom_fields", "key": "area", "fallback": "you"},
        ],
    )

    by_person = {str(entry["to"]): entry for entry in meta.requests}
    assert len(by_person) == 4 and len(meta.requests) == 4
    expected = {
        named: (["Ahmed", "our offer"], ["you"]),
        nameless: (["عميلنا", "our offer"], ["you"]),
        interested: (["Mona", "apartment finishing"], ["you"]),
        custom: (["Omar", "villa"], ["150m"]),
    }
    for contact_id, (body, header) in expected.items():
        entry = by_person[await _wa_id(database, contact_id)]
        assert _values(entry, "body") == body
        assert _values(entry, "header") == header
        # The button stays the campaign's constant.
        assert _values(entry, "button") == ["spring"]
    # Nobody's request carries a name that is not theirs.
    names = {"Ahmed", "Mona", "Omar"}
    for contact_id, (body, _) in expected.items():
        entry = by_person[await _wa_id(database, contact_id)]
        others = names - set(body)
        assert not any(other in str(entry["payload"]) for other in others)


async def test_values_are_normalised_and_a_long_one_falls_back(
    database: Database, prepared_database: str
) -> None:
    meta = FakeMeta()
    workspace = await _people(database, 3)
    spaced, long_name, plain = workspace.contact_ids
    await _set_name(database, spaced, "  Sara\n\tAli          Hassan \x07")
    await _set_name(database, long_name, "x" * 150)  # the column holds 200; the cap is 60
    await _set_name(database, plain, "Ali")

    await _compose_and_send(
        database,
        prepared_database,
        workspace,
        meta,
        body_parameters=[NAME, {"source": "constant", "value": "spring"}],
        header_variables=["you"],
    )

    by_person = {str(entry["to"]): entry for entry in meta.requests}
    assert _values(by_person[await _wa_id(database, spaced)], "body")[0] == "Sara Ali Hassan"
    assert _values(by_person[await _wa_id(database, long_name)], "body")[0] == "عميلنا"
    assert _values(by_person[await _wa_id(database, plain)], "body") == ["Ali", "spring"]


async def test_values_are_frozen_when_the_audience_is_written(
    database: Database, prepared_database: str
) -> None:
    meta = FakeMeta()
    workspace = await _people(database, 1)
    (person,) = workspace.contact_ids
    await _set_name(database, person, "Before")

    async def rename() -> None:
        await _set_name(database, person, "After")

    await _compose_and_send(
        database,
        prepared_database,
        workspace,
        meta,
        before_send=rename,
        body_parameters=[NAME, INTEREST],
        header_variables=["you"],
    )

    (entry,) = meta.requests
    assert _values(entry, "body")[0] == "Before"
    async with database.session_factory() as session:
        stored = await session.scalar(
            select(CampaignRecipient.rendered_parameters).where(
                CampaignRecipient.contact_id == person
            )
        )
    assert stored is not None and stored["contact_id"] == str(person)


async def test_a_body_that_would_exceed_meta_s_limit_is_sent_with_the_fallbacks(
    database: Database, prepared_database: str
) -> None:
    meta = FakeMeta()
    long_body = "Hello {{1}}, about {{2}}." + " " + ("." * 960)
    workspace = await seed(
        database,
        recipients=1,
        components=[
            {"type": "HEADER", "format": "TEXT", "text": "For {{1}}"},
            {"type": "BODY", "text": long_body},
            COMPONENTS[2],
        ],
        body_text=long_body,
        variable_count=2,
    )
    (person,) = workspace.contact_ids
    await _set_name(database, person, "N" * 40)
    await _lead(database, workspace, person, interest="I" * 50)

    await _compose_and_send(
        database,
        prepared_database,
        workspace,
        meta,
        body_parameters=[NAME, INTEREST],
        header_variables=["you"],
    )

    (entry,) = meta.requests
    assert _values(entry, "body") == ["عميلنا", "our offer"]
    async with database.session_factory() as session:
        stored = await session.scalar(
            select(CampaignRecipient.rendered_parameters).where(
                CampaignRecipient.contact_id == person
            )
        )
    assert stored is not None and stored["body_too_long"] is True


async def test_sources_that_are_not_allowed_are_refused(database: Database) -> None:
    workspace = await _people(database, 1)
    async with client_for(database, workspace) as client:
        button = await _create(
            client,
            workspace,
            body_parameters=[NAME, INTEREST],
            header_variables=["you"],
            button_parameters=[{"index": 0, "value": "x", "source": "contact.display_name"}],
        )
        assert button.status_code == 422, button.text
        assert button.json()["error"]["code"] == "parameter_source_not_allowed"

        phone = await _create(
            client,
            workspace,
            body_parameters=[{"source": "contact.wa_id", "fallback": "x"}, INTEREST],
            header_variables=["you"],
        )
        assert phone.status_code == 422, phone.text

        no_fallback = await _create(
            client,
            workspace,
            body_parameters=[{"source": "contact.display_name"}, INTEREST],
            header_variables=["you"],
        )
        assert no_fallback.status_code == 422
        assert no_fallback.json()["error"]["code"] == "parameter_fallback_required"

        both = await _create(
            client,
            workspace,
            variables=["a", "b"],
            body_parameters=[NAME, INTEREST],
            header_variables=["you"],
        )
        assert both.status_code == 422
        assert both.json()["error"]["code"] == "parameters_conflict"

        unknown_key = await _create(
            client,
            workspace,
            body_parameters=[
                NAME,
                {"source": "lead.custom_fields", "key": "nowhere", "fallback": "x"},
            ],
            header_variables=["you"],
        )
        assert unknown_key.status_code == 422
        assert unknown_key.json()["error"]["code"] == "parameter_key_unknown"

        too_long = await _create(
            client,
            workspace,
            body_parameters=[{"source": "contact.display_name", "fallback": "f" * 61}, INTEREST],
            header_variables=["you"],
        )
        assert too_long.status_code == 422
        assert too_long.json()["error"]["code"] == "parameter_fallback_too_long"

        wrong_count = await _create(
            client, workspace, body_parameters=[NAME], header_variables=["you"]
        )
        assert wrong_count.status_code == 422


async def test_preview_shows_exactly_the_people_asked_for(database: Database) -> None:
    workspace = await _people(database, 6)
    names = ["Ahmed", "Mona", None, "Omar", "Sara", "Ali"]
    for contact_id, name in zip(workspace.contact_ids, names, strict=True):
        await _set_name(database, contact_id, name)
    stranger = await _people(database, 1)

    async with client_for(database, workspace) as client:
        created = await _create(
            client, workspace, body_parameters=[NAME, INTEREST], header_variables=["you"]
        )
        campaign_id = created.json()["id"]
        asked = workspace.contact_ids[:5]

        # Before the audience is written: resolved now, from the eligible people.
        before = await client.post(
            f"{API}/campaigns/{campaign_id}/preview-messages",
            json={"contact_ids": [str(contact_id) for contact_id in asked]},
        )
        assert before.status_code == 200, before.text
        await client.post(f"{API}/campaigns/{campaign_id}/audience", json={})
        after = await client.post(
            f"{API}/campaigns/{campaign_id}/preview-messages",
            json={"contact_ids": [str(contact_id) for contact_id in asked]},
        )
        assert after.status_code == 200, after.text

        for response in (before, after):
            previews = response.json()["previews"]
            assert [preview["contact_id"] for preview in previews] == [str(c) for c in asked]
            for preview, name in zip(previews, names[:5], strict=True):
                shown = name or "عميلنا"
                assert preview["body"] == f"Hello {shown}, about our offer."
                assert preview["header"] == "For you"
                assert preview["buttons"] == ["Shop (https://shop.example/spring)"]
                assert preview["fallbacks"]["body"] == ([1] if name else [0, 1])
            # "Ali", the sixth person, was not asked for and appears nowhere.
            assert "Ali" not in response.text

        foreign = await client.post(
            f"{API}/campaigns/{campaign_id}/preview-messages",
            json={"contact_ids": [str(stranger.contact_ids[0])]},
        )
        assert foreign.status_code == 404
        six = await client.post(
            f"{API}/campaigns/{campaign_id}/preview-messages",
            json={"contact_ids": [str(c) for c in workspace.contact_ids]},
        )
        assert six.status_code == 422


async def test_a_lead_budget_and_status_read_as_labels(
    database: Database, prepared_database: str
) -> None:
    meta = FakeMeta()
    workspace = await _people(database, 1)
    (person,) = workspace.contact_ids
    await _lead(
        database,
        workspace,
        person,
        status=LeadStatus.QUALIFIED,
        budget_amount=Decimal("500000.00"),
        budget_currency="EGP",
    )
    await _compose_and_send(
        database,
        prepared_database,
        workspace,
        meta,
        body_parameters=[
            {"source": "lead.status", "fallback": "x"},
            {"source": "lead.budget", "fallback": "y"},
        ],
        header_variables=["you"],
    )
    (entry,) = meta.requests
    assert _values(entry, "body") == ["Qualified", "500,000 EGP"]
