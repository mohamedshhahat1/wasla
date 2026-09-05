# Billing

**Status: Implemented** — plans, the seeded catalogue, subscriptions, entitlements and their enforcement, the lifecycle and its sweep, invoices, payment records, the invoice API and the provider boundary all exist and are exercised against real PostgreSQL (migrations `0016` and `0017`; ADR-029, ADR-030, ADR-031). Paymob checkout, refunds and saved-card renewals are Implemented (ADR-044, ADR-045); a priced plan is granted only by verified settlement (ADR-059). Dunning is Implemented (ADR-061): an unpaid renewal moves a workspace to `past_due` and then to `suspended`, where the paid plan stops resolving. Overage pricing is Planned, for a reason recorded in [../TASKS.md](../TASKS.md). See [../TASKS.md](../TASKS.md) phase 13.

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
| `storage_bytes` | Bytes held in the object store, summed over attachments that still name one |
| `period_messages` | Messages sent **and** received in the billing period |
| `period_ai_requests` | Provider calls in the billing period |
| `period_campaign_messages` | Campaign messages in the billing period |

The first five measure what exists now; a workspace over one stays over it until something is deleted, because downgrading a plan should stop somebody adding more rather than delete their work. The last three count `usage_events` since `current_period_start`, which is what makes "1,000 messages a month" reset.

### Storage is the one that is a `SUM` (ADR-091)

`storage_bytes` is a capacity rather than a count, and it is deliberately **not** read from `usage_events`. That table is authoritative for what a workspace has *consumed* and is append-only by design (ADR-030), so `STORAGE_USED` records bytes when they are written and never subtracts when retention deletes them: it answers "how much has this workspace ever stored" and cannot answer "how much is it holding". A capacity limit needs the second question, so it sums `message_media.byte_size` over the rows whose `storage_state` still names an object.

That choice has a consequence worth stating: **the committed upload intent is the reservation.** A row goes `PENDING` before the object exists (ADR-087), and `PENDING` counts — so two uploads racing for the last megabyte serialise on the workspace's advisory lock, the first commits its intent, and the second counts those bytes and is refused. There is no counter to drift and nothing to reconcile: a write that never lands is settled to `ABSENT` by the upload reconciler and its space comes back in the same statement, and retention purging a file frees it the same way.

`MISMATCHED` counts, because that object is still in the bucket and is deliberately never deleted. `PURGING` counts, because a delete in flight is not a delete that happened.

**Where it is enforced.** Both paths that write an object: an inbound attachment is *skipped* with the reason on its row — a customer's WhatsApp message is never refused for the business's billing (ADR-030) — and an authenticated upload is refused with a 402 before Meta is asked to do anything.

**What it is not.** It is not a commercial tier. Migration `0043` writes the same 50 GiB onto starter, pro and business as a technical safety ceiling, because storage pricing is a product decision nobody has taken and a limit a customer hits is one somebody has to have agreed to sell them. Enterprise is left without the key, which is what "custom" means here. Tiering it later is an `UPDATE` on four rows.

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

### Claiming, and why two workers are safe (ADR-082)

Every phase claims its rows with `FOR UPDATE ... SKIP LOCKED` and processes each claim in **its own transaction**. Two billing workers therefore divide a cohort instead of colliding over it.

They used to collide. The sweep was one transaction over one unlocked batch, so both workers read the same due subscriptions, both called `issue_for_period`, both found no invoice because neither had committed, and both inserted — the loser blocking on `uq_invoices_tenant_id_period_start` until the winner committed and then raising, which aborts a PostgreSQL transaction and took the rest of that worker's pass with it. Correctness rested on a constraint that fires *after* the work.

| phase | what is claimed | why that row |
| --- | --- | --- |
| roll-over and invoicing | the subscription | one per tenant, and the period bounds, status and invoice uniqueness all hang off it |
| saved-card collection | the invoice | an attempt belongs to an invoice, and two workspaces' invoices are independent work |
| past-due and suspension | the invoice **and** its subscription | a workspace can have several overdue invoices, so claiming only the invoice lets two workers transition one subscription twice |

`SKIP LOCKED` rather than a plain `FOR UPDATE`: a row somebody else holds is somebody else's work, and queueing behind it to find that out is how a second worker spends a pass achieving nothing.

Each claim is taken twice — once by the batch query, which commits and releases, and again by the transaction that acts, with the same eligibility predicate. Acting on a row whose lock has been released is exactly the duplicate this is for, and re-asking under the lock is what makes "one attempt reaches the provider" true of the row rather than of a unique key that fires after money has moved.

**`CLAIM_LIMIT` is a batch size, not a ceiling.** A phase drains until it finds nothing, so a first-of-the-month cohort finishes in one pass rather than at 200 per ten minutes.

Two starvation bugs were fixed on the way, and neither was about concurrency. Chasing an invoice changes its *subscription* and leaves the invoice as overdue as it was, so with the status checked after the claim an already-chased invoice held the front of every future batch for ever; the subscription status is now a predicate in the query. And an invoice whose collection attempts are spent has `next_collection_at IS NULL`, indistinguishable from one nobody has tried, so `MAX_COLLECTION_ATTEMPTS` is passed into the claim query too — passed in, so the rule still lives in `RecurringService`.

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

### How a workspace gets onto a paid plan (ADR-059)

**A plan with a price is granted only by settlement.** `POST
/billing/subscription` and `POST /billing/subscription/plan` refuse one with
`402 payment_required`; they remain the route to a *free* plan, so downgrading
to the default tier is still self-service. Buying is:

    POST /billing/checkout  {"plan_code": "pro"}
        -> invoice raised for pro, pending payment written, hosted page returned
        -> customer pays
        -> POST /webhooks/paymob   (signed, and the only authoritative signal)
        -> invoice paid  ->  subscription moved onto invoice.plan_code

The plan is read from the **invoice**, which recorded it before the provider was
ever called, so the grant is decided by a row this system wrote. The callback
contributes one fact: the money arrived. A declined, unsigned, mismatched or
replayed callback grants nothing.

This closes a hole in which the whole pipeline above was optional: `change_plan`
moved a workspace onto any public plan the moment an owner asked, so every paid
tier was free for the asking. The money path was already strict; nothing joined
it to the plan.

### What a payment does to a subscription

Beyond applying the plan the invoice was raised for, deliberately very little.
Paying settles an invoice; it does not resubscribe, revive or extend anything.

| Before | After a successful payment |
| --- | --- |
| on another plan, invoice names a priced plan | moved onto that plan, `active`, period restarts (ADR-059) |
| on the plan the invoice names | unchanged — a renewal, and the sweep owns periods |
| `past_due` | `active` — grace was running and the money arrived |
| `suspended` | `active` — service was withheld pending exactly this payment (ADR-061) |
| `trialing` | unchanged; a trial is not ended by paying |
| `active` | unchanged |
| `cancelled` / `expired` | unchanged — paying is not a request to resubscribe, and reviving would undo a deliberate decision |

The recoverable set is closed at those two, in `CheckoutService`, rather than
expressed as "any status that is not active" - so a sixth status added later
cannot silently become revivable by a payment.

### When a workspace stops being served (ADR-061)

    ACTIVE ──BILLING_PAST_DUE_DAYS──▶ PAST_DUE ──BILLING_SUSPEND_AFTER_DAYS──▶ SUSPENDED

Both thresholds count days from the invoice's `issued_at` - the day the customer
was actually asked for money, rather than a period boundary they never saw - and
that column is written once, so neither threshold moves under a workspace while
it is being chased.

`PAST_DUE` is inside `SERVING_STATUSES` and `SUSPENDED` is not. So the paid plan
keeps resolving through the grace and stops afterwards, and the workspace falls
back to `DEFAULT_PLAN_CODE` rather than losing access outright. The billing
worker changes the status; `EntitlementService` decides what a status means.
Neither knows the other's job.

A suspended subscription is excluded from the roll-over sweep, so no new period
opens, no further invoice is raised, and `RecurringService` will not charge a
saved card against it.

`SUSPENDED` is a distinct status rather than a reuse of `cancelled` because this
vocabulary records *who decided*: a cancellation is the customer's decision and a
suspension is the platform's. Collapsing them would misattribute it in the audit
trail, count it as churn beside genuine cancellations, and make recovery
impossible to express.

**Recovery is something the customer can reach.** A suspended owner keeps every
billing route: they can read the subscription, and `POST /billing/checkout`
accepts both doors - naming the overdue `invoice_id`, or naming the plan, which
issues one for the current period. Neither is gated on the subscription status,
and that is deliberate rather than accidental. `SUSPENDED` is a member of
`TERMINAL_SUBSCRIPTION_STATUSES`, so a generic `is_terminal` refusal anywhere on
the way to a payment page would close the loop - a workspace that must pay to
recover and cannot start a payment because it has not paid. The reachable path
is asserted end to end over HTTP in `tests/integration/test_dunning_lifecycle.py`
rather than inferred from settlement supporting the transition.

What recovery does **not** open is a free door. `POST /billing/subscription/plan`
still answers 402 for a priced plan and 409 for the free one, so a suspended
workspace can neither re-select what it owes for nor downgrade out of the bill;
only a verified settlement moves it (ADR-059). And `cancelled` and `expired`
remain unrecoverable by the same route: they may open a checkout and pay it, the
invoice settles honestly, and the subscription still does not come back.

A workspace with no subscription at all is not given one, and a paid invoice
for it settles while granting nothing. That state needs `DEFAULT_PLAN_CODE` to
name no plan — where limits are already unenforced — and is logged as
`billing.paid_plan_without_subscription` rather than guessed at: inventing trial
and period rules inside a settlement path is the parallel state machine ADR-059
exists to avoid.

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
        ├─ refund_requested_at written, audited, COMMITTED
        │                                           ← before the provider call
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

A refund requested and never confirmed is a findable state — `refund_requested_at`
set with `refunded_at` still empty — and it usually means the callback URL is
wrong and a customer is waiting. `GET /billing/payments/{id}` reports it as
`refund_pending`.

**Committed, not flushed, and that is the difference between one refund and
two** (ADR-088). The request used to be flushed before the provider call, which
a rollback undoes along with everything else — so a process killed after Paymob
accepted the reversal left no record of having asked, and the next request
reversed the same money again. It is committed now, and the refusal that stops a
second reversal is keyed on the outstanding request rather than on the reference
written after the provider answers.

### Reversals: what happens to the plan the money bought (ADR-096)

The money going back is half of a refund. The other half is the commercial
grant, and until this was written there was no answer to it: a workspace owner
could refund their own payment through the endpoint above, receive the money,
and keep the plan it had bought — permanently, because nothing later looks at a
grant nobody is paying for.

The rule is about **cover**, not about the refund existing:

| after the reversal | plan | invoice |
|---|---|---|
| nothing is left paid on the invoice | withdrawn to `DEFAULT_PLAN_CODE` | reopened, not chased |
| some money remains on it | unchanged | reopened, and the dunning clock starts |

Withdrawing on a *partial* reversal would take a month of Pro away over one
unit returned, which is not the trade anybody wants; the customer has still
paid for most of it and owes the difference, so the invoice becomes chaseable
instead. That is what `issued_at` is for, and it is written at the reopen
rather than at creation — a checkout invoice somebody abandoned is not a debt,
and a fully refunded one is not either. Chasing somebody for the sum they were
just repaid would be worse than the defect this fixes.

Four conditions each exclude a downgrade that would be wrong. There must be a
subscription and a plan to fall back to; it must not be cancelled or expired,
which stay as the customer left them; the workspace must still be on the plan
*this* invoice bought, so a reversal never reaches past a later decision; and
no other settled invoice whose period contains this moment may be covering it
— a workspace paid up for the current period on a second invoice has bought
the plan, whatever happened to the first.

The transition is `SubscriptionService.change_plan(self_service=False)`, the
same state machine settlement uses, so period arithmetic and trial clearing
have one owner. It is audited as `subscription_plan_withdrawn` rather than as a
plan change, because a downgrade read from the trail must not be
indistinguishable from the customer having chosen the free plan themselves.

Failure is contained in the same direction as settlement: the money is the part
that must never be rolled back. A grant that could not be withdrawn — a
deployment whose `DEFAULT_PLAN_CODE` names no catalogue row — is logged loudly
and leaves the reversal recorded.

A reversal that got **no answer** therefore refuses the next attempt: the money
may already be going back, and somebody should look rather than ask again. A
reversal Paymob explicitly **declined** withdraws the record instead, because
that is an answer — nothing is reversing, so the cause can be fixed and the
refund asked for again.

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

### Automatic card debits: the decision that was superseded

**Superseded by ADR-046.** Saved cards and merchant-initiated renewal ship —
see *Saved cards* and *Automatic renewal* below, which describe what actually
runs. This section is kept because the reasoning it records is still the
reasoning for the part that did *not* change: Paymob's Subscriptions Module is
still not used, and *Why the Subscriptions Module is not used* below is the
current statement of that.

What follows is the position as it stood before a MOTO integration was
available.

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

**That is where it went.** `RecurringProvider` extends the seam,
`PaymobProvider.charge_saved_method` implements it, and the merchant-level
dependency is still the gate: `can_charge_saved_methods` is false without
`PAYMOB_MOTO_INTEGRATION_ID`, and a deployment without one bills exactly as
this section describes.

### Automatic renewal (ADR-046)

When the provider can charge a saved card, the sweep collects a due renewal
without anybody present:

```
period ends
    │
    ├─ invoice issued for the period that ended
    │
    ├─ sweep: is there a default card, a serving subscription, an attempt left?
    │       │
    │       ├─ no  →  invoice emailed and chased, exactly as before
    │       │
    │       └─ yes →  intention on the Moto integration
    │                 POST /api/acceptance/payments/pay  { token, payment_token }
    │                        │
    │                        └─ callback → the same settlement path as a link
    │
    └─ declined → attempt counted, retried after 1 then 3 days, then stopped
```

**Nothing is charged that should not be.** The refusals live in
`RecurringService` and each has a test:

| Refusal | Why |
| --- | --- |
| `not_supported` | No Moto integration — renewals are invoiced instead, which is how this product billed before saved cards |
| `not_serving` | **Cancelled or expired.** The most important line here: debiting somebody who has left is the failure customers do not forgive |
| `no_card` | No default card, or the customer removed it |
| `not_collectible` | The invoice is not open, or nothing is outstanding |
| `attempts_exhausted` | Three tries is enough; a card that declined three times will not work on the fourth |
| `not_due` | A retry before its scheduled time |

Attempts are counted **before** the provider is called, because a request that
timed out may still have been carried out. The claim is a payment row keyed
`auto:{invoice}:{attempt}`, so two sweeps racing cannot both charge.

### The attempt is durable before Paymob can move money (ADR-088)

Two workers racing is not the only way a card gets charged twice. One worker
*dying* is the other, and it used to be the dangerous one: the payment row, the
attempt counter and the Paymob request all became durable at the same commit,
and the money was first. A process killed in that window had taken 100 EGP and
left PostgreSQL with no record that it had, so the next sweep read an untouched
invoice and took it again.

So collection is three transactions, and the commits between them are the
point:

```
TX1   claim the invoice, insert the attempt, count it
      collection_state = claimed                          → COMMIT
TX2   collection_state = requested                        → COMMIT
--    call Paymob. No transaction, no connection, no lock.
TX3   record what came back                               → COMMIT
```

`collection_state` answers a question `status` cannot: **may another charge be
sent for this invoice?**

| State | Meaning | Another charge? |
| --- | --- | --- |
| `claimed` | Durable, provider not asked. Money cannot have moved | No — resolve first |
| `requested` | Asked, or may have been. **Money may have moved** | No — only a callback or a lookup may resolve it |
| `settled` | The outcome is known and on the payment | Yes, if the invoice is still open and the budget allows |
| `abandoned` | Shown never to have reached the provider; the attempt is returned, the schedule is not | Yes, after a backoff |

**`abandoned` returns the money-moving budget and advances the clock, and it
took a bug to separate those.** A request that provably never left must not
spend one of the three chances to debit a card — that half was always right.
What it also did was clear `next_collection_at`, which made the invoice
eligible again on the very next poll, so `MAX_COLLECTION_ATTEMPTS` could never
engage: the branch that declined to spend the budget was the branch that
cleared the schedule.

Two things followed from that, and the second is the worse one. Every poll
re-ran the eligibility checks, re-read the card and attempted an insert that
rolled back. And because the count went back to zero, the *next* attempt was
number one again — whose idempotency key the abandoned row already holds, for
ever. Measured against real PostgreSQL with a provider refusing the intention:

```
8 polls, five minutes apart
  provider calls 1 · payment rows 1 · collection_attempts 0
  next_collection_at None · eligible every poll
  30 days later, provider fixed: charged=False reason=not_due
```

One transient misconfiguration ended automatic collection for that invoice
permanently, in silence, even after the cause was fixed.

`ABANDON_BACKOFF` now spaces the retries — fifteen minutes, an hour, six hours,
then a day as the cap — keyed on how many attempts this invoice has already
abandoned, counted from the `ABANDONED` rows rather than a column beside them.
It is a separate table from `RETRY_BACKOFF` because it answers a different
question: that one spaces out asking a *customer's card* again, this one spaces
out asking a *provider* that would not take the request. It has no terminal
entry, because "give up" must not be expressed by spending a money-moving
attempt. And the claim key carries a retry suffix once an invoice has abandoned
anything, so a returned budget can actually be spent again.

`billing.collection_attempt_abandoned` carries the running count and the next
attempt time. One abandonment is noise; the fourth on one invoice is a
configuration problem somebody has to fix, and the count is what tells them
apart.

A partial unique index on `payments (invoice_id) WHERE collection_state IN
('claimed','requested')` is what actually guarantees it. The claim query
excludes such invoices too, but the query is the politeness and the index is the
guarantee.

**A due date is not a licence.** An invoice whose last attempt is unresolved is
not collectible however far `next_collection_at` has passed. If a renewal is not
being taken, look at the last attempt's state before looking at the schedule.

**And such a workspace is not suspended.** The audit named two harms, and the
second was a customer whose card was debited by a worker that then died being
cut off for non-payment thirty days later. So the suspension sweep skips an
invoice whose last collection attempt has no outcome.

Chasing is deliberately *not* guarded, and the asymmetry is the point:
`PAST_DUE` still serves the customer, and the notice is what gets a person to
look at an attempt nobody can resolve. Being cut off is the irreversible-feeling
act, and that is the one that waits.

The cost is stated rather than hidden: an attempt that can never be resolved
keeps a workspace served. That is why the backlog is alertable on its *age*
rather than being something only a support ticket would surface.

### Reconciling an attempt nobody answered

The billing sweep asks Paymob what became of an attempt that has gone quiet,
before it decides what to collect:

```
POST /api/auth/tokens                        { api_key }        → bearer token
POST /api/ecommerce/orders/transaction_inquiry
     Authorization: Bearer …                 { merchant_order_id }
```

`merchant_order_id` is the `special_reference` sent when the intention was
created, which is the payment id — an identifier committed before Paymob could
be told about it. The answer goes through the same translation and the same
settlement path a webhook does, so a callback and the reconciler arriving at
once are decided by `UNIQUE(provider, provider_event_id)` on `payment_events`:
one settles, the other records a duplicate.

| Answer | What happens |
| --- | --- |
| Succeeded | Settle the invoice, exactly as a callback would |
| Failed | Record the decline; the attempt stays spent |
| Pending | Leave it, ask again next sweep |
| No such reference | **Wait.** Believed only after a day of the same answer, then the attempt is returned and the invoice becomes collectible by a *new* one |
| Unreachable | Learn nothing. Never read as "no such reference" |

Nothing in this path can send a charge.

**The inquiry takes a fourth credential.** `PAYMOB_API_KEY` is the legacy API
key, not the secret key that creates intentions, and Paymob issues them
separately. Set it wherever `PAYMOB_MOTO_INTEGRATION_ID` is set. Without it an
attempt whose callback never arrived can never be resolved — the invoice is
still never charged again, but it also cannot be collected until somebody looks.
Watch `wasla_payment_reconciliation_total{outcome="pending"}` and
`wasla_oldest_pending_payment_age_seconds`.

### Saved cards

A card is saved when a customer ticks the box on the provider's page. It
arrives on a signed callback of `type: "TOKEN"` — a **different signature
scheme** from a transaction, eight fields rather than twenty — and is attached
to the workspace whose checkout produced it, resolved through the intention
reference we stored ourselves.

**No card data is stored.** `payment_methods` holds an opaque token, the
provider's id for it, the masked last four digits and the scheme name, and has
no column for a PAN, an expiry or a CVV.

| Action | Who |
| --- | --- |
| `GET /billing/payment-methods` | Workspace owner |
| `POST /billing/payment-methods/{id}/default` | Workspace owner |
| `DELETE /billing/payment-methods/{id}` | Workspace owner |

A card is revoked rather than deleted: payments point at it, and the record of
what collected last month should survive somebody tidying their account. The
first card saved becomes the default; later ones do not, because silently
moving renewals onto a card used once is a surprise.

### Why the Subscriptions Module is not used

Paymob documents one, and it is not the path taken. Both it and MIT require a
Moto integration, so availability did not decide it — fit did:

- Paymob subscription plans bill on a **fixed number of days** (7, 15, 30, 60,
  90, 180, 360). Wasla bills on calendar months, so a plan on `30` drifts away
  from the period this system charges for and the two disagree within a year.
- It would mean mirroring the plan catalogue into Paymob and keeping it in step
  through `change_plan` and `cancel` — a second source of truth for pricing.

MIT leaves the schedule here, where the product already decides it, and asks
the processor only to move money.

### What is still not built

Honest list.

- **Automatic card debits in a deployment without a MOTO integration.** The
  code ships; the capability is a merchant-level setting, so
  `can_charge_saved_methods` is false and renewals fall back to invoicing.
  `PAYMOB_API_KEY` belongs with it: without that credential an attempt whose
  callback never arrives cannot be reconciled, and the backlog is a metric
  rather than a duplicate debit.
- **Credit notes, and partial refunds *initiated here*.** `POST
  /billing/payments/{id}/refund` always asks for the payment's whole remaining
  balance: there is no field a caller can send to name a smaller figure, which
  is also why there is no field to name a larger one. A partial reversal can
  still *arrive* — issued from Paymob's own dashboard — and is applied, because
  the ledger has to match the bank whoever moved the money. What that does to
  the plan is set out under *Reversals* below.
- **Void.** Available at the provider seam and through Paymob's dashboard; not
  wired to an endpoint, because choosing between void and refund needs error
  semantics this integration has not seen.
- **Nothing has been verified against a live Paymob account.** Every claim in
  this document is about code checked against published documentation; the HTTP
  boundary is exercised against a mock transport, and the HMAC against the
  vendor's own published worked example. Read this as a description of the
  implementation, not of a transaction that happened.
