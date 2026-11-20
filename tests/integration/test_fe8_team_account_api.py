"""FE-8's backend half over HTTP: a member's role, an invitation's sender, an account's ways in.

FEB-42 (RC): `PATCH /workspace/members/{user_id}` moves an active member
between member and administrator. An owner may change anyone's but an owner's
and their own; an administrator may promote a member but not demote a peer;
nobody changes their own; ownership is never assigned; a removed person is
refused; the same role again is answered unchanged and not audited; every
change is audited with what it was and what it became.

FEB-44 (IB): every invitation read says when it was sent and who sent it -
null when that account is closed, whose kept address is never read back.

FEB-45 (SM): `GET /auth/me` says whether the account has a password and a
Google identity, and nothing else of either.

Real rows and real signed tokens; FE-7's fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Membership, MembershipStatus, TenantInvitation, TenantRole, User
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.identity import FederatedIdentity, IdentityProvider
from tests.integration import test_fe7_account_api as _shared
from tests.integration.test_fe7_account_api import (
    API,
    _bearer,
    _login,
    _user,
    _workspace,
)

# The shared fixtures, bound by name so pytest finds them here.
app = _shared.app
fe7_settings = _shared.fe7_settings
http = _shared.http

pytestmark = pytest.mark.integration


async def _member(session: AsyncSession, *, tenant_id: Any, email: str, role: TenantRole) -> User:
    user = await _user(session, email=email)
    session.add(Membership(tenant_id=tenant_id, user_id=user.id, role=role))
    await session.flush()
    return user


async def _team(session: AsyncSession, slug: str) -> dict[str, Any]:
    owner = await _user(session, email=f"{slug}-owner@example.com")
    tenant = await _workspace(session, slug=slug, member=owner)
    people: dict[str, Any] = {"tenant": tenant, "owner": owner}
    for name, role in (
        ("owner2", TenantRole.TENANT_OWNER),
        ("admin", TenantRole.TENANT_ADMIN),
        ("admin2", TenantRole.TENANT_ADMIN),
        ("member", TenantRole.MEMBER),
        ("member2", TenantRole.MEMBER),
    ):
        people[name] = await _member(
            session, tenant_id=tenant.id, email=f"{slug}-{name}@example.com", role=role
        )
    return people


async def _change(http: AsyncClient, session: dict[str, Any], user: User, role: str) -> Any:
    return await http.patch(
        f"{API}/workspace/members/{user.id}", json={"role": role}, headers=_bearer(session)
    )


async def _role_changes(db: AsyncSession, tenant_id: Any) -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == AuditAction.MEMBER_ROLE_CHANGED,
        )
    )
    return list(rows.scalars())


# --- FEB-42: changing a member's role -----------------------------------------------


async def test_an_owner_promotes_and_demotes_and_each_change_is_audited(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session, "rc-owner")
    session = await _login(http, "rc-owner-owner@example.com", "rc-owner")

    promoted = await _change(http, session, team["member"], "tenant_admin")
    assert promoted.status_code == 200, promoted.text
    body = promoted.json()
    assert body["role"] == "tenant_admin" and body["user_id"] == str(team["member"].id)

    demoted = await _change(http, session, team["admin"], "member")
    assert demoted.status_code == 200, demoted.text
    assert demoted.json()["role"] == "member"

    audits = await _role_changes(db_session, team["tenant"].id)
    assert sorted(str((row.meta or {}).get("to")) for row in audits) == ["member", "tenant_admin"]
    for row in audits:
        assert row.actor_id == team["owner"].id
        assert row.target_type == "membership"
        assert set((row.meta or {}).keys()) == {"from", "to"}


async def test_the_new_role_authorizes_the_next_request(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session, "rc-next")
    owner = await _login(http, "rc-next-owner@example.com", "rc-next")
    member = await _login(http, "rc-next-member@example.com", "rc-next")

    before = await http.get(f"{API}/invitations", headers=_bearer(member))
    assert before.status_code == 403
    assert (await _change(http, owner, team["member"], "tenant_admin")).status_code == 200
    after = await http.get(f"{API}/invitations", headers=_bearer(member))
    assert after.status_code == 200, after.text


async def test_an_administrator_promotes_a_member_but_never_demotes_a_peer(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session, "rc-admin")
    session = await _login(http, "rc-admin-admin@example.com", "rc-admin")

    promoted = await _change(http, session, team["member"], "tenant_admin")
    assert promoted.status_code == 200, promoted.text

    refused = await _change(http, session, team["admin2"], "member")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "member_role_admin_owner_required"
    unchanged = await db_session.get(Membership, (await _membership(db_session, team["admin2"])))
    assert unchanged is not None and unchanged.role is TenantRole.TENANT_ADMIN


async def _membership(db: AsyncSession, user: User) -> Any:
    row = await db.execute(select(Membership.id).where(Membership.user_id == user.id))
    return row.scalar_one()


@pytest.mark.parametrize("caller", ["owner", "admin"])
async def test_nobody_changes_their_own_role(
    http: AsyncClient, db_session: AsyncSession, caller: str
) -> None:
    slug = f"rc-self-{caller}"
    team = await _team(db_session, slug)
    session = await _login(http, f"{slug}-{caller}@example.com", slug)
    response = await _change(http, session, team[caller], "member")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "member_role_self"


async def test_an_owners_role_is_never_changed_here(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session, "rc-own")
    session = await _login(http, "rc-own-owner@example.com", "rc-own")
    response = await _change(http, session, team["owner2"], "member")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "member_role_owner"


async def test_owner_is_not_a_role_a_change_can_ask_for(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session, "rc-ask")
    session = await _login(http, "rc-ask-owner@example.com", "rc-ask")
    response = await _change(http, session, team["member"], "tenant_owner")
    assert response.status_code == 422


async def test_a_member_cannot_change_anybodys_role(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session, "rc-mem")
    session = await _login(http, "rc-mem-member@example.com", "rc-mem")
    response = await _change(http, session, team["member2"], "tenant_admin")
    assert response.status_code == 403
    assert await _role_changes(db_session, team["tenant"].id) == []


async def test_a_removed_person_is_refused(http: AsyncClient, db_session: AsyncSession) -> None:
    team = await _team(db_session, "rc-gone")
    membership = await db_session.get(Membership, await _membership(db_session, team["member"]))
    assert membership is not None
    membership.status = MembershipStatus.REVOKED
    membership.revoked_at = datetime.now(UTC)
    await db_session.flush()
    session = await _login(http, "rc-gone-owner@example.com", "rc-gone")
    response = await _change(http, session, team["member"], "tenant_admin")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "member_not_active"


async def test_another_workspaces_member_is_not_found_here(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _team(db_session, "rc-a")
    other = await _team(db_session, "rc-b")
    session = await _login(http, "rc-a-owner@example.com", "rc-a")
    response = await _change(http, session, other["member"], "tenant_admin")
    assert response.status_code == 422
    still = await db_session.get(Membership, await _membership(db_session, other["member"]))
    assert still is not None and still.role is TenantRole.MEMBER


async def test_the_same_role_again_is_answered_unchanged_and_not_audited(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session, "rc-same")
    session = await _login(http, "rc-same-owner@example.com", "rc-same")
    response = await _change(http, session, team["member"], "member")
    assert response.status_code == 200 and response.json()["role"] == "member"
    assert await _role_changes(db_session, team["tenant"].id) == []


async def test_unknown_fields_are_refused(http: AsyncClient, db_session: AsyncSession) -> None:
    team = await _team(db_session, "rc-extra")
    session = await _login(http, "rc-extra-owner@example.com", "rc-extra")
    response = await http.patch(
        f"{API}/workspace/members/{team['member'].id}",
        json={"role": "tenant_admin", "status": "revoked"},
        headers=_bearer(session),
    )
    assert response.status_code == 422


# --- FEB-44: who sent an invitation, and when ---------------------------------------


async def test_an_invitation_says_who_sent_it_and_when(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session, "ib-sent")
    session = await _login(http, "ib-sent-admin@example.com", "ib-sent")

    created = await http.post(
        f"{API}/invitations", json={"email": "ib-new@example.com"}, headers=_bearer(session)
    )
    assert created.status_code == 201, created.text
    sender = created.json()["invited_by"]
    assert sender == {
        "user_id": str(team["admin"].id),
        "full_name": team["admin"].full_name,
        "email": team["admin"].email,
    }
    assert created.json()["created_at"]

    listed = await http.get(f"{API}/invitations", headers=_bearer(session))
    assert listed.status_code == 200
    (row,) = [entry for entry in listed.json() if entry["email"] == "ib-new@example.com"]
    assert row["invited_by"] == sender
    stored = await db_session.get(TenantInvitation, row["id"])
    assert stored is not None
    assert datetime.fromisoformat(row["created_at"]) == stored.created_at


async def test_a_closed_senders_address_is_never_read_back(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session, "ib-closed")
    admin = await _login(http, "ib-closed-admin@example.com", "ib-closed")
    created = await http.post(
        f"{API}/invitations", json={"email": "ib-closed-new@example.com"}, headers=_bearer(admin)
    )
    assert created.status_code == 201, created.text

    gone = await db_session.get(User, team["admin"].id)
    assert gone is not None
    gone.deleted_at = datetime.now(UTC)
    await db_session.flush()

    owner = await _login(http, "ib-closed-owner@example.com", "ib-closed")
    listed = await http.get(f"{API}/invitations", headers=_bearer(owner))
    (row,) = [entry for entry in listed.json() if entry["email"] == "ib-closed-new@example.com"]
    assert row["invited_by"] is None
    assert "ib-closed-admin@example.com" not in listed.text


async def test_a_sender_who_left_the_workspace_still_sent_it(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    team = await _team(db_session, "ib-left")
    admin = await _login(http, "ib-left-admin@example.com", "ib-left")
    created = await http.post(
        f"{API}/invitations", json={"email": "ib-left-new@example.com"}, headers=_bearer(admin)
    )
    assert created.status_code == 201
    membership = await db_session.get(Membership, await _membership(db_session, team["admin"]))
    assert membership is not None
    membership.status = MembershipStatus.REVOKED
    membership.revoked_at = datetime.now(UTC)
    await db_session.flush()

    owner = await _login(http, "ib-left-owner@example.com", "ib-left")
    listed = await http.get(f"{API}/invitations", headers=_bearer(owner))
    (row,) = [entry for entry in listed.json() if entry["email"] == "ib-left-new@example.com"]
    assert row["invited_by"]["user_id"] == str(team["admin"].id)


# --- FEB-45: which ways in the account has ------------------------------------------


async def test_me_says_whether_the_account_has_a_password_and_google(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _user(db_session, email="sm-me@example.com")
    await _workspace(db_session, slug="sm-me", member=user)
    session = await _login(http, "sm-me@example.com")

    async def facts() -> tuple[bool, bool]:
        me = await http.get(f"{API}/auth/me", headers=_bearer(session))
        assert me.status_code == 200, me.text
        body = me.json()
        assert "google_email" not in body and "provider_subject" not in me.text
        return body["has_password"], body["google_connected"]

    assert await facts() == (True, False)

    identity = FederatedIdentity(
        user_id=user.id, provider=IdentityProvider.GOOGLE, provider_subject="sm-subject-1"
    )
    db_session.add(identity)
    await db_session.flush()
    assert await facts() == (True, True)

    user.hashed_password = None
    await db_session.flush()
    assert await facts() == (False, True)

    await db_session.delete(identity)
    await db_session.flush()
    assert await facts() == (False, False)


async def test_patch_me_reports_the_same_facts(http: AsyncClient, db_session: AsyncSession) -> None:
    await _user(db_session, email="sm-patch@example.com")
    session = await _login(http, "sm-patch@example.com")
    changed = await http.patch(f"{API}/auth/me", json={"locale": "ar"}, headers=_bearer(session))
    assert changed.status_code == 200, changed.text
    assert changed.json()["has_password"] is True
    assert changed.json()["google_connected"] is False
