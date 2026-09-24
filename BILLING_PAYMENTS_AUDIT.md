# Wasla — Billing, Plans, Subscriptions & Paymob Audit

```text
Audit id        billing-03bb13b1
Type            AUDIT ONLY — no production code, test, migration or configuration was changed
Date            2026-09-24
Frozen HEAD     e1b5cec4961974f06b42f976ae89a5dccb430545  (branch worktree-billing-google-auth)
Alembic         0069 (single head) · `alembic check` clean
```

---

## 1. Executive summary

**Verdict: NOT READY for production billing.** The *money-safety* machinery Wasla built is
genuinely good — callback authentication, event idempotency, tenant isolation, the
three-transaction MIT protocol, concurrency claims, token encryption and reconciliation all held
under adversarial probes, real PostgreSQL concurrency and a real Paymob Test round trip. What
fails is the *commercial state machine around it*: several ordinary customer journeys take money
and grant the wrong thing, or grant something without the money.

The seven findings that matter most:

| ID | Sev | One line |
|---|---|---|
| BILL-01 | HIGH | After the 14-day Starter trial expires (i.e. for every workspace older than 14 days) a paid checkout **captures the money and grants nothing**. Reproduced with a **real Paymob Test payment** (txn 541139493). |
| BILL-02 | HIGH | An **abandoned** upgrade checkout is later **charged to the saved card** by the renewal sweep (MIT without consent) and then grants the plan. |
| BILL-03 | HIGH | The **first paid period is invoiced and collected twice** (checkout bills the current period in advance, the sweep bills the same service period in arrears). Reproduced with real Paymob Test money: 2 × 99 EGP for Sep 24 → Oct 24. |
| BILL-06 | HIGH | Opening a second checkout for a pricier plan re-prices the pending invoice; paying the **cheaper page grants the pricier plan** (99 EGP → Business 299 EGP) and the shortfall is never dunned. |
| BILL-07 | HIGH | A **declined card followed by a successful retry on the same payment page** is refused: customer charged, invoice unpaid, plan not granted. |
| BILL-04 | MEDIUM (release blocker for auto-renewal) | **Real saved cards never persist**: Wasla correlates the TOKEN callback on the intention id, Paymob sends the order id. Proven with two real TOKEN callbacks. |
| BILL-05 | MEDIUM (release blocker for auto-renewal) | **Every real MIT is refused by Paymob** (`billing_data.email="NOT_COLLECTED"` → HTTP 400) and silently retried daily for ever. |

BILL-04 and BILL-05 mean automatic renewal cannot work against real Paymob today — which, ironically,
is also what currently keeps BILL-02 from firing in production. Fixing them without fixing BILL-02
turns a dormant defect into unauthorized charges.

```text
REAL INITIAL PAYMENT E2E      VERIFIED   (txn 541130792, 99.00 EGP, callback via ngrok, HMAC ok, plan granted)
REAL TOKEN CALLBACK E2E       FAILED     (2 real TOKEN callbacks verified by HMAC, both `card_token_unmatched`)  → BILL-04
REAL SAVED CARD STORAGE       FAILED as shipped; VERIFIED with labelled correlation emulation (AES-GCM v1 envelope, fingerprint, 0 plaintext)
REAL MOTO/MIT E2E             FAILED as shipped (Paymob 400 on billing email) → BILL-05;
                              VERIFIED with labelled in-process email emulation (txn 541135124 and 541135994, MOTO 5934829)
REAL FAILURE PAYMENT E2E      MIT refusal VERIFIED (real Paymob refused an invalid token);
                              hosted-checkout decline NOT REPRODUCIBLE (Paymob Test approved a wrong-CVV attempt)
REAL CALLBACK VIA NGROK       VERIFIED   (Paymob egress 34.200.x → public HTTPS tunnel → local Wasla → HMAC → isolated DB)
```

Score **55 / 100** (section 37). Findings: **CRITICAL 0 · HIGH 5 · MEDIUM 9 · LOW 6 · INFO 3**.

---

## 2. Frozen state and isolation

```text
Branch / HEAD       worktree-billing-google-auth @ e1b5cec4961974f06b42f976ae89a5dccb430545 (unchanged throughout)
Working tree        9 untracked audit markdown files (pre-existing, untouched)
Audit worktree      E:\wasla-billing-audit      (detached @ e1b5cec)
Mutation worktree   E:\wasla-billing-mutation   (detached @ e1b5cec; every mutant byte-restored, sha256 verified)
Docker project      wasla-bill-03bb  (own network + volumes; loopback ports 57641 PG, 57642 Redis)
PostgreSQL          pgvector pg16, system_identifier 7689156240594284589
                    (developer PG is 7681598525836980262 — never written or queried beyond that one read)
                    databases: wasla_models, wasla_migrations, wasla_billing_models, wasla_billing_migrations,
                               wasla_probe (probes, committed rows), wasla_e2e (real Paymob), wasla_mut, wasla_alembic
Redis lane 1        run_id ed5f2666ce3ec4bf40697a66c9eff30efd396af6 (runner1 joins its netns → hard-coded localhost:6379 is isolated)
Redis lane 2        run_id 78f30dbb811f887525ac8cc748293ea03864a8eb (runner2 / mutations)
Developer Redis     run_id eb83af4a… (read-only INFO to prove difference; never used)
MinIO               in-project, bucket wasla-media (baseline media suites only)
Runner image        wasla-bill-runner:03bb (python:3.12-slim pinned digest + requirements.lock + dev tools)
```

* Stale registrations `E:\wasla-billing-audit`, `E:\wasla-billing-mutation`, `E:\wasla-billing-mutations` from an earlier,
  directory-less attempt were re-created with `git worktree add -f` at the frozen HEAD. No reset, clean or stash anywhere.
  Stale volumes from earlier attempts (`billing-20260920-t8_*`, `wasla-billing-ees5_pgdata`) were **not** reused.
* No overlap: every mutable resource above is exclusive to this audit. **Not contaminated.**
* Baseline, probes, E2E and mutations used disjoint databases; deterministic local evidence and real Paymob evidence are
  reported separately (sections 3–15 vs 18–23).

---

## 3. Baseline gates

| Gate | Result |
|---|---|
| ruff | All checks passed |
| black --check | 679 files unchanged |
| mypy (app + tests) | Success, 606 source files |
| alembic heads / upgrade / check | `0069 (head)` · upgrade clean · "No new upgrade operations detected" |
| Whole `tests/`, model-built | **5501 passed · 17 skipped · 0 failed** (skips: 11 OpenAI opt-in, 3 schema-parity-on-model-DB, 1 tmpfs, 2 invariant-sweep-needs-kept-data — all CI-sanctioned) |
| `tests/integration` + `tests/e2e`, migration-built | **2665 passed · 2 skipped · 0 failed** |
| Billing-targeted (35 files), model-built | **649 passed · 0 skipped · 0 failed** (lane 2, pristine mutation tree); re-run on the audit worktree (lane 1): **649 passed · 0 skipped · 0 failed** |
| Billing-targeted (35 files), migration-built | **649 passed · 0 skipped · 0 failed** |

The green baseline matters for interpreting this report: **every HIGH finding below coexists with a fully green suite.**

---

## 4. Billing architecture

```text
Wasla Plan (plans row) ─┐
                        ├─> Subscription (1 per tenant, UNIQUE tenant_id) ──> EntitlementService (live plan lookup)
Checkout (POST /billing/checkout) ─> Invoice (UNIQUE tenant_id+period_start) ─> Payment attempts (rows)
                                                                                   │
Paymob Intention API (hosted checkout / MOTO) <── PaymobProvider ──────────────────┘
          │  server-to-server callback (HMAC-SHA512)
          ▼
POST /api/v1/webhooks/paymob ─> payment_events (UNIQUE provider+event_id, insert = claim)
          └─> CheckoutService.apply ─> settle invoice ─> _apply_purchased_plan / restore past_due|suspended
TOKEN callback ─> remember_saved_method ─> payment_methods (AES-GCM envelope + HMAC fingerprint)
BillingWorker (10-min poll): roll-over+invoice (arrears) → reconcile → collect (MIT) → chase → suspend
```

* **Provider abstraction:** `CheckoutProvider` / `RecurringProvider` / `ChargeInquiryProvider` protocols; only
  `PaymobProvider` and `ManualProvider` exist. Services never name Paymob. Domain states are Wasla's own.
* **Wasla is the recurring engine.** Paymob's Subscription module is **NOT USED** (verified: no route, no client call,
  `Subscription.provider_reference` written by no code path). Renewal = Wasla invoice + saved card token + MOTO intention
  + Pay Request (MIT) — as the brief expected.

### Billing surface map

| Kind | Items |
|---|---|
| Models / tables | `plans`, `subscriptions`, `invoices`, `payments`, `payment_events`, `payment_methods`, `usage_events` (period meters), `audit_logs` (billing actions) |
| Migrations | 0016 plans/subscriptions+seed, 0017 invoices/payments, 0030 payment callbacks, 0031 refunds, 0032 saved cards/renewal, 0037 suspension, 0042 collection state, 0043 storage limit, 0047 withdrawn-plan audit, 0059 AI-turn entitlement, 0069 token protection |
| Repositories | `billing_repository` (Plan, Subscription, PlatformSubscription), `invoice_repository` (Invoice, Payment, PlatformInvoice, PlatformPayment), `payment_method_repository` |
| Services | checkout, subscription, invoice, recurring, refund, payment_method, payment_token, payment_reconciliation, entitlement, workspace_entitlement, usage |
| Routes | `/billing/*` (plans, subscription, subscription/plan, cancel, resume, checkout, payments/{id}, refund, entitlements, payment-methods…), `/invoices/*`, `/webhooks/paymob`, `/platform/invoices/{id}/payments`, `/platform/invoices/{id}/void`, `/platform/overview` |
| Workers | `BillingWorker` (roll-over, reconcile, collect, chase, suspend) |
| Provider client | `integrations/billing/paymob.py` (intention, MOTO intention + pay, refund, inquiry, callback + token HMAC) |
| Config | `BILLING_PROVIDER`, `PAYMOB_SECRET_KEY`, `PAYMOB_PUBLIC_KEY`, `PAYMOB_HMAC_SECRET`, `PAYMOB_API_KEY`, `PAYMOB_INTEGRATION_IDS`, `PAYMOB_MOTO_INTEGRATION_ID`, `PAYMOB_REGION`, `APP_PUBLIC_URL` (→ notification/redirect URL), `CREDENTIAL_ENCRYPTION_KEYS`, `PAYMENT_TOKEN_FINGERPRINT_KEY`, `DEFAULT_PLAN_CODE`, `BILLING_PAST_DUE_DAYS` (7), `BILLING_SUSPEND_AFTER_DAYS` (30), `BILLING_RECONCILIATION_*` |
| Metrics | `wasla_payment_reconciliation_total{outcome}`, `wasla_oldest_pending_payment_age_seconds`, provider-call metrics (`provider="paymob"`), `wasla_auth_security_events_total{event="paymob_webhook"}` |
| Alerts | **none for billing** (BILL-14) |
| Docs / runbook | `docs/BILLING.md`, `docs/SAAS.md`, `docs/RUNBOOK.md` (Paymob callback, reconciliation), ADR-029/030/031/044/045/046/059/061/082/088/091/096 |
| Tests | 35 billing-targeted files (section 3) |

---

## 5. Database model

| Table | PK | Ownership | Uniques / checks | Money & state | Provider ids | Deletion |
|---|---|---|---|---|---|---|
| `plans` | uuid | platform (no tenant) | `uq_plans_code` | `price Numeric(12,2)`, `currency char(3)`, `interval` enum {monthly, yearly}, `trial_days int`, `limits jsonb`, `is_public`, `is_active` | — | retire via `is_active`; FK RESTRICT from subscriptions |
| `subscriptions` | uuid | `tenant_id` FK CASCADE | `uq_subscriptions_tenant_id` | status enum {trialing, active, past_due, suspended, cancelled, expired}, period start/end, `trial_ends_at`, `cancel_at_period_end`, `cancelled_at`, `ended_at` | `provider`, `provider_reference` (**never written**) | retained by purge |
| `invoices` | uuid | `tenant_id` FK CASCADE; `subscription_id` FK SET NULL | `uq_invoices_tenant_id_period_start` | status {draft, open, paid, uncollectible, void}, `amount_due`, `amount_paid` Numeric(12,2), `plan_code` snapshot, `lines` jsonb snapshot, `issued_at`, `paid_at`, `voided_at`, `collection_attempts`, `next_collection_at` | `provider_reference` | retained by purge |
| `payments` | uuid | `tenant_id` CASCADE, `invoice_id` CASCADE, `payment_method_id` SET NULL | `uq(provider, provider_reference)`, `uq(tenant_id, idempotency_key)`, **partial** `uq(invoice_id) WHERE collection_state IN (claimed, requested)`, CHECK `(collection_state IS NULL) = NOT is_automatic` | status {pending, succeeded, failed, refunded}, `amount`, `refunded_amount`, `currency`, `collection_state` {claimed, requested, settled, abandoned} | `provider_reference` (txn), `provider_intent_reference` (intention), `refund_reference` | retained by purge |
| `payment_events` | uuid | via `payment_id` FK CASCADE (nullable) | `uq(provider, provider_event_id)` | `outcome`, `detail` (app-written, ≤300) | `provider_event_id = "<txn>:<kind>"`, `provider_transaction_id` | retained by purge |
| `payment_methods` | uuid | `tenant_id` FK CASCADE | `uq(provider, token_fingerprint)` | status {active, revoked}, `is_default` | `provider_token` (AES-GCM envelope, row-bound AAD), `token_fingerprint` (HMAC-SHA256), `provider_token_id` | revoked at deletion, erased by purge |

**No CHECK constraint protects any money column** (price, amount_due, amount_paid, amount, refunded_amount, trial_days,
currency): the database accepted `-5` price, `-3` trial days, currency `ZZZ` and negative invoice/payment amounts in
probe P20. Only the service layer stands in front of them — and there is no service for plans at all (BILL-12).

### Domain model and sources of truth

| Question | Authoritative source |
|---|---|
| Current plan | `subscriptions.plan_id` (if status ∈ SERVING), else `DEFAULT_PLAN_CODE` |
| Billing status | `subscriptions.status` |
| Next renewal | `subscriptions.current_period_end` (sweep bills the *ended* period) |
| Entitlements | **live** `plans.limits` row for the resolved plan (no snapshot) |
| Price charged | `payments.amount` snapshot (= `invoice.outstanding` at attempt time) |
| Invoice amount | `invoices.amount_due` snapshot — **but mutable by `_reprice` while unpaid** (BILL-06) |
| Payment state | `payments.status` via `PAYMENT_TRANSITIONS`; invoice via `INVOICE_TRANSITIONS` |
| Card ownership | `payment_methods.tenant_id`, bound into the AES-GCM AAD (`payment-card-token:v1:{provider}:{tenant}:{method}`) |

---

## 6. Full plan catalogue (live, from the migration-built database)

| Code | Name | Public | Active | Price | Interval | Trial | agents | numbers | team | docs | storage | msgs/period | AI turns/period | campaign msgs/period |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `starter` | Starter | yes | yes | **0.00 EGP** | monthly | **14 d** | 1 | 1 | 2 | 25 | 50 GiB | 1,000 | 100 | **0** |
| `pro` | Pro | yes | yes | **99.00 EGP** | monthly | 14 d *(never applied)* | 5 | 3 | 10 | 500 | 50 GiB | 10,000 | 5,000 | 5,000 |
| `business` | Business | yes | yes | **299.00 EGP** | monthly | 14 d *(never applied)* | 20 | 10 | 50 | 5,000 | 50 GiB | 50,000 | 25,000 | 25,000 |
| `enterprise` | Enterprise | **no** | yes | 0.00 EGP | monthly | 0 | — | — | — | — | — | — | — | — |

"—" = key absent = **unlimited**. `owned_workspaces` is on no plan (only the technical `ABSOLUTE_WORKSPACE_SAFETY_LIMIT`
applies). Grace/renewal policy is global configuration, not per plan: past-due after 7 days from `issued_at`, suspended
after 30. Currency: EGP only in data; nothing forbids another value (P20). Not present anywhere: tax/VAT, coupons,
discounts, credits, proration, overage pricing, usage-metered billing (usage lines carry quantity and `0.00`).

---

## 7. Plan feature → enforcement matrix

| Feature (key) | API enforcement | Worker enforcement | DB enforcement | Concurrency-safe | UI-only? |
|---|---|---|---|---|---|
| `agents` | `AgentSlotDep` → `require()` → **402** on `POST /agents` | — | none | **NO** (P7: 2 agents at limit 1) | no |
| `whatsapp_numbers` | `NumberSlotDep` on connect | — | none | **NO** (same code path) | no |
| `team_members` | `SeatDep` on invite + member add | — | none | **NO** (same code path) | no |
| `knowledge_documents` | `DocumentSlotDep` on submit | — | none | **NO** (same code path) | no |
| `storage_bytes` | `reserve()` under advisory lock on upload (402) | inbound media skipped with reason | none | yes (advisory lock) | no |
| `period_messages` | **none** — inbound never refused by design; outbound manual replies never refused | — | — | n/a | **counted, not enforced** |
| `period_ai_turns` | — | `consume()` under advisory lock; exhausted → handoff `AI_QUOTA_EXHAUSTED` | — | yes | no |
| `period_campaign_messages` | `require(..., additional=audience)` in `CampaignService` (402 for whole audience) | — | — | **NO** (require, not consume) | no |
| `owned_workspaces` | `WorkspaceEntitlementService` | — | — | not tested here (account scope) | no — but **no plan sets a value** |

Direct API access cannot bypass the resource guards (they are route dependencies; mutation B20 — guard answers without
refusing — is killed by the suite). The gap is concurrency (BILL-08) and `period_messages`, which is a meter, not a limit.

**Entitlement source of truth:** `EntitlementService._resolve` reads the subscription and then the **live** `plans` row
on every request. *If somebody edits a plan today, every existing subscriber changes immediately* — limits at once (P13),
price at the next renewal invoice (P11). There is no snapshot, version or effective date.

---

## 8. Plan administration

**There is no supported mechanism for managing the catalogue.** No API, service, role, validation or audit exists to
create, edit, deactivate or delete a plan; `PlanRepository` is read-only; `plans` rows come from migration 0016 and
later `UPDATE` migrations (0043, 0059). The only way to change a price, limit or availability is SQL or a new migration.

| Actor | Plan-catalogue routes | Change a subscriber's plan |
|---|---|---|
| tenant member / admin | none exist (403 on every money route — section 28) | no |
| tenant owner | none exist | only to a **free** plan (`POST /billing/subscription/plan`); priced plans only via checkout (402 otherwise — ADR-059) |
| platform admin / owner | **none exist** | **none exist** (no operator "move subscriber" route) |

Consequences (BILL-12): no optimistic concurrency, no audit trail of price/limit changes (so "why was customer X charged
Y?" can be answered only from invoice snapshots, never from the catalogue's history), no validation of price/currency/
limits beyond what the DB happens to enforce, and no operator way to comp, migrate or fix a subscriber.

**How an operator is currently expected to change things (as-is):** edit `plans` with SQL / a migration. Price → applies
to every subscriber's *next* sweep invoice and every *new* checkout; pending checkouts keep their snapshot. Limits →
apply instantly to everyone. `is_active=false` → hides from `/billing/plans` and checkout; existing subscribers keep
entitlements and keep renewing. `DELETE` → refused by FK RESTRICT while any subscription references it.

---

## 9. Price and feature change semantics (probes P11–P13, P20)

| Scenario | Actual behaviour | Consistent? |
|---|---|---|
| Plan 99 → 149, **existing active subscriber** | next renewal invoice **149.00** (issued by the sweep from the live row) | internally consistent; **no grandfathering, notice or audit** |
| …**already-issued invoice** | unchanged (99.00) | yes |
| …**pending checkout** created at 99 | callback for 99 settles; invoice 99 | yes — snapshot honoured |
| …**new subscriber** | checkout 149.00 | yes |
| `agents` 5 → 3 with 5 in use | all 5 kept; new creation refused (402) | yes (grandfather-existing, block-new) |
| Deactivate `pro` with subscribers | checkout refused (`No such plan`); existing entitlements resolve; renewals continue | yes |
| Hard `DELETE` of `pro` with subscribers | refused (FK RESTRICT) | yes |
| Negative price / trial, bogus currency | **accepted by the database** | no guard anywhere |

---

## 10. Subscription state machine

```text
                  start (registration, DEFAULT_PLAN_CODE=starter, trial_days=14)
                                   │
                                   ▼
   ┌──────────── TRIALING ──(period end, sweep)──▶ EXPIRED  (terminal, not serving)
   │                │  change_plan (free) / settlement (_apply_purchased_plan)
   │                ▼
   │            ACTIVE ◀────────────── settlement of the chased invoice ───────┐
   │                │  issued_at + 7 d unpaid (chase)                           │
   │                ▼                                                           │
   │            PAST_DUE (serving) ── issued_at + 30 d unpaid (suspend) ─▶ SUSPENDED (not serving)
   │                                                                   (skipped while an MIT attempt is unresolved)
   └─ cancel(immediately) / cancel_at_period_end → roll_over ─▶ CANCELLED (terminal)
```

| Transition | Actor | Trigger | Precondition | Side effects | Entitlement |
|---|---|---|---|---|---|
| — → TRIALING/ACTIVE | registration / `POST /billing/subscription` | start | no subscription; plan free for self-service | audit | plan |
| TRIALING/ACTIVE/PAST_DUE → ACTIVE (other plan) | owner (free plan) / settlement (priced) | change_plan | non-terminal, different plan | period **restarts now**, trial cleared, cancel flag cleared, audit | new plan now |
| TRIALING → EXPIRED | sweep | period end | status trialing | email | default plan |
| ACTIVE/PAST_DUE → next period | sweep | period end | not cancel-pending | arrears invoice issued first | unchanged |
| any serving → CANCELLED | owner / sweep | cancel(immediately) / period end with flag | non-terminal | email, audit | default plan |
| ACTIVE → PAST_DUE | sweep | invoice `issued_at` + 7 d | invoice open & issued | audit, email | unchanged (serving) |
| PAST_DUE → SUSPENDED | sweep | `issued_at` + 30 d | no unresolved MIT attempt | audit, email | default plan |
| PAST_DUE/SUSPENDED → ACTIVE | settlement | verified callback on the subscription's invoice | invoice.subscription_id matches | log | restored |

**Illegal transitions** (P1, P17, unit tests, mutations B31/B34): CANCELLED → ACTIVE and EXPIRED → ACTIVE are refused by
`change_plan`, `resume`, `cancel` and by settlement; ACTIVE → PENDING does not exist; `PATCH status` does not exist.
The problem is the other side of that correctness: *checkout still accepts payment* from EXPIRED/CANCELLED
subscriptions whose plan it will then refuse to grant (BILL-01).

### Trials

Only `starter` (a **free** plan) is ever trialled, because only registration calls `start()`. A trial of a free plan
grants nothing extra, but its expiry moves the subscription to the terminal `EXPIRED` state on day 14 — which is the root
of BILL-01. `pro`/`business` `trial_days=14` is dead configuration: checkout goes straight to paid. Trial abuse is moot
(nothing to abuse). Trial → paid works only inside the first 14 days.

---

## 11. Upgrade, downgrade, cancellation, resume

| Journey | Actual semantics |
|---|---|
| Upgrade (free/trial → priced) | Checkout → invoice for the **current** period at full price → on verified payment `change_plan` restarts the period *now*. No proration. The next sweep bills the new period in arrears → **first period billed twice** (BILL-03). |
| Upgrade (priced → pricier) | Same path. The pending invoice for the period is **re-priced** to the new plan (`_reprice`) — any earlier pending page for the cheaper plan can then buy the pricier plan (BILL-06). |
| Upgrade while payment pending | Race A-then-B: callback A (99) after B was opened → grants B (Business). Stale callbacks of an *older* transaction are refused (P10). |
| Downgrade to free | Self-service `change_plan` (owner), immediate, period restarts, **no refund of the unused paid period**, resources kept, new creation blocked above the lower limits. |
| Downgrade priced → cheaper priced | Only via checkout (pays the cheaper plan's full price again, immediate, no credit). |
| Cancel | `cancel_at_period_end` by default; `immediately=true` ends now and truncates the period. Twice → 409. After failed payment (past_due) → allowed. Callback after cancellation → invoice paid, **plan not granted, subscription stays cancelled** (P17). |
| Resume | Only a cancel-pending, non-terminal subscription; no payment involved; no schedule duplication (flag flip only). |
| Reactivate suspended | Only by a verified card payment of the subscription's invoice (checkout by invoice). Bank-transfer recording or voiding does **not** reactivate (BILL-10). |

---

## 12. Invoice state machine

`draft → {open, void}` · `open → {paid, uncollectible, void}` · `paid → {open}` (only via reversal) · `uncollectible →
{paid, void}` · `void → ∅`. Amounts (`amount_due`, `plan_code`, `lines`) are snapshots **except** that an unpaid,
unissued checkout invoice is re-priced when a different plan is checked out for the same period (BILL-06). Sweep invoices
(`issued_at` set) are never re-priced by the sweep. `InvoiceService._settle` (manual payments) does not consult the
transition table (it can overpay a paid invoice and move `uncollectible → paid`); `void` refuses only `paid`.

## 13. Payment state machine

`pending → {succeeded, failed}` · `succeeded → {refunded}` · `failed → ∅` · `refunded → ∅`, plus the orthogonal
`collection_state` for automatic attempts (`claimed → requested → settled|abandoned`). One invoice ⇄ many attempts;
failures are retained (verified: real E2E ledger keeps the abandoned attempt beside the settled one). The table is
correct for a *transaction*; it is wrong for a *payment page*, because one Paymob order can carry several transactions
and the first declined one freezes the row (`failed → succeeded` refused) — BILL-07.

---

## 14. Checkout and idempotency

* Amount, currency and workspace are server-side (`CheckoutRequestPayload` forbids extras). Owner-only.
* Invoice and pending payment are written **before** the provider call; reference = payment id = `special_reference`.
* **Same idempotency key ×5 concurrently** (P8): 1 payment, 1 invoice, **1 Paymob intention**, 4 × 409. Mutation B08
  (key dropped) is killed.
* No key: every request opens a new page (documented); two pages paid → second payment `succeeded` but refused against
  the invoice — customer charged twice, recorded only as a log warning (BILL-15).
* Activation happens **only** in `_settle` behind a verified server callback. The browser redirect settles nothing (there
  is no route; the ngrok policy even shows the redirect path answering 404).
* **Price snapshot:** a callback is validated against `payment.amount` (the attempt's snapshot), not the current plan
  (P11 confirmed; mutation B17 killed). The snapshot that is *not* protected is the invoice's own plan/amount (BILL-06).

## 15. Renewal worker

| Property | Result |
|---|---|
| Selection | `claim_due` / `claim_collectible` / `claim_overdue` with `FOR UPDATE … SKIP LOCKED`, per-row transaction, re-claim by id with the same predicate; deleted tenants excluded |
| Two real workers, same due subscription (real Paymob) | 1 renewal invoice, 1 MIT attempt, **1 Paymob transaction** (541135994), period advanced once |
| Existing concurrency tests | `test_billing_sweep_concurrency.py`, `test_billing_concurrency.py` green; mutation B09 (subscription re-check removed) — see section 32 |
| Crash windows | TX1 claim+count → commit; TX2 `requested` → commit; provider call with no transaction; TX3 record. Verified by existing `test_billing_crash_recovery.py` (child-process kill) and P18. |
| Ambiguous timeout (P18) | pay request timed out → attempt stays `requested`; 5 further sweeps sent **0** additional charges; suspension skipped; chase still happens |
| Boundary | `current_period_end <= now` with the **application** clock (not DB time); exact-boundary inclusive; multi-replica skew only shifts by poll interval |
| Month-end | 31st → 28th and **stays** on the 28th for ever (BILL-18) |
| Interval | calendar months/years; leap-day yearly → Feb 28; all UTC-aware |

---

## 16. Paymob architecture in Wasla

| Flow | Endpoint | Auth | Integration |
|---|---|---|---|
| Hosted checkout | `POST /v1/intention/` → Unified Checkout `eg.checkout.paymob.com/?publicKey&clientSecret` | `Token <secret key>` | `PAYMOB_INTEGRATION_IDS` (card 5885262 in Test) |
| Saved card (CIT flow) | same intention; customer ticks "Save card"; TOKEN callback | — | card |
| MIT renewal | `POST /v1/intention/` with `payment_methods=[MOTO]` → `payment_keys[0].key` → `POST /api/acceptance/payments/pay {source:{identifier:<card token>, subtype:TOKEN}, payment_token}` | `Token <secret key>` | `PAYMOB_MOTO_INTEGRATION_ID` (5934829 in Test) |
| Refund | `POST /api/acceptance/void_refund/refund {transaction_id, amount_cents}` | `Token <secret key>` | — |
| Inquiry | `POST /api/auth/tokens {api_key}` → `POST /api/ecommerce/orders/transaction_inquiry {merchant_order_id}` | Bearer | — |
| Callbacks | `POST /api/v1/webhooks/paymob?hmac=` (TRANSACTION and TOKEN) | HMAC-SHA512 | — |
| Void / capture / CIT with `card_tokens` / Subscription module | **not implemented** | | |

Token classes are kept apart correctly (verified by code, real E2E and mutations B26/B28): the reusable credential is
`obj.token` (56 hex); `obj.id` is the token *record* id (stored as `provider_token_id`); the MOTO payment token comes
from `payment_keys[0].key`; the `client_secret` is only placed in the redirect URL and never stored.

---

## 17. Paymob official contract (reviewed 2026-09-24, developers.paymob.com)

| Page | Last updated | Contract relevant to Wasla | Wasla |
|---|---|---|---|
| Getting Integration Credentials | Jul 21 2026 | Secret/public keys are **mode-specific**; **API key is the same for Test and Live**; integration IDs are mode-specific; callbacks set per integration | matches; mode-mismatch guard only covers sk/pk pair |
| Test Credentials | Jun 1 2026 | Mastercard 5123456789012346 / 5123450000000008, Visa 4111111111111111, exp 01/39, CVV 123; wallet 01010101010 | used for E2E (entered by the account holder) |
| Create Intention | Jun 1 2026 | `Token` secret key; response gives **intention id, order id, client secret**; `special_reference` returned as `merchant_order_id`; `notification_url` per intention (card integrations only); `redirection_url` | uses `id` for correlation — **wrong key for TOKEN callbacks** (BILL-04) |
| Create Card Token | **Aug 24 2026** | TOKEN callback carries `order_id` (= `intention_order_id`) and intention id; sample `next_payment_intention` is a *new* intention | correlates `order_id` against stored intention id → never matches |
| HMAC Card Token Callback | **Aug 24 2026** | 8 fields: card_subtype, created_at, email, id, masked_pan, merchant_id, order_id, token; hmac in query | exact match |
| HMAC Transaction Callback | Jun 1 2026 | 20 fields (…, `obj.id`, …, `order.id`, …); **`merchant_order_id`, `integration_id`-binding and `is_live` are not in the signature beyond `integration_id` itself** | exact match; relies on unsigned `merchant_order_id` (BILL-11) |
| Transaction callbacks / Webhook Testing Tool | Jun 1 2026 | processed (POST) + response (GET redirect) callbacks; refunds/voids produce callbacks for the parent with `is_refunded`/`is_voided` | handled |
| CIT | Jun 28 2026 | `card_tokens` on the intention (≤3), card integrations | not used |
| MIT | Jun 28 2026 | intention on **Moto**; `payment_keys[0].key`; Pay Request `{source:{identifier, subtype:"TOKEN"}, payment_token}` | exact match (billing email aside, BILL-05) |
| Refund / Void / Capture | Jun 28 2026 | `transaction_id` + `amount_cents`; callbacks on parent | refund only |
| Transaction Inquiry by order id / reference | Jun 28 2026 | returns **the last** transaction for the order/merchant order id | used for MIT reconciliation only |
| Card Token Inquiry | Aug 4 2026 | token by order id | not used (would recover a missed TOKEN) |
| Subscriptions (feature + Create Subscription Plan) | Jun 1 / Jun 28 2026 | Moto integration, fixed frequencies, `webhook_url` | **NOT USED** (verified) |
| Pay With Saved Cards (feature) | Jun 1 2026 | card types Normal 3DS, Auth, Card On File, **Moto** | consistent |
| Integration Checklist | Jul 14 2026 | go-live requires paperwork, risk approvals, live credential issuance | Deployment backlog |

No current page disagrees with Wasla's HMAC or MIT request *shapes*. The incompatibilities are correlation (BILL-04),
the required valid billing email on MOTO intentions (BILL-05, discovered only by a real request — the docs do not say
it), and the unsigned correlation field (BILL-11).

---

## 18. Paymob Test Dashboard inventory

```text
Mode proof      toggle = "Test mode" ON and banner "You are in test mode. No real money will be charged." on every page read
                (home, payment integrations, each integration page, API keys). Account shows "Complete Onboarding" (Live not enabled).
                Keys in use: egy_sk_test_… / egy_pk_test_… (class Test). Live mode was never opened.
Merchant        MID 1221143 (dashboard); transaction `owner` 2448541
```

| Integration ID | Payment method | Channel | Currency | Created | Use |
|---|---|---|---|---|---|
| **5885262** | VPC (card) | online | EGP | 29 Aug 2026 | **normal card integration** (hosted checkout) |
| 5897483 | UIG (mobile wallet) | online_new | EGP | 06 Sep 2026 | not configured in Wasla |
| **5934829** | VPC | **moto** | EGP | 20 Sep 2026 | **MOTO** (confirmed also by the API: `method_type: moto`, `live: false`) |

| Credential | Present | Mode badge | Printed? |
|---|---|---|---|
| Secret key | yes | Test | no |
| Public key | yes | Test | no |
| API key | yes | none (mode-independent per docs) | no |
| HMAC secret | yes | **none** (appears mode-independent) | no |

Callback configuration (restoration baseline, identical on all three Test integrations):
Webhook `https://recycler-gondola-numeral.ngrok-free.dev/api/v1/webhooks/paymob`,
Redirect `https://recycler-gondola-numeral.ngrok-free.dev/billing/checkout/return`.
**Not modified** — Wasla sends `notification_url`/`redirection_url` per intention, so no Dashboard change was needed.
**Restored = not applicable (nothing changed).**

| Capability | Visible in Test | Production status |
|---|---|---|
| Card (online) | yes | UNKNOWN |
| Save card | yes (checkbox on hosted checkout) | UNKNOWN |
| MOTO | yes | UNKNOWN |
| Mobile wallet | yes (integration exists, not used by Wasla) | UNKNOWN |
| Kiosk / Apple Pay / BNPL | not present | UNKNOWN |
| Refund / void / capture APIs | documented; not exercised live here | UNKNOWN |

---

## 19. ngrok

* `ngrok version 3.39.9`. The account's only static domain `recycler-gondola-numeral.ngrok-free.dev` was **already online**,
  held by an agent (PID 8540) started at 15:47 by an earlier, now-idle billing-audit session (`f38f2a`), forwarding to a
  dead port (127.0.0.1:57455) with a traffic policy that admits only `POST /api/v1/webhooks/paymob` and
  `GET /billing/checkout/return`. A second agent was refused (`ERR_NGROK_334`); stopping the other session's process was
  declined by the permission system.
* Least-invasive path taken: the isolated audit API was bound to **127.0.0.1:57455**, so the existing tunnel served it
  without touching the agent, its policy, or the Dashboard. Only the webhook path is exposed; DB, Redis, MinIO, metrics
  and every other route answer 404 at the edge (verified).
* Evidence (from the agent's own inspector, non-secret fields only): 20:49:47 unsigned probe → 403; **20:52:59 TOKEN
  (Paymob 34.200.x) → 200**; **20:53:17 TRANSACTION 541130792 → 200**; **20:57:55 TRANSACTION 541135124 (MOTO) → 200**;
  21:03:04 TOKEN → 200; **21:03:34 TRANSACTION 541139493 → 200**; plus the MIT for 541135994.
* After the E2E the audit API was stopped, so the tunnel is back to forwarding to a dead port — its state before this
  audit. **The agent itself was not stopped** (not ours; see section 35).

---

## 20. Real initial-payment E2E

```text
Tenant            synthetic, slug bill-audit-…, registered via the real API (trialing on starter)
Checkout          POST /billing/checkout {"plan_code":"pro"} → 201
Wasla snapshot    payment afe8ebf0-f5b2-435a-a927-f6490cd7df76 · invoice acf7c0b3-8a00-4219-a79b-3d0aeb732a46 · 99.00 EGP
Paymob            intention pi_test_7dab… · order 617221643 · hosted page showed "EGP 99.00 / Pro plan" (Card; Save card offered)
Payment           official Test Mastercard …2346, entered by the account holder in Chrome, Save card ticked
Callback          20:53:17 via ngrok, hmac present, verified; amount_cents 9900, EGP, success, integration 5885262, is_live=false,
                  merchant_order_id = afe8ebf0… (our payment id)
Before → after    payment pending → succeeded (ref 541130792) · invoice open → paid 99.00/99.00 ·
                  subscription trialing/starter → active/pro (period restarted 17:53Z) · event 541130792:succeeded = applied
Amount proof      Paymob 9900 EGP cents == payments.amount snapshot 99.00 EGP (not merely the catalogue)
```

**VERIFIED.** A second real checkout (tenant 2, txn 541139493, card …0008) is reported under BILL-01.

## 21. Real saved-card E2E

* Paymob sent a real TOKEN callback 18 s **before** the transaction callback. HMAC verified (it would otherwise log
  `card_token_rejected`); Wasla logged **`billing.card_token_unmatched`** and stored nothing. Cause: `obj.order_id =
  617221643` (the Paymob order id) is matched against `payments.provider_intent_reference = pi_test_7dab…` (the intention
  id). `obj.next_payment_intention` is a *different* intention, so it cannot correlate either. Reproduced by an exact
  replay of Paymob's own request (original body + original Paymob hmac): still unmatched. A second real TOKEN (tenant 2)
  was also unmatched. **FAILED (BILL-04).**
* Labelled emulation, isolated E2E DB only: `payments.provider_intent_reference := '617221643'` for that one row (the
  value the correct code would have stored). Replaying Paymob's captured TOKEN request **3× concurrently** →
  **1** payment method: status active, default, masked `xxxx-xxxx-xxxx-2346`, brand MasterCard, `provider_token_id`
  16472381 (`obj.id`), `provider_token` = `v1.` AES-GCM envelope (125 chars), 64-char fingerprint, **0 plaintext-shaped
  tokens**, and a 1,305-column DB sweep found the real token nowhere. Ownership: the envelope is bound to
  `tenant:method` in the AAD; ownership came from our own payment row, not from `email`/`masked_pan`.
* Contract: `obj.id` (int 16472381) ≠ `obj.token` (56 hex) — Wasla stores and uses them correctly.

## 22. Real MOTO / MIT E2E

| Step | Evidence |
|---|---|
| Renewal due | `BillingWorker.run_once(now=2026-10-24T17:55Z)` — the subscription's natural renewal moment, no row edited. Issued invoice bd0fd80e… for **Sep 24 17:53 → Oct 24 17:53, 99.00** (the month the checkout already paid — BILL-03) |
| As shipped | MOTO intention → **Paymob HTTP 400 `{"billing_data":{"email":["Enter a valid email address."]}}`** → `ChargeNotSentError` → attempt `abandoned`, budget returned, retry +1 day (BILL-05, BILL-16) |
| Direct confirmation | the same body with a valid email → **201**, `payment_methods[0] = {integration_id 5934829, method_type moto, live false}`, `payment_keys[0].key` present |
| Emulated fix (in-process only, labelled) | worker re-run at Oct 25 with the placeholder email replaced → MOTO intention → `payment_keys[0].key` → Pay Request with the **decrypted real saved token** → Paymob txn **541135124** success → callback via ngrok 20:57:55 (integration 5934829, `is_live=false`, `merchant_order_id` = automatic payment c68785cd…) → attempt `settled`, invoice paid, period unchanged (already advanced by the sweep) |
| Replay ×3 concurrent | 3 × `billing.callback_duplicate`; one settlement; period end unchanged |
| Duplicate invocation | two worker processes at Nov 24 simultaneously → 1 invoice, 1 attempt, **1 Paymob charge (541135994)** |
| Real MIT failure | synthetic *invalid* token sealed with the audit's keys as default card → worker at Dec 24 → Paymob refused the Pay Request → attempt `failed`/`settled` ("The provider refused the automatic charge."), counted 1/3, retry +1 day, invoice open |

**As shipped: FAILED. With labelled emulation: VERIFIED.**

### Second independent run (same day, fresh workspace, fresh synthetic keys)

| Step | Result |
|---|---|
| Checkout | payment c74e8a23…, 99.00 EGP; card …2346 entered by the account holder with Save card ticked |
| Transaction callback | txn **541205063**, order 617299535, integration 5885262, `is_live=false`, via ngrok 22:23:17 → applied; invoice paid; workspace → Pro |
| TOKEN callback | 22:23:00 (17 s **before** the transaction), `order_id` 617299535 vs stored `pi_test_9762…` → `card_token_unmatched` again (third real reproduction of BILL-04); exact replay also unmatched |
| Emulated correlation | same labelled one-row change; 3 concurrent replays → 1 card, `v1.` envelope, masked PAN only |
| Renewal as shipped (Oct 24) | MOTO intention refused (BILL-05) → abandoned, retry **+1 day** (BILL-16); renewal invoice again covers Sep 24 → Oct 24, already paid by the checkout (BILL-03) |
| Renewal with email emulation (Oct 25) | real MIT txn **541206542** on MOTO 5934829 → callback via ngrok → settled, invoice paid |
| MIT callback replay ×3 concurrent | 3 × duplicate; period, events and paid-invoice count unchanged |
| Two concurrent workers (Nov 24) | 1 invoice, **1** Paymob charge (541206845), period advanced once |
| Secret sweep of the run's logs | 0 hits over 11 sentinels (keys, HMAC, both real card tokens, synthetic keys) and 0 client-secret / hmac / payment-token patterns |

Both runs agree on every result.

## 23. Callback authenticity, HMAC and replay

Probes P9/P10 (real `PaymobProvider`, real HMAC, real settlement) and real traffic:

| Case | Result |
|---|---|
| Missing / wrong / non-ASCII / upper-cased valid hmac | refused (401 path → 403 response; docs say 401 — trivia) with no side effect before verification |
| Modified amount / integration_id after signing | refused |
| **Modified `order.merchant_order_id` after signing** | **accepted** — field is not in Paymob's signed set (BILL-11) |
| Amount ≠ attempt snapshot | `mismatched`, payment untouched |
| Currency ≠ invoice currency | `mismatched` |
| Unknown / non-UUID reference | 200 `received`, nothing recorded against any tenant |
| `success=true` + `pending=true` | `no_change` |
| Same event twice / ×3 concurrent (real) | `duplicate`, one effect |
| Late failure / pending for a settled transaction | `refused` |
| Decline then success on the same order | **refused** (BILL-07) |
| TOKEN callback | 8-field HMAC verified; duplicate ×3 concurrent → one card |
| Refund callback | applied once; parent-transaction fallback correlation works (P16) |
| Integration / mode binding | **absent** — `integration_id` and `is_live` are not compared with configuration (BILL-11) |

## 24. Saved-card security

Held: row-bound AES-GCM (`payment-card-token:v1:{provider}:{tenant}:{method}`) — mutation B14 (AAD unbound) killed;
plaintext storage (B15) killed; fingerprint dedup (B16) killed; token never in API responses (`PaymentMethodRead` has no
token field; B25 killed), never in logs (sentinel sweep 0), never in audit meta. Rotation: `CredentialCipher` key ring
with key-id per envelope — covered by `test_credential_encryption.py`/`test_payment_token_migration.py` (0069
migration), not re-derived here. Card management: list/default/revoke are owner-only and tenant-scoped (404 for foreign
and missing alike). Revoking the default card does **not** promote another card; renewals then fall back to invoicing.
A card already saved by workspace A cannot be saved by workspace B (global fingerprint uniqueness returns A's row and
B silently gets no card) — safe, but a silent functional gap. A revoked card re-saved stays revoked (same reason).

---

## 25. Failure and crash matrix

| Failure | Behaviour | Safe? |
|---|---|---|
| Crash after MIT claim, before `requested` | reconciler closes as abandoned (existing crash test) | yes |
| Crash after `requested` / during request / after provider success before commit | attempt stays unresolved; no second charge (partial unique index + claim predicate); reconciler inquires by `merchant_order_id` | yes, **if** `PAYMOB_API_KEY` is set |
| HTTP timeout on Pay Request (P18) | unresolved; 0 further charges over 5 sweeps | yes |
| Paymob 5xx / connection refused on intention | `ChargeNotSentError` → abandoned → backoff (1 day in production, BILL-16) | yes |
| Paymob 4xx on intention | same as above — **but a permanent 4xx (BILL-05) is retried daily for ever** | no-charge-safe, liveness-unsafe |
| Paymob 4xx on Pay Request | failed, attempt spent | yes |
| Paymob 429 | retryable label | yes |
| Malformed JSON 2xx | ProviderError non-retryable | on Pay Request this is recorded as a refusal although the charge may have happened (edge) |
| Callback crash between event claim and settlement | one transaction (claim is a savepoint in the request transaction) → full rollback → Paymob retries → converges | yes |
| **Lost hosted-checkout callback** | payment `pending` for ever; reconciliation ignores non-automatic attempts; no Card-Token or order inquiry | **no** (BILL-09) |
| Redis outage | billing correctness does not depend on Redis (DB claims/constraints only) | yes |
| PostgreSQL failure mid-settlement | transaction rolls back; Paymob retries | yes |
| Provider error text | truncated to 300 chars, own secrets redacted, generic message to tenant | yes |

## 26. Refund / void / capture

* **Refund: implemented** — owner self-service, 202, whole remaining balance only, request committed before the
  provider call, one outstanding request at a time, confirmation only by signed callback, plan withdrawn when the invoice
  is emptied (ADR-096). Partial refunds arrive only from the Dashboard and are applied. Duplicate/over-refund: amount is
  server-computed; refund callback `> payment.amount` → mismatched. **No eligibility window or operator approval**:
  P16 refunded 99 EGP after 29 days of Pro — the service consumed stays consumed (BILL-13). No real Paymob refund was
  performed.
* **Void: NOT PRESENT** (adapter-level only in docs; no route). **Capture: NOT APPLICABLE** (sale transactions only).

## 27. Reconciliation

| Question | Answerable today? |
|---|---|
| Which invoices are open? | yes (`/platform/overview` outstanding, SQL) |
| Which Paymob transactions are uncorrelated? | only from logs (`billing.callback_unknown_payment`) — not stored (unknown-reference events are not written to `payment_events`) |
| Succeeded at Paymob but not locally? | **automatic attempts: yes** (reconciler + metrics). **Hosted checkouts: no** (BILL-09) |
| Local payment without provider transaction | SQL on `payments.provider_reference IS NULL` |
| Stuck renewals | `wasla_oldest_pending_payment_age_seconds` — metric exists, **no alert** (BILL-14) |
| Double-paid invoices | only from `payment_events.outcome='refused'` + log warning (BILL-15) |

Inquiry returns only the **last** transaction per order (docs); fine for single-attempt MIT orders, insufficient as a
general reconciliation primitive for hosted pages with several attempts.

## 28. Authorization and tenant isolation (real HTTP, E2E API)

| Route | member | tenant_admin | foreign owner | anonymous |
|---|---|---|---|---|
| GET plans / subscription / entitlements | 200 | 200 | 200 (own) | 401 |
| GET invoices, invoice, invoice payments | 403 | 403 | 404 (foreign id) | 401 |
| GET payment / POST refund | 403 | 403 | 404 | 401 |
| GET/POST default/DELETE payment-methods | 403 | 403 | 404 | 401 |
| POST checkout (foreign invoice id) | 403 | 403 | 404 | 401 |
| POST subscription/plan, cancel, resume | 403 | 403 | 409 (own, terminal) | 401 |
| POST /platform/invoices/{id}/payments, /void | 403 | 403 | 403 | 401 |
| Nonexistent ids | 403 | 403 | **404 — identical to foreign** | 401 |

Platform routes admit `PLATFORM_OWNER` and `PLATFORM_ADMIN` (covered by `test_billing_authorization.py`). Webhook
tenant resolution reads the tenant off our own payment row; mutations B01/B02 (tenant filters removed) are killed.
No billing-derived weakness was found in the closed Authorization subsystem.

## 29. Concurrency matrix (real PostgreSQL)

| Race | Result |
|---|---|
| checkout × checkout (same key ×5) | 1 payment · 1 intention ✔ |
| renewal × renewal (two worker processes, real Paymob) | 1 charge ✔ |
| callback × callback (real, ×3) / TOKEN × TOKEN (real, ×3) | 1 effect ✔ |
| callback × cancel (P17) | payment captured, nothing granted ✘ (BILL-01 class) |
| callback × upgrade (P2) | cheaper payment grants pricier plan ✘ (BILL-06) |
| renewal × ambiguous prior attempt (P18) | no second charge ✔ |
| plan edit × pending checkout (P11) | snapshot honoured ✔ |
| plan edit × renewal (P11) | renewal at new price, silently ✘ (product decision) |
| agent create × agent create at limit (P7) | limit exceeded ✘ (BILL-08) |
| delete card × renewal | revoke flips `status`; MIT reads the default *active* card at claim time — no orphan charge (code review) ✔ |

## 30. Observability

Present: structured events for every billing step (`billing.*`), provider-call metrics labelled `paymob/<operation>`,
reconciliation counters and oldest-pending age, auth-refusal counter for `paymob_webhook`. Metric labels carry no
amounts, tokens, e-mails or phones (reviewed). **Missing:** any alert rule for billing (BILL-14); metrics for checkout
started / payment succeeded / failed / duplicate / mismatched / renewal outcome. Log sentinel sweep: **0** occurrences of
secret key, public key, API key, HMAC secret, any of 3 real card tokens, client secrets, MOTO payment tokens or
Authorization headers across all API/worker logs and audit files.

Billing e-mails (`INVOICE_ISSUED`, `SUBSCRIPTION_SUSPENDED`, `SUBSCRIPTION_CANCELLED`, `TRIAL_EXPIRED`) carry only amount,
currency, period and workspace name — no invoice/payment id, card data, token or payment link. Every free-Starter
workspace receives "your trial has ended" on day 14 (see BILL-01). Retention: invoices, payments, payment events,
subscriptions and audit logs are kept for ever by the purge; saved cards are revoked at deletion and erased by the purge;
failed attempts are never deleted (BILL-19 for the cascade caveat).

## 31. Billing invariants

Populated: probe DB 27 tenants / 27 subscriptions (active 4, past_due 3, suspended 13, cancelled 1, expired 6) / 60
invoices / 41 payments / 33 events / 5 cards; E2E DB 2 tenants with real Paymob data (5 invoices, 6 payments, 4 events,
2 cards). Every zero below is over a non-empty population.

| # | Invariant | probe | real E2E |
|---|---|---|---|
| I02/I03 | payment ⇄ invoice ⇄ card same tenant | 0 | 0 |
| I04 | one provider transaction → one payment | 0 | 0 |
| I05 | one provider event → one row | 0 | 0 |
| I06 | amount_paid ≤ amount_due | 0 | 0 |
| I08 | saved-token plaintext count | **0** | **0** |
| I09/I10 | ≤1 default card; ≤1 unresolved MIT attempt per invoice | 0 | 0 |
| I16/I17 | currency match; no negative money in data | 0 | 0 |
| I11/I19 | priced plan held without a covering settled invoice | **1** | 0 |
| I12 | paid invoice, plan never granted, subscription terminal | **2** | **1** |
| I13 | overlapping invoices for the same priced plan | **16** | **1** |
| I14 | open invoice with money on it and no `issued_at` (never dunned) | **1** | 0 |
| I15 | succeeded payment refused by its invoice (silent overpayment) | **3** | 0 |
| I20 | invalid plan money configuration | 0 (seed) | 0 |

---

## 32. Mutation matrix

Worktree `E:\wasla-billing-mutation`, lane 2 (own Redis, DB `wasla_mut`), killer suite = the 35 billing-targeted files with `-x`. A mutant is KILLED only by a genuine test failure (no INFRA outcomes occurred). Every file was restored byte-for-byte (sha256 before = after for all 30); `git status` of the mutation worktree is clean.

| ID | Property broken | File | Result | Killer test | sha256 before → after |
|---|---|---|---|---|---|
| B01 | invoice lookup without tenant filter | `app/repositories/invoice_repository.py` | **KILLED** | `tests/integration/test_billing_authorization.py::test_a_checkout_cannot_be_started_against_another_workspaces_invoice` | `4e2e5c0239ba6b74` → `4e2e5c0239ba6b74` |
| B02 | payment-method lookup without tenant filter | `app/repositories/payment_method_repository.py` | **KILLED** | `tests/integration/test_billing_authorization.py::test_another_workspaces_card_is_not_found` | `4b39892ef982093f` → `4b39892ef982093f` |
| B03 | transaction callback trusted without HMAC | `app/integrations/billing/paymob.py` | **KILLED** | `tests/e2e/test_billing_lifecycle.py::test_a_forged_callback_cannot_settle_anything_over_a_real_socket` | `0aadf9d136168b29` → `0aadf9d136168b29` |
| B04 | callback amount not checked | `app/services/checkout_service.py` | **KILLED** | `tests/e2e/test_billing_lifecycle.py::test_a_forged_callback_cannot_settle_anything_over_a_real_socket` | `139d1490c6f84475` → `139d1490c6f84475` |
| B05 | callback currency not checked | `app/services/checkout_service.py` | **KILLED** | `tests/integration/test_paymob_checkout.py::test_a_callback_in_a_different_currency_is_refused` | `139d1490c6f84475` → `139d1490c6f84475` |
| B07 | provider event id made unique per delivery (no replay dedup) | `app/services/checkout_service.py` | **KILLED** | `tests/integration/test_billing_concurrency.py::test_four_simultaneous_deliveries_settle_the_invoice_once` | `139d1490c6f84475` → `139d1490c6f84475` |
| B08 | checkout idempotency key dropped | `app/services/checkout_service.py` | **KILLED** | `tests/integration/test_billing_concurrency.py::test_one_idempotency_key_opens_one_payment_page` | `139d1490c6f84475` → `139d1490c6f84475` |
| B09 | subscription claim without SKIP LOCKED re-check (by id) | `app/repositories/billing_repository.py` | **KILLED** | `tests/integration/test_billing_sweep_concurrency.py::test_two_workers_advance_every_subscription_exactly_once` | `fee44a1d2c86c7be` → `fee44a1d2c86c7be` |
| B10 | plan granted on a failed payment | `app/services/checkout_service.py` | **KILLED** | `tests/e2e/test_billing_lifecycle.py::test_a_declined_payment_leaves_the_workspace_exactly_as_it_was` | `139d1490c6f84475` → `139d1490c6f84475` |
| B12 | any payment transition allowed (stale overwrite) | `app/db/models/invoice.py` | **KILLED** | `tests/integration/test_declined_and_saved_cards.py::test_a_declined_payment_cannot_later_succeed` | `b7d7b4632317ed69` → `b7d7b4632317ed69` |
| B14 | token AAD not bound to tenant/method | `app/services/payment_token_service.py` | **KILLED** | `tests/unit/test_payment_token_protection.py::test_ciphertext_cannot_be_moved_to_another_row_or_used_as_plaintext` | `a519734299aa8cc7` → `a519734299aa8cc7` |
| B15 | saved token stored in plaintext | `app/services/payment_method_service.py` | **KILLED** | `tests/integration/test_declined_and_saved_cards.py::test_a_saved_card_stores_a_token_and_no_card_number` | `1a87ae14273501be` → `1a87ae14273501be` |
| B16 | fingerprint dedup removed | `app/services/payment_token_service.py` | **KILLED** | `tests/integration/test_declined_and_saved_cards.py::test_a_repeated_saved_card_notification_creates_one_card` | `a519734299aa8cc7` → `a519734299aa8cc7` |
| B17 | checkout reprices even the same plan (snapshot lost) | `app/services/checkout_service.py` | **SURVIVED** | `—` | `139d1490c6f84475` → `139d1490c6f84475` |
| B20 | entitlement guard no longer refuses | `app/api/dependencies.py` | **KILLED** | `tests/integration/test_plan_enforcement.py::test_a_full_plan_refuses_another_agent` | `26a4ed0dfb72f94b` → `26a4ed0dfb72f94b` |
| B21 | AI-turn consumption without advisory lock | `app/services/entitlement_service.py` | **KILLED** | `tests/integration/test_ai_metering.py::test_concurrent_turns_never_oversell_the_turn_allowance` | `e44ab873f48293dd` → `e44ab873f48293dd` |
| B22 | any member may start a checkout | `app/api/v1/billing.py` | **KILLED** | `tests/integration/test_billing_authorization.py::test_only_an_owner_may_start_a_checkout[member]` | `4f20d2acc1f419f9` → `4f20d2acc1f419f9` |
| B23 | ambiguous MIT timeout treated as not-sent (retry) | `app/services/recurring_service.py` | **KILLED** | `tests/integration/test_recurring_billing.py::test_a_timed_out_charge_is_unknown_rather_than_failed` | `d04ad0b11345e035` → `d04ad0b11345e035` |
| B24 | saved card token logged | `app/integrations/billing/paymob.py` | **SURVIVED** | `—` | `0aadf9d136168b29` → `0aadf9d136168b29` |
| B25 | saved token exposed in API response | `app/schemas/invoice.py` | **KILLED** | `tests/integration/test_billing_authorization.py::test_the_card_token_never_leaves_through_the_api` | `dae9e94d83d08d6f` → `dae9e94d83d08d6f` |
| B26 | obj.id used as the reusable token | `app/integrations/billing/paymob.py` | **KILLED** | `tests/integration/test_declined_and_saved_cards.py::test_a_saved_card_stores_a_token_and_no_card_number` | `0aadf9d136168b29` → `0aadf9d136168b29` |
| B27 | MIT intention on the customer integration, not MOTO | `app/integrations/billing/paymob.py` | **KILLED** | `tests/integration/test_recurring_billing.py::test_the_charge_uses_the_moto_integration_and_our_own_reference` | `0aadf9d136168b29` → `0aadf9d136168b29` |
| B28 | client_secret used instead of payment_keys[0].key | `app/integrations/billing/paymob.py` | **KILLED** | `tests/integration/test_recurring_billing.py::test_the_charge_uses_the_moto_integration_and_our_own_reference` | `0aadf9d136168b29` → `0aadf9d136168b29` |
| B29 | callback no longer resolves an unresolved collection attempt | `app/services/checkout_service.py` | **SURVIVED** | `—` | `139d1490c6f84475` → `139d1490c6f84475` |
| B30 | period invoice idempotency check removed | `app/services/invoice_service.py` | **KILLED** | `tests/integration/test_billing_crash_recovery.py::test_an_unresolved_attempt_blocks_the_next_charge` | `f9c446f0be92cea8` → `f9c446f0be92cea8` |
| B31 | cancelled subscription revived by payment | `app/services/checkout_service.py` | **KILLED** | `tests/integration/test_dunning_lifecycle.py::test_an_old_callback_does_not_revive_an_ended_subscription[cancelled]` | `139d1490c6f84475` → `139d1490c6f84475` |
| B32 | cancelled workspace still auto-charged | `app/services/recurring_service.py` | **KILLED** | `tests/integration/test_billing_worker.py::test_the_sweep_never_charges_a_cancelled_workspace` | `d04ad0b11345e035` → `d04ad0b11345e035` |
| B33 | suspension ignores unresolved collection attempts | `app/workers/billing_worker.py` | **KILLED** | `tests/integration/test_dunning_lifecycle.py::test_a_workspace_whose_charge_may_have_landed_is_not_suspended` | `c6408eac0c0829bb` → `c6408eac0c0829bb` |
| B34 | self-service may select a priced plan | `app/services/subscription_service.py` | **KILLED** | `tests/e2e/test_billing_lifecycle.py::test_paying_a_plan_settles_the_invoice_and_moves_the_entitlements` | `45bbd36ef639586a` → `45bbd36ef639586a` |
| B35 | deleted workspaces not excluded from collection | `app/repositories/invoice_repository.py` | **KILLED** | `tests/integration/test_workspace_billing_lifecycle.py::test_automatic_collection_will_not_claim_a_deleted_workspaces_invoice` | `4e2e5c0239ba6b74` → `4e2e5c0239ba6b74` |

**Applied 30 · killed 27 · survived 3 · inapplicable 6.**

Survivors (test gaps, see section 33):

* **B17** — checkout re-prices an invoice even for the same plan. Nothing asserts that a second checkout for the *same* plan leaves the first attempt's invoice snapshot alone; the adjacent real defect is BILL-06.
* **B24** — the saved card token written into `billing.paymob_saved_method_charged`. No test captures provider-adapter logs for token sentinels (the sentinel sweep in this audit found none in practice).
* **B29** — a callback no longer moves an unresolved automatic attempt to `settled`. Only the reconciler path is tested; with the callback path mutated the invoice would stay blocked from further collection and from suspension until reconciliation, and without `PAYMOB_API_KEY` for ever.

Inapplicable (brief's list adapted to the implementation):

* **B06** stop checking the Paymob order — Wasla never checks it (BILL-11).
* **B11** advance the period twice on replay — settlement never advances a period (the sweep does); replay safety is B07.
* **B13** accept a foreign saved card — same code path as B02.
* **B18** allow a negative price — nothing forbids it today (BILL-12).
* **B19** allow deleting a plan with subscribers — enforced only by the migration's FK `RESTRICT`, not by application code; probe P12 confirmed the DB refuses.
* **Resource-limit lock (brief B21)** — no lock exists to remove (BILL-08); B21 here mutates the AI-turn advisory lock instead, and it was killed.

## 33. Structural test gaps

1. **Paymob identifiers are asserted, not observed.** `test_paymob_webhook_endpoint.py` sets
   `payment.provider_intent_reference = order` by hand before sending a TOKEN callback, so the test proves correlation
   works for a value production never stores (BILL-04).
2. **MIT request bodies are only compared to mocks.** No test asserts Paymob's validation rules for `billing_data`
   (BILL-05); the mock accepts anything.
3. **Session semantics differ from production.** Test sessions autoflush; the app's do not (`autoflush=False`), which
   hides BILL-16.
4. **Commercial journeys are tested per step, not end to end over time.** No test pays after the trial expires
   (BILL-01), pays after cancel (P17), buys then runs two renewals and sums what was billed (BILL-03), or opens two
   checkouts for different plans before paying (BILL-06).
5. **Payment ≠ transaction is not modelled in tests.** Every callback test uses one transaction per payment (BILL-07).
6. **Concurrency tests cover money, not limits.** No race test for any resource entitlement (BILL-08).
7. **No test for the "unsigned correlation field"** — tampering tests only mutate signed fields (BILL-11).
8. **Catalogue invariants live nowhere** — no DB CHECKs, no service, no test for a negative price (BILL-12).
9. **Mutation survivors B17, B24, B29** — invoice snapshot on same-plan re-checkout, token-in-log sentinel for the provider adapter, and callback resolution of an unresolved MIT attempt are untested.

## 34. Product decision ledger

| Decision | Current behaviour | Risk | Safe options |
|---|---|---|---|
| Existing subscribers on a price change | new price at next renewal, silently | surprise charges, disputes | grandfather via price snapshot on subscription; or notice + effective date |
| Existing subscribers on a limit change | immediate | silent downgrade of paid service | version plans; apply at next period |
| Paid purchase from expired/cancelled | money taken, nothing granted | BILL-01 | refuse checkout for terminal subs; or start a fresh subscription on settlement |
| Should a free plan have a "trial"? | Starter trial expires to EXPIRED | root of BILL-01 | no trial on free plans / expiry → active on free plan |
| Upgrade timing & proration | immediate, period restarts, full price, no credit | BILL-03 double billing | bill in advance consistently; or treat checkout as covering the new period |
| Downgrade priced → free | immediate, no refund of remainder | forfeited paid time | effective at period end |
| Cancel | period end (default) / immediate | — | keep |
| Grace / dunning | past_due at +7 d, suspended at +30 d, from `issued_at` | — | per-plan configuration if needed |
| Retry schedule | +1 d, +3 d, stop at 3 | — | keep; expose to customers |
| Refund eligibility | owner self-service, any time, full remaining | BILL-13 | window + operator approval; pro-rata |
| Refund effect on entitlement | withdraw only when invoice emptied | partial refunds keep plan | keep, document |
| Suspended (platform) workspace billing | still charged; owner locked out | BILL-17 | pause collection while platform-suspended |
| Hosted-page retries after decline | refused | BILL-07 | settle any successful transaction of our order |
| Payment method on removal of default | no auto-promotion | failed renewals | promote most recent active card, or ask |
| Currencies | EGP by data only | none today | enforce by constraint until multi-currency is designed |

## 35. Deployment verification backlog (Live — not attempted)

* Live secret/public keys (`sk_live`/`pk_live`), API key, **HMAC secret mode-scope** (appears shared with Test — confirm).
* Live card integration id, **Live MOTO id**, Live callback/redirect configuration, production merchant capabilities,
  card-saving and MIT/MOTO approval, transaction limits, settlement.
* `PAYMOB_INTEGRATION_IDS` / `PAYMOB_MOTO_INTEGRATION_ID` must be **Live** ids alongside live keys — Wasla cannot detect
  a Test integration id configured with Live keys (only the sk/pk pair is cross-checked; `is_live` is not read).
* `PAYMOB_API_KEY` present in production (without it hosted and MIT attempts whose callback is lost never resolve).
* Real production token population before running migration 0069 (per DEPLOYMENT.md).
* Alert delivery for the rules BILL-14 asks for.
* ngrok: the static domain is still held by agent PID 8540 from idle session `f38f2a` (not this audit's, not stopped).
  Its Test-Dashboard webhook/redirect URLs point at that domain; nothing is listening behind it now.

## 36. Findings ledger

### BILL-01 — HIGH — Paid checkout after the free trial expires (or after cancellation) captures money and grants nothing
* **Status** open · **Classification** subscription lifecycle, payment integrity, product decision
* **Files** `app/services/checkout_service.py` (`start`, `_settle`, `_apply_purchased_plan`), `app/services/subscription_service.py` (`roll_over`), seed 0016 (`starter.trial_days=14`)
* **Failure model** Every workspace starts `trialing` on Starter; on day 14 the sweep sets `EXPIRED`. `POST /billing/checkout` does not look at subscription status, so it opens a page, Paymob captures the money, `_settle` marks the invoice paid, and `_apply_purchased_plan` returns early because the subscription `is_terminal`. `start` refuses ("already has a subscription") and `change_plan` refuses (terminal), so there is no route to the paid plan at all. Same for `CANCELLED` (P17).
* **Evidence** P1 (probe: invoice paid 99/99, subscription expired/starter, agents limit 1); **real Paymob Test**: tenant 2 expired by the sweep, paid 99 EGP (txn 541139493, callback 21:03:34 via ngrok, applied) → invoice paid, subscription `expired`, Starter limits; invariant I12 (probe 2, real 1).
* **Financial impact** every customer who buys after day 14 pays for a plan they never receive; refunds and support by hand.
* **Required behaviour** a settled purchase always yields exactly the plan paid for, or the checkout is refused before money moves.
* **Why tests missed it** journey not tested across the trial boundary.
* **Remediation** decide the product rule (section 34); either start a new subscription on settlement for terminal subscriptions, or refuse checkout for them and provide a re-subscribe path; remove the trial from the free plan.
* **Regression test** expire trial via the sweep → checkout → signed callback → plan granted (or 409 before provider).
* **Deployment verification** none beyond the fix.

### BILL-02 — HIGH — Abandoned upgrade checkouts are charged to the saved card by the renewal sweep
* **Classification** MIT/renewal, payment integrity · **Files** `app/repositories/invoice_repository.py` (`claim_collectible`, `claim_by_id`), `app/services/recurring_service.py` (`_refusal`)
* **Failure model** Checkout invoices are `open` with a `subscription_id` and no `issued_at`. The collectible claim has no `issued_at`/"sweep-issued" predicate, so within ten minutes the worker debits the default saved card for the abandoned invoice's full outstanding amount, and the callback then moves the workspace onto that plan.
* **Evidence** P5: Pro customer with saved card opens a Business checkout and abandons it → worker: MOTO intention **29900** cents + Pay Request → callback → subscription moved to **Business**.
* **Impact** merchant-initiated charge the customer never authorised (card-scheme MIT rules require an agreed recurring amount); disputes/chargebacks. Currently masked in production by BILL-04/BILL-05; becomes live when they are fixed.
* **Required** only sweep-issued renewal invoices (`issued_at IS NOT NULL`, subscription's own plan) are collectible by MIT.
* **Why missed** tests only create sweep invoices before collecting.
* **Regression test** abandoned checkout + saved card + sweep → no Pay Request.

### BILL-03 — HIGH — The first paid period is invoiced and collected twice
* **Classification** invoice integrity, pricing · **Files** `checkout_service._open_invoice`, `subscription_service.change_plan`, `billing_worker._invoice`
* **Failure model** Checkout bills the *current* period (period bounds of the old subscription) in advance; settlement then restarts the period at payment time; the sweep bills each period *in arrears* when it ends, starting with the one the checkout already paid for.
* **Evidence** P6: checkout + 2 months → 3 × 99 billed; **real Paymob**: invoice acf7c0b3 (checkout, paid by 541130792) and invoice bd0fd80e (Sep 24 17:53 → Oct 24 17:53, paid by MIT 541135124) — two 99 EGP charges for the same month. Invariant I13 (probe 16, real 1).
* **Impact** one extra month's fee per purchase (every upgrade/downgrade restarts the cycle and repeats it).
* **Required** one billing basis (advance *or* arrears) for both paths; a period is billed once.
* **Regression test** buy → run N renewals → Σ billed = N (or N+1 under an explicit advance model with no overlapping periods).

### BILL-04 — MEDIUM (release blocker for automatic renewal) — Real TOKEN callbacks never attach a saved card
* **Classification** Paymob, saved cards · **Files** `app/api/v1/payment_webhooks.py` (`_tenant_for_order`), `checkout_service.start` (stores intention `id` in `provider_intent_reference`), `paymob.create_checkout`
* **Evidence** two real HMAC-verified TOKEN callbacks → `billing.card_token_unmatched`; `obj.order_id` 617221643 vs stored `pi_test_7dab…`; Paymob docs (Create Card Token, Aug 24 2026) define `order_id` as `intention_order_id`; emulating the correct stored value makes the same real callback persist a card.
* **Impact** no card is ever saved; every "save card" customer is invoiced by email and walked into past_due/suspended; MIT never runs.
* **Why missed** the endpoint test hand-writes the order id into `provider_intent_reference`.
* **Remediation** persist `intention_order_id` at checkout and correlate on it (optionally also the intention id from the transaction's `payment_key_claims`); consider Card Token Inquiry by order id for missed TOKENs.

### BILL-05 — MEDIUM (release blocker for automatic renewal) — Every real MIT intention is refused (invalid billing e-mail) and retried for ever
* **Classification** Paymob, MIT/renewal · **File** `paymob.charge_saved_method` (`billing_data.email = "NOT_COLLECTED"`)
* **Evidence** real Paymob Test: 400 `{"billing_data":{"email":["Enter a valid email address."]}}`; identical body with a valid address → 201 on MOTO 5934829; with that single emulated change the real MIT succeeded (541135124).
* **Impact** automatic renewal can never charge; the refusal is filed as "not sent", the budget is returned and the attempt repeats daily without end or alert.
* **Remediation** send a real billing contact (owner e-mail) or a valid placeholder address; classify permanent 4xx intention errors as terminal and alert.

### BILL-06 — HIGH — A cheaper pending payment can buy a pricier plan (invoice re-pricing)
* **Classification** pricing, entitlements, payment integrity · **File** `checkout_service._reprice`, `_settle`/`_apply_purchased_plan`
* **Evidence** P2: Pro page (99) opened, Business page (299) opened (same invoice re-priced to 299), the 99 page paid → subscription **Business**, invoice open 99/299, `issued_at` null so never dunned; invariants I11/I14/I19.
* **Impact** Business for the Pro price, indefinitely for the first period; exploitable by any owner with two browser tabs.
* **Required** grant only when the invoice is fully paid for the plan the *payment* was created for; never re-price an invoice that has a live attempt.
* **Regression test** the P2 sequence.

### BILL-07 — HIGH — A successful retry after a declined attempt on the same checkout is refused
* **Classification** payment integrity, Paymob · **Files** `checkout_service._apply_collection`, `PAYMENT_TRANSITIONS`
* **Evidence** P3 (signed callbacks through the real adapter): `txn1:failed` applied → payment failed; `txn2:succeeded` → **refused** ("failed cannot become succeeded"); invoice open, plan not granted. Paymob's checkout allows several transactions per order (`single_payment_attempt: false` in its sample; the shared ngrok inspector also shows two transactions on one order from the earlier session). Not reproduced for real: Paymob Test did not decline a wrong CVV.
* **Impact** a customer who mistypes a card and retries is charged and gets nothing.
* **Why tests missed it** `test_a_declined_payment_cannot_later_succeed` pins the refusal on the premise that "a customer retrying produces another attempt and another row"; on Paymob's hosted page a retry is another *transaction* under the same order and `merchant_order_id`, i.e. the same payment row (mutation B12 is killed by exactly this test).
* **Remediation** model the payment page as an order with many transactions; a success for our order settles regardless of earlier declines (keep the decline as history).

### BILL-08 — MEDIUM — Count-based entitlements are not concurrency-safe
* **Files** `app/api/dependencies.py` (`require_entitlement` → `require()`), agent/number/member/document creation
* **Evidence** P7: two concurrent creations at `agents=1` → 2 agents. Same pattern for numbers, seats, documents; campaign audience uses `require()` too.
* **Remediation** reserve under the existing advisory lock (as `storage_bytes` does) in the creating transaction.

### BILL-09 — MEDIUM — A hosted-checkout payment whose callback is lost is never recovered
* **Files** `payment_reconciliation_service` (claims `collection_state IN (claimed, requested)` only)
* **Evidence** code path; hosted attempts have `collection_state NULL`; no inquiry, no Card Token inquiry, no operator settle-from-provider path; manual `record_payment` neither grants the plan nor lifts suspension (BILL-10).
* **Impact** "I paid and nothing happened" with no automated or safe manual resolution.
* **Remediation** reconcile pending hosted payments older than N minutes by `merchant_order_id` inquiry through the same `apply` path.

### BILL-10 — MEDIUM — Recording a bank transfer or voiding an overdue invoice leaves the workspace suspended
* **Files** `invoice_service.record_payment/_settle/void`, `platform_billing`
* **Evidence** P15b: suspended workspace → platform `record_payment` → invoice paid, subscription **still suspended**; `void` → still suspended; only a new card checkout restores (customer pays twice).
* **Remediation** route manual settlement through the same settlement state machine (restore, grant) and define void semantics.

### BILL-11 — MEDIUM — Settlement correlation uses an unsigned field; callbacks are not bound to integration or mode
* **Files** `paymob.verify_callback/_event`, `payment_webhooks._tenant_for`, `checkout_service._matching_payment`
* **Evidence** P9: a validly signed body with `order.merchant_order_id` changed is accepted; Wasla never compares `order.id` with the order it created, `integration_id` with its configured ids, or `is_live` with its mode; the Dashboard shows the HMAC secret without a mode badge.
* **Impact** anyone holding the merchant's *Test* secret key (developers, CI, staging) can create Test payments whose callbacks a Live deployment accepts as settlement for a chosen payment (same amount), i.e. free paid plans; any holder of a signed callback can re-target it to another same-amount payment before the genuine callback arrives.
* **Remediation** store the Paymob order id at intention time and require `order.id` to match; require `integration_id ∈ configured` and `is_live == production`.

### BILL-12 — MEDIUM — No plan-catalogue administration, validation, versioning or audit
* **Evidence** sections 8–9; P11 (existing subscribers re-priced silently), P20 (negative price/trial and currency `ZZZ` accepted by the DB).
* **Remediation** platform-owner plan API with validation, optimistic concurrency, audit trail and price snapshots on subscriptions; DB CHECKs on money columns.

### BILL-13 — MEDIUM — Owners can self-refund consumed service in full at any time
* **Evidence** P16: full 99 EGP refund after 29 days of Pro; plan withdrawn only then.
* **Classification** product decision, financial · **Remediation** refund window / operator approval / pro-rata (section 34).

### BILL-14 — MEDIUM — No billing alerting and thin billing metrics
* **Evidence** `deploy/monitoring/alerts.yml` has 0 billing rules; no `paymob_webhook` signature alert (email and WhatsApp have one on the same counter); no alert on `wasla_oldest_pending_payment_age_seconds` though the runbook calls it alertable; no Paymob-outage alert; no counters for checkout/payment/renewal outcomes or duplicate/mismatched callbacks. BILL-05's infinite retry would go unnoticed.

### BILL-15 — LOW — Double payments are recorded silently
* **Evidence** P4 / I15: second paid page → payment `succeeded`, event `refused`, invoice unchanged, only a log warning; no refund, alert or operator view.

### BILL-16 — LOW — First abandonment backs off one day instead of fifteen minutes
* **Evidence** production sessions use `autoflush=False`; `count_abandoned()` misses the just-abandoned row → `ABANDON_BACKOFF[-1]` (1 day). Seen in the real E2E (+1 day) and P19b; P19 with an autoflushing session gives 15 min.

### BILL-17 — LOW — A platform-suspended workspace keeps being charged while its owner is locked out
* **Evidence** P14 (renewal MIT sent for a `suspended` tenant) + `get_active_workspace` refuses suspended tenants on every billing route (no cancel/refund/card removal). Documented as intended ("untouched"); recorded as a product decision with customer-harm risk.

### BILL-18 — LOW — Month-end anniversaries drift permanently to the 28th
* **Evidence** `add_interval` chained from the previous `current_period_end`: Jan 31 → Feb 28 → Mar 28 … → Jan 28 (362 days/year); the docstring implies recovery.

### BILL-19 — LOW — Financial history cascades with the tenant row
* **Evidence** `invoices`, `payments`, `payment_events` FKs are `ON DELETE CASCADE` to `tenants`; retention relies on the purge never deleting the tenant row. A manual/erroneous delete erases the ledger.

### BILL-20 — LOW — Settlement of invoices outside the transition table
* **Evidence** `InvoiceService._settle` (manual) ignores `INVOICE_TRANSITIONS`, can pay an already-paid invoice again and pays `uncollectible` without checks; `void` allows any non-paid state.

### BILL-21 — INFO — Paymob Subscription module not used (verified); void/capture/tax/coupons/credits/proration/overage not present.
### BILL-22 — INFO — Hosted checkout shows the customer "NOT_COLLECTED" as billing name/phone; unsigned callbacks answer 403 where docs say 401.
### BILL-23 — INFO — Dead configuration: `trial_days` on priced plans, `Subscription.provider/provider_reference`, `owned_workspaces` with no values.

## 37. Billing score

Weights chosen before scoring.

| Dimension | Weight | Score | Weighted |
|---|---|---|---|
| Plan / catalogue correctness | 0.06 | 40 | 2.4 |
| Entitlement enforcement | 0.08 | 70 | 5.6 |
| Pricing integrity | 0.08 | 45 | 3.6 |
| Subscription lifecycle | 0.10 | 35 | 3.5 |
| Invoice correctness | 0.07 | 45 | 3.15 |
| Payment correctness | 0.09 | 50 | 4.5 |
| Paymob integration | 0.09 | 40 | 3.6 |
| Saved-card security | 0.06 | 90 | 5.4 |
| Renewal / MIT correctness | 0.07 | 45 | 3.15 |
| Idempotency / concurrency | 0.07 | 80 | 5.6 |
| Tenant isolation | 0.06 | 95 | 5.7 |
| Failure recovery | 0.05 | 65 | 3.25 |
| Reconciliation | 0.03 | 50 | 1.5 |
| Auditability | 0.03 | 55 | 1.65 |
| Testing | 0.03 | 60 | 1.8 |
| Operational readiness | 0.03 | 35 | 1.05 |
| **Total** | **1.00** | | **≈ 55 / 100** |

## 38. Final verdict

**NOT READY.** Keep what is strong — HMAC verification, the event ledger, tenant isolation, the ADR-088 collection
protocol, token protection and the snapshot-based amount check all survived real Paymob traffic, concurrency and
mutation. Before any live money: fix BILL-01, -02, -03, -06, -07 (a customer is charged for the wrong thing), fix
BILL-04 and -05 together *with* BILL-02 (automatic renewal), then BILL-11 and the operational set (BILL-09, -10, -12,
-14). Live Paymob verification (section 35) remains a separate deployment audit.
