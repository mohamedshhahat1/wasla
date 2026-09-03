"""Which domain events queue email, to whom, and in whose transaction.

The transactional-outbox guarantee is the point of most of these: the row that
says an email should exist is written on the caller's own session, so it
commits with the domain action or rolls back with it. An invitation that was
never issued must not have been announced.

The other half is the recipient. Every address here comes from a row this
application wrote - a user's `email`, an invitation's `email`, a workspace's
owners - and never from the request that triggered the notice.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import hash_password
from app.db.models import (
    Membership,
    MembershipStatus,
    Tenant,
    TenantRole,
    TenantStatus,
    User,
)
from app.db.models.billing import (
    BillingInterval,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.db.models.email import OutboundEmail
from app.services.account_service import AccountService
from app.services.email_templates import EmailTemplate
from app.services.invitation_service import InvitationService
from app.services.subscription_service import SubscriptionService
from tests.fakes import as_database

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="CRITICAL",
        cors_origins=[],
        rate_limit_enabled=False,
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.com",
    )


def _disabled_settings() -> Settings:
    settings = _settings()
    return settings.model_copy(update={"email_enabled": False})


async def _queued(
    session: AsyncSession, template: EmailTemplate | None = None
) -> list[OutboundEmail]:
    statement = select(OutboundEmail)
    if template is not None:
        statement = statement.where(OutboundEmail.template == template.value)
    return list((await session.execute(statement)).scalars())


async def _user(session: AsyncSession, email: str, *, active: bool = True) -> User:
    row = User(email=email, hashed_password=hash_password(PASSWORD), is_active=active)
    session.add(row)
    await session.flush()
    return row


async def _workspace(session: AsyncSession, *, slug: str, owner: User) -> Tenant:
    tenant = Tenant(name=slug.title(), slug=slug, status=TenantStatus.ACTIVE)
    session.add(tenant)
    await session.flush()
    session.add(
        Membership(
            tenant_id=tenant.id,
            user_id=owner.id,
            role=TenantRole.TENANT_OWNER,
            status=MembershipStatus.ACTIVE,
        )
    )
    await session.flush()
    return tenant


# --- Invitations ---------------------------------------------------------


async def test_issuing_an_invitation_queues_its_email(db_session: AsyncSession) -> None:
    inviter = await _user(db_session, "owner@example.com")
    tenant = await _workspace(db_session, slug="acme-invite", owner=inviter)

    await InvitationService(session=db_session, settings=_settings()).issue(
        tenant_id=tenant.id,
        inviter=inviter,
        inviter_role=TenantRole.TENANT_OWNER,
        email="invited@example.com",
        role=TenantRole.MEMBER,
    )

    queued = await _queued(db_session, EmailTemplate.WORKSPACE_INVITATION)
    assert len(queued) == 1
    assert queued[0].recipient == "invited@example.com"
    assert queued[0].tenant_id == tenant.id


async def test_the_invitation_email_carries_the_token_and_the_workspace_name(
    db_session: AsyncSession,
) -> None:
    inviter = await _user(db_session, "owner2@example.com")
    tenant = await _workspace(db_session, slug="acme-token", owner=inviter)

    _, raw_token = await InvitationService(session=db_session, settings=_settings()).issue(
        tenant_id=tenant.id,
        inviter=inviter,
        inviter_role=TenantRole.TENANT_OWNER,
        email="invited2@example.com",
        role=TenantRole.MEMBER,
    )

    queued = await _queued(db_session, EmailTemplate.WORKSPACE_INVITATION)
    assert queued[0].context["token"] == raw_token
    # Read from the tenant row, never from the request.
    assert queued[0].context["workspace_name"] == tenant.name


async def test_the_invitation_email_is_keyed_to_its_invitation_row(
    db_session: AsyncSession,
) -> None:
    inviter = await _user(db_session, "owner3@example.com")
    tenant = await _workspace(db_session, slug="acme-key", owner=inviter)

    invitation, _ = await InvitationService(session=db_session, settings=_settings()).issue(
        tenant_id=tenant.id,
        inviter=inviter,
        inviter_role=TenantRole.TENANT_OWNER,
        email="invited3@example.com",
        role=TenantRole.MEMBER,
    )

    queued = await _queued(db_session, EmailTemplate.WORKSPACE_INVITATION)
    assert queued[0].idempotency_key == f"invitation:{invitation.id}"


async def test_no_invitation_email_is_queued_when_email_is_disabled(
    db_session: AsyncSession,
) -> None:
    """A disabled deployment is a no-op, not a row that waits forever."""
    inviter = await _user(db_session, "owner4@example.com")
    tenant = await _workspace(db_session, slug="acme-off", owner=inviter)

    await InvitationService(session=db_session, settings=_disabled_settings()).issue(
        tenant_id=tenant.id,
        inviter=inviter,
        inviter_role=TenantRole.TENANT_OWNER,
        email="invited4@example.com",
        role=TenantRole.MEMBER,
    )

    assert await _queued(db_session) == []


async def test_a_rolled_back_invitation_leaves_no_email(db_session: AsyncSession) -> None:
    """The whole point of the outbox: the two share a fate."""
    inviter = await _user(db_session, "owner5@example.com")
    tenant = await _workspace(db_session, slug="acme-rollback", owner=inviter)
    savepoint = await db_session.begin_nested()

    await InvitationService(session=db_session, settings=_settings()).issue(
        tenant_id=tenant.id,
        inviter=inviter,
        inviter_role=TenantRole.TENANT_OWNER,
        email="invited5@example.com",
        role=TenantRole.MEMBER,
    )
    await savepoint.rollback()

    assert await _queued(db_session, EmailTemplate.WORKSPACE_INVITATION) == []


# --- Security notices ----------------------------------------------------


async def test_changing_a_password_notifies_the_account(db_session: AsyncSession) -> None:
    person = await _user(db_session, "person@example.com")

    await AccountService(db_session, settings=_settings()).change_password(
        user=person,
        current_password=PASSWORD,
        new_password="a-brand-new-passphrase",
    )

    queued = await _queued(db_session, EmailTemplate.PASSWORD_CHANGED)
    assert len(queued) == 1
    assert queued[0].recipient == "person@example.com"
    assert queued[0].user_id == person.id


async def test_revoking_sessions_notifies_the_account(db_session: AsyncSession) -> None:
    person = await _user(db_session, "revoked@example.com")

    await AccountService(db_session, settings=_settings()).revoke_sessions(user=person)

    queued = await _queued(db_session, EmailTemplate.SESSIONS_REVOKED)
    assert len(queued) == 1
    assert queued[0].recipient == "revoked@example.com"


async def test_disabling_an_account_notifies_it(db_session: AsyncSession) -> None:
    person = await _user(db_session, "suspended@example.com")
    staff = await _user(db_session, "staff@example.com")

    await AccountService(db_session, settings=_settings()).disable(
        user_id=person.id,
        actor=staff,
    )

    queued = await _queued(db_session, EmailTemplate.ACCOUNT_DISABLED)
    assert len(queued) == 1
    # The account's own address, not the administrator's.
    assert queued[0].recipient == "suspended@example.com"


async def test_enabling_an_account_notifies_it(db_session: AsyncSession) -> None:
    person = await _user(db_session, "restored@example.com", active=False)
    staff = await _user(db_session, "staff2@example.com")

    await AccountService(db_session, settings=_settings()).enable(
        user_id=person.id,
        actor=staff,
    )

    queued = await _queued(db_session, EmailTemplate.ACCOUNT_ENABLED)
    assert len(queued) == 1
    assert queued[0].recipient == "restored@example.com"


async def test_a_security_notice_is_keyed_to_the_version_it_announces(
    db_session: AsyncSession,
) -> None:
    """One notice per act, however many times the request is retried."""
    person = await _user(db_session, "keyed@example.com")
    service = AccountService(db_session, settings=_settings())

    await service.revoke_sessions(user=person)
    version = person.token_version
    queued = await _queued(db_session, EmailTemplate.SESSIONS_REVOKED)

    assert queued[0].idempotency_key == f"security-sessions_revoked:{person.id}:{version}"


async def test_two_acts_on_one_account_queue_two_notices(db_session: AsyncSession) -> None:
    person = await _user(db_session, "twice@example.com")
    service = AccountService(db_session, settings=_settings())

    await service.revoke_sessions(user=person)
    await service.revoke_sessions(user=person)

    assert len(await _queued(db_session, EmailTemplate.SESSIONS_REVOKED)) == 2


async def test_a_rolled_back_revocation_leaves_no_notice(db_session: AsyncSession) -> None:
    """An email announcing something that did not happen cannot be taken back."""
    person = await _user(db_session, "rollback@example.com")
    savepoint = await db_session.begin_nested()

    await AccountService(db_session, settings=_settings()).revoke_sessions(user=person)
    await savepoint.rollback()

    assert await _queued(db_session, EmailTemplate.SESSIONS_REVOKED) == []


# --- Billing -------------------------------------------------------------


async def _subscription(
    db_session: AsyncSession, *, owner_email: str, slug: str
) -> tuple[Any, ...]:
    owner = await _user(db_session, owner_email)
    tenant = await _workspace(db_session, slug=slug, owner=owner)
    plan = Plan(
        code=f"plan-{slug}",
        name="Pro",
        price=Decimal("49.00"),
        currency="USD",
        interval=BillingInterval.MONTHLY,
    )
    db_session.add(plan)
    await db_session.flush()
    now = datetime.now(UTC)
    subscription = Subscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        status=SubscriptionStatus.ACTIVE,
        current_period_start=now - timedelta(days=30),
        current_period_end=now,
    )
    db_session.add(subscription)
    await db_session.flush()
    return tenant, owner, plan, subscription


async def test_cancelling_a_subscription_notifies_the_owners(db_session: AsyncSession) -> None:
    tenant, owner, _, _ = await _subscription(
        db_session, owner_email="billing@example.com", slug="acme-cancel"
    )

    await SubscriptionService(db_session, tenant_id=tenant.id, settings=_settings()).cancel(
        actor=owner
    )

    queued = await _queued(db_session, EmailTemplate.SUBSCRIPTION_CANCELLED)
    assert len(queued) == 1
    assert queued[0].recipient == "billing@example.com"
    assert queued[0].tenant_id == tenant.id
    assert queued[0].context["workspace_name"] == tenant.name


async def test_a_cancellation_notice_names_the_workspace_from_its_row(
    db_session: AsyncSession,
) -> None:
    """The only variable in a billing template, and it is never caller-supplied."""
    tenant, owner, _, _ = await _subscription(
        db_session, owner_email="billing2@example.com", slug="acme-name"
    )

    await SubscriptionService(db_session, tenant_id=tenant.id, settings=_settings()).cancel(
        actor=owner
    )

    queued = await _queued(db_session, EmailTemplate.SUBSCRIPTION_CANCELLED)
    assert queued[0].context["workspace_name"] == "Acme-Name".title()


async def test_only_active_owners_are_notified(db_session: AsyncSession) -> None:
    """Billing notices go to the people who can act on them."""
    tenant, owner, _, _ = await _subscription(
        db_session, owner_email="owner-a@example.com", slug="acme-owners"
    )
    member = await _user(db_session, "member@example.com")
    revoked_owner = await _user(db_session, "gone@example.com")
    db_session.add_all(
        [
            Membership(
                tenant_id=tenant.id,
                user_id=member.id,
                role=TenantRole.MEMBER,
                status=MembershipStatus.ACTIVE,
            ),
            Membership(
                tenant_id=tenant.id,
                user_id=revoked_owner.id,
                role=TenantRole.TENANT_OWNER,
                status=MembershipStatus.REVOKED,
            ),
        ]
    )
    await db_session.flush()

    await SubscriptionService(db_session, tenant_id=tenant.id, settings=_settings()).cancel(
        actor=owner
    )

    queued = await _queued(db_session, EmailTemplate.SUBSCRIPTION_CANCELLED)
    assert {row.recipient for row in queued} == {"owner-a@example.com"}


async def test_another_workspaces_owners_are_never_notified(db_session: AsyncSession) -> None:
    """Tenant isolation, on the one path that fans out to several people."""
    tenant, owner, _, _ = await _subscription(
        db_session, owner_email="ours@example.com", slug="acme-ours"
    )
    stranger = await _user(db_session, "theirs@example.com")
    await _workspace(db_session, slug="other-co", owner=stranger)

    await SubscriptionService(db_session, tenant_id=tenant.id, settings=_settings()).cancel(
        actor=owner
    )

    queued = await _queued(db_session, EmailTemplate.SUBSCRIPTION_CANCELLED)
    assert {row.recipient for row in queued} == {"ours@example.com"}
    assert all(row.tenant_id == tenant.id for row in queued)


# --- Secret containment --------------------------------------------------


async def test_no_reset_token_survives_a_completed_send(db_session: AsyncSession) -> None:
    """The context carries the link, and the send is where its life ends."""
    from app.integrations.email.fake import FakeEmailProvider
    from app.repositories.email_repository import EmailOutboxRepository
    from app.workers.email_worker import EmailWorker

    person = await _user(db_session, "contained@example.com")
    settings = _settings()
    await EmailOutboxRepository(db_session).enqueue(
        recipient=person.email,
        template=EmailTemplate.PASSWORD_RESET.value,
        subject="Reset your Wasla password",
        context={"token": "a-very-secret-token"},
        idempotency_key="reset-containment",
        available_at=datetime.now(UTC),
        user_id=person.id,
    )

    class _Handle:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        @asynccontextmanager
        async def session(self) -> AsyncIterator[AsyncSession]:
            yield self._session

    worker = EmailWorker(
        database=as_database(_Handle(db_session)),
        settings=settings,
        provider=FakeEmailProvider(),
    )
    await worker.run_once()

    rows = await _queued(db_session, EmailTemplate.PASSWORD_RESET)
    assert rows[0].context == {}
    # The row keeps an identifier for the message, never its contents.
    assert "a-very-secret-token" not in str(rows[0].context)
    assert rows[0].provider_message_id is not None
