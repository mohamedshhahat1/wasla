# Billing operations

**Status: Implemented** (ADR-112). This guide is for platform staff running
billing. It covers the catalogue, subscribers, invoices, payments, refunds,
reconciliation and incidents. Everything here goes through
`/api/v1/platform/billing/*`. None of it needs SQL, and none of it should be done
with SQL.

The rules behind it are in [BILLING.md](BILLING.md#the-commercial-rules-adr-112)
and the endpoint list is in [API.md](API.md#platform-billing).

## Before you change anything

- **Roles.** `PLATFORM_OWNER` or `PLATFORM_ADMIN` for everything. Only
  `PLATFORM_OWNER` can delete a plan.
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
| See what can be limited | `GET /features` | Each key, its unit, whether it is enforced, and whether it is safe under concurrency. `period_messages` is a meter only. |
| Create a plan | `POST /plans` | `code` is permanent. The terms become version 1. `trial_days` must be 0. The only currency is EGP. |
| Rename, reorder, show or hide it | `PATCH /plans/{id}` | Presentation only. A price or limit cannot be changed here. |
| Change price, interval or limits | `POST /plans/{id}/versions/preview`, then `POST /plans/{id}/versions` | Preview writes nothing. It tells you how many subscribers are on each version, and for each limit how many workspaces are already above the new value. Publishing affects **new checkouts only**, from `effective_at`. |
| Move existing subscribers to the new terms | `POST /plans/{id}/migrations` with `confirm: false`, then `confirm: true` | The first call only counts the subscribers. The second records one migration. Each subscriber moves at their own next renewal: a cheaper version is applied at the boundary, and a pricier one is invoiced at the boundary and adopted only after that invoice is paid. |
| Stop selling a plan | `POST /plans/{id}/deactivate` | Existing subscribers keep it and keep renewing on it. |
| Delete a plan | `DELETE /plans/{id}` (owner only) | Refused with `409` if any subscription, invoice, scheduled change or migration has ever referenced it. Deactivate it instead. |

A version cannot be edited once published, not even with SQL: a database
trigger refuses any `UPDATE`. To correct a mistake, publish another version.

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

Resolve an incident with `POST /incidents/{id}/resolve` and a `note`. The note
is audited.

## Alerts

The rules are in the `wasla-billing` group of `deploy/monitoring/alerts.yml`.

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

## Configuration you may need

| Setting | Purpose |
| --- | --- |
| `PAYMOB_INTEGRATION_IDS` | The card and wallet integrations offered at checkout. Their callbacks are accepted. |
| `PAYMOB_CALLBACK_INTEGRATION_IDS` | Further integrations whose callbacks may settle a checkout. Leave this empty unless Paymob routes payments through an integration you do not offer directly. |
| `PAYMOB_MOTO_INTEGRATION_ID` | Needed for saved-card renewals. |
| `PAYMOB_API_KEY` | Needed for transaction inquiry and Card Token Inquiry. Without it, lost callbacks are never recovered. |
| `BILLING_HOSTED_RECONCILIATION_MAX_AGE_SECONDS` | How long a pending checkout is still looked up. |

A test key (`sk_test_…`) produces payments marked `test`, and a live key
produces payments marked `live`. A callback whose mode does not match its
payment is refused, so test traffic can never settle a live invoice, and live
traffic can never settle a test one.
