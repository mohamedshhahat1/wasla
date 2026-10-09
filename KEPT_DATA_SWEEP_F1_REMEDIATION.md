# Kept-Data Sweep F-1 Remediation

Stage: **KEPT-DATA SWEEP & AI INVARIANTS FOR ENT-02 (F-1)** — stage 4 of the canonical merge.
Branch `kept-data-invariants-20261009` in `E:\wasla-kept-data`, on `KD_BASE_HEAD` `fe12a37`.
Date: 2026-10-09. Nothing pushed, merged or deployed.

## 1. Executive Summary

The canonical merge verification (`b1431bf`) stopped on one finding, F-1: CI's kept-data sweep — the last step of the `migration-parity` job — failed 3 tests at `fe12a37`.

This stage reproduced F-1 exactly as `ci.yml` runs it: 282 collected, 279 passed, 3 failed. That matches the merge verification. It also reproduced F-1 at `a7fc8b5` (268 / 265 / 3) and found the sweep green at `2c58c35` (232 / 232 / 0).

Every counted row was classified. There were 4 F-1a rows and 14 F-1b workspaces, and F-1c's counter read 2. Each row is either a **legal ENT-02 state** or a **test artefact**. None is an application defect, so `app/` is unchanged.

The merge verification's reading was confirmed with two additions:

- **Two artefact classes.** R-OPEN is a dead worker's hold that no test ever swept. R-LATE is a late charge on a turn its test never completed.
- **A latent time bomb in the old F-1a.** Released provider failures stay `engaged` by design. The old check would have counted them in any sweep that ran longer than 15 minutes.

What changed:

| ID | Change |
|---|---|
| F-1b | "customer turns charged other than once per engaged turn" becomes **"customer turns whose charge disagrees with their outcome or usage"**. Rules B1..B7 compare what the application wrote. Each rule is proven by its own injection. |
| F-1a | "engaged turns stranded past any healthy turn's length" becomes **"AI turn holds nothing settled, released or expired within the TTL and a sweep"**. Rules A1..A3 apply, with a cutoff of TTL + sweep interval + 60 s = 1,560 s. |
| F-1c | The expired-hold test now asserts the increment its own sweep caused, read from the database. It writes a second workspace's dead hold on purpose. |
| artefacts | Two tests now leave a legal final state through the real paths: the sweep's release, and the worker's completion. |
| KD-04 | `python -m scripts.kept_data_sweep`, plus a unit test that proves its selection equals `ci.yml`'s. |

Results at `8b79d80`:

- **Kept-data sweep:** 300 / 300 / 0 / 0, three times out of three on fresh databases.
- **Mutation matrix:** 13 / 13 killed. That covers M-K01..M-K10 and the regression lane M-E01, M-E02 and M-E08.
- **Application mutants:** the sweep itself counts the rows ENT-02 forbids. Charging an empty answer gives 7 rows under B6. Charging at engagement gives 20 rows under B6. A sweeper that never expires a hold gives 1 row under A.
- **Model-built lane:** 6,819 collected, 6,803 passed, 0 failed, 16 skipped.
- **Migration-built lane:** 6,819 collected, 6,807 passed, 0 failed, 12 skipped.
- **Every `ci.yml` job:** listed, and each was run locally or recorded with what proves it (§13).

**Verdict: KEPT-DATA SWEEP F-1 CLOSED WITH EXTERNAL ACTIONS REMAINING**

## 2. Starting Repository State

| Item | Value |
|---|---|
| `E:\wasla` | `worktree-billing-google-auth` at `3473068`, clean tracked tree; 11 untracked user documents (not touched) |
| origin | `origin/worktree-billing-google-auth` = `354db53` (still lacks `aa11098`) |
| `b1431bf` | on `canonical-merge-verification-20261009` only |
| platform operations | `platform-entitlement-ops-20261008` at `fe12a37`, unchanged since the merge verification → **KD_BASE_HEAD = `fe12a37`** |
| entitlements / omnichannel | `a7fc8b5` / `2c58c35`, unchanged |
| Alembic | single head `0100` (no migration added or edited by this stage) |
| Python / Docker / Compose | 3.12.7 / Engine 29.8.0 / Compose v5.5.1 |
| PostgreSQL / Redis | 16.15 (`pgvector/pgvector:pg16`) / 7.4.11 |
| `app.__file__` | `E:\wasla-kept-data\app\__init__.py` (asserted by every run in this stage; baselines assert their own worktree) |

Worktrees: `E:\wasla-kept-data` (this branch), plus detached, never-edited worktrees: `E:\wasla-kd-base-omni` (`2c58c35`), `E:\wasla-kd-base-ent` (`a7fc8b5`), `E:\wasla-kd-bisect` (`fe12a37`).

Infrastructure (this stage only): `wasla-kd-pg` 127.0.0.1:56361, `wasla-kd-redis` :56371, `wasla-kd-redis2` :56372 (`TEST_REDIS_URL`), `wasla-kd-minio` :59361 with the `wasla-media` bucket created before the first run. The development-iteration and bisect pair is `wasla-kd-pg2` :56362 and `wasla-kd-redis3` :56373. Every database is a `wasla_kd_*` database, and the helper refuses any other name. The developer's Redis DB 0, the development database and the other stages' containers were never used.

## 3. Baseline Reproductions

Every run below used one fresh database, `WASLA_TEST_KEEP_AI_DATA=1` and `WASLA_TEST_SCHEMA=migrations`, which is the job's schema. The selection was `ci.yml`'s rule (`grep -l "ai_harness" tests/integration/test_*.py | grep -v _invariants`) plus the two invariant files, and nothing else ran on that server at the time. The printed file lists matched the shell rule exactly: 28 suites at `fe12a37`, 27 at `a7fc8b5` and 24 at `2c58c35`.

| Commit | Collected | Passed | Failed | Skipped | Failing checks |
|---|---|---|---|---|---|
| `fe12a37` (KD_BASE_HEAD) | 282 | 279 | **3** | 0 | F-1a **4 rows**, F-1b **14 workspaces**, F-1c `hold_expired` **2.0 ≠ 1.0** |
| `a7fc8b5` (entitlements) | 268 | 265 | **3** | 0 | the same three (F-1a 4, F-1b 14, F-1c 2 ≠ 1) |
| `2c58c35` (omnichannel) | 232 | 232 | 0 | 0 | — |

The four suites added after `2c58c35` were `test_ai_turn_charging`, `test_channel_not_in_plan`, `test_entitlement_observability` and `test_grant_withdrawal`. `a7fc8b5` lacks only the last of them.

**Bisect at `fe12a37`.** Each run kept data on a fresh database and used the old invariants. The order was the suite, then `test_entitlement_observability.py`, then `test_ai_invariants.py`. The observability suite contributes 2 stranded rows of its own in every run.

| Suite | F-1a | F-1b | F-1c |
|---|---|---|---|
| `test_ai_turn_charging` | 4 (2 own + 2 observability) | **3** | **2.0 ≠ 1.0** |
| `test_entitlement_observability` alone | 2 | 0 | pass |
| `test_channel_not_in_plan` | 2 (observability's) | 0 | pass |
| `test_grant_withdrawal` | 2 (observability's) | 0 | pass |
| `test_ai_empty_response` (pre-existing) | 2 | **5** | pass |
| `test_crm_handoff_authority` (pre-existing) | 2 | **3** | pass |
| `test_tool_authority` (pre-existing) | 2 | **1** | pass |
| `test_tool_execution_records` (pre-existing) | 2 | **1** | pass |
| `test_ai_lifecycle` (pre-existing) | 2 | **1** | pass |

3 + 5 + 3 + 1 + 1 + 1 = 14. Eleven of the fourteen F-1b workspaces come from suites that existed at `2c58c35`. Those suites did not change. ENT-02 changed how their turns are charged: an empty answer, an escalation and a pre-composition suppression were charged at engagement before ADR-131 and are released after it.

## 4. Decisions Applied

| ID | Applied as |
|---|---|
| KD-01 | Both invariants were rewritten. No suite is excluded, and `ci.yml` is unchanged. |
| KD-02 | F-1b has become rules B1..B7. One adaptation: B4 permits `hold_expired` beside a charge, because the code (`ai_turn_charge.py:132-140`) and the database constraint `ck_agent_turns_charge_state_consistent` both keep the sweep's release reason on a late charge, which ADR-131 decided. |
| KD-03 | F-1a has become rules A1..A3. The cutoff is TTL + `POLL_SECONDS` + 60 s, which is 1,560 s with the defaults. An engaged turn that wrote no hold at all is also counted. |
| KD-04 | `scripts/kept_data_sweep.py` and `tests/unit/test_kept_data_sweep.py`. The test parses `ci.yml`, so `ci.yml` is not modified (see §13). |
| KD-05 | F-1c now measures its own pass. |
| KD-06 | `app/` is unchanged; no row class is a defect. |
| KD-07 | Every job was inventoried and run (§13). |

## 5. Row Classification

All rows come from the `fe12a37` sweep, dumped before the session dropped its schema by a scratch pytest plugin that hooks the last test's teardown. The dump held 221 turns and 155 `ai_turn` events. The producing test was identified by matching the workspace's creation time against each test's run window.

### F-1b — 14 workspaces, one turn each

| Class | Turns | Row shape | Producing tests | Code that writes it | ENT-02 | Decision |
|---|---|---|---|---|---|---|
| R-EMPTY | 7 | completed · `empty_response` · `released/not_chargeable` · 0 events | `test_ai_empty_response` ×4 (refusal part, tools-only, Arabic, tells the customer), `test_crm_handoff_authority::test_an_empty_response_handoff_and_a_takeover_make_one_handoff`, `test_tool_execution_records::test_the_turn_budget_outlives_one_response`, `test_ai_turn_charging::test_an_empty_answer_is_not_charged` | `AgentOutcome.chargeable` returns False for `EMPTY_RESPONSE` (`orchestrator.py:153-170`); `AITurnCharge.settle` releases with `NOT_CHARGEABLE` (`ai_turn_charge.py:114-126`) | "not charged: an empty answer" | **LEGAL ENT-02 STATE** |
| R-ESCALATED | 2 | completed · `escalated` · `released/not_chargeable` · 0 | `test_ai_lifecycle::test_an_escalation_is_recorded_as_one`, `test_ai_turn_charging::test_an_escalation_before_any_reply_is_not_charged` | the same; escalation before composition means `composed` is False | "not charged: a sentiment escalation before any composition" | **LEGAL ENT-02 STATE** |
| R-SUPPRESSED | 4 | completed · `suppressed_human` (3) / `suppressed_workspace` (1) · `released/not_chargeable` · 0 | `test_crm_handoff_authority` ×2 (takeover during the sentiment reading; handoff tool that loses to a takeover), `test_tool_authority::test_a_colleagues_takeover_outranks_every_stale_tool_call`, `test_ai_empty_response::test_an_empty_answer_for_a_workspace_suspended_mid_turn_sends_nothing` | no reply text was composed, so `chargeable` is False | charged only for "reply text … withheld by a re-read after it was written"; none was written | **LEGAL ENT-02 STATE** |
| R-LATE (F-1b side) | 1 | workspace with 2 charges against 1 engaged-with-outcome turn: the replied turn, plus the dead hold that was late-charged (`engaged` · no outcome · `charged/hold_expired` · 1 event) | `test_ai_turn_charging::test_an_expired_hold_stops_counting_and_is_released_by_the_sweep` | `settle(chargeable=True)` on an expired hold returns `LATE_CHARGE` (`ai_turn_charge.py:129-156`) | "a settle arriving afterwards still charges" | The charge is a **LEGAL ENT-02 STATE**. The missing completion is a **TEST ARTEFACT** (class R-LATE below). |

### F-1a — 4 rows

| Class | Rows | Row shape | Producing test | Decision |
|---|---|---|---|---|
| R-OPEN (swept by another suite) | 1 | `engaged` · `released/hold_expired`, held 2026-09-30 23:58 | `test_ai_turn_charging::test_a_hold_counts_only_in_the_usage_cycle_it_was_taken_in` | **TEST ARTEFACT**. The test writes a dead worker's hold (`_dead_hold`) and never runs the sweep. The hold stayed open until the F-1c test's global sweep happened to release it. Production's billing sweep releases such a hold within one interval of the TTL (`billing_worker.py:454-471`, `agent_turn_repository.py:460-498`). Under the new A3 the expired hold is explained, but the test's own data is left legal anyway. Otherwise the outcome would depend on which suite runs after it: the bisect run of the charging suite alone would leave the hold open. |
| R-LATE | 2 | `engaged` · no outcome · `charged/hold_expired` · 1 event | `test_ai_turn_charging::test_an_expired_hold_stops_counting_and_is_released_by_the_sweep`; `test_entitlement_observability::test_an_expired_hold_and_its_late_charge_are_counted` | The late charge is a **LEGAL ENT-02 STATE** (ADR-131), and `engaged` is legal for a dead worker. The missing outcome is a **TEST ARTEFACT**. The worker settles and then completes (`ai_worker.py:757` → `_complete_turn`), but the tests drove only the settle. Left as it was, the row looks exactly like a charged provider failure (B6). |
| R-OPEN | 1 | `engaged` · `held`, 16 min old | `test_entitlement_observability::test_the_gauges_render_holds_capacity_and_reductions` (and its second hold, 30 s old, which is not yet counted) | **TEST ARTEFACT**: a dead hold that is never swept. |

There were no rows in the APPLICATION DEFECT class.

### Classes the old checks did not count but the new ones must leave alone

| Class | Rows in the dump | Shape | Decision |
|---|---|---|---|
| R-FAILED | 18 | `engaged` · no outcome · `released/generation_failed` · 0 | **LEGAL**. ENT-02 releases a provider failure, and the turn stays `engaged` by design (ADR-074; RUNBOOK "A worker died mid-turn"). The old F-1a would have counted all 18 in any sweep longer than 900 s. That latent time dependence is removed (F-1a). |
| R-UNHELD | 32 | completed · `quota_blocked` / `suppressed_*` / `channel_not_in_plan` · no charge state | **LEGAL**. These were refused before engagement and never held (`AITurnChargeState` docstring). |
| R-COMPOSED-SUPPRESSED | 12 | completed · `suppressed_*` · `charged` · 1 | **LEGAL**. Reply text was composed, then withheld by a re-read; ENT-02 charges it. |

### F-1c — counter 2, expected 1

The F-1c test's pass, `BillingWorker.run_once(now=07:10:44.692463Z)`, released two holds. One was its own. The other was R-OPEN's hold from the charging suite's usage-cycle test (held 2026-09-30 23:58:00Z), released at that same `released_at`. The pass is global by design: `ExpiredHoldSweep` is unscoped (`agent_turn_repository.py:441-458`), as the billing sweep is in production. So the counter was right and the assertion was wrong. **F-1c is a TEST DEFECT, not an application defect.**

## 6. The Rewritten Invariants

`tests/integration/test_ai_invariants.py`. Both checks are read-only. They run in every lane, as before; only the non-vacuity check is kept-only. Each failure prints up to 20 counted rows as `rule workspace row`.

**F-1b — "customer turns whose charge disagrees with their outcome or usage".** The old name, "customer turns charged other than once per engaged turn", is recorded in the module and the ADR. It counts one row per offending turn (B1..B6) or event (B7):

| Rule | Counts |
|---|---|
| B1 | `charge_state = charged` and the turn's `ai_turn` events ≠ 1 |
| B2 | an `ai_turn` event names a turn whose `charge_state` is not `charged` |
| B3 | `released` with a reason that is NULL or outside `{not_chargeable, generation_failed, hold_expired}` |
| B4 | `charged` with any release reason other than `hold_expired` (a late charge keeps the sweep's) |
| B5 | outcome `replied` / `handed_off` and not `charged` |
| B6 | `charged` with outcome in `{escalated, empty_response, nothing_to_answer, quota_blocked, channel_not_in_plan}`, or with **no outcome** (a provider failure charged; also a worker that died between charge and completion, which the AI-09 gauge shows too) |
| B7 | an `ai_turn` event with no `agent_turn_id`, naming another workspace's turn, or naming a missing turn while its conversation still exists (a turn deleted with its conversation leaves its charge by design, `usage.py:205-209`) |

The never-charged list is asserted equal to the entitlement ledger's E06 list (`scripts/omnichannel_invariants._NOT_CHARGEABLE`). The release reasons are asserted equal to `AITurnReleaseReason`. ENT-02 as documented (ADR-131 decision 2; AI_AGENTS table) and as implemented (`AgentOutcome.chargeable`, `AITurnCharge.settle`) agree on every outcome, so the §18 stop condition did not trigger.

**F-1a — "AI turn holds nothing settled, released or expired within the TTL and a sweep".** The old name, "engaged turns stranded past any healthy turn's length", is recorded.

| Rule | Condition |
|---|---|
| A1 | `charge_state = held`, or `state = engaged` with no charge state (a hold nobody wrote) |
| A2 | `coalesce(held_at, engaged_at) < now() − hold_cutoff_seconds` |
| A3 | implied by A1: a turn charged or released (including `hold_expired`) is explained |

`hold_cutoff_seconds = AI_TURN_HOLD_TTL_SECONDS + billing_worker.POLL_SECONDS + 60`, which is **900 + 600 + 60 = 1,560 s**. The TTL is read from the settings the harness's workers run with. The billing sweep releases a dead hold at most one interval after its TTL, and the margin absorbs commit and clock skew. `test_the_stranded_hold_cutoff_is_the_ttl_plus_one_sweep_plus_a_margin` pins the formula. `test_a_hold_past_its_ttl_but_within_one_sweep_is_not_yet_stranded` pins the boundary: TTL + 120 s is not counted.

**Tool invariants reviewed.** `test_tool_invariants.py` has one state-based check, "turns stranded engaged while a tool of theirs was recorded" (engaged > 900 s with a tool execution). It does not read charge state, and ENT-02 did not change when a turn leaves `engaged`: failures stayed engaged before ADR-131 too. It is therefore **not stale against ENT-02** and was left unchanged. A fourth kept run at `8b79d80` dumped every engaged turn that had a tool execution: there were none. The 900 s clock in that check is therefore not reachable by the kept data either.

## 7. Observability Test

`test_an_expired_hold_and_its_late_charge_are_counted` now runs as follows:

1. It writes its own dead hold, and a second workspace's dead hold on purpose.
2. It runs the real `BillingWorker.run_once(now)`.
3. It reads `swept` from the database: the turns whose `released_at = now` and whose reason is `hold_expired`.
4. It asserts that both holds are in `swept`, and that the counter equals `{hold_expired: len(swept), late_charge: 1}`.

In a normal lane `len(swept)` is exactly 2. In kept data it is at least 2, which is correct for a global sweep. Reverting to the process-wide `1.0` (M-K09) fails in every mode, kept or not. The test's late-charged turn is then completed as the worker does (R-LATE).

## 8. Suites Changed to Leave a Legal State

| Suite / test | Class | Change | Visible as |
|---|---|---|---|
| `test_ai_turn_charging::test_a_hold_counts_only_in_the_usage_cycle_it_was_taken_in` | R-OPEN | after its assertions, `_expire_like_the_sweep` runs `ExpiredHoldSweep.release_expired` at the first moment the billing sweep would | comment citing F-1 and R-OPEN |
| `test_ai_turn_charging::test_an_expired_hold_stops_counting_and_is_released_by_the_sweep` | R-LATE | after its assertions, `_complete_like_the_worker` completes the late-charged turn with `REPLIED` through `AgentTurnRepository.complete` | helper docstring citing F-1 and R-LATE |
| `test_entitlement_observability::test_the_gauges_render_holds_capacity_and_reductions` | R-OPEN | after its assertions, the sweep's release, as of the younger hold's TTL end (releases both) | comment |
| `test_entitlement_observability::test_an_expired_hold_and_its_late_charge_are_counted` | R-LATE | completion as above | — |

No assertion changed in normal mode: both helpers run after the tests' assertions. `_expire_like_the_sweep` itself asserts that it released something, which is why M-K08 also fails these tests. Proof: the full kept sweep at `8b79d80` ends with 0 open holds and 0 charged turns without an outcome in every run (§14).

## 9. Application Changes

**None.** `git diff fe12a37..8b79d80 -- app alembic` is empty. No migration was added or edited.

## 10. Injection Tests

Added to `test_ai_invariants.py`. They run in both normal lanes and in the kept sweep. Each test builds a legal set inside the `db_session` rolled-back transaction. The set holds a charged reply, a late-charged reply (`charged/hold_expired`), a released failure and a released empty answer, plus two spare triggers. The test asserts that both allowance checks read 0 on that set, adds one violation, and asserts that `{check: +1}` is the only change across all 15 AI checks. It also asserts that the evidence names exactly the expected rule. Where the database itself refuses the violation, the guard is lifted inside the same rolled-back transaction: `uq_usage_events_tenant_id_agent_turn_id` for B1, and `ck_agent_turns_charge_state_consistent` for B3 and B4.

| Test | Rule | F-1b before → after | F-1a before → after |
|---|---|---|---|
| `test_a_turn_charged_twice_is_counted` | B1 | 0 → 1 | 0 → 0 |
| `test_a_charged_turn_without_its_usage_event_is_counted` | B1 | 0 → 1 | 0 → 0 |
| `test_a_released_turn_with_a_usage_event_is_counted` | B2 | 0 → 1 | 0 → 0 |
| `test_a_released_turn_without_a_reason_is_counted` | B3 | 0 → 1 | 0 → 0 |
| `test_a_charged_turn_with_a_release_reason_is_counted` | B4 | 0 → 1 | 0 → 0 |
| `test_a_replied_turn_left_uncharged_is_counted` | B5 | 0 → 1 | 0 → 0 |
| `test_a_provider_failure_that_was_charged_is_counted` | B6 | 0 → 1 | 0 → 0 |
| `test_an_empty_answer_that_was_charged_is_counted` | B6 | 0 → 1 | 0 → 0 |
| `test_a_usage_event_without_a_turn_is_counted` | B7 | 0 → 1 | 0 → 0 |
| `test_a_usage_event_of_another_workspaces_turn_is_counted` | B7 | 0 → 1 | 0 → 0 |
| `test_a_hold_nothing_settled_released_or_expired_is_counted` | A | 0 → 0 | 0 → 1 |
| `test_a_charge_whose_conversation_was_deleted_is_not_counted` | negative (B7) | 0 → 0 | 0 → 0 |
| `test_a_hold_past_its_ttl_but_within_one_sweep_is_not_yet_stranded` | negative (A2 boundary) | 0 → 0 | 0 → 0 |
| `test_an_expired_hold_is_not_stranded` | negative (A3) | 0 → 0 | 0 → 0 |
| `test_a_released_provider_failure_is_not_counted` | negative | 0 → 0 | 0 → 0 |
| `test_an_uncharged_empty_answer_is_not_counted` | negative (the F-1b shape) | 0 → 0 | 0 → 0 |
| `test_the_legal_set_holds_every_shape_the_rules_must_not_count` | baseline non-vacuity | — | — |
| `test_the_stranded_hold_cutoff_is_the_ttl_plus_one_sweep_plus_a_margin` | formula (M-K05) | — | — |

That makes 18 tests: 11 positive, 5 negative and 2 structural. The module goes from 17 to 35 tests, which is why the kept sweep grows from 282 to 300. The unit suite gains 3 tests (`test_kept_data_sweep.py`).

## 11. Mutation Matrix

`python -m scripts.verification.run_kept_data_mutations` was run on database `wasla_kd_mut`, alone on the server. The runner:

- runs a control first, covering the killers and the full kept sweep;
- applies one exact-match edit per mutant and restores it, checking each file by SHA-256 and checking `git status`;
- runs with bytecode off;
- reports KILLED only when every named killer test fails. Otherwise the verdict is `wrong-kill`, or `invalid` when the run never reached a test.

Control: 12 killer tests passed (11 normal, 1 kept), and the control sweep gave 300 / 300.

| Mutant | Removes | Verdict | Killer and evidence | Kept sweep at the mutant |
|---|---|---|---|---|
| M-K01 | B1 | KILLED | `test_a_turn_charged_twice_is_counted`: `assert {} == {'customer…usage': 1}` | — |
| M-K02 | B2 | KILLED | `test_a_released_turn_with_a_usage_event_is_counted` | — |
| M-K03 | B6 | KILLED | `test_a_provider_failure_that_was_charged_is_counted` (and `…empty_answer…`) | — |
| M-K04 | A3 always true | KILLED (second run) | `test_a_hold_nothing_settled_released_or_expired_is_counted`: `assert {} == {'AI turn hol…': 1}` | — |
| M-K05 | fixed 900 s threshold | KILLED | `test_a_hold_past_its_ttl_but_within_one_sweep_is_not_yet_stranded`: `assert {'AI turn hol…': 1} == {}` | — |
| **M-K06** | app charges an empty answer | KILLED | `test_an_empty_answer_is_not_charged`: `CHARGED is RELEASED` | 300 / 281 / **19 failed**; F-1b **7 rows, all B6** (the 7 empty answers) |
| **M-K07** | app charges at engagement | KILLED | `test_a_provider_failure_is_not_charged_and_gives_its_hold_back`: `CHARGED is RELEASED` | 300 / 274 / **26 failed**; F-1b **20 rows, all B6** (failures and never-charged endings charged) |
| **M-K08** | sweeper never expires | KILLED | `test_an_expired_hold_stops_counting_and_is_released_by_the_sweep`: `HELD is RELEASED` | 300 / 277 / **23 failed**; F-1a **1 row, A** (the usage-cycle test's hold, held 2026-09-30) |
| M-K09 | F-1c reads the process total | KILLED (kept mode) | `test_an_expired_hold_and_its_late_charge_are_counted`: the counter dict differs on `hold_expired` | — |
| M-K10 | runner drops a file | KILLED | `test_the_runner_selects_exactly_what_ci_selects` | — |
| M-E01 | regression | KILLED | `test_a_provider_failure_is_not_charged_and_gives_its_hold_back` | — |
| M-E02 | regression | KILLED | `test_an_empty_answer_is_not_charged` | — |
| M-E08 | regression (expired holds keep counting) | KILLED | `test_an_expired_hold_stops_counting_and_is_released_by_the_sweep`: `(1, False) == (0, True)` | — |

**Total: 13 mutants, 13 killed, 0 survived, 13 restored.** M-K04's first run was `invalid`, not a survival. The edit left the A1 condition's `OR` dangling, PostgreSQL refused the query, and the runner reported the run as invalid as designed. The edit was corrected (`5cdf37a`) and the re-run was KILLED.

The other failures in the M-K06..M-K08 sweeps are the application suites' own ENT-02 assertions. They also include the injection tests: their legal-baseline assertion requires both allowance checks to read 0, and under a mutant the kept data does not.

## 12. Test Non-Vacuity

| Claim | Evidence |
|---|---|
| The kept sweep ran CI's selection | the runner printed 28 suites + 2 sweeps; that list equals `grep -l ai_harness … | grep -v _invariants` run in Git Bash on the same tree, and `test_the_runner_selects_exactly_what_ci_selects` proves the equality on every lane |
| Kept mode was really on | `test_a_kept_run_really_had_something_to_sweep` (AI and tool) skip unless `WASLA_TEST_KEEP_AI_DATA=1`; the runner fails on any skip; 0 skipped in all runs |
| The invariants read a non-empty dataset | each of the three runs at `8b79d80`: **222 turns, 200 with an outcome, 155 charges, 11 distinct outcomes**; charge states present: `charged` 153, `charged/hold_expired` 2, `released/generation_failed` 18, `released/not_chargeable` 13, `released/hold_expired` 4, none 32; the non-vacuity check now requires each of the five charge shapes |
| Injection baselines are legal | `_legal` asserts both allowance checks read 0 before each injection; `test_the_legal_set_holds_every_shape_the_rules_must_not_count` asserts the four shapes are present |
| F-1c expired its own hold | `assert {dead, theirs} <= swept`, where `swept` is read from the database |
| Mutated code paths executed | M-K06: 7 `empty_response` turns charged; M-K07: 20 turns charged under B6; M-K08: the cycle hold stayed `held` (the A row) |

## 13. CI Job Inventory (KD-07)

`.github/workflows/ci.yml` has five jobs, triggered on push to any branch, on pull requests and on `workflow_dispatch`. The other workflows, `security.yml` and `deploy.yml`, are outside KD-07's scope.

| Job | Steps | Local equivalent at `8b79d80` | Result |
|---|---|---|---|
| `quality` | `ruff check .`; `black --check .`; `mypy app tests` | same commands (plus `mypy scripts`) | ruff clean; black 901 files unchanged; mypy 0 errors in 789 files; scripts 0 in 11 — **CLOSED** |
| `tests` | full `pytest -rs` with the two kept-only deselects and `--cov`; skip gate; object store not skipped; application factory; `alembic upgrade/downgrade base/upgrade`; `db_preflight verify`; `alembic check` | the same steps on `wasla_kd_models` with MinIO; `--cov` dropped (see note) | pytest 6,819 / 6,803 / 0 / 16 (42 min). Skip gate: the only skip outside the allowed reasons is the tmpfs test (local-only, see note). Object store: 0 skipped for want of a store. Application factory ok. Migrations up / down base / up: exit 0. `db_preflight verify`: ok. `alembic check`: no new upgrade operations. **CLOSED** |
| `migration-parity` | `test_schema_parity.py`; parity-not-skipped check; `pytest tests/integration tests/e2e` (deselects) under `WASLA_TEST_SCHEMA=migrations`; kept-data sweep | parity suite; the full suite (a superset of the integration+e2e step) under migrations; `scripts.kept_data_sweep` ×3 | parity suite passed with 0 skipped; full suite under migrations 6,819 / 6,807 / 0 / 12 (44 min), skip gate tmpfs only; kept-data sweep 300 / 300 three times — **CLOSED** |
| `monitoring` | `promtool check config`; `promtool test rules`; `amtool check-config` | the same images and commands via Docker | config valid, 70 rules, rules tests SUCCESS, Alertmanager valid (1 inhibit rule, 2 receivers); all exit 0 — **CLOSED** |
| `docker` | buildx build `--target runtime`; run as `staging` with a per-run JWT secret; `/health/live` | `docker build --target runtime`, same run and probe | build ok (263 s). The first liveness probe, run straight after the build, got `Empty reply from server` for its 30 attempts. The container's own log showed a normal startup, and probing inside the container returned `{"status":"alive"}`. Three fresh runs then passed at attempts 5, 4 and 4. The cause was not identified, and the failure was not reproduced. It is recorded here rather than counted as a pass. **CLOSED** on the three passing runs. |

Notes:

- **Coverage.** CI's `tests` job adds `--cov=app --cov-report=term-missing`. The repository sets no `fail_under`, so coverage only reports and cannot change the job's result. A first model run with `--cov` reached 29% in 50 minutes, because tracing slowed the async suite about 4× on this machine. It was stopped, its processes were killed and verified gone, and the lane was re-run without coverage. Recorded as a deviation, not a gap.
- **`WASLA_TEST_TMPFS`.** CI mounts a 2 MB tmpfs. Windows has none, so `test_local_storage_cleanup.py` skips locally. That is the one skip CI's skip gate would reject that is local-only, and on the runner the tmpfs step provides it. **EXTERNAL VERIFICATION** for that single test: the GitHub run proves it.
- **GitHub-only mechanics.** `actions/setup-python` caching, `buildx` `type=gha` cache and the `concurrency` group have no local equivalent. They change speed, not results.

## 14. Kept-Data Sweep Results

`python -m scripts.kept_data_sweep` at `8b79d80`, each run on a fresh `wasla_kd_kept` with nothing else on the server, schema `migrations`:

| Run | Window (UTC) | Collected | Passed | Failed | Skipped | Turns seen | Open holds left | Charged without outcome |
|---|---|---|---|---|---|---|---|---|
| 1 | 08:19:48–08:27:50 | 300 | 300 | 0 | 0 | 222 | 0 | 0 |
| 2 | 08:27:53–08:35:23 | 300 | 300 | 0 | 0 | 222 | 0 | 0 |
| 3 | 08:35:25–08:42:55 | 300 | 300 | 0 | 0 | 222 | 0 | 0 |

A fourth run (`head4`, 11:06–11:14) at `8b79d80` also gave 300 / 300, with no engaged turn holding a tool execution (§6). An earlier run (`fix1`, 07:28–07:35) on the uncommitted tree that became `569fc4c`..`9731bfb` also gave 300 / 300. The mutation runner's control sweep gave 300 / 300 as well. The kept sweep at `2c58c35` was not re-run with the new invariant files, because that commit keeps its old tests, which were already green (§3).

## 15. Model-Built Test Results

MODEL-BUILT (`WASLA_TEST_SCHEMA` unset), `E:\wasla-kept-data` at `8b79d80`, database `wasla_kd_models`, one uninterrupted run from 09:15:04 to 09:57:34 UTC (42 min, without `--cov`; see §13):

| Collected | Passed | Failed | Errors | Skipped | xfailed | xpassed |
|---|---|---|---|---|---|---|
| 6,819 | **6,803** | **0** | 0 | 16 | 0 | 0 |

The skips are the known set: 11 real-provider (no key), 4 schema parity (model lane), and 1 `WASLA_TEST_TMPFS`.

Compared with the merge verification's `fe12a37` lane (6,800 collected, 6,782 passed, 18 skipped):

- the 2 kept-only non-vacuity tests are now deselected, as `ci.yml` does, rather than skipped;
- the lane adds 18 invariant tests and 3 runner tests;
- 6,800 + 21 − 2 = 6,819, and 6,782 + 21 = 6,803.

An earlier run with `--cov` was stopped at 29%. Its processes were killed and verified gone before this run started.

## 16. Migration-Built Test Results

MIGRATION-BUILT (`WASLA_TEST_SCHEMA=migrations`, a fresh `alembic upgrade head` by the session fixture), database `wasla_kd_migrations`, one uninterrupted run from 09:58:44 to 10:43:01 UTC (44 min). This is the full suite, a superset of the job's `tests/integration tests/e2e` step, with the job's two deselects.

| Collected | Passed | Failed | Errors | Skipped | xfailed | xpassed |
|---|---|---|---|---|---|---|
| 6,819 | **6,807** | **0** | 0 | 12 | 0 | 0 |

The skips are 11 real-provider and 1 `WASLA_TEST_TMPFS`. Schema parity ran and passed, run on its own first as the job does, with 0 skipped. Compared with the baseline's 6,786 passed and 14 skipped: 6,786 + 21 = 6,807, and 14 − 2 deselected = 12.

The kept-data sweep (the job's last step) is in §14.

## 17. Targeted Families, Races, Time-Sensitive Test

All at `8b79d80`, database `wasla_kd_targeted` (models), nothing else running on the server:

| Family | Result |
|---|---|
| entitlements: AI turn charging, entitlement observability, entitlement invariants, oracle, capacity reductions, channel top-ups, channel capacity, entitlements API, channel not in plan, AI invariants, ledger bindings, the runner's unit test | 152 passed, 1 skipped (the kept-only non-vacuity check, outside kept mode) |
| `test_channel_capacity_concurrency` | 8 passed |
| `test_entitlement_races` | 4 passed |
| `test_grant_withdrawal_concurrency` | 3 passed |
| `test_settlement_concurrency` | 10 passed |
| `test_topup_concurrency` | 9 passed |
| oracle (`-s`) | `entitlement oracle comparisons: 168, over limit: {True: 125, False: 43}`, **0 mismatches** (the test asserts `mismatches == []`) |

`test_ten_overlapping_turns_against_three_charge_three_and_hand_off_seven` was run alone 5 times and passed 5 of 5. Session durations were 12.56 s, 12.20 s, 9.63 s, 8.61 s and 11.95 s; the test's own call in run 5 took 6.16 s.

**E01..E18:** `python -m scripts.omnichannel_invariants verify` ran on the `tests`-job database after migrations up / down / up. Every check read 0 (E01..E18 and the omnichannel set, 44 checks), ending `verify: ok`. Inside the lanes, `test_entitlement_invariants.py` passed in both schema modes.

## 18. Ruff / Black / MyPy

At `8b79d80`:

- `ruff check .`: all checks passed.
- `black --check .`: 901 files unchanged.
- `mypy app tests`: no issues in 789 source files.
- `mypy scripts`: no issues in 11 source files.

No `type: ignore` was added. The `noqa` comments added are `S608`, on module-constant SQL, following the existing ledger's convention, and `T201` on the scripts' CLI output.

## 19. Documentation Changes

| File | Change |
|---|---|
| `DECISIONS.md` | ADR-131 amended (2026-10-09) with B1..B7 and A1..A3; **ADR-133** records KD-01..KD-07 |
| `docs/RUNBOOK.md` | new "The kept-data sweep (CI's last migration-parity step) failed": how to run it locally, and a table of what each rule means and where to look |
| `docs/AI_AGENTS.md` | what the sweep holds the allowance to |
| `docs/OBSERVABILITY.md` | `hold_expired` is the deployment's counter, and how to read it |
| `README.md` | the sweep as a pre-push gate |

## 20. Decisions Ledger

| Item | Status |
|---|---|
| F-1a | **CLOSED** |
| F-1b | **CLOSED** |
| F-1c | **CLOSED** |
| R-EMPTY, R-ESCALATED, R-SUPPRESSED (legal) | **CLOSED BY DESIGN** |
| R-LATE (charge legal, missing completion artefact) | **CLOSED** |
| R-OPEN (artefact) | **CLOSED** |
| R-FAILED, R-UNHELD, R-COMPOSED-SUPPRESSED (legal, not counted) | **CLOSED BY DESIGN** |
| B6's count of a worker that died between charge and completion | **ACCEPTED RESIDUAL**: rare, already shown by AI-09, and worth a person's look |
| CI `quality` | **CLOSED** |
| CI `tests` | **CLOSED** (tmpfs test: EXTERNAL VERIFICATION) |
| CI `migration-parity` | **CLOSED** |
| CI `monitoring` | **CLOSED** |
| CI `docker` | **CLOSED** (first probe after the build unexplained and not reproduced in 3 runs) |
| `test_local_storage_cleanup.py` (tmpfs) | **EXTERNAL VERIFICATION**: needs CI's tmpfs mount |
| New findings | none. No CI job failed at `KD_BASE_HEAD` or at `8b79d80` for any reason unrelated to F-1. |

## 21. Remaining External Actions

Not performed by this stage:

1. Re-run the canonical merge verification with this branch as **stage 4**, from its own prompt ("WASLA — CANONICAL MERGE VERIFICATION (OMNICHANNEL, ENTITLEMENTS, PLATFORM OPERATIONS, KEPT-DATA FIX)").
2. Then merge in order and push: billing (already in canonical) → omnichannel remediation (`2c58c35`) → entitlements (`a7fc8b5`) → platform operations (`fe12a37`) → this branch. Until then origin lacks `aa11098`.
3. Deploy, following the RUNBOOK rule: stop old processes before `alembic upgrade` (0093 adds an enum label, 0097 drops four contacts columns).
4. Open entitlement items:
   - real prices on the six channel top-ups;
   - the placeholder plan values;
   - the capacity-reduction email wording;
   - Paymob Live;
   - real-provider verification per adapter.
5. `FRONTEND_MASTER_PLAN.md` and the other untracked documents in `E:\wasla`: commit them or back them up.
6. After review, remove this stage's containers (`wasla-kd-pg`, `-pg2`, `-redis`, `-redis2`, `-redis3`, `-minio`) and the detached worktrees `E:\wasla-kd-base-omni`, `E:\wasla-kd-base-ent` and `E:\wasla-kd-bisect`.
7. The first GitHub run on push is the final proof of the tmpfs test and the cached-build mechanics (§13).

## 22. Final Verdict

**KEPT-DATA SWEEP F-1 CLOSED WITH EXTERNAL ACTIONS REMAINING**

**The question.** If the canonical merge verification were re-run tomorrow with this branch as stage 4, and the branch then pushed, would every GitHub Actions job go green on the first run?

**Answer: yes, as far as a local run can show.** Every job in `ci.yml` was run here at `8b79d80` with CI's selection, schema modes, deselects and pass rules, and each was green. Two things only GitHub can prove: the tmpfs-bound storage test, and the build cache mechanics.

**The second question.** If the application ever started charging a customer for an AI turn it should not, or forgot to charge one it should, would the kept-data sweep be the first thing to say so?

**Answer: yes, it would.** It is one of two independent nets, the other being the named ENT-02 tests:

- Charging an empty answer turned the sweep red with 7 B6 rows.
- Charging at engagement turned it red with 20.
- A sweeper that never expires a hold turned it red with an A row.
- A turn charged twice, a reply left uncharged, a charge with no turn and a hold left open are each proven by an injection to be counted by exactly one rule.

None of this needed a test to be weakened, a suite to be excluded, or the application to change.

## Required Final Runtime Matrix

| Scenario | Before | After | Verdict |
|---|---|---|---|
| CI kept-data sweep at the branch head | 3 failed (282 / 279) | 0 failed, 300 / 300, 3 runs | PASS |
| Engaged turns stranded (F-1a) | 4 rows | 0; an unsettled hold injected → 1 | PASS |
| Charge state inconsistent (F-1b) | 14 workspaces | 0; each of B1..B7 injected → 1 (exactly that rule) | PASS |
| Expired hold counted (F-1c) | counter 2, expected 1 | counter = holds its own pass released (2 normal; ≥2 kept), its own included | PASS |
| Application charges an empty answer (mutant) | — | named test fails, and the sweep counts 7 rows under B6 | PASS |
| Application charges at engagement (mutant) | — | named test fails, and the sweep counts 20 rows under B6 | PASS |
| Sweeper never expires a hold (mutant) | — | named test fails, and the sweep counts the abandoned hold under A | PASS |
| Runner selection vs ci.yml | not checked | equal, proven by a unit test (M-K10 killed) | PASS |
| Every CI job run locally | two lanes only | 5 jobs listed and run; tmpfs test and caches GitHub-only | PASS |

## Required Numbers

| Item | Value |
|---|---|
| F-1 at `fe12a37` | 282 / 279 / 3; F-1a 4, F-1b 14, F-1c 2 ≠ 1 |
| F-1 at `a7fc8b5` | 268 / 265 / 3; the same three |
| F-1 at `2c58c35` | 232 / 232 / 0 |
| Row classes | F-1b: R-EMPTY 7, R-ESCALATED 2, R-SUPPRESSED 4, R-LATE 1 (= 14); F-1a: R-OPEN 2, R-LATE 2 (= 4); not counted, legal: R-FAILED 18, R-UNHELD 32, R-COMPOSED-SUPPRESSED 8 |
| Injection tests | 18 added (11 positive, 5 negative, 2 structural); each positive 0 → 1 on exactly one check |
| Mutations | 13 total, 13 killed, 0 survived, 13 restored; counted rows M-K06 7 (B6), M-K07 20 (B6), M-K08 1 (A) |
| Kept-data sweep at `8b79d80` | 3 runs × 300 / 300 / 0 / 0; 222 turns, 155 charges per run |
| Model-built lane | 6,819 / 6,803 / 0 / 16 / 0 xfailed / 0 xpassed (models) |
| Migration-built lane | 6,819 / 6,807 / 0 / 12 / 0 xfailed / 0 xpassed (migrations) |
| Time-sensitive test | 5 / 5 alone: 12.56, 12.20, 9.63, 8.61, 11.95 s per session |
| CI jobs | 5 listed, 5 run locally, 5 green; GitHub-only: tmpfs test, caches |
| E01..E18 | E01..E18 all 0 (44 checks, `verify: ok`) |
| Oracle | 168 comparisons, 0 mismatches |
