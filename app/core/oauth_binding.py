"""Tying an OAuth authorization to the browser that started it.

The state was already unguessable, single-use, ten-minute and server-side. All
of that proves *this server issued this state*. None of it proves *this browser
asked for it*, because the API is cookieless and there was nothing in the
callback that only the initiating browser could produce. So an attacker could
run a Google authorization on their own account, capture the resulting `code`
and `state`, and induce a victim's browser to post them - and the victim would
be signed in as the attacker, on the attacker's account, with whatever they
subsequently typed into it (SEC-07, CWE-352).

The fix is one value the initiating browser holds and nobody else can guess.
The browser gets a random secret in a cookie; the server-side flow record keeps
only its SHA-256. A callback must present a cookie that hashes to the value
stored beside the state, so completing somebody else's flow now needs their
state *and* their browser.

**What is on the browser is a secret and nothing else.** No Google token, no
PKCE verifier, no nonce, no user id, no tenant id, no session. It authorizes
nothing on its own: presented without a live state it is worth exactly nothing,
and every other route ignores it entirely. That matters for SEC-18 - this
application has no CSRF tokens because it authenticates by bearer token, and a
cookie that granted anything would have invalidated that reasoning. This one
does not.

**Why the hash and not the value.** Redis holds in-flight OAuth flows, and a
reader of that keyspace should not come away able to complete them. SHA-256
rather than a slow hash for `hash_invitation_token`'s reason: the input is 256
bits of randomness, so there is nothing to brute-force and a deliberately slow
hash would only slow the callback down.

**One secret per browser, not per flow.** A fresh value on every `authorize`
would mean the second tab silently broke the first - open Google sign-in twice
and whichever you finish second fails. Reusing a well-formed cookie makes
concurrent flows in one browser work, at the cost of the secret outliving a
single flow. It is `HttpOnly`, host-only and expires with the flow window, and
anybody who can read it can read the session it protects, so the trade is
comfortably worth making.

**Cleared after a successful callback, and never after a failed one.** Success
means the secret has done its job. Failure is the interesting case: clearing
there would let anybody who can induce one forged callback destroy a legitimate
flow in the victim's browser, which turns the defence into a denial of service
against the person it defends. So a refusal changes nothing, and the cookie
expires on its own.

**`SameSite=Lax` is defence in depth here, not the mechanism.** The binding
holds whatever a browser does with the cookie, because the security is in the
secret. It does mean the frontend and the API must be same-site - subdomains of
one registrable domain, which is the topology `GOOGLE_REDIRECT_URI` already
assumes - since Lax withholds the cookie from a genuinely cross-site request.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Final

from fastapi import Request, Response

from app.core.config import Settings
from app.core.oauth_flow import FLOW_TTL_SECONDS

# 32 bytes, url-safe base64 encoded: 43 characters and 256 bits of entropy, the
# same sizing as the state and the PKCE verifier next door.
BINDING_BYTES: Final = 32

# The name in production, where `__Host-` is enforceable. The prefix is a
# promise a browser checks rather than a convention we keep: it refuses the
# cookie unless it is `Secure`, has `Path=/` and carries no `Domain`, which is
# exactly the host-only, HTTPS-only cookie this needs to be. A sibling
# subdomain therefore cannot set one on our behalf.
SECURE_COOKIE_NAME: Final = "__Host-wasla_oauth"
# The name everywhere else. A `__Host-` cookie without `Secure` is rejected
# outright by browsers, so keeping the prefix over plain HTTP would not be a
# weaker cookie - it would be no cookie at all, and Google sign-in would not
# work locally. The name changes; nothing else about the value does.
COOKIE_NAME: Final = "wasla_oauth"

# Environments that terminate TLS. `staging` is included deliberately: a
# staging deployment that ran with a non-secure cookie would be testing a
# different mechanism from the one production uses.
_SECURE_ENVIRONMENTS: Final = frozenset({"staging", "production"})

# Exactly what `token_urlsafe(BINDING_BYTES)` produces. Checked before the value
# is hashed, because the cookie is caller-supplied: this accepts everything this
# module issues and refuses a megabyte of junk becoming a digest. A cookie that
# fails this is treated as absent, which is the same answer as a cookie that
# never arrived.
_SHAPE: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")


def cookie_name(settings: Settings) -> str:
    """The name this deployment sets and reads."""
    return SECURE_COOKIE_NAME if uses_secure_cookies(settings) else COOKIE_NAME


def uses_secure_cookies(settings: Settings) -> bool:
    """Whether this deployment serves over TLS and can carry a `__Host-` cookie."""
    return settings.environment in _SECURE_ENVIRONMENTS


def hash_binding(secret: str) -> str:
    """The value stored beside the state. Never the secret itself."""
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def presented(request: Request, settings: Settings) -> str | None:
    """The binding secret this request carries, if it carries a usable one.

    `None` covers absent, malformed and wrong-length alike. They are the same
    answer to the caller, and distinguishing them would only tell somebody
    guessing which half of their guess to fix.
    """
    value = request.cookies.get(cookie_name(settings))
    if value is None or not _SHAPE.match(value):
        return None
    return value


def ensure(request: Request, settings: Settings) -> str:
    """The secret this browser will present at the callback.

    Reuses the one it already has when there is one, so a second tab starting
    its own authorization does not invalidate the first. Mints a fresh one
    otherwise.
    """
    return presented(request, settings) or secrets.token_urlsafe(BINDING_BYTES)


def matches(*, secret: str | None, expected: str) -> bool:
    """Whether this browser is the one that started the flow.

    `compare_digest` because the comparison is against a stored digest and a
    byte-at-a-time equality check over enough attempts is a practical oracle -
    the same reasoning `verify_callback` gives for the Paymob HMAC.
    """
    if secret is None:
        return False
    return hmac.compare_digest(hash_binding(secret), expected)


def attach(response: Response, *, secret: str, settings: Settings) -> None:
    """Set or refresh the binding cookie on an `authorize` response.

    `max_age` is the flow window, so the cookie cannot outlive the longest
    thing it can be used for. Refreshing it on every initiation is what keeps a
    browser that starts a second flow late in the window from losing the first.
    """
    secure = uses_secure_cookies(settings)
    response.set_cookie(
        cookie_name(settings),
        secret,
        max_age=FLOW_TTL_SECONDS,
        # No `domain`: host-only, which `__Host-` requires and which keeps a
        # neighbouring subdomain from receiving it.
        path="/",
        secure=secure,
        # Unreadable from script. The value is only ever compared server-side,
        # so there is nothing a frontend could legitimately do with it, and
        # `HttpOnly` removes an XSS on any page of this origin as a way to
        # steal it.
        httponly=True,
        # See the module docstring: this is depth, not the mechanism. `Strict`
        # would be no stronger here and would break the ordinary case of
        # arriving at the frontend by following Google's redirect.
        samesite="lax",
    )


def clear(response: Response, settings: Settings) -> None:
    """Remove the binding cookie after a callback that consumed it.

    Only after a *successful* one. See the module docstring: clearing on a
    refusal would hand an attacker who can induce one forged callback the
    ability to destroy a legitimate in-flight flow.

    The attributes are repeated because a browser matches a deletion against
    name, path and domain; a `delete_cookie` that disagreed with `attach` about
    any of them would leave the cookie in place.
    """
    response.delete_cookie(
        cookie_name(settings),
        path="/",
        secure=uses_secure_cookies(settings),
        httponly=True,
        samesite="lax",
    )
