# Wasla — Custom Plans, Top-ups & Paymob Offers: Merge Verification

The final integration of Billing Remediation (ADR-112), Custom Plans & Top-ups
(ADR-113) and Paymob-bound Custom Plan Offers (ADR-114) into the canonical
branch. Implementation and evidence: [CUSTOM_PLANS_TOPUPS_IMPLEMENTATION.md](CUSTOM_PLANS_TOPUPS_IMPLEMENTATION.md).
Nothing in the billing design changed in this step.

## Branches and commits

| | |
|---|---|
| Source branch | `custom-plans-topups` |
| Source final HEAD (`SOURCE_FINAL_HEAD`) | `39836efa5ccb8ea2cbab511dd1087052b0e4d9bc` |
| Parent branch | `billing-findings-remediation` (pre-merge `e7c604e8d2b7c685577001b36f9d9bd4d1463d42`) |
| Billing-remediation merge commit | `c8f85bdb2e8deaa5a6882e0dbb2b970d95fe39ed` — `merge: custom plans and topups billing` |
| Canonical target branch | `worktree-billing-google-auth` (tracks `origin/worktree-billing-google-auth`) |
| Canonical pre-merge HEAD | `e1b5cec4961974f06b42f976ae89a5dccb430545` |
| Canonical merge commit (`CANONICAL_MERGED_HEAD`) | `d2d8390d65db41cb8f1cd19f9e96e6bf3e8649e1` — `merge: billing remediation and commercial plans` |
| Migration head | `0073` (single) |
| Merge conflicts | **None** in either merge |
| Live Paymob | **NOT USED** |
| Push | pending at the time this record was written; see the final report |

**Ancestry, checked before merging** (`git merge-base --is-ancestor`):
`worktree-billing-google-auth` ⊂ `billing-findings-remediation` ⊂
`custom-plans-topups`. After `git fetch origin --prune` the canonical branch was
86 ahead and 0 behind its upstream (no divergence). Neither feature branch has
an upstream.

**Tree equality.** `git diff --exit-code custom-plans-topups c8f85bd` and
`git diff --exit-code 39836ef d2d8390` both exit 0: each merge added history
only, and the canonical tree is byte-for-byte the tree every gate below ran on.

**The canonical worktree held an untracked `BILLING_PAYMENTS_AUDIT.md`** that
the merge brings in as a tracked file. It was byte-identical to the incoming
blob (`306e59f1f3be5a4cfd65a2aa77595fe6f841fb59`). It was backed up and moved
aside for the merge, and the merged file was then verified to be the same blob.
The other untracked files in that worktree were not touched.

## Report corrections (commit `9501a6d6fd0cce65b00d72be705cf0e184aff4d5`)

* **Final/current HEAD metadata.** The header named `d3103ac` (Part I's final
  code/test HEAD) as the final HEAD. It now records Part I's and Part II's
  code/test HEADs, Part II's docs HEAD, and the current migration head `0073`
  and 192 operations; Part I's `0072`/186 are labelled historical.
* **Paymob Test total.** 6,590 EGP was wrong; the eight real Test transactions
  sum to **7,940 EGP (Test)** (1,500 × 5 + 200 + 150 + 90), plus one real TOKEN
  callback. No evidence row changed.
* A Part II commit ledger was added.

## Part II mutation verification (ADR-114)

Detached temporary worktree, isolated database, Part I's protocol (one textual
change, in-memory compile, no bytecode, killer first, `-x`, sha256 restore).

| Run | HEAD | Applied | Killed | Equivalent | Survived |
|---|---|---|---|---|---|
| 1 | `9501a6d` | 13 | 10 | 0 | 3 (OFFER-03, 07, 11) |
| 2 (final) | `adb82a8` | **13** | **13** | **0** | **0** |

The three survivors of run 1 were real test gaps, closed by
`adb82a8d341ab6a0144f7f285d139d94496a32bc` (`test(billing): close custom-plan
offer mutation gaps`); the new tests pass on both schemas. 13/13 files restored
byte for byte. Details and per-mutant hashes: implementation report §22a.

## Gates

Run on `SOURCE_FINAL_HEAD` from clean `git archive` exports, each lane on its
own database; then static, migration and targeted gates again on
`CANONICAL_MERGED_HEAD`.

| Gate | Where | Result |
|---|---|---|
| ruff / black --check / mypy (300 files) | source export; merge commit | **PASS** / **PASS** |
| `alembic heads` | source; merge | `0073` single head |
| Fresh empty database `0001` → `0073` | source (`wasla_srcalembic`); merge (`wasla_mergealembic`) | 73 upgrades, **PASS** |
| `0073` → `0072` → `0073`, then `alembic check` | source | **PASS**, no drift |
| promtool `check config` / `test rules` | source | **53 rules** load / all tests **PASS** |
| Targeted billing (offers, lifecycle, top-ups, concurrency, invariants, commercial and platform billing API, reconciliation, recurring/MOTO, remediation journeys, tenant isolation, platform authorization, access audit, Paymob checkout) | source (`wasla_srcmodels`) | **245 passed**, 0 failed (124 s) |
| Targeted billing + documentation truth (API count 192, migration range) + workspace purge classification | merge commit (`wasla_mergetargeted`) | **281 passed**, 0 failed (129 s) |
| Whole suite, model-built schema | source (`wasla_srcmodels2`) | **5,718 passed, 136 skipped, 0 failed** (5,854 collected, 1,432 s) |
| Integration + e2e, migration-built schema | source (`wasla_srcmigs`) | **2,715 passed, 110 skipped, 0 failed** (2,825 collected, 1,471 s) |

**One flaky failure, stated.** The first model-built run (`wasla_srcmodels`,
1,541 s) had 1 failure out of 5,854:
`test_rag_integrity.py::test_knowledge_administration_is_audited_without_its_content`.
It orders four audit rows written microseconds apart by `occurred_at` alone, so
equal timestamps can swap. Neither the test nor the knowledge service is touched
by this branch (`app/db/models/audit.py` only gained billing enum labels). It
passed 10/10 in isolation and in every other whole run. The whole model-built
suite was re-run from the same frozen export on a fresh database, with 0
failures, and that rerun is the authoritative result above. The flake itself is
out of scope here and left as it was.

Documentation truth (192 operations, migrations `0001`–`0073`), route policy,
access-audit coverage and the workspace purge classification are all asserted
inside these suites.

## Real Paymob evidence

Not repeated: no merge conflict and no code change touched a provider-facing path
after the Part II Test E2E, and the merged tree equals the tested tree. The Part II
evidence stands: 8 real Paymob Test transactions (7,940 EGP Test) on card
integration 5885262 and MOTO integration 5934829, `provider_mode: test`,
callbacks `is_live: false`, plus one real TOKEN callback. No secret, card token,
client secret or payment key appears in this repository.
