"""Meta's later `failed` statuses and template status events are understood (CMP-10, CMQ-05).

On the audited head the status projection kept a failed status's text and
dropped its code (`advance_status`): an asynchronous 131050 recorded no opt-out,
a template-pacing 132015 paused nothing, and statistics could not tell a
message Meta accepted and then failed. `message_template_status_update` was not
parsed at all.

Every webhook here goes through the real ASGI route with a valid
`X-Hub-Signature-256`, the real WhatsApp parser and ingestion, into a real
database. Shapes: the Cloud API status webhook with `errors[]`, and the
`message_template_status_update` field (developers.facebook.com, "Webhooks
components" / "Message templates" / "Template pacing", read 2026-10-13;
EXT-CMP-04, EXT-CMP-07, EXT-CMP-08).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.core.dependencies import get_settings_from_state
from app.db.models.campaign import Campaign, CampaignStatus, CampaignStopReason, OptOutVia
from app.db.models.channel import Channel
from app.db.models.consent import ContactChannelConsent
from app.db.models.conversation import Conversation, Message, MessageOrigin, MessageStatus
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateStatus, WhatsAppTemplate
from app.db.session import Database
from app.main import create_app
from app.repositories.campaign_repository import AudienceFilter
from app.services.campaign_service import CampaignService
from app.workers.campaign_worker import CampaignWorker
from tests.conftest import FakeDependency
from tests.integration.campaign_harness import (
    FakeMeta,
    Workspace,
    compose,
    erase,
    ledger,
    quiesce,
    seed,
    worker,
)

pytestmark = pytest.mark.integration

PATH = "/api/v1/webhooks/whatsapp"
APP_SECRET = "campaign-feedback-app-secret"


@pytest_asyncio.fixture
async def world(
    prepared_database: str, settings: Settings, fake_redis: FakeDependency
) -> AsyncIterator[tuple[FakeMeta, CampaignWorker, Database, AsyncClient]]:
    meta = FakeMeta()
    each, database = worker(prepared_database, meta, name="feedback")
    await quiesce(database)
    configured = settings.model_copy(update={"meta_app_secret": APP_SECRET})
    application: FastAPI = create_app(configured)
    application.state.database = database
    application.state.redis = fake_redis
    application.dependency_overrides[get_settings_from_state] = lambda: configured
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://wasla.test"
        ) as client:
            yield meta, each, database, client
    finally:
        await quiesce(database)
        await erase(database)
        await database.dispose()


def _signed(body: bytes) -> dict[str, str]:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}", "Content-Type": "application/json"}


async def _post(client: AsyncClient, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    response = await client.post(PATH, content=body, headers=_signed(body))
    assert response.status_code == 200, response.text


async def _account(database: Database, workspace: Workspace) -> WhatsAppAccount:
    async with database.session_factory() as session:
        account = await session.get(WhatsAppAccount, workspace.account_id)
        assert account is not None
        return account


async def _sent(database: Database, workspace: Workspace) -> list[Message]:
    async with database.session_factory() as session:
        return list(
            (
                await session.execute(
                    select(Message)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(
                        Conversation.account_id == workspace.account_id,
                        Message.origin == MessageOrigin.CAMPAIGN,
                        Message.wa_message_id.is_not(None),
                    )
                    .order_by(Message.created_at)
                )
            )
            .scalars()
            .all()
        )


def _failed_status(account: WhatsAppAccount, message: Message, code: int) -> dict[str, Any]:
    """A Cloud API `statuses[]` entry reporting the message failed, with Meta's `errors[]`."""
    stamp = int(datetime.now(UTC).timestamp())
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": account.waba_id,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "201000000000",
                                "phone_number_id": account.phone_number_id,
                            },
                            "statuses": [
                                {
                                    "id": message.wa_message_id,
                                    "status": "failed",
                                    "timestamp": str(stamp),
                                    "recipient_id": "201000000001",
                                    "errors": [
                                        {
                                            "code": code,
                                            "title": "harness",
                                            "message": "harness",
                                            "error_data": {"details": "harness"},
                                        }
                                    ],
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _template_event(
    account: WhatsAppAccount, template: WhatsAppTemplate, event: str, *, at: datetime
) -> dict[str, Any]:
    """The `message_template_status_update` field, keyed by the business account."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": account.waba_id,
                "time": int(at.timestamp()),
                "changes": [
                    {
                        "field": "message_template_status_update",
                        "value": {
                            "event": event,
                            "message_template_id": 1234567,
                            "message_template_name": template.name,
                            "message_template_language": template.language,
                            "reason": "NONE",
                        },
                    }
                ],
            }
        ],
    }


async def _template(database: Database, workspace: Workspace) -> WhatsAppTemplate:
    async with database.session_factory() as session:
        row = await session.get(WhatsAppTemplate, workspace.template_id)
        assert row is not None
        return row


async def test_an_asynchronous_131050_records_the_opt_out_and_the_next_campaign_skips(
    world: tuple[FakeMeta, CampaignWorker, Database, AsyncClient],
) -> None:
    """Audit W5': not recorded. Now consent on the channel, `via=provider_status`."""
    meta, each, database, client = world
    workspace = await seed(database, recipients=3)
    await compose(database, workspace)
    await each.run_once()
    first = (await _sent(database, workspace))[0]

    await _post(client, _failed_status(await _account(database, workspace), first, 131050))

    async with database.session_factory() as session:
        stored = await session.get(Message, first.id)
        assert stored is not None
        assert stored.failure_code == 131050
        consent = (
            await session.execute(
                select(ContactChannelConsent).where(
                    ContactChannelConsent.tenant_id == workspace.tenant_id
                )
            )
        ).scalar_one()
        assert consent.channel is Channel.WHATSAPP
        assert consent.opt_out_via is OptOutVia.PROVIDER_STATUS
        assert consent.marketing_opt_out_at is not None
        preview = await CampaignService(
            session=session, tenant_id=workspace.tenant_id
        ).preview_audience(account_id=workspace.account_id, filters=AudienceFilter())
        assert preview == 2


async def test_a_paced_template_failed_later_is_paused_and_the_next_batch_stops(
    world: tuple[FakeMeta, CampaignWorker, Database, AsyncClient],
) -> None:
    """Template pacing: a held message fails with 132015. Audit: text only, no write-back."""
    meta, each, database, client = world
    workspace = await seed(database, recipients=2)
    await compose(database, workspace)
    await each.run_once()
    held = (await _sent(database, workspace))[0]
    # A second campaign on the same template, scheduled before Meta reports.
    campaign_id = await compose(database, workspace)

    await _post(client, _failed_status(await _account(database, workspace), held, 132015))
    assert (await _template(database, workspace)).status is TemplateStatus.PAUSED

    requests = len(meta.requests)
    await each.run_once()

    assert len(meta.requests) == requests
    row = await ledger(database, campaign_id)
    assert row.status is CampaignStatus.FAILED
    async with database.session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        assert campaign is not None
        assert campaign.status_reason == CampaignStopReason.TEMPLATE_WITHDRAWN


async def test_statistics_count_a_message_meta_failed_after_accepting_it(
    world: tuple[FakeMeta, CampaignWorker, Database, AsyncClient],
) -> None:
    meta, each, database, client = world
    workspace = await seed(database, recipients=3)
    campaign_id = await compose(database, workspace)
    await each.run_once()
    first = (await _sent(database, workspace))[0]

    await _post(client, _failed_status(await _account(database, workspace), first, 131049))

    async with database.session_factory() as session:
        statistics = await CampaignService(
            session=session, tenant_id=workspace.tenant_id
        ).statistics(campaign_id)
        stored = await session.get(Message, first.id)
    assert statistics.sent == 3
    assert statistics.failed_after_send == 1
    assert statistics.delivered == 0
    assert stored is not None and stored.status is MessageStatus.FAILED
    assert stored.failure_code == 131049


async def test_the_template_status_webhook_pauses_the_registry_without_a_send(
    world: tuple[FakeMeta, CampaignWorker, Database, AsyncClient],
) -> None:
    """CMQ-05: not parsed on the audited head."""
    meta, _, database, client = world
    workspace = await seed(database, recipients=1)
    template = await _template(database, workspace)
    account = await _account(database, workspace)

    await _post(
        client,
        _template_event(account, template, "PAUSED", at=datetime.now(UTC) + timedelta(seconds=5)),
    )

    assert (await _template(database, workspace)).status is TemplateStatus.PAUSED
    assert len(meta.requests) == 0


async def test_an_older_template_event_does_not_overwrite_newer_knowledge(
    world: tuple[FakeMeta, CampaignWorker, Database, AsyncClient],
) -> None:
    """A replayed or late event, dated before the row was last written, changes nothing."""
    _, _, database, client = world
    workspace = await seed(database, recipients=1)
    template = await _template(database, workspace)
    account = await _account(database, workspace)

    await _post(
        client,
        _template_event(account, template, "PAUSED", at=template.updated_at - timedelta(hours=1)),
    )

    assert (await _template(database, workspace)).status is TemplateStatus.APPROVED


async def test_a_template_event_never_reaches_another_accounts_template(
    world: tuple[FakeMeta, CampaignWorker, Database, AsyncClient],
) -> None:
    """Routed by the business account: a same-named template elsewhere is untouched."""
    _, _, database, client = world
    mine = await seed(database, recipients=1)
    theirs = await seed(database, recipients=1)
    template = await _template(database, mine)
    async with database.session_factory() as session:
        other = await session.get(WhatsAppTemplate, theirs.template_id)
        assert other is not None
        other.name = template.name
        await session.commit()

    await _post(
        client,
        _template_event(
            await _account(database, mine),
            template,
            "DISABLED",
            at=datetime.now(UTC) + timedelta(seconds=5),
        ),
    )

    assert (await _template(database, mine)).status is TemplateStatus.DISABLED
    assert (await _template(database, theirs)).status is TemplateStatus.APPROVED
