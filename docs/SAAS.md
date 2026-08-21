# SaaS Model

**Status: Planned** — no tenancy code exists yet. See [../TASKS.md](../TASKS.md) phases 1-3 and 13.

Scope: tenancy, workspaces, platform administration, plans, and entitlements. Decisions are recorded as ADR-001, ADR-002, and ADR-003 in [../DECISIONS.md](../DECISIONS.md).

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

Platform roles (`PLATFORM_OWNER`, `PLATFORM_ADMIN`) are separate from tenant roles. Capabilities: list, search, create, suspend, activate, and safely deactivate tenants; inspect tenant users, WhatsApp accounts, agents, conversations, and leads; view usage, billing, subscriptions, plans, system health, audit logs, and platform analytics including token usage, estimated AI cost, revenue, MRR, and ARR. Platform actions are always audit-logged.

## Plans and entitlements

Plans are stored and configurable, never hardcoded across the codebase. Limits are enforced centrally by a usage/entitlement service.

| Plan | Numbers | Agents | Messages | AI requests | Members |
| --- | --- | --- | --- | --- | --- |
| Starter | 1 | 1 | 1,000 | 100 | 2 |
| Pro | 3 | 5 | 10,000 | 5,000 | 10 |
| Business | 10 | 20 | 50,000 | 25,000 | 50 |
| Enterprise | Custom | Custom | Custom | Custom | Custom |

Billing detail is in [BILLING.md](BILLING.md); usage metering is in [ANALYTICS.md](ANALYTICS.md).
