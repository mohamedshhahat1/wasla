"""Adversarial multi-tenant isolation, driven over HTTP against real rows.

Every other endpoint test in this suite overrides the service dependency with a
stub, which pins routing, shapes and roles but proves nothing about isolation:
a stub returns whatever it was told to return regardless of which workspace
asked. The repository tests in `test_authorization.py` prove the filter works
when a scoped repository is constructed correctly — they cannot prove that
every route constructs one.

These tests close that gap from the outside. Two workspaces exist against a
real PostgreSQL schema. The victim owns one of every tenant-scoped entity. The
attacker holds a genuine signed token, is **TENANT_OWNER of their own
workspace** — the highest role available to a customer — and knows the
victim's UUIDs exactly, as they would from a leaked link, a screenshot or a
support thread. Then they try every operation the API offers.

Three deliberate choices keep a pass meaningful rather than accidental:

- **The attacker is an owner, not a member.** A refusal can never be attributed
  to a role check that would have refused anybody.
- **Entitlements are stubbed to allow everything.** A plan limit answering 403
  would look exactly like isolation working, and would still be a failure if
  the row was reachable.
- **Every refusal is asserted as 404, never 403.** Distinguishing "not yours"
  from "does not exist" is itself a disclosure: it confirms the id names a real
  row somewhere on the platform. `TenantIsolationError` exists for precisely
  this, and these tests are what stop a future handler answering 403 instead.

The control cases matter as much as the refusals. A suite that only asserts
"the attacker gets 404" passes just as well against an endpoint that is broken
for everyone, so each resource is also fetched successfully by its owner.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import create_access_token
from app.db.models import (
    Membership,
    PlatformRole,
    Tenant,
    TenantInvitation,
    TenantRole,
    User,
)
from app.db.models.agent import Agent, AgentStatus
from app.db.models.campaign import Campaign, CampaignRecipient, CampaignStatus
from app.db.models.conversation import (
    Contact,
    Conversation,
    Message,
    MessageDirection,
    MessageKind,
    MessageStatus,
)
from app.db.models.enums import InvitationStatus, TenantStatus
from app.db.models.follow_up import FollowUp, FollowUpStatus
from app.db.models.invoice import Invoice, InvoiceStatus
from app.db.models.knowledge import (
    Document,
    DocumentSource,
    DocumentStatus,
    KnowledgeBase,
)
from app.db.models.lead import Lead, LeadNote, LeadSource, LeadStatus
from app.db.models.media import MediaStatus, MessageMedia
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.db.models.whatsapp_template import (
    TemplateCategory,
    TemplateStatus,
    WhatsAppTemplate,
)
from app.main import create_app
from tests.conftest import AllowingEntitlements, FakeDependency

pytestmark = pytest.mark.integration

MOMENT = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
API = "/api/v1"

# One attempted operation: (method, path, JSON body, query parameters).
Attack = tuple[str, str, dict[str, object] | None, dict[str, str] | None]


# --------------------------------------------------------------------- set-up


@dataclass(frozen=True, slots=True)
class Workspace:
    """One tenant, its owner, and one of every tenant-scoped row it owns."""

    tenant: Tenant
    user: User
    token: str
    account: WhatsAppAccount
    contact: Contact
    conversation: Conversation
    message: Message
    media: MessageMedia
    lead: Lead
    note: LeadNote
    agent: Agent
    knowledge_base: KnowledgeBase
    document: Document
    template: WhatsAppTemplate
    campaign: Campaign
    follow_up: FollowUp
    invitation: TenantInvitation
    invoice: Invoice

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


async def _seed(session: AsyncSession, settings: Settings, *, slug: str) -> Workspace:
    """Build a complete workspace, written straight to the database.

    Deliberately not built through the services: seeding through the same code
    under test would make a shared isolation bug invisible in both directions.
    """
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    user = User(
        email=f"owner@{slug}.example",
        full_name=f"{slug.title()} Owner",
        hashed_password="x",
        is_active=True,
        platform_role=None,
    )
    session.add_all([tenant, user])
    await session.flush()

    session.add(Membership(tenant_id=tenant.id, user_id=user.id, role=TenantRole.TENANT_OWNER))

    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"pnid-{slug}",
        waba_id=f"waba-{slug}",
        display_phone_number=f"+2010000{slug[:4]}",
        display_name=f"{slug} number",
        status=WhatsAppAccountStatus.ACTIVE,
    )
    contact = Contact(tenant_id=tenant.id, wa_id=f"wa-{slug}", display_name="Ahmed Hassan")
    session.add_all([account, contact])
    await session.flush()

    conversation = Conversation(
        tenant_id=tenant.id,
        contact_id=contact.id,
        account_id=account.id,
        last_message_at=MOMENT,
        last_inbound_at=MOMENT,
    )
    knowledge_base = KnowledgeBase(tenant_id=tenant.id, name=f"{slug} handbook")
    agent = Agent(
        tenant_id=tenant.id,
        name=f"{slug} agent",
        model="gpt-5",
        system_prompt="Be helpful.",
        status=AgentStatus.ACTIVE,
        is_default=True,
    )
    session.add_all([conversation, knowledge_base, agent])
    await session.flush()

    message = Message(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        wa_message_id=f"wamid-{slug}",
        direction=MessageDirection.INBOUND,
        kind=MessageKind.TEXT,
        status=MessageStatus.DELIVERED,
        # The canary. If any response body ever contains this string for the
        # wrong workspace, isolation has failed regardless of status code.
        body=f"SECRET-{slug.upper()}",
    )
    document = Document(
        tenant_id=tenant.id,
        knowledge_base_id=knowledge_base.id,
        title=f"{slug} pricing",
        source=DocumentSource.TEXT,
        status=DocumentStatus.READY,
        content_hash=uuid.uuid4().hex,
        content=f"SECRET-{slug.upper()}",
    )
    template = WhatsAppTemplate(
        tenant_id=tenant.id,
        account_id=account.id,
        name=f"{slug}_reminder",
        language="en",
        category=TemplateCategory.MARKETING,
        status=TemplateStatus.APPROVED,
    )
    lead = Lead(
        tenant_id=tenant.id,
        contact_id=contact.id,
        conversation_id=conversation.id,
        name="Ahmed Hassan",
        interest=f"SECRET-{slug.upper()}",
        status=LeadStatus.NEW,
        source=LeadSource.MANUAL,
        last_activity_at=MOMENT,
    )
    session.add_all([message, document, template, lead])
    await session.flush()

    media = MessageMedia(
        tenant_id=tenant.id,
        message_id=message.id,
        conversation_id=conversation.id,
        wa_media_id=f"media-{slug}",
        status=MediaStatus.STORED,
        mime_type="image/jpeg",
        byte_size=12,
        storage_key=f"{tenant.id}/{slug}.jpg",
    )
    note = LeadNote(tenant_id=tenant.id, lead_id=lead.id, body=f"SECRET-{slug.upper()}")
    campaign = Campaign(
        tenant_id=tenant.id,
        account_id=account.id,
        template_id=template.id,
        name=f"{slug} launch",
        status=CampaignStatus.DRAFT,
        messages_per_minute=10,
    )
    follow_up = FollowUp(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        scheduled_at=MOMENT + timedelta(hours=1),
        status=FollowUpStatus.PENDING,
        body="Still interested?",
    )
    invitation = TenantInvitation(
        tenant_id=tenant.id,
        email=f"invited@{slug}.example",
        role=TenantRole.MEMBER,
        token_hash=uuid.uuid4().hex,
        status=InvitationStatus.PENDING,
        expires_at=MOMENT + timedelta(days=7),
        invited_by_id=user.id,
    )
    invoice = Invoice(
        tenant_id=tenant.id,
        subscription_id=None,
        plan_code="starter",
        status=InvoiceStatus.OPEN,
        amount_due=Decimal("100.00"),
        currency="USD",
        period_start=MOMENT,
        period_end=MOMENT + timedelta(days=30),
        lines=[],
        issued_at=MOMENT,
    )
    session.add_all([media, note, campaign, follow_up, invitation, invoice])
    await session.flush()

    session.add(
        CampaignRecipient(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            contact_id=contact.id,
        )
    )
    await session.flush()

    token, _ = create_access_token(settings=settings, subject=user.id, tenant_id=tenant.id)
    return Workspace(
        tenant=tenant,
        user=user,
        token=token,
        account=account,
        contact=contact,
        conversation=conversation,
        message=message,
        media=media,
        lead=lead,
        note=note,
        agent=agent,
        knowledge_base=knowledge_base,
        document=document,
        template=template,
        campaign=campaign,
        follow_up=follow_up,
        invitation=invitation,
        invoice=invoice,
    )


@pytest.fixture
def isolation_app(
    settings: Settings,
    db_session: AsyncSession,
    fake_redis: FakeDependency,
) -> Iterator[FastAPI]:
    """The real application, wired to the test's transaction.

    The session override is what makes this end-to-end: routes resolve real
    services, which build real repositories, which query the rows seeded below.
    Only the entitlement service is faked, so a plan limit can never be mistaken
    for isolation.
    """
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
async def http(isolation_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=isolation_app),
        base_url="http://wasla.test",
    ) as client:
        yield client


@pytest_asyncio.fixture
async def victim(db_session: AsyncSession, settings: Settings) -> Workspace:
    return await _seed(db_session, settings, slug="victim")


@pytest_asyncio.fixture
async def attacker(db_session: AsyncSession, settings: Settings) -> Workspace:
    return await _seed(db_session, settings, slug="attacker")


# ------------------------------------------------------------- the attack set


def _attacks(target: Workspace) -> list[Attack]:
    """Every operation the API offers against another workspace's identifiers.

    Returned as (method, path, json, params) so the matrix stays readable and a
    new route added without a corresponding entry is visible as an omission
    rather than hidden inside a helper.
    """
    conversation = target.conversation.id
    lead = target.lead.id
    return [
        # --- conversations, messages and media
        ("GET", f"{API}/conversations/{conversation}", None, None),
        ("GET", f"{API}/conversations/{conversation}/messages", None, None),
        ("POST", f"{API}/conversations/{conversation}/messages", {"body": "hello"}, None),
        (
            "POST",
            f"{API}/conversations/{conversation}/messages/template",
            {"name": "greeting", "language": "en"},
            None,
        ),
        (
            "GET",
            f"{API}/conversations/{conversation}/media/{target.media.id}",
            None,
            None,
        ),
        ("POST", f"{API}/conversations/{conversation}/mode", {"mode": "human"}, None),
        ("POST", f"{API}/conversations/{conversation}/priority", {"priority": "urgent"}, None),
        ("POST", f"{API}/conversations/{conversation}/assignment", {}, None),
        ("POST", f"{API}/conversations/{conversation}/close", None, None),
        ("POST", f"{API}/conversations/{conversation}/reopen", None, None),
        # --- analytics for one conversation
        (
            "GET",
            f"{API}/analytics/conversations/{conversation}/events",
            None,
            None,
        ),
        # --- contacts
        ("POST", f"{API}/contacts/{target.contact.id}/opt-out", {}, None),
        ("DELETE", f"{API}/contacts/{target.contact.id}/opt-out", None, None),
        # --- leads
        ("GET", f"{API}/leads/{lead}", None, None),
        ("POST", f"{API}/leads/{lead}/status", {"status": "won"}, None),
        ("POST", f"{API}/leads/{lead}/assignment", {}, None),
        ("POST", f"{API}/leads/{lead}/score", {"score": 90}, None),
        ("GET", f"{API}/leads/{lead}/notes", None, None),
        ("POST", f"{API}/leads/{lead}/notes", {"body": "mine now"}, None),
        ("GET", f"{API}/leads/{lead}/activity", None, None),
        # --- agents
        ("POST", f"{API}/agents/{target.agent.id}/default", None, None),
        ("GET", f"{API}/agents/{target.agent.id}/tools", None, None),
        (
            "PUT",
            f"{API}/agents/{target.agent.id}/tools",
            {"name": "search_knowledge", "enabled": True},
            None,
        ),
        (
            "DELETE",
            f"{API}/agents/{target.agent.id}/tools/search_knowledge",
            None,
            None,
        ),
        # --- knowledge
        ("GET", f"{API}/knowledge/bases/{target.knowledge_base.id}", None, None),
        ("GET", f"{API}/knowledge/bases/{target.knowledge_base.id}/documents", None, None),
        (
            "POST",
            f"{API}/knowledge/bases/{target.knowledge_base.id}/documents",
            {"title": "theirs", "content": "planted"},
            None,
        ),
        ("GET", f"{API}/knowledge/documents/{target.document.id}", None, None),
        ("POST", f"{API}/knowledge/documents/{target.document.id}/ingest", None, None),
        ("DELETE", f"{API}/knowledge/documents/{target.document.id}", None, None),
        # --- campaigns
        ("GET", f"{API}/campaigns/{target.campaign.id}", None, None),
        ("POST", f"{API}/campaigns/{target.campaign.id}/audience", {}, None),
        ("POST", f"{API}/campaigns/{target.campaign.id}/schedule", {}, None),
        ("POST", f"{API}/campaigns/{target.campaign.id}/pause", None, None),
        ("POST", f"{API}/campaigns/{target.campaign.id}/cancel", None, None),
        ("GET", f"{API}/campaigns/{target.campaign.id}/statistics", None, None),
        ("GET", f"{API}/campaigns/{target.campaign.id}/recipients", None, None),
        # --- campaigns created *against* another workspace's number/template
        (
            "POST",
            f"{API}/campaigns",
            {
                "account_id": str(target.account.id),
                "template_id": str(target.template.id),
                "name": "hijack",
            },
            None,
        ),
        (
            "POST",
            f"{API}/campaigns/audience/preview",
            {"account_id": str(target.account.id)},
            None,
        ),
        # --- templates
        ("GET", f"{API}/templates/{target.template.id}", None, None),
        ("POST", f"{API}/templates/sync", None, {"account_id": str(target.account.id)}),
        # --- follow-ups
        ("GET", f"{API}/follow-ups/{target.follow_up.id}", None, None),
        ("POST", f"{API}/follow-ups/{target.follow_up.id}/cancel", {}, None),
        (
            "POST",
            f"{API}/follow-ups",
            {"conversation_id": str(conversation), "body": "planted", "delay_minutes": 30},
            None,
        ),
        # --- whatsapp numbers
        ("POST", f"{API}/whatsapp/accounts/{target.account.id}/disable", None, None),
        ("POST", f"{API}/whatsapp/accounts/{target.account.id}/enable", None, None),
        # --- invitations and billing
        ("DELETE", f"{API}/invitations/{target.invitation.id}", None, None),
        ("GET", f"{API}/invoices/{target.invoice.id}", None, None),
        ("GET", f"{API}/invoices/{target.invoice.id}/payments", None, None),
        # --- leads created *referencing* another workspace's rows
        (
            "POST",
            f"{API}/leads",
            {"contact_id": str(target.contact.id), "name": "planted"},
            None,
        ),
        (
            "POST",
            f"{API}/leads",
            {"conversation_id": str(conversation), "name": "planted"},
            None,
        ),
    ]


def _ids(attacks: list[Attack]) -> list[str]:
    """Readable test ids: the verb and the route, with the uuid stripped out."""
    labels = []
    for method, path, body, _ in attacks:
        trimmed = "/".join("{id}" if _looks_like_uuid(part) else part for part in path.split("/"))
        suffix = "+body" if body and any(k.endswith("_id") for k in body) else ""
        labels.append(f"{method} {trimmed}{suffix}")
    return labels


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


# ----------------------------------------------------------------- the proofs


async def test_every_operation_against_another_workspace_answers_not_found(
    http: AsyncClient,
    victim: Workspace,
    attacker: Workspace,
) -> None:
    """The whole matrix, in one test, so a gap is reported as a list.

    Parametrising would need the fixtures at collection time, which they cannot
    be — the rows do not exist until a test runs. Collecting the failures and
    asserting once at the end gives the same diagnostic value: a regression
    names every route it broke rather than only the first.
    """
    attacks = _attacks(victim)
    labels = _ids(attacks)
    reachable: list[str] = []
    leaked: list[str] = []

    for (method, path, body, params), label in zip(attacks, labels, strict=True):
        response = await http.request(
            method,
            path,
            json=body,
            params=params,
            headers=attacker.headers,
        )
        if response.status_code != 404:
            reachable.append(f"{label} -> {response.status_code}")
        if "SECRET-VICTIM" in response.text:
            leaked.append(label)

    assert leaked == [], f"another workspace's content appeared in the response: {leaked}"
    assert reachable == [], (
        "these routes did not answer 404 for another workspace's identifier: " f"{reachable}"
    )


async def test_the_owner_can_reach_everything_the_attacker_could_not(
    http: AsyncClient,
    victim: Workspace,
) -> None:
    """The control.

    Without this the suite above would pass against an API that answers 404 to
    everybody, which is isolation by breakage rather than by design.
    """
    reads = [
        f"{API}/conversations/{victim.conversation.id}",
        f"{API}/conversations/{victim.conversation.id}/messages",
        f"{API}/leads/{victim.lead.id}",
        f"{API}/leads/{victim.lead.id}/notes",
        f"{API}/leads/{victim.lead.id}/activity",
        f"{API}/agents/{victim.agent.id}/tools",
        f"{API}/knowledge/bases/{victim.knowledge_base.id}",
        f"{API}/knowledge/bases/{victim.knowledge_base.id}/documents",
        f"{API}/knowledge/documents/{victim.document.id}",
        f"{API}/campaigns/{victim.campaign.id}",
        f"{API}/campaigns/{victim.campaign.id}/statistics",
        f"{API}/campaigns/{victim.campaign.id}/recipients",
        f"{API}/templates/{victim.template.id}",
        f"{API}/follow-ups/{victim.follow_up.id}",
        f"{API}/invoices/{victim.invoice.id}",
        f"{API}/invoices/{victim.invoice.id}/payments",
    ]
    failures = []
    for path in reads:
        response = await http.get(path, headers=victim.headers)
        if response.status_code != 200:
            failures.append(f"{path} -> {response.status_code} {response.text[:120]}")

    assert failures == [], f"the owning workspace could not read its own rows: {failures}"


async def test_a_listing_never_includes_another_workspaces_rows(
    http: AsyncClient,
    victim: Workspace,
    attacker: Workspace,
) -> None:
    """Collection endpoints, which leak by inclusion rather than by lookup.

    A missing tenant predicate on a list route does not produce an error — it
    produces a longer list, which is why this is asserted separately from the
    id-addressed routes above.
    """
    listings = [
        f"{API}/conversations",
        f"{API}/leads",
        f"{API}/agents",
        f"{API}/campaigns",
        f"{API}/templates",
        f"{API}/follow-ups",
        f"{API}/knowledge/bases",
        f"{API}/whatsapp/accounts",
        f"{API}/invitations",
        f"{API}/invoices",
        f"{API}/audit-logs",
    ]
    leaks = []
    for path in listings:
        response = await http.get(path, headers=attacker.headers)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        text = response.text
        if "SECRET-VICTIM" in text or str(victim.tenant.id) in text:
            leaks.append(path)
        for identifier in (
            victim.conversation.id,
            victim.lead.id,
            victim.agent.id,
            victim.campaign.id,
            victim.template.id,
            victim.follow_up.id,
            victim.knowledge_base.id,
            victim.account.id,
            victim.invitation.id,
            victim.invoice.id,
        ):
            if str(identifier) in text:
                leaks.append(f"{path} (contains {identifier})")

    assert leaks == [], f"a listing exposed another workspace's rows: {leaks}"


async def test_a_workspace_cannot_assign_its_work_to_an_outsider(
    http: AsyncClient,
    victim: Workspace,
    attacker: Workspace,
) -> None:
    """The inverse direction: the attacker's own rows, someone else's user id.

    This is the assignment variant of the same trust question. The row being
    written is legitimately the attacker's, so a tenant filter on the *object*
    cannot catch it; what has to be checked is the identifier in the body.
    Getting it wrong would attach a workspace's conversations and pipeline to a
    person outside it, and expose that person's id back through the response.
    """
    outsider = {"assigned_to_id": str(victim.user.id)}

    conversation = await http.post(
        f"{API}/conversations/{attacker.conversation.id}/assignment",
        json=outsider,
        headers=attacker.headers,
    )
    assert conversation.status_code == 404

    lead = await http.post(
        f"{API}/leads/{attacker.lead.id}/assignment",
        json=outsider,
        headers=attacker.headers,
    )
    assert lead.status_code == 404

    created = await http.post(
        f"{API}/leads",
        json={"name": "planted", "assigned_to_id": str(victim.user.id)},
        headers=attacker.headers,
    )
    assert created.status_code == 404


async def test_a_token_naming_a_workspace_the_user_never_joined_is_refused(
    http: AsyncClient,
    victim: Workspace,
    attacker: Workspace,
    settings: Settings,
) -> None:
    """A forged workspace claim, signed with the real key.

    The tenant a request acts on comes from the token rather than from request
    input, which moves the question to what happens when the token itself names
    the wrong workspace. Anyone holding the signing key could mint this, and so
    can a bug in the issuing path — membership is therefore re-read on every
    request rather than trusted from the claim.
    """
    forged, _ = create_access_token(
        settings=settings,
        subject=attacker.user.id,
        tenant_id=victim.tenant.id,
    )
    headers = {"Authorization": f"Bearer {forged}"}

    listing = await http.get(f"{API}/conversations", headers=headers)
    assert listing.status_code == 404
    assert "SECRET-VICTIM" not in listing.text

    direct = await http.get(
        f"{API}/conversations/{victim.conversation.id}",
        headers=headers,
    )
    assert direct.status_code == 404


async def test_a_suspended_workspace_stops_serving_immediately(
    http: AsyncClient,
    db_session: AsyncSession,
    attacker: Workspace,
) -> None:
    """Suspension is re-read per request, not carried in the token.

    An access token issued before a workspace was suspended stays signed and
    unexpired. If the check happened only at issuance, that token would keep
    working until it aged out.
    """
    attacker.tenant.status = TenantStatus.SUSPENDED
    await db_session.flush()

    response = await http.get(f"{API}/conversations", headers=attacker.headers)
    assert response.status_code == 403


async def test_platform_administration_is_closed_to_a_workspace_owner(
    http: AsyncClient,
    attacker: Workspace,
    victim: Workspace,
) -> None:
    """Owning a workspace grants nothing across the platform.

    The platform routes are the one place in the API that reads across tenants
    by design, so the guard on them is the whole of the isolation story there.
    """
    refused = [
        ("GET", f"{API}/platform/overview", None),
        ("GET", f"{API}/platform/tenants", None),
        ("GET", f"{API}/platform/audit-logs", None),
        (
            "POST",
            f"{API}/platform/invoices/{victim.invoice.id}/payments",
            {"amount": "100.00", "currency": "USD", "provider": "manual"},
        ),
        ("POST", f"{API}/platform/invoices/{victim.invoice.id}/void", None),
    ]
    for method, path, body in refused:
        response = await http.request(method, path, json=body, headers=attacker.headers)
        assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
        assert "SECRET-VICTIM" not in response.text


async def test_platform_staff_are_recognised_by_role_not_by_membership(
    http: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    attacker: Workspace,
) -> None:
    """The counterpart to the test above, so the 403s there mean something.

    Granting the platform role — and nothing else, no new membership — opens
    the platform routes. That is what proves those 403s came from the platform
    guard rather than from a route that is broken for everyone.
    """
    attacker.user.platform_role = PlatformRole.PLATFORM_OWNER
    await db_session.flush()

    token, _ = create_access_token(
        settings=settings,
        subject=attacker.user.id,
        tenant_id=attacker.tenant.id,
    )
    response = await http.get(
        f"{API}/platform/tenants",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


async def test_an_id_from_one_workspace_is_not_reachable_through_a_nested_route(
    http: AsyncClient,
    victim: Workspace,
    attacker: Workspace,
) -> None:
    """Nested routes, where two ids are checked and only one may be enforced.

    The dangerous shape is a handler that scopes the parent and then trusts the
    child, or the reverse. Each combination below pairs one workspace's parent
    with the other's child.
    """
    combinations = [
        # attacker's conversation, victim's media
        f"{API}/conversations/{attacker.conversation.id}/media/{victim.media.id}",
        # victim's conversation, attacker's media
        f"{API}/conversations/{victim.conversation.id}/media/{attacker.media.id}",
        # victim's conversation, victim's media, attacker's token
        f"{API}/conversations/{victim.conversation.id}/media/{victim.media.id}",
    ]
    for path in combinations:
        response = await http.get(path, headers=attacker.headers)
        assert response.status_code == 404, f"{path} -> {response.status_code}"

    # The victim's own document, reached through the attacker's knowledge base.
    crossed = await http.get(
        f"{API}/knowledge/documents/{victim.document.id}",
        headers=attacker.headers,
    )
    assert crossed.status_code == 404


async def test_a_guessed_identifier_is_indistinguishable_from_a_real_one(
    http: AsyncClient,
    victim: Workspace,
    attacker: Workspace,
) -> None:
    """The oracle test.

    A route that answers 404 for a random id and 403 for a real one belonging
    to someone else has told the caller that the real one exists. Both must
    answer identically, body included.
    """
    invented = uuid.uuid4()
    paths = [
        (f"{API}/conversations/{{}}", victim.conversation.id),
        (f"{API}/leads/{{}}", victim.lead.id),
        (f"{API}/campaigns/{{}}", victim.campaign.id),
        (f"{API}/templates/{{}}", victim.template.id),
        (f"{API}/follow-ups/{{}}", victim.follow_up.id),
        (f"{API}/knowledge/documents/{{}}", victim.document.id),
        (f"{API}/invoices/{{}}", victim.invoice.id),
    ]
    for template, real in paths:
        theirs = await http.get(template.format(real), headers=attacker.headers)
        nowhere = await http.get(template.format(invented), headers=attacker.headers)
        assert theirs.status_code == nowhere.status_code, template
        assert theirs.json()["error"]["message"] == nowhere.json()["error"]["message"], template
