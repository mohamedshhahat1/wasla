"""Completing somebody else's Google authorization, and why it no longer works.

The attack (SEC-07, CWE-352). The state was unguessable, single-use,
ten-minute and server-side, which proves *this server issued it*. The API is
cookieless, so nothing in the callback could prove *this browser asked*. An
attacker therefore starts a Google authorization on their own account, captures
the `code` and `state` Google hands back, and induces a victim's browser to post
them to the callback. The victim is signed in - as the attacker, on the
attacker's account - and whatever they type next goes somewhere the attacker can
read. The link flow is the same shape with a worse ending: a Google identity
grafted onto an account, which is a permanent additional way in.

What closes it is one value only the initiating browser holds. `authorize` sets
a random secret in a cookie and stores its SHA-256 beside the state; the
callback must present a cookie that hashes to it.

**Two clients, not one.** Every refusal below is written with a second
`AsyncClient` against the same application, because a browser is exactly what an
httpx client's cookie jar models. A test that hand-set a header would be
asserting that a function compares two strings; these assert that a *different
browser* cannot finish the flow, which is the actual claim.

The happy path lives in `test_google_endpoints.py` and passes without any
special handling, because one client carries its cookies the way one browser
does. That is the point: the binding is invisible to a legitimate flow.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_entitlement_service
from app.api.v1 import google_oauth as google_routes
from app.core.config import Settings
from app.core.dependencies import SESSION_STATE_ATTRIBUTE, get_session
from app.core.oauth_binding import COOKIE_NAME
from app.db.models.user import User
from app.main import create_app
from tests.conftest import AllowingEntitlements, FakeDependency

# The harness is imported, the fixtures are not. Every integration module in
# this suite builds its own application fixture - importing one would shadow the
# name a test then takes as a parameter - so what is shared here is the
# machinery that is genuinely expensive or genuinely subtle: the RSA key, the
# key document, the token signer, the Redis fake that implements MULTI/EXEC
# faithfully, and the stubbed exchange. Only Google's network is replaced; the
# verifier, the flow store, the routes and the database are real.
from tests.integration.test_google_endpoints import (
    API,
    CLIENT_ID,
    GOOGLE_EMAIL,
    REDIRECT_URI,
    _account,
    _bearer,
    _Exchange,
    _FixedKeyRing,
    _FlowRedis,
    _id_token,
    _identities,
    _start,
    _StubbedClient,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def google_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        google_enabled=True,
        google_client_id=CLIENT_ID,
        google_client_secret="a-test-client-secret",
        google_redirect_uri=REDIRECT_URI,
    )


@pytest.fixture
def exchange() -> Iterator[_Exchange]:
    holder = _Exchange()
    previous = _StubbedClient.exchange_result
    _StubbedClient.exchange_result = holder
    try:
        yield holder
    finally:
        _StubbedClient.exchange_result = previous


@pytest.fixture
def google_app(
    google_settings: Settings,
    db_session: AsyncSession,
    exchange: _Exchange,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FastAPI]:
    """The real application, with Google's network replaced and nothing else."""
    monkeypatch.setattr(google_routes, "GoogleOAuthClient", _StubbedClient)
    monkeypatch.setattr(google_routes, "_KEY_RING", _FixedKeyRing())

    application = create_app(google_settings)
    application.state.database = FakeDependency(name="postgresql")
    application.state.redis = _FlowRedis()

    async def _session(request: Request) -> AsyncIterator[AsyncSession]:
        setattr(request.state, SESSION_STATE_ATTRIBUTE, db_session)
        yield db_session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_entitlement_service] = AllowingEntitlements
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def http(google_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=google_app),
        base_url="http://wasla.test",
    ) as client:
        yield client


@pytest_asyncio.fixture
async def other_browser(google_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A second browser against the same application: its own cookie jar.

    This is the whole apparatus for the attack. The attacker's browser and the
    victim's browser differ in exactly one respect - which cookies they hold -
    and that is the difference the binding is supposed to notice.
    """
    async with AsyncClient(
        transport=ASGITransport(app=google_app),
        base_url="http://wasla.test",
    ) as client:
        yield client


def _binding(client: AsyncClient) -> str | None:
    """The binding secret this browser is holding, if any."""
    return client.cookies.get(COOKIE_NAME)


async def _callback(client: AsyncClient, state: str, *, code: str = "an-authorization-code"):
    return await client.post(f"{API}/auth/google/callback", json={"code": code, "state": state})


# --- the cookie a browser is given -------------------------------------------


async def test_starting_a_login_hands_the_browser_a_binding_secret(http: AsyncClient) -> None:
    """One cookie, high entropy, and nothing else in it.

    The value must be a secret and not a session: this API has no CSRF tokens
    precisely because it authenticates by bearer token, and a cookie that
    carried anything would have invalidated that reasoning.
    """
    response = await http.post(f"{API}/auth/google/authorize")

    assert response.status_code == 200
    header = response.headers.get("set-cookie", "")
    assert header.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.replace("SameSite=Lax", "SameSite=lax")

    secret = _binding(http)
    assert secret is not None
    assert len(secret) == 43
    # Not derived from anything the response also carries.
    assert secret not in response.text


async def test_a_second_tab_keeps_the_binding_the_first_one_got(http: AsyncClient) -> None:
    """One secret per browser, not per flow.

    A fresh value on every initiation would mean opening Google sign-in in a
    second tab silently broke the first, and the person would get a refusal
    indistinguishable from an attack. Both flows share the browser's secret, so
    both remain completable.
    """
    await http.post(f"{API}/auth/google/authorize")
    first = _binding(http)

    await http.post(f"{API}/auth/google/authorize")

    assert _binding(http) == first


async def test_two_browsers_are_never_given_the_same_secret(
    http: AsyncClient,
    other_browser: AsyncClient,
) -> None:
    await http.post(f"{API}/auth/google/authorize")
    await other_browser.post(f"{API}/auth/google/authorize")

    assert _binding(http) != _binding(other_browser)


async def test_the_stored_flow_still_holds_no_secret_a_reader_could_use(
    http: AsyncClient,
    google_app: FastAPI,
) -> None:
    """Only the digest is in Redis.

    Somebody who can read the flow keyspace must not come away able to complete
    a flow, which is why the browser's value is hashed before it is stored.
    """
    await http.post(f"{API}/auth/google/authorize")
    secret = _binding(http)
    assert secret is not None

    stored = "".join(google_app.state.redis.commands.values.values())

    assert secret not in stored


# --- the callback ------------------------------------------------------------


async def test_a_callback_with_no_binding_cookie_is_refused(
    http: AsyncClient,
    other_browser: AsyncClient,
    exchange: _Exchange,
    db_session: AsyncSession,
) -> None:
    """A valid, unspent, correctly-typed state is not enough on its own.

    This is the minimal statement of the finding: everything the old code
    checked still passes here, and the callback is refused anyway.
    """
    state, nonce = await _start(http, "/auth/google/authorize")
    exchange.id_token = _id_token(nonce=nonce)

    response = await _callback(other_browser, state)

    assert response.status_code == 401
    assert await db_session.scalar(select(User).where(User.email == GOOGLE_EMAIL)) is None
    assert await _identities(db_session) == []


async def test_a_callback_from_a_different_browser_is_refused(
    http: AsyncClient,
    other_browser: AsyncClient,
    exchange: _Exchange,
    db_session: AsyncSession,
) -> None:
    """The attack itself: browser B holds a binding, just not the right one.

    Both browsers have started their own authorization, so both hold a
    well-formed cookie. Only one of them holds the cookie that matches *this*
    state.
    """
    await other_browser.post(f"{API}/auth/google/authorize")
    assert _binding(other_browser) is not None

    state, nonce = await _start(http, "/auth/google/authorize")
    exchange.id_token = _id_token(nonce=nonce)

    response = await _callback(other_browser, state)

    assert response.status_code == 401
    assert await _identities(db_session) == []


async def test_a_refused_callback_never_reaches_google(
    http: AsyncClient,
    other_browser: AsyncClient,
    exchange: _Exchange,
) -> None:
    """The binding is checked before the exchange, which costs the attacker
    everything and us nothing.

    A callback from the wrong browser makes no outbound request at all, so it
    cannot be used to make this deployment talk to Google on somebody else's
    schedule, and no authorization code is ever spent.
    """
    state, nonce = await _start(http, "/auth/google/authorize")
    exchange.id_token = _id_token(nonce=nonce)

    await _callback(other_browser, state)

    assert exchange.calls == []


async def test_a_forged_callback_burns_only_the_attacker_s_own_state(
    http: AsyncClient,
    other_browser: AsyncClient,
    exchange: _Exchange,
) -> None:
    """Consuming the state on a binding failure is the right way round.

    In the attack the state being presented belongs to whoever is presenting
    it, so burning it costs the attacker their flow and costs the victim
    nothing - the victim's own flow, under their own state, is untouched.
    """
    victim_state, victim_nonce = await _start(http, "/auth/google/authorize")
    attacker_state, _ = await _start(other_browser, "/auth/google/authorize")

    exchange.id_token = _id_token(nonce=victim_nonce)
    refused = await _callback(http, attacker_state)
    assert refused.status_code == 401

    # The victim's own flow still completes normally afterwards.
    exchange.id_token = _id_token(nonce=victim_nonce)
    assert (await _callback(http, victim_state)).status_code == 200


async def test_a_replayed_callback_is_still_refused_for_the_right_browser(
    http: AsyncClient,
    exchange: _Exchange,
) -> None:
    """Binding must not have turned a single-use state into a reusable one.

    The second attempt comes from the same browser with the same cookie, so the
    binding passes and the *state* is what refuses - which is the property that
    existed before this change and had to survive it.
    """
    state, nonce = await _start(http, "/auth/google/authorize")
    exchange.id_token = _id_token(nonce=nonce)

    assert (await _callback(http, state)).status_code == 200

    exchange.id_token = _id_token(nonce=nonce)
    assert (await _callback(http, state)).status_code == 401


async def test_an_unknown_state_is_refused_even_with_a_valid_cookie(
    http: AsyncClient,
    exchange: _Exchange,
) -> None:
    """The binding is an addition, not a replacement. A browser holding a
    perfectly good cookie still cannot invent a state."""
    await http.post(f"{API}/auth/google/authorize")

    response = await _callback(http, "a" * 43)

    assert response.status_code == 401
    assert exchange.calls == []


async def test_a_tampered_cookie_is_refused(
    http: AsyncClient,
    exchange: _Exchange,
) -> None:
    """A near-miss is a miss. The comparison is over a digest, so changing one
    character of the secret changes every character of what is compared."""
    state, nonce = await _start(http, "/auth/google/authorize")
    exchange.id_token = _id_token(nonce=nonce)
    secret = _binding(http)
    assert secret is not None
    http.cookies.set(COOKIE_NAME, secret[:-1] + ("A" if secret[-1] != "A" else "B"))

    assert (await _callback(http, state)).status_code == 401


# --- what the cookie does after a callback -----------------------------------


async def test_the_cookie_is_cleared_after_a_successful_callback(
    http: AsyncClient,
    exchange: _Exchange,
) -> None:
    """The secret is destroyed as soon as it has done its job.

    The documented cost is the concurrent-tab case below: a second flow still
    pending in the same browser is no longer completable. That is a rare
    inconvenience against a secret that lingers, and the person simply starts
    sign-in again.
    """
    state, nonce = await _start(http, "/auth/google/authorize")
    exchange.id_token = _id_token(nonce=nonce)

    assert (await _callback(http, state)).status_code == 200

    assert _binding(http) is None


async def test_a_refused_callback_leaves_the_cookie_alone(
    http: AsyncClient,
    other_browser: AsyncClient,
    exchange: _Exchange,
) -> None:
    """Clearing on failure would be a denial of service, not a defence.

    Anybody who can induce one forged callback in the victim's browser could
    otherwise destroy the binding for a legitimate flow running in it. So a
    refusal changes nothing, and the victim's real flow still completes.
    """
    victim_state, victim_nonce = await _start(http, "/auth/google/authorize")
    held = _binding(http)

    attacker_state, _ = await _start(other_browser, "/auth/google/authorize")
    assert (await _callback(http, attacker_state)).status_code == 401
    assert _binding(http) == held

    exchange.id_token = _id_token(nonce=victim_nonce)
    assert (await _callback(http, victim_state)).status_code == 200


async def test_a_second_pending_flow_in_the_same_browser_ends_with_the_first(
    http: AsyncClient,
    exchange: _Exchange,
) -> None:
    """The concurrency behaviour, pinned rather than discovered.

    Two flows started in one browser share its secret, so both are completable
    - until one of them succeeds and the secret is cleared. Starting sign-in
    again mints a new one, so this is a restart rather than a lockout. Written
    down because the alternative reading, "latest flow wins and silently breaks
    the earlier one at `authorize` time", is what a per-flow cookie would have
    given and is worse.
    """
    first_state, first_nonce = await _start(http, "/auth/google/authorize")
    second_state, _ = await _start(http, "/auth/google/authorize")

    exchange.id_token = _id_token(nonce=first_nonce)
    assert (await _callback(http, first_state)).status_code == 200

    exchange.id_token = _id_token(nonce=first_nonce)
    assert (await _callback(http, second_state)).status_code == 401

    # And the browser is not stuck: a fresh authorization works immediately.
    third_state, third_nonce = await _start(http, "/auth/google/authorize")
    exchange.id_token = _id_token(nonce=third_nonce)
    assert (await _callback(http, third_state)).status_code == 200


# --- the link flow, which is the more sensitive of the two -------------------


async def _signed_in(
    db_session: AsyncSession,
    google_settings: Settings,
    *,
    email: str,
) -> tuple[User, dict[str, str]]:
    """An account, and the header that authenticates it.

    Both halves come from the Google suite's own helpers, so a change to how
    this application mints an access token cannot leave this file authenticating
    against a shape the real one no longer issues.
    """
    user = await _account(db_session, email=email)
    return user, _bearer(user, google_settings)


async def test_linking_from_another_browser_is_refused_even_with_the_right_session(
    http: AsyncClient,
    other_browser: AsyncClient,
    exchange: _Exchange,
    db_session: AsyncSession,
    google_settings: Settings,
) -> None:
    """Two bindings, and the second one is not redundant.

    The account is recorded in the flow, so a link can only ever land on
    whoever started it - that check already existed. This proves the addition:
    the *same* authenticated user, presenting the *same* state from a different
    browser, is refused. Without it, an attacker who could get a victim's
    browser to post their state would graft a Google identity they control onto
    the victim's account, which is a permanent additional way in.
    """
    user, auth = await _signed_in(db_session, google_settings, email="linker@example.com")

    state, nonce = await _start(http, "/auth/identities/google/authorize", headers=auth)
    exchange.id_token = _id_token(nonce=nonce)

    response = await other_browser.post(
        f"{API}/auth/identities/google/link",
        json={"code": "c", "state": state},
        headers=auth,
    )

    assert response.status_code == 401
    assert await _identities(db_session) == []
    assert exchange.calls == []


async def test_linking_from_the_initiating_browser_still_works(
    http: AsyncClient,
    exchange: _Exchange,
    db_session: AsyncSession,
    google_settings: Settings,
) -> None:
    """The control. Every refusal above has to be the binding firing, not
    linking being broken."""
    user, auth = await _signed_in(db_session, google_settings, email="linker@example.com")

    state, nonce = await _start(http, "/auth/identities/google/authorize", headers=auth)
    exchange.id_token = _id_token(nonce=nonce)

    response = await http.post(
        f"{API}/auth/identities/google/link",
        json={"code": "c", "state": state},
        headers=auth,
    )

    assert response.status_code == 200, response.text
    identities = await _identities(db_session)
    assert len(identities) == 1
    assert identities[0].user_id == user.id


# --- what must not have changed ----------------------------------------------


async def test_the_redirect_target_is_still_fixed_configuration(http: AsyncClient) -> None:
    """No open redirect was introduced along with the cookie.

    Nothing in this flow reads a redirect target from a caller: the authorize
    response names `GOOGLE_REDIRECT_URI` and there is no request field that
    could change it.
    """
    response = await http.post(
        f"{API}/auth/google/authorize",
        json={"redirect_uri": "https://evil.test/steal"},
    )

    url = response.json()["authorization_url"]
    assert "evil.test" not in url
    assert "app.wasla.test" in url


async def test_the_binding_never_appears_in_a_response_body(
    http: AsyncClient,
    exchange: _Exchange,
) -> None:
    """The secret lives in a `Set-Cookie` header and nowhere else.

    A copy in the JSON would be readable by script and would undo `HttpOnly`.
    """
    start = await http.post(f"{API}/auth/google/authorize")
    secret = _binding(http)
    assert secret is not None
    assert secret not in start.text

    state, nonce = await _start(http, "/auth/google/authorize")
    exchange.id_token = _id_token(nonce=nonce)
    finished = await _callback(http, state)

    assert secret not in finished.text
