"""Talking to Google: the authorization URL, and the code exchange.

Two fixed endpoints, both literals. No discovery document is fetched: it would
be a third network dependency whose only output is two URLs that have not
changed in a decade, and it would put the key document's location under the
control of whatever the discovery response happened to say.

Both requests go through `build_guarded_client`, which resolves the host,
refuses any answer that is not globally routable, and pins the connection to
the address it judged while preserving the `Host` header and SNI so certificate
verification still binds to the name. That is what closes DNS rebinding, which
`app/core/net.py` records as having been reproduced against an earlier version
of itself.
"""

from __future__ import annotations

import json
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from app.core.logging import get_logger
from app.core.net import UnsafeUrlError, build_guarded_client

logger = get_logger(__name__)

GOOGLE_AUTHORIZATION_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
# `noqa: S105`: the name contains "token" and the value is a URL, not a secret.
GOOGLE_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105

# Exactly what is needed to identify somebody, and nothing else. `openid` asks
# for an ID token at all; `email` and `profile` fill in the claims this
# application reads. A scope that is not requested is a scope that cannot be
# misused later by code that finds it already granted.
SCOPES: Final = ("openid", "email", "profile")

TOKEN_TIMEOUT_SECONDS: Final = 10.0
MAX_TOKEN_RESPONSE_BYTES: Final = 64 * 1024
# An authorization code is a couple of hundred characters. The cap exists so
# that a caller cannot make this process relay a megabyte to Google on their
# behalf: the request is refused here rather than forwarded.
MAX_CODE_LENGTH: Final = 2_048


class GoogleExchangeFailed(Exception):
    """The authorization code could not be exchanged for an ID token.

    Covers a refusal by Google, a network failure, a timeout, and a response
    that did not contain what it must. They are one class on purpose: the caller
    is told the same thing in every case, because which of them happened is
    information about our configuration and about their own code's validity.

    Deliberately not a `WaslaError`, so it cannot become an HTTP response by
    accident - the same reasoning `app/core/net.py` gives for `UnsafeUrlError`.
    """


class GoogleOAuthClient:
    """The two HTTP conversations this feature has with Google."""

    def __init__(self, *, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def authorization_url(self, *, state: str, nonce: str, challenge: str) -> str:
        """Where to send the browser.

        `access_type=online` is the load-bearing parameter and the reason this
        application can promise it never stores a Google refresh token: with it,
        Google does not issue one. "Do not store the refresh token" stops being
        a rule somebody has to remember and becomes a thing that cannot happen.

        `prompt=select_account` costs a click and buys two things. Somebody with
        several Google accounts sees which one they are about to use, and a
        linking attempt cannot silently reuse whichever session the browser
        happened to be holding.

        `redirect_uri` is configuration. It is never assembled from request
        input, which is why open redirection is not something this method has to
        defend against.
        """
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{GOOGLE_AUTHORIZATION_URL}?{urlencode(params)}"

    async def exchange(self, *, code: str, code_verifier: str) -> str:
        """Trade an authorization code for an ID token, and return only that.

        The access token in Google's response is deliberately dropped on the
        floor. Nothing in this application calls a Google API on a user's
        behalf, so keeping it would be holding a credential with no purpose -
        and the ID token already carries every claim that is needed, inside a
        signature, which an access token does not.

        :raises GoogleExchangeFailed: for every failure, with no detail.
        """
        if not code or len(code) > MAX_CODE_LENGTH:
            raise GoogleExchangeFailed("The authorization code was not usable.")

        form = {
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }

        payload = await self._post_token_request(form)

        id_token = payload.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            # A 200 with no ID token. Either the scopes were changed without
            # this code being updated, or something is answering for Google.
            logger.warning(
                "google.token_response_without_id_token",
                extra={"event": "google.token_response_without_id_token"},
            )
            raise GoogleExchangeFailed("Google did not return an identity token.")
        return id_token

    async def _post_token_request(self, form: dict[str, str]) -> dict[str, Any]:
        """POST the exchange and read a bounded response.

        Nothing from `form` and nothing from the response body is ever logged.
        Between them they contain the client secret, the authorization code and
        the ID token, and a log line is the easiest place in a system to leak a
        credential to somebody who was only supposed to be able to read logs.
        """
        body = bytearray()
        try:
            async with (
                build_guarded_client(timeout=httpx.Timeout(TOKEN_TIMEOUT_SECONDS)) as client,
                client.stream("POST", GOOGLE_TOKEN_URL, data=form) as response,
            ):
                if response.status_code >= httpx.codes.BAD_REQUEST:
                    # Google's error body is never read, never logged and never
                    # returned. It is provider internals, it sometimes echoes
                    # the request that produced it, and a caller who can see it
                    # learns about this deployment's configuration.
                    logger.warning(
                        "google.token_exchange_refused",
                        extra={
                            "event": "google.token_exchange_refused",
                            "status_code": response.status_code,
                        },
                    )
                    raise GoogleExchangeFailed("Google refused the authorization code.")

                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_TOKEN_RESPONSE_BYTES:
                        raise GoogleExchangeFailed("Google's response was too large.")
        except GoogleExchangeFailed:
            raise
        except (httpx.HTTPError, UnsafeUrlError) as exc:
            # Includes the timeout, and includes a resolver answering with a
            # private address, which the guarded transport refuses outright.
            logger.warning(
                "google.token_exchange_unreachable",
                extra={
                    "event": "google.token_exchange_unreachable",
                    "reason": type(exc).__name__,
                },
            )
            raise GoogleExchangeFailed("Google could not be reached.") from exc

        try:
            payload: Any = json.loads(bytes(body))
        except json.JSONDecodeError as exc:
            raise GoogleExchangeFailed("Google's response was not JSON.") from exc
        if not isinstance(payload, dict):
            raise GoogleExchangeFailed("Google's response was not an object.")
        return payload
