"""The account's language, the last-used workspace and FE-7's codes, over HTTP (ADR-201).

FEB-09: `GET /auth/me` reads the account's language, `PATCH /auth/me` writes
it (and nothing else), sign-up and invitations carry one, and every e-mail is
queued in the language `locale_for_recipient` decides - the recipient's own,
except an invitation, which goes in the inviter's choice.

FEB-36: a sign-in that names no workspace opens the one the person last chose
while it is still active and the membership still stands, else the oldest
active one, and never a suspended or closed one. Only a choice is recorded: a
switch, or a sign-in that named a workspace.

FEB-06 (EC7-02): the refusals FE-7's screens tell apart kept their status and
sentence and gained their own `code`. The Google link and unlink codes are
asserted in `test_google_endpoints.py`, beside the flows that reach them.

Real rows and real signed tokens; Redis is the in-memory fake the account
suites use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import hash_password
from app.db.models import Membership, MembershipStatus, Tenant, TenantRole, User
from app.db.models.email import OutboundEmail
from app.db.models.enums import TenantStatus
from app.services.email_service import (
    LOCALE_CONTEXT_KEY,
    EmailOutbox,
    locale_for_recipient,
    open_email_context,
)
from app.services.email_templates import EmailTemplate
from tests.conftest import AllowingEntitlements
from tests.fakes import TEST_CREDENTIAL_ENCRYPTION_KEY
from tests.integration.test_account_self_deletion import _Infra
from tests.workspace_timezone import TEST_TIMEZONE

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def fe7_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        default_plan_code="",
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.test",
        credential_encryption_keys=[TEST_CREDENTIAL_ENCRYPTION_KEY],
    )


@pytest.fixture
def app(fe7_settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    from app.main import create_app

    application = create_app(fe7_settings)
    application.state.database = _Infra()
    application.state.redis = _Infra()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://wasla.test") as c:
        yield c


async def _user(
    session: AsyncSession, *, email: str, verified: bool = True, locale: str | None = None
) -> User:
    user = User(
        email=email,
        full_name=email.split("@")[0].title(),
        hashed_password=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC) if verified else None,
        locale=locale,
    )
    session.add(user)
    await session.flush()
    return user


async def _workspace(
    session: AsyncSession,
    *,
    slug: str,
    member: User,
    role: TenantRole = TenantRole.TENANT_OWNER,
    created_at: datetime | None = None,
) -> Tenant:
    tenant = Tenant(
        timezone=TEST_TIMEZONE, name=slug.title(), slug=slug, status=TenantStatus.ACTIVE
    )
    session.add(tenant)
    await session.flush()
    membership = Membership(tenant_id=tenant.id, user_id=member.id, role=role)
    if created_at is not None:
        membership.created_at = created_at
    session.add(membership)
    await session.flush()
    return tenant


async def _login(http: AsyncClient, email: str, slug: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"email": email, "password": PASSWORD}
    if slug is not None:
        body["workspace_slug"] = slug
    response = await http.post(f"{API}/auth/login", json=body)
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _bearer(session: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


def _opened(session: dict[str, Any]) -> str | None:
    workspace = session["active_workspace"]
    return None if workspace is None else str(workspace["slug"])


# --- FEB-09: the account's language -------------------------------------------------


async def test_me_reads_and_patch_writes_only_the_language(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _user(db_session, email="lc-me@example.com")
    other = await _user(db_session, email="lc-other@example.com", locale="fr")
    session = await _login(http, "lc-me@example.com")

    me = await http.get(f"{API}/auth/me", headers=_bearer(session))
    assert me.status_code == 200 and me.json()["locale"] is None

    for code in ("ar", "tr", None):
        changed = await http.patch(
            f"{API}/auth/me", json={"locale": code}, headers=_bearer(session)
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["locale"] == code
        assert (await http.get(f"{API}/auth/me", headers=_bearer(session))).json()["locale"] == code

    await db_session.refresh(other)
    assert other.locale == "fr"


@pytest.mark.parametrize(
    "body",
    [{}, {"locale": "de"}, {"locale": "ar", "full_name": "X"}, {"locale": "ar", "user_id": "x"}],
)
async def test_patch_refuses_anything_but_a_supported_language(
    http: AsyncClient, db_session: AsyncSession, body: dict[str, Any]
) -> None:
    user = await _user(db_session, email=f"lc-refuse-{len(body)}@example.com")
    session = await _login(http, user.email)
    response = await http.patch(f"{API}/auth/me", json=body, headers=_bearer(session))
    assert response.status_code == 422
    await db_session.refresh(user)
    assert user.locale is None


async def test_an_unverified_account_may_choose_its_language(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _user(db_session, email="lc-unverified@example.com", verified=False)
    session = await _login(http, "lc-unverified@example.com")
    response = await http.patch(f"{API}/auth/me", json={"locale": "ar"}, headers=_bearer(session))
    assert response.status_code == 200 and response.json()["locale"] == "ar"


async def test_patch_needs_a_session(http: AsyncClient) -> None:
    assert (await http.patch(f"{API}/auth/me", json={"locale": "ar"})).status_code == 401


async def test_sign_up_stores_the_form_s_language_and_mails_the_code_in_it(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    for email, locale in (("lc-signup-ar@example.com", "ar"), ("lc-signup@example.com", None)):
        body: dict[str, Any] = {
            "email": email,
            "password": PASSWORD,
            "workspace_name": "Signup",
            "workspace_slug": email.split("@")[0],
            "timezone": TEST_TIMEZONE,
        }
        if locale is not None:
            body["locale"] = locale
        assert (await http.post(f"{API}/auth/register", json=body)).status_code == 202
        user = await db_session.scalar(select(User).where(User.email == email))
        assert user is not None and user.locale == locale
        row = await db_session.scalar(
            select(OutboundEmail).where(
                OutboundEmail.user_id == user.id,
                OutboundEmail.template == EmailTemplate.EMAIL_VERIFICATION.value,
            )
        )
        assert row is not None
        if locale == "ar":
            assert row.subject == "تأكيد عنوان بريدك الإلكتروني على Wasla"
        else:
            assert row.subject == "Verify your email address for Wasla"


async def test_an_invitation_goes_in_the_inviter_s_choice_not_the_invitee_s(
    http: AsyncClient, db_session: AsyncSession, fe7_settings: Settings
) -> None:
    owner = await _user(db_session, email="lc-owner@example.com", locale="ar")
    await _user(db_session, email="lc-invitee@example.com", locale="fr")
    await _workspace(db_session, slug="lc-invite", member=owner)
    session = await _login(http, owner.email)

    created = await http.post(
        f"{API}/invitations",
        json={"email": "lc-invitee@example.com", "role": "member", "locale": "tr"},
        headers=_bearer(session),
    )
    assert created.status_code == 201, created.text
    assert created.json()["locale"] == "tr"
    listed = await http.get(f"{API}/invitations", headers=_bearer(session))
    assert [entry["locale"] for entry in listed.json()] == ["tr"]

    row = await db_session.scalar(
        select(OutboundEmail).where(OutboundEmail.recipient == "lc-invitee@example.com")
    )
    assert row is not None
    assert row.subject == "Wasla'da bir çalışma alanına davet edildiniz"  # noqa: RUF001 - Turkish
    assert open_email_context(row, fe7_settings)[LOCALE_CONTEXT_KEY] == "tr"

    plain = await http.post(
        f"{API}/invitations",
        json={"email": "lc-nolang@example.com", "role": "member"},
        headers=_bearer(session),
    )
    assert plain.status_code == 201 and plain.json()["locale"] is None
    english = await db_session.scalar(
        select(OutboundEmail).where(OutboundEmail.recipient == "lc-nolang@example.com")
    )
    assert english is not None
    assert english.subject == "You have been invited to a workspace on Wasla"
    assert LOCALE_CONTEXT_KEY not in open_email_context(english, fe7_settings)


async def test_the_one_choice_function(db_session: AsyncSession) -> None:
    arabic = await _user(db_session, email="lc-choice@example.com", locale="ar")
    plain = await _user(db_session, email="lc-choice-none@example.com")
    choose = locale_for_recipient
    reset = EmailTemplate.PASSWORD_RESET
    invite = EmailTemplate.WORKSPACE_INVITATION
    assert await choose(db_session, template=reset, user_id=arabic.id, chosen="fr") == "ar"
    assert await choose(db_session, template=reset, user_id=plain.id, chosen=None) == "en"
    assert await choose(db_session, template=reset, user_id=None, chosen="tr") == "en"
    assert await choose(db_session, template=invite, user_id=arabic.id, chosen="tr") == "tr"
    assert await choose(db_session, template=invite, user_id=arabic.id, chosen=None) == "en"
    with pytest.raises(ValueError):
        await choose(db_session, template=invite, user_id=None, chosen="de")


async def test_an_english_row_is_exactly_what_it_was(
    db_session: AsyncSession, fe7_settings: Settings
) -> None:
    user = await _user(db_session, email="lc-row@example.com")
    row = await EmailOutbox(db_session, fe7_settings).enqueue(
        template=EmailTemplate.PASSWORD_CHANGED,
        recipient=user.email,
        idempotency_key="lc-row-en",
        user_id=user.id,
    )
    assert row is not None and row.context == {}
    assert row.subject == "Your Wasla password was changed"


# --- FEB-36: the last-used workspace ----------------------------------------------


async def _three(db_session: AsyncSession, prefix: str) -> tuple[User, list[Tenant]]:
    person = await _user(db_session, email=f"{prefix}@example.com")
    tenants = []
    for index, name in enumerate(("a", "b", "c")):
        tenants.append(
            await _workspace(
                db_session,
                slug=f"{prefix}-{name}",
                member=person,
                created_at=datetime(2026, 1, 1 + index, tzinfo=UTC),
            )
        )
    return person, tenants


async def _switch(http: AsyncClient, session: dict[str, Any], slug: str) -> int:
    response = await http.post(
        f"{API}/auth/workspace", json={"workspace_slug": slug}, headers=_bearer(session)
    )
    return response.status_code


async def test_a_sign_in_without_a_workspace_opens_the_last_chosen_one(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    person, (a, _b, c) = await _three(db_session, "lw-last")
    first = await _login(http, person.email)
    assert _opened(first) == a.slug  # nothing chosen yet: the oldest, as before
    assert await _switch(http, first, c.slug) == 200
    assert _opened(await _login(http, person.email)) == c.slug

    refreshed = await http.post(
        f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert refreshed.status_code == 200, refreshed.text
    assert _opened(refreshed.json()) == c.slug


async def test_a_suspended_closed_or_left_last_workspace_falls_back_to_the_oldest(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    person, (a, _b, c) = await _three(db_session, "lw-fall")
    assert await _switch(http, await _login(http, person.email), c.slug) == 200

    c.status = TenantStatus.SUSPENDED
    await db_session.flush()
    assert _opened(await _login(http, person.email)) == a.slug

    c.status = TenantStatus.ACTIVE
    c.deleted_at = datetime.now(UTC)
    await db_session.flush()
    assert _opened(await _login(http, person.email)) == a.slug

    c.deleted_at = None
    membership = await db_session.scalar(
        select(Membership).where(Membership.tenant_id == c.id, Membership.user_id == person.id)
    )
    assert membership is not None
    membership.status = MembershipStatus.REVOKED
    await db_session.flush()
    assert _opened(await _login(http, person.email)) == a.slug


async def test_every_workspace_suspended_still_opens_none(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    person, tenants = await _three(db_session, "lw-none")
    assert await _switch(http, await _login(http, person.email), tenants[2].slug) == 200
    for tenant in tenants:
        tenant.status = TenantStatus.SUSPENDED
    await db_session.flush()
    assert _opened(await _login(http, person.email)) is None


async def test_only_a_choice_moves_the_pointer(http: AsyncClient, db_session: AsyncSession) -> None:
    person, (a, b, c) = await _three(db_session, "lw-choice")
    session = await _login(http, person.email)
    await db_session.refresh(person)
    assert person.last_workspace_id is None  # a sign-in naming none chose nothing

    named = await _login(http, person.email, slug=b.slug)
    assert _opened(named) == b.slug
    await db_session.refresh(person)
    assert person.last_workspace_id == b.id

    c.status = TenantStatus.SUSPENDED
    await db_session.flush()
    assert await _switch(http, session, c.slug) == 403  # refused: nothing written
    await db_session.refresh(person)
    assert person.last_workspace_id == b.id

    stamp = person.updated_at
    assert await _switch(http, session, b.slug) == 200  # unchanged: nothing written
    await db_session.refresh(person)
    assert person.updated_at == stamp
    assert a.id != person.last_workspace_id


async def test_the_pointer_is_nobody_else_s_and_is_never_shown(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    one, (a1, _b1, c1) = await _three(db_session, "lw-one")
    two, (a2, _b2, _c2) = await _three(db_session, "lw-two")
    session = await _login(http, one.email)
    assert await _switch(http, session, c1.slug) == 200
    assert _opened(await _login(http, two.email)) == a2.slug
    me = await http.get(f"{API}/auth/me", headers=_bearer(session))
    assert "last_workspace_id" not in me.text and str(c1.id) not in me.text.replace(
        f'"id":"{c1.id}"', ""
    )
    assert a1.id != c1.id


# --- FEB-06: the codes FE-7's screens tell apart --------------------------------


def _refusal(response: Any, status: int, code: str, sentence: str) -> None:
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert error["message"] == sentence


async def test_an_address_with_a_pending_invitation_is_told_so(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session, email="ec7-pending@example.com")
    await _workspace(db_session, slug="ec7-pending", member=owner)
    session = await _login(http, owner.email)
    body = {"email": "ec7-invitee@example.com", "role": "member"}
    assert (
        await http.post(f"{API}/invitations", json=body, headers=_bearer(session))
    ).status_code == 201
    again = await http.post(f"{API}/invitations", json=body, headers=_bearer(session))
    _refusal(again, 409, "invitation_pending", "That address already has a pending invitation.")


async def test_inviting_or_reinstating_a_current_member_is_told_so(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session, email="ec7-member-owner@example.com")
    member = await _user(db_session, email="ec7-member@example.com")
    tenant = await _workspace(db_session, slug="ec7-member", member=owner)
    db_session.add(Membership(tenant_id=tenant.id, user_id=member.id, role=TenantRole.MEMBER))
    await db_session.flush()
    session = await _login(http, owner.email)
    sentence = "That person is already a member of this workspace."

    invited = await http.post(
        f"{API}/invitations",
        json={"email": member.email, "role": "member"},
        headers=_bearer(session),
    )
    _refusal(invited, 409, "already_member", sentence)
    reinstated = await http.post(
        f"{API}/workspace/members/{member.id}/reinstate",
        json={"role": "member"},
        headers=_bearer(session),
    )
    _refusal(reinstated, 409, "already_member", sentence)


async def test_revoking_an_invitation_twice_is_told_so(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session, email="ec7-revoke@example.com")
    await _workspace(db_session, slug="ec7-revoke", member=owner)
    session = await _login(http, owner.email)
    created = await http.post(
        f"{API}/invitations",
        json={"email": "ec7-revoked@example.com", "role": "member"},
        headers=_bearer(session),
    )
    invitation = created.json()["id"]
    first = await http.delete(f"{API}/invitations/{invitation}", headers=_bearer(session))
    assert first.status_code in (200, 204)
    second = await http.delete(f"{API}/invitations/{invitation}", headers=_bearer(session))
    _refusal(second, 409, "invitation_not_pending", "That invitation is no longer pending.")


async def test_a_taken_workspace_address_is_told_so(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session, email="ec7-slug@example.com")
    await _workspace(db_session, slug="ec7-slug-mine", member=owner)
    other = await _user(db_session, email="ec7-slug-other@example.com")
    await _workspace(db_session, slug="ec7-slug-taken", member=other)
    session = await _login(http, owner.email)
    response = await http.patch(
        f"{API}/workspace", json={"slug": "ec7-slug-taken"}, headers=_bearer(session)
    )
    _refusal(response, 409, "workspace_slug_taken", "That workspace address is already in use.")


async def test_a_wrong_current_password_is_not_a_lost_session(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _user(db_session, email="ec7-password@example.com")
    session = await _login(http, "ec7-password@example.com")
    sentence = "The current password is incorrect."

    changed = await http.post(
        f"{API}/auth/password",
        json={"current_password": "not the password", "new_password": "another long passphrase"},
        headers=_bearer(session),
    )
    _refusal(changed, 401, "current_password_invalid", sentence)
    closed = await http.request(
        "DELETE",
        f"{API}/auth/me",
        json={"current_password": "not the password"},
        headers=_bearer(session),
    )
    _refusal(closed, 401, "current_password_invalid", sentence)
    # The session is untouched by either refusal.
    assert (await http.get(f"{API}/auth/me", headers=_bearer(session))).status_code == 200


async def test_the_same_password_again_is_told_so(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    await _user(db_session, email="ec7-same@example.com")
    session = await _login(http, "ec7-same@example.com")
    response = await http.post(
        f"{API}/auth/password",
        json={"current_password": PASSWORD, "new_password": PASSWORD},
        headers=_bearer(session),
    )
    _refusal(
        response, 422, "password_unchanged", "The new password must differ from the current one."
    )
