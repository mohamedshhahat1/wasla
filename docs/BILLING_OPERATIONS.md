# Billing operations

**Status: Implemented** (ADR-112, ADR-113). This guide is for platform staff
running billing. It covers the catalogue, custom plans, top-ups, subscribers,
invoices, payments, refunds, reconciliation and incidents. Everything here goes through
`/api/v1/platform/billing/*`. None of it needs SQL, and none of it should be done
with SQL.

The rules behind it are in [BILLING.md](BILLING.md#the-commercial-rules-adr-112)
and the endpoint list is in [API.md](API.md#platform-billing).

## Before you change anything

- **Roles.** `PLATFORM_OWNER` or `PLATFORM_ADMIN` for everything. Only
  `PLATFORM_OWNER` can delete a plan or a top-up product.
- **Every change needs a `reason`.** It is between 3 and 500 characters and goes
  into the audit log with your identity, your role, the request id, and the
  values before and after.
- **Every change needs the version you looked at.** Send `expected_revision`
  for a plan, subscription, invoice or payment, or `expected_version` for a new
  plan version. You get it from the `revision` field on the read. If the row
  changed since you read it, you get `409` and nothing is written. Read the row
  again, check what changed, then retry.
- **Lists** take `limit` (100 at most) and `offset`, and return `total`.
- **Nothing here charges a card.** Reconciliation only asks Paymob what
  happened. A manual payment records money you have already seen. A refund sends
  money back.

## The plan catalogue

| Task | Call | Notes |
| --- | --- | --- |
| See what can be limited | `GET /features` | Each key, its unit, whether it is enforced, and whether it is safe under concurrency. `period_messages` is a meter only; `allowed_channel_types` is channel policy; `whatsapp_numbers` is listed as retired (ADR-131). |
| Create a plan | `POST /plans` | `code` is permanent. The terms become version 1, and must state `allowed_channel_types` - the channel types a workspace on it may connect and automate, named, no wildcard (an empty list is none). `channel_connections` is the connection limit; `whatsapp_numbers` is refused. `trial_days` must be 0. The only currency is EGP. |
| Rename, reorder, show or hide it | `PATCH /plans/{id}` | Presentation only. A price or limit cannot be changed here. |
| Change price, interval or limits | `POST /plans/{id}/versions/preview`, then `POST /plans/{id}/versions` | Preview writes nothing. It tells you how many subscribers are on each version, for each limit how many workspaces are already above the new value, and for the channel types how many workspaces hold a connection of a type the new version removes. Publishing affects **new checkouts only**, from `effective_at`. |
| Move existing subscribers to the new terms | `POST /plans/{id}/migrations` with `confirm: false`, then `confirm: true` | The first call only counts the subscribers. The second records one migration. Each subscriber moves at their own next renewal: a cheaper version is applied at the boundary, and a pricier one is invoiced at the boundary and adopted only after that invoice is paid. |
| Stop selling a plan | `POST /plans/{id}/deactivate` | Existing subscribers keep it and keep renewing on it. |
| Delete a plan | `DELETE /plans/{id}` (owner only) | Refused with `409` if any subscription, invoice, scheduled change or migration has ever referenced it. Deactivate it instead. |

A version cannot be edited once published, not even with SQL: a database
trigger refuses any `UPDATE`. To correct a mistake, publish another version.

## Prices (ADR-116)

- **Add a yearly price to a standard plan** without copying its version:
  `POST /plan-versions/{version_id}/prices` with `{"billing_interval":
  "yearly", "amount": "...", "reason": "..."}`. The version's limits are the
  same for monthly and yearly customers. Wasla invents no annual price: every
  yearly price exists because an operator published it.
- **Change a price:** `POST /prices/{id}/retire`, then create the new one.
  Nobody already subscribed moves; `GET /prices/{id}` shows how many
  subscriptions, scheduled changes, invoices and offers still name the old one.
  Moving existing subscribers to the new price is a separate, explicit version
  migration.
- **Never** `UPDATE plan_prices` by hand: a trigger refuses any change to a
  price's terms, and un-retiring one.
- **Support questions** - "is this customer paid up?" - read
  `current_period_end` (the paid-through date) and `billing_interval` on
  `GET /subscriptions/{id}`; "why are they out of AI turns?" reads
  `usage_period_*`, the current monthly cycle.
- A price scheduled for a subscriber and later retired is still honoured at the
  boundary: what the customer agreed to does not change under them.

## Custom plans (ADR-113)

A custom plan is a normal plan with `scope: tenant`, restricted to one company
for ever. Everything about plans above applies to it; this is the one-screen
flow for creating one.

| Task | Call | Notes |
| --- | --- | --- |
| See the company first | `GET /tenants/{tenant_id}/summary` | Plan, version, price, period, next renewal, the seven limits with usage, live top-ups and grants, recent invoices, payments, incidents and timeline. |
| Preview the terms | `POST /tenants/{tenant_id}/custom-plan/preview` | Writes nothing. Shows current versus proposed for each of the seven keys, what is in use, which proposals are already below usage, the limits inherited from the current plan (`agents`, `owned_workspaces`), when it would take effect and the next charge. |
| Create it | `POST /tenants/{tenant_id}/custom-plan` | `code`, `name`, `price`, `currency` (EGP), `billing_interval`, **all seven limits** and `allowed_channel_types` are required. `null` is unlimited, `0` is none, and leaving a key out is refused. Storage is in bytes (GiB x 1024^3). |
| Offer it to the company (the normal way to sell it) | the same, with `financial_basis: "customer_checkout"` and optionally `offer_expires_at` | Creates the plan and an **offer** (ADR-114). Nothing is assigned; the owner sees the terms, clicks Accept & Pay, and the plan applies when Paymob confirms the payment. |
| Create and assign at renewal | the same, with `assign_to_tenant: true`, `assignment_mode: "next_renewal"` and `expected_subscription_revision` | **Free custom plans only.** A priced custom plan the company does not already hold is refused here (422): it would be billed, possibly to a saved card, at a price the customer never accepted. Offer it instead. |
| Create and assign now | `assignment_mode: "now"` | A free custom plan applies at once. A priced one needs `financial_basis`: `customer_checkout` (nothing is assigned: it makes an **offer** the owner accepts and pays; ADR-114), `manual_payment` (with the payment details you have seen) or `complimentary` (with `complimentary_until`). |
| Change its terms | `POST /plans/{id}/versions` | A new immutable version. The company stays on its version until you migrate it (`POST /plans/{id}/migrations` or `change-plan` with `next_renewal`). |
| Stop offering it | `POST /plans/{id}/deactivate` | The company keeps it and keeps renewing on it. |
| List a company's custom plans | `GET /plans?scope=tenant&tenant_id=…` | |

A custom plan cannot be assigned to another company: `change-plan` answers
`422 custom_plan_not_available_for_workspace`, and the database refuses it even
by SQL. It cannot be made public (`PATCH` with `is_public: true` is `422`), and
its owning company cannot be changed.

### Offers (ADR-114)

| Task | Call | Notes |
| --- | --- | --- |
| Offer a version (e.g. a new v2) | `POST /tenants/{tenant_id}/custom-offers` `{plan_version_id, expires_at?, reason}` | Only the company's own priced custom plan. One open offer per company (409 otherwise); free versions are assigned, not offered (422). |
| See a company's offers | `GET /tenants/{tenant_id}/custom-offers` | Status, full terms, accepted/activated/declined/cancelled/expired times. |
| Withdraw an offer | `POST /custom-offers/{offer_id}/cancel` `{expected_revision, reason}` | From `offered` or `pending_payment`. A page the customer already opened and pays anyway is **held** (`refused_settlement` incident) and must be refunded. |

An offer is `active` only when Paymob's signed callback, or a transaction
inquiry recovering a lost one, settles its invoice. A browser redirect never
activates anything. If a customer says they paid and the offer still reads
`pending_payment`, run reconciliation for its payment (`POST /reconciliation/{payment_id}/run`, or wait for
the sweep): it asks Paymob and settles through the same path.

## Top-ups (ADR-113)

A top-up adds allowance to one of the seven keys until the end of the current
billing period. It never changes the plan, its price or the renewal, and it
never renews.

### The catalogue

| Task | Call | Notes |
| --- | --- | --- |
| List products | `GET /topups?scope=…&tenant_id=…&entitlement_key=…&channel_type=…&active=…` | `code=` finds one product by its code (any case); `search=` matches part of a code or name, any case (`%` and `_` match themselves). |
| Create a product | `POST /topups` | `code` (permanent), `name`, `entitlement_key` (one of the seven), `quantity` (> 0; storage in bytes), `price`, `currency` (EGP), `scope` (`global`, or `tenant` with `tenant_id`), `is_public`, `reason`. A `tenant` product is visible, purchasable and grantable to that company only. For `channel_connections`: `channel_type` (omit for a general slot any allowed type may use) and `eligible_plan_codes` (omit or `[]` for every plan). |
| Price a placeholder | `PATCH /topups/{id}` with `price`, then `POST /topups/{id}/activate` | Migration 0098 seeds six channel products - `channel-connection-1` (general) and `whatsapp-`, `instagram-`, `messenger-`, `telegram-`, `tiktok-connection-1` - each +1, inactive and **unpriced**. Activating one with no price is refused (409, and a database check). Wasla invents no price. |
| Change price, quantity, name, visibility, channel type or eligible plans | `PATCH /topups/{id}` with `expected_revision` | For new purchases only. Every existing purchase keeps what it was bought at, its channel type included. |
| Stop selling it | `POST /topups/{id}/deactivate` | Existing purchases and grants are untouched. |
| Delete it | `DELETE /topups/{id}` (owner only) | Refused with `409` once anybody has bought it. Deactivate instead. |

### Purchases, grants and refunds

| Task | Call | Notes |
| --- | --- | --- |
| Find purchases | `GET /topup-purchases?tenant_id=…&status=…&source=…&entitlement_key=…&channel_type=…` | `status=paid` lists money taken and not granted - each has an incident and needs a refund. `status=withdrawn` lists grants staff took back. |
| Give allowance without payment | `POST /tenants/{tenant_id}/topups/grant` | `entitlement_key`, `quantity`, `valid_until: "current_period_end"`, `reason`, `expected_subscription_revision`, and for channel slots optionally `channel_type`. Recorded as `source: platform_grant` with no invoice, payment or price. Refused for a key the plan leaves unlimited, and for a channel type the company's plan does not allow (422). |
| Decide a refunded top-up | `POST /topup-purchases/{id}/refund-review` | Only for `status: refund_review`. `decision: keep` leaves the allowance; `withdraw` removes it from the limit from now on. Withdrawing deletes nothing and never makes usage negative - the company is just over its limit until it fits again. |
| Take a grant back (ADR-132) | `POST /topup-purchases/{id}/withdraw` with `tenant_id`, `reason`, `expected_revision` | A platform grant that still counts, given by mistake or no longer due. It stops counting now. `tenant_id` must be the grant's company (else 404). A paid purchase is refused (409) - refund it instead. See below for what the company sees. |

To refund a top-up, refund its payment with `POST /payments/{id}/refund` as
usual. Nothing is withdrawn automatically: before the grant a full refund cancels
the purchase, after it the purchase waits in `refund_review` for your decision.
A top-up refund never changes the plan and never starts dunning.

### Withdrawing a grant (ADR-132)

What happens depends on the key:

- **Channel slots.** If the company still fits without the slot, nothing else
  happens. If it does not, a capacity reduction opens with cause
  `grant_withdrawn`, naming the grant, and the ordinary 7-day grace below: the
  owner chooses what to keep, otherwise the fallback runs at the grace end.
  The withdrawal itself never disables a connection.
- **Usage keys** (AI turns, messages, campaign messages). The allowance drops at
  once. Turns and messages already used stay used - nothing is refunded or
  removed - and if the company is now at or past its new limit, the next AI
  turn is handed to a person exactly as when an allowance runs out.
- **Other capacity keys** (storage, team members, knowledge documents). The limit
  drops; nothing is deleted; new creation is refused until the company fits.

The response names the limit before and after and the reduction the company is
now in, if any. The withdrawal is audited (`billing_topup_grant_withdrawn`)
and visible in the purchase's history:
`GET /api/v1/platform/audit-logs?target_type=topup_purchase&target_id={id}`.
There is no operation to shorten a grant to a later date: withdraw it, or let
it end with the period.

### Channel slots (ADR-131)

A channel top-up is sold and granted exactly like any capacity top-up. What is
particular to it:

- **Typed or general.** A general slot (`channel_type` omitted) serves any
  channel type the company's plan allows; a typed slot serves only its own. A
  top-up never opens a channel type: a typed product for a type the company's
  plan does not include is not listed to it, its checkout answers 422, and so
  does a grant.
- **Eligible plans.** A product with `eligible_plan_codes` is invisible to a
  company on any other plan (404 at checkout). Starter may buy one if you make
  it eligible.
- **Valid until the billing term ends** - a year on a yearly price - like every
  capacity top-up.

## Channel capacity reductions (ADR-131)

When a company's channel capacity falls below its active connections - a
downgrade or migration taking effect, a channel top-up or grant expiring, a
refund you withdrew, a grant you withdrew - a **reduction** opens.

**The reduction queue** (ADR-132):

| Task | Call |
| --- | --- |
| Everyone in a grace, soonest end first | `GET /capacity-reductions?status=pending_selection` |
| Graces ending this week | `GET /capacity-reductions?status=pending_selection&grace_ends_before=…` |
| By cause | `GET /capacity-reductions?cause=grant_withdrawn` (repeatable, as is `status`) |
| One reduction: kept, disabled, the owner's pre-selection, what the fallback would do | `GET /capacity-reductions/{id}` |
| One company's history | `GET /tenants/{tenant_id}/capacity-reductions` |
| Exactly what the company's capacity page shows | `GET /tenants/{tenant_id}/channel-capacity` |
| The company's connections, as it lists them | `GET /tenants/{tenant_id}/channel-connections` |

Each queue row shows the company's name, its active connections now and how
many the fallback would disable if the grace ended now. The summary
(`GET /tenants/{tenant_id}/summary`) still carries the latest reduction. You can
read a reduction but not act on it: extending, shortening or resolving one on a
customer's behalf is not offered (a product decision not yet made).

What to tell the customer during the grace (`CHANNEL_CAPACITY_GRACE_DAYS`, 7):

- Every connection keeps working - messages in and out, the AI, campaigns.
- No new connection can be made and none re-enabled until they fit.
- An owner chooses which connections to keep on the billing page
  (`POST /billing/channel-capacity/selection`); the others are disabled, not
  deleted: history, conversations, contacts and the number's claim stay, and
  any of them can be enabled again once there is a free slot.
- With no choice by the end of the grace, connections of a type their plan no
  longer allows are disabled first, then the newest; the oldest are kept.
- Buying more slots, or an upgrade, during the grace ends it with nothing
  disabled.

You do not disable connections by hand, and nothing on the platform API
bypasses the capacity guard. A suspended, cancelled or expired company never
gets a reduction: it keeps every connection, reads over its limit, and its
automation on a channel the default plan lacks stops until it pays.

## Subscribers

| Task | Call |
| --- | --- |
| Find one | `GET /subscriptions?tenant_id=…&status=…&plan=…&renews_before=…` |
| See its history | `GET /subscriptions/{id}/timeline`: audit entries, invoices and payments in order, oldest first |
| Change the plan at the next renewal | `POST /subscriptions/{id}/change-plan` with `mode: "next_renewal"` |
| Change the plan now | `mode: "now"`. For a priced version you must also send `financial_basis`, which is either `manual_payment` (with `manual_payment` details of money you have seen) or `complimentary` (with `complimentary_until`). There is no option to grant a paid plan without saying what pays for it. |
| Cancel | `POST /subscriptions/{id}/cancel`. This takes effect at the period end unless you send `immediately: true`. |
| Undo a pending cancellation | `POST /subscriptions/{id}/resume` |

## Invoices

| Task | Call |
| --- | --- |
| Find unpaid bills | `GET /invoices?status=open&purpose=renewal` |
| Record a bank transfer | `POST /invoices/{id}/payments` with `amount`, `currency`, `method` and `reference`. The payment is settled like a card payment: it restores a past-due or suspended workspace and grants a purchased plan. It is refused if it is more than the amount outstanding. An `uncollectible` invoice can be paid only with `recover_uncollectible: true`. |
| Withdraw a bill | `POST /invoices/{id}/void`. This is refused if money is already on the invoice (refund it first) or if an automatic charge is still waiting for Paymob's answer. When voiding the current renewal of a workspace that is behind, you must choose a `subscription_policy`: `unchanged` leaves the workspace as it is, `cancel` ends it, and `waive` keeps it served and records a waiver. |

## Payments and refunds

`GET /payments/{id}` shows the provider's facts: the transaction id, the order
id, the intention id, the integration and the test/live mode. It never shows a
card token or the raw callback.

**Refunds are made here and nowhere else.** When a workspace owner uses the
refund button, a `refund_requested` incident is raised and no money moves.

`POST /payments/{id}/refund` takes an `amount` (for a partial refund),
`currency`, `reason` and `expected_revision`. Paymob is asked to reverse the
money, and nothing is marked as refunded until Paymob's signed callback confirms
it. After confirmation:

- **A partial refund you asked for** is treated as goodwill. The invoice stays
  paid and the plan is not changed.
- **A full refund** withdraws the plan the payment bought and voids the invoice.
- **A partial reversal made from Paymob's dashboard** (not requested here)
  reopens the invoice for the rest of the amount, and dunning starts on it.

If a refund was requested but never confirmed (`refund_pending: true` for more
than a day), the callback probably did not arrive. Check the payment in
Paymob's dashboard before asking again.

## Reconciliation

`GET /reconciliation` counts payments in each state that needs attention:
hosted checkouts pending past the grace period, automatic charges whose outcome
is unknown, issued invoices open for more than thirty days, and open incidents
of each kind.

The billing worker asks Paymob about pending hosted checkouts on every pass, up
to `BILLING_HOSTED_RECONCILIATION_MAX_AGE_SECONDS` (default 7 days). It looks the
transaction up by our merchant order id. A payment that succeeded is settled
through the normal settlement path, and a saved card from that order is
recovered by Card Token Inquiry. To ask about one payment now, use `POST
/reconciliation/{payment_id}/run`. This never charges.

## Incidents

`GET /incidents?status=open&kind=…` lists them. Each incident names its
workspace, payment, invoice, amount and provider transaction.

| Kind | What happened | What to do |
| --- | --- | --- |
| `duplicate_payment` | Money arrived for an invoice that was already paid, or a second time for the same purchase. Nothing was granted twice. | Confirm the transaction in Paymob, refund it from `POST /payments/{id}/refund`, then resolve the incident. |
| `refused_settlement` | A payment succeeded, but the invoice could not accept it (voided, uncollectible or draft), or the customer cancelled after opening the checkout. | Refund it, or record it against the correct invoice if the customer agrees. |
| `mismatched_callback` | A correctly signed callback named a payment whose order, integration, mode, amount or currency does not match. | Treat it as a security event. Check whether another system shares the Paymob account or the HMAC secret. |
| `unknown_callback` | A signed callback named nothing that Wasla created. | The same as above. It may be another application on the same Paymob account. |
| `permanent_provider_error` | Paymob permanently refused a saved-card renewal, for example because the workspace has no billing e-mail or the integration was rejected. | Fix the cause (usually the owner's e-mail address). The invoice waits in dunning and the customer can still pay by checkout. |
| `recovered_by_reconciliation` | A payment whose callback never arrived was found and settled by the reconciler. | Nothing is needed for the payment. If there are many, check the callback URL. |
| `refund_requested` | A workspace owner asked for a refund. | Decide, and refund or reply. |
| `topup_paid_but_not_granted` | A top-up was paid but could not be granted: its period had ended, or the subscription was not active. The customer holds nothing for their money. | Refund the payment, or - if the customer agrees - give the allowance as a platform grant and refund anyway. |
| `topup_duplicate_payment` | Money arrived twice for one top-up page, or for a top-up invoice already paid. Nothing was granted twice. | Refund the second transaction. |
| `topup_refund_after_consumption` | A granted top-up was refunded after some of its allowance was used. Nothing was withdrawn. | Decide with `refund-review`: usually keep it. |
| `topup_entitlement_reversal_blocked` | A granted top-up was refunded before any of it was used. Nothing was withdrawn, because that is never automatic. | Decide with `refund-review`: usually withdraw it. |
| `topup_unknown_callback` | A top-up invoice was paid with no purchase behind it. The checkout path cannot produce this. | Treat as a bug; refund and report it. |
| `custom_plan_scope_mismatch` | Money arrived for an invoice that would put a company on another company's custom plan. It was held. | Treat as a bug or tampering; refund and investigate. |

Resolve an incident with `POST /incidents/{id}/resolve` and a `note`. The note
is audited.

## Alerts

The rules are in the `wasla-billing` and `wasla-billing-topups` groups of
`deploy/monitoring/alerts.yml`. None of them fires on an ordinary card decline.

| Alert | Severity | First look |
| --- | --- | --- |
| `BillingDuplicatePayment` | critical | Duplicate payment incidents |
| `BillingMismatchedCallback` | critical | Mismatched callback incidents; HMAC secret; shared Paymob account |
| `BillingCheckoutProviderFailing` | critical | Paymob secret key, card integration id, Paymob status |
| `BillingPermanentChargeFailure` | warning | Permanent provider error incidents |
| `BillingInvalidCallbackSpike` | warning | `PAYMOB_HMAC_SECRET` rotation; who is posting to the callback URL |
| `BillingRenewalFailureSpike` | warning | MOTO integration; `recurring.*` log lines |
| `BillingHostedReconciliationFailing` | warning | `PAYMOB_API_KEY`; Paymob reachability |
| `BillingHostedPaymentStuck` | warning | The public callback URL; `GET /reconciliation` |
| `BillingTopupPaidNotGranted` | critical | `topup_paid_but_not_granted` incidents; refund the payment |
| `BillingTopupSettlementFailure` | warning | `topup_duplicate_payment` and `topup_unknown_callback` incidents |
| `BillingTopupCallbackMismatchSpike` | warning | Mismatched callback incidents on top-up payments |
| `BillingTopupReconciliationStuck` | warning | `GET /topup-purchases?status=paid` |
| `BillingCustomPlanFailureSpike` | warning | API error logs for `/tenants/*/custom-plan` |
| `AITurnHoldsStuck` | warning | AI turn holds past their TTL are not being released: is the billing worker running? (`docs/RUNBOOK.md`) |
| `ChannelCapacityAutoDisableSpike` | warning | Many connections disabled by expired graces in an hour: which reductions resolved automatically, and why owners did not choose |
| `AITurnLateChargeSpike` | warning | Holds released by the sweep and charged later: `AI_TURN_HOLD_TTL_SECONDS` is shorter than real turns take |

## Configuration you may need

| Setting | Purpose |
| --- | --- |
| `PAYMOB_INTEGRATION_IDS` | The card and wallet integrations offered at checkout. Their callbacks are accepted. |
| `PAYMOB_CALLBACK_INTEGRATION_IDS` | Further integrations whose callbacks may settle a checkout. Leave this empty unless Paymob routes payments through an integration you do not offer directly. |
| `PAYMOB_MOTO_INTEGRATION_ID` | Needed for saved-card renewals. |
| `PAYMOB_API_KEY` | Needed for transaction inquiry and Card Token Inquiry. Without it, lost callbacks are never recovered. |
| `BILLING_HOSTED_RECONCILIATION_MAX_AGE_SECONDS` | How long a pending checkout is still looked up. |
| `AI_TURN_HOLD_TTL_SECONDS` | How long an AI turn's hold counts before the sweep releases it (900). Keep it above the longest a turn takes. |
| `CHANNEL_CAPACITY_GRACE_DAYS` | How long a company keeps every connection while it chooses which to keep (7). |

A test key (`sk_test_…`) produces payments marked `test`, and a live key
produces payments marked `live`. A callback whose mode does not match its
payment is refused, so test traffic can never settle a live invoice, and live
traffic can never settle a test one.
