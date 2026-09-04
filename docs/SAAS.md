# SaaS Model

**Status: Implemented** — tenants, global users, memberships, invitations, workspace switching and the platform role layer (phases 1-2); the cross-workspace platform surface (phase 12); and plans, entitlements and billing (phase 13). The first platform role is granted by an operator command rather than by SQL (ADR-094). Decisions: ADR-001, ADR-002, ADR-003.

Scope: tenancy, workspaces, platform administration, plans, and entitlements.

## Tenants

Every business is a Tenant (`id`, `name`, `slug`, `status`, timestamps). A tenant owns users through memberships, WhatsApp accounts, agents, conversations, contacts, leads, knowledge bases, documents, follow-ups, campaigns, templates, analytics, usage, subscription, and settings. Tenant-owned records carry `tenant_id`.

## Multi-workspace users

Users are global identities. Company access is `User -> Membership -> Tenant`, with `UNIQUE(user_id, tenant_id)` and roles scoped to the membership:

```
User
 +-- Membership(role=TENANT_OWNER) --> Tenant A
 +-- Membership(role=TENANT_ADMIN) --> Tenant B
 +-- Membership(role=MEMBER)       --> Tenant C
```

Requirements: one account across many companies, creating multiple companies, switching the active workspace, inviting users, per-company roles, removing and suspending memberships, and tenant-scoped authorization on every operation.

## Active workspace

A request executes in exactly one workspace. The authorization chain is `User -> Membership -> Tenant -> Role -> Resource`. A client-supplied tenant identifier is only accepted after verifying an active membership.

## Invitations

`tenant_invitations` holds `tenant_id`, `email`, `role`, token reference, `expires_at`, `accepted_at`, `created_by`, `created_at`. Tokens are single-use, expiring, and cannot bypass tenant authorization.

## Platform owner

Platform roles (`PLATFORM_OWNER`, `PLATFORM_ADMIN`) are separate from tenant roles, in both directions: a platform role grants nothing inside a workspace, and owning a workspace grants nothing across the platform.

**Implemented today**, under `/api/v1/platform/*` and in `app/platform/`: an overview of how many workspaces exist and in what state, how many WhatsApp numbers are connected and live, what the platform consumed in a window, and a searchable list of workspaces with each one's usage. The package exists so the one legitimate cross-tenant read in the codebase is visible; a cross-tenant query outside it is a bug.

**Planned**, and each for a stated reason rather than for want of time: revenue, MRR, ARR and churn need subscriptions (phase 13); estimated AI cost needs per-model prices that are stored nowhere, and inventing them would put a fabricated figure on a dashboard; and tenant administration — create, suspend, activate, deactivate — needs the audit log that arrives in phase 14, because those are exactly the actions that must never happen untraced.

## Plans and entitlements

Plans are stored and configurable, never hardcoded across the codebase. Limits are enforced centrally by a usage/entitlement service.

| Plan | Numbers | Agents | Messages | AI requests | Members |
| --- | --- | --- | --- | --- | --- |
| Starter | 1 | 1 | 1,000 | 100 | 2 |
| Pro | 3 | 5 | 10,000 | 5,000 | 10 |
| Business | 10 | 20 | 50,000 | 25,000 | 50 |
| Enterprise | Custom | Custom | Custom | Custom | Custom |

Billing detail is in [BILLING.md](BILLING.md); usage metering is in [ANALYTICS.md](ANALYTICS.md).
