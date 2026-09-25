# Wasla — Tenant Custom Plans & Top-ups Implementation

Two billing capabilities built on the completed billing remediation
([BILLING_FINDINGS_REMEDIATION.md](BILLING_FINDINGS_REMEDIATION.md), ADR-112), not
beside it: **tenant-scoped custom plans** and **one-time top-ups**. Decision record:
ADR-113 in [DECISIONS.md](DECISIONS.md). Reference: [docs/BILLING.md](docs/BILLING.md#custom-plans-and-top-ups-adr-113).
Operator procedures: [docs/BILLING_OPERATIONS.md](docs/BILLING_OPERATIONS.md).

| | |
|---|---|
| Starting HEAD | `e7c604e` on `billing-findings-remediation` (worktree `E:\wasla-billing-remediation`, clean; `alembic heads` `0071`, `alembic check` clean) |
| Branch / worktree | `custom-plans-topups` / `E:\wasla-custom-plans-topups` |
| Part I final code/test HEAD (historical) | `d3103ac2a033b8e31bf8ba76ac82b12dd12ff20a` (every gate in section 11 ran from this commit) |
| Part II final code/test HEAD | `e85f88326699c173f66c0fbd515b15ef3e7ecbaf` (the section 22 gates ran on exactly this code and these tests) |
| Part II report/docs HEAD | `09ba2adefa061680b106d54980967eb5caecaeab` |
| Current feature branch HEAD | the commit that carries this corrected report (`docs(billing): correct custom-plan Paymob verification report`); see `git log` |
| Current migration head / API operations | **`0073`** / **192** (Part I ended at `0072` / 186) |
| Alembic | `0071` → **`0072`** (Part I) → **`0073`** (Part II), single head, `alembic check` clean |
| Merge / push | Not merged or pushed when Parts I and II were written; the merge and push are recorded in [CUSTOM_PLANS_TOPUPS_MERGE_VERIFICATION.md](CUSTOM_PLANS_TOPUPS_MERGE_VERIFICATION.md). Paymob **Live** never used. |

**Part I** (sections 1-13) is the ADR-113 implementation as delivered at `d3103ac`.
**Part II** (sections 14-23) binds custom plans and top-ups to the current
official Paymob contract: custom plans become **offers** the customer accepts and
pays (ADR-114), and every provider claim is backed by the official
documentation, the Test dashboard and real Test-mode payments.

---

## 1. Summary

* **A custom plan is an ordinary `Plan` with `scope = tenant`,** owned by one
  workspace (`plans.tenant_id`) for ever, with ordinary immutable `PlanVersion`s,
  migrations and settlement. There is no second plan model. It can never be held
  by another workspace: the service layer refuses it on every assignment path, and
  database triggers on `subscriptions` and `invoices` refuse it for every writer.
* **A top-up is a frozen, one-time purchase** of extra allowance for one of seven
  keys, valid until the end of the billing period it was bought in. It is paid
  through the existing hosted checkout and settlement engine, granted exactly once,
  never charged to a saved card, never renewed, and never changes the plan.
* **The effective limit** is computed in the one place every limit is read,
  `EntitlementService.check`:
  `pinned plan version + active paid top-ups + active platform grants`. Every
  existing enforcement path, advisory lock and meter uses it unchanged.
* **Real Paymob Test E2E passed** (section 9): two real payments (350 EGP Test),
  AI turns 5,000 → 15,000, callback replay stays at 15,000, same plan and price,
  capacity 1 + 2 = 3 with the fourth refused, expiry at the boundary deleting
  nothing, zero invariant violations, zero secrets in logs.
* **Mutation testing** (section 8): 21 mutants, **21 killed, 0 survived, 0
  equivalent**, every file restored byte for byte.

## 2. Commits

| Commit | Message |
|---|---|
| `ba004db` | feat(billing): add tenant custom plans and one-time top-ups |
| `88d97d3` | test(billing): prove custom plans and top-ups end to end |
| `a307b67` | feat(observability): alert on top-up and custom-plan billing failures |
| `e10db55` | docs(billing): document custom plans and top-ups (ADR-113) |
| `d3103ac` | test(billing): read the summary's plan version from the pinned row |

`ba004db` carries the existing-test and registry updates the feature makes
necessary, plus only the two documented numbers the documentation-truth test pins
(at the time 186 operations and migrations `0001`–`0072`; Part II took them to 192 and `0073`). Its tree was verified in isolation
(ruff, black, mypy over 637 files; 3,135 unit and billing-adjacent tests) before
it was committed. The rest of the documentation is its own commit.

## 3. Schema changes (migration `0072`)

* **`plans`**: `scope` (`plan_scope`: public, private, tenant), backfilled from
  `is_public` (public stays public, everything else becomes private), and
  `tenant_id` (RESTRICT, indexed). CHECKs: `(scope = 'tenant') = (tenant_id IS NOT
  NULL)` and `(scope = 'public') = is_public`.
* **`topup_products`**: code (unique), name, description, `entitlement_key`
  (native enum of the seven keys), `quantity` (BIGINT > 0), `price` (0 ≤ price ≤
  1,000,000), `currency` = EGP, `scope` (global, tenant) with CHECK on `tenant_id`,
  `is_active`, `is_public`, `validity_policy` (`current_period_end`), `created_by`,
  revision, timestamps.
* **`topup_purchases`**: tenant (RESTRICT), product (RESTRICT, null for a grant),
  subscription (SET NULL), `source` (purchase, platform_grant), the frozen snapshot
  (product code and name, key, quantity, unit price, total, currency, billing
  period, `expires_at`), `status` (pending, paid, granted, expired, cancelled,
  refund_review), `invoice_id` (RESTRICT, **unique**), `payment_id` (RESTRICT),
  `paid_at`, `granted_at`, `ended_at`, `idempotency_key` (unique per tenant),
  reason, actor. CHECKs: quantity > 0; non-negative money; EGP; period ordered;
  `expires_at > billing_period_start`; granted/expired/refund_review ⇒
  `granted_at`; `expires_at > granted_at`; **a platform grant has no invoice, no
  payment, zero price and a reason**; a purchase has an invoice and a product.
* **Triggers** (installed identically by `create_all` and the migration):
  `subscriptions_custom_plan_scope` and `invoices_custom_plan_scope` (no workspace
  may point at another's custom plan: plan, pinned version, scheduled version,
  invoiced version); `plans_tenant_immutable`; `plans_derive_scope` (a plan
  inserted without a scope takes the one `is_public` implies);
  `payments_no_automatic_topup` (no MIT attempt against a TOPUP invoice);
  `topup_purchases_snapshot_immutable`; `topup_purchases_product_scope`.
* **Enum labels** (added last, outside the transaction): `invoice_purpose.topup`,
  six `billing_incident_kind` values, sixteen `audit_action` values.
* Verified: fresh chain `0001`→`0072` (72 migrations) to a single head,
  `alembic check` clean, downgrade to `0071` and upgrade again, clean. The
  downgrade refuses while any TOPUP invoice exists (the label cannot be dropped).

## 4. The seven entitlements

| Key | Kind | Counted by (unchanged) | Enforcement |
|---|---|---|---|
| `period_messages` | usage | `WHATSAPP_MESSAGE_SENT` + `_RECEIVED` meters | **Metered only** (ADR-030). A top-up raises the reported allowance and `remaining`; the API says `enforced: false`. No enforcement was invented. |
| `period_ai_turns` | usage | `AI_TURN` meter | `consume`, advisory lock (unchanged); exhaustion hands off to a person |
| `period_campaign_messages` | usage | `CAMPAIGN_MESSAGE` meter + other live campaigns' unsent recipients | `reserve_period` at scheduling, advisory lock (unchanged) |
| `storage_bytes` | capacity | SUM of occupying media bytes (integer) | `reserve` on upload (unchanged) |
| `whatsapp_numbers` | capacity | not disabled, not released | `reserve_or_refuse` on connect (unchanged) |
| `team_members` | capacity | active members + open invitations | `reserve_or_refuse` (unchanged) |
| `knowledge_documents` | capacity | documents, any indexing state | `reserve_or_refuse` (unchanged) |

Usage top-ups expire at the period end captured at checkout, with no carry-over.
Capacity top-ups also expire at the period end in v1. Nothing is deleted when they
do: the workspace reads `over_limit: true`, `remaining: 0`, and new creation is
refused until usage fits.

## 5. Effective-limit implementation

`EntitlementService.check` reads the pinned version's limit and adds
`TopupPurchaseRepository.active_totals(at=clock())`: `SUM(quantity)` grouped by
source where status ∈ {granted, refund_review} and `granted_at <= now <
expires_at`. It reads fresh on every check, so the figure taken under the
advisory lock is current. An unlimited base stays unlimited. `Entitlement` gains
`base_limit`, `topup_limit`, `grant_limit`, `over_limit` and the period; `limit`
remains the effective limit, so every caller is unchanged. A grant takes the same
per-workspace advisory lock as consumption, which orders it against in-flight
turns and reservations.

## 6. APIs

**Tenant** (`/api/v1/billing`): `GET /plans` (now includes the workspace's own
custom plan, `is_custom`), `GET /entitlements` (the breakdown), `GET /topups`
(members), `POST /topups/{id}/checkout` (owners; `idempotency_key`; 409 with
`details.purchase_id` on a repeat), `GET /topup-purchases` (owners), and
`GET /summary` (owners, Usage & Top-ups).

**Platform** (`/api/v1/platform/billing`, owner/admin; product delete owner-only):
`POST /tenants/{id}/custom-plan/preview`, `POST /tenants/{id}/custom-plan`,
`GET /tenants/{id}/summary`, `POST /tenants/{id}/topups/grant`,
`GET|POST /topups`, `GET|PATCH|DELETE /topups/{id}`, `POST /topups/{id}/activate|deactivate`,
`GET /topup-purchases`, `GET /topup-purchases/{id}` and
`POST /topup-purchases/{id}/refund-review`. `GET /plans` also takes `scope` and
`tenant_id`. At the end of Part I the production shape served **186 operations** (was 168); Part II brings it to **192**.

Custom plan creation requires all seven limits (`null` = unlimited, `0` = none;
omission is refused) and inherits `agents` and `owned_workspaces` from the current
plan. A priced plan applied `now` needs `financial_basis`: `customer_checkout`
(superseded by Part II: it now makes an offer the owner accepts and pays), `manual_payment` or
`complimentary`. `next_renewal` uses the existing scheduled-change machinery.
Every mutation is audited with actor, platform role, reason, before, after and
request id. Every read records a `platform_billing_read` access entry.

## 7. Invariants and concurrency

**Concurrency** (`tests/integration/test_topup_concurrency.py`: separate committed
connections started at an `asyncio.Barrier`, no sleep-based ordering; 9 tests,
stable across 6 consecutive runs):

| Race | Result |
|---|---|
| The same top-up callback ×4 | 1 applied, 3 duplicates; 1 grant; 1 event row; +5 exactly |
| The same checkout idempotency key ×4 | 1 created, 3 × 409; 1 purchase, 1 invoice, 1 payment, **1 Paymob intention** |
| Two workers settling one paid top-up | 1 grant, 1 grant audit (purchase row lock) |
| Two different successes on one page | 1 applied, 1 refused, 1 `topup_duplicate_payment` incident |
| 12 AI turns racing a +5 grant (base 5) | allowed = recorded ≤ 10; the remainder is exactly consumable afterwards |
| 3 campaigns (4 recipients each) racing +5 (base 8) | 2 or 3 scheduled, promised ≤ 13, never over the limit in force |
| Expiry racing number creation (1 + 2) | ≤ 3 held; at most one created after expiry; nothing deleted |
| Custom plan assign vs another assign (same revision) | 1 wins, 1 × 409 |
| Custom version publish race | 1 publishes v2, 1 × 409; no v3 |

**Invariants** (`tests/integration/test_topup_invariants.py`, over a ledger holding
every state; each is SQL that counts violations): T01 one workspace per
purchase/invoice/payment, T02 quantity > 0, T03 paid ⇒ granted or held with an
incident, T04 nothing unpaid granted, T05 grants carry no money, T06 no MIT on
TOPUP, T07 snapshot equals invoice, T08 product scope respected, T09 one purchase
per invoice, T10 expiry after grant at period end, C01 tenant plan ⇔ one owner,
C02 no subscription on another's custom plan, C03 no invoice for another's custom
plan, C04 pinned version belongs to the plan, C05 custom plan never public. The
cross-feature formula (spec 72) is recomputed independently in SQL for every
workspace and key and compared with the engine. **All zero** in tests and on the
real E2E database.

## 8. Mutation testing

Worktree `E:\wasla-cpt-mutation` (detached at `e10db55`), database `wasla_mut`.
`d3103ac` differs from `e10db55` in one test only
(`test_the_platform_company_summary_is_one_complete_read`, which is no mutant's
killer); `app/`, `alembic/` and `deploy/` are byte-identical, so the results
below hold for the final HEAD.
The killer suite is the six new test files (76 tests); the unmutated baseline
passed 76/76. Each mutant applies one textual change to a unique anchor, compiles
in memory with bytecode writing disabled (the previous run's stale-`.pyc` trap),
runs its named killer first and then the rest with `-x`, and counts KILLED only on
a real test failure. Every file is restored byte for byte and its sha256 compared.

| ID | Property broken | File | Result | Killer test | sha256 before → after |
|---|---|---|---|---|---|
| CT-01 | a TENANT plan without its workspace is refused | `app/schemas/platform_billing.py` | **KILLED** | `test_commercial_api.py::test_a_custom_plan_cannot_be_made_public_or_created_without_its_workspace` | `74f527312cae8251` → `74f527312cae8251` |
| CT-02 | a workspace's catalogue never shows another workspace's custom plan | `app/repositories/billing_repository.py` | **KILLED** | `test_commercial_api.py::test_a_custom_plan_is_one_workspaces_and_nobody_elses` | `4359f431e7c81b22` → `4359f431e7c81b22` |
| CT-03 | new terms publish a new version; an existing version is never mutated | `app/platform/plan_admin.py` | **KILLED** | `test_commercial_api.py::test_a_new_custom_version_changes_nobody_until_they_are_moved` | `ac55e96dd10b4bd5` → `ac55e96dd10b4bd5` |
| CT-04 | a custom plan cannot be assigned to another workspace | `app/db/models/billing.py` | **KILLED** | `test_commercial_api.py::test_a_custom_plan_is_one_workspaces_and_nobody_elses` | `4fa03772e4fdaf7a` → `4fa03772e4fdaf7a` |
| TU-01 | nothing is granted before the payment settles | `app/services/topup_service.py` | **KILLED** | `test_topups.py::test_a_paid_topup_raises_the_limit_once_and_leaves_the_plan_alone` | `ea112eaa826e0f5a` → `ea112eaa826e0f5a` |
| TU-02 | a paid top-up is granted exactly once however often settlement is asked | `app/services/topup_ledger.py` | **KILLED** | `test_topup_concurrency.py::test_two_workers_granting_one_paid_topup_grant_it_once` | `7fd44c6fef77c35c` → `7fd44c6fef77c35c` |
| TU-03 | a TOPUP invoice is never claimed for a saved-card (MIT) charge | `app/repositories/invoice_repository.py` | **KILLED** | `test_topups.py::test_a_topup_invoice_is_never_claimed_for_a_saved_card_charge` | `2b9ef80fc0be6be7` → `2b9ef80fc0be6be7` |
| TU-04 | the grant is the purchase snapshot, not the product's current quantity | `app/repositories/topup_repository.py` | **KILLED** | `test_topups.py::test_the_topup_granted_is_the_one_shown_at_checkout` | `886b2ebbf7977398` → `886b2ebbf7977398` |
| TU-05 | money arriving after the period is held, never granted | `app/services/topup_ledger.py` | **KILLED** | `test_topups.py::test_money_arriving_after_the_period_is_held_and_raised` | `7fd44c6fef77c35c` → `7fd44c6fef77c35c` |
| TU-06 | one workspace's limit never counts another's top-ups | `app/repositories/topup_repository.py` | **KILLED** | `test_topups.py::test_one_workspace_cannot_see_buy_or_read_anothers_topups` | `886b2ebbf7977398` → `886b2ebbf7977398` |
| TU-07 | a platform grant creates no payment | `app/platform/topup_admin.py` | **KILLED** | `test_topups.py::test_a_platform_grant_adds_allowance_and_is_never_a_sale` | `45ec25b5dfd3e878` → `45ec25b5dfd3e878` |
| TU-08 | the effective limit includes paid top-ups | `app/services/entitlement_service.py` | **KILLED** | `test_topups.py::test_a_paid_topup_raises_the_limit_once_and_leaves_the_plan_alone` | `caf5a39542449556` → `caf5a39542449556` |
| TU-09 | an expired top-up contributes nothing | `app/repositories/topup_repository.py` | **KILLED** | `test_topups.py::test_a_usage_topup_ends_with_its_period_and_does_not_carry_over` | `886b2ebbf7977398` → `886b2ebbf7977398` |
| TU-10 | a failed payment grants nothing | `app/services/checkout_service.py` | **KILLED** | `test_topups.py::test_a_declined_topup_payment_grants_nothing` | `207fc05575f566da` → `207fc05575f566da` |
| TU-11 | the same idempotency key is the same purchase | `app/services/topup_service.py` | **KILLED** | `test_topups.py::test_the_same_idempotency_key_is_the_same_purchase` | `ea112eaa826e0f5a` → `ea112eaa826e0f5a` |
| TU-12 | a capacity top-up's expiry deletes no resource | `app/workers/billing_worker.py` | **KILLED** | `test_topups.py::test_a_capacity_topup_expiry_keeps_every_number_and_blocks_new_ones` | `7dc36c3050f4eb5a` → `7dc36c3050f4eb5a` |
| TU-13 | an unknown entitlement key is refused | `app/schemas/topup.py` | **KILLED** | `test_commercial_api.py::test_topup_products_are_validated_versioned_and_listed` | `71ace099e24521e3` → `71ace099e24521e3` |
| TU-14 | a non-positive quantity is refused | `app/schemas/topup.py` | **KILLED** | `test_commercial_api.py::test_topup_products_are_validated_versioned_and_listed` | `71ace099e24521e3` → `71ace099e24521e3` |
| TU-15 | editing a product never rewrites an existing purchase | `app/platform/topup_admin.py` | **KILLED** | `test_topups.py::test_the_topup_granted_is_the_one_shown_at_checkout` | `45ec25b5dfd3e878` → `45ec25b5dfd3e878` |
| X-01 | a refund after the grant never withdraws the allowance on its own | `app/services/topup_ledger.py` | **KILLED** | `test_topups.py::test_a_refund_after_the_grant_never_subtracts_on_its_own` | `7fd44c6fef77c35c` → `7fd44c6fef77c35c` |
| X-02 | a platform grant is reported as a grant, not as a paid top-up | `app/services/entitlement_service.py` | **KILLED** | `test_topups.py::test_a_platform_grant_adds_allowance_and_is_never_a_sale` | `caf5a39542449556` → `caf5a39542449556` |

**Final: 21 mutants, 21 killed, 0 equivalent, 0 surviving.** Every file was
restored (21/21) and the worktree is clean. X-01 and X-02 go beyond the spec's
list.

Several mutants (CT-01, CT-03, CT-04, TU-05, TU-13, TU-14, TU-15) also meet a
database constraint or trigger behind the mutated code. That is the intended
defence in depth. Each is still killed by an assertion on the correct
application behaviour (a `422` with the right code, a `201`, an unchanged limit),
not by the database error alone. So the tests prove the application layer
refuses, and separate tests prove the database refuses independently.

One gap was found while planning the run and closed before it. TU-12 targets the
expiry sweep, but the capacity test originally proved expiry only by the clock.
It now also runs the real billing sweep across the boundary and asserts every
number survives.

## 9. Real Paymob Test E2E (2026-09-25)

**Setup.** Paymob **Test** keys only; the E2E env builder refuses to start unless
both keys carry `_test_`, and the hosted pages were `egy_pk_test` /
`egy_csk_test`. The API ran from this branch on `127.0.0.1:57455`, database
`wasla_e2e` migrated from empty to `0072`, isolated Redis database 7 (never DB 0).
The existing ngrok agent (PID 8540, static domain → `127.0.0.1:57455`) was reused
unchanged. No Paymob dashboard setting was changed. Checkouts were opened over
HTTP with a real access token. The cards were entered by the user on Paymob's
hosted Test pages. Integration 5885262.

| # | Scenario | Result |
|---|---|---|
| 1 | Before payment | **PASS**: `GET /billing/entitlements` → `period_ai_turns` base 5,000, effective **5,000** |
| 2 | AI Turns +10,000 (200 EGP): real payment, txn **541508368** | **PASS**: signed callback via ngrok → `applied`; invoice purpose **`topup`**, paid 200/200; purchase **granted** once; effective **15,000** (5,000 + 10,000) |
| 3 | Replay the genuine callback through the ngrok agent | **PASS**: API outcome `duplicate`, no new event row, effective **15,000** (not 25,000), still one grant |
| 4 | Top-up does not touch the plan | **PASS**: same plan, same `PlanVersion`, recurring price 99.00 before and after |
| 5 | Capacity: WhatsApp Numbers +2 (150 EGP), txn **541508739** | **PASS**: effective 1 + 2 = **3**; three numbers created under the real guard, the **fourth refused** |
| 6 | Renewal boundary (time moved only in this throwaway DB) | **PASS**: AI effective 15,000 at `period_end − 1s` and 5,000 at `period_end`; the real billing sweep (no provider, so it cannot charge) rolled the period and marked both purchases **expired**, with quantity, grant time and expiry unchanged |
| 7 | Capacity after expiry | **PASS**: all **3 numbers remain**; `over_limit: true`, `remaining: 0`; a new number is refused |
| 8 | Custom plan over the real platform API | **PASS**: preview 200; create 201 (`scope: tenant`); assigning it to the other company → **422 `custom_plan_not_available_for_workspace`**; owner sees it in `/billing/plans`, the other company does not; the other company's checkout of its code → 422 "No such plan."; company summary 200 |
| 9 | Invariants on the E2E database | **PASS**: all 15 at **0** |
| 10 | Secrets in logs | **PASS**: 0 client secrets, 0 Paymob secret key, 0 HMAC secret, 0 API key, 0 JWT secret in the API log |

Totals: 2 real Test payments, 350 EGP (Test), both on integration 5885262 in
`test` mode. The workspace's Pro plan was applied as a settled purchase would
apply it (the plan money path was proven against real Paymob by the billing
remediation); the top-ups stood on it. Custom-plan migration at renewal
(v1 → v2 exactly once) is proven deterministically in
`test_custom_plan_lifecycle.py`, not with real money.

**Cleanup.** The E2E API was stopped. The ngrok agent was left exactly as found.
Scratch files holding secrets are removed at the end of the session.

## 10. Metrics and alerts

Counters, every label a closed word with no workspace, product, invoice, payment
or amount:
* `wasla_billing_topup_checkout_total{entitlement,outcome}`;
* `wasla_billing_topup_grant_total{entitlement,source,outcome}`;
* `wasla_billing_topup_purchase_total{entitlement,outcome}` (settled, expired,
  cancelled, refund_review, kept, withdrawn, callback_mismatched, stuck);
* `wasla_billing_custom_plan_total{operation,outcome}`.

`wasla_billing_incidents_total` gains the six new kinds.

Alerts (`wasla-billing-topups`): `BillingTopupPaidNotGranted` (critical),
`BillingTopupSettlementFailure`, `BillingTopupCallbackMismatchSpike`,
`BillingTopupReconciliationStuck` and `BillingCustomPlanFailureSpike`. Eight
promtool cases prove each fires, clears, and stays silent on ordinary declines
and refusals. promtool: 53 rules load, every test passes.

## 11. Final test gates (from `d3103ac`)

| Gate | Result |
|---|---|
| ruff / black / mypy (app + tests, 644 files) | clean |
| `alembic heads` / `alembic check` | single head `0072` / no drift |
| Fresh migration chain | 72 migrations to `0072`; check clean; downgrade/upgrade clean |
| Whole suite, model-built schema | **5,725 passed, 88 skipped, 0 failed** (22 min) |
| Integration + e2e, migration-built schema | **2,723 passed, 73 skipped, 0 failed** (21 min) |
| New suites: custom plans, top-ups, platform billing, isolation, entitlements, concurrency, invariants | 76 tests, included above (and 76/76 as the mutation baseline) |
| Mutation suite | 21/21 killed |
| promtool | 53 rules; all tests pass |

The two whole suites ran **sequentially** from one exported tree of `d3103ac`,
each on its own database, and never concurrently in one checkout, so
`test_database_safety` cannot collide.

A pre-commit whole-suite run on the working tree found three real policy gaps,
all fixed before any commit:
* the platform route policy table did not list the 14 new routes;
* the platform access-audit coverage did not list the new reads;
* the workspace purge classification did not cover `plans`, `topup_products` and
  `topup_purchases`. All three are now **retained**, as financial and commercial
  records.

The first authoritative run, from `e10db55`, found one more gap: in the
migration-built run, `test_the_platform_company_summary_is_one_complete_read`
asserted plan version 1. That holds only on a model-built schema; on a
migration-built one `pro` is seeded, and the fixture publishes its terms as
version 2. The summary was correct. The test now compares against the pinned
version (`d3103ac`), all 76 new tests pass in both schema modes, and both whole
suites were run again from `d3103ac`. The same model-built run also had one
setup error in `test_whatsapp_reverification.py`: `OSError: [WinError 121] The
semaphore timeout period has expired` while connecting. That file passes 15/15
on its own at the same commit, and it passed in the `d3103ac` run above.

## 12. Decisions and deviations, stated

* **`refund_review` still counts toward the limit.** The spec's active-sum names
  only `GRANTED`. Counting a refunded-after-grant top-up until an operator
  decides is what spec 45 requires ("do not blindly subtract"); it is then kept
  (`granted`) or withdrawn (`cancelled`).
* **Idempotent checkout answers `409` naming the purchase.** It does not replay
  the page, because the page URL carries a client secret that is deliberately
  never stored (ADR-044). The same key still yields one purchase and one Paymob
  intention, proven ×4 concurrently.
* **Top-ups need an `active` subscription.** `past_due` is refused ("settle the
  open invoice first"). A key the plan leaves unlimited, and a zero-price product,
  cannot be bought (a free allowance is a platform grant).
* **Money arriving after the period ends is held, not granted:** `paid`, plus a
  `topup_paid_but_not_granted` incident for a refund. It cannot grant what the
  customer was shown.
* **Platform grants live in `topup_purchases`** with `source = platform_grant`,
  made semantically safe by the CHECK that forbids any invoice, payment or price
  on one.
* **`custom_plan_scope_mismatch` is raised where a transaction survives the
  refusal.** That is settlement holding money for such an invoice. A refused
  platform assignment rolls its own transaction back, so it is a `422`, a warning
  log and a `custom_plan_total{outcome="refused"}` sample rather than an incident.
* **`TopupStatus` records `expired` in the sweep,** but no limit depends on it:
  the arithmetic filters on the clock.
* **Capacity creation in the real E2E ran through the real entitlement guard**,
  not the HTTP connect route, which needs Meta. The route's guard is that same
  `reserve_or_refuse`.

## 13. Remaining product gaps

No carry-over, no top-ups that outlive the period, and no multi-pack purchases.
No top-ups for `agents` or `owned_workspaces`. `period_messages` is not enforced
(by ADR-030). No automated self-refund. No proration when a custom plan is
applied mid-period (unchanged from ADR-112). No real-money proof of a custom plan
renewal migration. Operator tooling is API-only. Paymob **Live** remains
unverified and is its own audit.

Out of scope, as required: no merge, no push, no Database or Deployment audit,
no Live.

---

# Part II — Paymob-bound custom plan offers and top-ups (ADR-114)

## 14. Summary

* **A paid custom plan cannot become a workspace's entitlement until its
  authoritative payment succeeds.** Creating or offering one grants nothing;
  `custom_plan_offers` (migration `0073`) carries the lifecycle `offered →
  pending_payment → active` / `declined` / `expired` / `cancelled`, and only
  `InvoiceSettlement`, on a signed Paymob callback or a transaction inquiry,
  makes an offer `active`.
* **The customer sees exactly what they buy** (price, currency, interval, all
  seven limits, the period) and pays exactly the offered version's price at a
  normal Paymob hosted checkout. The checkout invoice is pinned to that version
  and names the offer; the database refuses any other combination.
* **Renewal follows the optional saved card:** saved → the existing MOTO/MIT
  path at the custom version's price; not saved → a renewal invoice the owner
  pays at a hosted checkout, never an automatic charge.
* **Verified against real Paymob Test** (section 20): 8 real Paymob Test
  transactions, 7,940 EGP (Test), plus one real TOKEN callback. Initial custom plan with Save Card, the real TOKEN callback,
  a real MOTO/MIT renewal, a no-card renewal paid at checkout, callback replay,
  two lost callbacks recovered by Transaction Inquiry, and three real top-ups.
  All 21 ledger invariants are 0. No secret, client secret, payment key or card
  token appears in any log.
* **One provider-contract difference was found and fixed** (section 16):
  Transaction Inquiry now documents `auth_token` as a body field.

### Part II commits

| Commit | Message | Purpose |
|---|---|---|
| `60bed7c6cc3a61e1ca1bbadeffb2d071bdc8ce22` | fix(billing): send the documented auth_token in Paymob transaction inquiry | Paymob Transaction Inquiry contract fix (P-1) |
| `1ff0fb5db385ab842820e02b48c3c01ac6040dd6` | feat(billing): make priced custom plans offers the customer accepts and pays | Custom plan offer implementation, migration `0073`, APIs, registries |
| `e85f88326699c173f66c0fbd515b15ef3e7ecbaf` | test(billing): prove custom plan offers and every top-up key end to end | Offer suite and per-key top-up application E2E |
| `09ba2adefa061680b106d54980967eb5caecaeab` | docs(billing): record ADR-114 and the real Paymob Test custom plan E2E | ADR-114, billing docs, this Part II with the Paymob Test evidence |

## 15. Official Paymob documentation reviewed (2026-09-25)

Read from developers.paymob.com (each page's `.md` source, which carries the
same "Last Updated Date" as the rendered page). Where these pages and anything
older in this repository disagree, these pages win.

| Page | URL (developers.paymob.com/paymob-docs/…) | Last updated | Contract Wasla uses |
|---|---|---|---|
| Getting Integration Credentials | `need-help/setup-guides/getting-integration-credentials` | 2026-07-21 | Secret and public keys are **mode-specific** (`sk_test`/`pk_test` vs live); integration ids are listed per mode; the **API key is the same in Test and Live**; per-integration Webhook URL and Redirect URL. |
| Create Intention | `developers/intention-apis/create-intention` | 2026-06-01 | `POST /v1/intention/`, `Authorization: Token <secret key>`; `amount` in cents, `currency` matching the integration, `payment_methods` (ids whose mode must match the key), `items`, `billing_data`, `extras`, `special_reference` (→ `merchant_order_id`), `notification_url` (card only; receives the transaction **and** the card token), `redirection_url`. Response: `intention_order_id` (Order ID), `id` (Intention ID), `client_secret`, `payment_keys`. |
| Unified Checkout (redirection) | `developers/checkout-experiences/unified-checkout-redirection` | 2026-07-22 | `https://eg.checkout.paymob.com/?publicKey=…&clientSecret=…`. |
| Transaction Callbacks | `developers/webhook-callbacks-and-hmac/transaction-callbacks` | 2026-06-01 | Processed callback is a POST of the transaction; `order.id` correlates it with the order bound at intention time; `is_refunded`, `is_voided`, `refunded_amount_cents`. |
| HMAC (transaction) | `developers/webhook-callbacks-and-hmac/hmac/hmac-transaction-callback` | 2026-06-01 | 20 keys in the documented order (`obj.id`, `order.id` for POST), SHA-512, `hmac` in the query string. Identical to `HMAC_FIELDS`. |
| HMAC (card tokens) | `developers/webhook-callbacks-and-hmac/hmac/hmac-for-card-tokens` | 2026-08-24 | 8 keys `card_subtype, created_at, email, id, masked_pan, merchant_id, order_id, token`, SHA-512. Identical to `TOKEN_HMAC_FIELDS`. |
| Create Card Token | `developers/pay-with-saved-cards/create-card-token` | 2026-08-24 | Save the card in the Paymob UI; the `TOKEN` object arrives at `notification_url`; correlate on `intention_order_id`/`order_id`. The reusable credential is `obj.token` (not `obj.id`, the intention id, the client secret or a payment key). |
| MIT | `developers/pay-with-saved-cards/mit` | 2026-06-28 | Intention on a **Moto** integration → `payment_keys[0].key` → `POST api/acceptance/payments/pay {source: {identifier: <card token>, subtype: "TOKEN"}, payment_token}`. |
| CIT | `developers/pay-with-saved-cards/cit` | 2026-06-28 | Read; **not used** (top-ups are hosted checkouts; no one-click CIT decision exists). |
| Transaction Inquiry by order id / reference | `developers/transaction-inquiry-apis/transaction-inquiry/by-order-id-or-reference` | 2026-06-28 | `POST api/ecommerce/orders/transaction_inquiry {auth_token, order_id | merchant_order_id}`. See section 16. |
| Transaction Inquiry by transaction id | `developers/transaction-inquiry-apis/transaction-inquiry/by-transaction-id` | 2026-06-28 | Read; not used. |
| Card Token Inquiry | `developers/transaction-inquiry-apis/card-token-inquiry` | 2026-08-04 | `POST api/acceptance/order_card_tokens {auth_token, order_id}`. Unchanged. |
| Auth token | `developers/authentication-request-generate-auth-token-1` | 2026-06-01 | `POST api/auth/tokens {api_key}`. |
| Test Credentials | `need-help/setup-guides/test-credentials` | 2026-06-01 | Two Mastercard and one Visa test card (exp 01/39, CVV 123), a test wallet. Not copied into source. |
| Callback URL configuration | the "Change Callback for the Integration IDs" part of Getting Integration Credentials; `notification_url`/`redirection_url` on Create Intention | 2026-07-21 / 2026-06-01 | Wasla sends both per intention (from `APP_PUBLIC_URL` + the real route `/api/v1/webhooks/paymob`); no dashboard change was needed. |
| Integration Checklist | `getting-started/integration-checklist` | 2026-07-14 | Test → technical approval → Live credentials. This account is not onboarded for Live. |
| Webhook overview | `developers/webhook-callbacks-and-hmac/overview` | 2026-08-03 | General. |

## 16. Provider-contract differences found

| # | Documentation says | Wasla did | Resolution |
|---|---|---|---|
| P-1 | Transaction Inquiry takes `auth_token` as a **required body field** | Sent the token only as `Authorization: Bearer` (from Paymob's Postman collection, 2026-09-04) with `{merchant_order_id}` | Fixed in `PaymobProvider.inquire_charge`: the body now carries `auth_token` and `merchant_order_id`; the Bearer header is kept beside it so either reading works. The bearer is added to the redaction list, since it now travels in a body an error could quote. **Proven on real Test:** two lost callbacks recovered with this request (section 20, R-8/R-9). |
| P-2 | The API key is the same in Test and Live | (Not assumed either way) | Recorded. The API key alone therefore does not prove Test mode; Wasla's mode proof is the secret/public key class plus the integration ids, and callback binding checks `integration_id` and `is_live`. |
| P-3 | `notification_url` receives the transaction callback **and** the card token | Already relied on | Confirmed on real Test: the TOKEN callback arrived at the per-intention URL (R-2). |

Nothing else in sections 15's pages disagreed with the implementation.

## 17. Paymob Test dashboard (Chrome, existing session, 2026-09-25)

* **Test mode confirmed visually:** the mode toggle read *Test mode* and the
  banner *"You are in test mode. No real money will be charged."* The account
  (MID 1221143) shows onboarding incomplete, so Live is not available at all.
* **Credentials present** (Settings → API Keys): API key, Public key (**Test**),
  Secret key (**Test**), HMAC. No value was viewed or copied.
* **Integrations in Test mode:** 5885262 (VPC, EGP, `online` — the normal card
  integration), 5934829 (VPC, EGP, `moto`), 5897483 (UIG, EGP, `online_new`).
* **Callback configuration:** 5885262 and 5934829 both had Webhook URL
  `https://<ngrok static domain>/api/v1/webhooks/paymob` and Redirect URL
  `…/billing/checkout/return`, set before this session. **Nothing on the
  dashboard was changed**; Wasla also sends `notification_url` and
  `redirection_url` per intention.
* **Save Card:** offered on the hosted card page for integration 5885262 — the
  customer saved a card in R-1 and Paymob sent the TOKEN callback.
* **Test vs Live proof before every run:** the E2E env builder refuses to start
  unless the secret key contains `sk_test_` and the public key `pk_test_`; the
  driver asserts it again; every hosted page opened was checked to start with
  `https://eg.checkout.paymob.com/?publicKey=egy_pk_test_` before it was
  opened; the integration ids are the Test ids above; and every callback and
  payment row reads `is_live: false` / `provider_mode: test`.

## 18. Design (ADR-114)

**Schema (`0073`).** `custom_plan_offers`: tenant (RESTRICT), plan and version
(RESTRICT), `status` (native enum), `expires_at`, `reason`, actor columns for
creation, acceptance, decline and cancellation, the matching timestamps,
revision. `UNIQUE (id, tenant_id)`; a partial unique index for one open offer
per workspace; CHECKs that each terminal state has its moment. Trigger
`custom_plan_offers_integrity`: an offer's tenant, plan and version never change,
and the plan must be the offering workspace's own `tenant` plan at that version.
`invoices.custom_plan_offer_id` with a composite foreign key onto
`custom_plan_offers (id, tenant_id)`, a CHECK that only a `checkout` invoice with
a pinned version may name an offer, and trigger `invoices_custom_plan_offer`
requiring the invoice to sell the offered version and never be re-pointed.
Six `audit_action` labels. Fresh chain, round trip and `alembic check` clean.

**Flow.**

    platform: POST /platform/billing/tenants/{id}/custom-plan
              (financial_basis: customer_checkout)          -> plan + v1 + offer (offered)
    owner:    GET  /billing/custom-offers                     -> full terms
              POST /billing/custom-offers/{id}/accept          -> CHECKOUT invoice (v1, offer)
                                                                + Paymob Create Intention
                                                                (card integration, amount from v1)
                                                              -> offer pending_payment; plan unchanged
    Paymob:   hosted checkout (optional Save Card)
              TRANSACTION callback -> HMAC -> order/integration/is_live/amount/currency
              binding -> InvoiceSettlement -> CustomPlanOfferLedger.refusal? -> grant v1
              -> offer active (once, audited)
              TOKEN callback -> HMAC -> correlated on the order -> encrypted saved card
    renewal:  saved card -> RENEWAL invoice at v1 -> MOTO intention -> pay -> callback
              no card    -> RENEWAL invoice at v1 -> summary.payment_required
                            -> POST /billing/checkout {invoice_id} -> hosted checkout

**Refusals.** Buying a custom plan by code (422, pointing to the offer);
accepting a declined/cancelled/expired/active offer (409); another workspace's
offer (404); a request carrying a price (422, `extra = forbid`); offering a free,
retired or foreign version (422); a second open offer (409); scheduling a priced
custom plan the workspace does not hold onto its next renewal (422). Money for an
offer declined or withdrawn after its page opened is **held** with a
`refused_settlement` incident; a page opened before expiry is honoured.

**Zero price.** Not offered; assigned through the existing path, audited with
actor, platform role, reason, workspace, and `plan_version_id`.

**Files.** New: `app/db/models/custom_plan_offer.py`,
`app/repositories/custom_plan_offer_repository.py`,
`app/services/custom_plan_offer_service.py`,
`app/services/custom_plan_offer_ledger.py`, `app/platform/custom_plan_offers.py`,
`alembic/versions/20260925_0073_custom_plan_offers.py`. Changed: the invoice
model, checkout (`start_offer`, custom plans refused by code), settlement (the
offer hook), `custom_plan_admin` (basis → offer), `billing_operations` (the
renewal guard), the billing worker (offer expiry phase), the tenant and platform
routers, schemas, telemetry label domain, and `paymob.py` (P-1).

## 19. Tests added or changed

* `tests/integration/test_custom_plan_offers.py` (16): activation exactly once
  and only when paid, with replay; a declined transaction then a success; the
  1,500/1,800 snapshot; declined and withdrawn offers holding money; expiry
  honouring a page opened in time; wrong amount, currency, order, integration
  and `is_live` refused; custom plan MIT renewal at its own price, once, while
  an open top-up invoice is **not** collected; no-card renewal never charged
  and paid at checkout; lost callback recovered through the sweep with
  `auth_token` in the inquiry body; schema guards; priced custom plans refused
  for scheduling and free ones for offering.
* `tests/integration/test_commercial_api.py` (+4, and the authority lists):
  the customer UI contract and ownership, cross-tenant 404s for offers, invoices
  and private top-ups, platform offer rules and history, zero-price audit.
* `tests/integration/test_topups.py` (+7): the application E2E for each of the
  seven keys (base → base + quantity once, plan/version/price unchanged).
* Updated: `test_custom_plan_lifecycle.py` buys through an offer; the
  platform route policy, access-audit coverage, README migration range and
  API operation count (192).

## 20. Real Paymob Test E2E (2026-09-25)

**Setup.** API from this branch on `127.0.0.1:57455`, database `wasla_e2e2`
migrated from empty to `0073`, isolated Redis database 7. The existing ngrok
agent (static domain → `127.0.0.1:57455` only; no database, Redis or metrics
exposed) was reused unchanged. Before paying, an unsigned POST through
`https://<ngrok>/api/v1/webhooks/paymob` reached Wasla and was refused 403,
proving the path. Four synthetic workspaces on the seeded Pro plan (applied as
a settled purchase applies it; the plan money path was proven by the billing
remediation): A *ABC Company*, B *XYZ Company*, C *Topup Company*, D *Recovery
Company*. Custom offers and checkouts were driven over the real HTTP API with
real access tokens; renewals and recovery by the real billing worker with the
real Paymob provider. The user entered the official Test card on Paymob's hosted
pages (Wasla never sees a PAN or CVV). Time was moved only for renewals, and only
in this throwaway database (the subscription's period shifted back so its end
was one minute ago).

**Evidence ledger** (safe identifiers only; all EGP, all `provider_mode: test`,
all callbacks `is_live: false`):

| # | UTC | Flow | Wasla invoice | Wasla payment | Paymob order | Paymob txn | Integration | Amount | Callback | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| R-1 | 15:58:00 | A custom offer, Save Card | `4f4fa830…` | `75083548…` | 617876658 | 541696639 | 5885262 | 1,500.00 | 200, `applied` | offer **active**, v1 pinned, 7 limits |
| R-2 | 15:57:42 | A TOKEN callback | — | — | 617876658 | — | — | — | 200 | card saved encrypted (`v1.`), `…2346`, MasterCard, default |
| R-3 | 15:58:36 | B custom offer, no save | `415baab1…` | `1ef01166…` | 617876703 | 541697119 | 5885262 | 1,500.00 | 200, `applied` | offer **active**; no card |
| R-4 | 15:59:12 | C AI Turns +10,000 | `5bb33e88…` | `b7a6e687…` | 617876753 | 541697534 | 5885262 | 200.00 | 200, `applied` | 5,000 → **15,000** |
| R-5 | 15:59:47 | C WhatsApp Numbers +2 | `d096e53e…` | `65510275…` | 617876805 | 541697985 | 5885262 | 150.00 | 200, `applied` | 3 → **5** |
| R-6 | 16:01:39 | A renewal, **MOTO/MIT** | `709339c5…` | `f4e7bb08…` (automatic) | 617881135 | 541699548 | **5934829** | 1,500.00 | 200, `applied` | renewal paid; same v1 |
| R-7 | 16:05:24 | B renewal at hosted checkout | `b38b1344…` | `4cd3033a…` | 617882282 | 541701924 | 5885262 | 1,500.00 | 200, `applied` | renewal paid; same v1 |
| R-8 | 16:08:49 | D custom offer, **callback lost** | `ccae9c0a…` | `efd5a92c…` | 617882326 | 541703186 | 5885262 | 1,500.00 | **502** (API down) → inquiry | offer **active** via reconciliation |
| R-9 | 16:08:51 | C Team Members +5, **callback lost** | `8c49be83…` | `9f0fe2ad…` | 617882375 | 541703582 | 5885262 | 90.00 | **502** (API down) → inquiry | 10 → **15** via reconciliation |

Totals: 8 real Paymob Test transactions, 7,940 EGP (Test)
(1,500 + 1,500 + 200 + 150 + 1,500 + 1,500 + 1,500 + 90), plus one real TOKEN
callback. No Live key, integration or transaction was used.

**Scenario results.**

| Scenario | Result |
|---|---|
| Offer shown to the customer | **PASS.** `ABC Enterprise 1500.00 EGP monthly`, the seven limits (100,000 / 40,000 / 50,000 / 107,374,182,400 bytes / 5 / 30 / 3,000), `starts: on_payment`, `can_accept: true`. Creating and offering changed no plan. |
| Accept & Pay | **PASS.** 201; intention on card integration 5885262, amount 150,000 cents from the version; offer `pending_payment`; subscription still Pro until the callback. |
| Activation after callback | **PASS.** Invoice `paid`, payment `succeeded`, offer `active`, subscription pinned to the purchased version, period starting at settlement, effective limits exactly the seven. |
| Save Card / TOKEN | **PASS.** Real TOKEN callback, HMAC verified, correlated on the Paymob order, stored encrypted; B (not saved) has none. |
| MOTO/MIT renewal | **PASS.** The real sweep issued the renewal at the custom version's price, sent a MOTO intention on 5934829 with `payment_keys[0].key` and the saved card token, and the callback settled it. A second sweep: 0 handled, 0 charges, the period advanced exactly once. |
| No-saved-card renewal | **PASS.** The same sweep sent **0** MOTO intentions and **0** pay requests for B; the renewal invoice was `open`; `GET /billing/summary` showed `automatic_renewal: false` and it under `payment_required`; the owner paid it at a hosted checkout and it settled on the same v1. |
| Callback replay | **PASS.** The genuine signed callbacks for R-1 and R-4 replayed through the ngrok agent: `duplicate`, no new event row, A's period unchanged, C's AI turns still **15,000** (not 25,000). |
| Lost-callback recovery | **PASS.** API stopped; Paymob's callbacks for R-8/R-9 got 502. Wasla showed both `pending`. After restart, the worker's reconcile phase asked Transaction Inquiry (with `auth_token` in the body, P-1) and settled both through the normal path, once; two `recovered_by_reconciliation` incidents, resolved. No second payment. Paymob sent no retry afterwards. |
| Capacity after top-up | **PASS.** 5 numbers created (3 + 2), the 6th refused. At the period end (clock only): all 5 kept, `over_limit: true`, `remaining: 0`, a new one refused; nothing disconnected. |
| Top-ups leave the plan alone | **PASS.** C stayed on Pro v1 at 99.00 through three top-ups. |
| Invariants | **PASS.** T01-T10, C01-C05 and O01-O06 (offers: active ⇒ paid invoice at its version; no paid invoice for a declined/cancelled offer; invoice = offered version and price; no priced custom plan held without payment; one open offer; no MIT on a TOPUP invoice) all **0**. |
| Secrets in logs | **PASS.** 0 occurrences of the secret key, HMAC secret, API key, JWT secret, encryption key, any client secret or payment key in ~36 KB of API and worker logs; the real card token, decrypted in memory only, **0**. |

## 21. Final E2E matrix

| Item | Result | Evidence |
|---|---|---|
| CUSTOM PLAN OFFER | **VERIFIED** | real HTTP (R-1, R-3, R-8) + `test_commercial_api` |
| CUSTOM PLAN INITIAL PAYMOB PAYMENT | **VERIFIED** | REAL R-1, R-3 |
| CUSTOM PLAN ACTIVATION AFTER CALLBACK | **VERIFIED** | REAL R-1, R-3 |
| CUSTOM PLAN SAVE CARD TOKEN | **VERIFIED** | REAL R-2 |
| CUSTOM PLAN MOTO/MIT RENEWAL | **VERIFIED** | REAL R-6 |
| CUSTOM PLAN NO-SAVED-CARD RENEWAL | **VERIFIED** | REAL R-7 (0 MOTO, 0 charges before it) |
| CUSTOM PLAN CALLBACK REPLAY | **VERIFIED** | REAL replay of R-1 |
| CUSTOM PLAN LOST-CALLBACK RECOVERY | **VERIFIED** | REAL R-8 |
| TOPUP period_messages | **APP-E2E** | `test_each_topup_raises_its_own_limit_once…[period_messages]` |
| TOPUP period_ai_turns | **REAL** | R-4 (and Part I txn 541508368) |
| TOPUP period_campaign_messages | **APP-E2E** | `…[period_campaign_messages]` |
| TOPUP storage_bytes | **APP-E2E** | `…[storage_bytes]` |
| TOPUP whatsapp_numbers | **REAL** | R-5 (and Part I txn 541508739) |
| TOPUP team_members | **REAL** | R-9 (settled by recovery) |
| TOPUP knowledge_documents | **APP-E2E** | `…[knowledge_documents]` |
| TOPUP CALLBACK REPLAY | **VERIFIED** | REAL replay of R-4 |
| TOPUP LOST-CALLBACK RECOVERY | **VERIFIED** | REAL R-9 |
| TOPUP NEVER MIT-COLLECTED | **VERIFIED** | App E2E: a saved card and an open top-up invoice through a real sweep → only the renewal charged (`test_a_custom_plan_renews_from_a_saved_card…`); the `payments_no_automatic_topup` trigger; O06 = 0 on the real E2E database after the real MIT sweep. No real saved-card workspace held an open top-up invoice during a real sweep. |

APP-E2E means the full application path - real services, real Paymob adapter
signing and verifying the callback, real settlement - with Paymob faked at the
socket. It is not claimed as a real Paymob payment.

## 22. Gates (Part II)

| Gate | Result |
|---|---|
| ruff / black / mypy (`app`, `alembic`, `tests`; mypy 300 source files) | clean |
| `alembic heads` / `alembic check` | single head `0073` / no drift |
| Fresh migration chain | `0001` → `0073` on an empty database; `0073` → `0072` → `0073` round trip; check clean |
| Whole suite, model-built schema | **5,852 collected, 5,716 passed, 136 skipped, 0 failed** (exit 0) |
| Integration + e2e, migration-built schema (separate exported tree and database) | **2,823 collected, 2,713 passed, 110 skipped, 0 failed** (exit 0) |
| New offer suite / HTTP offer tests / per-key top-up app E2E | 16 / 4 (+ authority lists) / 7, included above |

Two real gaps were caught by the first whole-suite pass and fixed before any
commit: `custom_plan_offers` had to be classified for workspace purge (it is
**retained**, as a commercial record), and the invariant ledger fixture still
*scheduled* a priced custom plan, which ADR-114 now refuses - it now offers it,
so the populated ledger holds an open offer and the sweep checks O01-O05 too. A
second pass had been invalidated by my own concurrent test run on the same
database; the figures above are from clean, non-overlapping runs.

## 23. Decisions, deviations and remaining gaps

* **No `DRAFT` state.** A custom plan nobody has offered is a plan with no
  offer; the smallest model that keeps the invariant.
* **Accepting again re-opens a page** for the same terms while
  `pending_payment`; whichever page is paid first activates the offer, and a
  second payment for the same period is refused as a duplicate with an
  incident (existing rule).
* **A custom offer cheaper than a pricier paid period in progress** is refused
  at acceptance like any downgrade (ADR-112). An operator can offer it when the
  period ends, or assign it with a stated basis.
* **Priced custom plans can no longer be scheduled onto a renewal** without the
  customer's acceptance; migrating between versions of the plan a subscriber
  already holds is unchanged (ADR-112).
* **The E2E base plan (Pro) was applied as a settled purchase**, not paid
  again; everything the spec asked to be paid was paid.
* **Time was moved for renewals** in the throwaway E2E database only (subscription
  period shifted, never the provider).
* **Four top-up keys are APP-E2E, not REAL.** They share the one generic path the
  three REAL keys used; only key, quantity and price differ.
* Remaining: no Live verification (its own audit); no customer frontend in this
  repository (the APIs carry everything the Billing UI needs); operator tooling is
  API-only.
