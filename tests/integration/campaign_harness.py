"""Real campaign workers against a counting Meta transport (CMP-21, ADR-134).

The worker suite (`test_campaign_worker.py`) hands the worker a stub and one
shared session, which is why it could not see two replicas sending the same
person the same campaign (CMP-01). This harness runs the production path end to
end: each worker is a real `CampaignWorker` with its **own** `Database` - its
own pool and connections and its own commits - driving the real
`CampaignService`, `MessagingService`, `WhatsAppClient` and Meta error
classifier. Only Meta is replaced, at the HTTP transport (`httpx.MockTransport`),
and every request that reaches it is counted per recipient.

**"Sent once" is a count of requests that reached the transport, never of
rows.** The recipient rows showed each person `sent` once while two workers had
each sent them a copy (CAMPAIGNS_OUTBOUND_MESSAGING_AUDIT.md section 15).

Meta-shaped answers follow the Cloud API "Send messages" response and the
Graph error envelope (developers.facebook.com/docs/whatsapp/cloud-api/reference/messages,
.../cloud-api/support/error-codes, read 2026-10-13, audit section 36).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any

import httpx
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.storage import MediaStorage, build_key
from app.db.models.audit import AuditLog
from app.db.models.billing import Plan, Subscription
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from app.db.models.campaign_media import CampaignHeaderMedia
from app.db.models.channel import Channel
from app.db.models.conversation import (
    Contact,
    Conversation,
    ConversationMode,
    ConversationStatus,
    Message,
    MessageOrigin,
)
from app.db.models.media import MediaStorageState
from app.db.models.tenant import Tenant
from app.db.models.usage import UsageEvent, UsageEventType
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount
from app.db.models.whatsapp_template import TemplateCategory, TemplateStatus, WhatsAppTemplate
from app.db.session import Database
from app.repositories.campaign_repository import AudienceFilter
from app.services.campaign_service import CampaignService
from app.services.entitlement_service import EntitlementService
from app.services.messaging_service import MessagingService
from app.workers.campaign_worker import CampaignWorker
from tests.billing_fixtures import erase_ledger
from tests.channel_plans import EVERY_CHANNEL, allow_channels
from tests.workspace_timezone import TEST_TIMEZONE

PLATFORM_TOKEN = "harness-platform-token"

#: A body-only marketing template as Meta's template list returns it.
BODY_ONLY: list[dict[str, Any]] = [{"type": "BODY", "text": "Hello."}]


def harness_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "database_url": database_url,
        "database_pool_size": 10,
        "database_max_overflow": 10,
        "meta_access_token": PLATFORM_TOKEN,
    }
    values.update(overrides)
    return Settings(**values)


Behaviour = Callable[[dict[str, Any], int], Awaitable[httpx.Response]]
#: How Meta answers the n-th upload (1-based): a response, or an exception raised
#: at the transport (a connect error never reaches Meta).
UploadBehaviour = Callable[[int], Awaitable[httpx.Response]]


def accepted(payload: dict[str, Any], n: int) -> httpx.Response:
    """Meta's 200 for an accepted message: `contacts` and a `messages[].id`."""
    return httpx.Response(
        200,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": payload.get("to"), "wa_id": payload.get("to")}],
            "messages": [{"id": f"wamid.harness{n:06d}{uuid.uuid4().hex[:8]}"}],
        },
    )


def meta_error(
    status: int, code: int | None, headers: dict[str, str] | None = None
) -> httpx.Response:
    """The Graph error envelope, `{"error": {"code", "type", ...}}`."""
    body: dict[str, Any] = {
        "error": {"message": "harness", "type": "OAuthException", "fbtrace_id": "harness"}
    }
    if code is not None:
        body["error"]["code"] = code
    return httpx.Response(status, json=body, headers=headers or {})


@dataclass
class FakeMeta:
    """The provider. Counts every request that reaches it, by recipient and by worker."""

    latency: float = 0.0
    behaviour: Behaviour | None = None
    #: Seconds to wait before answering particular request numbers (1-based).
    stall: dict[int, float] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    on_request: Callable[[dict[str, Any], int], Awaitable[None]] | None = None
    #: `POST /{phone-number-id}/media`, counted apart from sends (HM-04): Meta's
    #: "Media" upload, answering `{"id": ...}`.
    uploads: list[dict[str, Any]] = field(default_factory=list)
    upload_behaviour: UploadBehaviour | None = None
    #: Seconds to wait before answering particular upload numbers (1-based).
    upload_stall: dict[int, float] = field(default_factory=dict)
    on_upload: Callable[[int], Awaitable[None]] | None = None
    #: The simulated time a request reached Meta at (`SimClock.at`); real time
    #: when unset. What "never inside quiet hours" and "never over the limit in
    #: a moving 24 hours" are counted by.
    clock: Callable[[], datetime] | None = None

    async def upload(self, request: httpx.Request, *, worker: str) -> httpx.Response:
        n = len(self.uploads) + 1
        body = request.content
        self.uploads.append(
            {
                "worker": worker,
                "phone_number_id": request.url.path.rstrip("/").split("/")[-2],
                "n": n,
                "bytes": len(body),
                "body": body,
            }
        )
        if self.on_upload is not None:
            await self.on_upload(n)
        delay = self.upload_stall.get(n, 0.0)
        if delay:
            await asyncio.sleep(delay)
        if self.upload_behaviour is not None:
            return await self.upload_behaviour(n)
        return httpx.Response(200, json={"id": f"hm{n:04d}{uuid.uuid4().hex[:6]}"})

    def header_ids(self) -> list[str | None]:
        """The header media id each send carried, in order (None for no media header)."""
        found: list[str | None] = []
        for entry in self.requests:
            template = entry["payload"].get("template") or {}
            media_id = None
            for component in template.get("components") or []:
                if component.get("type") != "header":
                    continue
                for parameter in component.get("parameters") or []:
                    kind = parameter.get("type")
                    if isinstance(parameter.get(kind), dict):
                        media_id = parameter[kind].get("id")
            found.append(media_id)
        return found

    async def handle(self, request: httpx.Request, *, worker: str) -> httpx.Response:
        if request.url.path.rstrip("/").endswith("/media"):
            return await self.upload(request, worker=worker)
        payload = json.loads(request.content or b"{}")
        n = len(self.requests) + 1
        self.requests.append(
            {
                "to": payload.get("to") or payload.get("recipient"),
                "worker": worker,
                "payload": payload,
                "n": n,
                "at": self.clock() if self.clock is not None else datetime.now(UTC),
            }
        )
        if self.on_request is not None:
            await self.on_request(payload, n)
        delay = self.stall.get(n, self.latency)
        if delay:
            await asyncio.sleep(delay)
        if self.behaviour is not None:
            return await self.behaviour(payload, n)
        return accepted(payload, n)

    def client(self, worker: str) -> httpx.AsyncClient:
        async def handler(request: httpx.Request) -> httpx.Response:
            return await self.handle(request, worker=worker)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)

    def per_recipient(self) -> Counter[str]:
        return Counter(str(entry["to"]) for entry in self.requests)

    def received_twice(self) -> int:
        """People this transport was asked to message more than once."""
        return sum(1 for count in self.per_recipient().values() if count > 1)

    def by_worker(self) -> Counter[str]:
        return Counter(str(entry["worker"]) for entry in self.requests)


@dataclass
class SimClock:
    """The worker's clock in a test: a moment the test chooses, advancing as a sweep runs.

    `CampaignWorker.run_once(now=...)` dates a batch from `now` and advances it
    by the time the batch takes, so a sweep started at 21:59:59 local and
    slowed by Meta's latency crosses 22:00 part-way, exactly as a real one
    would. `at` is that clock, for the fake Meta to stamp each request with.
    """

    now: datetime
    _started: float = 0.0

    def at(self) -> datetime:
        return self.now + timedelta(seconds=perf_counter() - self._started)

    async def sweep(self, replica: CampaignWorker) -> int:
        self._started = perf_counter()
        return await replica.run_once(now=self.now)


async def make_due(database: Database, campaign_id: uuid.UUID, *, at: datetime) -> None:
    """Put a scheduled campaign's moment at `at`, as if it had been scheduled for then.

    `schedule()` refuses a time in the past and moves a past one to now; a
    test standing at a daylight-saving transition in April needs the campaign
    to be due at that simulated moment instead.
    """
    async with database.session_factory() as session:
        await session.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(scheduled_at=at, next_send_at=None)
        )
        await session.commit()


def worker(
    database_url: str,
    meta: FakeMeta,
    *,
    name: str,
    lease_seconds: float = 300.0,
    claim_limit: int = 5,
    storage: MediaStorage | None = None,
    **settings_overrides: Any,
) -> tuple[CampaignWorker, Database]:
    """One replica: its own `Database`, its own HTTP pool to the counting transport.

    The production `MessagingService`, built per batch from the campaign's own
    workspace exactly as the worker builds it, only with this replica's client.
    """
    settings = harness_settings(database_url, **settings_overrides)
    database = Database(settings)
    http = meta.client(name)

    def messaging(session: AsyncSession, tenant_id: uuid.UUID) -> MessagingService:
        return MessagingService(session=session, settings=settings, tenant_id=tenant_id, http=http)

    return (
        CampaignWorker(
            database=database,
            settings=settings,
            messaging_factory=messaging,
            lease_seconds=lease_seconds,
            claim_limit=claim_limit,
            storage=storage,
        ),
        database,
    )


@dataclass
class Workspace:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    template_id: uuid.UUID
    contact_ids: list[uuid.UUID]


async def seed(
    database: Database,
    *,
    recipients: int,
    components: list[dict[str, Any]] | None = None,
    category: TemplateCategory = TemplateCategory.MARKETING,
    limits: dict[str, int] | None = None,
    variable_count: int = 0,
    body_text: str = "Hello.",
    channels: Iterable[Channel] = EVERY_CHANNEL,
    timezone: str = TEST_TIMEZONE,
    last_inbound_at: datetime | None = None,
) -> Workspace:
    """A workspace with one number, an approved template and N people who wrote first.

    In `timezone` (TZ-01), with the default quiet hours. Every person last
    wrote two days ago - their service window closed - unless
    `last_inbound_at` says otherwise.
    """
    suffix = uuid.uuid4().hex[:10]
    async with database.session_factory() as session:
        tenant = Tenant(
            timezone=timezone, name=f"Harness {suffix}", slug=f"{TENANT_PREFIX}{suffix}"
        )
        session.add(tenant)
        await session.flush()
        await allow_channels(session, tenant.id, channels, limits=limits)
        account = WhatsAppAccount(
            tenant_id=tenant.id,
            phone_number_id=f"pn-{suffix}",
            waba_id=f"waba-{suffix}",
            display_phone_number="+201000000000",
        )
        session.add(account)
        await session.flush()
        template = WhatsAppTemplate(
            tenant_id=tenant.id,
            account_id=account.id,
            name=f"offer_{suffix}",
            language="ar_EG",
            category=category,
            status=TemplateStatus.APPROVED,
            body_text=body_text,
            variable_count=variable_count,
            components=components if components is not None else list(BODY_ONLY),
        )
        session.add(template)
        contact_ids: list[uuid.UUID] = []
        base = int(suffix[:6], 16) % 10**6
        for index in range(recipients):
            contact = Contact(tenant_id=tenant.id, wa_id=f"20{base:06d}{index:05d}")
            session.add(contact)
            await session.flush()
            session.add(
                Conversation(
                    tenant_id=tenant.id,
                    contact_id=contact.id,
                    account_id=account.id,
                    status=ConversationStatus.OPEN,
                    mode=ConversationMode.AI,
                    last_inbound_at=(
                        last_inbound_at
                        if last_inbound_at is not None
                        else datetime.now(UTC) - timedelta(days=2)
                    ),
                )
            )
            contact_ids.append(contact.id)
        await session.commit()
        return Workspace(tenant.id, account.id, template.id, contact_ids)


#: A small PNG, as a campaign header file's bytes (HM-02).
HEADER_PNG = b"\x89PNG\r\n\x1a\n" + b"a spring sale photograph" * 32


async def store_file(
    database: Database,
    storage: MediaStorage,
    workspace: Workspace,
    *,
    content: bytes = HEADER_PNG,
    kind: str = "image",
    mime_type: str = "image/png",
    filename: str | None = None,
) -> uuid.UUID:
    """A stored header file, as the upload route leaves one: object written, row `stored`."""
    key = build_key(tenant_id=workspace.tenant_id, mime_type=mime_type)
    await storage.put_at(key=key, data=content, mime_type=mime_type)
    async with database.session_factory() as session:
        row = CampaignHeaderMedia(
            tenant_id=workspace.tenant_id,
            kind=kind,
            mime_type=mime_type,
            byte_size=len(content),
            filename=filename,
            content_hash=hashlib.sha256(content).hexdigest(),
            storage_key=key,
            storage_state=MediaStorageState.STORED,
            upload_started_at=datetime.now(UTC),
        )
        session.add(row)
        await session.commit()
        return row.id


async def compose(
    database: Database,
    workspace: Workspace,
    *,
    rate: int = 600,
    schedule: bool = True,
    variables: list[str] | None = None,
    entitlements: bool = False,
    **create: Any,
) -> uuid.UUID:
    """Create, materialise and (by default) schedule a campaign, committing each step."""
    async with database.session_factory() as session:
        service = CampaignService(session=session, tenant_id=workspace.tenant_id)
        campaign = await service.create(
            account_id=workspace.account_id,
            template_id=workspace.template_id,
            name="Harness",
            messages_per_minute=rate,
            variables=variables,
            **create,
        )
        await service.set_audience(campaign_id=campaign.id, filters=AudienceFilter())
        await session.commit()
        campaign_id = campaign.id
    if schedule:
        async with database.session_factory() as session:
            service = CampaignService(
                session=session,
                tenant_id=workspace.tenant_id,
                entitlements=(
                    EntitlementService(session, tenant_id=workspace.tenant_id)
                    if entitlements
                    else None
                ),
            )
            await service.schedule(campaign_id=campaign_id)
            await session.commit()
    return campaign_id


#: Every workspace and person the harness makes is recognisable, so a teardown
#: can erase exactly them: committed rows outlive the test, and other suites
#: count audit entries and plans across the whole database.
TENANT_PREFIX = "harness-"
USER_DOMAIN = "@harness.example.com"


async def erase(database: Database) -> None:
    """Erase every workspace, plan and person the harness made, and their trail."""
    async with database.session_factory() as session:
        ids = list(
            (
                await session.execute(
                    select(Tenant.id).where(Tenant.slug.startswith(TENANT_PREFIX))
                )
            ).scalars()
        )
        if ids:
            campaigns = select(Campaign.id).where(Campaign.tenant_id.in_(ids))
            await session.execute(
                delete(AuditLog).where(
                    or_(
                        AuditLog.tenant_id.in_(ids),
                        AuditLog.target_id.in_(ids),
                        AuditLog.target_id.in_(campaigns),
                    )
                )
            )
            await erase_ledger(session, ids)
            plans = list(
                (
                    await session.execute(
                        select(Subscription.plan_id).where(Subscription.tenant_id.in_(ids))
                    )
                ).scalars()
            )
            await session.execute(delete(Subscription).where(Subscription.tenant_id.in_(ids)))
            await session.execute(delete(Tenant).where(Tenant.id.in_(ids)))
            if plans:
                await session.execute(delete(Plan).where(Plan.id.in_(plans)))
        users = select(User.id).where(User.email.endswith(USER_DOMAIN))
        await session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(users)))
        await session.execute(delete(User).where(User.email.endswith(USER_DOMAIN)))
        await session.commit()


async def quiesce(database: Database, *, keep: set[uuid.UUID] | None = None) -> None:
    """Stop every live campaign this test did not make: the sweep is not tenant-scoped."""
    async with database.session_factory() as session:
        statement = update(Campaign).where(
            Campaign.status.in_([CampaignStatus.SCHEDULED, CampaignStatus.RUNNING])
        )
        if keep:
            statement = statement.where(Campaign.tenant_id.not_in(keep))
        await session.execute(statement.values(status=CampaignStatus.CANCELLED))
        await session.commit()


@dataclass(frozen=True)
class Ledger:
    """What the database says happened, beside what the transport saw."""

    recipients: Counter[str]
    sent: int
    meters: int
    campaign_messages: int
    conversations_with_two: int
    status: CampaignStatus


async def ledger(database: Database, campaign_id: uuid.UUID) -> Ledger:
    async with database.session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        assert campaign is not None
        rows = (
            await session.execute(
                select(CampaignRecipient.status, CampaignRecipient.attempts).where(
                    CampaignRecipient.campaign_id == campaign_id
                )
            )
        ).all()
        meters = await session.scalar(
            select(func.count())
            .select_from(UsageEvent)
            .where(
                UsageEvent.tenant_id == campaign.tenant_id,
                UsageEvent.event_type == UsageEventType.CAMPAIGN_MESSAGE,
                UsageEvent.meta["campaign_id"].astext == str(campaign_id),
            )
        )
        per_conversation = (
            select(Message.conversation_id, func.count().label("n"))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.account_id == campaign.account_id,
                Message.origin == MessageOrigin.CAMPAIGN,
            )
            .group_by(Message.conversation_id)
            .subquery()
        )
        twice = await session.scalar(
            select(func.count()).select_from(per_conversation).where(per_conversation.c.n > 1)
        )
        messages = await session.scalar(
            select(func.coalesce(func.sum(per_conversation.c.n), 0)).select_from(per_conversation)
        )
        return Ledger(
            recipients=Counter(f"{status.value}/attempts={attempts}" for status, attempts in rows),
            sent=sum(1 for status, _ in rows if status is RecipientStatus.SENT),
            meters=int(meters or 0),
            campaign_messages=int(messages or 0),
            conversations_with_two=int(twice or 0),
            status=campaign.status,
        )


__all__ = [
    "BODY_ONLY",
    "HEADER_PNG",
    "USER_DOMAIN",
    "FakeMeta",
    "Ledger",
    "SimClock",
    "Workspace",
    "accepted",
    "compose",
    "erase",
    "harness_settings",
    "ledger",
    "make_due",
    "meta_error",
    "quiesce",
    "seed",
    "store_file",
    "worker",
]
