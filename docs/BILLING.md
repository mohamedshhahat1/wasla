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

## Taking payments (ADR-044)

Card payments go through Paymob's Intention API and Unified Checkout. The
provider is chosen by `BILLING_PROVIDER`, which defaults to `manual` — a
deployment that configures nothing bills exactly as it did before, by
recording what is owed and waiting for a person to confirm a transfer.

### The flow

```
POST /api/v1/billing/checkout   {"plan_code": "pro"}      owner only
        │
        ├─ plan read from the database, priced there
        ├─ invoice opened (or the period's existing one reused)
        ├─ payment row written, status pending          ← reference is its id
        │
        ↓
   Paymob POST /v1/intention/    Authorization: Token <secret key>
        │                        special_reference = our payment id
        ↓
   { client_secret, id }
        │
        ↓
   redirect_url = https://eg.checkout.paymob.com/?publicKey=…&clientSecret=…
        │
   customer pays on Paymob's page
        │
        ├──────────────► redirect back to the app  ← settles NOTHING
        │
        ↓
   POST /api/v1/webhooks/paymob?hmac=…              ← the authoritative signal
        │
        ├─ HMAC-SHA512 over 20 fields, compare_digest
        ├─ payment_events insert  ← UNIQUE(provider, provider_event_id)
        ├─ payment matched by our own reference, scoped to its tenant
        ├─ amount and currency checked against the invoice
        ↓
   invoice paid · payment succeeded · past_due subscription → active
```

### What settles an invoice, and what does not

Only a callback whose signature verifies. The customer's redirect back to the
site is for showing them a success page; anybody can visit a URL, and there is
deliberately no endpoint that reads it and changes anything.

Four refusals stand between a verified callback and a paid invoice:

| Check | Why |
| --- | --- |
| The event is new | `UNIQUE(provider, provider_event_id)`, and the insert *is* the claim — a preceding read is what a retry storm defeats |
| It names a payment we issued | By our own reference, sent as `special_reference` and returned as `order.merchant_order_id` |
| That payment is this workspace's | The repository's tenant filter, so a leaked reference still reaches nothing |
| The amount and currency match | A provider reporting a different figure is not settling this invoice, whatever it says |

### What a payment does to a subscription

Deliberately very little. Paying an invoice settles an invoice; which plan a
workspace is on is `SubscriptionService`'s decision.

| Before | After a successful payment |
| --- | --- |
| `past_due` | `active` — the one state a payment changes on its own |
| `trialing` | unchanged; a trial is not ended by paying |
| `active` | unchanged |
| `cancelled` / `expired` | unchanged — paying is not a request to resubscribe, and reviving would undo a deliberate decision |

A workspace with no subscription at all is not given one. Checkout collects for
an invoice; starting a subscription is `POST /billing/subscription`.

### Configuration

| Variable | Meaning |
| --- | --- |
| `BILLING_PROVIDER` | `manual` (default) or `paymob` |
| `PAYMOB_SECRET_KEY` | Authenticates **us to Paymob** when creating an intention |
| `PAYMOB_PUBLIC_KEY` | Not secret; goes in the checkout URL a browser follows |
| `PAYMOB_HMAC_SECRET` | Authenticates **Paymob to us** on the callback |
| `PAYMOB_INTEGRATION_IDS` | Which integrations a checkout may use — see below |
| `PAYMOB_REGION` | `egypt`, `uae`, `oman` or `saudi` — picks the API host *and* the checkout host, which differ |

Setting `BILLING_PROVIDER=paymob` without all of them refuses to boot, in every
environment. Test and live share a base URL: Paymob's documentation states the
mode is decided by which keys and integration ids are used, so there is no
sandbox setting.

### Which payment methods a customer sees

**Configuration, not code.** The list in `PAYMOB_INTEGRATION_IDS` is passed to
the Intention API as `payment_methods` and Paymob renders the hosted checkout
from it. Adding a wallet alongside cards is that variable changing; no service,
schema, migration or business rule is involved.

Paymob documents the field as taking the Integration ID(s) "as integers (e.g.,
1256) or as names enclosed in quotes (e.g., `"card"`)", and both forms are
accepted here. Numeric entries become integers, everything else stays a string,
and neither is interpreted.

**Nothing in this application knows what an entry means.** There is no table
mapping a number to card or wallet, and there must not be one: the meaning is
Paymob's, it is visible in their dashboard under Settings → Payment
Integrations, and a copy here could only ever go stale or be wrong. An id is an
opaque token that is quoted to the provider.

`tests/unit/test_payment_methods_are_configuration.py` holds that boundary
open. It asserts the configured list reaches the intention request untouched,
that two providers differing only in configuration produce requests differing
only in `payment_methods`, that no module between `CheckoutService` and the
adapter names a payment method in code, and that no integration id is hardcoded
anywhere in `app/`.

### Paying a renewal

`POST /billing/checkout` takes **either** a `plan_code` or an `invoice_id`, and
exactly one of them. Naming a plan is somebody choosing what to buy; naming an
invoice is somebody paying a bill the sweep already issued for them, and the
second is what makes the billing cycle collectible rather than merely recorded.

The amount is the invoice's `outstanding`, so a part-paid bill collects what is
left rather than the whole figure again.

### Checkout idempotency

An optional `idempotency_key` on the request, unique per workspace, so a client
that retried a request it never saw the answer to does not open a second
payment page.

A repeat is **refused with 409, not replayed.** The response carries a URL
containing the provider's client secret, which is deliberately never stored, so
there is no honest replay available — and Paymob documents `special_reference`
as unique, so the same page cannot be re-fetched under the same reference
either. The caller learns its first request was accepted and reads the
payment's status, which is what it wanted to know.

Two requests without a key both open a page. That is correct: each attempt is
its own row, and the *invoice* constraint is what stops a period being billed
twice. Double collection is prevented on the settling side instead — see the
state machines below.

### Reading a payment back

`GET /billing/payments/{id}` is what a client polls after the customer returns
from the payment page. The provider redirects them with the result in the query
string and **that is not evidence**; this endpoint answers from the signed
callback.

`pending` is a real answer and not a missing one. 3-D Secure and several local
methods complete after the customer has already been sent back, so a client
that renders `pending` as failure tells people their payment did not work while
it is still working.

### State machines

Payment statuses arrive from outside — a processor decides what a payment did
and tells us — so a signed callback is checked against what we already believe
before it is applied. `PAYMENT_TRANSITIONS` and `INVOICE_TRANSITIONS` in
`db/models/invoice.py` are the rules.

| From | May become |
| --- | --- |
| `pending` | `succeeded`, `failed` |
| `succeeded` | `refunded` |
| `failed` | nothing — a retry is another attempt and another row |
| `refunded` | nothing |

`refunded → succeeded` never happens: it is how a refunded customer keeps the
product. `failed → succeeded` never happens: it would erase the decline a
dispute turns on. An invoice that is already `paid` cannot be settled again,
which is what stops a second payment being added to `amount_paid`.

| Invoice | May become |
| --- | --- |
| `draft` | `open`, `void` |
| `open` | `paid`, `uncollectible`, `void` |
| `paid` | `open` — **only** by being refunded |
| `uncollectible` | `paid`, `void` |
| `void` | nothing |

`paid → open` looks wrong and is not: `amount_paid` records money we *hold*, so
giving it back leaves an invoice its payments no longer cover. Voiding it
afterwards — because the customer is leaving — is a separate deliberate act
rather than something inferred from a reversal.

### The provider event ledger

`payment_events` is the record of everything a processor has told us. Its
unique constraint is the idempotency mechanism, and the insert *is* the claim:
the row is written before the decision is made, and the outcome is filled in
once there is one.

`provider_event_id` is deliberately **not** the bare transaction id. It pairs
the transaction with the state being reported — `192036465:succeeded`,
`192036465:refunded` — because one transaction produces several callbacks over
its life. Keying on the transaction alone would file each later one as a
duplicate of the first, so a 3-D Secure payment that reported `pending` before
`success` would settle nothing, and a refund notification on the original
transaction would be silently dropped. The raw id is kept beside it in
`provider_transaction_id`, which is the number the provider's dashboard uses.

| Outcome | Meaning |
| --- | --- |
| `applied` | The event was believed and something changed |
| `duplicate` | This exact event was already recorded |
| `unmatched` | Verified, but naming no payment of ours |
| `mismatched` | The reported amount or currency disagreed with the invoice |
| `no_change` | Believed, and said nothing new |
| `refused` | Believed, and asked for a move the rules forbid |

`detail` is a sentence written by this application. **No part of the provider's
payload is stored** — the callback body carries a masked card number, the
customer's billing details and a redirect URL containing a bearer token, and
none of it is ours to keep.

## Refunds (ADR-045)

`POST /billing/payments/{id}/refund`, owners only, **202 Accepted**.

There is no amount in the request. It is the payment's own unreturned balance,
computed on the server, so no client can ask for more back than was paid. That
also settles the partial-refund question: Wasla has no credit notes and no way
to render "half of March", so a refund returns what is left of one payment.
Enforcing full-remainder semantics is the honest version of not supporting
partial refunds — the alternative is inventing a concept the invoice model
cannot express.

**Asking is not confirming.** The service records that the reversal was
requested and stores the provider's id for it; the payment still says
`succeeded` and `refunded_amount` is still zero. Nothing is marked returned
until a signed callback says the reversal happened — which is also the path a
refund issued from Paymob's own dashboard arrives on, so there is exactly one
place that writes `refunded_amount`.

```
POST /billing/payments/{id}/refund
        │
        ├─ payment must be this workspace's, collected, not already reversed
        ├─ refund_requested_at written, audited     ← before the provider call
        │
        ↓
   Paymob POST /api/acceptance/void_refund/refund
        │       { transaction_id, amount_cents }
        ↓
   { id: <reversal transaction> }  → payment.refund_reference
        │
        │   … hours or days …
        ↓
   POST /api/v1/webhooks/paymob     is_refunded: true
        │
        ↓
   refunded_amount set · payment refunded · invoice reopened
```

A refund requested and never confirmed is a findable state — `refund_reference`
set with `refunded_amount` still zero — and it usually means the callback URL
is wrong and a customer is waiting. `GET /billing/payments/{id}` reports it as
`refund_pending`.

Void is not attempted as a fallback. Paymob documents
`/api/acceptance/void_refund/void` for a transaction that has not settled yet,
and choosing between the two from an error body would mean guessing at response
codes this integration has never seen. A refund Paymob refuses surfaces as a
provider error an operator can act on, and the dashboard can void it.

## Recurring billing

Wasla renews on its own calendar and collects through a hosted checkout. There
is no card on file.

```
period ends
    │
    ├─ billing worker issues the invoice for the period that ended
    ├─ owners are emailed that it is due
    │
    ├─ customer pays it:  POST /billing/checkout {"invoice_id": …}
    │       └─ callback settles it; past_due → active
    │
    └─ nobody pays it for GRACE_DAYS (7) from issue
            └─ subscription → past_due, audited, owners emailed again
```

`past_due` still serves the customer. It is in `SERVING_STATUSES` deliberately:
a failed payment is a conversation to have, not a disconnection, and the
platform decides separately when that grace has run out. A trial is never
marked behind — nobody agreed to pay for it — and neither is a cancelled or
expired subscription, which would hand back entitlements the cancellation took
away.

The grace runs from `issued_at` rather than from the period boundary, so a
customer gets seven days from being *asked*, and a sweep that had been down for
a fortnight does not mark every customer behind the moment it comes back.

### Automatic card debits are not built, and why

Paymob documents a Subscription API
(`POST /api/acceptance/subscription-plans`, then `subscription_plan_id` on the
intention) and CIT/MIT card tokenisation. Neither is used, and this is a
decision rather than an omission:

- It **requires a MOTO integration ID**, which Paymob enables per merchant. It
  cannot be obtained, tested or even confirmed available without an account.
- It authenticates with a **Bearer auth token** from the older
  `/api/auth/tokens` flow, which needs an API key — a fourth credential,
  separate from the secret key everything else here uses.
- Its billing frequency is a **fixed number of days** (7, 15, 30, 60, 90, 180,
  360). Wasla bills on calendar months, so a Paymob subscription on `30` drifts
  away from the period this system charges for, and the two would disagree
  about what a customer owes within a year.
- Wasla's plan catalogue would need a mirrored Paymob plan per plan and
  currency, kept in step through `change_plan` and `cancel`.

Building that against a payload shape nobody here can exercise would be writing
an unverifiable subsystem to match a provider capability. The provider seam —
`CheckoutProvider` — is where it goes if the product decides it wants card-on-
file renewals, and the merchant-level dependency is the thing to resolve first.

### What is still not built

Honest list.

- **Automatic card debits**, for the reasons above. The external dependency is
  *merchant must enable a MOTO integration and issue an API key*, not missing
  code.
- **Credit notes and partial refunds.** A refund returns a payment's remaining
  balance; there is no way to say "half of March" because there is nothing that
  would render it on an invoice.
- **Void.** Available at the provider seam and through Paymob's dashboard; not
  wired to an endpoint, because choosing between void and refund needs error
  semantics this integration has not seen.
- **Nothing has been verified against a live Paymob account.** Every claim in
  this document is about code checked against published documentation; the HTTP
  boundary is exercised against a mock transport, and the HMAC against the
  vendor's own published worked example. Read this as a description of the
  implementation, not of a transaction that happened.
