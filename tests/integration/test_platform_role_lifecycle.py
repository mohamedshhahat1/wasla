"""The operator command's half of the last-live-owner invariant.

`PlatformRoleService` is the only way a platform role is ever written, and it
had the weaker half of AUTHZ-02: `owners()` selected on `platform_role` alone,
so a tombstone counted as a remaining owner and the guard could be satisfied by
an account that no longer existed.

`test_platform_role_command.py` covers the command as a command - parsing, exit
codes, the entry it writes. This file covers the *invariant*: which accounts
count as owners, and every state an account can be in when a role is granted to
it or taken from it. The lifecycle matrix is here rather than in the HTTP tests
because these transitions have no route - by design (ADR-094), the only way to
grant platform authority is a shell on the deployment.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.db.models.enums import PlatformRole
from app.db.models.user import User
from app.platform.owner_service import PlatformRoleService

pytestmark = pytest.mark.integration


async def _account(
    session: AsyncSession,
    *,
    role: PlatformRole | None = None,
    active: bool = True,
    deleted: bool = False,
    label: str = "account",
) -> User:
    user = User(
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="argon2-placeholder-never-verified-here",
        full_name=label.title(),
        platform_role=role,
        is_active=active,
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def service(db_session: AsyncSession) -> AsyncIterator[PlatformRoleService]:
    yield PlatformRoleService(db_session)


# Every lifecycle state an owner account can be in, and whether it counts as an
# owner the platform still has. The first row is the one AUTHZ-02 got wrong.
LIFECYCLE = [
    ("tombstoned", {"deleted": True, "active": False}, False),
    ("disabled", {"active": False}, False),
    ("live", {}, True),
]


@pytest.mark.parametrize(
    ("description", "state", "counts"),
    LIFECYCLE,
    ids=[row[0] for row in LIFECYCLE],
)
async def test_only_live_accounts_count_as_platform_owners(
    service: PlatformRoleService,
    db_session: AsyncSession,
    description: str,
    state: dict[str, bool],
    counts: bool,
) -> None:
    """One definition, asked about each state directly.

    Both halves matter. A tombstoned or disabled owner counting as "remaining"
    is the finding; a *live* owner not counting would be a guard that refuses
    every legitimate revocation, which the third case is here to rule out.
    """
    owner = await _account(
        db_session,
        role=PlatformRole.PLATFORM_OWNER,
        # Named rather than unpacked. `**state` is a `dict[str, bool]`, which
        # the checker cannot match against keyword-only parameters, and naming
        # them also puts the two axes the table varies in front of the reader.
        active=state.get("active", True),
        deleted=state.get("deleted", False),
    )

    found = [row.id for row in await service.owners()]

    assert (owner.id in found) is counts, description


async def test_the_roster_hides_departed_staff(
    service: PlatformRoleService, db_session: AsyncSession
) -> None:
    """`list` answers about today, so it carries the same lifecycle filter.

    Smaller stakes than `owners()` - nothing branches on it - but an operator
    asking who has authority over this platform and being shown accounts deleted
    last year has been given the wrong answer to the question they asked.
    """
    live = await _account(db_session, role=PlatformRole.PLATFORM_ADMIN, label="live")
    gone = await _account(
        db_session,
        role=PlatformRole.PLATFORM_ADMIN,
        deleted=True,
        active=False,
        label="gone",
    )

    listed = [row.id for row in await service.staff()]

    assert live.id in listed
    assert gone.id not in listed


async def test_the_last_live_owner_cannot_be_revoked_past_a_tombstone(
    service: PlatformRoleService, db_session: AsyncSession
) -> None:
    """The reproduction from the audit, at the service that answered wrongly.

    A tombstoned owner beside a live one used to make the guard say yes, and the
    installation ended up with nobody who could administer it.
    """
    await _account(
        db_session,
        role=PlatformRole.PLATFORM_OWNER,
        deleted=True,
        active=False,
        label="ghost",
    )
    survivor = await _account(db_session, role=PlatformRole.PLATFORM_OWNER, label="survivor")

    with pytest.raises(ValidationError, match="only remaining platform owner"):
        await service.revoke(survivor.email)

    assert survivor.platform_role is PlatformRole.PLATFORM_OWNER


async def test_an_owner_may_be_revoked_while_another_live_owner_remains(
    service: PlatformRoleService, db_session: AsyncSession
) -> None:
    """The control. A guard that refused every revocation would pass the rest."""
    leaving = await _account(db_session, role=PlatformRole.PLATFORM_OWNER, label="leaving")
    await _account(db_session, role=PlatformRole.PLATFORM_OWNER, label="staying")

    change = await service.revoke(leaving.email)

    assert change.previous is PlatformRole.PLATFORM_OWNER
    assert change.current is None
    assert leaving.platform_role is None


async def test_demoting_the_last_owner_to_admin_is_the_same_removal(
    service: PlatformRoleService, db_session: AsyncSession
) -> None:
    """`grant` can take ownership away, and the guard has to know it.

    `grant <the last owner> platform_admin` reads like an addition and is a
    removal: afterwards the platform has an administrator and no owner. The
    invariant follows the shape of the act rather than the subcommand it
    arrived under.
    """
    owner = await _account(db_session, role=PlatformRole.PLATFORM_OWNER, label="only")

    with pytest.raises(ValidationError, match="only remaining platform owner"):
        await service.grant(owner.email, PlatformRole.PLATFORM_ADMIN)

    assert owner.platform_role is PlatformRole.PLATFORM_OWNER


async def test_an_owner_may_be_demoted_while_another_remains(
    service: PlatformRoleService, db_session: AsyncSession
) -> None:
    """The paired control for the demotion guard."""
    owner = await _account(db_session, role=PlatformRole.PLATFORM_OWNER, label="stepping-down")
    await _account(db_session, role=PlatformRole.PLATFORM_OWNER, label="taking-over")

    change = await service.grant(owner.email, PlatformRole.PLATFORM_ADMIN)

    assert change.previous is PlatformRole.PLATFORM_OWNER
    assert change.current is PlatformRole.PLATFORM_ADMIN


async def test_a_tombstoned_account_cannot_be_granted_a_platform_role(
    service: PlatformRoleService, db_session: AsyncSession
) -> None:
    """A deleted identity is documented as never reusable, and this is part of it.

    Granting would write authority onto a row that can never authenticate again
    - an entry in the trail saying somebody was made a platform owner, with no
    owner at the end of it. The deletion path clears `platform_role` for the
    same reason; this stops it being put back.
    """
    gone = await _account(db_session, deleted=True, active=False, label="gone")

    with pytest.raises(ValidationError, match="permanently deleted"):
        await service.grant(gone.email, PlatformRole.PLATFORM_OWNER)

    assert gone.platform_role is None


async def test_a_disabled_account_cannot_be_granted_a_platform_role(
    service: PlatformRoleService, db_session: AsyncSession
) -> None:
    """Suspended is a sequencing mistake, so the message says which order.

    Granting silently would leave authority that takes effect only if somebody
    later re-enables the account for a reason unconnected to this decision.
    """
    suspended = await _account(db_session, active=False, label="suspended")

    with pytest.raises(ValidationError, match="Re-enable the account"):
        await service.grant(suspended.email, PlatformRole.PLATFORM_ADMIN)

    assert suspended.platform_role is None


async def test_granting_a_live_account_still_works(
    service: PlatformRoleService, db_session: AsyncSession
) -> None:
    """The control for both refusals above."""
    person = await _account(db_session, label="ordinary")

    change = await service.grant(person.email, PlatformRole.PLATFORM_OWNER)

    assert change.current is PlatformRole.PLATFORM_OWNER
    assert person.platform_role is PlatformRole.PLATFORM_OWNER
