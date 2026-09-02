"""Tests for the OAuth flow store, PKCE, and the Google authorization request.

The shared `FakeRedisCommands` in `tests/conftest.py` implements `rpush`,
`incr`, `expire` and `ttl` - enough for the rate limiter and nothing else. This
module brings its own fake rather than extending that one, because 237 existing
tests depend on the shared fixture and widening it to suit this feature is a
change to their substrate for no benefit to them.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from typing import cast
from urllib.parse import parse_qs, urlparse

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.exceptions import DependencyUnavailableError
from app.core.oauth_binding import hash_binding
from app.core.oauth_flow import (
    FLOW_TTL_SECONDS,
    KEY_PREFIX,
    FlowKind,
    OAuthFlowStore,
    code_challenge,
)
from app.core.redis import RedisClient
from app.integrations.google.client import (
    GOOGLE_AUTHORIZATION_URL,
    MAX_CODE_LENGTH,
    SCOPES,
    GoogleExchangeError,
    GoogleOAuthClient,
)

CLIENT_ID = "1234567890-abcdef.apps.googleusercontent.com"
REDIRECT_URI = "https://app.wasla.test/auth/google/callback"


class _FakePipeline:
    """Enough of a redis-py pipeline to be MULTI/EXEC for these tests."""

    def __init__(self, store):
        self._store = store
        self._queued = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_: object):
        return False

    def get(self, key):
        self._queued.append(("get", key))

    def delete(self, key):
        self._queued.append(("delete", key))

    async def execute(self):
        if self._store.broken:
            raise RedisConnectionError("redis is down")
        results = []
        for operation, key in self._queued:
            if operation == "get":
                results.append(self._store.values.get(key))
            else:
                results.append(1 if self._store.values.pop(key, None) is not None else 0)
        return results


class _FakeRedisCommands:
    def __init__(self):
        self.values = {}
        self.expiries = {}
        self.broken = False

    async def set(self, key, value, *, ex=None, nx=False):
        if self.broken:
            raise RedisConnectionError("redis is down")
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.expiries[key] = ex
        return True

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakeRedis:
    def __init__(self):
        self.commands = _FakeRedisCommands()

    @property
    def client(self):
        return self.commands


# A digest, because that is what the store is given: the secret itself never
# reaches it, which is what keeps the value in Redis from being enough to finish
# a flow.
BINDING = hash_binding("a-browser-binding-secret")


def _store():
    fake = _FakeRedis()
    return OAuthFlowStore(cast("RedisClient", fake)), fake


# --- state --------------------------------------------------------------------


async def test_a_flow_can_be_started_and_spent_once():
    store, _ = _store()
    started = await store.start(kind=FlowKind.LOGIN, binding=BINDING)

    spent = await store.spend(state=started.state)
    assert spent is not None
    assert spent.nonce == started.flow.nonce
    assert spent.code_verifier == started.flow.code_verifier

    # Single use. The second attempt is indistinguishable from a state that
    # never existed, which is the point.
    assert await store.spend(state=started.state) is None


async def test_state_and_nonce_are_unpredictable():
    store, _ = _store()
    states = set()
    nonces = set()
    for _ in range(20):
        started = await store.start(kind=FlowKind.LOGIN, binding=BINDING)
        states.add(started.state)
        nonces.add(started.flow.nonce)
    assert len(states) == 20
    assert len(nonces) == 20
    # 32 bytes, url-safe base64, no padding.
    assert all(len(state) >= 43 for state in states)


async def test_a_flow_is_stored_with_a_bounded_lifetime():
    store, fake = _store()
    started = await store.start(kind=FlowKind.LOGIN, binding=BINDING)
    assert fake.commands.expiries[f"{KEY_PREFIX}{started.state}"] == FLOW_TTL_SECONDS


async def test_an_expired_or_unknown_state_is_refused():
    store, _ = _store()
    # Nothing was stored, which is what an expired key looks like from here.
    assert await store.spend(state="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") is None


@pytest.mark.parametrize(
    "state",
    ["", "short", "has spaces in it and is long enough", "x" * 200, "../../etc/passwd"],
    ids=["empty", "too-short", "spaces", "too-long", "traversal"],
)
async def test_a_misshapen_state_never_becomes_a_lookup(state):
    store, fake = _store()
    fake.commands.broken = True
    # Refused on shape alone, so Redis is never touched - which is also why a
    # broken Redis does not raise here.
    assert await store.spend(state=state) is None


async def test_a_flow_records_which_kind_it_is():
    store, _ = _store()
    login = await store.start(kind=FlowKind.LOGIN, binding=BINDING)
    link = await store.start(kind=FlowKind.LINK, binding=BINDING, user_id=uuid.uuid4())

    assert (await store.spend(state=login.state)).kind is FlowKind.LOGIN
    assert (await store.spend(state=link.state)).kind is FlowKind.LINK


async def test_a_link_flow_remembers_the_account_that_started_it():
    store, _ = _store()
    user_id = uuid.uuid4()
    started = await store.start(kind=FlowKind.LINK, binding=BINDING, user_id=user_id)
    spent = await store.spend(state=started.state)
    assert spent is not None
    assert spent.user_id == user_id


async def test_a_login_flow_carries_no_account():
    store, _ = _store()
    started = await store.start(kind=FlowKind.LOGIN, binding=BINDING)
    assert (await store.spend(state=started.state)).user_id is None


async def test_a_corrupt_record_is_treated_as_absent():
    store, fake = _store()
    started = await store.start(kind=FlowKind.LOGIN, binding=BINDING)
    fake.commands.values[f"{KEY_PREFIX}{started.state}"] = "{not json"
    assert await store.spend(state=started.state) is None


# --- degradation --------------------------------------------------------------


async def test_starting_a_flow_fails_closed_when_redis_is_down():
    store, fake = _store()
    fake.commands.broken = True
    with pytest.raises(DependencyUnavailableError):
        await store.start(kind=FlowKind.LOGIN, binding=BINDING)


async def test_spending_a_flow_fails_closed_when_redis_is_down():
    """ADR-051. A replay control that answers "not found" during an outage is a
    replay control that has been switched off, so this raises instead."""
    store, fake = _store()
    started = await store.start(kind=FlowKind.LOGIN, binding=BINDING)
    fake.commands.broken = True
    with pytest.raises(DependencyUnavailableError):
        await store.spend(state=started.state)


# --- PKCE ---------------------------------------------------------------------


def test_the_pkce_challenge_is_s256_of_the_verifier():
    verifier = "a-verifier-value"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert code_challenge(verifier) == expected
    assert "=" not in code_challenge(verifier)


def test_the_challenge_never_contains_the_verifier():
    verifier = "a-verifier-value"
    assert verifier not in code_challenge(verifier)


async def test_the_verifier_meets_the_rfc_length_floor():
    store, _ = _store()
    started = await store.start(kind=FlowKind.LOGIN, binding=BINDING)
    assert 43 <= len(started.flow.code_verifier) <= 128


# --- the authorization request ------------------------------------------------


def _client():
    return GoogleOAuthClient(
        client_id=CLIENT_ID,
        client_secret="a-client-secret",
        redirect_uri=REDIRECT_URI,
    )


def _params(url):
    return {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}


def test_the_authorization_url_points_at_google():
    url = _client().authorization_url(state="s", nonce="n", challenge="c")
    assert url.startswith(GOOGLE_AUTHORIZATION_URL + "?")


def test_the_authorization_url_carries_the_flow_values():
    params = _params(
        _client().authorization_url(state="the-state", nonce="the-nonce", challenge="c")
    )
    assert params["state"] == "the-state"
    assert params["nonce"] == "the-nonce"
    assert params["code_challenge"] == "c"
    assert params["code_challenge_method"] == "S256"
    assert params["response_type"] == "code"
    assert params["scope"] == " ".join(SCOPES)


def test_the_redirect_uri_comes_from_configuration():
    """There is no argument to this method that could carry a redirect target."""
    params = _params(_client().authorization_url(state="s", nonce="n", challenge="c"))
    assert params["redirect_uri"] == REDIRECT_URI


def test_the_authorization_request_never_asks_for_offline_access():
    """A regression guard on a promise.

    `access_type=online` is why no Google refresh token can be stored: none is
    issued. Change it to `offline` and the claim quietly stops being true, so
    this test exists to make that change loud.
    """
    params = _params(_client().authorization_url(state="s", nonce="n", challenge="c"))
    assert params["access_type"] == "online"


def test_the_client_secret_is_never_in_the_authorization_url():
    url = _client().authorization_url(state="s", nonce="n", challenge="c")
    assert "a-client-secret" not in url


# --- the code exchange --------------------------------------------------------


class _ScriptedClient(GoogleOAuthClient):
    """A client whose token request is scripted rather than networked."""

    def __init__(self, outcome, **kwargs):
        super().__init__(**kwargs)
        self.outcome = outcome
        self.form = None

    async def _post_token_request(self, form):
        self.form = form
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _scripted(outcome):
    return _ScriptedClient(
        outcome,
        client_id=CLIENT_ID,
        client_secret="a-client-secret",
        redirect_uri=REDIRECT_URI,
    )


async def test_an_exchange_returns_only_the_id_token():
    client = _scripted({"id_token": "the.id.token", "access_token": "an-access-token"})
    assert await client.exchange(code="a-code", code_verifier="a-verifier") == "the.id.token"


async def test_the_exchange_sends_the_verifier_and_the_configured_redirect():
    client = _scripted({"id_token": "t"})
    await client.exchange(code="a-code", code_verifier="a-verifier")
    assert client.form["code_verifier"] == "a-verifier"
    assert client.form["redirect_uri"] == REDIRECT_URI
    assert client.form["grant_type"] == "authorization_code"


async def test_a_response_without_an_id_token_is_a_failure():
    client = _scripted({"access_token": "an-access-token"})
    with pytest.raises(GoogleExchangeError):
        await client.exchange(code="a-code", code_verifier="a-verifier")


async def test_a_refused_exchange_is_a_failure():
    client = _scripted(GoogleExchangeError("refused"))
    with pytest.raises(GoogleExchangeError):
        await client.exchange(code="a-code", code_verifier="a-verifier")


@pytest.mark.parametrize("code", ["", "x" * (MAX_CODE_LENGTH + 1)], ids=["empty", "oversized"])
async def test_an_unusable_code_is_never_relayed_to_google(code):
    client = _scripted({"id_token": "t"})
    with pytest.raises(GoogleExchangeError):
        await client.exchange(code=code, code_verifier="a-verifier")
    assert client.form is None
