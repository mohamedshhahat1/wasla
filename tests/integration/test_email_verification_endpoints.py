"""The verification endpoints, driven over HTTP against real rows.

What the service tests below this cannot see: whether the routes are reachable
without a session, whether the request schema really refuses the fields that
would let one account act on another, whether the limiter is wired to anything,
and whether the route commits at all.

That last one is not hypothetical. `include_router` preserves each route's own
class rather than adopting the parent's, so a router declared without
`route_class=CommittingRoute` silently never commits - and verification would
answer 200 with a timestamp while `email_verified_at` was rolled back on the
way out. `app/api/route.py` claims in its docstring that every router carries
the class; nothing checked, and one did not.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.routing import APIRoute, _IncludedRouter
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.api.route import CommittingRoute
from app.api.v1 import api_router
from app.core.config import Settings
from app.core.dependencies import get_session
from app.core.security import create_access_token, hash_password
from app.db.models.audit import AuditAction, AuditLog
from app.db.models.email import OutboundEmail
from app.db.models.email_verification import EmailVerificationChallenge
from app.db.models.user import User
from app.main import create_app
from app.services.email_templates import EmailTemplate
from app.services.email_verification_service import (
    _ATTEMPT_LIMIT,
    _SEND_LIMIT,
    INVALID_CODE,
    VERIFICATION_SENT_MESSAGE,
)
from tests.conftest import AllowingEntitlements

pytestmark = pytest.mark.integration

API = "/api/v1"
SEND = f"{API}/auth/email/verification/send"
VERIFY = f"{API}/auth/email/verification/verify"
PASSWORD = "correct horse battery staple"

# A code with a leading zero, used as the canary in the logging test. Distinctive
# enough that finding it in captured output means it came from here.
CANARY = "013579"


class _Redis:
    """Enough Redis for the limiter, and nothing more.

    The real `RateLimiter` runs against this, so a policy that is not applied
    leaves no counter here - which is what makes "the limiter is wired" an
    assertion rather than an assumption.
    """

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.fail = False

    async def incr(self, key: str) -> int:
        if self.fail:
            from redis.exceptions import RedisError

            raise RedisError("redis is unavailable")
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self.expiries.get(key, -1)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        return True

    async def exists(self, key: str) -> int:
        return 0

    async def rpush(self, key: str, value: str) -> int:
        return 1


class _Infra:
    def __init__(self) -> None:
        self.commands = _Redis()

    @property
    def client(self) -> _Redis:
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
        return None


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "log_format": "console",
        "log_level": "WARNING",
        "cors_origins": [],
        "rate_limit_enabled": False,
        "email_enabled": True,
        "email_provider": "fake",
        "email_from": "no-reply@example.com",
        "app_public_url": "https://app.example.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def verification_settings() -> Settings:
    return _settings()


@pytest.fixture
def redis() -> _Infra:
    return _Infra()


@pytest.fixture
def app(
    verification_settings: Settings,
    redis: _Infra,
    db_session: AsyncSession,
) -> Iterator[FastAPI]:
    application = create_app(verification_settings)
    application.state.database = _Infra()
    application.state.redis = redis

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


async def _account(session: AsyncSession, email: str) -> User:
    user = User(
        email=email,
        full_name="Owner",
        hashed_password=hash_password(PASSWORD),
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user


def _bearer(user: User, settings: Settings) -> dict[str, str]:
    token, _ = create_access_token(
        settings=settings,
        subject=user.id,
        tenant_id=None,
        token_version=user.token_version,
    )
    return {"Authorization": f"Bearer {token}"}


async def _code(session: AsyncSession, user: User) -> str:
    """The plaintext of the account's live challenge, from the queued mail."""
    challenge = (
        (
            await session.execute(
                select(EmailVerificationChallenge).where(
                    EmailVerificationChallenge.user_id == user.id,
                    EmailVerificationChallenge.consumed_at.is_(None),
                    EmailVerificationChallenge.superseded_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    assert challenge is not None
    row = (
        (
            await session.execute(
                select(OutboundEmail).where(
                    OutboundEmail.idempotency_key == f"email-verification:{challenge.id}",
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    return str(row.context["code"])


# ------------------------------------------------------------------- wiring


def _api_routes(routes: object) -> Iterator[APIRoute]:
    """Every concrete route, through the deferred-inclusion wrappers.

    `include_router` does not flatten: a mounted router appears as a single
    `_IncludedRouter` holding the original, whose routes keep the class they
    were declared with. Filtering `api_router.routes` for `APIRoute` therefore
    matches *nothing at all* - a check written that way passes vacuously, which
    is worse than not writing it, because the next person reads a green test as
    evidence.
    """
    for route in routes:  # type: ignore[attr-defined]
        if isinstance(route, _IncludedRouter):
            yield from _api_routes(route.original_router.routes)
        elif isinstance(route, APIRoute):
            yield route


def test_every_v1_route_commits_its_work() -> None:
    """The whole router tree, not only this feature's corner.

    A router that omits `route_class=CommittingRoute` does not inherit it from
    `api_router`; the route keeps the class it was built with. The failure is
    silent and total - the handler runs, the response is a success, and the
    transaction is discarded on the way out. `email_verification` shipped that
    way, and nothing in the suite noticed: every other test asserts on the
    session it passed in rather than on what was committed, and
    `app/api/route.py` asserted the property in a docstring instead of a test.
    """
    routes = list(_api_routes(api_router.routes))
    assert routes, "the walk found no routes, so this test would prove nothing"

    wrong = sorted(
        {route.path for route in routes if not isinstance(route, CommittingRoute)},
    )
    assert wrong == []


# ----------------------------------------------------------- authentication


@pytest.mark.parametrize("path", [SEND, VERIFY])
async def test_neither_route_is_reachable_without_a_session(
    http: AsyncClient,
    path: str,
) -> None:
    response = await http.post(path, json={"code": "000000"})
    assert response.status_code == 401


@pytest.mark.parametrize("path", [SEND, VERIFY])
async def test_a_forged_bearer_token_is_refused(http: AsyncClient, path: str) -> None:
    response = await http.post(
        path,
        json={"code": "000000"},
        headers={"Authorization": "Bearer not.a.real.token"},
    )
    assert response.status_code == 401


# -------------------------------------------------------------- the flow


async def test_sending_then_verifying_proves_the_address(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    user = await _account(db_session, "owner@acme-example.com")
    headers = _bearer(user, verification_settings)

    sent = await http.post(SEND, headers=headers)
    assert sent.status_code == 202
    assert sent.json() == {"message": VERIFICATION_SENT_MESSAGE}

    code = await _code(db_session, user)
    verified = await http.post(VERIFY, json={"code": code}, headers=headers)
    assert verified.status_code == 200
    assert verified.json()["verified_at"] is not None

    # Flushed rather than refreshed. `Session.refresh` re-reads the row and
    # discards pending changes to the attributes it reloads, and the commit
    # that would have made them durable does not happen here: the real
    # `get_session` parks its session on `request.state` for `CommittingRoute`
    # to find, and the override above yields one without doing so. Flushing
    # sends the UPDATE, which is what proves the change is a real write rather
    # than an attribute set on an object. That the route *does* commit in the
    # deployed application is covered by `test_every_v1_route_commits_its_work`
    # and, over a real socket, by `test_commit_boundary.py`.
    await db_session.flush()
    assert user.email_verified_at is not None

    stored = (
        await db_session.execute(select(User.email_verified_at).where(User.id == user.id))
    ).scalar_one()
    assert stored is not None


async def test_the_response_never_carries_the_code(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    """Not in the send response, and not in the verify response either."""
    user = await _account(db_session, "quiet@acme-example.com")
    headers = _bearer(user, verification_settings)

    sent = await http.post(SEND, headers=headers)
    code = await _code(db_session, user)
    assert code not in sent.text

    verified = await http.post(VERIFY, json={"code": code}, headers=headers)
    assert code not in verified.text


async def test_the_profile_reports_whether_the_address_is_proven(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    """So a client can decide whether to prompt without mailing a code to find out."""
    user = await _account(db_session, "profile@acme-example.com")
    headers = _bearer(user, verification_settings)

    before = await http.get(f"{API}/auth/me", headers=headers)
    assert before.status_code == 200
    assert before.json()["email_verified_at"] is None

    await http.post(SEND, headers=headers)
    code = await _code(db_session, user)
    await http.post(VERIFY, json={"code": code}, headers=headers)

    after = await http.get(f"{API}/auth/me", headers=headers)
    assert after.json()["email_verified_at"] is not None


# ------------------------------------------------------- cross-account reach


async def test_one_account_cannot_aim_verification_at_another(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    """Not a rule that is enforced - a request that cannot be expressed.

    `extra="forbid"` turns an attempt to name somebody else into a 422 rather
    than into a field that is quietly ignored, which is how somebody comes to
    believe they verified an address they did not.
    """
    attacker = await _account(db_session, "attacker@acme-example.com")
    victim = await _account(db_session, "victim@acme-example.com")
    headers = _bearer(attacker, verification_settings)

    for body in (
        {"code": "000000", "user_id": str(victim.id)},
        {"code": "000000", "email": victim.email},
        {"code": "000000", "target": str(victim.id)},
    ):
        response = await http.post(VERIFY, json=body, headers=headers)
        assert response.status_code == 422, response.text

    await db_session.flush()
    assert victim.email_verified_at is None


async def test_a_code_issued_to_one_account_does_not_verify_another(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    """The challenge is found by account, so a stolen code is useless elsewhere."""
    victim = await _account(db_session, "target@acme-example.com")
    attacker = await _account(db_session, "thief@acme-example.com")

    await http.post(SEND, headers=_bearer(victim, verification_settings))
    stolen = await _code(db_session, victim)

    response = await http.post(
        VERIFY,
        json={"code": stolen},
        headers=_bearer(attacker, verification_settings),
    )
    assert response.status_code == 422
    await db_session.flush()
    assert attacker.email_verified_at is None
    assert victim.email_verified_at is None


async def test_the_send_route_takes_no_body_it_could_aim_anywhere(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    """An address in the body is ignored entirely: the recipient is the row's.

    This is what makes the endpoint incapable of being an enumeration oracle or
    a way to make the platform mail a stranger - there is no address parameter
    to probe.
    """
    user = await _account(db_session, "self@acme-example.com")
    response = await http.post(
        SEND,
        json={"email": "somebody-else@example.com"},
        headers=_bearer(user, verification_settings),
    )
    assert response.status_code == 202

    rows = list(
        (
            await db_session.execute(
                select(OutboundEmail).where(
                    OutboundEmail.template == EmailTemplate.EMAIL_VERIFICATION.value,
                )
            )
        ).scalars()
    )
    assert [row.recipient for row in rows] == ["self@acme-example.com"]


# ------------------------------------------------------- indistinguishability


async def test_every_rejection_looks_the_same(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    """Wrong, expired, never-issued and replayed all answer identically.

    An error that separates "expired" from "wrong" tells somebody guessing
    whether it is worth continuing.
    """
    user = await _account(db_session, "same@acme-example.com")
    headers = _bearer(user, verification_settings)

    # No challenge at all.
    never = await http.post(VERIFY, json={"code": "111111"}, headers=headers)

    # A live challenge, wrong code.
    await http.post(SEND, headers=headers)
    wrong = await http.post(VERIFY, json={"code": "111111"}, headers=headers)

    # A consumed challenge, replayed.
    code = await _code(db_session, user)
    await http.post(VERIFY, json={"code": code}, headers=headers)
    replayed = await http.post(VERIFY, json={"code": code}, headers=headers)

    assert never.status_code == wrong.status_code == replayed.status_code == 422

    # Everything except the request id, which is per-request by design and is
    # the one field that is *supposed* to differ - it is how an operator ties a
    # complaint to a log line, and it says nothing about the code.
    def _without_request_id(response: object) -> dict:
        body = dict(response.json()["error"])  # type: ignore[attr-defined]
        body.pop("request_id", None)
        return body

    assert _without_request_id(never) == _without_request_id(wrong)
    assert _without_request_id(never) == _without_request_id(replayed)
    assert INVALID_CODE in never.text


async def test_asking_for_a_code_when_already_verified_says_the_same_thing(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    user = await _account(db_session, "done@acme-example.com")
    headers = _bearer(user, verification_settings)
    await http.post(SEND, headers=headers)
    code = await _code(db_session, user)
    await http.post(VERIFY, json={"code": code}, headers=headers)

    again = await http.post(SEND, headers=headers)
    assert again.status_code == 202
    assert again.json() == {"message": VERIFICATION_SENT_MESSAGE}


# ------------------------------------------------------------ malformed HTTP


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"code": ""},
        {"code": None},
        {"code": 482731},
        {"code": ["482731"]},
        {"code": "0" * 200},
        {"Code": "482731"},
    ],
)
async def test_a_malformed_body_is_refused_by_validation(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
    body: object,
) -> None:
    """Refused before Argon2 is reached.

    Hashing is the expensive step on this route, and it must never be reachable
    with arbitrary input length - an unbounded `code` field would make the
    endpoint a CPU amplifier.
    """
    user = await _account(db_session, f"malformed-{uuid.uuid4().hex[:8]}@acme-example.com")
    response = await http.post(
        VERIFY,
        json=body,
        headers=_bearer(user, verification_settings),
    )
    assert response.status_code == 422, response.text


async def test_a_non_json_body_is_refused(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    user = await _account(db_session, "formish@acme-example.com")
    response = await http.post(
        VERIFY,
        content="code=482731",
        headers={
            **_bearer(user, verification_settings),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert response.status_code == 422


# ------------------------------------------------------------ rate limiting


async def test_asking_for_codes_repeatedly_is_refused(
    db_session: AsyncSession,
    redis: _Infra,
) -> None:
    """The send budget bounds how much mail one account can cause."""
    settings = _settings(rate_limit_enabled=True)
    application = create_app(settings)
    application.state.database = _Infra()
    application.state.redis = redis

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements

    user = await _account(db_session, "eager@acme-example.com")
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://wasla.test",
    ) as client:
        headers = _bearer(user, settings)
        for _ in range(_SEND_LIMIT):
            assert (await client.post(SEND, headers=headers)).status_code == 202
        refused = await client.post(SEND, headers=headers)

    assert refused.status_code == 429
    assert "Retry-After" in refused.headers
    application.dependency_overrides.clear()


async def test_guessing_repeatedly_is_refused_and_recorded(
    db_session: AsyncSession,
    redis: _Infra,
) -> None:
    """The attempt budget, and the audit row a throttled attempt leaves.

    The entry is committed by the service rather than staged, because the
    refusal raises and an exception discards the request's transaction - so an
    entry written the ordinary way would be rolled back by the very refusal it
    describes, and "was this account being hammered" would have no answer.
    """
    settings = _settings(rate_limit_enabled=True)
    application = create_app(settings)
    application.state.database = _Infra()
    application.state.redis = redis

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements

    user = await _account(db_session, "guesser@acme-example.com")
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://wasla.test",
    ) as client:
        headers = _bearer(user, settings)
        for _ in range(_ATTEMPT_LIMIT):
            assert (await client.post(VERIFY, json={"code": "999999"}, headers=headers)).status_code
        refused = await client.post(VERIFY, json={"code": "999999"}, headers=headers)

    assert refused.status_code == 429
    application.dependency_overrides.clear()

    reasons = [
        row.meta.get("reason")
        for row in (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.target_id == user.id,
                    AuditLog.action == AuditAction.EMAIL_VERIFICATION_FAILED,
                )
            )
        ).scalars()
        if row.meta
    ]
    assert "rate_limited" in reasons


async def test_the_limit_survives_redis_being_unavailable(
    db_session: AsyncSession,
    redis: _Infra,
) -> None:
    """ADR-040: a control in front of a guessable secret degrades, it does not
    disappear.

    With the shared counter unreachable the limiter falls back to counting in
    this process - weaker than a distributed limit and enormously stronger than
    "unlimited for the duration of the outage".
    """
    settings = _settings(rate_limit_enabled=True)
    application = create_app(settings)
    application.state.database = _Infra()
    application.state.redis = redis
    redis.commands.fail = True

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements

    user = await _account(db_session, "outage@acme-example.com")
    statuses = []
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://wasla.test",
    ) as client:
        headers = _bearer(user, settings)
        for _ in range(_SEND_LIMIT + 3):
            statuses.append((await client.post(SEND, headers=headers)).status_code)

    application.dependency_overrides.clear()
    assert 429 in statuses, "a Redis outage must not mean an unlimited budget"


# ---------------------------------------------------------------- the canary


async def test_the_code_never_reaches_a_log(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A known code, every log record captured, and the code in none of them.

    Written as a regression test rather than a review note because the failure
    mode is a single `extra={"code": ...}` added by somebody debugging, which no
    other test in this repository would see.

    Everything is inspected, not only the message: `extra` fields become record
    attributes, and the structured formatter emits them, so a code smuggled in
    as an attribute would ship to whatever aggregates these logs.
    """
    user = await _account(db_session, "canary@acme-example.com")
    headers = _bearer(user, verification_settings)

    with caplog.at_level(logging.DEBUG):
        await http.post(SEND, headers=headers)
        code = await _code(db_session, user)
        # Both halves: the wrong guess and the successful one.
        await http.post(VERIFY, json={"code": CANARY}, headers=headers)
        await http.post(VERIFY, json={"code": code}, headers=headers)

        haystack = []
        for record in caplog.records:
            haystack.append(record.getMessage())
            haystack.extend(str(value) for value in record.__dict__.values())

    assert code not in "\n".join(haystack), "the issued code reached a log"
    assert CANARY not in "\n".join(haystack), "a submitted guess reached a log"


async def test_the_code_never_reaches_the_audit_trail(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    """An audit log of credentials is a second copy of them."""
    user = await _account(db_session, "trail@acme-example.com")
    headers = _bearer(user, verification_settings)

    await http.post(SEND, headers=headers)
    code = await _code(db_session, user)
    await http.post(VERIFY, json={"code": CANARY}, headers=headers)
    await http.post(VERIFY, json={"code": code}, headers=headers)

    rows = list(
        (
            await db_session.execute(
                select(AuditLog).where(AuditLog.target_id == user.id),
            )
        ).scalars()
    )
    assert rows
    for row in rows:
        rendered = f"{row.meta} {row.target_label} {row.actor_label}"
        assert code not in rendered
        assert CANARY not in rendered


# -------------------------------------------------------------- registration


async def test_registering_queues_a_verification_code_in_the_same_transaction(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A new account starts unverified with its first code already on the way.

    The challenge, the outbox row, the user, the workspace and the membership
    are one unit of work: a signup that rolls back mails nobody.
    """
    response = await http.post(
        f"{API}/auth/register",
        json={
            "email": "fresh@acme-example.com",
            "password": PASSWORD,
            "workspace_name": "Fresh",
            "workspace_slug": "fresh",
        },
    )
    assert response.status_code == 201, response.text

    user = (
        (
            await db_session.execute(
                select(User).where(User.email == "fresh@acme-example.com"),
            )
        )
        .scalars()
        .one()
    )
    assert user.email_verified_at is None, "a new account is not verified by existing"

    challenges = list(
        (
            await db_session.execute(
                select(EmailVerificationChallenge).where(
                    EmailVerificationChallenge.user_id == user.id,
                ),
            )
        ).scalars()
    )
    assert len(challenges) == 1
    assert challenges[0].email == "fresh@acme-example.com"

    queued = (
        (
            await db_session.execute(
                select(OutboundEmail).where(
                    OutboundEmail.idempotency_key == f"email-verification:{challenges[0].id}",
                ),
            )
        )
        .scalars()
        .first()
    )
    assert queued is not None
    assert queued.recipient == "fresh@acme-example.com"


async def test_registration_does_not_return_the_code(
    http: AsyncClient,
    db_session: AsyncSession,
) -> None:
    response = await http.post(
        f"{API}/auth/register",
        json={
            "email": "silent@acme-example.com",
            "password": PASSWORD,
            "workspace_name": "Silent",
            "workspace_slug": "silent",
        },
    )
    assert response.status_code == 201

    user = (
        (await db_session.execute(select(User).where(User.email == "silent@acme-example.com")))
        .scalars()
        .one()
    )
    challenge = (
        (
            await db_session.execute(
                select(EmailVerificationChallenge).where(
                    EmailVerificationChallenge.user_id == user.id,
                ),
            )
        )
        .scalars()
        .one()
    )
    row = (
        (
            await db_session.execute(
                select(OutboundEmail).where(
                    OutboundEmail.idempotency_key == f"email-verification:{challenge.id}",
                ),
            )
        )
        .scalars()
        .one()
    )
    assert str(row.context["code"]) not in response.text


async def test_an_unverified_account_can_use_the_application(
    http: AsyncClient,
    db_session: AsyncSession,
    verification_settings: Settings,
) -> None:
    """The most important assertion in this file.

    Verification grants nothing, so it must also withhold nothing. If this ever
    starts failing, somebody has turned an account-integrity fact into an
    authorization input and locked out every account created before it existed.
    """
    user = await _account(db_session, "unproven@acme-example.com")
    headers = _bearer(user, verification_settings)

    assert user.email_verified_at is None
    for path in (f"{API}/auth/me", "/health"):
        response = await http.get(path, headers=headers)
        assert response.status_code == 200, path
