# Wasla Annual Billing + Database/Paymob Merge Verification

Final integration gate for three bodies of work: the database findings
remediation (ADR-115), the Paymob E2E findings remediation (PAY-E2E-01/02/03)
and monthly + annual plan pricing (ADR-116). Evidence of implementation is in
[DATABASE_FINDINGS_REMEDIATION.md](DATABASE_FINDINGS_REMEDIATION.md),
[PAYMOB_E2E_FINDINGS_REMEDIATION.md](PAYMOB_E2E_FINDINGS_REMEDIATION.md) and
[ANNUAL_BILLING_IMPLEMENTATION.md](ANNUAL_BILLING_IMPLEMENTATION.md); this
record is the independent re-verification, the two merges and the push.
Nothing was deployed, no production database was touched, Paymob Live was
never used, and no push was forced.

## 1. Executive summary

The annual branch was re-verified from scratch in an isolated worktree, with
fresh databases and none of the implementation's own results reused: static
gates, one uninterrupted whole model-built suite (6,091 passed, 0 failed), one
uninterrupted migration-built integration + e2e lane (2,991 passed, 0 failed,
0 skipped), the kept-data sweep, the targeted billing and database families,
the calendar and entitlement oracles, the 19-rule ledger, real-PostgreSQL
races, a 20-mutant campaign (20 killed) and the migration matrix, including a
populated `0080 -> 0081` upgrade of a copy of real Paymob Test evidence.

It was then merged in the mandated order - `database-findings-remediation`
first, `annual-billing` second - as two normal merge commits with no
conflicts. Each merged tree is byte-identical to the branch verified before
it, and every gate was run again on the final tree with the same results.

## 2. Source branches and exact heads

| | |
|---|---|
| Canonical branch | `worktree-billing-google-auth` (tracks `origin/worktree-billing-google-auth`) |
| CANONICAL_START_HEAD | `11cf44b9db405cbdc1f6019e650c26d17d78e448` |
| ORIGIN_CANONICAL_START_HEAD | `11cf44b9db405cbdc1f6019e650c26d17d78e448` |
| DATABASE_REMEDIATION_HEAD | `4654a79e3b7d72b176e4bea27ce247fe6ee38593` (`database-findings-remediation`) |
| ANNUAL_BILLING_HEAD | `68e9afbbc42f418a738da06895fda986bd30c1e4` (`annual-billing`) |
| Alembic before / after | `0080` / `0081`, single head |

Ancestry: `merge-base(canonical, database-findings-remediation) = 11cf44b`,
`merge-base(database-findings-remediation, annual-billing) = 4654a79`, so
canonical ⊂ remediation ⊂ annual. Both source worktrees had an empty
`git status --short`. The nine untracked audit/plan documents in `E:\wasla`
were left untouched; none of their names exists in the merged tree.
`origin` was re-fetched before merging and before pushing; it had not moved.

## 3. Independent annual verification

Worktree `E:\wasla-annual-verify`, detached at `68e9afb`, never edited. Every
run asserted `app.__file__ = E:\wasla-annual-verify\app\__init__.py` (the
editable install points at `E:\wasla`; `PYTHONPATH` plus `python -m` overrode
it). Dedicated containers: pgvector/pg16, Redis 7 and the CI MinIO mirror
(so the object-store suites ran instead of skipping), one fresh database per
lane.

| Gate | Result |
|---|---|
| `ruff check .` | All checks passed |
| `black --check .` | 768 files unchanged |
| `mypy app tests` | no issues in 680 source files |
| `alembic heads` | `0081 (head)`, single; 81 revisions |
| `alembic check` | No new upgrade operations detected |
| `python -m scripts.db_preflight verify` | ok |

## 4. Clean whole-suite evidence

| Lane | Tree | Collected | Passed | Failed | Errors | Skipped | Deselected | Duration |
|---|---|---|---|---|---|---|---|---|
| Model-built, whole `tests/` | annual `68e9afb` | 6,109 | **6,091** | **0** | 0 | 16 | 2 | 27 m 59 s |
| Migration-built, `tests/integration tests/e2e` | annual `68e9afb` | 2,993 | **2,991** | **0** | 0 | **0** | 2 | 26 m 12 s |
| Model-built, whole `tests/` | merged `7eb7a74` | 6,109 | **6,091** | **0** | 0 | 16 | 2 | 27 m 52 s |
| Migration-built, `tests/integration tests/e2e` | merged `7eb7a74` | 2,993 | **2,991** | **0** | 0 | **0** | 2 | 26 m 4 s |

Each was one uninterrupted run, sequential, with no other heavy lane running
and no source file touched. Deselections are CI's two
`test_a_kept_run_really_had_something_to_sweep` checks, which run in the
kept-data sweep. The migration lanes ran `test_schema_parity.py` first on its
own (4 passed, not skipped), as CI does.

Model-built skips, all on CI's allow-list or provisioned there: 11 real-provider
(`OPENAI_API_KEY` absent), 4 schema parity (runs in the migration lane), 1
`WASLA_TEST_TMPFS` (a Windows host cannot mount the 2 MB tmpfs CI mounts).

Stated for completeness: the first migration-built run on the annual tree
exited 0 (no failure) but was launched with `-q` on top of the configured
`-q`, which suppressed the summary and skip report. Its counts could not be
audited, so it was discarded and rerun on a fresh database; the rerun above is
the evidence.

## 5. Database remediation merge

`a13e6963a658feb4d06a77a4e69950eee8dd1dc6` - `merge(billing): integrate
database and Paymob remediation`, made in a fresh worktree `E:\wasla-final-merge`
on branch `final-merge-annual`, created from `origin/worktree-billing-google-auth`.

| Check | Result |
|---|---|
| Conflicts | 0 |
| `git diff --exit-code database-findings-remediation HEAD` | exit 0 (tree identical) |
| `git status` | clean |
| Alembic | `0080 (head)` single; fresh upgrade, `alembic check` clean, `db_preflight` ok |
| ruff / black / mypy | pass / 758 unchanged / no issues in 672 files |
| 70 targeted files, migration-built (Paymob findings, refunds, settlement concurrency and backstop, financial integrity, checkout boundary, reconciliation, purge scale, privileges, timeouts, migration recovery, search_path, schema parity, backup scripts, the whole billing family) | **963 passed, 0 failed, 0 skipped** |

DB-001 (settlement concurrency, `test_settlement_concurrency.py`) and DB-002
(purge indexes, `test_workspace_purge_scale.py`) are **CLOSED** at this merge.

## 6. Paymob remediation preservation

| Finding | Permanent tests | At merge 1 | At merge 2 |
|---|---|---|---|
| PAY-E2E-01 cumulative partial refunds, lost refund callback recovery | `test_paymob_refund_semantics.py`, `test_paymob_refund_inquiry_findings.py`, `test_paymob_refunds.py`, `test_refund_entitlements.py` | pass | pass |
| PAY-E2E-02 complimentary grant audit | `test_commercial_api.py`, `test_paymob_refund_inquiry_findings.py` (paid purchase still `purchase_settled`) | pass | pass |
| PAY-E2E-03 refund-child inquiry | `test_paymob_refund_semantics.py`, `test_paymob_refund_inquiry_findings.py` | pass | pass |

All three remain **CLOSED**. Provider-facing code did not change after the
remediation except where ADR-116 names the price (MIT eligibility compares an
invoice with its price; checkout takes the price's amount), both covered by the
annual suites and the real Test evidence (§20).

## 7. Annual billing merge

`7eb7a740dc04ddb7dc60dc8c1fab47ca7742ef0d` - `merge(billing): integrate monthly
and annual plan pricing`, on top of `a13e696`. Conflicts: 0.

## 8. Tree equality

| Comparison | Result |
|---|---|
| `git diff --exit-code database-findings-remediation a13e696` | exit 0 |
| `git diff --exit-code annual-billing 7eb7a74` | exit 0 |
| Unexpected differences | 0 |

The canonical code tree is byte-for-byte the tree every independent gate in
§3, §4 and §10-§19 ran on; the post-merge runs were repeated anyway.

## 9. Alembic / schema state

On `7eb7a74`: `alembic heads` = `0081 (head)` single; `alembic current` after a
fresh upgrade = `0081 (head)`; `alembic check` clean; `db_preflight verify` ok;
the application factory builds.

## 10. Migration verification

| Gate | Annual tree | Merged tree |
|---|---|---|
| Fresh empty `0001 -> 0081` | PASS (7.4 s) | PASS |
| `0081 -> 0080 -> 0081` (empty) | PASS | PASS |
| `head -> base -> head` (CI) | PASS | PASS |
| Populated `0080 -> 0081`: `test_plan_price_migration.py` (real 0080 billing rows) | PASS | PASS |
| Preflight refusal leaves the DB at `0080`, no `plan_prices` | PASS | PASS |
| Downgrade refuses a subscriber on a yearly price | PASS | PASS |
| Populated real-data copy: `pg_dump` of `wasla_paymob_refund_fix_e2e` (0080, real Paymob Test history) restored into a scratch DB, upgraded to `0081` | PASS: every priced subscription/invoice pinned, every usage cycle = its term, 0 yearly prices, ledger 0; `0081 -> 0080 -> 0081` PASS, ledger 0 again | - |
| Catalog diff fresh-migrated vs upgraded-populated (`scripts.schema_catalog`) | schemas identical | - |
| Model vs migration parity (`test_schema_parity.py`, migration lane) | 4 passed | 4 passed |
| `alembic check` / `db_preflight` | clean / ok | clean / ok |

The source evidence database was only read (`pg_dump`); it was not modified.

**No annual price is invented.** A fresh database at `0081`:

| Plan | Version price | Price rows |
|---|---|---|
| starter | 0.00 | none |
| enterprise | 0.00 | none |
| pro | 99.00 | monthly 99.00 |
| business | 299.00 | monthly 299.00 |

Yearly prices: **0**.

## 11. Platform API verification

Besides the suites, an independent HTTP-level test was written for this gate
and run from a disposable worktree (never committed), against the real app
with the Paymob adapter faked at the socket:

| Operation | Result |
|---|---|
| List plans, read plan, list versions, read version, price history (`?active=`) | PASS |
| Create monthly price (duplicate active slot → 409) and yearly price (201, audited `billing_plan_price_created`) | PASS |
| Price on a free version → 422 | PASS |
| Business yearly **2,990** created; tenant A subscribes yearly | PASS |
| Retire 2,990 (retired, kept in history), create yearly **3,290** | PASS |
| `PATCH /prices/{id}` → **409** "retire and create" | PASS |
| Direct SQL `UPDATE plan_prices SET amount = 3290` on the 2,990 row → refused | PASS |
| Existing subscriber still pinned to 2,990; new customer's catalogue shows 3,290; a new checkout on 2,990 refused | PASS |
| `GET /subscriptions?billing_interval=` / `?plan_price_id=`; `GET /subscriptions/{id}` exposes price, interval, amount | PASS |
| `GET /tenants/{id}/summary` exposes price, term and usage cycle | PASS |
| Custom plan with monthly + yearly prices; annual custom offer by `plan_price_id`; ambiguous version-only offer refused; another workspace's custom price and a global price refused as offers | PASS |
| Authorization: platform owner and platform admin allowed (existing `PlatformStaffDep`); a tenant owner gets 403 on create, retire, PATCH, read and custom plan | PASS |
| Invariant ledger over every workspace of the test | 0 violations |

## 12. Customer API verification

`GET /billing/plans` (`prices[]`, `billing_required`, legacy `price` = monthly),
`POST /billing/checkout` with `plan_price_id` and with legacy `plan_code`
(monthly 99.00, one month), `GET /billing/summary` (billing term vs usage
cycle, `next_renewal_amount`), offers and acceptance: `test_annual_billing.py`,
`test_billing_endpoints.py`, `test_invoice_endpoints.py`, the independent test
above. All pass on both trees.

## 13. Annual billing semantics

| Behaviour | Proven by | Result |
|---|---|---|
| PlanPrice immutability, one active price per slot | ledger, `test_a_price_is_never_edited_the_database_refuses_it`, §11 | PASS |
| Yearly purchase = one invoice, one hosted payment, 12-month term, 1-month cycle | `test_a_yearly_checkout_charges_one_year_...` | PASS |
| Monthly unchanged | `test_a_monthly_checkout_is_unchanged_one_month_one_cycle`, §11 | PASS |
| Usage resets monthly inside the year, no invoice, no charge | `test_annual_usage_resets_monthly_...`, races | PASS |
| Annual saved-card renewal = one MOTO for the year | `test_an_annual_renewal_with_a_saved_card_...`, race | PASS |
| Annual no-card renewal = no charge, hosted invoice | `test_an_annual_renewal_without_a_card_...` | PASS |
| Upgrade/downgrade/cancel timing | matrix tests, `test_cancelling_an_annual_subscription_...` | PASS |
| Annual custom plans and offers | offer tests, §11 | PASS |
| Open page settles at its own price | `test_an_open_annual_page_settles_...`, §11 | PASS |

## 14. Top-up semantics

Usage top-ups end with the monthly usage cycle; capacity top-ups end with the
billing term and delete nothing on expiry; on monthly prices both windows are
one (`test_a_usage_top_up_on_an_annual_plan_ends_with_its_month`,
`test_a_capacity_top_up_..._lasts_the_year_...`, `test_topups.py`,
`test_topup_rules.py`, `test_topup_invariants.py`, `test_topup_concurrency.py`).
PASS on both trees; ledger rule "a usage top-up outlives a monthly usage
cycle" 0.

## 15. Concurrency

`test_annual_billing_concurrency.py`, real independent transactions, 10
workers, both schema lanes and the merged tree:

| Race | Result |
|---|---|
| 10 annual renewal workers | 1 renewal invoice, 1 MOTO request for the full year, 1 applied settlement, 1 twelve-month advance |
| 10 usage roll workers (months behind) | 1 cycle update to the right month, 0 provider requests, 0 invoices, term unchanged |
| Renewal racing usage roll at the annual boundary | 1 annual invoice, next year's term, its first usage cycle, 1 MOTO |

Settlement concurrency (DB-001, `test_settlement_concurrency.py`) passed in
both targeted runs.

## 16. Calendar oracle

`python -m scripts.billing_calendar_properties 1200000`: **1,200,040 checks, 0
violations** (annual tree and merged tree).

## 17. Entitlement oracle

`test_annual_billing_invariants.py`: 48 workspaces × 5 moments × 6 keys =
**1,440 comparisons, 0 mismatches**, on the model-built and migration-built
schemas and on the merged tree. The population covers monthly and yearly
prices; active, past_due, cancelled, expired and suspended subscriptions; base,
zero and unlimited allowances; usage and capacity top-ups and grants that are
live, expired, future-dated, under refund review or cancelled. The oracle
derives the usage cycle from the anchor with PostgreSQL month arithmetic, not
from the stored cycle.

## 18. Financial invariant ledger

`annual_billing_oracle.LEDGER`, **19 rules**: price belongs to version
(subscription, scheduled, invoice, offer); invoice amount/currency/term =
price snapshot; priced purchase names a price; paid annual invoice = 12
calendar months, monthly = 1; usage cycle inside term and ≤ 31 days; live term
length = price's; one active price per slot; no price on a free version; no
new checkout on a retired price; no cross-tenant custom price; usage top-ups
never outlive a month. **0 violations** on: the oracle population, every
annual scenario, the populated upgraded copy (before and after its round
trip) and the independent API test.

## 19. Mutation campaign

Disposable detached worktree `E:\wasla-annual-mut2` at `68e9afb`, own database;
the runner asserted each edit matched exactly once and `git status --porcelain`
was empty before and after every mutant. Control run (unmutated): 77 passed.

| Applied | Killed | Equivalent | Survived | Meaningful survivors |
|---|---|---|---|---|
| **20** (AB-M01 … AB-M20) | **20** | 0 | 0 | **0** |

Each kill was read, not only counted. Three are killed through a database
backstop, which was confirmed separately:

- **AB-M05** (annual renewal billed at the monthly price): the renewal invoice
  is refused by PostgreSQL, "an invoice charges exactly the price it names",
  so no renewal is issued and the renewal assertion fails.
- **AB-M10** (a new price edits the existing row): the `plan_prices` guard
  trigger refuses the UPDATE.
- **AB-M16** (cross-tenant custom price accepted for an offer): with the
  service check removed, a probe with no competing offer showed the insert is
  still refused by the database (`IntegrityError`); the test pins the
  service's own `ValidationError` and fails. Observation, not a defect: the
  service reports that database refusal with its generic "already has an open
  offer" message.

The other 17 are killed by a direct assertion on the behaviour named (see the
implementation report §25 for killers).

## 20. Paymob Test evidence

Reused, not repeated. The real Paymob Test scenarios A1-A7, T1 and T2
(implementation report §26) were produced by the implementation worktree
`E:\wasla-annual-billing`. The code tree that produced them equals the merged
code tree:

- no commit after `1db424f` touches `app/` or `alembic/` (`546c9da` adds tests
  and `scripts/billing_calendar_properties.py`; `91e7ffb` and `68e9afb` are
  documentation);
- the newest file under `app/` in that worktree was written at 16:55 UTC on
  2026-09-27, before the evidence payments (19:51-20:01 UTC, read-only query of
  the kept evidence database: 8 payments, all `provider_mode = test`), and the
  worktree is clean at `68e9afb`;
- merged tree = `annual-billing` tree (§8).

No new Test transaction was made, `E:\secrets.txt` was not read for
credentials, and Paymob Live was not used.

## 21. Security / secret scan

Scanned, printing counts only: the full merge diff `11cf44b..7eb7a74`, this
report, every verification log and JUnit file, the pre-merge summary. Checked
for the 25 values in `E:\secrets.txt` (read by the scanner, never printed) and
for patterns: Paymob secret keys, live public keys, JWTs, Bearer tokens, the
Test PAN, CVV values, database URLs with inline passwords, ngrok tokens, AWS
keys, private keys. **0 leaks.** Two pattern hits were inspected and are not
secrets: `${P…}` in `scripts/pitr_drill.sh` (a shell variable) and a
`sk_test_…` literal in the JUnit output that is a synthetic fixture already
committed in `tests/unit/test_paymob_*.py`.

## 22. Static gates

| | Annual `68e9afb` | Merge 1 `a13e696` | Merged `7eb7a74` |
|---|---|---|---|
| ruff | pass | pass | pass |
| black --check | 768 unchanged | 758 unchanged | 768 unchanged |
| mypy app tests | 680 files, no issues | 672 files, no issues | 680 files, no issues |

## 23. Full test lanes

| Lane | Annual tree | Merged tree |
|---|---|---|
| Model-built whole | 6,091 passed / 0 failed / 16 skipped | 6,091 / 0 / 16 |
| Migration-built integration + e2e | 2,991 / 0 / 0 | 2,991 / 0 / 0 |
| Kept-data AI/tool sweep (22 `ai_harness` suites + both invariant files, `WASLA_TEST_KEEP_AI_DATA=1`) | 218 / 0 / 0 | 218 / 0 / 0 |
| Targeted billing/annual/Paymob family (52 files, migration-built) | 859 / 0 / 0 | 859 / 0 / 0 |
| Targeted database remediation family (24 files, migration-built, incl. backup/restore script tests) | 183 / 0 / 0 | 183 / 0 / 0 |
| Annual oracle + races + 0081 migration tests (both schemas on annual; model on merged) | 6 / 0 / 0 | 6 / 0 / 0 |

## 24. Docker / CI-equivalent gates

Read from the current `.github/workflows/ci.yml` and run on `7eb7a74`:

| Gate | Result |
|---|---|
| Application factory | ok |
| `alembic upgrade head` / `downgrade base` / `upgrade head` | PASS |
| `db_preflight verify`, `alembic check` | ok / clean |
| Skip gate (only the three allowed reasons) | satisfied: real-provider opt-in and model-built parity only; the tmpfs skip is local-only (CI mounts it) |
| Object store not skipped | MinIO provisioned; 0 object-store skips |
| promtool `check config` | SUCCESS, 60 rules |
| promtool `test rules alerts_test.yml` | SUCCESS |
| amtool `check-config` | SUCCESS |
| `docker build --target runtime` | PASS |
| Container `/health/live` (`ENVIRONMENT=staging`, per-run `JWT_SECRET`) | `{"status":"alive"}` |
| `docker build -f Dockerfile.backup` | PASS |

`security.yml` runs on `main`/pull requests and `deploy.yml` on `main` CI
completion and `v*` tags, so pushing the canonical branch runs `CI` only and
deploys nothing.

## 25. Merge commit ledger

| Commit | Message |
|---|---|
| `a13e6963a658feb4d06a77a4e69950eee8dd1dc6` | merge(billing): integrate database and Paymob remediation |
| `7eb7a740dc04ddb7dc60dc8c1fab47ca7742ef0d` | merge(billing): integrate monthly and annual plan pricing |
| (this commit) | docs(billing): record annual billing merge verification |

```
11cf44b (canonical start)
   +-- a13e696 merge database-findings-remediation (4654a79)
   +-- 7eb7a74 merge annual-billing (68e9afb)
   +-- this report
```

## 26. Push preparation

The merges were made on the local branch `final-merge-annual`; the canonical
branch is fast-forwarded to it in `E:\wasla` (where it is checked out) and
pushed to its upstream `origin/worktree-billing-google-auth` without force.
`main`, feature branches and verification branches are not pushed. The
post-push SHA, origin equality and CI result are recorded in §29.

## 27. Remaining deployment verification

Not authorized by this merge, and not done:

| Item | State |
|---|---|
| Migration 0081 against a production copy | **NOT YET DEPLOYED** |
| Real annual prices published through the platform API | **NOT YET PUBLISHED** |
| Paymob Live MOTO for annual amounts | **NOT VERIFIED** |
| Frontend: show `prices[]`, send `plan_price_id` | **NOT DEPLOYED** |
| Production PITR (DB-009) | NOT VERIFIED (unchanged) |
| Online index/constraint builds (0075-0081) timed at production size | NOT VERIFIED |

## 28. Final verdict

Every requirement for READY TO PUSH holds: clean tree; `0081` single head;
clean uninterrupted model-built and migration-built runs (before and after the
merges); static gates; annual, Paymob and database remediation targeted gates;
0 financial invariant violations; 0 entitlement mismatches; 0 calendar
violations; 0 meaningful mutation survivors; tree equality verified; 0 secret
leaks.

**READY TO PUSH**

## 29. Push and CI

Recorded after the push in a follow-up commit.
