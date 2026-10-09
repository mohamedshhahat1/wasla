"""Channel capacity, enforced on every activation path (ENT-05..ENT-09).

The whole application is built - the real routes, the real dependency graph,
the real `WhatsAppAccountService` and `ChannelCapacityGuard`, the real entitlement
engine - against real PostgreSQL. Meta's Graph API is faked at the socket, so
"the workspace at capacity never made Wasla call Meta" is a count of requests
that left the process, not of calls into a stub. Connections on the synthetic
channels go through `ChannelConnectionService`, the neutral flow every future
adapter must use, under a registry that operates them.

Proved here, against the baseline the implementation started from (2c58c35):

* a second connection on a one-connection plan is **409
  `channel_capacity_exceeded`** (was 402), and Meta is asked nothing;
* disabling frees the slot, and **enabling needs it back** - the baseline let a
  1-slot workspace reach 2 active by enabling a disabled number;
* re-authorising an active number takes no new slot; disabling and releasing
  are never refused, even over capacity;
* a WhatsApp number released frees a slot an Instagram connection can take;
* typed slots serve their own type first; capacity is judged before type;
* a plan's channel types are enforced: TikTok on Pro is **409
  `channel_type_not_allowed`** with a slot free;
* a version published before ADR-131 is held to its number limit and to
  WhatsApp alone;
* another workspace's connections never count.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import ActiveWorkspace, get_active_workspace
from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.core.dependencies import get_session
from app.db.models.billing import LimitKey, Plan
from app.db.models.channel import Channel, ChannelConnection, ConnectionStatus
from app.db.models.enums import MembershipStatus, TenantRole
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.topup import TopupEntitlement, TopupPurchase, TopupSource, TopupStatus
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.integrations.whatsapp import ownership as ownership_module
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.main import create_app
from app.services.channel_capacity import (
    ChannelCapacityExceededError,
    ChannelTypeNotAllowedError,
)
from app.services.channel_connection_service import ChannelConnectionService
from app.services.entitlement_service import EntitlementService
from app.services.plan_catalog import PlanCatalog
from app.services.subscription_service import SubscriptionService
from tests.billing_fixtures import add_owner
from tests.channel_fakes import SyntheticAdapter
from tests.integration.plan_catalogue import own_plan

pytestmark = pytest.mark.integration

ACCOUNTS = "/api/v1/whatsapp/accounts"
ALL = ["whatsapp", "instagram", "messenger", "telegram", "tiktok"]


class MetaGraph:
    """Meta's Graph API at the socket: answers ownership for any number, counted."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        number = request.url.path.rstrip("/").split("/")[-1]
        return httpx.Response(
            200,
            json={
                "id": number,
                "display_phone_number": "+20 100 000 0000",
                "verified_name": "Capacity Co",
                "whatsapp_business_account": {"id": "555000111"},
            },
        )


class _Infra:
    def __init__(self) -> None:
        self.commands = self

    @property
    def client(self) -> _Infra:
        return self.commands

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return -1

    async def rpush(self, key: str, value: str) -> int:
        return 1

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> MetaGraph:
    desk = MetaGraph()

    def guarded(*_args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(desk.handler),
            base_url="https://graph.facebook.com",
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(ownership_module, "build_guarded_client", guarded)
    return desk


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        jwt_secret="channel-capacity-secret-not-for-deployment",
        rate_limit_enabled=False,
        default_plan_code="starter",
    )


@pytest.fixture
def app(db_session: AsyncSession, graph: MetaGraph) -> Iterator[FastAPI]:
    application = create_app(_settings())
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wasla.test") as c:
        yield c


def _registry() -> ChannelRegistry:
    """WhatsApp and every synthetic channel operational, as the adapter stage will make them."""
    adapters: dict[Channel, ChannelAdapter] = {Channel.WHATSAPP: WhatsAppAdapter()}
    for channel in (Channel.INSTAGRAM, Channel.MESSENGER, Channel.TELEGRAM, Channel.TIKTOK):
        adapters[channel] = cast(ChannelAdapter, SyntheticAdapter(channel))
    return ChannelRegistry(adapters)


async def _plan(
    session: AsyncSession, code: str, connections: int | None, types: list[str]
) -> Plan:
    limits: dict[str, Any] = {} if connections is None else {"channel_connections": connections}
    return await own_plan(
        session,
        code=code,
        price=Decimal("0.00"),
        limits=limits,
        allowed_channel_types=types,
    )


async def _workspace(
    session: AsyncSession, app: FastAPI | None, *, plan: Plan, name: str = "Capacity Co"
) -> tuple[Tenant, User]:
    tenant = Tenant(name=name, slug=f"cap-{uuid.uuid4().hex[:10]}")
    session.add(tenant)
    await session.flush()
    owner = await add_owner(session, tenant)
    await SubscriptionService(session, tenant_id=tenant.id).start(
        plan_code=plan.code, now=datetime.now(UTC), self_service=False
    )
    if app is not None:
        _act_as(app, tenant, owner)
    return tenant, owner


def _act_as(app: FastAPI, tenant: Tenant, owner: User) -> None:
    app.dependency_overrides[get_active_workspace] = lambda: ActiveWorkspace(
        user=owner,
        membership=Membership(
            id=uuid.uuid4(),
            user_id=owner.id,
            tenant_id=tenant.id,
            role=TenantRole.TENANT_OWNER,
            status=MembershipStatus.ACTIVE,
        ),
        tenant=tenant,
    )


def _number_body(number: str) -> dict[str, str]:
    return {"phone_number_id": number, "access_token": "EAAG-test-token", "waba_id": "555000111"}


async def _active(session: AsyncSession, tenant: Tenant) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ChannelConnection)
            .where(ChannelConnection.tenant_id == tenant.id)
            .where(ChannelConnection.status == ConnectionStatus.ACTIVE)
            .where(ChannelConnection.released_at.is_(None))
        )
        or 0
    )


def _neutral(session: AsyncSession, tenant: Tenant) -> ChannelConnectionService:
    return ChannelConnectionService(
        session, tenant_id=tenant.id, default_plan_code="starter", registry=_registry()
    )


def _number_id() -> str:
    return f"1{uuid.uuid4().int % 10**11:011d}"


# ------------------------------------------------------------------ WhatsApp


async def test_a_second_connection_on_a_one_connection_plan_is_409_and_meta_is_not_asked(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, graph: MetaGraph
) -> None:
    plan = await _plan(db_session, "starter", 1, ["whatsapp"])
    tenant, _ = await _workspace(db_session, app, plan=plan)

    first = await http.post(ACCOUNTS, json=_number_body(_number_id()))
    assert first.status_code == 201, first.text
    asked_for_the_first = len(graph.calls)
    assert asked_for_the_first > 0, "the first claim was proven against Meta"

    second = await http.post(ACCOUNTS, json=_number_body(_number_id()))
    assert second.status_code == 409, second.text
    error = second.json()["error"]
    assert error["code"] == "channel_capacity_exceeded"
    assert error["details"] == {
        "effective_limit": 1,
        "active": 1,
        "channel": "whatsapp",
        "typed_capacity": {},
        "over_limit": False,
    }
    assert len(graph.calls) == asked_for_the_first, "a workspace at capacity never calls Meta"
    assert await _active(db_session, tenant) == 1


async def test_disabling_frees_the_slot_and_enabling_needs_it_back(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """The baseline's bypass, closed: enable #1 while #2 holds the only slot is 409."""
    plan = await _plan(db_session, "starter", 1, ["whatsapp"])
    tenant, _ = await _workspace(db_session, app, plan=plan)
    one = (await http.post(ACCOUNTS, json=_number_body(_number_id()))).json()["id"]

    assert (await http.post(f"{ACCOUNTS}/{one}/disable")).status_code == 200
    connection = await db_session.get(ChannelConnection, uuid.UUID(one))
    assert connection is not None
    await db_session.refresh(connection)
    assert connection.disabled_reason is not None and connection.disabled_reason.value == "manual"

    two = await http.post(ACCOUNTS, json=_number_body(_number_id()))
    assert two.status_code == 201, "a disabled number frees its slot"

    enable = await http.post(f"{ACCOUNTS}/{one}/enable")
    assert enable.status_code == 409, enable.text
    assert enable.json()["error"]["code"] == "channel_capacity_exceeded"
    assert await _active(db_session, tenant) == 1

    assert (await http.post(f"{ACCOUNTS}/{two.json()['id']}/release")).status_code == 200
    again = await http.post(f"{ACCOUNTS}/{one}/enable")
    assert again.status_code == 200, again.text
    reason = await db_session.scalar(
        select(ChannelConnection.disabled_reason).where(ChannelConnection.id == connection.id)
    )
    assert reason is None, "enabling clears why it was disabled"
    assert await _active(db_session, tenant) == 1


async def test_enabling_an_active_number_asks_nothing_and_changes_nothing(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    plan = await _plan(db_session, "starter", 1, ["whatsapp"])
    tenant, _ = await _workspace(db_session, app, plan=plan)
    one = (await http.post(ACCOUNTS, json=_number_body(_number_id()))).json()["id"]
    response = await http.post(f"{ACCOUNTS}/{one}/enable")
    assert response.status_code == 200, "an active number at capacity is not refused"
    assert await _active(db_session, tenant) == 1


async def test_reauthorising_an_active_number_takes_no_new_slot(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, graph: MetaGraph
) -> None:
    plan = await _plan(db_session, "starter", 1, ["whatsapp"])
    tenant, _ = await _workspace(db_session, app, plan=plan)
    one = (await http.post(ACCOUNTS, json=_number_body(_number_id()))).json()["id"]
    before = len(graph.calls)
    response = await http.post(f"{ACCOUNTS}/{one}/verify", json={"access_token": "EAAG-new"})
    assert response.status_code == 200, response.text
    assert len(graph.calls) > before, "the credential was proven again"
    assert await _active(db_session, tenant) == 1
    state = await EntitlementService(db_session, tenant_id=tenant.id).check(
        LimitKey.CHANNEL_CONNECTIONS, additional=0
    )
    assert (state.limit, state.used, state.over_limit) == (1, 1, False)


async def test_disabling_and_releasing_are_never_refused_even_over_capacity(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    plan = await _plan(db_session, "starter", 3, ["whatsapp"])
    tenant, _ = await _workspace(db_session, app, plan=plan)
    ids = [
        (await http.post(ACCOUNTS, json=_number_body(_number_id()))).json()["id"] for _ in range(3)
    ]
    # The plan now allows one: the workspace is over capacity.
    await _plan(db_session, "starter", 1, ["whatsapp"])
    subscription = await SubscriptionService(db_session, tenant_id=tenant.id).get()
    assert subscription is not None
    current = await PlanCatalog(db_session).current_version(plan)
    assert current is not None
    subscription.plan_version_id = current.id
    await db_session.flush()
    state = await EntitlementService(db_session, tenant_id=tenant.id).check(
        LimitKey.CHANNEL_CONNECTIONS, additional=0
    )
    assert (state.limit, state.used, state.over_limit, state.remaining) == (1, 3, True, 0)

    refused = await http.post(ACCOUNTS, json=_number_body(_number_id()))
    assert refused.status_code == 409
    assert (await http.post(f"{ACCOUNTS}/{ids[0]}/disable")).status_code == 200
    assert (await http.post(f"{ACCOUNTS}/{ids[1]}/release")).status_code == 200
    assert await _active(db_session, tenant) == 1


async def test_another_workspaces_connections_never_count(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    plan = await _plan(db_session, "starter", 1, ["whatsapp"])
    rival, _ = await _workspace(db_session, None, plan=plan, name="Rival")
    for index in range(5):
        db_session.add(
            WhatsAppAccount(
                tenant_id=rival.id,
                phone_number_id=f"rival-{uuid.uuid4().hex[:10]}",
                waba_id="rival",
                display_phone_number=f"+2010000005{index}",
                status=WhatsAppAccountStatus.ACTIVE,
            )
        )
    await db_session.flush()
    tenant, _ = await _workspace(db_session, app, plan=plan)
    response = await http.post(ACCOUNTS, json=_number_body(_number_id()))
    assert response.status_code == 201, response.text
    assert (await _active(db_session, tenant), await _active(db_session, rival)) == (1, 5)


# ----------------------------------------------------------- any channel


async def test_a_released_number_frees_a_slot_another_channel_can_take(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    """ENT-07: delete (release) WhatsApp #1, create Instagram #1 on a 1-connection plan."""
    plan = await _plan(db_session, "starter", 1, ["whatsapp", "instagram"])
    tenant, owner = await _workspace(db_session, app, plan=plan)
    one = (await http.post(ACCOUNTS, json=_number_body(_number_id()))).json()["id"]
    with pytest.raises(ChannelCapacityExceededError):
        await _neutral(db_session, tenant).connect(
            channel=Channel.INSTAGRAM, external_account_id="ig-1", actor=owner
        )
    assert (await http.post(f"{ACCOUNTS}/{one}/release")).status_code == 200
    connected = await _neutral(db_session, tenant).connect(
        channel=Channel.INSTAGRAM, external_account_id="ig-1", actor=owner
    )
    assert connected.is_active and connected.channel is Channel.INSTAGRAM
    assert await _active(db_session, tenant) == 1


async def test_a_type_the_plan_does_not_include_is_refused_with_a_slot_free(
    db_session: AsyncSession,
) -> None:
    """ENT-09: Pro allows WhatsApp, Instagram and Messenger - not TikTok, slot or no slot."""
    plan = await _plan(db_session, "pro", 3, ["whatsapp", "instagram", "messenger"])
    tenant, owner = await _workspace(db_session, None, plan=plan)
    with pytest.raises(ChannelTypeNotAllowedError) as refused:
        await _neutral(db_session, tenant).connect(
            channel=Channel.TIKTOK, external_account_id="tt-1", actor=owner
        )
    assert refused.value.status_code == 409
    assert refused.value.details == {
        "channel": "tiktok",
        "allowed_channel_types": ["whatsapp", "instagram", "messenger"],
    }
    connected = await _neutral(db_session, tenant).connect(
        channel=Channel.INSTAGRAM, external_account_id="ig-pro", actor=owner
    )
    assert connected.is_active


async def test_an_adapter_is_refused_before_it_asks_its_provider(
    db_session: AsyncSession,
) -> None:
    """ENT-08's pre-check, for the adapters to come.

    A connect flow proves ownership with its provider before it can call
    `connect`, so `precheck` is what it calls first: refused at capacity and
    for a type the plan does not include, writing nothing - the provider is
    never asked. M-E12b's killer.
    """
    plan = await _plan(db_session, "starter", 1, ["whatsapp", "instagram"])
    tenant, owner = await _workspace(db_session, None, plan=plan)
    neutral = _neutral(db_session, tenant)
    await neutral.precheck(Channel.INSTAGRAM)
    with pytest.raises(ChannelTypeNotAllowedError):
        await neutral.precheck(Channel.TIKTOK)
    await neutral.connect(channel=Channel.INSTAGRAM, external_account_id="ig-pre", actor=owner)
    with pytest.raises(ChannelCapacityExceededError) as full:
        await neutral.precheck(Channel.INSTAGRAM)
    assert full.value.details is not None
    assert (full.value.details["effective_limit"], full.value.details["active"]) == (1, 1)
    assert await _active(db_session, tenant) == 1


async def test_capacity_is_judged_before_type(db_session: AsyncSession) -> None:
    """Both fail: the workspace is told it is full before it is told the type is not on its plan."""
    plan = await _plan(db_session, "starter", 1, ["whatsapp", "instagram"])
    tenant, owner = await _workspace(db_session, None, plan=plan)
    await _neutral(db_session, tenant).connect(
        channel=Channel.INSTAGRAM, external_account_id="ig-full", actor=owner
    )
    with pytest.raises(ChannelCapacityExceededError):
        await _neutral(db_session, tenant).connect(
            channel=Channel.TIKTOK, external_account_id="tt-full", actor=owner
        )


async def test_a_typed_slot_serves_only_its_own_type(db_session: AsyncSession) -> None:
    """ENT-11: 1 general + 1 Instagram slot: a second Page is refused, an Instagram is not."""
    plan = await _plan(db_session, "pro", 1, ["whatsapp", "instagram", "messenger"])
    tenant, owner = await _workspace(db_session, None, plan=plan)
    await _grant(db_session, tenant, quantity=1, channel=Channel.INSTAGRAM)
    neutral = _neutral(db_session, tenant)
    await neutral.connect(channel=Channel.MESSENGER, external_account_id="pg-1", actor=owner)
    with pytest.raises(ChannelCapacityExceededError) as full:
        await neutral.connect(channel=Channel.MESSENGER, external_account_id="pg-2", actor=owner)
    assert full.value.details is not None
    assert full.value.details["typed_capacity"] == {"instagram": 1}
    await neutral.connect(channel=Channel.INSTAGRAM, external_account_id="ig-typed", actor=owner)
    with pytest.raises(ChannelCapacityExceededError):
        await neutral.connect(channel=Channel.INSTAGRAM, external_account_id="ig-2", actor=owner)
    state = await EntitlementService(db_session, tenant_id=tenant.id).check(
        LimitKey.CHANNEL_CONNECTIONS, additional=0
    )
    assert state.capacity is not None
    assert (state.limit, state.used, state.remaining) == (2, 2, 0)
    assert state.capacity.typed_used(Channel.INSTAGRAM) == 1


async def test_an_enabled_connection_of_another_channel_needs_its_slot_back(
    db_session: AsyncSession,
) -> None:
    plan = await _plan(db_session, "starter", 1, ["whatsapp", "instagram", "messenger"])
    tenant, owner = await _workspace(db_session, None, plan=plan)
    neutral = _neutral(db_session, tenant)
    ig = await neutral.connect(channel=Channel.INSTAGRAM, external_account_id="ig-a", actor=owner)
    await neutral.disable(ig.id, actor=owner)
    await neutral.connect(channel=Channel.MESSENGER, external_account_id="pg-a", actor=owner)
    with pytest.raises(ChannelCapacityExceededError):
        await neutral.enable(ig.id, actor=owner)
    assert await _active(db_session, tenant) == 1


async def test_a_connection_with_no_adapter_is_not_connected(db_session: AsyncSession) -> None:
    """Allowed by the plan and a slot free, but nothing operates Telegram here."""
    from app.channels.registry import ChannelUnavailableError

    plan = await _plan(db_session, "business", 10, ALL)
    tenant, owner = await _workspace(db_session, None, plan=plan)
    whatsapp_only = ChannelConnectionService(
        db_session, tenant_id=tenant.id, default_plan_code="starter"
    )
    with pytest.raises(ChannelUnavailableError):
        await whatsapp_only.connect(
            channel=Channel.TELEGRAM, external_account_id="tg-1", actor=owner
        )
    assert await _active(db_session, tenant) == 0


# ---------------------------------------------------------- legacy terms


async def test_a_version_published_before_adr_131_is_held_to_its_number_limit(
    db_session: AsyncSession,
) -> None:
    """ENT-05 choice A, against a row shaped exactly as one written before 0093."""
    plan = await _plan(db_session, "starter", 1, ["whatsapp"])
    tenant, owner = await _workspace(db_session, None, plan=plan)
    legacy_id = uuid.uuid4()
    # A row the version trigger would now refuse - no channel types, the retired
    # key - which is exactly what a version published before 0093 is. Disabled
    # inside this test's rolled-back transaction only; the immutability trigger
    # is untouched.
    await db_session.execute(
        text("ALTER TABLE plan_versions DISABLE TRIGGER plan_versions_entitlement_terms")
    )
    await db_session.execute(
        text(
            "INSERT INTO plan_versions (id, plan_id, version, name, price, currency, interval,"
            " trial_days, limits, effective_at, created_at)"
            " VALUES (:id, :plan, 99, 'Legacy', 0, 'EGP', 'monthly', 0,"
            " CAST('{\"whatsapp_numbers\": 2}' AS jsonb), :at, :at)"
        ),
        {"id": legacy_id, "plan": plan.id, "at": datetime.now(UTC) - timedelta(days=400)},
    )
    await db_session.execute(
        text("ALTER TABLE plan_versions ENABLE TRIGGER plan_versions_entitlement_terms")
    )
    subscription = await SubscriptionService(db_session, tenant_id=tenant.id).get()
    assert subscription is not None
    subscription.plan_version_id = legacy_id
    await db_session.flush()

    entitlements = EntitlementService(db_session, tenant_id=tenant.id)
    capacity = await entitlements.channel_capacity()
    assert (capacity.base, capacity.allowed) == (2, frozenset({Channel.WHATSAPP}))
    for index in range(2):
        db_session.add(
            WhatsAppAccount(
                tenant_id=tenant.id,
                phone_number_id=f"legacy-{uuid.uuid4().hex[:10]}",
                waba_id="legacy",
                display_phone_number=f"+2010000009{index}",
            )
        )
    await db_session.flush()
    with pytest.raises(ChannelCapacityExceededError):
        await _neutral(db_session, tenant).connect(
            channel=Channel.INSTAGRAM, external_account_id="ig-legacy", actor=owner
        )
    # Release one: the slot is free, and Instagram is still not on the legacy terms.
    account = await db_session.scalar(
        select(WhatsAppAccount).where(WhatsAppAccount.tenant_id == tenant.id).limit(1)
    )
    assert account is not None
    account.status = WhatsAppAccountStatus.RELEASED
    account.released_at = datetime.now(UTC)
    await db_session.flush()
    with pytest.raises(ChannelTypeNotAllowedError):
        await _neutral(db_session, tenant).connect(
            channel=Channel.INSTAGRAM, external_account_id="ig-legacy", actor=owner
        )
    # And the trigger refuses a new version shaped like it.
    with pytest.raises(Exception, match="states the channel types"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO plan_versions (id, plan_id, version, name, price, currency,"
                    " interval, trial_days, limits, effective_at, created_at)"
                    " VALUES (:id, :plan, 100, 'New', 0, 'EGP', 'monthly', 0,"
                    " CAST('{}' AS jsonb), now(), now())"
                ),
                {"id": uuid.uuid4(), "plan": plan.id},
            )


async def test_a_new_version_naming_the_retired_key_is_refused_by_the_database(
    db_session: AsyncSession,
) -> None:
    plan = await _plan(db_session, "starter", 1, ["whatsapp"])
    for limits, types, message in (
        ('{"whatsapp_numbers": 1}', ["whatsapp"], "retired"),
        ("{}", ["whatsapp", "whatsapp"], "named once"),
        ("{}", ["whatsapp", "fax"], "unknown channel type"),
    ):
        with pytest.raises(Exception, match=message):
            async with db_session.begin_nested():
                await db_session.execute(
                    text(
                        "INSERT INTO plan_versions (id, plan_id, version, name, price, currency,"
                        " interval, trial_days, limits, allowed_channel_types, effective_at,"
                        " created_at) VALUES (:id, :plan, 101, 'New', 0, 'EGP', 'monthly', 0,"
                        " CAST(:limits AS jsonb), :types, now(), now())"
                    ),
                    {"id": uuid.uuid4(), "plan": plan.id, "limits": limits, "types": types},
                )


# ------------------------------------------------------------- helpers


async def _grant(
    session: AsyncSession, tenant: Tenant, *, quantity: int, channel: Channel | None
) -> TopupPurchase:
    """A live platform grant of channel slots, general or typed, until the term ends."""
    subscription = await SubscriptionService(session, tenant_id=tenant.id).get()
    assert subscription is not None
    now = datetime.now(UTC)
    grant = TopupPurchase(
        tenant_id=tenant.id,
        subscription_id=subscription.id,
        source=TopupSource.PLATFORM_GRANT,
        product_name="Platform grant",
        entitlement_key=TopupEntitlement.CHANNEL_CONNECTIONS,
        channel_type=channel,
        quantity=quantity,
        unit_price=Decimal("0.00"),
        total_amount=Decimal("0.00"),
        currency="EGP",
        billing_period_start=subscription.current_period_start,
        billing_period_end=subscription.current_period_end,
        expires_at=subscription.current_period_end,
        status=TopupStatus.GRANTED,
        granted_at=now - timedelta(seconds=1),
        reason="Test grant.",
    )
    session.add(grant)
    await session.flush()
    return grant
