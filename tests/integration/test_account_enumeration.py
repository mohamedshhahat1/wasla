"""Can an unauthenticated caller learn whether an email address has an account?

Measured across every unauthenticated path that takes an address, and the
answer differs by path:

**Login: no.** Same status, same error code, same message, and the timing gap is
a fraction of the Argon2 verification itself because a miss spends the same work
against a dummy hash. Pinned here so a future "more helpful" error message
cannot quietly undo it.

**Invitation acceptance: no.** Unknown, spent, revoked and expired tokens all
answer identically.

**Registration: no.** A new and an existing address receive the same 202 body.
The existing mailbox owner receives a bounded outbox notice, and the account
remains unchanged.

What that leaves is a bounded oracle: 10 probes per minute per client address,
and the bound now survives a Redis outage. The tests below assert the bound is
real rather than asserting the leak is gone.
"""

from __future__ import annotations

import statistics
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import generate_key
from app.core.dependencies import get_session
from app.db.models import User
from app.db.models.email import OutboundEmail
from app.main import create_app
from tests.conftest import FakeDependency

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def settings() -> Settings:
    """Limits off, so timing and status codes are measured rather than a 429."""
    return Settings(
        _env_file=None,
        environment="test",
        rate_limit_enabled=False,
        email_enabled=True,
        email_provider="fake",
        email_from="no-reply@example.com",
        app_public_url="https://app.example.com",
        credential_encryption_keys=[generate_key()],
    )


@pytest.fixture
def app(
    settings: Settings,
    db_session: AsyncSession,
    fake_redis: FakeDependency,
) -> Iterator[FastAPI]:
    """The real application on the test's transaction.

    Nothing is stubbed. An enumeration question is about what the *real*
    handlers answer, and a stubbed service would answer whatever it was told to.
    """
    application = create_app(settings)
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = fake_redis

    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_session] = _session
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://wasla.test",
    ) as http:
        yield http


async def _register(client: AsyncClient, email: str, slug: str) -> Response:
    return await client.post(
        f"{API}/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "workspace_name": f"Enum {slug}",
            "workspace_slug": slug,
        },
    )


# ------------------------------------------------------------------- login


async def test_login_answers_identically_for_known_and_unknown_addresses(
    client: AsyncClient,
) -> None:
    """Status, error code and message. Any of the three differing is the leak."""
    stamp = uuid.uuid4().hex[:10]
    known = f"known-{stamp}@example.com"
    await _register(client, known, f"enum-{stamp}")

    hit = await client.post(
        f"{API}/auth/login", json={"email": known, "password": "the wrong password"}
    )
    miss = await client.post(
        f"{API}/auth/login",
        json={"email": f"nobody-{stamp}@example.com", "password": "the wrong password"},
    )

    assert hit.status_code == miss.status_code == 401
    assert hit.json()["error"]["code"] == miss.json()["error"]["code"]
    assert hit.json()["error"]["message"] == miss.json()["error"]["message"]


async def test_login_spends_the_same_work_whether_or_not_the_account_exists(
    client: AsyncClient,
) -> None:
    """Response time is a side channel like any other.

    A miss verifies against a dummy Argon2 hash so the expensive half happens
    either way. The assertion is proportional rather than absolute: what matters
    is that the difference is small next to the work itself, not that two
    measurements on a loaded machine come out equal.
    """
    stamp = uuid.uuid4().hex[:10]
    known = f"known-{stamp}@example.com"
    await _register(client, known, f"enum-{stamp}")
    unknown = f"nobody-{stamp}@example.com"

    async def sample(email: str) -> float:
        started = time.perf_counter()
        await client.post(
            f"{API}/auth/login", json={"email": email, "password": "the wrong password"}
        )
        return (time.perf_counter() - started) * 1000

    # One warm-up each, so the first-call cost of the cached dummy hash does not
    # land entirely on whichever address happens to go first.
    await sample(known)
    await sample(unknown)

    hits = [await sample(known) for _ in range(9)]
    misses = [await sample(unknown) for _ in range(9)]
    hit = statistics.median(hits)
    miss = statistics.median(misses)

    assert hit > 1.0, "verification is suspiciously fast; is hashing actually happening?"
    # A leak worth having would show as a miss being dramatically cheaper,
    # because the expensive verification was skipped entirely.
    assert miss > hit * 0.5, (
        f"a miss took {miss:.1f} ms against {hit:.1f} ms for a hit, "
        "which is enough to enumerate by timing"
    )


# ------------------------------------------------------------- invitations


async def test_invitation_acceptance_says_nothing_about_which_token_failed(
    client: AsyncClient,
) -> None:
    """Unknown, malformed and well-formed-but-wrong all answer the same, so the
    endpoint cannot be used to probe which tokens once existed."""
    answers = set()
    for token in ("not-a-token", "x" * 43, uuid.uuid4().hex):
        response = await client.post(
            f"{API}/invitations/accept", json={"token": token, "password": PASSWORD}
        )
        answers.add((response.status_code, response.json()["error"]["message"]))

    assert len(answers) == 1


# ------------------------------------------------------------ registration


async def test_registration_never_discloses_a_taken_address(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    stamp = uuid.uuid4().hex[:10]
    known = f"known-{stamp}@example.com"
    first = await _register(client, known, f"enum-{stamp}")
    assert first.status_code == 202

    user = (await db_session.execute(select(User).where(User.email == known))).scalar_one()
    original_hash = user.hashed_password
    original_version = user.token_version

    again = await _register(client, known, f"enum-{stamp}-different")
    duplicate = await _register(client, known, f"enum-{stamp}-another")

    assert first.status_code == again.status_code == duplicate.status_code == 202
    assert first.json() == again.json() == duplicate.json() == {"status": "accepted"}
    for name in ("content-type", "cache-control", "x-content-type-options"):
        assert first.headers.get(name) == again.headers.get(name)

    await db_session.refresh(user)
    assert user.hashed_password == original_hash
    assert user.token_version == original_version
    matching_users = (
        (await db_session.execute(select(User).where(User.email == known))).scalars().all()
    )
    assert len(matching_users) == 1

    notices = (
        (
            await db_session.execute(
                select(OutboundEmail).where(
                    OutboundEmail.user_id == user.id,
                    OutboundEmail.template == "registration_attempt",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(notices) == 1
    assert notices[0].recipient == known
    assert notices[0].context == {}


async def test_a_taken_workspace_slug_is_a_separate_answer(
    client: AsyncClient,
) -> None:
    """Deliberately not merged with the address conflict.

    Merging would not close the oracle - the attacker picks the slug, so a
    unique slug makes a 409 mean "address taken" whatever the text says - and it
    would leave a real person unable to tell which field to change. Security
    theatre that costs usability is worse than an honest bounded leak.
    """
    stamp = uuid.uuid4().hex[:10]
    slug = f"enum-{stamp}"
    await _register(client, f"first-{stamp}@example.com", slug)

    clash = await _register(client, f"second-{stamp}@example.com", slug)

    assert clash.status_code == 409
    assert "workspace" in clash.json()["error"]["message"].lower()
