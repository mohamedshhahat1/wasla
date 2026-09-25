# Wasla — Billing Findings Remediation

Remediation of the independent billing audit `billing-03bb13b1`
([BILLING_PAYMENTS_AUDIT.md](BILLING_PAYMENTS_AUDIT.md)).

| | |
|---|---|
| Audit | `billing-03bb13b1`, frozen at `e1b5cec`, score **55 / 100**, verdict **NOT READY** |
| Branch | `billing-findings-remediation` (worktree `E:\wasla-billing-remediation`), from `worktree-billing-google-auth` @ `e1b5cec` |
| Migrations | baseline `0069` → `0070`, `0071` (single head `0071`) |
| Remediated score | **≈ 86 / 100** (section 9) |
| Verdict | **READY for Paymob Test / staging.** Live money still needs a separate Live deployment verification (section 11). |
| Merge / push | Not merged and not pushed. |

---

## 1. Summary

Every finding from BILL-01 to BILL-20 is fixed, and the three INFO items are
addressed or documented. The fixes rest on one commercial model, one
settlement engine and an operator control plane:

* **Commercial rules** (ADR-112): Starter is free for good, with no trial on
  any plan. Renewals are billed in advance from a durable anchor. An upgrade
  starts a full new period on settlement, with no credit. Downgrades and
  cancellations take effect at period end.
* **Immutable plan versions.** Subscriptions and invoices are pinned to one
  version. A price or limit change reaches new customers only, until an
  operator schedules a migration. That migration is adopted at each
  subscriber's renewal, and only once that renewal is paid.
* **Invoice purposes.** Every invoice is a checkout, renewal, manual or
  adjustment invoice. A strict predicate decides which renewals may be charged
  to a saved card. Every checkout is an immutable snapshot of what was bought.
* **`InvoiceSettlement`** is the only path that applies money or voids an
  invoice. Callbacks, reconciliation and manual payments all go through it,
  and every refusal becomes a durable `billing_incidents` row.
* **Paymob binding.** A callback must match the signed order id, the
  integration and the test/live mode. TOKEN callbacks are correlated by order
  id. Lost callbacks are recovered by transaction inquiry and Card Token
  Inquiry. MOTO charges carry the owner's e-mail. Provider errors are
  classified as permanent, retryable or ambiguous.
* **`/api/v1/platform/billing/*`**: 29 platform routes with RBAC, optimistic
  concurrency, a full audit trail and access auditing of reads.
* **Metrics and alerts:** closed-label billing counters and eight alert rules,
  each with a promtool test.

The key fixes were proven against **real Paymob Test** through ngrok: an
initial 99 EGP payment, a real TOKEN callback that saved a card, a real
MOTO/MIT renewal, rerun and concurrent renewal safety, an abandoned
checkout never charged, a lost callback recovered by inquiry, and replay
safety (section 8). The audit's real runs had failed at the first three.

Mutation testing (section 7) ends with 58 mutants: 56 killed, 2 equivalent and
0 surviving. That includes the audit's three survivors (B17, B24, B29).

---

## 2. Commits

| Commit | Message |
|---|---|
| `0d92caa` | docs(billing): record independent billing audit billing-03bb13b1 |
| `d0eba2c` | feat(billing): remediate billing audit billing-03bb13b1 core findings |
| `c8b937a` | feat(platform): add billing control plane under /platform/billing |
| `7e0c64e` | test(billing): prove the remediated billing journeys end to end |
| `9068b8a` | feat(observability): add billing alert rules with promtool tests |
| `a71ce15` | docs(billing): document the commercial rules and operator procedures |
| `f1a1567` | fix(billing): read Paymob's no-card 404 as no card, not a failure |
| `0e3cccd` | test(billing): pin a checkout to the terms it was opened at |
| `0658c3d` | test(billing): roll a legacy free trial on as active |
| `62c3e2e` | test(billing): refuse our reference on an order Wasla never created |
| `17a6dbd` | test(billing): make the entitlement race test depend on the lock |
| `b7fd83d` | test(billing): prove the billing sweep itself recovers lost hosted payments |
| `ffa43ac` | test(billing): count every billing incident exactly once |
| `9ad172c` | test(billing): prove roll-over counts periods from the billing anchor |
| `ae30827` | test(billing): prove an invoice alone keeps its tenant row |

Each commit was checked from an exported snapshot of its own tree:
ruff, black, mypy, the unit suite, and the route, documentation and policy
tests. The core, domain and settlement changes are one commit because they
cannot be separated without intermediate trees that fail their tests.

*Correction to `7e0c64e`'s message:* the decline→success regression is
`test_declined_and_saved_cards.py::test_a_success_after_a_decline_on_the_same_page_settles_once`,
and the month-end anchor is proven in `tests/unit/test_billing_calendar.py`.
Neither lives in the journeys file the message describes. History was not
rewritten to fix this.

`f1a1567` came out of the real E2E. Paymob Test answers Card Token Inquiry
with 404 "No card tokens found for this order" whenever the customer did not
save the card. The reconciler used to log that as a failure.

The eight `test(billing)` commits after `f1a1567` each close a gap the
mutation run found (section 7). Every new test was first shown to fail
against its mutant on this branch, with the file restored and its sha256
checked, before it was committed. One of them, `17a6dbd`, corrects a test of
mine rather than adding one. The BILL-08 race test passed even with the
advisory lock removed: its fixture left the plan unversioned, so the two racing
requests serialised on materialising version 1. With the version published
first, removing the lock now yields two rows for every resource.

---

## 3. Findings ledger

| ID | Sev. | Status | Fix | Regression proof |
|---|---|---|---|---|
| BILL-01 | HIGH | **Fixed** | Free plans have no trial and no expiry; `0070` revives expired free trials; a settled purchase is granted through `apply_purchase` whatever the prior state, including after an expiry or a cancellation; money for a checkout opened *before* a later cancellation is held and raised as a `refused_settlement` incident | `test_starter_is_permanently_free_and_a_later_purchase_is_granted`, `test_a_purchase_after_cancelling_brings_the_workspace_back`, `test_billing_migrations.py`; **real**: new signup is `active`, trial 0 |
| BILL-02 | HIGH | **Fixed** | Only an `OPEN`, issued `renewal` for the current period, priced at its version, of an `ACTIVE` workspace, with no refund and no unresolved attempt, may be charged (`_automatically_collectible`), checked again in `RecurringService` | `test_an_abandoned_upgrade_checkout_is_never_charged_by_the_sweep`; **real**: 299 EGP abandoned checkout never charged across two renewals |
| BILL-03 | HIGH | **Fixed** | Advance billing: the sweep rolls the period over first, then invoices the period that opened; settlement starts a fresh anchored period; `purpose` separates checkout from renewal uniqueness | `test_every_paid_period_is_billed_exactly_once`, `test_billing_worker.py` |
| BILL-04 | MED | **Fixed** | `provider_order_id` stored separately from `provider_intent_reference`; TOKEN callbacks correlated on it; Card Token Inquiry recovery | `test_paymob_webhook_endpoint.py`; **real**: TOKEN callback saved the card |
| BILL-05 | MED | **Fixed** | MOTO billing block carries the earliest active owner's e-mail and name; no valid address is a permanent refusal before anything is sent; permanent 4xx stops collection, raises an incident and increments a metric | `test_an_automatic_charge_sends_the_owners_real_email`, `test_a_permanent_intention_refusal_stops_automatic_collection`; **real**: MIT accepted and settled |
| BILL-06 | HIGH | **Fixed** | Every checkout is a new invoice freezing version, price, currency and interval; invoices are never re-priced; settlement grants the invoice's own version | `test_a_cheaper_page_buys_only_the_cheaper_plan`, B17 (section 7) |
| BILL-07 | HIGH | **Fixed** | A hosted decline records history and keeps the payment `pending` (`DECLINED`); a later success on the same order settles | `test_a_success_after_a_decline_on_the_same_page_settles_once` (real reproduction not possible; section 8) |
| BILL-08 | MED | **Fixed** | `require_entitlement` → `reserve_or_refuse`: per-workspace `pg_advisory_xact_lock` held to commit; invitations count; campaigns reserve pending sends | `test_entitlement_races.py` (agents, numbers, invitations, documents under real concurrency) |
| BILL-09 | MED | **Fixed** | `run_hosted` in the sweep asks Paymob by merchant order id and applies the answer through `CheckoutService.apply`; recovers the card; raises `recovered_by_reconciliation` | `test_a_hosted_payment_whose_callback_was_lost_is_recovered_by_inquiry`; **real**: recovered with the API down |
| BILL-10 | MED | **Fixed** | Manual payment and void go through `InvoiceSettlement`: restore past-due/suspended, grant purchases, explicit void policy (`unchanged`/`cancel`/`waive`) | `test_a_manual_payment_restores_a_suspended_workspace`, `test_voiding_an_overdue_renewal_needs_an_explicit_policy` |
| BILL-11 | MED | **Fixed** | `order.id` must equal the stored order; integration must be allowed (hosted ids + `PAYMOB_CALLBACK_INTEGRATION_IDS`, MOTO only for automatic); `is_live` must match the key's mode; mismatch raises an incident | `test_a_signed_callback_cannot_be_re_aimed_or_cross_environments` |
| BILL-12 | MED | **Fixed** | `plan_versions` (trigger-immutable), platform plan API with validation, preview, migrations, optimistic concurrency and audit; DB CHECKs on money; EGP only | `test_billing_constraints.py`, `test_platform_billing_api.py`, `test_a_price_change_reaches_new_customers_and_not_existing_ones`, `test_a_scheduled_migration_moves_a_subscriber_only_when_the_renewal_is_paid` |
| BILL-13 | MED | **Fixed** | Tenant refund → review request (`202`, incident, audit, no money moves); platform refunds full or partial; partial operator refund is goodwill, full withdraws the plan and voids | `test_billing_authorization.py`, `test_a_platform_refund_is_bounded_and_confirmed_later` |
| BILL-14 | MED | **Fixed** | Seven closed-label counters and one histogram; eight alert rules with promtool tests | `deploy/monitoring/tests/alerts_test.yml`, `test_metric_catalogue.py` |
| BILL-15 | LOW | **Fixed** | Durable, deduplicated `billing_incidents` for duplicate, refused, mismatched and unknown money and for permanent provider errors; platform list and resolve | journeys; `test_paymob_checkout.py` |
| BILL-16 | LOW | **Fixed** | Flush before counting abandoned attempts | `test_the_first_abandonment_backs_off_fifteen_minutes_without_autoflush` |
| BILL-17 | LOW | **Fixed** | Collection and dunning claims exclude non-`ACTIVE` workspaces | `test_a_platform_suspended_workspace_is_never_charged` |
| BILL-18 | LOW | **Fixed** | `billing_anchor_at` + `next_boundary`: 31 Jan → 28 Feb → 31 Mar; 29 Feb yearly → 28 Feb, 29 Feb in leap years | `test_billing_calendar.py` |
| BILL-19 | LOW | **Fixed** | Financial FKs `ON DELETE RESTRICT`; purge classifies incidents and adjustments as retained | `test_a_tenant_with_a_financial_ledger_cannot_be_deleted`, `test_workspace_purge.py` |
| BILL-20 | LOW | **Fixed** | One invoice state machine; settlement refuses paid/void/draft, and uncollectible without an explicit recovery flag; money invariants in the DB | `test_billing_constraints.py`, `test_platform_billing_api.py` |
| BILL-21 | INFO | Documented | Subscriptions Module still unused by design; void/tax/credit notes/proration/overage are listed as not built in BILLING.md | — |
| BILL-22 | INFO | **Fixed** / kept | The billing block no longer says `NOT_COLLECTED` (**real** checkout showed "E2E a, Phone: NA"); unsigned callbacks still answer 403 to keep the Security subsystem's contract (docstring corrected) | `test_paymob_http.py` |
| BILL-23 | INFO | **Fixed** | `trial_days` forced to 0 and only 0 accepted; `Subscription.provider*` documented as reserved | `test_an_invalid_plan_is_refused[invalid2]` (`trial_days: 14` → 422) |

### Product decisions implemented as locked

Starter permanently free and `ACTIVE`. Bill in advance. Upgrade is immediate,
with a full new period and no credit. Downgrade at period end through a
scheduled change. Cancel at period end by default. Plan versions are immutable
and subscriptions are pinned. Price and limit changes apply to new customers
only, until an explicit migration. The checkout snapshot is immutable. Invoice
purposes are CHECKOUT, RENEWAL, MANUAL and ADJUSTMENT. MIT eligibility is
strict. The Paymob intention id and order id are stored separately. TOKEN
callbacks are correlated on the order id, with Card Token Inquiry recovery.
MOTO uses a valid billing e-mail (the owner's). Errors are permanent,
retryable or ambiguous. Decline→success on the same order settles. Callbacks
are bound to the signed order id, integration and `is_live`. Count limits are
concurrency-safe. Hosted payments are reconciled through the same settlement
path. Manual settlement and void semantics are unified. There is a platform
billing control plane. Tenant refunds are request-only. Billing has metrics
and alerts. Duplicate payments become durable incidents. The backoff
autoflush is fixed. MIT is paused for suspended workspaces. The billing
anchor holds. Financial FKs are RESTRICT. There is one invoice state machine.
The database enforces money constraints. EGP is the only currency.

---

## 4. Schema changes

**`0070` (data):** `trial_days = 0` on every plan. A subscription trialing a
free plan becomes `active`. A free-plan trial that expired without a
cancellation becomes `active` again with a fresh period. `cancelled` and
priced plans are untouched. The downgrade restores the Starter catalogue trial
only.

**`0071`:**
* `plan_versions` (immutable trigger; v1 backfilled per plan, effective at the
  epoch).
* `plans.revision`.
* Subscription columns: `plan_version_id`, `billing_anchor_at` (legacy rule:
  the start when start + interval = end, else the end), the scheduled-change
  columns, and `revision`.
* `invoices.purpose` (issued → renewal, else checkout), plus `plan_version_id`
  and `revision`. The unique constraint on `(tenant_id, period_start)` is
  replaced by a partial index for renewals only.
* Payment columns: `provider_order_id`, `provider_mode`,
  `provider_integration_id`, `refund_requested_amount` (backfilled for
  outstanding requests) and `revision`.
* New tables: `plan_version_migrations`, `billing_adjustments`,
  `billing_incidents`.
* Financial FKs switched to RESTRICT.
* Money CHECKs, added `NOT VALID` and then validated, so any legacy
  violations warn instead of blocking the deployment.
* New `audit_action` labels.

Verified by `alembic upgrade`/`downgrade`/`upgrade`, with `alembic check`
clean and a single head. `test_billing_migrations.py` builds a database to
`0069` holding real billing shapes (a free workspace mid-trial, expired and
cancelled free workspaces, paid subscriptions anchored on the 31st and on a
leap day, an issued renewal, an unissued checkout, a payment with an
outstanding refund request), upgrades it, inspects every backfill, downgrades
and upgrades again.

---

## 5. Platform billing control plane

`/api/v1/platform/billing/*` has 29 operations. `PLATFORM_OWNER` or
`PLATFORM_ADMIN` can use all of them, and `DELETE /plans/{id}` is owner-only.
Every write carries a `reason` and an `expected_revision` (or
`expected_version`), answers `409` when stale, and is audited with actor,
role, reason, before, after and request id. Every read records a
`platform_billing_read` access entry (ADR-095). Lists are offset-paged, 100 at
most. Responses are checked in tests for the absence of any token, secret or
payload marker.

| Area | Operations |
|---|---|
| Catalogue | `GET /features`, `GET/POST /plans`, `GET/PATCH/DELETE /plans/{id}`, `POST /plans/{id}/activate`, `POST /plans/{id}/deactivate`, `GET/POST /plans/{id}/versions`, `POST /plans/{id}/versions/preview`, `POST /plans/{id}/migrations` |
| Subscriptions | `GET /subscriptions`, `GET /subscriptions/{id}`, `GET /subscriptions/{id}/timeline`, `POST /subscriptions/{id}/change-plan`, `/cancel`, `/resume` |
| Invoices | `GET /invoices`, `GET /invoices/{id}`, `POST /invoices/{id}/payments`, `POST /invoices/{id}/void` |
| Payments | `GET /payments`, `GET /payments/{id}`, `POST /payments/{id}/refund` |
| Reconciliation | `GET /reconciliation`, `POST /reconciliation/{payment_id}/run` |
| Incidents | `GET /incidents`, `POST /incidents/{id}/resolve` |

The tenant API adds `POST /billing/subscription/scheduled-change/cancel`.
`POST /billing/payments/{id}/refund` now files a review request. The
production shape serves **168 operations**, up from 138; `docs/API.md` states
this and a test asserts it.

---

## 6. Test results

| Gate | Result |
|---|---|
| ruff / black / mypy (app + tests, 625 files) | clean |
| `alembic heads` / `alembic check` | single head `0071` / no drift |
| Whole suite, model-built schema (`a71ce15`) | **5,586 passed, 88 skipped, 1 failed**¹ |
| Integration + e2e, migration-built schema (`a71ce15`) | **2,658 passed, 73 skipped, 1 failed**¹ |
| Tests added after that run (`f1a1567` … `ae30827`) | each new or changed test file passes (models mode; constraints also migration mode) |
| Billing killer suite, unmutated baseline (`f1a1567`) | **747 passed** |
| promtool `check rules` / `test rules` | 48 rules / all tests pass, including 8 new billing cases |
| DNS-dependent unit tests² | pass on a network with public DNS |

¹ `test_database_safety.py`: the two runs shared one checkout at the same time,
and this test writes and deletes a probe file in that checkout, so the two runs
collided. It passes when run on its own, in both schema modes.
² `test_whatsapp_client.py`, `test_media_fetch_boundary.py` and
`test_outbound_url_safety.py` resolve public hosts. The isolated test network
has no public DNS. None of them are billing tests.

New tests: journeys 18, platform billing API 15 (27 cases with parametrisation),
constraints 6 (14 cases), entitlement races 3 (5 cases), incidents 1,
migration data 1, billing calendar 5, token logging 4, and 2 new roll-over
cases in `test_subscription_lifecycle.py`. Existing test files adapted to the new rules: 32 (plus two new shared helpers, `tests/billing_fixtures.py` and `tests/paymob_orders.py`).
Tests that pinned audited defects were inverted rather than deleted:
`test_a_declined_payment_cannot_later_succeed` (BILL-07), the dunning
expectations around trials (BILL-01), and owner refunds (BILL-13).

---

## 7. Mutation testing

Worktree `E:\wasla-billing-remediation-mutation` (detached at `f1a1567`),
lane 1, database `wasla_mut`. The killer suite is the audit's 36
billing-targeted files plus 7 new ones (8 in pass 2, which adds
`test_billing_incidents.py`). Each mutant runs its relevant files
first, then the rest, with `-x`. A mutant counts as **KILLED** only on a real
test failure. Each mutated file is restored byte for byte and its sha256
compared (first 16 hex digits shown). The unmutated baseline of the same suite
passed (747).

The first attempt was discarded. Its syntax check used `py_compile`, which
wrote `.pyc` files, and an aborted pass left stale mutated bytecode behind for
same-length mutants. That falsely "killed" B16 and B17. All bytecode was
purged, the check now compiles in memory, and every mutant was rerun.

| ID | Property broken | File | Pass 1 (`f1a1567`) | Pass 2 (`ae30827`) | Final | Killer test (final) | sha256 before → after |
|---|---|---|---|---|---|---|---|
| B01 | invoice lookup without tenant filter | `app/repositories/invoice_repository.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_authorization.py::test_a_checkout_cannot_be_started_against_another_workspaces_invoice` | `3240c1f1b4497133` → `3240c1f1b4497133` |
| B02 | payment-method lookup without tenant filter | `app/repositories/payment_method_repository.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_authorization.py::test_another_workspaces_card_is_not_found` | `4b39892ef982093f` → `4b39892ef982093f` |
| B03 | transaction callback trusted without HMAC | `app/integrations/billing/paymob.py` | KILLED | — | **KILLED** | `tests/e2e/test_billing_lifecycle.py::test_a_forged_callback_cannot_settle_anything_over_a_real_socket` | `bb94764a491ae98a` → `bb94764a491ae98a` |
| B04 | callback amount not checked | `app/services/checkout_service.py` | KILLED | — | **KILLED** | `tests/e2e/test_billing_lifecycle.py::test_a_forged_callback_cannot_settle_anything_over_a_real_socket` | `ce48fc2bdfe0ceb6` → `ce48fc2bdfe0ceb6` |
| B05 | callback currency not checked | `app/services/checkout_service.py` | KILLED | — | **KILLED** | `tests/integration/test_paymob_checkout.py::test_a_callback_in_a_different_currency_is_refused` | `ce48fc2bdfe0ceb6` → `ce48fc2bdfe0ceb6` |
| B07 | provider event id made unique per delivery (no replay dedup) | `app/services/checkout_service.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_concurrency.py::test_four_simultaneous_deliveries_settle_the_invoice_once` | `ce48fc2bdfe0ceb6` → `ce48fc2bdfe0ceb6` |
| B08 | checkout idempotency key dropped | `app/services/checkout_service.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_concurrency.py::test_one_idempotency_key_opens_one_payment_page` | `ce48fc2bdfe0ceb6` → `ce48fc2bdfe0ceb6` |
| B09 | subscription claim without SKIP LOCKED | `app/repositories/billing_repository.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_sweep_concurrency.py::test_a_row_another_worker_holds_is_skipped_rather_than_waited_for` | `662dba65c2879580` → `662dba65c2879580` |
| B10 | plan granted on a failed payment | `app/services/checkout_service.py` | KILLED | — | **KILLED** | `tests/integration/test_declined_and_saved_cards.py::test_an_automatic_charge_declined_stays_declined` | `ce48fc2bdfe0ceb6` → `ce48fc2bdfe0ceb6` |
| B12 | any payment transition allowed | `app/db/models/invoice.py` | KILLED | — | **KILLED** | `tests/integration/test_declined_and_saved_cards.py::test_an_automatic_charge_declined_stays_declined` | `9ce031d62eefaa1e` → `9ce031d62eefaa1e` |
| B14 | token AAD not bound to tenant/method | `app/services/payment_token_service.py` | KILLED | — | **KILLED** | `tests/unit/test_payment_token_protection.py::test_ciphertext_cannot_be_moved_to_another_row_or_used_as_plaintext` | `a519734299aa8cc7` → `a519734299aa8cc7` |
| B15 | saved token stored in plaintext | `app/services/payment_method_service.py` | KILLED | — | **KILLED** | `tests/integration/test_declined_and_saved_cards.py::test_a_saved_card_stores_a_token_and_no_card_number` | `1a87ae14273501be` → `1a87ae14273501be` |
| B16 | fingerprint dedup removed | `app/services/payment_method_service.py` | SURVIVED | SURVIVED | **EQUIVALENT** | equivalent: the `(provider, token_fingerprint)` unique key and its `IntegrityError` recovery still yield one card; covered by **B16b** | `1a87ae14273501be` → `1a87ae14273501be` |
| B17 | checkout settles at the plan's current terms, not the frozen snapshot | `app/services/settlement_service.py` | SURVIVED | KILLED | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_a_checkout_settles_on_the_terms_it_was_opened_at` | `e5f88457acc9af4f` → `e5f88457acc9af4f` |
| B20 | entitlement guard no longer refuses | `app/services/entitlement_service.py` | KILLED | — | **KILLED** | `tests/integration/test_entitlement_races.py::test_two_creations_at_a_limit_of_one_leave_one[agents-Agent-_agent]` | `b70cd9d2779f54bf` → `b70cd9d2779f54bf` |
| B21 | AI-turn consumption without advisory lock | `app/services/entitlement_service.py` | KILLED | — | **KILLED** | `tests/integration/test_ai_metering.py::test_concurrent_turns_never_oversell_the_turn_allowance` | `b70cd9d2779f54bf` → `b70cd9d2779f54bf` |
| B22 | any member may start a checkout | `app/api/v1/billing.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_authorization.py::test_only_an_owner_may_start_a_checkout[member]` | `3f692a85d204b7cc` → `3f692a85d204b7cc` |
| B23 | ambiguous MIT timeout treated as not-sent | `app/services/recurring_service.py` | KILLED | — | **KILLED** | `tests/integration/test_recurring_billing.py::test_a_timed_out_charge_is_unknown_rather_than_failed` | `eab6d93f889c9480` → `eab6d93f889c9480` |
| B24 | saved card token logged | `app/integrations/billing/paymob.py` | KILLED | — | **KILLED** | `tests/unit/test_paymob_token_logging.py::test_a_saved_card_charge_logs_no_bearer_value` | `bb94764a491ae98a` → `bb94764a491ae98a` |
| B25 | saved token exposed in API response | `app/schemas/invoice.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_authorization.py::test_an_owner_may_manage_saved_cards` | `8ed8ac408a193d8e` → `8ed8ac408a193d8e` |
| B26 | obj.id used as the reusable token | `app/integrations/billing/paymob.py` | KILLED | — | **KILLED** | `tests/unit/test_paymob_token_logging.py::test_a_card_token_inquiry_logs_no_bearer_value` | `bb94764a491ae98a` → `bb94764a491ae98a` |
| B27 | MIT intention on the customer integration, not MOTO | `app/integrations/billing/paymob.py` | KILLED | — | **KILLED** | `tests/integration/test_recurring_billing.py::test_the_charge_uses_the_moto_integration_and_our_own_reference` | `bb94764a491ae98a` → `bb94764a491ae98a` |
| B28 | client_secret used instead of payment_keys[0].key | `app/integrations/billing/paymob.py` | KILLED | — | **KILLED** | `tests/integration/test_recurring_billing.py::test_the_charge_uses_the_moto_integration_and_our_own_reference` | `bb94764a491ae98a` → `bb94764a491ae98a` |
| B29 | callback no longer resolves an unresolved collection attempt | `app/services/checkout_service.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_every_paid_period_is_billed_exactly_once` | `ce48fc2bdfe0ceb6` → `ce48fc2bdfe0ceb6` |
| B30 | period invoice idempotency check removed | `app/services/invoice_service.py` | KILLED | — | **KILLED** | `tests/integration/test_invoicing.py::test_billing_the_same_period_twice_issues_one_invoice` | `e1b37768923d07dd` → `e1b37768923d07dd` |
| B31 | cancelled subscription revived by payment | `app/services/settlement_service.py` | KILLED | — | **KILLED** | `tests/integration/test_dunning_lifecycle.py::test_an_old_callback_does_not_revive_an_ended_subscription[cancelled]` | `e5f88457acc9af4f` → `e5f88457acc9af4f` |
| B32 | cancelled workspace still auto-charged | `app/services/recurring_service.py` | KILLED | — | **KILLED** | `tests/integration/test_recurring_billing.py::test_a_cancelled_workspace_is_never_charged` | `eab6d93f889c9480` → `eab6d93f889c9480` |
| B33 | suspension ignores unresolved collection attempts | `app/workers/billing_worker.py` | KILLED | — | **KILLED** | `tests/integration/test_dunning_lifecycle.py::test_a_workspace_whose_charge_may_have_landed_is_not_suspended` | `7b3b445966a5ebe9` → `7b3b445966a5ebe9` |
| B34 | self-service may select a priced plan | `app/services/subscription_service.py` | KILLED | — | **KILLED** | `tests/integration/test_paid_plan_settlement.py::test_a_priced_plan_cannot_be_chosen_without_paying` | `85f493e5910cdbc3` → `85f493e5910cdbc3` |
| B35 | deleted workspaces not excluded from collection | `app/repositories/invoice_repository.py` | KILLED | — | **KILLED** | `tests/integration/test_workspace_billing_lifecycle.py::test_automatic_collection_will_not_claim_a_deleted_workspaces_invoice` | `3240c1f1b4497133` → `3240c1f1b4497133` |
| R-BILL-01 | a free plan's trial expires again | `app/services/subscription_service.py` | SURVIVED | KILLED | **KILLED** | `tests/unit/test_subscription_lifecycle.py::test_a_legacy_free_trial_rolls_on_as_active_rather_than_expiring` | `85f493e5910cdbc3` → `85f493e5910cdbc3` |
| R-BILL-02b | the sweep's renewal loses its purpose (never MIT-eligible) | `app/services/invoice_service.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_every_paid_period_is_billed_exactly_once` | `e1b37768923d07dd` → `e1b37768923d07dd` |
| R-BILL-02 | an abandoned checkout invoice is eligible for a saved-card charge | `app/repositories/invoice_repository.py` | SURVIVED | SURVIVED | **EQUIVALENT** | equivalent: an abandoned checkout is still excluded by `issued_at IS NOT NULL`, `amount_paid = 0`, the price/period match and `RecurringService`'s own purpose check in every reachable state | `3240c1f1b4497133` → `3240c1f1b4497133` |
| R-BILL-11 | transaction callback not bound to the stored Paymob order | `app/services/checkout_service.py` | SURVIVED | KILLED | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_a_signed_callback_cannot_be_re_aimed_or_cross_environments` | `ce48fc2bdfe0ceb6` → `ce48fc2bdfe0ceb6` |
| R-BILL-05 | MOTO sends a placeholder billing e-mail | `app/integrations/billing/paymob.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_an_automatic_charge_sends_the_owners_real_email` | `bb94764a491ae98a` → `bb94764a491ae98a` |
| R-BILL-06 | purchase re-priced at current catalogue terms (alias of B17 at the grant) | `app/services/settlement_service.py` | SURVIVED | KILLED | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_a_checkout_settles_on_the_terms_it_was_opened_at` | `e5f88457acc9af4f` → `e5f88457acc9af4f` |
| R-BILL-07 | a hosted decline closes the checkout | `app/services/checkout_service.py` | KILLED | — | **KILLED** | `tests/integration/test_declined_and_saved_cards.py::test_a_declined_payment_changes_nothing_it_should_not` | `ce48fc2bdfe0ceb6` → `ce48fc2bdfe0ceb6` |
| R-BILL-08 | count limits reserved without the advisory lock | `app/services/entitlement_service.py` | SURVIVED | KILLED | **KILLED** | `tests/integration/test_entitlement_races.py::test_two_creations_at_a_limit_of_one_leave_one[agents-Agent-_agent]` | `b70cd9d2779f54bf` → `b70cd9d2779f54bf` |
| R-BILL-09 | hosted reconciliation no longer runs in the sweep | `app/workers/billing_worker.py` | SURVIVED | KILLED | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_the_billing_sweep_itself_recovers_a_lost_hosted_payment` | `7b3b445966a5ebe9` → `7b3b445966a5ebe9` |
| R-BILL-20b | manual payment may overpay an invoice | `app/services/invoice_service.py` | KILLED | — | **KILLED** | `tests/integration/test_platform_billing_api.py::test_a_manual_payment_is_validated_and_settles_once` | `e1b37768923d07dd` → `e1b37768923d07dd` |
| R-BILL-05b | a permanent MOTO refusal is retried for ever | `app/services/recurring_service.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_a_permanent_intention_refusal_stops_automatic_collection` | `eab6d93f889c9480` → `eab6d93f889c9480` |
| R-BILL-12 | limits read from the live catalogue, not the pinned version | `app/services/entitlement_service.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_a_price_change_reaches_new_customers_and_not_existing_ones` | `b70cd9d2779f54bf` → `b70cd9d2779f54bf` |
| R-BILL-13b | an operator refund may exceed what is left of the payment | `app/services/refund_service.py` | KILLED | — | **KILLED** | `tests/integration/test_platform_billing_api.py::test_a_platform_refund_is_bounded_and_confirmed_later` | `e308113d64e4155c` → `e308113d64e4155c` |
| R-BILL-14 | incidents are no longer counted by metric | `app/services/billing_incident_service.py` | SURVIVED | KILLED | **KILLED** | `tests/integration/test_billing_incidents.py::test_an_incident_is_recorded_once_and_counted_once` | `c34e912c3b358951` → `c34e912c3b358951` |
| R-BILL-15 | a refused settlement raises no incident | `app/services/settlement_service.py` | KILLED | — | **KILLED** | `tests/integration/test_paymob_checkout.py::test_a_payment_does_not_revive_a_cancelled_subscription` | `e5f88457acc9af4f` → `e5f88457acc9af4f` |
| R-BILL-16 | abandonment counted before the flush (backoff off by one) | `app/services/recurring_service.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_the_first_abandonment_backs_off_fifteen_minutes_without_autoflush` | `eab6d93f889c9480` → `eab6d93f889c9480` |
| R-BILL-17 | platform-suspended workspace still auto-charged | `app/repositories/invoice_repository.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_a_platform_suspended_workspace_is_never_charged` | `3240c1f1b4497133` → `3240c1f1b4497133` |
| R-BILL-18 | period ends chained from the previous end | `app/services/subscription_service.py` | SURVIVED | KILLED | **KILLED** | `tests/unit/test_subscription_lifecycle.py::test_a_month_end_anchor_survives_february_through_roll_over` | `85f493e5910cdbc3` → `85f493e5910cdbc3` |
| R-BILL-19 | invoices cascade away with their tenant | `app/db/models/invoice.py` | SURVIVED | KILLED | **KILLED** | `tests/integration/test_billing_constraints.py::test_an_invoice_alone_keeps_its_tenant_row` | `9ce031d62eefaa1e` → `9ce031d62eefaa1e` |
| R-BILL-20 | any invoice transition allowed | `app/db/models/invoice.py` | KILLED | — | **KILLED** | `tests/unit/test_billing_state_machines.py::test_an_invoice_never_returns_to_draft` | `9ce031d62eefaa1e` → `9ce031d62eefaa1e` |
| R-PLAN-01 | a stale plan version publish overwrites silently | `app/platform/plan_admin.py` | KILLED | — | **KILLED** | `tests/integration/test_platform_billing_api.py::test_the_catalogue_lifecycle_is_versioned_serialised_and_audited` | `0f090a9271c57068` → `0f090a9271c57068` |
| R-PLAN-02 | a platform admin may hard-delete a plan | `app/api/v1/platform_billing.py` | KILLED | — | **KILLED** | `tests/integration/test_platform_billing_api.py::test_only_a_platform_owner_may_delete_a_plan` | `f3822530b7c5038f` → `f3822530b7c5038f` |
| R-PLAN-03 | a pinned subscriber is re-priced by a new version | `app/services/plan_catalog.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_a_price_change_reaches_new_customers_and_not_existing_ones` | `56211a89af699306` → `56211a89af699306` |
| R-BILL-03 | the sweep bills the period that just ended (arrears) | `app/workers/billing_worker.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_remediation_journeys.py::test_every_paid_period_is_billed_exactly_once` | `7b3b445966a5ebe9` → `7b3b445966a5ebe9` |
| R-BILL-04 | TOKEN callback correlated on the intention id, not the order id | `app/api/v1/payment_webhooks.py` | KILLED | — | **KILLED** | `tests/integration/test_paymob_webhook_endpoint.py::test_signed_card_token_is_stored_encrypted_and_retries_once` | `db5e4fd00aafd317` → `db5e4fd00aafd317` |
| R-BILL-10 | a paid renewal no longer lifts past-due / suspension | `app/services/settlement_service.py` | KILLED | — | **KILLED** | `tests/integration/test_platform_billing_api.py::test_a_manual_payment_is_validated_and_settles_once` | `e5f88457acc9af4f` → `e5f88457acc9af4f` |
| R-BILL-13 | a workspace owner refunds their own payment again | `app/api/v1/billing.py` | KILLED | — | **KILLED** | `tests/integration/test_billing_authorization.py::test_an_owner_refund_is_a_request_that_moves_no_money` | `3f692a85d204b7cc` → `3f692a85d204b7cc` |
| B16b | fingerprint made unique per notification (both dedup layers defeated) | `app/services/payment_token_service.py` | — | KILLED | **KILLED** | `tests/integration/test_declined_and_saved_cards.py::test_a_repeated_saved_card_notification_creates_one_card` | `a519734299aa8cc7` → `a519734299aa8cc7` |

**Pass 1** (`f1a1567`, all 57): killed 46, survived 11.
**Pass 2** (`ae30827`, the 11 survivors plus B16b): 10 killed, 2 survived (B16 and R-BILL-02, both equivalent).
**Final: 58 mutants: killed 56, equivalent 2, surviving (non-equivalent) 0.**
Every mutated file was restored byte for byte in both passes: **yes**; the
mutation worktree is clean.

The audit's three survivors are killed: **B17** by the new snapshot test
(`0e3cccd`), **B24** by `test_paymob_token_logging.py`, **B29** by
`test_a_callback_resolves_an_unresolved_automatic_attempt`.

Nine pass-1 survivors were real test gaps, and each is closed by one commit
(section 2): B17 and R-BILL-06 (`0e3cccd`), R-BILL-01 (`0658c3d`),
R-BILL-11 (`62c3e2e`), R-BILL-08 (`17a6dbd`), R-BILL-09 (`b7fd83d`),
R-BILL-14 (`ffa43ac`), R-BILL-18 (`9ad172c`) and R-BILL-19 (`ae30827`).
R-BILL-09 was also rewritten between passes. As first written, `hosted = 0 and ...`
crashed the phase instead of skipping it, so it was replaced with a clean skip,
`hosted = {} if True else ...`, which pass 2 applied.

Mutant IDs follow the audit's finding numbers: R-BILL-02 is MIT eligibility
(BILL-02), R-BILL-03 advance billing, R-BILL-11 callback binding, and so on.
The `b` variants are second mutants for the same finding: R-BILL-02b purpose
lost, R-BILL-05b permanent refusal retried, R-BILL-13b operator over-refund,
R-BILL-20b manual overpayment.


---

## 8. Real Paymob Test E2E (2026-09-25)

**Setup.** Paymob **Test** keys only; both key prefixes were checked
(`sk_test`, `pk_test`) before starting. The API ran from this branch on
`127.0.0.1:57455`, with the billing worker on the isolated `wasla-bill-rem`
network. Database `wasla_bill_e2e` was freshly migrated to `0071`. Redis lane
1, database 7, never DB0. Callback and redirect URLs were set per intention
(`APP_PUBLIC_URL`), so **no Paymob dashboard setting was changed**.

The existing ngrok agent (PID 8540, static domain → `127.0.0.1:57455`) was
reused, not restarted. Its traffic policy forwards only
`/api/v1/webhooks/*`. The card was entered by the user on Paymob's hosted
Test checkout. Integrations: card 5885262, MOTO 5934829.

| # | Scenario | Result |
|---|---|---|
| 1 | Signup | **PASS**: Starter `active`, `trial_days` 0, no trial end |
| 2 | Initial payment, Pro 99 EGP | **PASS**: intention created with mode `test`, order id stored separately. Txn **541400887** on integration 5885262 settled by the signed callback: checkout invoice paid 99/99, workspace Starter → **Pro**, period started at settlement |
| 3 | TOKEN callback / saved card | **PASS**: correlated on the Paymob order id; card saved encrypted (`v1.`), masked `…2346`, MasterCard, default (the audit saw `card_token_unmatched`) |
| 4 | MOTO / MIT renewal | **PASS**: MOTO intention on 5934829 carried the owner's e-mail (the audit saw 400 "Enter a valid email address"); charged 99 EGP; signed callback settled the renewal; attempt `settled` |
| 5 | Rerun sweep (double billing) | **PASS**: no new invoice, no new charge |
| 6 | Abandoned checkout safety | **PASS**: an open 299 EGP Business checkout stayed `open`/unpaid through two renewal sweeps; each renewal charged 99 for Pro only |
| 7 | Concurrent renewal | **PASS**: two workers raced on the due subscription: one advanced and invoiced, the other charged; **one** renewal invoice, **one** 99 EGP MIT |
| 8 | Lost callback recovery | **PASS**: API stopped before workspace B paid; Paymob's callback got 502 (ngrok inspector). The first reconciler pass got 404 from inquiry because Paymob had not yet exposed the transaction, and correctly left the payment pending. The second pass found txn **541401996** by merchant order id, settled it through the normal path (invoice paid, B → Pro), and raised `recovered_by_reconciliation` (resolved) |
| 9 | Card Token Inquiry (real) | **PASS**: order with a saved card → the adapter recovers `…2346`; order without → Paymob 404 "No card tokens found", now read as *no card* (`f1a1567`) |
| 10 | Duplicate delivery | **PASS**: the real signed checkout callback replayed through the ngrok agent → `200`, outcome `duplicate`, no new event row, no change |
| 11 | Decline → retry on the same order | **NOT REPRODUCIBLE** on Paymob Test (as in the audit: Test does not decline a wrong CVV). Proven with signed callbacks through the real adapter in `test_a_success_after_a_decline_on_the_same_page_settles_once` |
| 12 | Card token never logged | **PASS**: the real token, decrypted in memory, occurs **0** times in ~30 KB of API and worker logs |

Invariants on the E2E database after all scenarios:

| Invariant | Violations |
|---|---|
| I04 one transaction → one payment | 0 |
| I05 one event → one row | 0 |
| I06 amount_paid ≤ amount_due | 0 |
| I08 plaintext tokens | 0 |
| I09 >1 default card | 0 |
| I10 >1 unresolved attempt per invoice | 0 |
| I12 paid purchase not granted | 0 |
| I15 succeeded payment on unpaid invoice | 0 |
| I16 non-EGP or non-positive payment | 0 |

Totals: 4 real Test payments, 396 EGP, and 1 incident, which was the expected
recovery.

Scenarios 4 to 8 moved `current_period_end` into the past in the throwaway
E2E database to make a renewal due now. Because of that time shift, the first
renewal's period overlaps the checkout's paid period on the calendar. That is
an artefact of the shift, not of billing. The real-time property (a purchase
followed by N renewals bills N+1 adjacent periods) is proven by
`test_every_paid_period_is_billed_exactly_once`.

**Cleanup.** The E2E API and worker containers were removed. Scratch files
holding secrets, tokens and checkout URLs were deleted. The ngrok agent was
left exactly as found. No Paymob dashboard change needed restoring.

---

## 9. Score (audit weights)

| Dimension | Weight | Audit | Now | Weighted |
|---|---|---|---|---|
| Plan / catalogue correctness | 0.06 | 40 | 85 | 5.10 |
| Entitlement enforcement | 0.08 | 70 | 88 | 7.04 |
| Pricing integrity | 0.08 | 45 | 85 | 6.80 |
| Subscription lifecycle | 0.10 | 35 | 82 | 8.20 |
| Invoice correctness | 0.07 | 45 | 85 | 5.95 |
| Payment correctness | 0.09 | 50 | 85 | 7.65 |
| Paymob integration | 0.09 | 40 | 85 | 7.65 |
| Saved-card security | 0.06 | 90 | 92 | 5.52 |
| Renewal / MIT correctness | 0.07 | 45 | 85 | 5.95 |
| Idempotency / concurrency | 0.07 | 80 | 90 | 6.30 |
| Tenant isolation | 0.06 | 95 | 95 | 5.70 |
| Failure recovery | 0.05 | 65 | 85 | 4.25 |
| Reconciliation | 0.03 | 50 | 85 | 2.55 |
| Auditability | 0.03 | 55 | 88 | 2.64 |
| Testing | 0.03 | 60 | 85 | 2.55 |
| Operational readiness | 0.03 | 35 | 78 | 2.34 |
| **Total** | **1.00** | **≈ 55** | | **≈ 86** |

Why no dimension scores higher:
* Nothing was verified against Live.
* Decline→retry could not be reproduced for real.
* There are no credit notes, proration or tax.
* Trials are unsupported for every plan, a deliberate product rule.
* Recovery of lost hosted payments depends on `PAYMOB_API_KEY` being set.
* Operator tooling is API-only; there is no UI.

---

## 10. Documentation

* `DECISIONS.md` — **ADR-112**.
* `docs/BILLING.md` — new "The commercial rules" section, the platform control
  plane, metrics and alerts, and corrections to superseded text: trials,
  arrears billing, invoice uniqueness, callback matching, owner refunds.
* `docs/BILLING_OPERATIONS.md` — the operator guide: catalogue, subscribers,
  invoices, refunds, reconciliation, incidents, alerts, configuration.
* `docs/API.md` — the platform billing endpoints, the scheduled-change route,
  the refund request, and 168 operations.
* `.env.example` and `docker-compose.prod.yml` — `PAYMOB_CALLBACK_INTEGRATION_IDS`
  and `BILLING_HOSTED_RECONCILIATION_MAX_AGE_SECONDS`.
* `README.md` — migrations `0001`–`0071`.

---

## 11. Remaining risk

1. **Live deployment verification** (audit §35) is not attempted and remains
   its own audit: Live keys, the Live MOTO integration, Live callback
   integration ids, `is_live` binding, and TLS/ingress for the callback path.
2. **Hosted recovery needs `PAYMOB_API_KEY`.** Without it, lost callbacks wait
   for an operator. `BillingHostedPaymentStuck` alerts on this.
3. **Credit notes, proration and tax are not built** (BILL-21).
4. **Legacy rows that violate the new CHECKs** leave that one constraint
   `NOT VALID`, with a warning at migration time. Check the migration log on
   production data.
5. **Checkout invoices accumulate one per attempt.** Abandoned ones stay
   `open` without `issued_at` and are never dunned or charged.

Out of scope, as the brief required: no merge, no push, and no Database,
Observability or Deployment audit started.
