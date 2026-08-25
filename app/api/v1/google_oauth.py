"""Google sign-in endpoints.

Five routes, and two shapes worth explaining before the code.

**Initiation is a POST, not a GET.** It writes server state - a single-use flow
record - and a GET that writes state is one a browser or a link preview will
happily fetch on its own, filling Redis with flows nobody started.

**The callback is a POST from the frontend, not a GET from Google.** This is a
deliberate deviation from the brief's `GET` callback and it is recorded in
ADR-043. The reason is that this API is cookieless: it returns tokens in
response bodies. A `GET` callback reached by top-level navigation would render a
document containing a refresh token - unreadable by the SPA that needs it, and
visible to anything that can see the page. So Google redirects to a frontend
route, which posts the `code` and `state` here. The code is still exchanged
server-side with the client secret and the PKCE verifier, the frontend never
sees a Google token, and `GOOGLE_REDIRECT_URI` is still fixed configuration
that Google exact-matches.

The residual gap, disclosed rather than papered over: without a cookie the
state is unpredictable, single-use, short-lived and server-side, but it cannot
prove the browser finishing the flow is the one that started it. For linking the
binding is strong anyway, because the flow record holds the initiating account.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.dependencies import CurrentUserDep
from app.api.rate_limits import GoogleOAuthRateLimit
from app.api.route import CommittingRoute

# Private by name, shared on purpose. Section 14 requires a Google session and a
# password session to be the same thing; building the body with the same helper
# makes that a property of the code rather than a promise, because the two
# cannot drift into different shapes.
from app.api.v1.auth import _session_response
from app.core.dependencies import RedisDep, SessionDep, SettingsDep
from app.core.exceptions import NotFoundError
from app.core.oauth_flow import OAuthFlowStore
from app.core.rate_limit import RateLimiter
from app.core.token_store import RefreshTokenStore
from app.integrations.google.client import GoogleOAuthClient
from app.integrations.google.oidc import GoogleIdTokenVerifier, GoogleKeyRing
from app.schemas.auth import SessionResponse
from app.schemas.google_oauth import (
    GoogleAuthorizationResponse,
    GoogleCallbackRequest,
    GoogleIdentityResponse,
)
from app.services.auth_service import AuthService
from app.services.google_auth_service import GoogleAuthService

router = APIRouter(route_class=CommittingRoute, prefix="/auth", tags=["Authentication"])

# One key ring for the process, and this is not an optimisation. Built per
# request it would fetch Google's key document on every single login, which is a
# cache that costs a round trip and saves nothing - and it would put this
# deployment's request rate straight onto Google's endpoint.
_KEY_RING = GoogleKeyRing()


def _service(settings: SettingsDep, session: SessionDep, redis: RedisDep) -> GoogleAuthService:
    """Assemble the service, or refuse to exist.

    A deployment that has not configured Google answers 404, not 503. A feature
    nobody enabled is not temporarily unwell; it is not here. 503 would also
    tell an unauthenticated caller that the feature exists and is broken, which
    is a small piece of reconnaissance for nothing in return.

    The re-check of each value is not redundant with `Settings` validation. That
    validation is skipped under `is_testing`, so this is the guard that holds in
    the environment where it is easiest to misconfigure - and it is what proves
    to the type checker that these are not `None`.
    """
    client_id = settings.google_client_id
    client_secret = settings.google_client_secret
    redirect_uri = settings.google_redirect_uri
    if not settings.google_enabled or not client_id or not client_secret or not redirect_uri:
        raise NotFoundError("Google sign-in is not available.")

    return GoogleAuthService(
        session=session,
        settings=settings,
        flows=OAuthFlowStore(redis),
        client=GoogleOAuthClient(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        ),
        verifier=GoogleIdTokenVerifier(client_id=client_id, key_ring=_KEY_RING),
        auth=AuthService(
            session=session,
            settings=settings,
            token_store=RefreshTokenStore(redis),
            limiter=RateLimiter(redis),
        ),
    )


def _identity_response(identity_provider: str, connected_at: object, last: object) -> None:
    """Placeholder removed at review; see `_as_identity` below."""


@router.post("/google/authorize", response_model=GoogleAuthorizationResponse)
async def start_google_login(
    settings: SettingsDep,
    session: SessionDep,
    redis: RedisDep,
    _limit: GoogleOAuthRateLimit,
) -> GoogleAuthorizationResponse:
    """Begin signing in with Google.

    Unauthenticated by design - this is how somebody with no account starts.
    Limited by client address, because there is no other identity to count.
    """
    url, expires_in = await _service(settings, session, redis).start_login()
    return GoogleAuthorizationResponse(authorization_url=url, expires_in=expires_in)


@router.post("/google/callback", response_model=SessionResponse)
async def complete_google_login(
    payload: GoogleCallbackRequest,
    settings: SettingsDep,
    session: SessionDep,
    redis: RedisDep,
    _limit: GoogleOAuthRateLimit,
) -> SessionResponse:
    """Finish signing in with Google.

    Answers exactly what `/auth/login` answers, from the same helper. A caller
    cannot tell from the response which method opened the session, and nothing
    downstream can either.

    Errors: 401 for any failed authorization - bad, replayed or expired state,
    refused code, forged token, wrong nonce; 403 for a disabled account; 409
    when the verified Google address already has a Wasla account; 503 when
    Google or Redis cannot be reached; 404 when the feature is not configured.
    """
    result = await _service(settings, session, redis).complete_login(
        code=payload.code,
        state=payload.state,
    )
    return _session_response(result)


@router.post("/identities/google/authorize", response_model=GoogleAuthorizationResponse)
async def start_google_link(
    current: CurrentUserDep,
    settings: SettingsDep,
    session: SessionDep,
    redis: RedisDep,
    _limit: GoogleOAuthRateLimit,
) -> GoogleAuthorizationResponse:
    """Begin connecting Google to the account you are signed in as.

    Authenticated, and that is the whole point of having a second initiation
    endpoint: the account is written into the flow record here, where a caller
    cannot influence it, rather than being inferred later from the token.
    """
    url, expires_in = await _service(settings, session, redis).start_link(user=current.user)
    return GoogleAuthorizationResponse(authorization_url=url, expires_in=expires_in)


@router.post("/identities/google/link", response_model=GoogleIdentityResponse)
async def link_google_identity(
    payload: GoogleCallbackRequest,
    current: CurrentUserDep,
    settings: SettingsDep,
    session: SessionDep,
    redis: RedisDep,
    _limit: GoogleOAuthRateLimit,
) -> GoogleIdentityResponse:
    """Attach a verified Google account to the current account.

    Errors: 401 for a failed authorization or a flow started by a different
    account; 409 when this Google account is already connected somewhere, or
    when this account already has one.
    """
    identity = await _service(settings, session, redis).complete_link(
        user=current.user,
        code=payload.code,
        state=payload.state,
    )
    return GoogleIdentityResponse(
        provider=identity.provider,
        connected_at=identity.created_at,
        last_login_at=identity.last_login_at,
    )


@router.delete("/identities/google", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_google_identity(
    current: CurrentUserDep,
    settings: SettingsDep,
    session: SessionDep,
    redis: RedisDep,
    _limit: GoogleOAuthRateLimit,
) -> None:
    """Disconnect Google from the current account.

    Errors: 404 when nothing is connected; 403 when disconnecting would leave
    the account with no way to sign in - no password and no other identity.
    """
    await _service(settings, session, redis).unlink(user=current.user)
