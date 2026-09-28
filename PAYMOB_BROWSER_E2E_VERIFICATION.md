# Wasla Paymob Browser E2E Verification

Date: 2026-09-27 (UTC). Verification only: no application code, migration or
configuration file in this repository was changed. Every money-moving step in a
scenario marked **REAL** was a real Paymob **Test-mode** transaction, paid on
Paymob's hosted checkout in Chrome (or, for MOTO/MIT, charged by the real
billing worker), with the result delivered by Paymob's real signed callback
through ngrok or recovered by Paymob's real Transaction Inquiry.

Only safe identifiers appear here: Wasla UUID prefixes, Paymob order and
transaction ids, integration ids, amounts, masked last-4, HTTP statuses.

## 1. Source state

| Item | Value |
|---|---|
| Worktree | `E:\wasla-database-remediation` |
| Branch | `database-findings-remediation` |
| PAYMOB_E2E_SOURCE_HEAD | `91ba783` (report commit of the remediation); final **code** HEAD `e15965f`, verified as its parent |
| Tracked tree before and after | clean (`git status --short` empty) |
| Alembic | `0001 → 0080`, `alembic heads` = `0080 (head)` (single), `alembic current` = `0080`, `alembic check` = no new operations, `scripts.db_preflight verify` = ok |
| Merged / pushed | no / no |

## 2. Environment

| Component | Value |
|---|---|
| API | `uvicorn app.main:app` from this worktree on `127.0.0.1:57455`, `ENVIRONMENT=local`, `BILLING_PROVIDER=paymob`, JSON logs |
| Worker | the real `BillingWorker.run_once()` (advance → top-up expiry → offer expiry → reconcile → collect → chase → suspend), invoked per scenario |
| PostgreSQL | dedicated Compose project `wasla-paymob-browser-e2e` (pgvector/pg16, `127.0.0.1:55480`), evidence database **`wasla_paymob_browser_e2e`** (kept) |
| Redis | dedicated container in the same project (`127.0.0.1:56380`), logical DB 5 |
| Regression DB | `wasla_paymob_regression_test` (same container, disposable) |
| Paymob | region `egypt`; `PAYMOB_INTEGRATION_IDS=5885262`, `PAYMOB_MOTO_INTEGRATION_ID=5934829`; API key configured (inquiry enabled) |
| Public URL | `APP_PUBLIC_URL=https://recycler-gondola-numeral.ngrok-free.dev` |

**Code-path correction (recorded, not hidden).** The first worker sweeps were
launched as a *script*, which put the script's own directory first on
`sys.path`; `import app` then resolved to the editable install at `E:\wasla`
(pre-remediation `11cf44b`), not this worktree. The API (`python -m uvicorn`)
was never affected. Detected at P09 from a traceback path; fixed by pinning
`PYTHONPATH` and asserting `app.__file__` inside every sweep (`APP_PATH
E:\wasla-database-remediation\app\__init__.py` is printed by each later sweep).
**Every sweep-dependent scenario was re-run on the remediation code** (P05,
P06, P07, P08, P69). The earlier old-code runs are *not* counted as evidence;
their Paymob transactions are real and appear in the ledger, marked `(old
worker)`. The one failed old-code reconciliation attempt rolled back atomically
(`a settled invoice keeps the terms it was settled on`) and changed nothing.

## 3. Secret handling

* Credentials were read from `E:\secrets.txt` by a launcher that loads only
  `PAYMOB_SECRET_KEY`, `PAYMOB_PUBLIC_KEY`, `PAYMOB_HMAC_SECRET` and
  `PAYMOB_API_KEY` into the child process environment and refuses to start
  unless the keys are `…sk_test_…` / `…pk_test_…`. No value was printed; the
  file was inspected by key **names** only. `OPENAI_API_KEY`,
  `META_ACCESS_TOKEN` and `META_APP_SECRET` were removed from the child
  environment.
* The secrets file is outside the repository tree (`E:\`), so it cannot be
  tracked; `.gitignore` also ignores `.env`. Neither was copied, committed or
  placed in a Docker build context (no image was built).
* JWT secret, credential-encryption key, fingerprint key and Postgres password
  were generated fresh for this run and live only in the session scratch area.
* Hosted-checkout URLs (which carry a per-page client secret) were written only
  to a scratch file and opened through a localhost redirector, so they never
  appeared in command output.
* Card data: only Paymob's **published** Test cards (Mastercard …2346, Mastercard
  …0008, Visa …1111; docs "Test Credentials", last updated 2026-06-01) were
  typed into Paymob's hosted page. Chrome form persistence was not used; Wasla
  never receives a PAN or CVV.

## 4. Paymob Test account / integrations

Observed in the Paymob dashboard in Chrome (existing session), nothing changed:

* Mode toggle **Test mode**, banner "You are in test mode. No real money will be
  charged."; account shows *Complete Onboarding* → Live is not enabled.
* Integrations: **5885262** VPC/EGP `online` (hosted card), **5934829** VPC/EGP
  `moto` (MIT), 5897483 UIG/EGP `online_new` (wallet, not configured in Wasla).
* 5885262 Webhook URL `https://recycler-gondola-numeral.ngrok-free.dev/api/v1/webhooks/paymob`,
  Redirect URL `…/billing/checkout/return` - already pointing at the active
  tunnel; Wasla also sends `notification_url`/`redirection_url` per intention.
* Dashboard counters over the session: transactions 29 → 59, refunded
  transactions 2 → 5.

Official contract re-checked on developers.paymob.com (page "Last Updated"):

| Page | Updated | Relevant contract |
|---|---|---|
| Test Credentials | 2026-06-01 | published Test cards (exp 01/39, CVV 123) |
| Transaction Inquiry (order id / reference) | 2026-06-28 | `POST api/ecommerce/orders/transaction_inquiry`, body `auth_token` + `order_id` or `merchant_order_id`; **returns the most recent transaction of the order** |
| Transaction Callbacks | 2026-06-01 | callbacks carry `is_refunded`, `refunded_amount_cents`; "a payment transaction can have more than one partial refund transaction" |
| Refund | 2026-06-01 | partial refunds supported, **repeatedly until the full amount is refunded** |
| Create Intention / Unified Checkout / HMAC / Card-token HMAC / MIT | per `CUSTOM_PLANS_TOPUPS_IMPLEMENTATION.md` §15 (2026-09-25) | unchanged in behaviour observed today |

Test/live proof per transaction: every hosted page opened was
`eg.checkout.paymob.com/?publicKey=egy_pk_test_…`; every callback `is_live:
false`; every payment row `provider_mode = test` (ledger L13 = 0).

## 5. ngrok callback proof

| Check | Result |
|---|---|
| ngrok hostname | `recycler-gondola-numeral.ngrok-free.dev` → `127.0.0.1:57455` |
| Callback route (from `app/api/v1/payment_webhooks.py`, prefix `/webhooks/paymob` under `/api/v1`) | `POST /api/v1/webhooks/paymob` |
| Local `/health/live`, `/health/ready` | 200 / 200 (postgresql up, redis up) |
| Public `https://…/health/ready` | 200 |
| Unsigned POST to public callback route | **403** `permission_denied` |
| API stopped (P09, T07) | public requests **502**; Paymob's callback recorded as 502 by ngrok |

## 6. Chrome / browser methodology

Claude-in-Chrome drove the user's Chrome in a dedicated tab. For each hosted
payment: Wasla API call → hosted URL stored → Chrome opened it via the local
redirector → the page's test-key prefix was visible in the tab URL → published
Test card typed into Paymob's page → field values verified (last-4/expiry only)
before the page's own *Pay* button was pressed → Visa and Mastercard …0008 went
through Paymob's **ACS Emulator for 3DS V2** (result chosen explicitly) →
authoritative state read from PostgreSQL after the signed callback or inquiry.
The success redirect was never treated as evidence; ngrok's browser
interstitial on the return URL is irrelevant because no Wasla route reads it.

## 7. Published subscription payments

| ID | Scenario | Result |
|---|---|---|
| P01 | A buys Pro (99.00) at hosted checkout | **PASS — REAL PAYMOB TEST.** Before callback: payment `pending`, invoice `open`, A on Starter. Signed callback 200 → `applied`; txn 542748905, order 619136657, 9900 cents EGP, integration 5885262, `is_live=false`, MasterCard …2346. Invoice paid 99/99 with `paid_at`; subscription Pro v1, period anchored at settlement; one payment, one event. |
| P02 | Redirect is not settlement | **PASS — REAL PAYMOB TEST.** In P09 the browser showed "Thanks for your payment" and the redirect carried `success=true` for txn 542757558, while Wasla still had payment `pending`, invoice `open`, D on Starter until inquiry settled it. P68: no Wasla route reads the return URL (404). |
| P62 | Paid upgrade Pro → Business (299.00) | **PASS — REAL PAYMOB TEST.** txn 542749810; new full period from settlement (09:25:44 → +1 month), no proration, the paid Pro invoice unchanged. |
| P65 | Free plan checkout | **PASS — APP E2E.** `POST /billing/checkout {starter}` → 422 "That plan does not require payment."; 0 invoices, 0 payments, 0 Paymob calls. |
| P66 | Price/amount/currency/version tampering | **PASS — APP E2E.** `amount`, `price`, `currency`, `plan_version_id` → 422 `extra_forbidden`; same for top-up `quantity`/`price` and offer `price`; 0 rows, 0 Paymob calls. |
| P63 | Scheduled downgrade (B Pro → Starter) | **PASS — APP E2E.** Scheduled for period end (`scheduled_plan_version_id` set, still Pro); 0 payments created. |
| P64 | Period-end cancel (F) / platform immediate cancel (G) | **PASS — APP E2E.** F `cancel_at_period_end`, still active; G `cancelled` with `ended_at`; 0 payments created. |

## 8. Saved-card / TOKEN verification

| ID | Scenario | Result |
|---|---|---|
| P04 | Save Card during A's Business checkout | **PASS — REAL PAYMOB TEST.** Real TOKEN callback (order 619138197, `xxxx-xxxx-xxxx-2346`, MasterCard) → 200. Card: workspace A, brand MasterCard, masked PAN only, token stored as a `v1` AES-GCM envelope (row-bound context), fingerprint present, default. |
| P47 | Replay genuine TOKEN callback | **PASS — REAL PAYMOB TEST.** Replay → 200, `payment_method_created: false`; still one card. |
| — | Further real TOKEN callbacks | orders 619150600 (A, …2346), 619152613 (E, …2346), 619173850 (A, …0008): each created exactly one card in the paying workspace. |
| P46 | Default-card behaviour | **PASS — APP E2E on real saved cards.** A with two active cards: make-default …0008 then …2346 → always exactly one active default; revoking the default leaves **no** default (documented: renewals charge only a default card; otherwise `no_card` → hosted renewal); revoked card → 409 on make-default; E making A's card default → 404; replacement default chosen explicitly. Max active defaults per tenant = 1. |

## 9. MOTO/MIT renewal

| ID | Scenario | Result |
|---|---|---|
| P05 | A renewal from saved card (remediation code) | **PASS — REAL PAYMOB TEST.** Period moved (this DB only) to end one minute ago; real sweep issued the renewal invoice at Business 299.00, sent a MOTO intention on **5934829** and a pay request with the saved token; txn 542761282, order 619151856, `is_live=false`; the card belongs to A; invoice paid, period advanced exactly one calendar month. |
| P06 | Replay MOTO callback | **PASS — REAL PAYMOB TEST.** Replay → `duplicate`; events, invoices, automatic payments and period end unchanged. |
| P69 | Worker retries | **PASS — APP E2E.** Two further sweeps after each renewal: `SWEEP_HANDLED 0`, no new invoice or charge. |
| Custom | E custom plan renewal from saved card | **PASS — REAL PAYMOB TEST.** txn 542762646 on 5934829 at the custom price 250.00, same v1, E's own card; second sweep handled 0. |

## 10. Hosted no-card renewal

| ID | Scenario | Result |
|---|---|---|
| P07 | A revoked card → renewal due (remediation code) | **PASS — REAL PAYMOB TEST.** Automatic payments before/after the sweep: 1 → 1 (0 MOTO attempts); summary `automatic_renewal: false`, `payment_required` → `POST /billing/checkout`; paid in Chrome (Save Card), txn 542760797; applied once; 0 open invoices. |
| P08 | B, never saved a card (remediation code) | **PASS — REAL PAYMOB TEST.** 0 MOTO attempts; hosted renewal paid, txn 542759772; one renewal applied; sweep afterwards handled 0. |
| P12b | F custom plan, no card | **PASS — REAL PAYMOB TEST.** 0 MOTO attempts; hosted renewal at the same v1 price 180.00, txn 542764783; exactly one renewal. |

## 11. Custom plan offers

| ID | Scenario | Result |
|---|---|---|
| P11 | Paid custom offer with Save Card (E, 250.00) | **PASS — REAL PAYMOB TEST.** Offer shown with exact immutable terms (price, EGP, monthly, v1, all 7 limits, `starts: on_payment`); F cannot see or accept it (404), F cannot buy the code (422 "No such plan."), E buying by code → 422 pointing at the offer, E sending a `price` → 422. Accept → invoice pinned to v1 and the offer, 250.00 from the version; offer `offered → pending_payment`, E still Starter. TOKEN + TRANSACTION callbacks (txn 542762134) → offer `active` once, E on v1 with exactly the offered limits, card saved to E. |
| P12 | Paid custom offer without saving (F, 180.00) | **PASS — REAL PAYMOB TEST.** txn 542764035; offer active; **no** payment method created; renewal hosted (§10). |
| P13 | Zero-price / complimentary assignment (G) | **PASS — APP E2E.** 0 checkouts, 0 Paymob calls, 0 invoices, 0 payments; audited `billing_custom_plan_created` / `subscription_plan_changed` / `billing_custom_plan_assigned` by `platform_staff`, role `platform_owner`, with reason and `plan_version_id`. See finding PAY-E2E-02 (misleading extra `system` row). |
| P14 | Customer declines | **PASS — APP E2E.** Offer `declined`; later accept → **409** "This offer can no longer be accepted." (ADR-114); 0 invoices, 0 payments, plan unchanged. |
| P15 | Offer withdrawn after checkout opened, then paid | **PASS — REAL PAYMOB TEST.** Platform cancelled the offer while G's page was open; the page was then paid (txn 542767589, 220.00). Callback → `refused`; payment succeeded but **not applied**; invoice stays open; offer stays `cancelled`; G's plan unchanged; open `refused_settlement` incident "The platform withdrew this custom plan offer after the page was opened." - exactly ADR-114. |
| P16 | Offer expiry with open checkout | **PASS — REAL PAYMOB TEST.** Documented (ADR-114, `custom_plan_offer_ledger`): a page opened **before** `expires_at` is honoured. Offer expired 10:04:13; sweep marked it `expired`; the page opened at 10:01 was paid at 10:04:47 (txn 542769445) → applied, offer `active`, G on that v1. |
| P17 | Immutable price snapshot | **PASS — REAL PAYMOB TEST.** D's offer page (v1 260.00, 4 numbers) was open while v2 (999.00, 9 numbers, 99,999 messages) was published; paying the old page (txn 542770712) settled at **v1**: 260.00, D pinned to v1, limits 4 / 20,000. |
| P45 | Double payment of one custom-offer invoice | **NOT RUN.** Optional ("if practical"); the same settlement lock and refusal path is proven with real money in P44, and the offer-activation-once invariant (L18) holds. |

## 12. Top-up matrix

The seven keys in the current code (`TOPUP_LIMITS`, `app/db/models/billing.py`)
match the task list. Products created through `POST /platform/billing/topups`;
workspace C (Starter base) bought each at hosted checkout.

| ID | Key | Qty / price | Paymob txn | Before callback | After callback | Replay | Capacity check |
|---|---|---|---|---|---|---|---|
| T01 | period_messages | 5,000 / 50.00 | 542771537 | 0 granted, limit 1,000 | **6,000** | duplicate, 6,000, 1 grant | — |
| T02 | period_ai_turns | 2,000 / 60.00 | 542772172 | 0 granted, 100 | **2,100** | duplicate, 1 grant | — |
| T03 | period_campaign_messages | 3,000 / 70.00 | 542772683 | 0 granted, 0 | **3,000** | duplicate, 1 grant | — |
| T04 | storage_bytes | 1 GiB / 80.00 | 542773225 | 0 granted, 53,687,091,200 | **54,760,833,024** | duplicate, 1 grant | capacity semantics per §19 |
| T05 | whatsapp_numbers | 2 / 90.00 | 542774067 | 0 granted, 1 | **3** | duplicate, 1 grant | real guard (`reserve_or_refuse`, the connect route's `NumberSlotDep`) admitted numbers 1-3 (synthetic rows, no Meta call) and **refused the 4th** |
| T06 | team_members | 3 / 40.00 | 542776011 | 0 granted, 2 | **5** | duplicate, 1 grant | real `POST /invitations` (`SeatDep`): owner + 4 invitations accepted, 5th → **402** |
| T07 | knowledge_documents | 50 / 30.00 | 542777075 | 0 granted, 25 | **75** (via inquiry, §14) | second sweep no-op, 1 grant | real guard admitted the 75th synthetic document and **refused the 76th** |

Invariants for all seven: client cannot set price or quantity (422); invoice
purpose `topup` with `plan_version_id` null and the product snapshot in its
lines (code, key, quantity, amount, `valid_until`); quantity > 0; EGP;
`provider_mode = test`; 0 grant before settlement; exactly one grant (L17 = 0);
replay adds nothing; another workspace's products/purchases are not visible.
Top-ups never touched C's plan (Starter v1 throughout).

| ID | Scenario | Result |
|---|---|---|
| P41 | Same idempotency key ×3 | **PASS — REAL PAYMOB TEST.** 1 payment, 1 Paymob order; the 2nd/3rd calls → 409 naming the same purchase; the one page was then paid (T01). |
| P42 | Top-up lost callback | **PASS — REAL PAYMOB TEST** (T07). |
| P43 | Two pages for one top-up invoice | **NOT APPLICABLE.** The application refuses a second page: `POST /billing/checkout {invoice_id: <topup invoice>}` → 409 "Top-ups are bought with POST /billing/topups/{id}/checkout", and a new idempotency key creates a *new* purchase and invoice. Two provider successes for one top-up invoice cannot be produced through the product. |

## 13. Double-payment verification

**P44 — PASS — REAL PAYMOB TEST (DB-001 with real money).** B opened two
distinct hosted pages for one Pro invoice `22453682…` (orders 619141105 and
619141120), filled both in two Chrome tabs and pressed *Pay* on both within
25 ms (09:31:08.575Z / .600Z). Page A (…2346) completed immediately; page B
(…0008) passed through the 3DS emulator, so its callback arrived 25 s later -
the arrivals were sequential, not simultaneous (true concurrency is covered by
`test_settlement_concurrency.py`). Result:

| | Payment | Paymob txn | Status | Applied | Event |
|---|---|---|---|---|---|
| A | `ca9e0b8d…` | 542752485 | succeeded | yes | applied |
| B | `0b37026f…` | 542752484 | succeeded | **no** | **refused** |

invoice amount_paid 99.00 = amount_due = applied net 99.00; collected 198.00 =
accepted 99.00 + held 99.00; `duplicate_payment` incident for the held money;
subscription granted once (Pro v1, one period).

## 14. Lost-callback reconciliation

| ID | Scenario | Result |
|---|---|---|
| P09 | D Pro checkout, API stopped before paying | **PASS — REAL PAYMOB TEST.** Paymob's callback → 502 (ngrok, 12:41:37+03). Wasla pending/open, D on Starter. After restart the remediation sweep's hosted reconciliation asked Transaction Inquiry → txn 542757558 success → `InvoiceSettlement` applied once (D on Pro v1); `recovered_by_reconciliation` incident (auto-resolved). |
| T07 / P42 | Top-up lost callback | **PASS — REAL PAYMOB TEST.** Same protocol; txn 542777075 recovered; granted exactly once (25 → 75); second sweep handled 0. |
| P10 | Inquiry contract | **PASS — REAL PAYMOB TEST.** Independent read-only call through the adapter: `POST /api/auth/tokens` (body `api_key`) → 201; `POST /api/ecommerce/orders/transaction_inquiry` (body `auth_token`, `merchant_order_id`; plus Bearer header) → 200 → txn 542757558, order 619147177, 99 EGP, integration 5885262, `is_live=false`. |
| — | Delayed original callback | **NOT EXECUTED.** Paymob sent no retry within the observation window and ngrok retains no body for a request it could not forward, so no genuine delayed callback existed to deliver. Duplicate-callback protection on the same event identity is proven by P03/P06/T01-T06 replays. |

Note: the lease left by the discarded old-code attempt (900 s) was allowed to
expire naturally; no row was edited.

## 15. Refund / void operations

Inventory: the only provider-backed reversal is `PaymobProvider.refund`
(`POST /api/acceptance/void_refund/refund`, secret-key auth, amount in cents),
reached only through `POST /platform/billing/payments/{id}/refund` (platform
operators). The tenant route `POST /billing/payments/{id}/refund` files a
review request and moves no money. Void is **not** implemented against Paymob
(`POST /platform/billing/invoices/{id}/void` is a local invoice state change).

| ID | Scenario | Result |
|---|---|---|
| P60 | Refund the held duplicate (P44 B, 99.00) | **PASS — REAL PAYMOB TEST.** Paymob refund txn 542753275; refund callback applied as "held money"; payment `refunded` 99.00; **invoice amount_paid stays 99.00**; applied payment A untouched; subscription unchanged; incidents resolved by the operator. |
| P61 | Invalid refunds | **PASS — APP E2E.** 99.01 on a 99.00 payment → 422 "Only 99.00 … can still be refunded"; wrong currency → 422; another workspace's payment (tenant route) → 404; 0 provider refund requests (no `paymob_refund` log line) and no request recorded. |
| P58 | Partial operator refund (B renewal `ab29fd99…`, 30.00) | **PASS — REAL PAYMOB TEST.** Refund txn 542755261; refunded_amount 30.00; invoice stays `paid` at amount_paid 69.00 (goodwill rule, ADR-096); period kept; amount/currency unchanged. |
| P59 | Full refund of the remaining 69.00 | **FAIL — REAL PAYMOB TEST.** Paymob accepted the refund (txn 542755547, 69.00) and sent the parent callback with `refunded_amount_cents: 9900`, but Wasla answered 200 `duplicate` and changed nothing. Finding **PAY-E2E-01**. |

## 16. Negative / failed / abandoned payments

| ID | Scenario | Result |
|---|---|---|
| P54 | Legitimate Test decline | **PASS — REAL PAYMOB TEST.** Visa at the ACS emulator with "(N) Not Authenticated / Transaction denied": callback `success=false`, txn 542779427 → event `transaction.failed / declined`, `failure_reason AUTHENTICATION_FAILED`; invoice open, top-up not granted, limit unchanged, no money state. The attempt row stays `pending` (the same page may be retried). |
| — | Unintended approval | The first decline attempt's emulator selection did not register and Paymob **approved** txn 542778509 (A, period_ai_turns 60.00). Recorded as an extra real top-up; it granted exactly once. |
| P55 | Abandon and retry | **PASS — REAL PAYMOB TEST.** The declined page was left; same idempotency key → 409 naming the old purchase; a new key → a distinct purchase, invoice and Paymob order (619173850), paid once (txn 542780985, Save Card …0008). |
| P56 | Expired / stale attempt | **PASS — APP E2E; real provider expiry NOT EXECUTED.** An unpaid page older than the grace period was inquired (Paymob 404 not-found) and left `pending` with only a lease - not failed, not settled; old and new attempts stay distinct rows with distinct orders. Waiting for Paymob's own page expiry was not practical. |
| P68 | Back / forward / refresh | **PASS — REAL PAYMOB TEST.** Back to Paymob's result page, forward, F5 on the success redirect, and three GETs of the success return URL (404): events, payments and amounts identical; no new Paymob transaction. |
| P67 | Cross-workspace tampering | **PASS — APP E2E + DB.** A paying B's invoice → 404; A reading B's payment → 404; A accepting F's offer → 404; 0 payments created. DB: a payment for A on B's invoice → `fk_payments_tenant_invoice` violation. |

## 17. Replay / idempotency

Genuine signed callbacks replayed byte-for-byte through the ngrok agent: P01
(542748905), MOTO ×2 (542750552, 542761282), T01-T06 (542771537, 542772172,
542772683, 542773225, 542774067, 542776011) and one TOKEN (order 619138197) →
every one `duplicate` / not re-created, 0 state change. Checkout idempotency: P41. Worker retries: P69.

P53 authenticity (synthetic, after the real callbacks): the genuine T01 body
with `amount_cents` changed and the original HMAC → **403**, nothing mutated.
The same body with only the *unsigned* `is_live` or `order.merchant_order_id`
changed → 200 `received` but short-circuited as a duplicate of the signed
`txn:kind` identity: **0 rows changed**. (First-delivery binding of those
fields is covered by `test_a_signed_callback_cannot_be_re_aimed_or_cross_environments`.)

P48: inserting a second payment for B with A's Paymob transaction 542748905 →
`uq_payments_provider_provider_reference` violation.

## 18. Financial invariant ledger (independent SQL, evidence DB)

| # | Invariant | Violations |
|---|---|---|
| L01 | invoice over-collected | 0 |
| L02 | amount_paid = net of applied payments | 0 |
| L03 | collected-but-unapplied money has an incident | 0 |
| L04 | paid invoice has paid_at | 0 |
| L05 | collected payment has processed_at | 0 |
| L06 | provider transaction identity unique | 0 |
| L07 | active custom offer has a paid invoice at its version | 0 |
| L08 | granted purchased top-up has a paid invoice | 0 |
| L09 | payment/invoice tenant binding | 0 |
| L10 | payment/card tenant binding | 0 |
| L11 | subscription plan/version coherent | 0 |
| L12 | ≤ 1 active default card per tenant | 0 |
| L13 | every Paymob payment `provider_mode = test` | 0 |
| L14 | hosted on 5885262, automatic on 5934829 | 0 |
| L15 | payment currency = invoice currency | 0 |
| L16 | applied payment amount = invoice due | 0 |
| L17 | top-up granted at most once per invoice | 0 |
| L18 | offer activated by at most one paid invoice | 0 |
| L19 | at most one applied payment per invoice | 0 |
| L20 | no automatic charge on a top-up invoice | 0 |
| **Total** | | **0** |

P50/P51/P49 across all 38 genuine TRANSACTION callbacks: provider
`amount_cents` = payment amount × 100 = invoice due × 100 (integer compare),
currency EGP, `order.id` = the stored Paymob order, `is_live = false`:
**0 mismatches**.

These invariants describe Wasla's own books, which are internally consistent.
They do **not** catch PAY-E2E-01, where Wasla's books disagree with Paymob's.

## 19. Entitlement verification

* P71: effective limit recomputed independently (serving plan version's base,
  or the default plan when the subscription is not serving, + active granted
  top-ups) for 7 workspaces × 7 keys = 49 pairs vs `GET /billing/entitlements`:
  **0 mismatches**.
* P72 (clock injected into the real `EntitlementService`, C's top-ups expiring
  at the period end): at `end − 1s` numbers 3 / documents 75 / seats 5 /
  messages 6,000; at `end` and `end + 1 day` numbers 1 / documents 25 /
  seats 2 / messages 1,000; existing 3 numbers and 75 documents **not
  deleted**; numbers over limit and new creation refused. No payment needed.

## 20. Secret-leak scan

31 secret values (Paymob secret key, HMAC secret, API key, every other
credential in the secrets file, generated JWT/encryption/fingerprint keys,
Postgres password, ngrok auth token, the three Test PANs, and **4 reusable card
tokens decrypted in memory only**) plus patterns (client secret, `"cvv"`,
`auth_token` values, `payment_token` values, Bearer JWTs) searched in: API logs
(~100 KB), worker/sweep logs (~36 KB), ngrok agent log, redirector log, and the
DB text of `audit_logs.metadata`, incidents, payment events, payment failure
reasons, invoice notes/lines.

| Target | Hits |
|---|---|
| all logs | 0 |
| database audit / incident / event / invoice text | 0 |
| this report | 0 (scanned before commit) |
| repository diff | only this file |

Plaintext reusable card tokens: **0**. Full card numbers: **0**. CVV: **0**.
(ngrok's in-memory inspector held raw callback bodies during the run; it is not
persisted and was stopped at the end.)

## 21. Transaction ledger

All EGP, all `provider_mode = test`, all callbacks `is_live = false`.
Hosted = integration 5885262, MOTO = 5934829.

| # | Scenario | WS | Invoice | Amount | Paymob order | Paymob txn | Int. | Saved card | Callback | Replay | Inquiry | Final | Incident |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | P01 Pro | A | 93c9cca5 | 99.00 | 619136657 | 542748905 | 5885262 | no | 200 applied | dup | — | paid, Pro | — |
| 2 | P04/P62 Business + Save Card | A | 0e19829f | 299.00 | 619138197 | 542749810 | 5885262 | TOKEN …2346 | 200 applied | TOKEN dup | — | paid, Business | — |
| 3 | MOTO renewal (old worker) | A | 3356d1cb | 299.00 | 619139302 | 542750552 | 5934829 | …2346 | 200 applied | dup | — | paid | — |
| 4 | Hosted renewal, revoked card (old worker sweep) | A | 54132ed3 | 299.00 | 619140052 | 542751335 | 5885262 | no | 200 applied | — | — | paid | — |
| 5 | P44 page A | B | 22453682 | 99.00 | 619141105 | 542752485 | 5885262 | no | 200 applied | — | — | paid | — |
| 6 | P44 page B (held) | B | 22453682 | 99.00 | 619141120 | 542752484 | 5885262 | no | 200 refused | — | — | refunded 99 (P60) | duplicate_payment (resolved) |
| 7 | Refund of #6 | B | — | 99.00 | 619141120 | 542753275 | 5885262 | — | parent 200 applied | — | — | held money returned | refund_requested (resolved) |
| 8 | Hosted renewal (old worker sweep) → P58/P59 | B | 9ac16759 | 99.00 | 619143122 | 542754263 | 5885262 | no | 200 applied | — | — | paid 69 | — |
| 9 | P58 partial refund | B | 9ac16759 | 30.00 | 619143122 | 542755261 | 5885262 | — | parent 200 applied | — | — | refunded 30 | — |
| 10 | P59 refund of remainder | B | 9ac16759 | 69.00 | 619143122 | 542755547 | 5885262 | — | parent 200 **duplicate** | — | — | **not recorded (PAY-E2E-01)** | — |
| 11 | P09 lost callback | D | e6916d8c | 99.00 | 619147177 | 542757558 | 5885262 | no | **502** | — | **yes** | paid, Pro | recovered_by_reconciliation |
| 12 | P08 no-card renewal | B | 682e4e0c | 99.00 | 619149526 | 542759772 | 5885262 | no | 200 applied | — | — | paid | — |
| 13 | P07 hosted renewal + Save Card | A | 903d71fe | 299.00 | 619150600 | 542760797 | 5885262 | TOKEN …2346 | 200 applied | — | — | paid | — |
| 14 | P05 MOTO renewal | A | b661b6b5 | 299.00 | 619151856 | 542761282 | 5934829 | …2346 | 200 applied | dup | — | paid | — |
| 15 | P11 custom offer + Save Card | E | 582d4f51 | 250.00 | 619152613 | 542762134 | 5885262 | TOKEN …2346 | 200 applied | — | — | offer active | — |
| 16 | Custom MOTO renewal | E | 795c2e7d | 250.00 | 619153406 | 542762646 | 5934829 | …2346 | 200 applied | — | — | paid | — |
| 17 | P12 custom offer, no save | F | e865c6e8 | 180.00 | 619153583 | 542764035 | 5885262 | no | 200 applied | — | — | offer active | — |
| 18 | P12 no-card renewal | F | 70e35d83 | 180.00 | 619155591 | 542764783 | 5885262 | no | 200 applied | — | — | paid | — |
| 19 | P15 withdrawn offer paid | G | d809d5d2 | 220.00 | 619158721 | 542767589 | 5885262 | no | 200 refused | — | — | held | refused_settlement (open) |
| 20 | P16 expired offer, page opened in time | G | 7b849223 | 230.00 | 619159597 | 542769445 | 5885262 | no | 200 applied | — | — | offer active | — |
| 21 | P17 snapshot v1 | D | 2006d521 | 260.00 | 619161973 | 542770712 | 5885262 | no | 200 applied | — | — | v1 active | — |
| 22 | T01 period_messages | C | c5320e3f | 50.00 | 619163446 | 542771537 | 5885262 | no | 200 applied | dup | — | +5,000 | — |
| 23 | T02 period_ai_turns | C | 5533a66d | 60.00 | 619164190 | 542772172 | 5885262 | no | 200 applied | dup | — | +2,000 | — |
| 24 | T03 period_campaign_messages | C | 565dec14 | 70.00 | 619164771 | 542772683 | 5885262 | no | 200 applied | dup | — | +3,000 | — |
| 25 | T04 storage_bytes | C | a7aedc0c | 80.00 | 619165334 | 542773225 | 5885262 | no | 200 applied | dup | — | +1 GiB | — |
| 26 | T05 whatsapp_numbers | C | 29c12253 | 90.00 | 619165968 | 542774067 | 5885262 | no | 200 applied | dup | — | +2 | — |
| 27 | T06 team_members | C | 13e570ae | 40.00 | 619167783 | 542776011 | 5885262 | no | 200 applied | dup | — | +3 | — |
| 28 | T07 knowledge_documents, lost callback | C | 059e9141 | 30.00 | 619169417 | 542777075 | 5885262 | no | **502** | — | **yes** | +50 | recovered_by_reconciliation |
| 29 | Unintended approval (decline attempt 1) | A | b6452595 | 60.00 | 619171289 | 542778509 | 5885262 | no | 200 applied | — | — | +2,000 | — |
| 30 | P54 real decline | A | bc71fe0c | 50.00 | 619172328 | 542779427 | 5885262 | no | 200 declined | — | — | unpaid | — |
| 31 | P55 retry + Save Card | A | aaeef33f | 50.00 | 619173850 | 542780985 | 5885262 | TOKEN …0008 | 200 applied | — | — | +5,000 | — |

Totals: **27 approved collections = 4,189.00 EGP (Test)**, 1 decline, 3 refund
transactions = 198.00 EGP (Test), 4 real TOKEN callbacks, 3 MOTO transactions
(2 by the remediation worker), 24 approved hosted transactions, 2 inquiry
recoveries. **Live transactions: 0.**

## 22. Unsupported / not-applicable operations

| Operation | Classification | Evidence |
|---|---|---|
| Provider-backed void / cancel | NOT SUPPORTED | `PaymobProvider.refund` docstring: void is deliberately not attempted; invoice void is local only |
| Tenant self-service refund | NOT APPLICABLE (moves no money) | files a `refund_requested` incident (used in P60) |
| Two pages for one top-up invoice (P43) | NOT APPLICABLE | refused with 409 by the application |
| Double payment of one custom-offer invoice (P45) | NOT RUN | optional; see §11 |
| Genuine delayed callback after inquiry recovery | NOT EXECUTED | no Paymob retry observed; ngrok kept no body |
| Paymob-side page expiry | NOT EXECUTED | app-side behaviour verified (P56) |
| Paymob Subscriptions Module | NOT SUPPORTED by design | Wasla bills calendar months with MIT (DECISIONS.md) |

## 23. Failures / findings

### PAY-E2E-01 — A second refund of the same transaction is dropped as a duplicate (MEDIUM-HIGH)

* **Where:** `app/integrations/billing/paymob.py` `PaymobProvider._event` builds
  `event_id = f"{transaction_id}:{kind.value}"`; a refund callback reports the
  **parent** transaction with a cumulative `refunded_amount_cents`, so every
  refund of one transaction produces `"<parent>:refunded"`.
  `CheckoutService` treats a known `provider_event_id` as a duplicate before
  reading `refunded_amount`.
* **Real evidence:** payment `ab29fd99…` (txn 542754263). Partial refund 30.00
  (txn 542755261) → callback `refunded_amount_cents 3000` → applied. Refund of
  the remaining 69.00 (txn 542755547) → Paymob callback 12:37:26+03 with
  `refunded_amount_cents 9900` → API log `billing.callback_duplicate`,
  `provider_event_id "542754263:refunded"`, `outcome: duplicate`.
* **State now:** Paymob has refunded 99.00; Wasla shows `refunded_amount 30.00`,
  status `succeeded`, invoice `paid` at 69.00, and `refund_requested_amount
  99.00` outstanding forever - which also blocks any later refund request on
  this payment (`_refundable` → 409 "already been requested").
* **Impact:** a documented, supported Paymob flow (multiple partial refunds
  until the full amount) leaves Wasla's books wrong: the plan withdrawal /
  invoice void that ADR-096 prescribes for a full refund never happens, and
  money returned to the customer is still counted as paid. No
  over-collection (money moved *back*), but the platform's revenue and the
  customer's entitlement are overstated. Nothing in the internal invariant
  ledger detects it.
* **Direction for remediation (not done here):** key refund events on the
  cumulative refunded amount (e.g. `"<txn>:refunded:<refunded_amount_cents>"`)
  or on the refund child transaction id, and add a provider-vs-ledger
  refund reconciliation check.
* **Contained for this run:** no later scenario refunded the same transaction
  twice.

### PAY-E2E-03 — Inquiry can read a refund child as a successful collection (MEDIUM)

* **Where:** `PaymobProvider.inquire_charge` → `_inquiry_transaction` →
  `_event`. Paymob's Transaction Inquiry returns the **most recent**
  transaction of the order (documented, 2026-06-28). For a refunded order that
  is the refund child: `id 542755547, success true, is_refund true,
  has_parent_transaction true, parent_transaction 542754263, amount_cents
  6900`. `_event` checks `is_voided`/`is_refunded` (parent flags) but not
  `is_refund`, so it returns `kind: succeeded, amount 69 EGP` - observed on a
  real read-only inquiry of `ab29fd99…`.
* **Impact:** harmless for the payments reconciled in this run (reconciliation
  only asks about unresolved attempts, and both recovered ones had no refund).
  A plausible bad case: a checkout whose callback is lost is refunded from the
  Paymob dashboard before Wasla reconciles; the inquiry then returns the refund
  child, and when its amount equals the invoice (a full refund) Wasla would
  settle an invoice with money that has already gone back. Not reproduced
  end-to-end.
* **Direction:** treat `is_refund`/`has_parent_transaction` inquiry answers as
  reversals of the parent (or as "not a collection"), and bind the inquiry
  answer's transaction id to the parent the attempt is waiting for.

### PAY-E2E-02 — Complimentary assignment writes a misleading `purchase_settled` audit row (LOW)

`SubscriptionService` (grant helper, `reason: "purchase_settled"`,
`actor_kind: system`) is reused by the operator's zero-price assignment, so
G's trail contains a `system` "purchase_settled" row next to the correct
`platform_staff` rows, although nothing was purchased. No money path is
affected; the operator rows carry actor, role, reason and version.

### Process note (not a product defect)

The first sweeps ran pre-remediation code (§2); all affected scenarios were
re-run and only remediation-code results are claimed.

### Static and targeted regression (after the real E2E, no code changed)

| Check | Result |
|---|---|
| `ruff check .` | All checks passed |
| `black --check .` | 756 files unchanged |
| `mypy app tests` | no issues in 670 source files |
| `alembic heads` / `current` / `check` | `0080 (head)` single / `0080` / no new operations |
| `python -m scripts.db_preflight verify` | ok |
| Targeted suites on `wasla_paymob_regression_test`: `test_paymob_checkout`, `test_paymob_webhook_endpoint`, `test_paymob_refunds`, `test_paid_plan_settlement`, `test_settlement_backstop`, `test_settlement_concurrency`, `test_payment_reconciliation`, `test_recurring_billing`, `test_custom_plan_offers`, `test_custom_plan_lifecycle`, `test_topups`, `test_topup_invariants`, `test_topup_concurrency`, `test_billing_concurrency`, `test_financial_integrity`, `test_refund_entitlements`, `test_checkout_transaction_boundary`, `test_billing_endpoints`, `test_commercial_api`, `test_billing_crash_recovery`, `test_payment_token_migration` + matching `tests/unit` billing/Paymob/payment-method/refund/top-up modules | **567 passed, 0 failed, 0 errors, 0 skipped** (exit 0) |

The existing suites pass while PAY-E2E-01 and PAY-E2E-03 exist. For
PAY-E2E-01 the reason is the fixture, not missing coverage:
`test_a_partial_reversal_then_the_rest_adds_up_once`
(`tests/integration/test_paymob_refunds.py`) and
`test_a_second_partial_reversal_that_empties_the_invoice_withdraws`
(`tests/integration/test_refund_entitlements.py`) send each refund callback
with a **different** transaction id (`"1001"`, `"1002"`), whereas real Paymob
sent both refund callbacks on the **same parent** transaction (542754263) with
a cumulative `refunded_amount_cents` (3000, then 9900). No test builds an
inquiry answer with `is_refund: true` (PAY-E2E-03). The whole-repository suite was not re-run
(no tracked code changed).

## 24. Final verdict

Every item in the task's completion list passed against real Paymob Test:
hosted subscription, signed callback, callback replay, Save Card, TOKEN
callback, MOTO/MIT renewal, no-card hosted renewal, custom paid offer, custom
no-card renewal, all 7 top-up keys, lost callback + inquiry, real duplicate
payment, and refund of the held duplicate; invariant violations 0; Live
transactions 0; secret leaks 0.

However, a **supported Paymob-backed flow failed with real money**: a second
partial refund of one transaction (P59, PAY-E2E-01) is silently dropped,
leaving Wasla's books disagreeing with Paymob's. A second real-data defect
(PAY-E2E-03) makes inquiry misread refund children. Neither is a Test-account
limitation, so this cannot be "verified with provider-limited gaps".

**PAYMOB TEST E2E FAILED**

The evidence database `wasla_paymob_browser_e2e` (Compose project
`wasla-paymob-browser-e2e`, synthetic and Test-mode data only) is kept for
review.
