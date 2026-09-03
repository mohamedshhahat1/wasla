"""The cookie that ties an OAuth flow to one browser.

`test_oauth_browser_binding.py` proves the behaviour over HTTP. This file proves
the properties of the value itself, which are the ones a passing end-to-end test
would not notice: that the cookie carries a secret and not a session, that
production gets the attributes it needs, and that a caller-supplied cookie
cannot become a digest of whatever it likes.

The attribute assertions are deliberately literal. `Secure`, `HttpOnly`,
`SameSite` and the absence of `Domain` are not implementation detail here - they
are the difference between a binding cookie and a cookie a neighbouring
subdomain can set, and each one is a separate way to get this wrong quietly.
"""

from __future__ import annotations

import pytest
from fastapi import Response
from starlette.datastructures import Headers
from starlette.requests import Request

from app.core.config import Settings
from app.core.oauth_binding import (
    BINDING_BYTES,
    COOKIE_NAME,
    SECURE_COOKIE_NAME,
    attach,
    clear,
    cookie_name,
    ensure,
    hash_binding,
    matches,
    presented,
    uses_secure_cookies,
)
from app.core.oauth_flow import FLOW_TTL_SECONDS

SECRET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFG"  # 43 chars, the issued shape


def _settings(environment: str) -> Settings:
    return Settings(
        _env_file=None,
        environment=environment,
        log_format="console",
        log_level="WARNING",
        cors_origins=[],
        rate_limit_enabled=False,
        # Required outside `test`, and a literal here rather than a fixture
        # because these tests are about cookie attributes and nothing signs a
        # token in them.
        jwt_secret="a-random-value-long-enough-to-satisfy-the-minimum-length-check",
    )


def _production() -> Settings:
    """A settings object production would actually accept.

    Built in full rather than faked, because the point of naming production in
    the table below is that the real thing lands on the secure branch - and a
    stand-in would prove only that a string comparison works.
    """
    return Settings(
        _env_file=None,
        environment="production",
        log_format="json",
        log_level="WARNING",
        debug=False,
        docs_enabled=False,
        cors_origins=["https://app.wasla.test"],
        meta_app_secret="a-meta-app-secret-value",
        jwt_secret="a-random-value-long-enough-to-satisfy-the-minimum-length-check",
    )


def _request(cookie: str | None) -> Request:
    """A request carrying at most one cookie, built without an app."""
    raw = [(b"host", b"wasla.test")]
    if cookie is not None:
        raw.append((b"cookie", cookie.encode()))
    return Request({"type": "http", "headers": Headers(raw=raw).raw, "method": "POST", "path": "/"})


def _set_cookie(response: Response) -> str:
    values = response.headers.getlist("set-cookie")
    assert len(values) == 1, values
    return values[0]


# --- the value ---------------------------------------------------------------


def test_a_minted_secret_is_full_entropy_and_url_safe() -> None:
    """256 bits, and shaped so it survives a cookie header unescaped."""
    secret = ensure(_request(None), _settings("test"))

    assert len(secret) == 43  # 32 bytes, url-safe base64, no padding
    assert BINDING_BYTES == 32
    assert set(secret) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_two_browsers_never_receive_the_same_secret() -> None:
    settings = _settings("test")
    minted = {ensure(_request(None), settings) for _ in range(50)}

    assert len(minted) == 50


def test_the_stored_value_is_a_digest_and_never_the_secret() -> None:
    """A reader of the Redis keyspace must not come away able to finish a flow."""
    digest = hash_binding(SECRET)

    assert SECRET not in digest
    assert len(digest) == 64  # sha256, hex
    assert digest == hash_binding(SECRET)


def test_only_the_matching_secret_verifies() -> None:
    expected = hash_binding(SECRET)

    assert matches(secret=SECRET, expected=expected)
    assert not matches(secret=SECRET[:-1] + "H", expected=expected)
    assert not matches(secret="", expected=expected)
    # A callback with no cookie at all is a refusal, not a pass. This is the
    # one that a naive `if secret and ...` would get right and a naive
    # `if expected == ...` on an empty stored value would not.
    assert not matches(secret=None, expected=expected)


def test_a_flow_with_no_stored_binding_verifies_nothing() -> None:
    """Belt and braces on the store's own refusal to decode such a record.

    If an empty digest ever reached here it must not be satisfiable, or "no
    binding recorded" would silently mean "any browser will do".
    """
    assert not matches(secret=SECRET, expected="")
    assert not matches(secret=None, expected="")


# --- reading what the browser sent -------------------------------------------


def test_a_browser_with_a_cookie_keeps_it() -> None:
    """One secret per browser, not per flow: a second tab must not break the first."""
    settings = _settings("test")
    request = _request(f"{COOKIE_NAME}={SECRET}")

    assert presented(request, settings) == SECRET
    assert ensure(request, settings) == SECRET


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("short", id="too-short"),
        pytest.param("x" * 200, id="too-long"),
        pytest.param("has spaces in it and is forty three!!!!!!!!", id="wrong-alphabet"),
    ],
)
def test_a_malformed_cookie_is_treated_as_absent(value: str) -> None:
    """The cookie is caller-supplied, so it is shape-checked before it is hashed.

    Absent and malformed are one answer. Telling them apart would only help
    somebody guessing decide which half of their guess to fix - and refusing a
    megabyte of junk before it becomes a digest is the same argument
    `_STATE_SHAPE` makes next door.
    """
    settings = _settings("test")
    request = _request(f"{COOKIE_NAME}={value}")

    assert presented(request, settings) is None
    # And `ensure` mints rather than propagating the junk into a flow record.
    assert len(ensure(request, settings)) == 43


def test_a_cookie_under_the_other_environment_s_name_is_not_read() -> None:
    """The names are not interchangeable.

    A production deployment must not accept a binding presented under the
    non-prefixed name, because that name carries none of the guarantees
    `__Host-` is checked for.
    """
    production = _settings("staging")
    assert presented(_request(f"{COOKIE_NAME}={SECRET}"), production) is None
    assert presented(_request(f"{SECURE_COOKIE_NAME}={SECRET}"), production) == SECRET

    local = _settings("test")
    assert presented(_request(f"{SECURE_COOKIE_NAME}={SECRET}"), local) is None


# --- the cookie on the wire --------------------------------------------------


def test_the_production_cookie_carries_every_attribute_it_needs() -> None:
    """`__Host-`, `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, and no `Domain`.

    The prefix is the load-bearing part and the rest is what makes it valid: a
    browser refuses a `__Host-` cookie that is not secure, not path-root, or
    carries a domain, so getting any of them wrong means no cookie at all
    rather than a weaker one.
    """
    settings = _settings("staging")
    response = Response()

    attach(response, secret=SECRET, settings=settings)

    header = _set_cookie(response)
    assert header.startswith(f"{SECURE_COOKIE_NAME}={SECRET};")
    assert "Secure" in header
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.replace("SameSite=Lax", "SameSite=lax")
    assert "Path=/" in header
    # Host-only. A `Domain` attribute would let any sibling subdomain receive
    # it, and would make the `__Host-` prefix invalid into the bargain.
    assert "Domain=" not in header


def test_the_cookie_cannot_outlive_the_flow_it_binds() -> None:
    settings = _settings("staging")
    response = Response()

    attach(response, secret=SECRET, settings=settings)

    assert f"Max-Age={FLOW_TTL_SECONDS}" in _set_cookie(response)


def test_development_drops_the_prefix_rather_than_shipping_an_invalid_cookie() -> None:
    """Over plain HTTP a `__Host-` cookie is rejected outright, not weakened.

    Keeping the prefix locally would mean no cookie, and therefore no Google
    sign-in on a developer machine. The name changes; nothing else does -
    `HttpOnly` and `SameSite` still hold, and only `Secure` is dropped because
    there is no TLS to require.
    """
    settings = _settings("local")
    response = Response()

    attach(response, secret=SECRET, settings=settings)

    header = _set_cookie(response)
    assert header.startswith(f"{COOKIE_NAME}={SECRET};")
    assert "Secure" not in header
    assert "HttpOnly" in header


@pytest.mark.parametrize(
    ("environment", "secure"),
    [("local", False), ("test", False), ("staging", True), ("production", True)],
)
def test_which_environments_serve_a_secure_cookie(environment: str, secure: bool) -> None:
    """Every environment, named, so adding one cannot silently take the default.

    Staging is on the secure side deliberately: a staging deployment running a
    non-secure cookie would be exercising a different mechanism from the one
    production ships.
    """
    settings = _production() if environment == "production" else _settings(environment)

    assert uses_secure_cookies(settings) is secure
    assert cookie_name(settings) == (SECURE_COOKIE_NAME if secure else COOKIE_NAME)


def test_clearing_matches_the_cookie_it_is_meant_to_remove() -> None:
    """A deletion is matched on name, path and domain.

    A `delete_cookie` that disagreed with `attach` about any of them would look
    correct in a diff and leave the cookie in place in a browser.
    """
    settings = _settings("staging")
    response = Response()

    clear(response, settings)

    header = _set_cookie(response)
    assert header.startswith(f"{SECURE_COOKIE_NAME}=")
    assert "Path=/" in header
    assert "Domain=" not in header
    assert "Max-Age=0" in header
