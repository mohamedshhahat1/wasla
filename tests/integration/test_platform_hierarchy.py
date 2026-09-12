"""The platform staff hierarchy, over real HTTP, target by target.

`platform_owner` and `platform_admin` used to be the same authority wearing
different names. All eleven `/platform/*` routes sat behind `PlatformStaffDep`;
`PlatformOwnerDep` was defined and guarded nothing; and `AccountService.delete`
asked only whether the target was the *caller*, never what the target was. So a
platform admin could disable and then permanently tombstone every platform owner
on the installation, and keep reading `/platform/tenants` afterwards (AUTHZ-01).

Nothing customer-facing was reachable through it - a platform role grants
nothing inside a workspace, which is a separate boundary and a sound one. What
was lost was the platform's own way back in, and a tombstone is documented as
never reusable.

This file is the matrix rather than a few examples, because the finding was
precisely that one cell of the matrix had never been asked about. Every actor
role is run against every target role for every account-lifecycle verb, and each
refusal is paired with a control proving the same call succeeds against a target
it is allowed to act on - otherwise a route that refuses everybody would pass.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute, _IncludedRouter
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import hash_password
from app.db.models import PlatformRole, User
from app.db.models.audit import AuditAction, AuditLog
from app.main import create_app
from app.platform.hierarchy import live_platform_role
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"


class _Infra:
    """The application's infrastructure handles, unused by these routes."""

    def __init__(self) -> None:
        self.client = self

    async def ping(self) -> bool:  # pragma: no cover - never reached here
        return True


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
    )


@pytest.fixture
def app(settings: Settings, db_session: AsyncSession) -> Iterator[FastAPI]:
    application = create_app(settings)
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
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as client:
        yield client


async def _user(
    session: AsyncSession,
    *,
    role: PlatformRole | None = None,
    label: str = "person",
) -> User:
    """One verified account, optionally holding a platform role.

    No workspace and no membership: everything here is platform authority, and
    giving these accounts a workspace would quietly make it possible for a test
    to pass on the wrong grounds.
    """
    user = User(
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        full_name=label.title(),
        hashed_password=hash_password(PASSWORD),
        is_active=True,
        email_verified_at=datetime.now(UTC),
        platform_role=role,
    )
    session.add(user)
    await session.flush()
    return user


async def _bearer(http: AsyncClient, user: User) -> dict[str, str]:
    response = await http.post(
        f"{API}/auth/login",
        json={"email": user.email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return {"Authorization": f"Bearer {payload['access_token']}"}


async def _reread(session: AsyncSession, user_id: uuid.UUID) -> User:
    """The row as the database holds it, not as the session remembers it.

    The refresh is the point. `AccountService.delete` writes through an `UPDATE
    ... RETURNING` with `synchronize_session=False`, so the identity map still
    holds the pre-deletion values - and a test that read those would report a
    tombstone as untouched, or an untouched row as tombstoned, depending on
    which way the bug went.
    """
    row = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
    await session.refresh(row)
    return row


# Every (actor role, target role) pair, with what `disable` and `delete` must
# answer. Written out rather than computed: a table a reader can check against
# the policy is the deliverable, and a rule that generates the table would also
# generate the bug if the rule were wrong.
HIERARCHY = [
    ("admin acting on a plain account", PlatformRole.PLATFORM_ADMIN, None, 200),
    (
        "admin acting on another admin",
        PlatformRole.PLATFORM_ADMIN,
        PlatformRole.PLATFORM_ADMIN,
        200,
    ),
    (
        "admin acting on an owner",
        PlatformRole.PLATFORM_ADMIN,
        PlatformRole.PLATFORM_OWNER,
        403,
    ),
    ("owner acting on a plain account", PlatformRole.PLATFORM_OWNER, None, 200),
    (
        "owner acting on an admin",
        PlatformRole.PLATFORM_OWNER,
        PlatformRole.PLATFORM_ADMIN,
        200,
    ),
    (
        "owner acting on another owner",
        PlatformRole.PLATFORM_OWNER,
        PlatformRole.PLATFORM_OWNER,
        200,
    ),
]


@pytest.mark.parametrize(
    ("description", "actor_role", "target_role", "expected"),
    HIERARCHY,
    ids=[row[0] for row in HIERARCHY],
)
@pytest.mark.parametrize("verb", ["disable", "delete"])
async def test_the_platform_hierarchy_decides_every_destructive_call(
    http: AsyncClient,
    db_session: AsyncSession,
    description: str,
    actor_role: PlatformRole,
    target_role: PlatformRole | None,
    expected: int,
    verb: str,
) -> None:
    """The whole matrix, over HTTP, with the database checked after each call.

    A status code alone would not settle it. The finding was a 200 that
    tombstoned an owner, so the assertion that matters is what the row looks
    like afterwards: refused means still active and still not deleted, and
    allowed means the lifecycle fields actually moved.
    """
    actor = await _user(db_session, role=actor_role, label="actor")
    target = await _user(db_session, role=target_role, label="target")
    if target_role is PlatformRole.PLATFORM_OWNER:
        # A spare owner, so the last-live-owner guard cannot be what refuses
        # and hand this test a right answer for the wrong reason.
        await _user(db_session, role=PlatformRole.PLATFORM_OWNER, label="spare")
    headers = await _bearer(http, actor)

    if verb == "disable":
        response = await http.post(f"{API}/platform/users/{target.id}/disable", headers=headers)
    else:
        response = await http.request(
            "DELETE", f"{API}/platform/users/{target.id}", headers=headers
        )

    assert response.status_code == expected, f"{description}: {response.text}"
    row = await _reread(db_session, target.id)
    if expected == 403:
        assert response.json()["error"]["code"] == "permission_denied"
        assert row.is_active is True
        assert row.deleted_at is None
        assert row.platform_role is PlatformRole.PLATFORM_OWNER
    elif verb == "disable":
        assert row.is_active is False
        assert row.deleted_at is None
    else:
        assert row.deleted_at is not None
        assert row.is_active is False


async def test_an_admin_may_re_enable_a_suspended_owner(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """`enable` keeps plain staff authority, and that is the decision.

    It is the only account route that *restores* authority, so it cannot be the
    route by which an installation loses its owners - and it is the way back if
    the owners are ever suspended with nobody senior awake to undo it.
    """
    admin = await _user(db_session, role=PlatformRole.PLATFORM_ADMIN, label="admin")
    owner = await _user(db_session, role=PlatformRole.PLATFORM_OWNER, label="owner")
    owner.is_active = False
    await db_session.flush()

    response = await http.post(
        f"{API}/platform/users/{owner.id}/enable",
        headers=await _bearer(http, admin),
    )

    assert response.status_code == 200, response.text
    assert (await _reread(db_session, owner.id)).is_active is True


async def test_an_admin_keeps_every_read_it_had(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """The control for the whole file: the hierarchy narrowed two routes, not nine.

    Fixing AUTHZ-01 by putting every platform route behind `PlatformOwnerDep`
    would have passed each refusal test above and broken the product. This is
    what says the admin can still do the job.
    """
    admin = await _user(db_session, role=PlatformRole.PLATFORM_ADMIN, label="admin")
    headers = await _bearer(http, admin)

    overview = await http.get(f"{API}/platform/overview", headers=headers)
    tenants = await http.get(f"{API}/platform/tenants", headers=headers)
    audit = await http.get(f"{API}/platform/audit-logs", headers=headers)

    assert overview.status_code == 200, overview.text
    assert tenants.status_code == 200, tenants.text
    assert audit.status_code == 200, audit.text


@pytest.mark.parametrize("role", [PlatformRole.PLATFORM_OWNER, PlatformRole.PLATFORM_ADMIN])
@pytest.mark.parametrize("verb", ["disable", "delete"])
async def test_platform_staff_cannot_act_on_their_own_account(
    http: AsyncClient,
    db_session: AsyncSession,
    role: PlatformRole,
    verb: str,
) -> None:
    """AUTHZ-05: the protection was correct and nothing proved it.

    Mutation M13 removed the `user_id == actor.id` guard and every targeted
    suite still passed; the mutated build was then driven and a platform owner
    deleted its own account through the admin API. The runtime was right and the
    tests were silent, which is the same as having no guard the next time
    somebody refactors this method.

    Run for both roles because the two reach the code by different route
    dependencies now, and asserted on the row rather than only the status: the
    account must still be usable afterwards, and the token version untouched, or
    a "refusal" that had already revoked every session would pass.
    """
    actor = await _user(db_session, role=role, label="self")
    if role is PlatformRole.PLATFORM_OWNER:
        await _user(db_session, role=PlatformRole.PLATFORM_OWNER, label="spare")
    before = actor.token_version
    headers = await _bearer(http, actor)

    if verb == "disable":
        response = await http.post(f"{API}/platform/users/{actor.id}/disable", headers=headers)
    else:
        response = await http.request("DELETE", f"{API}/platform/users/{actor.id}", headers=headers)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"
    row = await _reread(db_session, actor.id)
    assert row.is_active is True
    assert row.deleted_at is None
    assert row.token_version == before

    # And the session it made the attempt with still works, which is the
    # practical meaning of "the account was not touched".
    assert (await http.get(f"{API}/auth/me", headers=headers)).status_code == 200


async def test_a_tombstoned_owner_does_not_keep_another_owner_deletable(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """AUTHZ-02's counting question, asked where it can actually be answered.

    **Which guard refuses is the whole point, so read this before extending it.**
    Over HTTP, the last-live-owner guard on `delete` is unreachable
    sequentially, and that is a consequence of the hierarchy rather than an
    oversight. To act on an owner you must be an owner; if the target is the
    *last* live owner, then you are the target - and the self-guard refuses
    first, with a different message. A test that deleted the last owner as
    itself and asserted `422` would pass with the last-owner guard deleted
    entirely.

    So this asks the half HTTP can answer: a tombstoned owner must not be
    counted as remaining. Three owners, one of them a ghost of exactly the kind
    that used to satisfy the old guard; `deleter` removes `live`, which is
    allowed because `deleter` itself is live; and the ghost is shown to count
    for nothing by removing the account whose deletion it would have authorised.

    The sequential last-owner refusal belongs to the operator commands, which
    can be run by somebody who is not the target - see
    `test_platform_role_lifecycle.py`. The concurrent one is in
    `test_platform_owner_concurrency.py`, where a second actor genuinely exists
    at the moment of the check.
    """
    live = await _user(db_session, role=PlatformRole.PLATFORM_OWNER, label="live")
    ghost = await _user(db_session, role=PlatformRole.PLATFORM_OWNER, label="ghost")
    ghost.deleted_at = datetime.now(UTC)
    ghost.is_active = False
    await db_session.flush()
    deleter = await _user(db_session, role=PlatformRole.PLATFORM_OWNER, label="deleter")

    response = await http.request(
        "DELETE", f"{API}/platform/users/{live.id}", headers=await _bearer(http, deleter)
    )
    assert response.status_code == 200, response.text

    # One live owner left. The ghost still carries `platform_role` in its row -
    # it predates the change that clears it - and must be worth nothing.
    assert (await _reread(db_session, ghost.id)).platform_role is PlatformRole.PLATFORM_OWNER
    remaining = await db_session.scalars(
        select(User).where(live_platform_role(PlatformRole.PLATFORM_OWNER))
    )
    assert [row.id for row in remaining] == [deleter.id]


async def test_a_tombstoned_owner_no_longer_holds_the_role(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """Deleting an owner clears `platform_role`, and the trail keeps the fact.

    A tombstone that still reads `platform_owner` is what made AUTHZ-02
    possible, and leaving the column set while every reader filters around it
    means the next reader has to remember. The history is not lost; it moves to
    the audit entry, which is where "who used to have authority" belongs.
    """
    target = await _user(db_session, role=PlatformRole.PLATFORM_OWNER, label="leaving")
    actor = await _user(db_session, role=PlatformRole.PLATFORM_OWNER, label="remaining")

    response = await http.request(
        "DELETE", f"{API}/platform/users/{target.id}", headers=await _bearer(http, actor)
    )
    assert response.status_code == 200, response.text

    row = await _reread(db_session, target.id)
    assert row.deleted_at is not None
    assert row.platform_role is None

    entry = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.target_id == target.id,
                AuditLog.action == AuditAction.USER_DELETED,
            )
        )
    ).scalar_one()
    assert entry.meta is not None
    assert entry.meta["previous_platform_role"] == PlatformRole.PLATFORM_OWNER.value
    assert entry.actor_id == actor.id


async def test_a_refused_hierarchy_attempt_changes_nothing_and_is_not_audited_as_a_deletion(
    http: AsyncClient, db_session: AsyncSession
) -> None:
    """A refusal must not leave a trail that reads like a success.

    The guard runs in a dependency, before the service and before anything is
    written, so the request is rejected with no `user_deleted` entry attached to
    it. Checked because the audit log is what an incident is reconstructed from,
    and an entry describing a deletion that did not happen is worse than none.
    """
    admin = await _user(db_session, role=PlatformRole.PLATFORM_ADMIN, label="admin")
    owner = await _user(db_session, role=PlatformRole.PLATFORM_OWNER, label="owner")

    response = await http.request(
        "DELETE", f"{API}/platform/users/{owner.id}", headers=await _bearer(http, admin)
    )

    assert response.status_code == 403
    entries = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.target_id == owner.id,
                    AuditLog.action.in_([AuditAction.USER_DELETED, AuditAction.USER_DISABLED]),
                )
            )
        )
        .scalars()
        .all()
    )
    assert list(entries) == []


# The §32 route policy, as code. Every platform route, and which guard it must
# resolve. A route added to this router without an entry fails the test below.
PLATFORM_ROUTE_POLICY = {
    ("GET", "/platform/overview"): "staff",
    ("GET", "/platform/tenants"): "staff",
    ("GET", "/platform/audit-logs"): "staff",
    ("POST", "/platform/invoices/{invoice_id}/payments"): "staff",
    ("POST", "/platform/invoices/{invoice_id}/void"): "staff",
    ("POST", "/platform/tenants/{tenant_id}/suspend"): "staff",
    ("POST", "/platform/tenants/{tenant_id}/restore"): "staff",
    ("POST", "/platform/tenants/{tenant_id}/ownership"): "staff",
    ("POST", "/platform/users/{user_id}/enable"): "staff",
    # The two that end an account's authority, and the only two that ask what
    # the target is.
    ("POST", "/platform/users/{user_id}/disable"): "target-aware",
    ("DELETE", "/platform/users/{user_id}"): "target-aware",
}


def _resolved(dependant: Any, seen: set[str] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    for sub in dependant.dependencies:
        call = getattr(sub, "call", None)
        if call is not None:
            seen.add(getattr(call, "__name__", type(call).__name__))
        _resolved(sub, seen)
    return seen


def _platform_routes(routes: Sequence[Any]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            if route.path.startswith("/platform/"):
                yield route
        elif isinstance(route, _IncludedRouter):
            yield from _platform_routes(route.original_router.routes)
        elif hasattr(route, "routes"):
            yield from _platform_routes(route.routes)


def test_each_platform_route_carries_the_guard_the_policy_says() -> None:
    """The hierarchy is a property of the route table, asserted as one.

    Without this the route-level guard has no detector: removing
    `PlatformAccountTargetDep` from `delete` and `disable` leaves every
    behavioural test above green, because `AccountService` enforces the same
    rule independently and answers the same `403`. That redundancy is the
    design - a service inherits nothing from a route guard - and it is exactly
    what makes the two layers invisible to each other's tests.

    Read from the resolved dependency graph rather than the decorators, because
    a guard can sit on the router instead of the route and an included router
    defers behind `_IncludedRouter`. The only honest question is what FastAPI
    resolves for this path.

    The other direction matters as much: nine routes must *not* be target-aware.
    Fixing AUTHZ-01 by putting the whole router behind an owner-only dependency
    would have passed every refusal test in this file and broken the product.
    """
    application = create_app(Settings(_env_file=None, environment="test"))
    actual: dict[tuple[str, str], str] = {}
    for route in _platform_routes(application.routes):
        names = _resolved(route.dependant)
        assert (
            "require_platform_roles" in names or "guard" in names
        ), f"{route.path} resolves no platform guard at all: {sorted(names)}"
        kind = "target-aware" if "require_platform_authority" in names else "staff"
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            actual[(method, route.path)] = kind

    assert actual == PLATFORM_ROUTE_POLICY
