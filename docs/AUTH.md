# Authentication and Authorization

**Status: Planned** — no authentication code exists yet. See [../TASKS.md](../TASKS.md) phase 2.

Scope: identity, credentials, sessions, membership resolution, and permission checks.

## Authentication

- Modern password hashing; plaintext credentials are never stored or logged.
- Access and refresh token strategy with an explicit revocation path for logout.
- Token validation on every protected request, producing the authenticated global user identity.
- Rate limiting on authentication endpoints.

## Authorization model

Every protected operation answers five questions: who is the user, which membership applies, which tenant is active, which resource is targeted, and does the resource belong to that tenant. Permission scopes:

| Scope | Roles |
| --- | --- |
| Platform | `PLATFORM_OWNER`, `PLATFORM_ADMIN` |
| Tenant | `TENANT_OWNER`, `TENANT_ADMIN`, `MEMBER` |

Planned future tenant roles: `SALES`, `SUPPORT`, `MANAGER`. Platform and tenant scopes never share authorization dependencies.

## Workspace context

The active workspace is resolved per request and verified against an active membership. `user.tenant_id` is never authoritative. Workspace switching changes context only, never identity.

## Enforcement points

1. Route dependency resolves the authenticated user.
2. Dependency resolves and verifies the active membership and role.
3. Services receive an explicit tenant context.
4. Repositories filter by `tenant_id`.
5. Denials return consistent, non-revealing errors.

## Testing requirements

RBAC per role, cross-tenant access attempts, platform-versus-tenant boundaries, membership suspension and removal, invitation token expiry and reuse, and token revocation are all explicitly tested. See [SECURITY.md](SECURITY.md).
