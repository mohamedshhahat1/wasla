# Billing

**Status: Implemented** — plans, the seeded catalogue, subscriptions, entitlements and their enforcement, the lifecycle and its sweep, invoices, payment records, the invoice API and the provider boundary all exist and are exercised against real PostgreSQL (migrations `0016` and `0017`; ADR-029, ADR-030, ADR-031). A live payment provider, overage pricing, refunds and dunning are Planned, each for a reason recorded in [../TASKS.md](../TASKS.md). See [../TASKS.md](../TASKS.md) phase 13.

Scope: plans, subscriptions, entitlements, invoicing, and payment provider boundaries. Plan limits are listed in [SAAS.md](SAAS.md); metering is in [ANALYTICS.md](ANALYTICS.md).

## Domain

| Concept | Status | Purpose |
| --- | --- | --- |
| Plan | Implemented | Configurable feature and limit definition |
| Subscription | Implemented | Tenant's current plan, period, and lifecycle state |
| Entitlement check | Implemented | Central authority for "is this action allowed now" |
| Invoice | Implemented | Billable period summary |
| Payment | Implemented | Recorded payment attempt and outcome |

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

Registering a workspace starts a subscription on `DEFAULT_PLAN_CODE`, on trial if that plan offers one. A signup whose catalogue has no such plan still succeeds, with a warning: a signup that 500s over billing configuration is the least forgivable failure in the product.

A workspace with no subscription — every workspace that predates billing — is entitled to the plan named by `DEFAULT_PLAN_CODE` all the same, and its period limits are counted over the calendar month. If that plan is missing, limits are not enforced and a warning is logged: a missing catalogue row should not take a deployment offline.

## The sweep

`WORKER_KINDS=billing` runs a loop that polls for subscriptions whose period has ended (ADR-022, like follow-ups and campaigns) and advances each one: a pending cancellation takes effect, a trial nobody acted on becomes `expired`, and anything else opens its next period starting where the last one ended — so a sweep that runs late does not shorten a customer's month.

It polls every ten minutes rather than every thirty seconds, because a period boundary is a date rather than an instant, and entitlements are computed from the row on each request anyway: a trial that ended at 09:00 and is noticed at 09:55 has cost nobody anything. A subscription that has already ended is never picked up again — its period ending is the past, not an event.

The rules live in `roll_over`, a pure function over the row, so the worker is only the query, the loop and the commit.

A refused action answers **402**, not 403. A permission error tells a caller to ask an administrator; this one tells them to upgrade, and a client that cannot tell them apart shows the wrong dialogue to a customer trying to give us money.

## Invoices

An invoice is a **record of a past period**, not a live calculation (ADR-031). The plan code and the amounts are copied onto the row when it is issued, so a plan repriced in April cannot change what March says — which is the only question anybody ever asks about an invoice.

An issued invoice is never edited. A mistake is **voided**, because the customer has already seen it and a bill that silently changes is worse than one visibly withdrawn. A paid invoice cannot be voided at all: that is a refund, a different operation and a different conversation.

Lines are the plan's fee plus what the workspace consumed. **The usage lines carry a quantity and no amount**, and that absence is deliberate: no per-unit overage price is stored anywhere, and inventing one would put a number on a bill that no pricing decision stands behind.

`UNIQUE(tenant_id, period_start)` makes billing a period twice impossible rather than merely unlikely — the caller is a sweep that may run on two replicas.

## Payments

Every attempt is a row. A failure is not forgotten when a later attempt succeeds, because that history is what a dispute, a chargeback and an angry email all turn on. Part payments leave the invoice open with a smaller balance; an overpayment leaves nothing outstanding rather than a negative.

`UNIQUE(provider, provider_reference)` is the processor's idempotency key: two webhooks about one charge become one payment.

## Provider independence

`PaymentProvider` is one method — charge this amount, with this idempotency key, and say what happened. Subscriptions, plans and periods stay Wasla's, because the moment a service knows what a "payment intent" is, the system belongs to that processor. A decline is an outcome, not an exception; only an unreachable provider raises.

`ManualProvider` is the shipped implementation and is **not a stub**. Much business-to-business SaaS is invoiced and paid by transfer weeks later, so `charge` returns `pending` and only a person who has seen the money marks the invoice paid. A provider that reported success would put a paid invoice in front of a finance team that has not paid.

Refunds, credits, proration and tax are all absent for one reason: they are decisions about money nobody has made yet, and a system that guesses produces numbers a customer is asked to pay.

## Entitlement enforcement

Limits are never compared inline. `EntitlementService` is the only thing that reads a plan, and where it is called is a decision in its own right (ADR-030): a refusal has a victim, and the question is always whether that person is the one who can fix the billing.

| Where | What happens |
| --- | --- |
| Creating an agent, connecting a number, inviting a colleague, submitting a document | **402**, from a dependency in the route signature |
| Scheduling a campaign | **402** for the whole audience at once, in the service, before a single message goes out |
| An agent turn with no AI requests left | The worker returns without composing; the job is released, not dead-lettered |
| A customer's inbound message | **Never refused.** No check exists on that path at all |
| Any read, including usage and entitlements | Never refused |
| A person's own reply | Never refused |

The guards are declared like the role guards — `slot: AgentSlotDep` in the signature — because a check written inside a handler is one the next handler forgets.

**Nothing on the inbound path is refused, and that is the most important line here.** The words belong to a customer who owes us nothing, and a non-2xx to Meta is retried until the subscription is disabled: a billing problem would become a permanently broken integration. Inbound messages count against the allowance and are never rejected because of it, so a workspace can exceed a period limit. That overage is visible in usage and is the platform's to price or to chase.

A workspace that downgrades keeps everything it already has. Its limits stop it adding more; nothing deletes an agent or disconnects a number, because a plan change is not a reason to destroy somebody's work.

## Platform revenue reporting

The platform layer exposes MRR, ARR, subscription revenue, active subscriptions, trials, cancellations, failed payments, upgrades and downgrades, estimated AI cost, estimated infrastructure cost, and estimated gross margin, with date-range filtering. Billing-affecting events are idempotent and audit-logged.
