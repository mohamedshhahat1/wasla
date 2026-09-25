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
| Final code/test HEAD | `d3103ac` (every gate in section 11 ran from this commit) |
| Alembic | `0071` → **`0072`**, single head, `alembic check` clean |
| Merge / push | **Not merged, not pushed.** Paymob **Live** never used. |

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
(186 operations, migrations `0001`–`0072`). Its tree was verified in isolation
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
`tenant_id`. The production shape now serves **186 operations** (was 168).

Custom plan creation requires all seven limits (`null` = unlimited, `0` = none;
omission is refused) and inherits `agents` and `owned_workspaces` from the current
plan. A priced plan applied `now` needs `financial_basis`: `customer_checkout`
(nothing assigned; the owner buys it at checkout), `manual_payment` or
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
