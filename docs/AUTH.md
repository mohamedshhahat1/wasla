# Authentication and Authorization

**Status: Implemented** — see [../TASKS.md](../TASKS.md) phase 2, and phase 14 for the rate limiting on these endpoints (ADR-032): per client address and, for login, per account as well.

Scope: identity, credentials, sessions, membership resolution, and permission checks.

## Passwords

Argon2id via `argon2-cffi`, in `app/core/security.py`. Hashes carry their own parameters, so a login detects a hash made with older cost settings and silently upgrades it. Plaintext credentials are never stored or logged; the log redactor drops any field whose name suggests a secret.

A login against an unknown address still spends the time a real verification would. Response time discloses whether an address is registered just as surely as a different error message would, so both are made uniform: every credential failure answers `401` with one message.

An account that exists but is disabled is only told so **after** its password is proven, which reveals nothing the caller did not already know. Soft-deleted accounts are excluded by ordinary repository lookups and every session-issuance path checks lifecycle state again. Platform deletion atomically writes the tombstone, disables the account, and increments `token_version`; explicit `*_including_deleted` methods are reserved for collision checks and lifecycle tooling.

## Signing in with Google

A second way to open a session, and the *only* other one: `AuthService.authenticate_federated` is the sole public entry to token issuance besides `login`. It takes a `User` rather than an address, so there is no argument a caller could supply that is a claim from a stranger, and everything that makes a session a session happens inside it — the account-status check, the workspace resolution, the current `token_version`, the same claims and the same rotation. A federated session is indistinguishable from a password session downstream, which is exactly what keeps every authorization dependency, tenant isolation check and membership rule below applicable without any of them being taught that Google exists. Anything that had to be taught would be a place this could bypass it.

An account created by Google login has **no password hash**, and needs no new code to be safe: `login` already refuses an account whose hash is `None`, in the same branch and after the same delay as an address that does not exist. Such an account also has no workspace until it is invited to one — `register` needs a name and a slug that Google does not supply.

The account's `full_name` and `avatar_url` follow Google on every login; its `email` is written once, at enrolment, and never refreshed. The design, the threat it closes, and the five ADRs behind it are in [GOOGLE_OAUTH.md](GOOGLE_OAUTH.md).

## Tokens

| Token | Lifetime | Carries | Revocable |
| --- | --- | --- | --- |
| Access | `ACCESS_TOKEN_TTL_SECONDS` (15 min default) | subject, type, `jti`, active `tid`, `ver` | Not individually; immediately in bulk through `token_version` |
| Refresh | `REFRESH_TOKEN_TTL_SECONDS` (14 days default) | subject, type, `jti` | Yes, in Redis |

Tokens are typed (`typ`), so a refresh token cannot be presented where an access token is required, and each carries a unique `jti`.

**Refresh tokens rotate, and spending one is atomic.** Presenting a token writes its identifier to a Redis denylist with `SET NX` — a single operation whose result says whether this caller was the first. A new pair is issued only to the winner, and the entry expires alongside the token it revokes, so the list cannot grow without bound.

The atomicity is the security property, not an optimisation (ADR-039). Checking a denylist and then writing to it is a race that both parties win: two requests carrying the same token both read "unspent" and both get a fresh pair, which is exactly what a stolen token used alongside the real one looks like. Losing the `SET NX` race *is* the detection.

**A replayed token tears the whole session estate down.** Rotation alone spends only the copy that is presented — usually the victim's, since the thief is the one racing — so the response to a replay is to raise `users.token_version`, which invalidates every access and refresh token the account holds. Both parties are signed out; the real person signs in again with a password the thief does not have. A `refresh_token_reused` audit entry is written and committed *before* the refusal is raised, because an exception would otherwise roll back the revocation that accompanies it. The caller learns only that the credentials are not valid: naming the teardown would tell a thief to move faster, and nothing on this path logs or records token material.

**Access tokens are deliberately not individually denylisted.** `POST /auth/logout` spends the supplied refresh token, but its already-issued access token can remain usable until its 15-minute maximum lifetime. `logout-all`, password reset, disable, delete, and refresh-replay teardown increment `token_version`, so their access tokens stop on the next request. Clients must clear both tokens after logout.

## Verification onboarding gate

Registration and password login may establish a limited session before inbox verification. `require_verified_user` centrally gates workspace selection, every workspace-scoped business API, and platform administration. `/auth/me`, refresh, logout, password recovery/change, verification itself, and Google identity recovery/linking remain reachable. A denied business request is `403 email_verification_required`. Google's signed `email_verified` claim satisfies the gate only for Gmail or a matching hosted Workspace domain; third-party addresses remain limited until they complete Wasla's code flow.

The dependency-graph test inventories every FastAPI route and fails if a future authenticated material route has no verification classification.

### Account lifecycle matrix

`limited` means account/recovery endpoints remain available but material
workspace/platform operations are refused. `no` means the lifecycle guard
refuses the identity regardless of credentials.

| State | Password / Google login | Access / refresh | Reset / verify | Invite acceptance | Workspace switch / business action |
| --- | --- | --- | --- | --- | --- |
| Active, verified | yes / yes when linked | yes / yes | reset yes / verify idempotent | yes | yes, subject to membership/tenant state |
| Active, unverified | yes / Google proof verifies | limited / yes | yes / yes | yes | no until verified |
| Disabled | no / no | no / no | neutral no-op / no | existing identity refused | no |
| Soft deleted | no / no; tombstone not reused | no / no | neutral no-op / no | refused | no |
| Passwordless Google | password no / Google yes | limited until proof, then yes | reset no / verify yes | yes | membership and verification required |
| Invited new account | password set by acceptance / unlinked no | no session minted | after login | one atomic winner | verification and membership required |
| Membership revoked | account login yes | account session yes | yes / yes | a new valid invite may reinstate | revoked workspace denied |
| Tenant suspended | account login yes | account session yes | yes / yes | account-level acceptance may succeed | that tenant's business actions denied (403) |
| Tenant deleted | account login yes | account session yes | yes / yes | account-level acceptance may succeed | that tenant's routes not found (404 — membership withdrawn) |
| Self-deleted | no / no; tombstone not reused | no / no | neutral no-op / no | refused | no |

## Closing an account

`DELETE /auth/me` closes the caller's own account. It is the same resource
`GET /auth/me` describes, and it names no target: the authenticated caller is
always the one deleted, which is what keeps it from being a global user-deletion
endpoint that happens to have a guard in front of it today.

**Proof beyond the session is required, and either kind will do.**

*The current password*, for an account that has one, verified the way a login is.

*A Google re-authentication proof*, for an account with a linked Google identity.
The person goes back through Google, the callback checks the returned `sub`
against the identity already on the account, and leaves a short-lived single-use
token that `DELETE /auth/me` spends. See [GOOGLE_OAUTH.md](GOOGLE_OAUTH.md).

This replaces the earlier rule, which was "set a password first". That was
secure and was a poor thing to ask of somebody who is leaving — it made them
acquire a credential in order to discard one.

**Either, not both.** Requiring both from an account that has both would be
step-up MFA, a product decision nobody has made, and would make the more
securely configured account the harder one to close.

Requiring "recent authentication" on its own is deliberately not accepted: an
access token is at most `ACCESS_TOKEN_TTL` old by construction, so requiring
recency of one asserts something already true, while a refresh token mints fresh
ones for a fortnight.

**Ownership is resolved before deletion, never after.** The account cannot close
while it is the last active owner of a live workspace; the refusal carries those
workspaces so the person can transfer ownership or delete them. Workspaces
already tombstoned do not count, so somebody who has wound their business down
is not trapped.

**What deletion does.** In one unit of work: every membership withdrawn (revoked
rather than deleted, so the trail keeps who left and when), `deleted_at` set,
`is_active` cleared, `token_version` raised. Every access and refresh token dies
with the bump; password login finds no live row; Google login resolves the
identity, sees `deleted_at`, and refuses.

**What it deliberately does not do.** It does not release the address, does not
delete the federated identity, does not touch the audit trail, and does not
reach any other account. The `SET NULL` foreign keys on `analytics_events`,
`audit_logs`, `conversations`, `leads`, `messages` and the rest mean history
keeps its shape and simply stops naming the person; `audit_logs.actor_label`
holds their address as a copy, which is the entire reason it is a copy.

### The tombstone policy, stated

Unchanged by this work and written down so it is not changed by accident:

- **The email address is reserved for ever.** Re-registration is refused, and so
  is a Google first-login that resolves to it. Releasing it would let a stranger
  inherit whatever an old invitation, a colleague's memory or a support ticket
  still associates with the address.
- **The federated identity is kept, not deleted.** Deleting it would free the
  Google `sub` while the address stayed reserved — the next sign-in would try to
  create an account the unique constraint refuses, which is a `500` where a
  clean refusal belongs.
- **There is no undelete.** Not by the person, not by platform staff. Reversing
  one is a database operation, in [RUNBOOK.md](RUNBOOK.md).

Platform staff close somebody else's account with
`DELETE /platform/users/{user_id}`, which follows the same policy. Both write
`user_deleted`; `actor_kind` (`user` versus `platform_staff`) is what tells them
apart in the trail.

## Reset abuse budget and outbox credentials

Password reset retains its client-address budget and adds a privacy-safe SHA-256 bucket over the canonical email (3 requests/hour by default). This account budget is shared through Redis, uses ADR-040's bounded process-local fallback, and suppresses additional reset rows/mail while returning the same generic 202 contract.

Verification codes, password-reset tokens, and invitation tokens are AES-256-GCM encrypted before `email_messages.context` is written. The ordered credential key ring supports rotation: the first key encrypts and every configured key may decrypt. The worker alone opens context; sent and permanently failed rows clear it.

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

Invitation tokens are generated from `secrets` and stored **only** as a SHA-256 hash on the invitation. The raw token is never returned by the API; its outbox copy is encrypted and delivered only to the invited address.

Acceptance is unauthenticated by necessity — the invited person may have no account yet — and the token in the body is the authorization. It creates the account when needed and always creates the membership, but **mints no session**: signing in stays a separate step, so a leaked invitation link cannot by itself produce a live session.

An administrator cannot invite an owner; only an owner can. Otherwise the boundary between the two roles would be decorative, since any admin could mint themselves a peer with full authority.

Unknown, spent, revoked, expired, and concurrently consumed invitations all answer identically. Acceptance claims the pending row with one conditional update before creating a user or membership.

## Enforcement points

1. Route dependency resolves the authenticated user from the bearer token.
2. Dependency resolves the workspace named by the token and re-verifies the membership and role.
3. Services receive an explicit tenant context; they never read one from input.
4. Repositories filter by `tenant_id`, applied in a single place (`TenantScopedRepository`).
5. Denials return consistent, non-revealing errors: cross-tenant access is `404`, never `403`.

## Testing

RBAC per role, cross-tenant access attempts, platform-versus-tenant boundaries, refresh rotation and replay, and invitation expiry and reuse are tested against a real PostgreSQL database in `tests/integration/test_authorization.py`. The HTTP surface and the role guards are tested separately with a stubbed service in `tests/integration/test_auth_endpoints.py`. Membership revocation is covered in `tests/integration/test_membership_revocation.py`, which walks the dependency graph and calls every workspace-scoped route with a revoked member's genuine token; refresh reuse in `tests/integration/test_refresh_reuse.py`.

Google sign-in is tested in four places: `tests/unit/test_google_oidc.py` mints real RS256 tokens against real key sets and attacks the verifier; `tests/unit/test_oauth_flow.py` attacks the single-use state store; `tests/unit/test_google_profile.py` pins which fields a login may change; and `tests/integration/test_google_profile.py` drives the login and link paths against a real database. `tests/integration/test_route_authorization.py` resolves every route's dependency tree and fails if the two open Google routes are not the ones documented. See [SECURITY.md](SECURITY.md).
