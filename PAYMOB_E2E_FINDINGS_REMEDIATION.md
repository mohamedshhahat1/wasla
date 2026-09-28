# Wasla Paymob E2E Findings Remediation

Date: 2026-09-27 (UTC). Remediates the three findings of
`PAYMOB_BROWSER_E2E_VERIFICATION.md` and re-verifies them against **real Paymob
Test** in Chrome through ngrok. Paymob Live was never used; nothing was merged,
pushed or deployed; no production database was touched.

Only safe identifiers appear here: Wasla UUID prefixes, Paymob order and
transaction ids, integration ids, amounts, masked last-4, HTTP statuses.

## 1. Executive summary

| Finding | Root cause | Fix | Real Paymob Test | Status |
|---|---|---|---|---|
| **PAY-E2E-01** second partial refund dropped as duplicate | refund callback identity was `"<parent>:refunded"`; Paymob reports every refund on the same parent with a *cumulative* `refunded_amount_cents` | identity `"<parent>:refunded:<cumulative cents>"`; the provider figure is treated as a running total, only the delta is applied, a late smaller total is stale; lost refund callbacks are confirmed from the provider's own record | RF01 30 + 69 on one parent: **PASS**; RF02 20/30/49: **PASS**; RF03 replays: **PASS** | **CLOSED** |
| **PAY-E2E-03** inquiry reads a refund child as a collection | Transaction Inquiry returns the latest transaction of the order (after a refund, the child `is_refund: true, success: true`); the adapter checked only parent flags | refund/void children are never collections; an inquiry child is bound to its parent (read by id, same order, must show the reversal); a reversal for a payment never recorded as collected is recorded as held + reversed with an incident, nothing settled | RF04 inquiry after refund: **PASS**; RF05 lost callback + provider refund before reconciliation: **PASS** (pre-fix code shown to read it as `succeeded 99`) | **CLOSED** |
| **PAY-E2E-02** misleading `purchase_settled` system audit on a complimentary assignment | shared grant helper always wrote `actor_kind=system, reason=purchase_settled` | contextual `PlanGrant`: operator, basis (`platform_grant` / `complimentary_grant`), reason, version, `payment: null`; paid purchases unchanged | RF06: 0 Paymob, 0 invoice, 0 payment, 0 `purchase_settled`: **PASS** | **CLOSED** |

No schema migration was needed (Alembic stays `0080`, single head). 16
mutants applied, 16 killed, 0 survivors.

## 2. Starting state

| Item | Value |
|---|---|
| Worktree / branch | `E:\wasla-database-remediation` / `database-findings-remediation` (continued; no child branch was needed) |
| PAYMOB_REMEDIATION_BASE_HEAD | `ab00033` (Paymob report) → parent `91ba783` (database report) → code `e15965f`, as expected |
| Tracked tree | clean |
| Alembic | `0080 (head)`, single |
| Canonical `worktree-billing-google-auth` | not touched |

## 3. PAY-E2E-01 baseline

Reproduced from preserved real evidence before any code changed
(`wasla_paymob_browser_e2e`, payment `ab29fd99…`, txn 542754263):

```
payment ab29fd99 | succeeded | amount 99.00 | refunded 30.00 | refund_requested_amount 99.00
invoice          | paid      | amount_paid 69.00
payment_events:  542754263:succeeded  applied
                 542754263:refunded   applied  "Refunded 30.00."
                 (no row for the second refund: the claim failed as a duplicate)
```

Paymob had refunded 99.00 (verified again by a read-only
`GET /api/acceptance/transactions/542754263` → `is_refunded: true,
refunded_amount_cents: 9900`). The permanent tests were rewritten to the real
callback shape (same parent id, cumulative total); mutant PM-E2E-M01 (the old
identity) fails them.

## 4. PAY-E2E-01 fix

`a5f8367 fix(paymob): handle cumulative partial refund callbacks`

* `PaymobProvider._event`: a reversal reported **on the collecting
  transaction** is keyed `"<txn>:refunded:<refunded_amount_cents>"` (a void on
  its whole amount). A replay of one cumulative state is a duplicate of itself;
  a larger total is a new event. Collection/pending/failure ids are unchanged.
* `CheckoutService._apply_reversal` (the single reversal primitive):
  `delta = provider_cumulative − local_refunded`; `delta = 0` → replay (no
  change); `delta < 0` → **stale provider callback**, the higher total stands
  (logged `billing.refund_state_stale`; the database trigger from 0077 refuses
  `refunded_amount` going down in any case).
* A reversal of a transaction other than the one the payment recorded (a second
  collection on the same order, BILL-15) is refused as mismatched, so it can
  never take money off the invoice the first collection funded.
* Fixtures that modelled each refund as its own transaction id (`1001`/`1002`,
  `SECOND_TRANSACTION`) — the reason the old suites passed — now use the same
  parent and a cumulative total.

`b92079d fix(billing): confirm lost refund callbacks from the provider's record`

* A refund whose confirming callback is lost used to leave
  `refund_requested_amount` outstanding for ever (the stuck request of
  PAY-E2E-01). The billing worker's reconciliation phase now also claims
  collected payments whose refund request has stood past the grace period
  (same `reconciled_at` lease), reads the payment's own transaction by id and
  applies the provider's running total through the same reversal primitive.
  Bounded to those rows; nothing else is polled; nothing moves money.
* The operator's "reconcile this payment" does the same for a collected
  payment instead of answering `nothing_to_do`.

## 5. Multiple partial refund semantics

| Case | Behaviour | Proven by |
|---|---|---|
| 30 → 99 on one parent | applies 30, then 69 | R1/R2, **real RF01** |
| 99 → 30 (late) | 99 stands; 30 = stale, no change | R5 |
| 30 → 30, 99 → 99 | duplicate | R3/R4, **real RF03** |
| 20 → 50 → 99 | 20, 30, 49 each once | R6, **real RF02** |
| operator request reaches its target | `refund_requested_amount` cleared; next request allowed | R8, RF01, RF02 |
| cumulative reaches the applied amount | ADR-096 exactly: payment `refunded`; invoice `amount_paid 0`; operator-requested → **void** ("Refunded in full at the platform's decision"), unrequested → reopened; plan withdrawn to the default if nothing else covers it | R9, R6, **RF01/RF02** (void, Pro → Starter) |
| held duplicate refunded in parts | held payment refunded; **invoice `amount_paid` and the applied payment untouched** | R7 |
| another workspace | cannot match the callback (unmatched) nor reach the refund route (404) | R10 |

No new refund product semantics were introduced.

## 6. PAY-E2E-03 baseline

Before code changed, the unchanged adapter was run read-only against real Paymob
for the refunded evidence order:

```
ab29fd99 verdict answered | kind succeeded | txn 542755547 | parent 542754263 | amount 69 | event_id 542755547:succeeded
```

Raw safe fields of that real inquiry answer: `id 542755547, success true,
is_refund true, is_refunded false, refunded_amount_cents 0, amount_cents 6900,
has_parent_transaction true, parent_transaction 542754263, order 619143122,
is_live false`. The parent read by id: `is_refunded true,
refunded_amount_cents 9900`.

## 7. Inquiry/refund-child fix

`6fb83de fix(paymob): classify refund children correctly during inquiry`

* `_event` reads `is_void` / `is_refund` first, then the parent flags
  `is_voided` / `is_refunded`, then **any transaction with a parent** (this
  integration never authorises and captures apart, so no collection of ours
  has one) — all before any success flag. A child is `reversal_child=True`,
  never `SUCCEEDED`, and carries no running total (its own `0` is ignored).
* `inquire_charge`: a child answer is bound to its **parent**: the parent is
  read with `GET /api/acceptance/transactions/{id}` (inquiry bearer token;
  verified on the real Test API), must be on the same order and must itself
  show the reversal. Disagreement → `PENDING` (ask again); unreadable or foreign
  parent → `UNREACHABLE`. Never a collection.
* The parent's event has the **same identity as its refund callback**, so a
  callback and reconciliation racing each other apply it once — one
  authoritative reversal path for callback, inquiry and transaction read.
* A child arriving as a callback is recorded and moves nothing.
* `CheckoutService._reversed_before_settlement`: a reversal for a payment still
  `pending` (collection callback lost, refunded before reconciliation) records
  the collection facts (`record_provider_outcome`), the reversal and a
  `refused_settlement` incident — open while any money is held, resolved when
  all of it went back. `applied_at` stays NULL: **no invoice paid, no plan,
  custom offer or top-up granted.** The attempt stops being pending; a late
  success callback cannot settle it.
* Hosted reconciliation reports that case as `reversed`; a held collection is
  no longer counted as settled.

`PaymentReconciler.compare_refund_state` (in `b92079d`) is the
provider-vs-ledger helper (§13).

## 8. PAY-E2E-02 audit fix

`2b9a618 fix(billing): distinguish complimentary grants in audit trail`

`SubscriptionService.apply_purchase(grant=PlanGrant(...))`. The platform's
zero-price assignment passes basis `platform_grant`, a complimentary grant of a
priced version `complimentary_grant`; both record `actor_kind=platform_staff`,
the operator's id, `operator_reason`, `plan_version_id` and `payment: null`.
Without a grant (a settled purchase) the row is unchanged:
`system` / `purchase_settled`. No new enum or audit action.

## 9. Permanent tests

| Suite | Tests | Covers |
|---|---|---|
| `tests/unit/test_paymob_refund_semantics.py` (new) | 21 | real payload shapes; cumulative ids; replay/void identity; I1–I6; child flags each sufficient; parent disagreement/unreadable/foreign; ordinary inquiry untouched; transaction-read guards |
| `tests/integration/test_paymob_refund_inquiry_findings.py` (new) | 17 | R1–R10 (R1/R2/R8/R9 one test on the exact 30+69 operator sequence); same-order other transaction; child as callback; I1; I7 plan full + partial, top-up, custom offer; I8 via the worker sweep; I6 via operator reconcile; provider-vs-ledger; paid purchase still `purchase_settled` |
| `tests/integration/test_commercial_api.py` (+2) | 2 | zero-price assignment and complimentary grant through the real platform route: 0 intentions, 0 invoices, 0 payments, operator on every row, no `purchase_settled` |
| corrected fixtures | `test_paymob_refunds.py`, `test_refund_entitlements.py`, `test_paymob_hmac.py` | same parent + cumulative total; the new identity |

## 10. Mutation campaign

Run in an isolated detached worktree (never the real tree), each mutant one
exact-text edit asserted to match once, against the six refund/audit suites.

| Mutant | Mutation | Result | Killed by |
|---|---|---|---|
| PM-E2E-M01 | refund event id ignores cumulative amount | KILLED | `test_each_running_total_of_one_parent_is_its_own_event` |
| PM-E2E-M02 | second cumulative refund treated as duplicate | KILLED | `test_r1_r2_r8_r9_…` |
| PM-E2E-M03 | callback cumulative used as the delta | KILLED | `test_r1_r2_r8_r9_…` |
| PM-E2E-M04 | late smaller refund reduces the local total | KILLED | `test_r5_…` |
| PM-E2E-M05 | `is_refund` ignored | KILLED (round 2) | `test_the_refund_flag_alone_is_enough_to_refuse_a_collection` |
| PM-E2E-M06 | refund child classified succeeded | KILLED | `test_i3_…` |
| PM-E2E-M07a | child answered as-is (no parent binding) | KILLED | `test_i5_…` |
| PM-E2E-M07b | parent's order not bound to the child's | KILLED | `test_a_parent_on_another_order_is_not_believed` |
| PM-E2E-M08 | lost callback + refunded inquiry settles/activates | KILLED | `test_i7_…[9900]` |
| PM-E2E-M09 | complimentary grant logs `purchase_settled` | KILLED | `test_an_assignment_without_payment_…[platform_grant]` |
| PM-E2E-M10 | paid purchase no longer logs `purchase_settled` | KILLED | `test_a_paid_purchase_still_records_purchase_settled_…` |
| PM-E2E-M11 | reversal of another transaction on the order accepted | KILLED | `test_a_refund_of_another_transaction_on_the_same_order_…` |
| PM-E2E-M12 | refund-confirmation sweep not run by the worker | KILLED (round 2) | `test_i8_…` |
| PM-E2E-M13 | refund child moves money on a callback | KILLED | `test_a_refund_child_arriving_as_a_callback_…` |
| PM-E2E-M14 | parent not showing the refund read as its collection | KILLED | `test_a_child_whose_parent_does_not_show_the_refund_yet_…` |
| PM-E2E-M15 | money still held marked resolved | KILLED | `test_i7_…[3000]` |

Round 1: 14 killed, 2 survived. **M05** survived because the parent-presence
rule independently classifies the real child shape; closed by
`0e749ff test(paymob): require the refund flag alone to refuse a collection`.
**M12** as first written (`refunds = {} or await …`) still called the sweep —
an invalid mutant, not a gap; re-formulated (`{} if True else …`) it is killed.
**Applied 16, killed 16, equivalent 0, survived 0, meaningful survivors 0.**

## 11. Real Paymob Test re-verification

Environment: API `uvicorn app.main:app` from this worktree (asserted
`app.__file__` = `E:\wasla-database-remediation\app\__init__.py` in every
worker/script run), `ENVIRONMENT=local`, `BILLING_PROVIDER=paymob`, fresh DB
**`wasla_paymob_refund_fix_e2e`** (migrated 0001→0080 by this code) in the
throwaway Compose container `wasla-paymob-browser-e2e`, Redis DB 6, the real
`BillingWorker.run_once()` for sweeps. ngrok
`recycler-gondola-numeral.ngrok-free.dev → 127.0.0.1:57455`: local and public
`/health/ready` 200, unsigned public callback **403**. Credentials loaded by
name from `E:\secrets.txt` into the child process only; the launcher refuses
anything but `sk_test_`/`pk_test_`. Hosted pages were
`eg.checkout.paymob.com/?publicKey=egy_pk_test_…`, opened in Chrome via a
localhost redirector and paid with Paymob's published Test Mastercard …2346;
every callback `is_live: false`, every payment `provider_mode = test`.

| ID | Scenario | Result |
|---|---|---|
| RF01 | A: Pro 99.00 → operator refund 30.00 → operator refund 69.00, same parent | **PASS — REAL.** txn 542930924 (order 619348330) applied by signed callback. Refund 30 (refund txn 542932312) → callback `refunded 3000` → `542930924:refunded:3000` applied: refunded 30, invoice **paid 69**, request cleared, still Pro. Refund 69 (refund txn 542932705) → callback **same parent** `refunded 9900` → `542930924:refunded:9900` **applied** ("Refunded 69.00") — not duplicate (0 `callback_duplicate` lines). Payment `refunded` 99.00, invoice **void** ("Refunded in full at the platform's decision"), plan withdrawn Pro → Starter, `refund_requested_amount` NULL. |
| RF02 | B: Pro 99.00 → 20 / 30 / 49 | **PASS — REAL.** txn 542934118 (order 619352472); refund txns 542934572, 542934839, 542935135; callbacks `2000`, `5000`, `9900` on the same parent → three events, each applied once (20, 30, 49); invoice void; Pro → Starter. |
| RF03 | Replay genuine refund callbacks | **PASS — REAL.** ngrok byte-for-byte replay of the `9900` and `3000` callbacks → 200 `duplicate` ×2; no state change. |
| RF04 | Inquiry after refund | **PASS — REAL.** Real inquiry of RF01's order: latest txn **542932705, `is_refund true`, parent 542930924**, 6900, order 619348330, `is_live false`. Fixed adapter → `refunded`, txn 542930924 (parent), cumulative 99, id `542930924:refunded:9900`; through `CheckoutService.apply` → `duplicate`. Old evidence order `ab29fd99` now reads `refunded 99` (was `succeeded 69`). |
| RF05 | Lost callback, then provider refund, then reconciliation | **PASS — REAL.** D checkout (order 619354494); API stopped; paid in Chrome (txn 542936054); Paymob callback → **502** (17:32:14+03). Before any reconciliation, a provider-side Test refund of 99.00 through Paymob's documented refund API (refund txn 542936652; the refund callback also 502, 17:32:50). Inquiry now answered with the child 542936652 (`is_refund`, **9900 = the invoice**). **The pre-fix adapter (`e15965f`, read-only) reads it as `succeeded 99`.** API restored; real sweep: event `542936054:refunded:9900` → `refused`; payment `refunded` 99 held (`applied false`), **invoice open, amount_paid 0.00**, **D stays on Starter**, `refused_settlement` incident (resolved, 0.00 held); second sweep handled 0. False invoice settlement: **0**. |
| RF06 | Zero-price custom plan assignment (G) | **PASS — REAL route.** 201 `assigned`; 0 Paymob intentions, 0 payments, 0 invoices; rows `billing_custom_plan_created`, `subscription_plan_changed` ×2, `billing_custom_plan_assigned`, all `platform_staff` by the operator; the grant row `reason platform_grant`, `operator_reason`, `plan_version_id`; **`purchase_settled` rows for G: 0**. |
| Reg. | Normal hosted checkout (E) | **PASS — REAL.** txn 542938034 applied by signed callback; E on Pro. |
| Reg. | Lost callback, no refund (F) | **PASS — REAL.** txn 542938878; callback 502 (17:36:39+03); sweep inquiry → settled once, `recovered_by_reconciliation`; F on Pro. |
| Reg. | Held duplicate refund | **Deterministic (R7).** The provider behaviour was proven with real money in P44/P60; the local rule (invoice `amount_paid` unchanged, applied payment untouched, held payment refunded — now in two partials) is pinned by R7. |

Paymob Test transactions this run: **5 collections (495.00 EGP)**, **6 refund
transactions (297.00 EGP)**, 0 declines, 0 MOTO. Live: **0**. Paymob sent
refund notifications only in the parent shape (`is_refund false`, cumulative
`refunded_amount_cents`); no child callback was delivered.

## 12. Refund transaction ledger

All EGP, integration 5885262, `provider_mode test`, `is_live false`.

| # | Scenario | WS | Payment | Order | Parent txn | Refund txn | Refund | Cumulative | Callback | Wasla event | Outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | RF01 collect | A | 1324d52e | 619348330 | 542930924 | — | — | — | 200 | `…:succeeded` | applied |
| 2 | RF01 refund #1 | A | 1324d52e | 619348330 | 542930924 | 542932312 | 30.00 | 3000 | 200 | `…:refunded:3000` | applied 30 |
| 3 | RF01 refund #2 | A | 1324d52e | 619348330 | 542930924 | 542932705 | 69.00 | 9900 | 200 | `…:refunded:9900` | **applied 69** |
| 4 | RF03 replays | A | 1324d52e | 619348330 | 542930924 | — | — | 9900, 3000 | 200 ×2 | same ids | duplicate ×2 |
| 5 | RF02 collect | B | f4130c46 | 619352472 | 542934118 | — | — | — | 200 | `…:succeeded` | applied |
| 6 | RF02 refund #1 | B | f4130c46 | 619352472 | 542934118 | 542934572 | 20.00 | 2000 | 200 | `…:refunded:2000` | applied 20 |
| 7 | RF02 refund #2 | B | f4130c46 | 619352472 | 542934118 | 542934839 | 30.00 | 5000 | 200 | `…:refunded:5000` | applied 30 |
| 8 | RF02 refund #3 | B | f4130c46 | 619352472 | 542934118 | 542935135 | 49.00 | 9900 | 200 | `…:refunded:9900` | applied 49 |
| 9 | RF05 collect | D | 9d243535 | 619354494 | 542936054 | — | — | — | **502** | — | — |
| 10 | RF05 provider refund | D | 9d243535 | 619354494 | 542936054 | 542936652 | 99.00 | 9900 | **502** | via inquiry `…:refunded:9900` | refused; held+reversed; nothing settled |
| 11 | Reg. normal | E | e34fe2cc | 619357124 | 542938034 | — | — | — | 200 | `…:succeeded` | applied |
| 12 | Reg. lost callback | F | bb11fa4c | 619358032 | 542938878 | — | — | — | **502** | via inquiry `…:succeeded` | applied, recovered |

## 13. Provider-vs-ledger reconciliation

`PaymentReconciler.compare_refund_state(payment_id)` reads the payment's own
transaction (`GET /api/acceptance/transactions/{id}`) and compares the
provider's cumulative refunded amount with `payments.refunded_amount`. It never
writes and is never scheduled; the only automatic provider reads are the
bounded refund-confirmation claims of §4.

| Database | Paymob payments checked | Match | Mismatch | Unknown |
|---|---|---|---|---|
| `wasla_paymob_refund_fix_e2e` (this run) | 5 | **5** (99/99, 99/99, 99/99, 0/0, 0/0) | **0** | 0 |
| `wasla_paymob_browser_e2e` (preserved, pre-fix, read-only) | 27 | 26 | **1: `ab29fd99` Paymob 99 vs Wasla 30.00** (PAY-E2E-01's residue) | 0 |

The helper finds exactly what the 20-row internal ledger could not. The
evidence database was not repaired (kept for review).

## 14. Financial invariants

Independent SQL on `wasla_paymob_refund_fix_e2e` after the run: the report's
L01–L20 plus four new ones.

| # | Invariant | Violations |
|---|---|---|
| L01–L20 | as in `PAYMOB_BROWSER_E2E_VERIFICATION.md` §18 (over-collection, amount_paid = applied net, held money has an incident, identity uniqueness, tenant bindings, test mode, integrations, …) | 0 |
| L21 | fulfilled refund request still outstanding | 0 |
| L22 | fully refunded applied payment whose invoice still counts as paid | 0 |
| L23 | invoice paid by money reversed before settlement | 0 |
| L24 | `refunded` payment with `refunded_amount < amount` | 0 |
| **Total (24 checks)** | | **0** |
| Provider-vs-ledger refund mismatches | | **0** |
| Stuck fulfilled refund requests | | **0** |

Money summary (Test): collected 495.00 (5), applied 396.00, held 99.00 (RF05,
fully reversed), refunded per Wasla 297.00 = refunded per Paymob 297.00.

## 15. Security/leak scan

27 secret values (Paymob secret, HMAC secret, API key, every other credential in
the secrets file, generated JWT/encryption/fingerprint keys, Postgres password,
ngrok auth token, the Test PANs) plus patterns (client secret, `"cvv"`,
`auth_token`/`payment_token` values, Bearer JWTs) over the API log, sweep log,
ngrok log, every scratch output, the mutation logs and the DB text of audit
metadata, incidents, payment events, payment failure reasons and invoices:
**0 hits**. No card was saved in this run (0 reusable tokens exist); no PAN or
CVV reaches Wasla. `E:\secrets.txt` was read by key name only, never printed,
modified, copied or committed. This report and the git diff were scanned before
commit (§16).

## 16. Static and test gates

| Gate | Result |
|---|---|
| `ruff check .` | All checks passed |
| `black --check .` | 758 files unchanged |
| `mypy app tests` | no issues in 672 source files |
| `alembic heads` / `current` / `check` | `0080 (head)` single / `0080` / no new operations |
| `python -m scripts.db_preflight verify` | ok |
| Targeted Paymob/billing (25 integration + 13 unit modules, list in §2 of the source report plus `test_billing_remediation_journeys`, `test_billing_worker`, `test_billing_incidents` and the two new suites) | **668 passed, 0 failed, 0 errors, 0 skipped** (at `0e749ff`) |
| Model-built, whole `tests/` | 6,013 collected: 5,915 passed, **9 failed**, 0 errors, 89 skipped (26 m 40 s). All 9 failures are media/WhatsApp fetch unit tests (`test_whatsapp_client` ×7, `test_media_fetch_boundary` ×1, `test_outbound_url_safety` ×1) raising "the host could not be resolved": a transient DNS outage on this machine during the run. Re-run in isolation at `0e749ff`: **38/38 passed** (and 38/38 at base `ab00033`); none touches billing code. |
| Migration-built, `tests/integration tests/e2e` (CI's two deselects) | 2,942 collected (+2 deselected): 2,870 passed, **1 failed**, 0 errors, 71 skipped (25 m 16 s). The failure is the wall-clock bound in `test_media_parser_containment::test_an_amplifying_pdf_is_killed_on_time…` (1.116 s vs 1.0 s) while both lanes shared the machine; the file re-run alone on the same migration-built DB: **9/9 passed**. Not billing code. |

Every intermediate commit was verified in an isolated worktree (ruff, black,
mypy and the suites that commit touches) before it was made.

## 17. Remaining gaps

* **Whole-suite runs were not clean on first pass.** 10 non-billing failures (DNS outage, one timing bound under parallel load) were each re-run and passed; no single uninterrupted all-green whole-suite run exists for `0e749ff`.
* **Partial refund *before* reconciliation (real).** RF05 exercised the full
  refund; the partial variant (money still held, incident open) is proven
  deterministically (`test_i7_…[3000]`), not with real money.
* **Real child callback.** Paymob delivered only parent-shaped refund
  callbacks in both runs. The child-as-callback rule is pinned by tests with
  the recorded child shape; it has not been observed as a webhook.
* **Held duplicate refund** re-proven deterministically only (R7); the real
  money path is the earlier P60.
* **Refund-confirmation claim query** filters `status = succeeded` and
  `refund_requested_amount IS NOT NULL` with no dedicated partial index
  (deliberately no migration). Adequate while outstanding requests are rare;
  a partial index is a future optimisation.
* **Provider-side refund of an unknown collection** was issued through Paymob's
  documented refund API with the Test secret key (Wasla's platform route
  correctly refuses a payment it never recorded as collected); it stands in
  for a dashboard refund.

## 18. Commit ledger

| Commit | Message |
|---|---|
| `a5f8367` | fix(paymob): handle cumulative partial refund callbacks |
| `6fb83de` | fix(paymob): classify refund children correctly during inquiry |
| `b92079d` | fix(billing): confirm lost refund callbacks from the provider's record |
| `2b9a618` | fix(billing): distinguish complimentary grants in audit trail |
| `6ac67a7` | test(paymob): pin real refund callback and inquiry semantics |
| `0e749ff` | test(paymob): require the refund flag alone to refuse a collection |
| (this report) | docs(billing): record Paymob E2E findings remediation |

Not merged, not pushed, not deployed.

## 19. Final verdict

| Finding | Status |
|---|---|
| PAY-E2E-01 | **CLOSED** |
| PAY-E2E-03 | **CLOSED** |
| PAY-E2E-02 | **CLOSED** |

Every mandatory real scenario (RF01–RF06 and both regressions) passed against
real Paymob Test; the gaps in §17 are covered by deterministic tests and none
blocked a required scenario.

**PAYMOB E2E FINDINGS CLOSED**
