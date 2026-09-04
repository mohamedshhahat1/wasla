"""Creating the first platform administrator, without SQL somebody typed.

`users.platform_role` is nullable, defaults to null, and had no code path
anywhere that wrote it - not a route, not a service, not a migration, not a
script. The seven `/platform/*` routes were therefore unreachable in any
deployment until an operator ran an `UPDATE` by hand, which no document
described, which validated nothing, and which left no record of who took
authority over every workspace on the platform (ADR-094).

The command is the supported step. These drive it end to end against a real
database, including the two refusals and the entry it writes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.db.models.audit import AuditAction, AuditActorKind, AuditLog
from app.db.models.enums import PlatformRole
from app.db.models.user import User
from app.main import create_app
from app.platform.owner_service import PlatformRoleService
from app.platform.roles import build_parser

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def account(db_session: AsyncSession) -> AsyncIterator[User]:
    """An ordinary user, exactly as registration leaves one: no platform role."""
    user = User(
        email=f"ops-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="argon2-placeholder-never-verified-here",
        full_name="Operations",
    )
    db_session.add(user)
    await db_session.flush()
    yield user


async def _entries(session: AsyncSession, user_id: uuid.UUID) -> list[AuditLog]:
    rows = await session.execute(
        select(AuditLog).where(AuditLog.target_id == user_id).order_by(AuditLog.occurred_at)
    )
    return list(rows.scalars().all())


async def test_an_operator_can_grant_the_first_platform_owner(
    db_session: AsyncSession, account: User
) -> None:
    """The finding itself: there is now a way, and it does not need SQL."""
    service = PlatformRoleService(db_session)
    assert await service.owners() == []

    change = await service.grant(account.email, PlatformRole.PLATFORM_OWNER)
    await db_session.flush()

    assert change.previous is None
    assert change.current is PlatformRole.PLATFORM_OWNER
    assert change.changed is True
    assert account.platform_role is PlatformRole.PLATFORM_OWNER
    assert [row.id for row in await service.owners()] == [account.id]


async def test_a_role_can_be_granted_by_exact_user_id(
    db_session: AsyncSession, account: User
) -> None:
    """Both forms, because an operator has one or the other to hand."""
    await PlatformRoleService(db_session).grant(str(account.id), PlatformRole.PLATFORM_ADMIN)
    await db_session.flush()
    assert account.platform_role is PlatformRole.PLATFORM_ADMIN


async def test_the_grant_is_audited(db_session: AsyncSession, account: User) -> None:
    """Recorded because nobody authorised it through the application.

    The actor is an operator at a shell, so there is no authenticated user to
    name - `SYSTEM` says that rather than pretending the target granted it to
    themselves. What the entry does name is the account, the role and the role
    it replaced.
    """
    await PlatformRoleService(db_session).grant(account.email, PlatformRole.PLATFORM_OWNER)
    await db_session.flush()

    (entry,) = await _entries(db_session, account.id)
    assert entry.action is AuditAction.PLATFORM_ROLE_GRANTED
    assert entry.actor_kind is AuditActorKind.SYSTEM
    assert entry.tenant_id is None, "a platform act belongs to no workspace"
    assert entry.target_label == account.email
    assert entry.meta == {
        "role": "platform_owner",
        "previous_role": None,
        "granted_by": "operator_command",
    }


async def test_granting_a_role_somebody_already_holds_writes_nothing(
    db_session: AsyncSession, account: User
) -> None:
    """Idempotent, so re-running after a failed deploy does not fill the trail."""
    service = PlatformRoleService(db_session)
    await service.grant(account.email, PlatformRole.PLATFORM_OWNER)
    await db_session.flush()
    again = await service.grant(account.email, PlatformRole.PLATFORM_OWNER)
    await db_session.flush()

    assert again.changed is False
    assert len(await _entries(db_session, account.id)) == 1


async def test_an_unknown_account_is_rejected(db_session: AsyncSession) -> None:
    """No account is created here. A role attaches to somebody who exists."""
    service = PlatformRoleService(db_session)
    with pytest.raises(ValidationError):
        await service.grant("nobody@example.com", PlatformRole.PLATFORM_OWNER)
    with pytest.raises(ValidationError):
        await service.grant(str(uuid.uuid4()), PlatformRole.PLATFORM_OWNER)


async def test_a_partial_address_matches_nothing(db_session: AsyncSession, account: User) -> None:
    """Exact matching, because a fuzzy one grants the platform to the wrong person."""
    with pytest.raises(ValidationError):
        await PlatformRoleService(db_session).grant(account.email[:6], PlatformRole.PLATFORM_OWNER)
    assert account.platform_role is None


def test_an_invalid_role_is_rejected_before_anything_runs() -> None:
    """argparse holds the vocabulary, so a typo never reaches the database."""
    parser = build_parser()
    parser.parse_args(["grant", "ops@example.com", "platform_owner"])
    with pytest.raises(SystemExit):
        parser.parse_args(["grant", "ops@example.com", "superuser"])


async def test_the_last_platform_owner_cannot_be_revoked(
    db_session: AsyncSession, account: User
) -> None:
    """An installation with no owner has no supported way back but this command."""
    service = PlatformRoleService(db_session)
    await service.grant(account.email, PlatformRole.PLATFORM_OWNER)
    await db_session.flush()

    with pytest.raises(ValidationError):
        await service.revoke(account.email)
    assert account.platform_role is PlatformRole.PLATFORM_OWNER


async def test_an_owner_can_be_revoked_once_somebody_else_holds_it(
    db_session: AsyncSession, account: User
) -> None:
    """The companion, so the refusal above is not simply "never revoke"."""
    successor = User(
        email=f"successor-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="argon2-placeholder-never-verified-here",
    )
    db_session.add(successor)
    await db_session.flush()

    service = PlatformRoleService(db_session)
    await service.grant(account.email, PlatformRole.PLATFORM_OWNER)
    await service.grant(successor.email, PlatformRole.PLATFORM_OWNER)
    await db_session.flush()

    change = await service.revoke(account.email)
    await db_session.flush()

    assert change.current is None
    assert account.platform_role is None
    _, revocation = await _entries(db_session, account.id)
    assert revocation.action is AuditAction.PLATFORM_ROLE_REVOKED
    assert revocation.meta == {
        "previous_role": "platform_owner",
        "revoked_by": "operator_command",
    }

    await db_session.execute(delete(User).where(User.id == successor.id))


def test_the_command_is_not_exposed_as_an_http_route() -> None:
    """The absence is the design, and it is asserted rather than assumed.

    A route that granted a platform role would need a caller who already held
    one, which answers the bootstrap question with itself - and would put the
    platform's own escalation path on the internet for an operation performed a
    handful of times in a deployment's life.
    """
    paths = {getattr(route, "path", "") for route in create_app().routes}
    forbidden = [
        path
        for path in paths
        if "platform-role" in path or "platform_role" in path or path.endswith("/bootstrap")
    ]
    assert forbidden == []
    assert not any(path.startswith("/api/v1/platform/users/") and "role" in path for path in paths)


async def test_a_role_change_needs_no_token_invalidation(
    db_session: AsyncSession, account: User
) -> None:
    """Because authorization reads the row, not the token (ADR-036's shape).

    `require_platform_roles` reads `user.platform_role` from the account row
    that authentication has already loaded, exactly as membership is re-read on
    every request. So a grant takes effect on the next request and a revocation
    takes effect immediately, and neither needs `token_version` moved - which
    would sign the person out of every workspace to change something about the
    platform.
    """
    before = account.token_version
    await PlatformRoleService(db_session).grant(account.email, PlatformRole.PLATFORM_OWNER)
    await db_session.flush()
    assert account.token_version == before
