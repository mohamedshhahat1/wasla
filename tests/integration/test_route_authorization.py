"""Every route's guards, read from the dependency graph rather than the source.

The authorization matrix in `docs/AUTHORIZATION.md` has been wrong twice, both
times the same way: it was written by hand, a later phase added a route, and
nobody went back. The password-reset routes went unlisted for one release and
the two provider webhooks for two. Neither was a vulnerability — every one of
them is signature-verified or rate-limited — but an inaccurate matrix is a
security problem of its own, because it is the thing a reviewer trusts instead
of reading 119 routes.

So this file is the matrix. It resolves each route's `dependant` tree, which is
what actually executes, and fails when the code and the documented set diverge.

Reading decorators would not do. A guard can sit on the router rather than the
route, an included router defers behind `_IncludedRouter`, and a dependency can
pull in others — so the only honest question is "what does FastAPI resolve for
this path", and that is what is asked here.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.routing import APIRoute, _IncludedRouter

from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration

# The two dependencies that establish who is calling. `get_current_user`
# authenticates; `get_active_workspace` additionally resolves an active
# membership. A route resolving neither is open to the internet.
AUTHENTICATING = frozenset({"get_current_user", "get_active_workspace"})

# Every route that is deliberately open, with the reason it is. Adding a route
# here is the deliberate act; forgetting to is what this test catches.
OPEN_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/auth/register"): "self-service signup",
    ("POST", "/auth/login"): "credentials are what it establishes",
    ("POST", "/auth/refresh"): "the refresh token is the credential",
    ("POST", "/auth/logout"): "revoking a token you hold needs no second credential",
    ("POST", "/auth/password-reset/request"): "the caller cannot sign in",
    ("POST", "/auth/password-reset/confirm"): "the emailed token is the authorization",
    # The two halves of a Google sign-in. Both must be reachable by somebody
    # who has no account at all - which is the point of them - so neither can
    # authenticate. Each carries `GoogleOAuthRateLimit`, counted by client
    # address, and the callback additionally refuses anything but a
    # server-verified Google signature over a single-use state (ADR-051).
    ("POST", "/auth/google/authorize"): "somebody with no account starts here",
    ("POST", "/auth/google/callback"): "the signed Google ID token is the authorization",
    ("POST", "/invitations/accept"): "the invitee may have no account yet",
    ("GET", "/webhooks/whatsapp"): "Meta's subscription challenge",
    ("POST", "/webhooks/whatsapp"): "Meta cannot hold a credential of ours",
    ("POST", "/webhooks/paymob"): "a payment processor cannot hold a credential of ours",
    ("POST", "/webhooks/email"): "Resend cannot hold a credential of ours",
    ("GET", "/health"): "a load balancer has no credential",
    ("GET", "/health/live"): "a load balancer has no credential",
    ("GET", "/health/ready"): "a load balancer has no credential",
    # A scraper has no credential either, and giving it one would mean every
    # scraper in a deployment holding the same shared token to read a document
    # that carries no customer data by construction. What keeps it off the
    # internet is topology rather than authentication: the API container
    # publishes no port, and `nginx.conf` answers 404 for this path on the
    # public listener rather than proxying it (ADR-070). `METRICS_ENABLED=false`
    # removes it entirely.
    ("GET", "/metrics"): "a metrics scraper has no credential; nginx refuses the path",
}

# Authenticated routes that deliberately stop at `get_current_user` rather than
# resolving a workspace. Each is about the *account* rather than about work
# inside a workspace, so requiring an active membership would be wrong: a user
# with no workspace at all must still be able to read themselves and create
# one, and platform staff act across every workspace by definition.
ACCOUNT_OR_PLATFORM: frozenset[str] = frozenset(
    {
        "/auth/me",
        "/auth/workspace",
        "/auth/logout-all",
        "/auth/password",
        # Choosing a first password is about the account, not about work inside
        # a workspace - and the accounts that need it are Google-first ones,
        # which may hold no membership at all.
        "/auth/password/set",
        "/auth/email/verification/send",
        "/auth/email/verification/verify",
        # Connecting and disconnecting Google is about the account, not about
        # work inside a workspace: a user with no membership at all must still
        # be able to attach an identity, and a Google-only account created by
        # first login is exactly such a user.
        "/auth/identities/google/authorize",
        "/auth/identities/google/link",
        "/auth/identities/google",
    }
)


def _routes(routes: Sequence[Any]) -> Iterator[APIRoute]:
    """Every `APIRoute`, descending through deferred inclusion.

    `_IncludedRouter` is how FastAPI defers `include_router`, and a naive pass
    over `app.routes` misses everything mounted through one - which here is
    most of the application, every webhook included.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        elif isinstance(route, _IncludedRouter):
            yield from _routes(route.original_router.routes)
        elif hasattr(route, "routes"):
            yield from _routes(route.routes)


def _dependencies(dependant: Any, seen: set[str] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    for sub in dependant.dependencies:
        call = getattr(sub, "call", None)
        if call is not None:
            seen.add(getattr(call, "__name__", type(call).__name__))
        _dependencies(sub, seen)
    return seen


@pytest.fixture(scope="module")
def graph() -> dict[tuple[str, str], set[str]]:
    """Every (method, path) mapped to the dependencies FastAPI resolves."""
    app = create_app(Settings(_env_file=None, environment="test"))
    resolved: dict[tuple[str, str], set[str]] = {}
    for route in _routes(app.routes):
        names = _dependencies(route.dependant)
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}:
            resolved[(method, route.path)] = names
    return resolved


def _open(graph: dict[tuple[str, str], set[str]]) -> set[tuple[str, str]]:
    return {key for key, names in graph.items() if not (names & AUTHENTICATING)}


def test_the_open_routes_are_exactly_the_ones_we_meant(
    graph: dict[tuple[str, str], set[str]],
) -> None:
    """The test this file exists for.

    A new route that forgets its guard is open to the internet, and nothing
    else in the suite would say so - its own tests would pass, because they
    authenticate. This fails instead, and names the route.
    """
    unexpected = _open(graph) - set(OPEN_ROUTES)
    assert not unexpected, (
        "these routes resolve no authentication and are not on the documented "
        f"open list: {sorted(unexpected)}"
    )


def test_no_documented_open_route_has_quietly_been_closed(
    graph: dict[tuple[str, str], set[str]],
) -> None:
    """The other direction, which keeps the list honest rather than merely safe.

    A stale entry here would let a genuinely open route be added later under a
    name already on the list, and would also mean the documented matrix
    overstates the attack surface. Both are worth knowing.
    """
    stale = set(OPEN_ROUTES) - _open(graph)
    assert not stale, f"documented as open but now authenticated: {sorted(stale)}"


def test_the_documented_count_matches_the_graph(graph: dict[tuple[str, str], set[str]]) -> None:
    """`docs/AUTHORIZATION.md` states a number. This is that number."""
    assert len(_open(graph)) == 17


def test_every_other_route_resolves_a_workspace_or_is_an_account_route(
    graph: dict[tuple[str, str], set[str]],
) -> None:
    """The structural guard against the commonest authorization mistake.

    A route that resolves `get_current_user` alone is authenticated but not
    *authorized*: it knows who is calling and nothing about which workspace
    they may act in. That is correct for account and platform routes and wrong
    for everything else, and the difference is invisible when reading a single
    handler.
    """
    offenders = sorted(
        (method, path)
        for (method, path), names in graph.items()
        if (method, path) not in OPEN_ROUTES
        and "get_active_workspace" not in names
        and path not in ACCOUNT_OR_PLATFORM
        and not path.startswith("/platform/")
    )
    assert not offenders, (
        "these routes authenticate but never resolve an active workspace, so "
        f"nothing binds them to a tenant: {offenders}"
    )


def test_every_platform_route_is_guarded_by_a_platform_role(
    graph: dict[tuple[str, str], set[str]],
) -> None:
    """Platform authority is separate from workspace authority.

    A `/platform/` route resolving only `get_current_user` would be reachable
    by any signed-in customer. The guard is a role dependency, so its presence
    is what is asserted - the roles it admits are covered by
    `test_tenant_isolation.py`.
    """
    unguarded = sorted(
        (method, path)
        for (method, path), names in graph.items()
        if path.startswith("/platform/") and "guard" not in names
    )
    assert not unguarded, f"platform routes without a role guard: {unguarded}"


def test_no_open_route_reaches_a_workspace_service(graph: dict[tuple[str, str], set[str]]) -> None:
    """An open route must not be able to act inside a tenant at all.

    The webhooks resolve a workspace *after* verifying a signature, from an
    identifier they were given - which is a different thing from a dependency
    handing them one, and is why none of them may depend on
    `get_active_workspace`.
    """
    for key in OPEN_ROUTES:
        names = graph.get(key, set())
        assert "get_active_workspace" not in names, f"{key} resolves a workspace while open"
        assert "get_current_user" not in names, f"{key} resolves a user while open"
