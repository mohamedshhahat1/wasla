# ruff: noqa: F811 - the capacity-reduction harness fixtures are imported by name.
"""What platform staff read about entitlements (PLAT-G2, PLAT-G3, PLAT-G5; ADR-132).

Every read goes through the real ASGI app. The "same as the tenant" tests call
the tenant's own route and the platform's route for the same workspace, both
for real, and compare the two bodies field by field - the platform read is the
same computation, not a second one. Reductions are opened by real grant
withdrawals through `boundary()`.

What is proved:

- the reduction queue lists every workspace's reductions, the grace ending
  soonest first, filtered by status, cause, workspace and grace window; one
  reduction's detail shows what it kept and disabled and, while open, the
  fallback preview the owner's page shows; a workspace's history is newest
  first;
- staff read a workspace's channel capacity and connections exactly as the
  workspace does, and never a credential;
- the channel vocabulary marks only the channels this deployment operates;
- every new read is audited, every new route refuses every tenant role, and
  an unknown workspace or reduction is 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_channel_registry
from app.channels.adapter import ChannelAdapter
from app.channels.registry import ChannelRegistry
from app.core.config import Settings
from app.core.crypto import generate_key
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.channel import Channel
from app.db.models.enums import MembershipStatus, TenantRole
from app.db.models.membership import Membership
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppAccount
from app.integrations.whatsapp.adapter import WhatsAppAdapter
from app.platform.topup_admin import TopupAdmin
from app.schemas.topup import TopupGrantWithdraw
from app.services.credential_service import CredentialService
from app.services.whatsapp_account_service import WhatsAppAccountService
from tests.channel_fakes import SyntheticAdapter
from tests.fake_ownership import FakeOwnershipVerifier
from tests.integration.test_capacity_reductions import (  # noqa: F401 - fixtures
    IG,
    MS,
    WA,
    _act_as,
    _connect,
    _reductions,
    _settings,
    app,
    http,
)
from tests.integration.test_grant_withdrawal import _granted, _purchase, _staff
from tests.integration.test_platform_billing_api import _act_as as _act_as_user
from tests.integration.test_platform_billing_api import _user

pytestmark = pytest.mark.integration

BASE = "/api/v1/platform/billing"
TOKEN = "EAAG-platform-read-secret-0123456789"
NEW_READS = (
    "/capacity-reductions",
    "/capacity-reductions/{reduction}",
    "/tenants/{tenant}/capacity-reductions",
    "/tenants/{tenant}/channel-capacity",
    "/tenants/{tenant}/channel-connections",
    "/channel-types",
)


async def _over_capacity(
    session: AsyncSession, staff: User, *, withdrawn_at: datetime
) -> tuple[Tenant, User, list[uuid.UUID]]:
    """A workspace two connections over a one-slot plan once its grant of two is withdrawn."""
    tenant, owner, grant = await _granted(session, staff, quantity=2)
    ids = [await _connect(session, tenant, owner, channel) for channel in (WA, IG, MS)]
    purchase = await _purchase(session, grant)
    await TopupAdmin(session, settings=_settings()).withdraw_grant(
        grant,
        TopupGrantWithdraw(
            tenant_id=tenant.id, reason="Granted in error.", expected_revision=purchase.revision
        ),
        actor=staff,
        now=withdrawn_at,
    )
    return tenant, owner, ids


def _of(body: dict[str, Any], tenants: set[uuid.UUID]) -> list[dict[str, Any]]:
    return [item for item in body["items"] if uuid.UUID(item["tenant_id"]) in tenants]


# -------------------------------------------------------- reduction queue


async def test_staff_list_open_reductions_across_workspaces_ordered_by_grace_end(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    now = datetime.now(UTC)
    later, _, _ = await _over_capacity(db_session, staff, withdrawn_at=now - timedelta(days=1))
    sooner, _, _ = await _over_capacity(db_session, staff, withdrawn_at=now - timedelta(days=3))
    _act_as_user(app, staff)

    response = await http.get(f"{BASE}/capacity-reductions", params={"status": "pending_selection"})

    assert response.status_code == 200, response.text
    body = response.json()
    mine = _of(body, {later.id, sooner.id})
    assert [item["tenant_id"] for item in mine] == [str(sooner.id), str(later.id)]
    ends = [item["grace_ends_at"] for item in body["items"]]
    assert ends == sorted(ends), "the grace ending soonest comes first"
    first = mine[0]
    assert first["cause"] == "grant_withdrawn"
    assert first["tenant_name"] == sooner.name
    assert (first["active_now"], first["would_be_disabled_count"]) == (3, 2)
    assert first["target_general_limit"] == 1
    assert first["topup_purchase_id"] is not None


async def test_reduction_filters_by_status_cause_tenant_and_grace_window(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    now = datetime.now(UTC)
    open_, _, _ = await _over_capacity(db_session, staff, withdrawn_at=now - timedelta(days=1))
    resolved, owner, ids = await _over_capacity(
        db_session, staff, withdrawn_at=now - timedelta(days=2)
    )
    service = _reductions(db_session, resolved)
    reduction = await service.open_reduction()
    assert reduction is not None
    await service.select([ids[0]], expected_revision=reduction.revision, actor=owner)
    _act_as_user(app, staff)

    async def tenants(**params: Any) -> set[str]:
        response = await http.get(f"{BASE}/capacity-reductions", params=params)
        assert response.status_code == 200, response.text
        return {item["tenant_id"] for item in response.json()["items"]}

    assert await tenants(tenant_id=str(open_.id)) == {str(open_.id)}
    assert await tenants(tenant_id=str(resolved.id)) == {str(resolved.id)}
    pending = await tenants(status="pending_selection")
    assert str(open_.id) in pending and str(resolved.id) not in pending
    both = await tenants(status=["pending_selection", "resolved_by_owner"], cause="grant_withdrawn")
    assert {str(open_.id), str(resolved.id)} <= both
    assert await tenants(cause="downgrade", tenant_id=str(open_.id)) == set()
    # The open one's grace ends in six days, the resolved one's in five.
    window = {
        "grace_ends_after": (now + timedelta(days=5, hours=12)).isoformat(),
        "grace_ends_before": (now + timedelta(days=6, hours=12)).isoformat(),
    }
    found = await tenants(**window)
    assert str(open_.id) in found and str(resolved.id) not in found
    bad = await http.get(f"{BASE}/capacity-reductions", params={"status": "maybe"})
    assert bad.status_code == 422


async def test_a_reduction_detail_shows_kept_disabled_selection_and_the_fallback_preview(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, owner, ids = await _over_capacity(db_session, staff, withdrawn_at=datetime.now(UTC))
    reduction = await _reductions(db_session, tenant).open_reduction()
    assert reduction is not None
    _act_as(app, tenant, owner, TenantRole.TENANT_OWNER)
    _act_as_user(app, staff)

    detail = await http.get(f"{BASE}/capacity-reductions/{reduction.id}")
    page = await http.get("/api/v1/billing/channel-capacity")

    assert detail.status_code == 200, detail.text
    assert page.status_code == 200, page.text
    open_ = detail.json()
    assert open_["status"] == "pending_selection"
    assert open_["automatic_fallback"] == page.json()["automatic_fallback"]
    assert open_["automatic_fallback"]["keep"] == [str(ids[0])]
    assert open_["would_be_disabled_count"] == len(open_["automatic_fallback"]["disable"]) == 2
    assert (open_["kept_connection_ids"], open_["disabled_connection_ids"]) == ([], [])
    assert open_["preselected"] == page.json()["preselected"] == []

    await _reductions(db_session, tenant).select(
        [ids[1]], expected_revision=reduction.revision, actor=owner
    )
    closed = (await http.get(f"{BASE}/capacity-reductions/{reduction.id}")).json()

    assert closed["status"] == "resolved_by_owner"
    assert closed["kept_connection_ids"] == [str(ids[1])]
    assert set(closed["disabled_connection_ids"]) == {str(ids[0]), str(ids[2])}
    assert closed["automatic_fallback"] is None
    assert closed["would_be_disabled_count"] == 2
    assert closed["active_now"] == 1


async def test_a_workspaces_reduction_history_lists_every_reduction_newest_first(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, owner, ids = await _over_capacity(
        db_session, staff, withdrawn_at=datetime.now(UTC) - timedelta(days=2)
    )
    service = _reductions(db_session, tenant)
    first = await service.open_reduction()
    assert first is not None
    await service.select([ids[0]], expected_revision=first.revision, actor=owner)
    # A second grant, a second connection, and the second grant withdrawn too.
    _, _, again = await _granted(db_session, staff, quantity=1, tenant=tenant)
    await _connect(db_session, tenant, owner, IG)
    await TopupAdmin(db_session, settings=_settings()).withdraw_grant(
        again,
        TopupGrantWithdraw(
            tenant_id=tenant.id,
            reason="Second one in error too.",
            expected_revision=(await _purchase(db_session, again)).revision,
        ),
        actor=staff,
    )
    second = await service.open_reduction()
    assert second is not None and second.id != first.id
    _act_as_user(app, staff)

    response = await http.get(f"{BASE}/tenants/{tenant.id}/capacity-reductions")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [str(second.id), str(first.id)]
    assert [item["status"] for item in body["items"]] == ["pending_selection", "resolved_by_owner"]
    page = await http.get(
        f"{BASE}/tenants/{tenant.id}/capacity-reductions", params={"limit": 1, "offset": 1}
    )
    assert [item["id"] for item in page.json()["items"]] == [str(first.id)]


# ----------------------------------------------------- workspace channels


async def _typed_workspace(session: AsyncSession, staff: User) -> tuple[Tenant, User]:
    """General and typed slots, three connections, a reduction open: every field filled."""
    tenant, owner, typed = await _granted(session, staff, channel=Channel.INSTAGRAM)
    _, _, general = await _granted(session, staff, quantity=1, tenant=tenant)
    for channel in (WA, IG, MS):
        await _connect(session, tenant, owner, channel)
    await TopupAdmin(session, settings=_settings()).withdraw_grant(
        general,
        TopupGrantWithdraw(
            tenant_id=tenant.id,
            reason="Wrong workspace.",
            expected_revision=(await _purchase(session, general)).revision,
        ),
        actor=staff,
    )
    assert typed
    return tenant, owner


async def test_staff_read_the_same_channel_capacity_the_tenant_reads(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, owner = await _typed_workspace(db_session, staff)
    _act_as(app, tenant, owner, TenantRole.MEMBER)
    _act_as_user(app, staff)

    theirs = await http.get("/api/v1/billing/channel-capacity")
    ours = await http.get(f"{BASE}/tenants/{tenant.id}/channel-capacity")

    assert theirs.status_code == 200, theirs.text
    assert ours.status_code == 200, ours.text
    assert ours.json() == theirs.json()
    body = ours.json()
    # Non-vacuous: typed slots, a reduction and its preview are all present.
    assert [slot["channel"] for slot in body["breakdown"]["typed_slots"]] == ["instagram"]
    assert body["reduction"]["cause"] == "grant_withdrawn"
    assert body["automatic_fallback"] is not None
    assert (body["active"], body["over_limit"]) == (3, True)


async def test_staff_read_the_same_channel_connections_the_tenant_reads(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, owner = await _typed_workspace(db_session, staff)
    _act_as(app, tenant, owner, TenantRole.MEMBER)
    _act_as_user(app, staff)

    for params in ({}, {"channel": "instagram"}):
        theirs = await http.get("/api/v1/channel-connections", params=params)
        ours = await http.get(f"{BASE}/tenants/{tenant.id}/channel-connections", params=params)
        assert theirs.status_code == ours.status_code == 200, (theirs.text, ours.text)
        assert ours.json() == theirs.json()
    assert len((await http.get(f"{BASE}/tenants/{tenant.id}/channel-connections")).json()) == 3


async def test_the_platform_channel_reads_never_expose_credentials(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, _, _ = await _granted(db_session, staff)
    number = f"7{uuid.uuid4().int % 10**11:011d}"
    sealing = Settings(
        _env_file=None, environment="test", credential_encryption_keys=[generate_key()]
    )
    await WhatsAppAccountService(
        session=db_session,
        ownership=FakeOwnershipVerifier().owns(number),
        credentials=CredentialService(sealing),
    ).connect(tenant_id=tenant.id, phone_number_id=number, access_token=TOKEN)
    account = await db_session.scalar(
        select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == number)
    )
    assert account is not None and account.access_token_encrypted
    secrets = {TOKEN, account.access_token_encrypted, TOKEN[5:]}
    _act_as_user(app, staff)

    bodies = [
        (await http.get(f"{BASE}/tenants/{tenant.id}/channel-connections")).text,
        (await http.get(f"{BASE}/tenants/{tenant.id}/channel-capacity")).text,
        (await http.get(f"{BASE}/tenants/{tenant.id}/capacity-reductions")).text,
    ]

    assert number in bodies[0], "non-vacuous: the number itself is listed"
    found = [secret for secret in secrets for body in bodies if secret in body]
    assert found == []
    for key in ("access_token", "token", "secret", "credential", "verification_code"):
        assert all(f'"{key}' not in body for body in bodies), key


# ------------------------------------------------------------ vocabulary


def _registry_with_instagram() -> ChannelRegistry:
    return ChannelRegistry(
        {
            Channel.WHATSAPP: WhatsAppAdapter(),
            Channel.INSTAGRAM: cast(ChannelAdapter, SyntheticAdapter(Channel.INSTAGRAM)),
        },
        paused=[Channel.INSTAGRAM],
    )


async def test_channel_types_lists_the_vocabulary_and_marks_whatsapp_operable_only(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    _act_as_user(app, await _staff(db_session))

    response = await http.get(f"{BASE}/channel-types")

    assert response.status_code == 200, response.text
    body = {item["channel"]: (item["operable"], item["state"]) for item in response.json()}
    assert set(body) == {channel.value for channel in Channel}
    assert body["whatsapp"] == (True, "operational")
    assert {channel for channel, (operable, _) in body.items() if operable} == {"whatsapp"}
    assert all(
        state == "unavailable" for channel, (_, state) in body.items() if channel != "whatsapp"
    )

    app.dependency_overrides[get_channel_registry] = _registry_with_instagram
    flipped = {item["channel"]: item for item in (await http.get(f"{BASE}/channel-types")).json()}
    assert (flipped["instagram"]["operable"], flipped["instagram"]["state"]) == (True, "paused")
    assert flipped["telegram"]["operable"] is False


# ------------------------------------------------- audit, roles, 404s


async def _paths(session: AsyncSession, staff: User) -> tuple[Tenant, list[str]]:
    tenant, _, _ = await _over_capacity(session, staff, withdrawn_at=datetime.now(UTC))
    reduction = await _reductions(session, tenant).open_reduction()
    assert reduction is not None
    return tenant, [
        BASE + path.format(tenant=tenant.id, reduction=reduction.id) for path in NEW_READS
    ]


async def test_every_new_platform_read_is_audited(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    staff = await _staff(db_session)
    tenant, paths = await _paths(db_session, staff)
    _act_as_user(app, staff)

    for path in paths:
        assert (await http.get(path)).status_code == 200, path

    entries = list(
        await db_session.scalars(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.PLATFORM_BILLING_READ)
            .where(AuditLog.actor_id == staff.id)
        )
    )
    resources = {(entry.meta or {}).get("resource"): entry for entry in entries}
    expected = {
        "billing.capacity_reductions": None,
        "billing.capacity_reduction": tenant.id,
        "billing.workspace_capacity_reductions": tenant.id,
        "billing.channel_capacity": tenant.id,
        "billing.channel_connections": tenant.id,
        "billing.channel_types": None,
    }
    assert set(expected) <= set(resources), sorted(str(name) for name in resources)
    # A workspace's read is findable from its side: the workspace is the target.
    for resource, tenant_id in expected.items():
        assert resources[resource].target_id == tenant_id, resource


@pytest.mark.parametrize(
    "role", [TenantRole.MEMBER, TenantRole.TENANT_ADMIN, TenantRole.TENANT_OWNER]
)
async def test_a_tenant_role_gets_403_on_every_new_platform_route(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient, role: TenantRole
) -> None:
    staff = await _staff(db_session)
    tenant, paths = await _paths(db_session, staff)
    person = await _user(db_session)
    db_session.add(
        Membership(
            tenant_id=tenant.id, user_id=person.id, role=role, status=MembershipStatus.ACTIVE
        )
    )
    await db_session.flush()
    _act_as_user(app, person)

    for path in paths:
        response = await http.get(path)
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "permission_denied", path
    withdraw = await http.post(
        f"{BASE}/topup-purchases/{uuid.uuid4()}/withdraw",
        json={"tenant_id": str(tenant.id), "reason": "Not mine to take.", "expected_revision": 1},
    )
    assert withdraw.status_code == 403
    assert withdraw.json()["error"]["code"] == "permission_denied"


async def test_an_unknown_tenant_or_reduction_is_404(
    db_session: AsyncSession, app: FastAPI, http: AsyncClient
) -> None:
    _act_as_user(app, await _staff(db_session))
    nobody = uuid.uuid4()

    for path in (
        f"{BASE}/tenants/{nobody}/capacity-reductions",
        f"{BASE}/tenants/{nobody}/channel-capacity",
        f"{BASE}/tenants/{nobody}/channel-connections",
        f"{BASE}/capacity-reductions/{nobody}",
    ):
        response = await http.get(path)
        assert response.status_code == 404, (path, response.text)
