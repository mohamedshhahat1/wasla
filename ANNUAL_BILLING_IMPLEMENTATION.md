# Wasla Monthly + Annual Billing Implementation

ADR-116. Implementation branch `annual-billing`, worktree `E:\wasla-annual-billing`.
Nothing was merged, pushed or deployed; no production database was touched;
Paymob Live was never used.

## 1. Executive summary

A plan version is now one set of entitlements sold at one or more **immutable
prices** (`plan_prices`): Business v4 is one version, and `299 EGP / month` and
`2,990 EGP / year` are two prices of it. Every subscription, scheduled change,
invoice and custom-plan offer pins an exact price; the database enforces that
the price belongs to the version beside it, that a priced invoice charges exactly
its price, and that a live priced subscription names one.

The subscription now carries **two clocks**: the billing term
(`current_period_*`, one price's term - a month or twelve calendar months) and
the **usage cycle** (`usage_period_*`, always one calendar month inside it).
Usage limits and usage top-ups follow the cycle; capacity top-ups and
cancellation follow the term. A yearly customer pays once, and their allowances
reset every month without an invoice, a charge or any change to the term.

Monthly behaviour is unchanged: migration 0081 gives every priced version one
price - its own published terms, nothing invented - pins everything to it, and
sets every usage cycle equal to its billing period.

Verified by: 3,074+ unit tests, the whole billing family, a 43-test annual
integration suite, real-PostgreSQL race tests, an independent SQL entitlement
oracle (1,440 comparisons, 0 mismatches), 1,200,040 independent calendar
property checks (0 violations), a 19-rule invariant ledger, a 20-mutant campaign
(20 killed), migration round trips, and real Paymob Test payments for all seven
annual scenarios plus both top-up kinds.

## 2. Starting repository state

| Item | Value |
| --- | --- |
| CURRENT_CANONICAL_HEAD | `11cf44b` (`worktree-billing-google-auth`, the billing integration line; `main` is `8e3a1ba`, an ancestor) |
| CURRENT_REMEDIATION_HEAD | `4654a79` (`database-findings-remediation`: database remediation and PAY-E2E-01/02/03 fixes, on top of `11cf44b`, unmerged) |
| Base branch / HEAD | `database-findings-remediation` @ `4654a79` |
| CURRENT_ALEMBIC_HEAD (before) | `0080` (single head) |
| New Alembic head | `0081` (single head) |
| Paymob findings | PAY-E2E-01, PAY-E2E-02, PAY-E2E-03 all **CLOSED** on the base (PAYMOB_E2E_FINDINGS_REMEDIATION.md); their fixes are untouched |
| Untracked files in `E:\wasla` | nine audit/plan documents, left untouched (a separate worktree was used) |

The implementation was built on the remediation head so that no Paymob or
database fix is regressed; it therefore inherits that branch's unmerged state.

## 3. Existing billing architecture

`plans` (identity) -> immutable `plan_versions` (price, currency, interval,
limits; a trigger refuses UPDATE) -> `subscriptions` pinned to a version, with
one period `current_period_*` anchored on `billing_anchor_at` (BILL-18).
`CheckoutService` opens one immutable `CHECKOUT` invoice per page;
`InvoiceSettlement` is the only settlement authority (ADR-112/115), taking the
payment -> invoice -> subscription -> offer/top-up lock order; `BillingWorker`
rolls periods over and bills the next period in advance, collects renewals from
a saved card (MOTO/MIT, ADR-088) or leaves them to a hosted checkout, dunning
and reconciliation. `EntitlementService` = pinned version + live top-ups +
grants. Top-ups expired at `current_period_end`. Custom plans are tenant-scoped
plans; priced ones reach a workspace through an offer (ADR-113/114).

What was missing: the version *was* the price, so one version could be sold on
one interval only, and the one period was both what a payment covered and what
usage was counted over.

## 4. Architecture decisions

ADR-116 (DECISIONS.md), summarised:

1. **Plan -> PlanVersion -> PlanPrice.** Entitlements on the version; amount,
   currency and term on the price. `plan_versions.price` keeps two meanings
   only: `0` marks a free version (no prices, never checked out); otherwise the
   terms it was published with, which the database makes its first price.
2. **Everything that sells names a price**, by composite keys onto
   `(plan_version_id, plan_price_id)`.
3. **Billing term and usage cycle are separate**, both anchored on
   `billing_anchor_at`; the yearly term's twelfth cycle ends exactly with it.
4. **Change timing** (`app/services/commercial_policy.py`): higher tier or
   longer term = purchase now (full price, new term from settlement, no credit,
   per ADR-112); lower tier, shorter term or free = at the end of the paid term,
   pinned to the exact price. Tiers across terms compare the target's price on
   the current term, else annualised by calendar months.
5. **Retired prices are honoured where already agreed** (scheduled changes,
   offers, open checkouts); only *new selection* is refused.
6. **No price is invented**: annual prices exist only because the platform API
   published them.
7. **`BillingInterval` keeps its stored spelling** (`monthly`/`yearly`); the
   platform API also accepts `month`/`year`. `interval_count` exists; only `1`
   is sold (422 otherwise).

## 5. PlanPrice model

`plan_prices` (`app/db/models/billing.py`):

| Column | Rule |
| --- | --- |
| `id` | UUID |
| `plan_version_id` | FK `plan_versions` ON DELETE CASCADE (only a never-sold plan can go; every reference to a price is RESTRICT) |
| `billing_interval` | enum `monthly`/`yearly` |
| `interval_count` | `> 0` (CHECK); only 1 accepted by the API |
| `amount` | `NUMERIC(12,2)`, `> 0` (CHECK); never float |
| `currency` | `= 'EGP'` (CHECK) |
| `created_at`, `created_by`, `reason` | provenance |
| `retired_at`, `retired_by`, `retirement_reason` | retirement; `retired_at >= created_at` |

- `UNIQUE (plan_version_id, id)` - the target of every composite key.
- Partial unique index `uq_plan_prices_active_slot (plan_version_id,
  billing_interval, interval_count, currency) WHERE retired_at IS NULL` - one
  active price per commercial slot; retired rows may repeat a slot.
- Trigger `plan_prices_guard`: refuses a price on a free version; refuses any
  change to version, interval, count, amount, currency, created_at or reason;
  refuses un-retiring or re-retiring.
- Trigger `plan_versions_publish_price` (AFTER INSERT on `plan_versions`, WHEN
  `price > 0`): a priced version is published with its own terms as its first
  price, whoever writes it.

## 6. Billing vs usage periods

| | Billing term | Usage cycle |
| --- | --- | --- |
| Columns | `current_period_start/end` (unchanged names) | `usage_period_start/end` (new) |
| Length | one price term: 1 or 12 calendar months | always 1 calendar month |
| Anchor | `billing_anchor_at` | `billing_anchor_at` |
| Drives | renewal date, invoice period, cancellation, capacity top-up expiry, paid-through | `period_*` limits, usage top-up expiry, usage display |
| Moved by | settlement (`apply_purchase`), roll-over | `open_term`, the usage sweep |

`app/services/billing_calendar.py`: `months_after`, `anniversary`,
`next_boundary`, `usage_cycle`, `usage_period`, `first_usage_period`,
`current_usage_period`. No 30/365-day arithmetic anywhere; every length is
`MONTHS_PER_INTERVAL x interval_count`.

`current_usage_period(subscription, now)` is the cycle actually in force:
the stored one while `now` is inside it; after it ends but inside the paid term,
the anchored month containing `now`, computed exactly as the sweep will store
it (so enforcement never lags a sweep); past the term, the stored one (the
roll-over decides what opens next).

CHECK `ck_subscriptions_usage_period_within_term`: while live, the cycle is
non-empty and inside the term.

## 7. Migration/backfill

`alembic/versions/20260927_0081_plan_prices_and_usage_periods.py` (one
migration, `0080 -> 0081`):

1. **Preflight** (refuses, changing nothing): priced purchases/renewals whose
   amount or currency differs from their version's; invoices charging money
   for a free version; offers of a free version.
2. Create `plan_prices` + guard trigger.
3. **Backfill**: one price per priced version, from its own
   `price/currency/interval`, `created_at` of the version; then install the
   publish trigger.
4. Add columns; pin subscriptions, scheduled changes, priced invoices (with
   interval snapshot) and offers to that one price; `usage_period = current
   period` for every subscription; `NOT NULL`s set.
5. Constraints `NOT VALID` then `VALIDATE`; new/updated triggers
   (`subscriptions_price_pinned` deferred, `invoices_price_snapshot`, offer
   integrity and invoice-offer triggers now include the price); indexes built
   `CONCURRENTLY` (INVALID leftovers dropped first); two audit-action labels.
6. **Downgrade** refuses while any row names a price other than its version's
   published terms, or any live usage cycle differs from its term.

Seeded catalogue on a fresh database after 0081: Pro 99 monthly, Business 299
monthly - **no yearly price invented**; Starter and Enterprise (free) have no
price rows.

## 8. Monthly compatibility

- Every existing subscription keeps its version and gets its version's one
  price; its usage cycle is its billing period - so counting is unchanged.
- `POST /billing/checkout {"plan_code"}` still buys the plan's monthly price.
- `PlanRead.price/currency/interval` remain the monthly price; `prices[]` and
  `billing_required` are additive.
- `POST /plans` and `/versions` still accept `price` + `interval`.
- Offers may still be created with `plan_version_id` when the version has one
  active price (ambiguity is refused).
- A usage cycle that equals its term moves with the term when code sets the
  term directly (a model validator), so any existing writer stays consistent.
- The unchanged monthly suites (renewals, dunning, refunds, reconciliation,
  top-ups, offers, settlement concurrency) pass after the fixture changes
  listed in section 28.

## 9. Annual subscriptions

`apply_purchase(version, price)` opens `[settlement, settlement + price
term)` and the first monthly cycle, re-anchoring at settlement. A yearly
checkout is one `CHECKOUT` invoice for the full year (provisional 12-month
period, fixed at settlement), one hosted payment for that amount.

## 10. Annual renewals

The worker's `_next_terms` returns `(version, price)` pairs; `roll_over` opens
the next term at the length of the price being **billed** (`term_price`), so a
yearly renewal invoice always covers a year - including a migration billed
before its version is adopted. The renewal is one `RENEWAL` invoice for the
full annual amount, with interval snapshot. MIT eligibility
(`invoice_repository._automatically_collectible`) now compares the invoice to
**its price** (`PlanPrice.amount`) - it compared to `PlanVersion.price`, which
would have excluded every yearly renewal from MOTO collection. With a default
card: one MOTO charge on the MOTO integration; without: no attempt, a hosted
renewal.

## 11. Usage-cycle rollover

`BillingWorker._advance_usage` (phase after the billing roll):
`claim_usage_due` / `claim_usage_by_id` select serving subscriptions whose
cycle ended **inside a term that has not** (`usage_period_end <= now <
current_period_end`), `FOR UPDATE SKIP LOCKED` on the same subscription row the
renewal claims. The new cycle is computed directly by `current_usage_period`,
so any number of missed months is caught up in one write. No invoice, no
payment, no term change; counters need no reset (they are sums over
`usage_events` in the window).

## 12. Top-up semantics

`topup_ledger.validity_window(subscription, entitlement, now)`:

- usage keys (`period_messages`, `period_ai_turns`,
  `period_campaign_messages`): the current **usage cycle** - an annual customer's
  AI top-up bought on 15 October expires at the 1 November cycle boundary;
- capacity keys (`storage_bytes`, `whatsapp_numbers`, `team_members`,
  `knowledge_documents`): the current **billing term**, as before - a year on a
  yearly price.

On a monthly price both windows are the same one. Expiry deletes nothing
(`over_limit`, creation refused), as before.

## 13. Standard plan pricing

`POST /platform/billing/plan-versions/{id}/prices` adds a yearly (or monthly)
price to the current version of Pro/Business without copying it. Changing a
price = retire + create; subscribers stay pinned. Migrations keep each
subscriber on their term and refuse a target not sold on a term in use.

## 14. Custom plan pricing

`CustomPlanCreate.prices` (monthly, yearly or both) + `selected_billing_interval`
(which one the offer/assignment uses). Custom prices belong to the tenant's own
plan; the existing TENANT triggers plus the composite keys make another
workspace's price unusable by any writer.

## 15. Platform API

All under `/api/v1/platform/billing`, `PlatformStaffDep` (platform owner or
admin) - the same authority as publishing a version - audited.

| Method | Path | Purpose | Request | Response | Invariants |
| --- | --- | --- | --- | --- | --- |
| GET | `/plans` | list plans | filters | versions with `prices[]` (incl. retired) and subscriber counts | read audited |
| GET | `/plans/{id}` | read plan | - | same | |
| GET | `/plans/{id}/versions` | versions + full price history | - | `PlanVersionRead[]` with `prices`, `billing_required` | |
| POST | `/plans` | create plan + v1 with prices | `prices[]` or `price`+`interval` | plan | headline = monthly; one per slot |
| POST | `/plans/{id}/versions` | new version with prices | `prices[]` or legacy | version | same |
| GET | `/plan-versions/{version_id}` | read one version | - | limits + all prices | |
| GET | `/plan-versions/{version_id}/prices` | price history | `?active=` | `PlanPriceRead[]` with reference counts | |
| POST | `/plan-versions/{version_id}/prices` | create monthly **or** yearly price (one path) | `billing_interval`, `interval_count`=1, `amount`, `currency`, `reason` | price (201) | 422 free/retired plan/superseded/term/currency/amount; 409 duplicate slot; audit `billing_plan_price_created` |
| GET | `/prices/{price_id}` | one price | - | price + references | |
| POST | `/prices/{price_id}/retire` | retire | `reason` | price | never deletes; subscribers keep it; audit `billing_plan_price_retired` |
| PATCH | `/prices/{price_id}` | (refused) | any price field | **409** "retire and create" | DB trigger refuses too |
| GET | `/subscriptions` | list | `billing_interval`, `plan_price_id` (+ existing filters) | term **and** usage cycle, `plan_price_id`, `billing_interval`, `amount`, `scheduled_plan_price_id` | |
| GET | `/subscriptions/{id}` | inspect price, interval, scheduled price | - | same | |
| POST | `/subscriptions/{id}/change-plan` | now/next renewal at a price | `plan_version_id`, `plan_price_id?` | subscription | price must belong to version |
| GET | `/tenants/{id}/summary` | company billing | - | `plan_price_id`, `billing_interval`, term, `usage_period_*`, scheduled price | |
| POST | `/tenants/{id}/custom-plan` | custom plan with prices | `prices[]`, `selected_billing_interval` | plan, version with prices, offer | tenant-owned prices |
| POST | `/tenants/{id}/custom-offers` | offer a monthly or yearly price | `plan_price_id` | offer with `billing_interval`, `price` | pins price; own tenant's plan only |

## 16. Customer API

| Method | Path | Change |
| --- | --- | --- |
| GET | `/billing/plans` | `prices[]` (id, interval, count, amount, currency), `billing_required` |
| POST | `/billing/checkout` | `plan_price_id` (or legacy `plan_code` = monthly, or `invoice_id`); extra fields 422 |
| POST | `/billing/subscription/plan` | `plan_price_id` or `plan_code`; longer/higher = 402, shorter/lower = scheduled with pinned price |
| GET | `/billing/subscription`, `/billing/summary` | `plan_price`, `billing_interval`, `billing_period_*`, `paid_through`, `usage_period_*`, `next_renewal_at`, `next_renewal_amount`, scheduled price |
| GET | `/billing/invoices*` | `plan_price_id`, `billing_interval`, `interval_count` |
| GET | `/billing/entitlements` | `period_start/end` = the usage cycle in force |
| GET | `/billing/custom-offers` | `plan_price_id`, `billing_interval`, exact price |
| POST | `/billing/custom-offers/{id}/accept` | unchanged body; response adds `plan_price_id`, `billing_interval` |

## 17. Checkout and Paymob

No Paymob Subscriptions module. Checkout derives plan, version, price, amount,
currency and term from the selected price (`PlanCatalog.selectable_price`
refuses retired prices, superseded versions, retired plans and other tenants'
custom prices; a custom plan is bought only via its offer). The invoice's price
trigger makes a client-supplied figure unwritable. The Paymob intention amount
is the price's amount; the provider call remains outside the database
transaction (DB-008), unchanged. A page opened before a price change settles at
its own price.

## 18. Upgrade/downgrade/cancel semantics

| From -> to | Timing |
| --- | --- |
| monthly Pro -> yearly Pro | purchase now, new annual term at settlement, no credit |
| monthly Pro -> yearly Business | purchase now |
| yearly Pro -> yearly Business | purchase now |
| yearly Business -> yearly Pro | at annual term end, pinned price |
| yearly Business -> monthly Business | at annual term end |
| yearly Business -> monthly Pro | at annual term end |
| monthly Business -> yearly Pro | at term end (lower tier) |
| any -> free | at term end |
| cancel | `cancel_at_period_end` = end of the paid **year**; cycles roll monthly until then |

Settlement's duplicate guard lets a same-plan **longer-term** purchase through
(it used to refuse it as "already paid this period").

**Scheduled change whose price is later retired** (spec 98): honoured - the
scheduled price is applied at the boundary.

## 19. Database constraints

`plan_prices`: see section 5. `subscriptions`: `fk_subscriptions_plan_price_of_version`,
`fk_subscriptions_scheduled_price_of_version`, `ck_subscriptions_price_pinned`,
`ck_subscriptions_scheduled_price_pinned`, `ck_subscriptions_usage_period_within_term`,
deferred trigger `subscriptions_price_pinned`. `invoices`:
`fk_invoices_plan_price_of_version`, `ck_invoices_price_pinned`,
`ck_invoices_price_snapshot_complete`, trigger `invoices_price_snapshot`.
`custom_plan_offers`: `fk_custom_plan_offers_plan_price_of_version`, price in
the integrity trigger; `invoices_custom_plan_offer` requires the offered price.
Every reference to a price is RESTRICT; indexes on every referencing column.

## 20. Tenant isolation

A price's tenant is its plan's; composite keys bind it to its version; the
existing TENANT triggers refuse another workspace's version on subscriptions and
invoices; the offer trigger requires the workspace's own plan. Service layer:
`selectable_price` treats another tenant's custom price exactly like an unknown
id. Proved by `test_another_workspaces_custom_price_is_refused_everywhere`
(checkout, plan request, offer, and a direct SQL UPDATE refused by the trigger)
and ledger rule "a workspace holds another workspace's custom price".

## 21. Concurrency

`tests/integration/test_annual_billing_concurrency.py`, real commits, 10
workers:

| Test | Result |
| --- | --- |
| 10 annual renewal workers | 1 renewal invoice, 1 MOTO request (full annual amount), 10 concurrent callback deliveries -> 1 applied, 1 twelve-month advance |
| 10 usage workers, months behind | 1 cycle move to the correct month, 0 provider requests, 0 invoices, term unchanged |
| renewal racing usage roll at the boundary (stale cycle) | 1 annual invoice, next year's term, its first month; 1 MOTO |

## 22. Invariant ledger

`tests/integration/annual_billing_oracle.py`, 19 SQL rules, e.g. price belongs
to version (subscription, scheduled, invoice, offer); invoice amount/currency/
term = its price; priced purchase names a price; paid annual invoice covers 12
calendar months, monthly one month; usage cycle inside term and <= 31 days; live
term length = price's; no two active prices per slot; no price on a free version;
no new checkout on a retired price; no cross-tenant custom price; usage top-ups
never outlive a month. Run at the end of every annual scenario (0 violations),
over the oracle population (0), and over the real Paymob evidence DB (0).

## 23. Migration evidence

| Gate | Result |
| --- | --- |
| fresh empty -> head | PASS (`0081`) |
| previous head (0080, with real rows) -> head | PASS (`test_plan_price_migration.py`) |
| head -> 0080 -> head | PASS (empty and seeded) |
| `alembic heads` | `0081 (head)` - single |
| `alembic current` | `0081 (head)` |
| `alembic check` | "No new upgrade operations detected." |
| `db_preflight verify` | ok |
| model/migration catalog parity | `scripts.schema_catalog`: "schemas identical"; `test_schema_parity.py` in the migration lane: see section 24 |
| preflight refusal | inconsistent renewal -> RuntimeError, still `0080`, no `plan_prices` |
| downgrade refusal | subscriber on an added yearly price -> RuntimeError, still `0081` |
| 0070/0071 suite (0069 -> head -> 0069 -> head) | PASS |

## 24. Tests

| Suite | Result |
| --- | --- |
| ruff / black --check / `mypy app tests` | clean / clean / "no issues found in 680 source files" |
| Unit (`tests/unit`) | 3,074 passed, 1 skipped (before new files); new unit files 30 passed |
| Billing family (targeted, model-built) | 714 passed + fixes (section 28) -> all green on rerun of affected files |
| New annual suites | `test_annual_billing.py` 43, concurrency 3, oracle 1, migration 2 - all passed |
| Whole model-built suite | 6,021 passed, 85 skipped, 3 failed (1,883 s). All three were stale registries/raw inserts in existing tests (new price routes unlisted; new NOT NULL usage columns missing from a raw INSERT), fixed in `1db424f`; those files then pass 36/36 |
| Migration-built integration + e2e | parity 4 passed; integration + e2e: 2,921 passed, 67 skipped, 2 deselected, 3 failed - the same three, run before the fix; rerun on the migration-built schema: 36/36 passed |
| Calendar property checks | 1,200,040, 0 violations (`python -m scripts.billing_calendar_properties 1200000`) |
| Entitlement oracle comparisons | 1,440, 0 mismatches (48 workspaces x 5 moments x 6 keys: monthly/yearly; active, past_due, cancelled, expired, suspended; base, zero, unlimited; live/expired/future/refund_review/cancelled top-ups and grants) |

Baseline on the untouched base (model-built): 5,928 passed, 85 skipped, 0 failed.

## 25. Mutation campaign

Each mutant applied to a copy of the tree, the annual suites run, the file
restored. **20 applied, 20 killed, 0 equivalent, 0 survived.**

| ID | Mutant | Killed by |
| --- | --- | --- |
| AB-M01 | yearly gets 12x usage | `test_annual_usage_resets_monthly_and_is_never_granted_twelve_times` |
| AB-M02 | usage counted over the annual term | same |
| AB-M03 | usage top-up expires at the annual boundary | `test_a_usage_top_up_on_an_annual_plan_ends_with_its_month` |
| AB-M04 | capacity top-up expires at the monthly boundary | `test_a_capacity_top_up_..._lasts_the_year_and_deletes_nothing` |
| AB-M05 | annual renewal charges the monthly price | `test_retiring_a_price_keeps_its_subscribers_and_its_history` |
| AB-M06 | annual renewal advances one month | same |
| AB-M07 | monthly renewal advances one year | `test_yearly_to_monthly_waits_for_the_end_of_the_paid_year` |
| AB-M08 | usage roll creates an invoice | `test_annual_usage_resets_monthly_...` |
| AB-M09 | usage roll makes a provider charge | same |
| AB-M10 | new price mutates the existing row | `test_a_price_is_refused_for_a_duplicate_term_...` |
| AB-M11 | subscribers re-pinned to the new price | `test_retiring_a_price_keeps_its_subscribers_...` |
| AB-M12 | checkout trusts a client amount | `test_a_checkout_never_trusts_the_client_...` |
| AB-M13 | scheduled change without price | `test_yearly_to_monthly_waits_...` |
| AB-M14 | offer lets customer change interval | `test_an_offer_names_its_price_and_the_customer_cannot_change_it` |
| AB-M15 | retired price selectable | `test_retiring_a_price_keeps_...` |
| AB-M16 | cross-tenant custom price accepted for an offer | `test_another_workspaces_custom_price_is_refused_everywhere` |
| AB-M17 | no-card annual renewal attempts MOTO | `test_an_annual_renewal_without_a_card_is_never_charged_automatically` |
| AB-M18 | saved-card annual renewal left to hosted checkout | `test_an_annual_renewal_with_a_saved_card_is_one_moto_charge_for_the_year` |
| AB-M19 | annual cancel ends at the monthly boundary | `test_cancelling_an_annual_subscription_ends_with_the_year_not_the_month` |
| AB-M20 | price snapshot follows a newly published price | `test_an_open_annual_page_settles_at_the_price_it_was_opened_at` |

## 26. Real Paymob Test evidence

Environment: API from this worktree on `127.0.0.1:57455`, `BILLING_PROVIDER=paymob`,
evidence database `wasla_annual_paymob` (kept, isolated), integrations 5885262
(hosted card) and 5934829 (MOTO), ngrok `recycler-gondola-numeral.ngrok-free.dev`
(public `/health/ready` 200; unsigned POST to the webhook 403), Chrome (Claude
in Chrome) on Paymob's hosted page (`egy_pk_test_…`), Paymob's published Test
Mastercard …2346. Every payment `provider_mode = test`; Live never used. Yearly
prices are synthetic Test values (Business 2,990; custom 2,500).

| ID | Scenario | Result |
| --- | --- | --- |
| A1 | Business yearly hosted purchase (Save Card) | **PASS - REAL PAYMOB TEST.** Page "EGP 2,990.00, Business plan (yearly)"; txn 543161957 (5885262); invoice paid 2,990.00 yearly 2026-09-27 -> 2027-09-27; subscription yearly price, term 12 months, usage cycle 1 month; real TOKEN callback saved …2346 |
| A2 | Replay the genuine callbacks (ngrok agent) | **PASS - REAL PAYMOB TEST.** TRANSACTION -> `duplicate`; TOKEN -> `payment_method_created: false`; one event, one payment, same term, one card |
| A3 | Annual saved-card renewal (time shift, this DB only) | **PASS - REAL PAYMOB TEST.** Real sweep: one MOTO charge on **5934829** for **2,990.00**, txn 543162876, settled by callback; term +12 months, first cycle 1 month; second sweep handled 0 |
| A4 | Annual no-card renewal | **PASS - REAL PAYMOB TEST.** B paid yearly without saving (txn 543163502); after shift, sweep: **0 MOTO**; renewal invoice 2,990.00 yearly paid at hosted checkout, txn 543164358, applied once |
| A5 | Yearly custom offer | **PASS - REAL PAYMOB TEST.** Offer shows yearly 2,500.00 EGP + seven limits; accept -> page "Enterprise Custom plan (yearly) EGP 2,500.00"; txn 543164983; offer `active` once; yearly term, monthly cycle |
| A6 | Custom annual renewal without card | **PASS - REAL PAYMOB TEST.** 0 MOTO; hosted yearly renewal invoice 2,500.00 |
| A7 | Lost callback (API stopped) | **PASS - REAL PAYMOB TEST.** Paymob's callback 502 at ngrok; D pending on Starter; API restored; sweep's Transaction Inquiry settled once (txn 543165945), `recovered_by_reconciliation` resolved, yearly term |
| T1 | Usage top-up on annual (AI turns +10,000) | **PASS - REAL PAYMOB TEST.** 150.00, txn 543167167; granted, **expires 2026-10-27** (usage cycle), not 2027-09-27 |
| T2 | Capacity top-up on annual (WhatsApp +1) | **PASS - REAL PAYMOB TEST.** 200.00, txn 543167785; granted, **expires 2027-09-27** (billing term) |

Evidence DB afterwards: 8 settled payments (1 automatic), 0 live, no invoice
with two applied payments, invariant ledger 19 rules / 0 violations.

## 27. Security / secret scan

Scanned: API log, ngrok log, mutation log, suite log, evidence-DB audit meta,
incident details, payment-event details, invoice lines, payment failure
reasons, the git diff, and the new annual files - for every value in
`E:\secrets.txt` (28 values incl. the session keys and the ngrok authtoken),
Paymob secret/client-secret shapes, JWTs, the Test PAN, CVV fields and raw card
tokens: **0 leaks**. The launcher loaded only the four Paymob Test keys,
refused non-test keys, and printed no value; hosted URLs went through a
localhost redirector. (The Chrome tool's own tab listing showed the Test page
URLs, which carry a one-page Test client secret, in the session transcript
only; none reached a file or the repository.)

## 28. Compatibility

Existing tests changed, and why:

- `tests/billing_fixtures.py`: `pin()` also pins the version's published price
  (looked up before the row changes); new `price_terms()` for hand-built
  invoices; `renewal_invoice()` names its price.
- Hand-built priced invoices/subscriptions in six suites now name their price
  (the database now requires it); `topup_harness` and two tests pass `price=` to
  `apply_purchase`; `test_billing_endpoints` stubs gained the new read methods;
  the forged-offer invoice in `test_custom_plan_offers` carries the offer's price
  so the test still exercises the cross-workspace refusal; a manual reset in
  `test_topup_concurrency` clears the scheduled price too;
  `test_payment_methods_are_configuration` pins the checkout signature with
  `plan_price_id` (a *what-is-bought* parameter, not a payment method).
- API: all additions are additive except the platform `PATCH /prices/{id}`
  (new, always 409). Route count 192 -> 198 (docs/API.md).

## 29. Remaining deployment verification

- Run 0081 against a copy of production; its preflight lists any history it
  refuses to guess about.
- Publish real annual prices through the platform API (none are invented).
- Confirm the Paymob Live MOTO integration accepts annual amounts (Test only
  here).
- The base branch (`database-findings-remediation`) is itself unmerged; this
  branch must be merged after it.
- Front-end: show `prices[]`, send `plan_price_id`.

## 30. Commit ledger

All on branch `annual-billing` (base `4654a79`), local only - not merged, not pushed.

| Commit | Message | Contents |
| --- | --- | --- |
| `1db424f` | feat(billing): sell plan versions at monthly and yearly prices | models, migration 0081, services, worker, platform/customer API, schemas, adapted existing tests |
| `546c9da` | test(billing): prove annual billing, usage cycles and price pinning | annual integration/concurrency/oracle/migration suites, unit tests, calendar property script |
| `91e7ffb` | docs(billing): document monthly and yearly prices (ADR-116) | DECISIONS.md, README, docs/API.md, BILLING.md, BILLING_OPERATIONS.md, RUNBOOK.md |
| (this) | docs(billing): record annual billing implementation | this report |

Operational note: during the Paymob run one broad `taskkill /F /IM python.exe`
with a window-title filter was issued to stop the local API; it matched no
other process (the running pytest lane survived), but it should not have been
that broad.

## 31. Final verdict

**Implemented and verified locally; not deployed.** Monthly behaviour is
preserved; yearly prices, monthly usage cycles inside annual terms, pinned
prices everywhere and the platform/customer APIs are in place and proven by
the suites, oracle, property checks, mutation campaign and real Paymob
**Test** payments (A1-A7, T1, T2 all PASS). PAY-E2E-01/02/03 remain closed.
Production readiness still depends on section 29: running 0081 against a
production copy, publishing real annual prices, confirming Live MOTO for
annual amounts, and merging the base branch first.
