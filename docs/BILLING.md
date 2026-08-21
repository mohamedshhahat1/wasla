# Billing

**Status: Planned** — no billing code exists yet. See [../TASKS.md](../TASKS.md) phase 13.

Scope: plans, subscriptions, entitlements, invoicing, and payment provider boundaries. Plan limits are listed in [SAAS.md](SAAS.md); metering is in [ANALYTICS.md](ANALYTICS.md).

## Domain

| Concept | Purpose |
| --- | --- |
| Plan | Configurable feature and limit definition |
| Subscription | Tenant's current plan, period, and lifecycle state |
| Invoice | Billable period summary |
| Payment | Recorded payment attempt and outcome |
| Entitlement check | Central authority for "is this action allowed now" |

Subscription states to support: trialing, active, past_due, cancelled, plus upgrades and downgrades.

## Provider independence

Billing logic is provider-agnostic behind an abstraction, so a payment provider can be added later without touching business rules. Billing APIs and internal models work in local development without a live provider.

## Entitlement enforcement

Limits are never hardcoded across the codebase. A single usage/entitlement service answers limit questions using stored plan configuration and aggregated usage, covering WhatsApp numbers, agents, messages, AI requests, and team members. Limit denials return consistent, actionable errors.

## Platform revenue reporting

The platform layer exposes MRR, ARR, subscription revenue, active subscriptions, trials, cancellations, failed payments, upgrades and downgrades, estimated AI cost, estimated infrastructure cost, and estimated gross margin, with date-range filtering. Billing-affecting events are idempotent and audit-logged.
