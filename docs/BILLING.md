# Billing

**Status: In Progress** — plans, the seeded catalogue, subscriptions, the entitlement service, the subscription lifecycle and the billing APIs are Implemented (migration `0016`, ADR-029). The enforcement points, the period roll-over sweep, invoices and payment providers are Planned. See [../TASKS.md](../TASKS.md) phase 13.

Scope: plans, subscriptions, entitlements, invoicing, and payment provider boundaries. Plan limits are listed in [SAAS.md](SAAS.md); metering is in [ANALYTICS.md](ANALYTICS.md).

## Domain

| Concept | Status | Purpose |
| --- | --- | --- |
| Plan | Implemented | Configurable feature and limit definition |
| Subscription | Implemented | Tenant's current plan, period, and lifecycle state |
| Entitlement check | Implemented | Central authority for "is this action allowed now" |
| Invoice | Planned | Billable period summary |
| Payment | Planned | Recorded payment attempt and outcome |

## Plans

A plan is a row in `plans`: a stable `code`, a name, a price in `Numeric` (never float — 19.99 is not representable in binary floating point, and that error reaches an invoice), an interval, trial days, and its limits.

**Limits are JSONB keyed by a closed vocabulary, and an absent key means unlimited** (ADR-029). That convention is what makes "custom limits" expressible for Enterprise without a sentinel number somebody eventually sums or displays. A malformed value reads as unlimited too: a plan edited badly must not lock a paying customer out of their own product.

| Key | Counts |
| --- | --- |
| `whatsapp_numbers` | Connected numbers that are not disabled |
| `agents` | Configured agents, drafts and disabled ones included |
| `team_members` | Memberships |
| `knowledge_documents` | Documents |
| `period_messages` | Messages sent **and** received in the billing period |
| `period_ai_requests` | Provider calls in the billing period |
| `period_campaign_messages` | Campaign messages in the billing period |

The first four count rows that exist now; a workspace over one stays over it until something is deleted, because downgrading a plan should stop somebody adding more rather than delete their work. The last three count `usage_events` since `current_period_start`, which is what makes "1,000 messages a month" reset.

A disabled number frees its slot and a draft agent does not, and the asymmetry is deliberate: a disabled number is connected to nothing, while a limit that ignored draft agents would be satisfied by twenty agents somebody toggles.

Migration `0016` seeds starter, pro, business and enterprise from the table in [SAAS.md](SAAS.md). They are seeded, not owned: editing a price afterwards is a change to the row.

## Subscriptions

One per workspace, enforced by `UNIQUE(tenant_id)` rather than by a service — two subscriptions are two answers to "what am I allowed to do". States: `trialing`, `active`, `past_due`, `cancelled`, `expired`.

`past_due` still serves the workspace. A failed card is a conversation to have with a customer, not a reason to cut them off mid-sentence with their own customers; when that grace runs out is a separate decision the platform makes. `expired` is what a trial becomes when nobody acts, kept apart from `cancelled` because nobody chose it.

A workspace with no subscription — every workspace that predates billing — is entitled to the plan named by `DEFAULT_PLAN_CODE`, and its period limits are counted over the calendar month. If that plan is missing, limits are not enforced and a warning is logged: a missing catalogue row should not take a deployment offline.

A refused action answers **402**, not 403. A permission error tells a caller to ask an administrator; this one tells them to upgrade, and a client that cannot tell them apart shows the wrong dialogue to a customer trying to give us money.

## Provider independence

Billing logic is provider-agnostic behind an abstraction, so a payment provider can be added later without touching business rules. Billing APIs and internal models work in local development without a live provider.

## Entitlement enforcement

Limits are never hardcoded across the codebase. A single usage/entitlement service answers limit questions using stored plan configuration and aggregated usage, covering WhatsApp numbers, agents, messages, AI requests, and team members. Limit denials return consistent, actionable errors.

## Platform revenue reporting

The platform layer exposes MRR, ARR, subscription revenue, active subscriptions, trials, cancellations, failed payments, upgrades and downgrades, estimated AI cost, estimated infrastructure cost, and estimated gross margin, with date-range filtering. Billing-affecting events are idempotent and audit-logged.
