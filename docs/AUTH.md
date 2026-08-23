# Authentication and Authorization

**Status: Implemented** — see [../TASKS.md](../TASKS.md) phase 2. Rate limiting on authentication endpoints remains Planned (phase 14).

Scope: identity, credentials, sessions, membership resolution, and permission checks.

## Passwords

Argon2id via `argon2-cffi`, in `app/core/security.py`. Hashes carry their own parameters, so a login detects a hash made with older cost settings and silently upgrades it. Plaintext credentials are never stored or logged; the log redactor drops any field whose name suggests a secret.

A login against an unknown address still spends the time a real verification would. Response time discloses whether an address is registered just as surely as a different error message would, so both are made uniform: every credential failure answers `401` with one message.

An account that exists but is disabled is only told so **after** its password is proven, which reveals nothing the caller did not already know.

## Tokens

| Token | Lifetime | Carries | Revocable |
| --- | --- | --- | --- |
| Access | `ACCESS_TOKEN_TTL_SECONDS` (15 min default) | subject, type, `jti`, active `tid` | No, by design |
| Refresh | `REFRESH_TOKEN_TTL_SECONDS` (14 days default) | subject, type, `jti` | Yes, in Redis |

Tokens are typed (`typ`), so a refresh token cannot be presented where an access token is required, and each carries a unique `jti`.

**Refresh tokens rotate, and spending one is atomic.** Presenting a token writes its identifier to a Redis denylist with `SET NX` — a single operation whose result says whether this caller was the first. A new pair is issued only to the winner, and the entry expires alongside the token it revokes, so the list cannot grow without bound.

The atomicity is the security property, not an optimisation (ADR-039). Checking a denylist and then writing to it is a race that both parties win: two requests carrying the same token both read "unspent" and both get a fresh pair, which is exactly what a stolen token used alongside the real one looks like. Losing the `SET NX` race *is* the detection.

**A replayed token tears the whole session estate down.** Rotation alone spends only the copy that is presented — usually the victim's, since the thief is the one racing — so the response to a replay is to raise `users.token_version`, which invalidates every access and refresh token the account holds. Both parties are signed out; the real person signs in again with a password the thief does not have. A `refresh_token_reused` audit entry is written and committed *before* the refusal is raised, because an exception would otherwise roll back the revocation that accompanies it. The caller learns only that the credentials are not valid: naming the teardown would tell a thief to move faster, and nothing on this path logs or records token material.

**Access tokens are deliberately not revocable.** They live for minutes, and checking a denylist on every request would surrender the whole benefit of stateless verification for very little. Immediate withdrawal of access is handled where it actually belongs — see below.

## Authorization model

Every protected operation answers five questions: who is the user, which membership applies, which tenant is active, which resource is targeted, and does the resource belong to that tenant. Permission scopes:

| Scope | Roles |
| --- | --- |
| Platform | `PLATFORM_OWNER`, `PLATFORM_ADMIN` |
| Tenant | `TENANT_OWNER`, `TENANT_ADMIN`, `MEMBER` |

Planned future tenant roles: `SALES`, `SUPPORT`, `MANAGER`. Platform and tenant scopes never share authorization dependencies: owning a workspace grants nothing across the platform, and platform staff hold no workspace membership implicitly.

Authorization is expressed as dependencies built by `require_tenant_roles(...)` and `require_platform_roles(...)` in `app/api/dependencies.py`, so a route cannot be written that forgets the check.

## Workspace context

The active workspace is read from the signed access token (`tid`) and from nowhere else — never a path, query, or body field. There is consequently no request field a caller could forge to aim a route at another workspace's data. `user.tenant_id` does not exist.

**Membership is reloaded on every request** rather than trusted from the token. This is the counterpart to non-revocable access tokens: removing somebody from a workspace takes effect on their next request instead of whenever their token happens to expire. The cost is one indexed lookup per request.

Switching workspace mints a new access token and leaves the refresh token untouched, so moving between workspaces never disturbs the long-lived credential. Identity never changes.

## Invitations

Invitation tokens are generated from `secrets` and stored **only** as a SHA-256 hash, so a stolen database yields no usable invitation. The raw token is visible exactly once, in the response to the administrator who issued it; when mail delivery lands it will go only to the invited address.

Acceptance is unauthenticated by necessity — the invited person may have no account yet — and the token in the body is the authorization. It creates the account when needed and always creates the membership, but **mints no session**: signing in stays a separate step, so a leaked invitation link cannot by itself produce a live session.

An administrator cannot invite an owner; only an owner can. Otherwise the boundary between the two roles would be decorative, since any admin could mint themselves a peer with full authority.

Unknown, spent, revoked, and expired invitations all answer identically, so the endpoint cannot be used to probe which tokens once existed.

## Enforcement points

1. Route dependency resolves the authenticated user from the bearer token.
2. Dependency resolves the workspace named by the token and re-verifies the membership and role.
3. Services receive an explicit tenant context; they never read one from input.
4. Repositories filter by `tenant_id`, applied in a single place (`TenantScopedRepository`).
5. Denials return consistent, non-revealing errors: cross-tenant access is `404`, never `403`.

## Testing

RBAC per role, cross-tenant access attempts, platform-versus-tenant boundaries, refresh rotation and replay, and invitation expiry and reuse are tested against a real PostgreSQL database in `tests/integration/test_authorization.py`. The HTTP surface and the role guards are tested separately with a stubbed service in `tests/integration/test_auth_endpoints.py`. Membership revocation is covered in `tests/integration/test_membership_revocation.py`, which walks the dependency graph and calls every workspace-scoped route with a revoked member's genuine token; refresh reuse in `tests/integration/test_refresh_reuse.py`. See [SECURITY.md](SECURITY.md).
