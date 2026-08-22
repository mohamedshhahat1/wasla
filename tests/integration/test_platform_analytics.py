"""The platform view, and the boundary that keeps it out of everyone else's hands.

Two things are proved here and they pull in opposite directions. The service is
*supposed* to see every workspace, which is the one place in this codebase where
crossing a tenant boundary is correct - so the aggregation is checked against
several workspaces' rows. And the routes are supposed to be unreachable without
a platform role, including by the owner of a workspace, so the real guard runs
rather than a mock of it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.api.dependencies import (
    get_current_user,
    get_platform_analytics_service,
)
from app.db.models import PlatformRole, Tenant, TenantStatus, User
from app.db.models.usage import UsageEventType, UsageUnit
from app.db.models.whatsapp import WhatsAppAccount, WhatsAppAccountStatus
from app.platform.platform_analytics import (
    PlatformAnalyticsService,
    PlatformOverview,
    WorkspacePage,
    WorkspaceRow,
)
from app.platform.platform_usage import PlatformUsageService
from app.repositories.usage_repository import UsageTotal
from app.services.usage_service import UsageRecorder, resolve_window, summarise

pytestmark = pytest.mark.integration

PATH = "/api/v1/platform"
SINCE = datetime(2026, 8, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 1, tzinfo=UTC)
INSIDE = datetime(2026, 8, 15, tzinfo=UTC)


async def _tenant(session, slug: str, *, status=TenantStatus.ACTIVE, name=None) -> Tenant:
    tenant = Tenant(name=name or slug.title(), slug=slug, status=status)
    session.add(tenant)
    await session.flush()
    return tenant


async def _number(session, tenant, *, status=WhatsAppAccountStatus.ACTIVE):
    account = WhatsAppAccount(
        tenant_id=tenant.id,
        phone_number_id=f"phone-{uuid.uuid4().hex[:10]}",
        waba_id="555000111",
        display_phone_number="+201000000000",
        status=status,
    )
    session.add(account)
    await session.flush()
    return account


# ------------------------------------------------------------------ the service


async def test_the_overview_counts_every_workspace_and_its_numbers(db_session):
    live = await _tenant(db_session, "acme")
    await _tenant(db_session, "rival")
    await _tenant(db_session, "paused", status=TenantStatus.SUSPENDED)
    await _number(db_session, live)
    await _number(db_session, live, status=WhatsAppAccountStatus.DISABLED)
    await db_session.flush()

    overview = await PlatformAnalyticsService(db_session).overview(since=SINCE, until=UNTIL)

    assert overview.tenants_total == 3
    assert overview.tenants_active == 2
    assert overview.tenants_suspended == 1
    assert overview.whatsapp_numbers == 2
    assert overview.whatsapp_numbers_active == 1


async def test_a_deleted_workspace_is_not_counted(db_session):
    """It is gone as far as an operator is concerned, and including it would
    make the total disagree with the list under it."""
    await _tenant(db_session, "acme")
    gone = await _tenant(db_session, "closed")
    gone.deleted_at = datetime.now(UTC)
    await db_session.flush()

    overview = await PlatformAnalyticsService(db_session).overview(since=SINCE, until=UNTIL)
    assert overview.tenants_total == 1


async def test_platform_usage_is_the_sum_of_every_workspace(db_session):
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    UsageRecorder(db_session, tenant_id=acme.id).record(
        UsageEventType.WHATSAPP_MESSAGE_SENT,
        quantity=3,
        occurred_at=INSIDE,
    )
    UsageRecorder(db_session, tenant_id=rival.id).record(
        UsageEventType.WHATSAPP_MESSAGE_SENT,
        quantity=4,
        occurred_at=INSIDE,
    )
    await db_session.flush()

    overview = await PlatformAnalyticsService(db_session).overview(since=SINCE, until=UNTIL)
    assert overview.usage.messages_sent == 7


async def test_each_workspace_carries_its_own_consumption(db_session):
    acme = await _tenant(db_session, "acme")
    rival = await _tenant(db_session, "rival")
    UsageRecorder(db_session, tenant_id=acme.id).ai_request(
        input_tokens=500,
        output_tokens=50,
        occurred_at=INSIDE,
    )
    await db_session.flush()

    page = await PlatformAnalyticsService(db_session).workspaces(since=SINCE, until=UNTIL)
    by_slug = {row.tenant.slug: row for row in page.rows}

    assert page.total == 2
    assert by_slug["acme"].usage.total_tokens == 550
    # A workspace that consumed nothing reads as zero, not as absent: a caller
    # should never have to tell "no rows" apart from "no usage".
    assert by_slug["rival"].usage.total_tokens == 0
    assert by_slug["rival"].tenant.id == rival.id


async def test_workspaces_can_be_searched_by_name_or_address(db_session):
    await _tenant(db_session, "acme-finishing", name="Acme Finishing")
    await _tenant(db_session, "zeta", name="Zeta Contracting")
    await db_session.flush()

    service = PlatformAnalyticsService(db_session)
    by_name = await service.workspaces(search="finishing", since=SINCE, until=UNTIL)
    by_slug = await service.workspaces(search="zeta", since=SINCE, until=UNTIL)

    assert [row.tenant.slug for row in by_name.rows] == ["acme-finishing"]
    assert [row.tenant.slug for row in by_slug.rows] == ["zeta"]


async def test_a_wildcard_in_a_search_is_not_a_wildcard(db_session):
    """A search for "100%" must match a workspace called that, not everything."""
    await _tenant(db_session, "hundred", name="100% Finishing")
    await _tenant(db_session, "other", name="Other")
    await db_session.flush()

    page = await PlatformAnalyticsService(db_session).workspaces(
        search="%",
        since=SINCE,
        until=UNTIL,
    )
    assert [row.tenant.slug for row in page.rows] == ["hundred"]


async def test_workspaces_can_be_filtered_by_status(db_session):
    await _tenant(db_session, "acme")
    await _tenant(db_session, "paused", status=TenantStatus.SUSPENDED)
    await db_session.flush()

    page = await PlatformAnalyticsService(db_session).workspaces(
        status=TenantStatus.SUSPENDED,
        since=SINCE,
        until=UNTIL,
    )
    assert [row.tenant.slug for row in page.rows] == ["paused"]
    assert page.total == 1


async def test_the_page_total_counts_matches_not_the_page(db_session):
    for index in range(5):
        await _tenant(db_session, f"tenant-{index}")
    await db_session.flush()

    page = await PlatformAnalyticsService(db_session).workspaces(limit=2, since=SINCE, until=UNTIL)
    assert len(page.rows) == 2
    assert page.total == 5


async def test_per_tenant_usage_of_an_empty_page_asks_nothing(db_session):
    """The guard that keeps an empty dashboard from aggregating the platform."""
    window = resolve_window(since=SINCE, until=UNTIL)
    assert await PlatformUsageService(db_session).by_tenant([], window=window) == {}


# ------------------------------------------------------------------- the routes


class StubPlatform:
    """Stands in for the service so the guard is what is under test."""

    def __init__(self) -> None:
        self.calls = 0

    async def overview(self, *, since=None, until=None):
        self.calls += 1
        raise AssertionError("The guard should have refused this request.")

    async def workspaces(self, **kwargs):
        self.calls += 1
        raise AssertionError("The guard should have refused this request.")


def _as_user(app, role: PlatformRole | None) -> None:
    from app.api.dependencies import CurrentUser
    from app.core.security import TokenClaims, TokenType

    user = User(
        id=uuid.uuid4(),
        email="someone@example.com",
        is_active=True,
        platform_role=role,
    )
    claims = TokenClaims(
        subject=user.id,
        token_type=TokenType.ACCESS,
        token_id=uuid.uuid4(),
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC),
        tenant_id=None,
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user=user, claims=claims)


@pytest.fixture
def platform(app) -> StubPlatform:
    stub = StubPlatform()
    app.dependency_overrides[get_platform_analytics_service] = lambda: stub
    return stub


async def test_a_workspace_owner_is_refused(client, app, platform):
    """Owning a workspace grants nothing across the platform."""
    _as_user(app, None)

    response = await client.get(f"{PATH}/overview")

    assert response.status_code == 403
    assert platform.calls == 0


async def test_an_anonymous_caller_is_refused(client, app, platform):
    response = await client.get(f"{PATH}/tenants")

    assert response.status_code == 401
    assert platform.calls == 0


class AnsweringPlatform:
    """Returns fixed figures, so the response shape is what is asserted."""

    async def overview(self, *, since=None, until=None):
        window = resolve_window(since=since, until=until)
        return PlatformOverview(
            window=window,
            usage=summarise(
                [
                    UsageTotal(
                        event_type=UsageEventType.WHATSAPP_MESSAGE_SENT,
                        unit=UsageUnit.COUNT,
                        quantity=120,
                        events=120,
                    )
                ],
                window=window,
            ),
            tenants_total=4,
            tenants_active=3,
            tenants_suspended=1,
            whatsapp_numbers=6,
            whatsapp_numbers_active=5,
        )

    async def workspaces(self, *, since=None, until=None, **kwargs):
        window = resolve_window(since=since, until=until)
        return WorkspacePage(
            window=window,
            total=1,
            rows=[
                WorkspaceRow(
                    tenant=Tenant(
                        id=uuid.uuid4(),
                        name="Acme",
                        slug="acme",
                        status=TenantStatus.ACTIVE,
                        created_at=datetime.now(UTC),
                    ),
                    usage=summarise([], window=window),
                )
            ],
        )


@pytest.fixture
def answering(app) -> AnsweringPlatform:
    stub = AnsweringPlatform()
    app.dependency_overrides[get_platform_analytics_service] = lambda: stub
    return stub


async def test_platform_staff_can_read_the_overview(client, app, answering):
    _as_user(app, PlatformRole.PLATFORM_ADMIN)

    response = await client.get(f"{PATH}/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["tenants_active"] == 3
    assert body["whatsapp_numbers_active"] == 5
    assert body["usage"]["messages_sent"] == 120
    # No revenue field, and none until there are subscriptions to compute one
    # from: a plausible zero is worse than an absent figure.
    assert "mrr" not in body
    assert "revenue" not in body


async def test_platform_staff_can_list_workspaces_with_their_usage(client, app, answering):
    _as_user(app, PlatformRole.PLATFORM_OWNER)

    response = await client.get(f"{PATH}/tenants", params={"search": "acme"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["tenant"]["slug"] == "acme"
    # A workspace that consumed nothing still reports every counter.
    assert body["items"][0]["counters"]["messages_sent"] == 0
