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

The two platform roles are **ranked**: `PLATFORM_OWNER` outranks `PLATFORM_ADMIN`. An admin administers the platform over every account that is not a platform owner; disabling, deleting or revoking the role of an owner is an owner's act. Nine of the eleven platform routes are staff-wide, because reading the estate, settling an invoice and suspending a workspace are the job rather than a privilege — only `disable` and `delete` consult the target's own standing. Above all of it sits one invariant: **at least one live platform owner must remain** after any supported operation, where "live" means active and not deleted. See [AUTHORIZATION.md §6.18](AUTHORIZATION.md).

**Implemented today**, under `/api/v1/platform/*` and in `app/platform/`: an overview of how many workspaces exist and in what state, how many WhatsApp numbers are connected and live, what the platform consumed in a window, and a searchable list of workspaces with each one's usage. The package exists so the one legitimate cross-tenant read in the codebase is visible; a cross-tenant query outside it is a bug.

**Planned**, and each for a stated reason rather than for want of time: revenue, MRR, ARR and churn need subscriptions (phase 13); estimated AI cost needs per-model prices that are stored nowhere, and inventing them would put a fabricated figure on a dashboard; and tenant administration — create, suspend, activate, deactivate — needs the audit log that arrives in phase 14, because those are exactly the actions that must never happen untraced.

## Plans and entitlements

Plans are stored and configurable, never hardcoded across the codebase. Limits are enforced centrally by a usage/entitlement service.

| Plan | Channel connections | Channel types | Agents | Messages | AI turns | Members |
| --- | --- | --- | --- | --- | --- | --- |
| Starter | 1 | WhatsApp | 1 | 1,000 | 100 | 2 |
| Pro | 3 | WhatsApp, Instagram, Messenger | 5 | 10,000 | 5,000 | 10 |
| Business | 10 | all five | 20 | 50,000 | 25,000 | 50 |
| Enterprise | Custom (unlimited) | all five | Custom | Custom | Custom | Custom |

The figures are **placeholders** platform staff confirm or replace (ADR-131,
ENT-20, ENT-24); migration 0098 publishes the channel columns as new versions of
the seeded plans, and existing subscribers keep the version they bought. A
*channel connection* is one active connection on any channel - a WhatsApp number
is one - and the channel types are the ones a workspace on the plan may connect
and automate. "All five" is WhatsApp, Instagram, Messenger, Telegram and TikTok;
only WhatsApp has an adapter today, so the others are vocabulary until theirs
ship. Channel slots can be added with top-ups, general or for one channel type,
which staff price before they are sold. The AI turn allowance is one per
workspace, shared by every channel. See [BILLING.md](BILLING.md).

An **AI turn** is one customer message an agent took on, however many provider calls answering it took — a sentiment classification and up to three inference rounds are one turn. The figure was written "AI requests" until the AI remediation; migration 0059 moved each plan's number to the turn allowance unchanged, and provider requests are now metered as cost only (ADR-104).

Billing detail is in [BILLING.md](BILLING.md); usage metering is in [ANALYTICS.md](ANALYTICS.md).

### Follow-up settings and the inbox stream

Every workspace has follow-up settings (ADR-151) - **on, WhatsApp, one reminder after 5 minutes** from its creation (and for every workspace that existed when migration 0122 ran). An owner or admin changes them; any member reads them. Sequences run on every plan with an AI allowance; each step that asks the model is one AI turn. Deployment bounds:

| Setting | Default | What it bounds |
| --- | --- | --- |
| `FOLLOW_UP_SEQUENCE_MAX_STEPS` | 5 | Reminders a workspace may set (never more than 5) |
| `FOLLOW_UP_WINDOW_MARGIN_SECONDS` | 1800 | How much of the customer's 24-hour window must remain for a step to go |
| `RT_STREAMS_PER_USER` | 5 | Open inbox event streams one person may hold, across every API container |
| `RT_STREAMS_PER_WORKSPACE` | 100 | Open event streams one workspace may hold (ADR-155) |

### Campaign platform settings

Set by the deployment, never by a workspace ([CAMPAIGNS.md](CAMPAIGNS.md)):

| Setting | Default | What it bounds |
| --- | --- | --- |
| `CAMPAIGN_PORTFOLIO_LIMIT_SHARE` | 0.80 | The share of a business portfolio's Meta messaging limit campaigns may use (ADR-146) |
| `CAMPAIGN_MESSAGING_LIMIT_REFRESH_SECONDS` | 21600 | How stale a number's limit may be before scheduling reads it again |
| `CAMPAIGN_CONTACT_MARKETING_INTERVAL_SECONDS` | 86400 | One marketing message per customer per this long (ADR-147) |
| `CAMPAIGN_PARAMETER_MAX_CHARS` | 60 | The longest a person's own value may be (ADR-148) |
| `CAMPAIGN_TEST_SENDS_PER_DAY` | 10 | Test sends per workspace in a moving day |
| `CAMPAIGN_EXPORTS_PER_HOUR` | 5 | Recipient-list exports per workspace in a moving hour |
| `CAMPAIGN_REPLY_ATTRIBUTION_SECONDS` | 259200 | How long after a copy an answer counts as its reply |

Each workspace chooses its own **time zone** at onboarding and its **quiet
hours** (default 22:00-09:00, on) in it (ADR-145). Meta's **rate cards** for the
campaign cost estimate are entered by platform staff (ADR-150); Wasla charges
nothing for WhatsApp messages - Meta bills the business.
