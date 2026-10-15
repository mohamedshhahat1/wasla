"""A template is filled correctly or refused before anyone is written to (CMP-05, CMP-09, CMP-19).

Ported from the audit's P07 (the payload Meta received for a header and button
template: a body component alone) and the template rows of P06 (132000, 132012
and 131008 cost 9 requests for 3 people, `completed`, no reason; 132001 spent
an attempt on every un-asked recipient). Payloads are asserted as the JSON the
fake transport received, never as `send_components`' return value.

Template component shapes are Meta's, as `list_templates` returns them and a
sync stores them ("Message templates" / "Components", and "Send message
templates", developers.facebook.com, read 2026-10-13; audit section 36).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.core.exceptions import ValidationError
from app.db.models.campaign import Campaign, CampaignStatus, CampaignStopReason
from app.db.models.conversation import Conversation, Message, MessageOrigin
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus, WhatsAppTemplate
from app.db.session import Database
from app.services.campaign_service import TEMPLATE_CATEGORY_CODE, CampaignService
from app.services.template_parameters import (
    TEMPLATE_NOT_SYNCED,
    TEMPLATE_PARAMETERS,
    TEMPLATE_UNSUPPORTED,
)
from app.workers.campaign_worker import CampaignWorker
from tests.integration.campaign_harness import (
    FakeMeta,
    Workspace,
    accepted,
    compose,
    erase,
    ledger,
    meta_error,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration

TEXT_HEADER_URL_BUTTON: list[dict[str, Any]] = [
    {"type": "HEADER", "format": "TEXT", "text": "An offer for {{1}}"},
    {"type": "BODY", "text": "Hello {{1}}, {{2}} off today."},
    {"type": "FOOTER", "text": "Reply STOP to opt out."},
    {
        "type": "BUTTONS",
        "buttons": [
            {"type": "URL", "text": "Shop", "url": "https://shop.example/offer/{{1}}"},
            {"type": "QUICK_REPLY", "text": "Stop promotions"},
            {"type": "COPY_CODE", "example": "SAVE20"},
        ],
    },
]
IMAGE_HEADER_URL_BUTTON: list[dict[str, Any]] = [
    {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["4::aW1hZ2U="]}},
    {"type": "BODY", "text": "Hello {{1}}."},
    {
        "type": "BUTTONS",
        "buttons": [{"type": "URL", "text": "Shop", "url": "https://shop.example/{{1}}"}],
    },
]
NAMED_BODY: list[dict[str, Any]] = [
    {"type": "BODY", "text": "Hello {{first_name}}, your code is {{code}}."},
]


@pytest_asyncio.fixture
async def world(prepared_database: str) -> AsyncIterator[tuple[FakeMeta, CampaignWorker, Database]]:
    meta = FakeMeta()
    each, database = worker(prepared_database, meta, name="templates")
    await quiesce(database)
    try:
        yield meta, each, database
    finally:
        await quiesce(database)
        await erase(database)
        await database.dispose()


async def _create(database: Database, workspace: Workspace, **fields: Any) -> uuid.UUID:
    async with database.session_factory() as session:
        campaign = await CampaignService(session=session, tenant_id=workspace.tenant_id).create(
            account_id=workspace.account_id,
            template_id=workspace.template_id,
            name="Templates",
            **fields,
        )
        await session.commit()
        return campaign.id


async def _refused(database: Database, workspace: Workspace, **fields: Any) -> ValidationError:
    with pytest.raises(ValidationError) as raised:
        await _create(database, workspace, **fields)
    return raised.value


async def _template(database: Database, workspace: Workspace) -> WhatsAppTemplate:
    async with database.session_factory() as session:
        row = await session.get(WhatsAppTemplate, workspace.template_id)
        assert row is not None
        return row


async def test_a_header_and_button_template_sends_every_component_meta_needs(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    """P07, with the parameters supplied: header, body and both dynamic buttons reach Meta."""
    meta, each, database = world
    workspace = await seed(database, recipients=1, components=TEXT_HEADER_URL_BUTTON)
    await compose(
        database,
        workspace,
        variables=["Ahmed", "20%"],
        header_variables=["Ahmed"],
        button_parameters={0: "spring-20", 2: "SAVE20"},
    )

    await each.run_once()

    assert len(meta.requests) == 1
    template = meta.requests[0]["payload"]["template"]
    assert template["components"] == [
        {"type": "header", "parameters": [{"type": "text", "text": "Ahmed"}]},
        {
            "type": "body",
            "parameters": [{"type": "text", "text": "Ahmed"}, {"type": "text", "text": "20%"}],
        },
        {
            "type": "button",
            "sub_type": "url",
            "index": "0",
            "parameters": [{"type": "text", "text": "spring-20"}],
        },
        {
            "type": "button",
            "sub_type": "copy_code",
            "index": "2",
            "parameters": [{"type": "coupon_code", "coupon_code": "SAVE20"}],
        },
    ]


async def test_named_placeholders_are_sent_with_their_names(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    meta, each, database = world
    workspace = await seed(database, recipients=1, components=NAMED_BODY)
    await compose(database, workspace, variables=["Ahmed", "SAVE20"])

    await each.run_once()

    assert meta.requests[0]["payload"]["template"]["components"] == [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "parameter_name": "first_name", "text": "Ahmed"},
                {"type": "text", "parameter_name": "code", "text": "SAVE20"},
            ],
        }
    ]


async def test_missing_header_or_button_parameters_are_refused_at_composition(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    """The audit's P07 composition was accepted with the body alone."""
    _, _, database = world
    workspace = await seed(database, recipients=1, components=TEXT_HEADER_URL_BUTTON)

    header = await _refused(
        database, workspace, variables=["Ahmed", "20%"], button_parameters={0: "a", 2: "b"}
    )
    assert header.error_code == TEMPLATE_PARAMETERS
    assert "header" in header.message

    button = await _refused(
        database, workspace, variables=["Ahmed", "20%"], header_variables=["Ahmed"]
    )
    assert button.error_code == TEMPLATE_PARAMETERS
    assert "button" in button.message


async def test_a_media_header_without_its_file_is_refused_at_composition(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    """An IMAGE header needs the campaign's header file (ADR-144; was deferred, ADR-137).

    Refused naming the header, never sent without the image. A LOCATION
    header carries no file and is still refused as unsupported.
    """
    _, _, database = world
    workspace = await seed(database, recipients=1, components=IMAGE_HEADER_URL_BUTTON)

    refused = await _refused(
        database, workspace, variables=["Ahmed"], button_parameters={0: "spring"}
    )
    assert refused.error_code == TEMPLATE_PARAMETERS
    assert "image" in refused.message and "header" in refused.message

    location = await seed(
        database,
        recipients=1,
        components=[{"type": "HEADER", "format": "LOCATION"}, {"type": "BODY", "text": "Hi."}],
    )
    refused = await _refused(database, location)
    assert refused.error_code == TEMPLATE_UNSUPPORTED
    assert "location" in refused.message


async def test_a_template_never_synced_with_its_components_is_refused(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    _, _, database = world
    workspace = await seed(database, recipients=1)
    async with database.session_factory() as session:
        await session.execute(
            update(WhatsAppTemplate)
            .where(WhatsAppTemplate.id == workspace.template_id)
            .values(components=None)
        )
        await session.commit()

    refused = await _refused(database, workspace)
    assert refused.error_code == TEMPLATE_NOT_SYNCED
    assert "Sync" in refused.message


@pytest.mark.parametrize("code", [132000, 132012, 131008])
async def test_a_parameter_refusal_costs_one_request_and_fails_the_campaign(
    world: tuple[FakeMeta, CampaignWorker, Database], code: int
) -> None:
    """P06: audit 9 requests for 3 people, `completed`, no reason. Now 1, FAILED, with the code."""
    meta, each, database = world
    meta.behaviour = lambda payload, n: _answer(meta_error(400, code))
    workspace = await seed(database, recipients=3)
    campaign_id = await compose(database, workspace)

    for _ in range(3):
        await each.run_once()

    assert len(meta.requests) == 1
    row = await ledger(database, campaign_id)
    assert row.status is CampaignStatus.FAILED
    # The one asked spent its attempt; the two never asked spent nothing.
    assert row.recipients == {"pending/attempts=1": 1, "pending/attempts=0": 2}
    async with database.session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.status_reason == CampaignStopReason.TEMPLATE_PARAMETERS_REFUSED
        assert str(code) in (campaign.last_error or "")
        codes = (
            (
                await session.execute(
                    select(Message.failure_code)
                    .where(Message.origin == MessageOrigin.CAMPAIGN)
                    .where(Message.conversation_id.in_(_conversations(workspace)))
                )
            )
            .scalars()
            .all()
        )
        assert list(codes) == [code]
    # Not the template's fault: it still works with the right parameters (M25).
    assert (await _template(database, workspace)).status is TemplateStatus.APPROVED


async def test_a_withdrawal_mid_batch_spends_nothing_on_the_people_not_asked(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    """P06 132001: the un-asked recipients each spent an attempt locally. Now none does."""
    meta, each, database = world

    async def third_withdrawn(payload: dict[str, Any], n: int) -> httpx.Response:
        return meta_error(400, 132001) if n == 3 else accepted(payload, n)

    meta.behaviour = third_withdrawn
    workspace = await seed(database, recipients=6)
    campaign_id = await compose(database, workspace)

    for _ in range(3):
        await each.run_once()

    assert len(meta.requests) == 3
    row = await ledger(database, campaign_id)
    assert row.status is CampaignStatus.FAILED
    assert row.recipients == {
        "sent/attempts=0": 2,
        "pending/attempts=1": 1,
        "pending/attempts=0": 3,
    }
    async with database.session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.status_reason == CampaignStopReason.TEMPLATE_WITHDRAWN
    assert (await _template(database, workspace)).status is TemplateStatus.PAUSED


@pytest.mark.parametrize(
    "category",
    [TemplateCategory.UTILITY, TemplateCategory.AUTHENTICATION, TemplateCategory.UNKNOWN],
)
async def test_a_campaign_needs_a_marketing_template(
    world: tuple[FakeMeta, CampaignWorker, Database], category: TemplateCategory
) -> None:
    """CMP-09: P07 accepted all three; the model docstring always said marketing only."""
    _, _, database = world
    workspace = await seed(database, recipients=1, category=category)

    refused = await _refused(database, workspace)
    assert refused.error_code == TEMPLATE_CATEGORY_CODE


async def test_a_template_recategorised_after_scheduling_stops_the_campaign(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    meta, each, database = world
    workspace = await seed(database, recipients=3)
    campaign_id = await compose(database, workspace)
    async with database.session_factory() as session:
        await session.execute(
            update(WhatsAppTemplate)
            .where(WhatsAppTemplate.id == workspace.template_id)
            .values(category=TemplateCategory.UTILITY)
        )
        await session.commit()

    await each.run_once()

    assert len(meta.requests) == 0
    async with database.session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.status is CampaignStatus.FAILED
        assert campaign.status_reason == CampaignStopReason.TEMPLATE_CATEGORY


async def _answer(response: httpx.Response) -> httpx.Response:
    return response


def _conversations(workspace: Workspace) -> Any:
    return select(Conversation.id).where(Conversation.account_id == workspace.account_id)


async def test_a_template_paused_elsewhere_mid_batch_stops_the_campaign_without_spending(
    world: tuple[FakeMeta, CampaignWorker, Database],
) -> None:
    """CMP-19 through the registry: another send paused the template while this batch ran.

    The next recipient's send is refused locally (`TemplateRefusedError`) before
    anything is staged; the campaign fails once and nobody after it spends an
    attempt (kills M-C18).
    """
    meta, each, database = world
    workspace = await seed(database, recipients=6)

    async def pause_after_two(payload: dict[str, Any], n: int) -> None:
        if n == 2:
            async with database.session_factory() as session:
                await session.execute(
                    update(WhatsAppTemplate)
                    .where(WhatsAppTemplate.id == workspace.template_id)
                    .values(status=TemplateStatus.PAUSED)
                )
                await session.commit()

    meta.on_request = pause_after_two
    campaign_id = await compose(database, workspace)

    await each.run_once()

    assert len(meta.requests) == 2
    row = await ledger(database, campaign_id)
    assert row.status is CampaignStatus.FAILED
    assert dict(row.recipients) == {"sent/attempts=0": 2, "pending/attempts=0": 4}
    async with database.session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.status_reason == CampaignStopReason.TEMPLATE_WITHDRAWN
